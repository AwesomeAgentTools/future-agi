package otel

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"
)

const (
	// otlpTracesPath is appended when the configured endpoint carries no path
	// of its own, so both "http://collector:4318" and a full URL work.
	otlpTracesPath  = "/v1/traces"
	otlpContentType = "application/x-protobuf"

	// scopeName identifies the instrumentation that produced these spans.
	scopeName = "github.com/futureagi/agentcc-gateway"

	// resourceAttrPrefix marks span attributes that are really resource-level.
	// They are emitted on the OTLP Resource instead and skipped on the span.
	resourceAttrPrefix = "resource."

	otlpBatchSize     = 512              // flush as soon as this many spans are queued
	otlpFlushInterval = 5 * time.Second  // ...and at least this often
	otlpMaxQueue      = 4096             // hard ceiling; spans past it are dropped
	otlpHTTPTimeout   = 10 * time.Second // per-request budget for the collector
	otlpMaxRetries    = 3                // consecutive failures before dropping a batch
)

// OTLPExporter ships spans to an OpenTelemetry collector over OTLP/HTTP with
// protobuf encoding.
//
// Export never blocks on the network: spans are queued and flushed by a
// background goroutine, because Export runs on the request path and a slow or
// unreachable collector must not add latency to LLM traffic. A full queue drops
// spans rather than applying backpressure — telemetry is not worth stalling a
// request for.
type OTLPExporter struct {
	url      string
	client   *http.Client
	resource *resourcepb.Resource
	headers  map[string]string

	mu               sync.Mutex
	buffer           []*Span
	consecutiveFails int

	dropped atomic.Int64

	stopCh   chan struct{}
	doneCh   chan struct{}
	stopOnce sync.Once
}

// OTLPOptions configures an OTLPExporter. Resource and Headers are both
// string maps, so they are named rather than positional.
type OTLPOptions struct {
	// Endpoint is the collector base URL. "/v1/traces" is appended when it
	// carries no path of its own.
	Endpoint string

	// ServiceName becomes the service.name resource attribute.
	ServiceName string

	// Resource holds additional OTLP resource attributes.
	Resource map[string]string

	// Headers are sent on every export request. Hosted collectors
	// authenticate this way; values are credentials and are never logged.
	Headers map[string]string
}

// NewOTLPExporter creates an exporter posting to opts.Endpoint and starts its
// background flusher.
func NewOTLPExporter(opts OTLPOptions) (*OTLPExporter, error) {
	u, err := url.Parse(opts.Endpoint)
	if err != nil {
		return nil, fmt.Errorf("otlp endpoint %q: %w", opts.Endpoint, err)
	}
	if u.Scheme == "" || u.Host == "" {
		return nil, fmt.Errorf("otlp endpoint %q must be an absolute http(s) URL", opts.Endpoint)
	}
	if u.Path == "" || u.Path == "/" {
		u.Path = otlpTracesPath
	}

	headers := make(map[string]string, len(opts.Headers))
	for k, v := range opts.Headers {
		headers[k] = v
	}

	e := &OTLPExporter{
		url:      u.String(),
		client:   &http.Client{Timeout: otlpHTTPTimeout},
		resource: buildResource(opts.ServiceName, opts.Resource),
		headers:  headers,
		buffer:   make([]*Span, 0, otlpBatchSize),
		stopCh:   make(chan struct{}),
		doneCh:   make(chan struct{}),
	}
	go e.run()
	return e, nil
}

// run flushes on a ticker until Shutdown, then flushes whatever is left.
func (e *OTLPExporter) run() {
	defer close(e.doneCh)
	ticker := time.NewTicker(otlpFlushInterval)
	defer ticker.Stop()

	for {
		select {
		case <-e.stopCh:
			e.flush()
			return
		case <-ticker.C:
			e.flush()
		}
	}
}

// Export queues spans for delivery. It does not block on the collector.
func (e *OTLPExporter) Export(spans []*Span) error {
	if len(spans) == 0 {
		return nil
	}

	e.mu.Lock()
	room := otlpMaxQueue - len(e.buffer)
	if room <= 0 {
		e.mu.Unlock()
		total := e.dropped.Add(int64(len(spans)))
		slog.Warn("otlp exporter: queue full, dropping spans",
			"dropped", len(spans), "dropped_total", total)
		return nil
	}
	if len(spans) > room {
		total := e.dropped.Add(int64(len(spans) - room))
		slog.Warn("otlp exporter: queue full, dropping excess spans",
			"queued", room, "dropped", len(spans)-room, "dropped_total", total)
		spans = spans[:room]
	}
	e.buffer = append(e.buffer, spans...)
	full := len(e.buffer) >= otlpBatchSize
	e.mu.Unlock()

	if full {
		go e.flush()
	}
	return nil
}

// Shutdown stops the background flusher and sends anything still queued.
func (e *OTLPExporter) Shutdown() error {
	e.stopOnce.Do(func() { close(e.stopCh) })

	select {
	case <-e.doneCh:
	case <-time.After(otlpHTTPTimeout + otlpFlushInterval):
		slog.Warn("otlp exporter: shutdown timed out waiting for final flush")
	}
	if n := e.dropped.Load(); n > 0 {
		slog.Warn("otlp exporter: spans dropped over process lifetime", "dropped_total", n)
	}
	return nil
}

// Dropped reports how many spans were discarded because the queue was full.
func (e *OTLPExporter) Dropped() int64 { return e.dropped.Load() }

// flush sends one batch. Retry semantics match the request-log flusher:
// transport failures, 5xx and 429 are re-queued until otlpMaxRetries
// consecutive failures; every other 4xx is dropped immediately because
// retrying sends the same bytes to the same complaint.
func (e *OTLPExporter) flush() {
	e.mu.Lock()
	if len(e.buffer) == 0 {
		e.mu.Unlock()
		return
	}
	spans := e.buffer
	e.buffer = make([]*Span, 0, otlpBatchSize)
	e.mu.Unlock()

	body, err := proto.Marshal(e.encode(spans))
	if err != nil {
		// The batch is gone: count it, or the one drop path that takes out
		// spans which were themselves fine stays invisible to Dropped().
		e.dropped.Add(int64(len(spans)))
		slog.Error("otlp exporter: marshal failed, dropping batch", "error", err, "count", len(spans))
		return
	}

	req, err := http.NewRequest(http.MethodPost, e.url, bytes.NewReader(body))
	if err != nil {
		slog.Error("otlp exporter: create request failed", "error", err)
		return
	}
	req.Header.Set("Content-Type", otlpContentType)
	for k, v := range e.headers {
		req.Header.Set(k, v)
	}

	resp, err := e.client.Do(req)
	if err != nil {
		e.retry(spans, "send failed", slog.Any("error", err))
		return
	}
	defer resp.Body.Close()

	switch {
	case resp.StatusCode >= 500:
		e.retry(spans, "collector server error", slog.Int("status", resp.StatusCode))
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		// Called out separately: every span will be discarded for as long as
		// the credential is wrong, and nothing else in the gateway looks broken.
		e.mu.Lock()
		e.consecutiveFails = 0
		e.mu.Unlock()
		e.dropped.Add(int64(len(spans)))
		slog.Error("otlp exporter: collector rejected the credentials, dropping spans — check otel.headers",
			"status", resp.StatusCode, "count", len(spans), "url", e.url,
			"header_names", headerNames(e.headers))
	case resp.StatusCode == http.StatusTooManyRequests:
		// Overloaded, not wrong. Retryable by spec; the flush interval is the
		// backoff, and otlpMaxRetries still bounds how long we hold the batch.
		e.retry(spans, "collector rate limited", slog.Int("status", resp.StatusCode))
	case resp.StatusCode >= 400:
		// Malformed payload or wrong endpoint — a retry sends the same bytes.
		e.mu.Lock()
		e.consecutiveFails = 0
		e.mu.Unlock()
		e.dropped.Add(int64(len(spans)))
		slog.Error("otlp exporter: collector rejected batch, dropping spans",
			"status", resp.StatusCode, "count", len(spans), "url", e.url)
	default:
		e.mu.Lock()
		e.consecutiveFails = 0
		e.mu.Unlock()
		slog.Debug("otlp exporter: sent spans", "count", len(spans), "status", resp.StatusCode)
	}
}

// retry re-queues a failed batch, or drops it once the collector has failed
// otlpMaxRetries times in a row.
func (e *OTLPExporter) retry(spans []*Span, reason string, attr slog.Attr) {
	e.mu.Lock()
	e.consecutiveFails++
	fails := e.consecutiveFails
	if fails > otlpMaxRetries {
		e.consecutiveFails = 0
		e.mu.Unlock()
		e.dropped.Add(int64(len(spans)))
		slog.Error("otlp exporter: max retries exceeded, dropping spans",
			"reason", reason, "count", len(spans), "consecutive_failures", fails, attr)
		return
	}

	room := otlpMaxQueue - len(e.buffer)
	if room <= 0 {
		e.mu.Unlock()
		e.dropped.Add(int64(len(spans)))
		slog.Warn("otlp exporter: queue full on re-enqueue, dropping spans",
			"reason", reason, "dropped", len(spans))
		return
	}
	dropped := 0
	if len(spans) > room {
		dropped = len(spans) - room
		spans = spans[:room]
	}
	// Failed spans go to the front so the oldest telemetry still leaves first.
	e.buffer = append(spans, e.buffer...)
	e.mu.Unlock()

	if dropped > 0 {
		e.dropped.Add(int64(dropped))
	}
	slog.Warn("otlp exporter: re-enqueuing spans",
		"reason", reason, "count", len(spans), "dropped", dropped, "retry", fails, attr)
}

// encode converts spans into an OTLP TracesData message.
//
// TracesData is wire-identical to the collector's ExportTraceServiceRequest —
// both are a single repeated ResourceSpans field 1 — so it can be posted as the
// OTLP/HTTP body directly. Using it avoids importing the collector service
// package, which would drag gRPC into a binary that has no other use for it.
func (e *OTLPExporter) encode(spans []*Span) *tracepb.TracesData {
	out := make([]*tracepb.Span, 0, len(spans))
	for _, s := range spans {
		if s == nil {
			continue
		}
		out = append(out, encodeSpan(s))
	}

	return &tracepb.TracesData{
		ResourceSpans: []*tracepb.ResourceSpans{{
			Resource: e.resource,
			ScopeSpans: []*tracepb.ScopeSpans{{
				Scope: &commonpb.InstrumentationScope{Name: scopeName},
				Spans: out,
			}},
		}},
	}
}

func encodeSpan(s *Span) *tracepb.Span {
	spanID := normalizeID(s.SpanID, 8)
	traceSeed := s.TraceID
	if traceSeed == "" {
		// Degenerate case: keep the span on its own trace rather than
		// collapsing every ID-less span onto one shared hash.
		traceSeed = s.SpanID
	}

	attrs := make([]*commonpb.KeyValue, 0, len(s.Attributes)+1)
	for k, v := range s.Attributes {
		if strings.HasPrefix(k, resourceAttrPrefix) {
			continue // already on the Resource
		}
		attrs = append(attrs, keyValue(k, v))
	}
	// The gateway's own trace ID is what appears in response headers and in the
	// request-log table. Carry it verbatim so a span can be tied back to a log
	// row even though the OTLP trace ID is a hash of it.
	if s.TraceID != "" {
		attrs = append(attrs, keyValue("agentcc.trace_id", s.TraceID))
	}

	pb := &tracepb.Span{
		TraceId:           normalizeID(traceSeed, 16),
		SpanId:            spanID,
		Name:              s.Name,
		Kind:              tracepb.Span_SPAN_KIND_CLIENT,
		StartTimeUnixNano: uint64(s.StartTime.UnixNano()),
		Attributes:        attrs,
	}
	if s.ParentID != "" {
		pb.ParentSpanId = normalizeID(s.ParentID, 8)
	}
	if !s.EndTime.IsZero() {
		pb.EndTimeUnixNano = uint64(s.EndTime.UnixNano())
	}

	if s.Status == StatusError.String() {
		pb.Status = &tracepb.Status{Code: tracepb.Status_STATUS_CODE_ERROR}
		if msg, ok := s.Attributes["error.message"].(string); ok {
			pb.Status.Message = msg
		}
	} else {
		pb.Status = &tracepb.Status{Code: tracepb.Status_STATUS_CODE_OK}
	}

	return pb
}

// normalizeID turns an arbitrary identifier into the fixed-width byte ID that
// OTLP requires.
//
// Span IDs are already hex, but trace IDs are not: the gateway propagates a
// caller-supplied or generated ULID in x-agentcc-trace-id, which is neither hex
// nor 16 bytes. Hashing is deterministic, so every span of one gateway trace
// still lands on one OTLP trace, and the original value travels as the
// agentcc.trace_id attribute.
func normalizeID(id string, size int) []byte {
	if len(id) == size*2 {
		if b, err := hex.DecodeString(id); err == nil {
			return b
		}
	}
	sum := sha256.Sum256([]byte(id))
	return sum[:size]
}

// headerNames lists configured header names for diagnostics. Names only —
// the values are credentials and must never reach a log.
func headerNames(headers map[string]string) []string {
	names := make([]string, 0, len(headers))
	for k := range headers {
		names = append(names, k)
	}
	sort.Strings(names)
	return names
}

func buildResource(serviceName string, attrs map[string]string) *resourcepb.Resource {
	if serviceName == "" {
		serviceName = "agentcc-gateway"
	}
	kvs := make([]*commonpb.KeyValue, 0, len(attrs)+1)
	kvs = append(kvs, keyValue("service.name", serviceName))
	for k, v := range attrs {
		if k == "service.name" {
			continue // set explicitly above
		}
		kvs = append(kvs, keyValue(k, v))
	}
	return &resourcepb.Resource{Attributes: kvs}
}

func keyValue(k string, v interface{}) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: k, Value: anyValue(v)}
}

func anyValue(v interface{}) *commonpb.AnyValue {
	switch t := v.(type) {
	case string:
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: t}}
	case bool:
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_BoolValue{BoolValue: t}}
	case int:
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: int64(t)}}
	case int32:
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: int64(t)}}
	case int64:
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: t}}
	case float32:
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_DoubleValue{DoubleValue: float64(t)}}
	case float64:
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_DoubleValue{DoubleValue: t}}
	default:
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: fmt.Sprint(t)}}
	}
}
