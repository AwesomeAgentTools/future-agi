-- =============================================================================
-- 022 — Bloom-filter indexes on attribute map VALUES
-- =============================================================================
--
-- SPAN_ATTRIBUTE equality filters (attrs_number['customer_id'] = X) can only
-- lean on idx_attrs_*_keys today, and a KEY bloom prunes nothing when most
-- spans carry the key — evaluating the VALUE then decompresses the entire map
-- column for every row in the window. On the largest tenant that is a 19+ GiB
-- read per filtered trace-list request, which is what pushed those queries
-- past the 10s execution budget (Code 159).
--
-- A bloom over mapValues answers "does this granule contain the value at
-- all", which prunes aggressively for high-cardinality values (customer ids,
-- session ids, order numbers). Equality and IN only — ranges and negations
-- cannot use it. Same layout as the OpenTelemetry ClickHouse exporter's
-- idx_span_attr_value and SigNoz's idx_numberTagMapValues.
--
-- attrs_bool deliberately gets no value index: its value space is {0,1}, so
-- every granule contains both and nothing could ever be skipped.
--
-- ADD INDEX is metadata-only; parts written after it are indexed on
-- insert/merge. MATERIALIZE INDEX backfills existing parts as a background
-- mutation (reads the map column once per replica: ~4 GiB compressed for
-- attrs_number, ~70 GiB for attrs_string) — non-blocking, tracked in
-- system.mutations, abortable with KILL MUTATION, reversible via DROP INDEX.

ALTER TABLE spans
    ADD INDEX IF NOT EXISTS idx_attrs_str_values mapValues(attrs_string)
    TYPE bloom_filter(0.01) GRANULARITY 1;

ALTER TABLE spans
    ADD INDEX IF NOT EXISTS idx_attrs_num_values mapValues(attrs_number)
    TYPE bloom_filter(0.01) GRANULARITY 1;

ALTER TABLE spans MATERIALIZE INDEX idx_attrs_str_values;

ALTER TABLE spans MATERIALIZE INDEX idx_attrs_num_values;
