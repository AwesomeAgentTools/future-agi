package otel

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/futureagi/agentcc-gateway/internal/config"
	"github.com/futureagi/agentcc-gateway/internal/models"
	"github.com/futureagi/agentcc-gateway/internal/privacy"
	"github.com/futureagi/agentcc-gateway/internal/tenant"
)

func bodyPlugin(t *testing.T, include bool) (*Plugin, *captureExporter) {
	t.Helper()
	exp := &captureExporter{}
	p := New(config.OTelConfig{Enabled: true, Exporter: "stdout", IncludeBodies: include})
	p.exporter = exp
	return p, exp
}

func runOnce(p *Plugin, rc *models.RequestContext) map[string]interface{} {
	ctx := context.Background()
	p.ProcessRequest(ctx, rc)
	p.ProcessResponse(ctx, rc)
	return p.exporter.(*captureExporter).Spans()[0].Attributes
}

func rcWithBodies() *models.RequestContext {
	rc := newTestRC()
	content := json.RawMessage(`"my password is hunter2"`)
	rc.Request.Messages = []models.Message{{Role: "user", Content: content}}
	out := json.RawMessage(`"the password is hunter2"`)
	rc.Response.Choices = []models.Choice{{Index: 0, Message: models.Message{Role: "assistant", Content: out}}}
	return rc
}

// input.value / output.value are the keys the platform lifts into its input and
// output columns. A rename here shows up as blank columns, not an error.
func TestBodiesUseTheConventionalKeys(t *testing.T) {
	p, _ := bodyPlugin(t, true)
	attrs := runOnce(p, rcWithBodies())

	in, ok := attrs["input.value"].(string)
	if !ok || !strings.Contains(in, "hunter2") {
		t.Fatalf("input.value = %v", attrs["input.value"])
	}
	if !json.Valid([]byte(in)) {
		t.Error("input.value is not valid JSON")
	}
	out, ok := attrs["output.value"].(string)
	if !ok || !strings.Contains(out, "hunter2") {
		t.Fatalf("output.value = %v", attrs["output.value"])
	}
	if attrs["input.mime_type"] != "application/json" || attrs["output.mime_type"] != "application/json" {
		t.Error("mime types not set")
	}
}

// The knob is the whole privacy contract: off means no content leaves.
func TestBodiesAbsentUnlessEnabled(t *testing.T) {
	p, _ := bodyPlugin(t, false)
	attrs := runOnce(p, rcWithBodies())

	for _, k := range []string{"input.value", "output.value", "input.mime_type", "output.mime_type"} {
		if v, ok := attrs[k]; ok {
			t.Errorf("%s present with include_bodies off: %v", k, v)
		}
	}
}

// Redaction must apply to spans exactly as it applies to the request log,
// otherwise trace export is a way around a configured privacy policy.
func TestBodiesRedactedWithOrgPatterns(t *testing.T) {
	store := tenant.NewStore()
	store.Set("org-1", &tenant.OrgConfig{Privacy: &tenant.PrivacyConfig{
		Enabled:  true,
		Mode:     "patterns",
		Patterns: []*tenant.RedactPatternConfig{{Name: "pw", Pattern: "hunter2"}},
	}})

	p, _ := bodyPlugin(t, true)
	p.SetTenantStore(store)

	rc := rcWithBodies()
	rc.Metadata[tenant.MetadataKeyOrgID] = "org-1"
	attrs := runOnce(p, rc)

	if in := attrs["input.value"].(string); strings.Contains(in, "hunter2") {
		t.Errorf("org pattern not applied to input.value: %s", in)
	}
	if out := attrs["output.value"].(string); strings.Contains(out, "hunter2") {
		t.Errorf("org pattern not applied to output.value: %s", out)
	}
}

// With no org config, the gateway-wide redactor still applies.
func TestBodiesRedactedWithGlobalRedactor(t *testing.T) {
	p, _ := bodyPlugin(t, true)
	p.SetRedactor(privacy.New("patterns", []privacy.PatternConfig{{Name: "pw", Pattern: "hunter2"}}))

	attrs := runOnce(p, rcWithBodies())
	if in := attrs["input.value"].(string); strings.Contains(in, "hunter2") {
		t.Errorf("global redactor not applied: %s", in)
	}
}

// Truncation happens after redaction, and the cap is what makes the order
// observable: a secret straddling the boundary is cut in half by
// truncate-first, stops matching the pattern, and its leading half is
// exported. Redact-first replaces the whole match before any cutting.
func TestBodiesRedactBeforeTruncate(t *testing.T) {
	const secret = "SECRET-1234567890"
	r := privacy.New("patterns", []privacy.PatternConfig{{Name: "s", Pattern: "SECRET-[0-9]{10}"}})

	// Position the secret so the cap falls 12 characters into it.
	filler := strings.Repeat("x", maxBodyAttrBytes-12)
	got := prepareBody(filler+secret, r, "patterns")

	if strings.Contains(got.value, "SECRET-12345") {
		t.Errorf("leading half of the secret survived — truncation ran before redaction")
	}
	if strings.Contains(got.value, secret) {
		t.Error("secret not redacted at all")
	}
	if len(got.value) > maxBodyAttrBytes {
		t.Errorf("value length %d exceeds the cap %d", len(got.value), maxBodyAttrBytes)
	}
}

func TestBodiesTruncatedAndFlagged(t *testing.T) {
	p, _ := bodyPlugin(t, true)
	rc := newTestRC()
	huge, _ := json.Marshal(strings.Repeat("x", maxBodyAttrBytes+5000))
	rc.Request.Messages = []models.Message{{Role: "user", Content: json.RawMessage(huge)}}
	rc.Response = nil

	attrs := runOnce(p, rc)
	in := attrs["input.value"].(string)
	if len(in) != maxBodyAttrBytes {
		t.Fatalf("input.value length = %d, want the cap %d", len(in), maxBodyAttrBytes)
	}
	if attrs["agentcc.input_truncated"] != true {
		t.Error("truncation not flagged")
	}
	if attrs["agentcc.input_original_bytes"] == nil {
		t.Error("original size not recorded")
	}
	// Truncated JSON is not parseable; saying so is what stops a consumer trying.
	if _, ok := attrs["input.mime_type"]; ok {
		t.Error("truncated value must not claim to be application/json")
	}
}

// The streaming husk — a response with usage but no choices — must not be
// exported as an empty completion.
func TestBodiesSkipEmptyStreamingResponse(t *testing.T) {
	p, _ := bodyPlugin(t, true)
	rc := rcWithBodies()
	rc.Response = &models.ChatCompletionResponse{Model: "gpt-4o"}

	attrs := runOnce(p, rc)
	if v, ok := attrs["output.value"]; ok {
		t.Errorf("exported an output for a response with no choices: %v", v)
	}
	if _, ok := attrs["input.value"]; !ok {
		t.Error("input should still be captured")
	}
}
