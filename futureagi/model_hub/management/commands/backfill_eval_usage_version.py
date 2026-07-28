"""
Backfill version info on existing APICallLog entries for user eval templates.

Standalone replacement for the expensive part of migration
``0115_eval_usage_version_backfill``. That migration stamps every
APICallLog row for every user template — O(usage-log rows), potentially
millions — and since migrations run automatically on every pod during
deploy, it blocks that pod's rollout for its full duration.

This command does the same work but as a manual, resumable, idempotent
step: run it once, on a single pod, after deploy — never as part of the
automatic migration path.

Optimizations over the migration's logic (same semantics, faster plan):
  - SINGLE keyset-paginated pass (``id > last_seen``) over the table that
    unwraps double-encoded string configs AND stamps the version in the
    same UPDATE. The migration walked the table twice (unwrap pass, then
    stamp pass) and re-searched from the start of the table on every batch.
  - Everything happens server-side in SQL (``config || jsonb_build_object``)
    for ALL templates at once via a VALUES join, instead of hydrating every
    row into Python and writing back with bulk_update — and instead of one
    full table scan per template (source_id alone has no usable index on
    usage_apicalllog).

Usage:
    python manage.py backfill_eval_usage_version                 # full run
    python manage.py backfill_eval_usage_version --dry-run       # counts only
    python manage.py backfill_eval_usage_version --chunk-size 5000
    python manage.py backfill_eval_usage_version --sleep 0.1     # throttle
    python manage.py backfill_eval_usage_version --template-id <uuid>
"""

import json
import time
from typing import Callable, Optional

import structlog
from django.core.management.base import BaseCommand

logger = structlog.get_logger(__name__)

# One batch of the combined unwrap+stamp pass.
#
# Row selection (the ``batch`` CTE) picks, in id order above the keyset
# cursor, every row that still needs work:
#   - double-encoded configs (JSONB string wrapping an object/array), or
#   - object configs missing version_id whose source is a user template.
# ``norm_config`` is the config with the string wrapper removed.
#
# The UPDATE then stamps version info only where it applies (object config,
# no version_id yet, source has a mapped version — LEFT JOIN, so unwrap-only
# rows such as arrays, system-template logs, or already-stamped strings are
# still normalized without being stamped).
_BACKFILL_BATCH_SQL = """
WITH batch AS (
    SELECT id, source_id,
           CASE WHEN jsonb_typeof(config) = 'string'
                THEN (config #>> '{{}}')::jsonb
                ELSE config END AS norm_config
    FROM usage_apicalllog
    WHERE id > %s
      AND deleted = false
      AND (
        (jsonb_typeof(config) = 'string'
         AND LEFT(config #>> '{{}}', 1) IN ('{{', '['))
        OR
        (source_id = ANY(%s)
         AND jsonb_typeof(config) = 'object'
         AND COALESCE(config ->> 'version_id', '') = '')
      )
    ORDER BY id
    LIMIT %s
)
UPDATE usage_apicalllog AS l
SET config = CASE
    WHEN jsonb_typeof(b.norm_config) = 'object'
         AND COALESCE(b.norm_config ->> 'version_id', '') = ''
         AND m.version_id IS NOT NULL
    THEN b.norm_config || jsonb_build_object(
             'version_id', m.version_id,
             'version_number', m.version_number)
    ELSE b.norm_config
END
FROM batch AS b
LEFT JOIN (VALUES {values_sql}) AS m(source_id, version_id, version_number)
    ON b.source_id = m.source_id
WHERE l.id = b.id
RETURNING l.id
"""


def _ensure_default_version(template, EvalTemplateVersion):
    """Return the template's default version, creating v1 if none exists yet."""
    version = (
        EvalTemplateVersion.objects.filter(
            eval_template_id=template.id, is_default=True, deleted=False
        )
        .order_by("-version_number")
        .first()
    )
    if not version:
        version = (
            EvalTemplateVersion.objects.filter(
                eval_template_id=template.id, deleted=False
            )
            .order_by("-version_number")
            .first()
        )
    if version:
        return version
    try:
        return EvalTemplateVersion.objects.create(
            eval_template_id=template.id,
            version_number=1,
            is_default=True,
            prompt_messages=[],
            config_snapshot=template.config or {},
            criteria=template.criteria or "",
            model=template.model or "",
            organization_id=template.organization_id,
            workspace_id=template.workspace_id,
            output_type_normalized=getattr(template, "output_type_normalized", None),
            pass_threshold=getattr(template, "pass_threshold", None),
            choice_scores=getattr(template, "choice_scores", None),
            error_localizer_enabled=getattr(
                template, "error_localizer_enabled", False
            ),
            eval_tags=list(getattr(template, "eval_tags", []) or []),
        )
    except Exception:
        logger.warning(
            "ensure_default_version_failed", template_id=str(template.id), exc_info=True
        )
        return None


def _build_version_mapping(only_template: Optional[str] = None) -> dict:
    """Map template id (str) → (version_id, version_number), creating missing v1s."""
    from model_hub.models.evals_metric import EvalTemplate, EvalTemplateVersion

    templates = EvalTemplate.objects.filter(deleted=False, owner="user")
    if only_template:
        templates = templates.filter(id=only_template)

    mapping = {}
    for template in templates.iterator():
        version = _ensure_default_version(template, EvalTemplateVersion)
        if version:
            mapping[str(template.id)] = (str(version.id), version.version_number)
    return mapping


def _backfill_pass_sql(
    connection,
    mapping: dict,
    batch: int,
    sleep_s: float,
    emit: Callable[[str], None],
) -> int:
    """One keyset-paginated pass: unwrap + stamp together, all server-side."""
    # LEFT JOIN needs at least one VALUES row; a NULL row matches nothing.
    if mapping:
        values_sql = ", ".join(["(%s, %s, %s)"] * len(mapping))
        values_params = []
        for source_id, (version_id, version_number) in mapping.items():
            values_params.extend([source_id, version_id, version_number])
    else:
        values_sql = "(NULL::text, NULL::text, NULL::int)"
        values_params = []
    source_ids = list(mapping.keys())
    sql = _BACKFILL_BATCH_SQL.format(values_sql=values_sql)

    total = 0
    last_id = 0
    while True:
        with connection.cursor() as cursor:
            cursor.execute(sql, [last_id, source_ids, batch] + values_params)
            ids = [row[0] for row in cursor.fetchall()]
        if not ids:
            break
        last_id = max(ids)
        total += len(ids)
        emit(f"  processed batch of {len(ids)} (total={total}, last_id={last_id})")
        if sleep_s:
            time.sleep(sleep_s)
    return total


def _backfill_pass_python(
    APICallLog,
    mapping: dict,
    chunk_size: int,
    sleep_s: float,
    emit: Callable[[str], None],
) -> int:
    """Non-Postgres fallback: per-row stamping through the ORM."""
    total = 0
    for source_id, (version_id, version_number) in mapping.items():
        logs = APICallLog.objects.filter(source_id=source_id, deleted=False)
        batch = []
        for row in logs.iterator(chunk_size=chunk_size):
            config = row.config
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(config, dict) or config.get("version_id"):
                continue
            config["version_id"] = version_id
            config["version_number"] = version_number
            row.config = config
            batch.append(row)
            if len(batch) >= chunk_size:
                APICallLog.objects.bulk_update(batch, ["config"])
                total += len(batch)
                batch = []
        if batch:
            APICallLog.objects.bulk_update(batch, ["config"])
            total += len(batch)
        if sleep_s:
            time.sleep(sleep_s)
    emit(f"  processed {total} rows (python fallback)")
    return total


def backfill_usage_logs(
    chunk_size: int = 5000,
    sleep_s: float = 0.0,
    only_template: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """Unwrap double-encoded configs and stamp untagged APICallLog rows with
    their template's default version — one combined pass.

    Idempotent — re-runs (or interrupted runs resumed) only touch rows still
    needing work. Safe to call from the command or a shell.
    """
    emit = log or (lambda _msg: None)

    try:
        from ee.usage.models.usage import APICallLog
    except ImportError:
        emit("usage app not installed (OSS build) — nothing to backfill.")
        return {"updated": 0}

    from django.db import connection

    mapping = _build_version_mapping(only_template)
    emit(f"Templates to stamp: {len(mapping)}")

    if connection.vendor == "postgresql":
        total = _backfill_pass_sql(connection, mapping, chunk_size, sleep_s, emit)
    else:
        total = _backfill_pass_python(APICallLog, mapping, chunk_size, sleep_s, emit)

    emit(f"Backfill complete: rows updated={total}")
    return {"updated": total}


class Command(BaseCommand):
    help = (
        "Backfill APICallLog.config.version_id for user eval templates. "
        "Run once, on a single pod, after deploy — not part of any migration."
    )

    def add_arguments(self, parser):
        parser.add_argument("--chunk-size", type=int, default=5000)
        parser.add_argument("--sleep", type=float, default=0.0)
        parser.add_argument("--template-id", type=str, default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        if opts["dry_run"]:
            self._dry_run(opts["template_id"])
            return
        backfill_usage_logs(
            chunk_size=opts["chunk_size"],
            sleep_s=opts["sleep"],
            only_template=opts["template_id"],
            log=self.stdout.write,
        )

    def _dry_run(self, template_id):
        """Report scope without writing anything (no v1 creation either)."""
        from model_hub.models.evals_metric import EvalTemplate

        try:
            from ee.usage.models.usage import APICallLog  # noqa: F401
        except ImportError:
            self.stdout.write("usage app not installed (OSS build) — nothing to do.")
            return

        from django.db import connection

        templates = EvalTemplate.objects.filter(deleted=False, owner="user")
        if template_id:
            templates = templates.filter(id=template_id)
        source_ids = [str(pk) for pk in templates.values_list("id", flat=True)]
        self.stdout.write(f"Templates to scan: {len(source_ids)}")
        if connection.vendor != "postgresql":
            return

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM usage_apicalllog "
                "WHERE deleted = false AND jsonb_typeof(config) = 'string' "
                "  AND LEFT(config #>> '{}', 1) IN ('{', '[')"
            )
            self.stdout.write(f"Double-encoded configs to unwrap: {cursor.fetchone()[0]}")
            if source_ids:
                cursor.execute(
                    "SELECT count(*) FROM usage_apicalllog "
                    "WHERE deleted = false AND source_id = ANY(%s) "
                    "  AND jsonb_typeof(config) = 'object' "
                    "  AND COALESCE(config ->> 'version_id', '') = ''",
                    [source_ids],
                )
                self.stdout.write(
                    f"Rows pending version stamp: {cursor.fetchone()[0]}"
                )
