-- =============================================================================
-- 023 — ngram bloom for substring (ILIKE) search over attribute values
-- =============================================================================
--
-- The value picker's search (`attrs_string[k] ILIKE '%q%'`) has no usable
-- index: the 022 value blooms hash whole values (equality only). Measured: a
-- search reads the same I/O as a full enumeration (48.79 GiB / 7-day window
-- on the largest tenant).
--
-- Two requirements, verified empirically on 25.3:
--   1. The ILIKE alone never engages the index (40/40 granules). The query
--      must AND a companion in the index's exact expression:
--        arrayStringConcat(arrayMap(x -> lower(x), mapValues(attrs_string)))
--            LIKE '%<lowered needle>%'
--      With it: 1/40 granules. Emitted by DashboardViewSet.filter_values —
--      keep the two expressions byte-identical or the index silently
--      disengages (same contract as 022's lowered bloom).
--   2. Needles < 4 chars (the ngram size) cannot prune; harmless otherwise.
--
-- Sizing: measured ~43k distinct lowered 4-grams per 8192-row granule on the
-- largest tenant; 32768 bytes ≈ 6% fpp, ~3 GiB total (1024 would saturate).
-- Selectivity is global, not per-key: common needles prune little,
-- distinctive ones (ids, slugs) prune hard. Never add the companion to
-- negated ops.
--
-- MATERIALIZE reads attrs_string once per replica (~70 GiB compressed on US).
-- Rollout: US schema_versions has no 022 row (applied out of band) — record
-- it or ship with --files; coordinate numbering with the spans_v3 branch,
-- which also claims 022/023.

ALTER TABLE spans
    ADD INDEX IF NOT EXISTS idx_attrs_str_ngram
    arrayStringConcat(arrayMap(x -> lower(x), mapValues(attrs_string)))
    TYPE ngrambf_v1(4, 32768, 3, 0) GRANULARITY 1;

ALTER TABLE spans MATERIALIZE INDEX idx_attrs_str_ngram;
