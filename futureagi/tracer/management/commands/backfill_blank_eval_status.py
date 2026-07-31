"""
Backfill ``status`` on legacy blank-status EvalLogger rows.

The legacy eval writer (`tracer.utils.eval`) persisted results without setting
``status`` (the column only exists since migration 0084), so rows it created
before the eval-task engine rollout carry ``status = ''`` even though they
hold a real, billed result. This command:

  1. Stamps ``completed`` on blank rows that verifiably succeeded (no error,
     no skip reason, and at least one populated output column).
  2. Re-runs the baseline status pass from migration 0087, which flips
     ``error = true`` rows to ``errored`` and skip-reason rows to ``skipped``
     — this catches blank rows the legacy error path created. The pass is
     idempotent, so re-running it is cheap.

Blank rows with no output at all are intentionally left untouched: their
outcome is unknown and mislabeling them would corrupt the metric this fixes.

Optionally (``--optimize-mirror``) it collapses duplicate row-versions in the
ClickHouse PeerDB mirror of this table. The mirror is a ReplacingMergeTree:
every UPDATE replicated from Postgres lands as a new row-version and stale
versions linger until a background merge, so ad-hoc queries without ``FINAL``
overcount. ``OPTIMIZE TABLE ... FINAL`` forces that merge. Run it only after
PeerDB has caught up with the backfill's updates (it lags by minutes), and
expect it to be slow on a large table.

Usage:
    python manage.py backfill_blank_eval_status --dry-run
    python manage.py backfill_blank_eval_status
    python manage.py backfill_blank_eval_status --batch-size 1000
    python manage.py backfill_blank_eval_status --optimize-mirror
    python manage.py backfill_blank_eval_status --optimize-mirror-only
"""

from django.core.management.base import BaseCommand

from tracer.services.eval_tasks.backfill import (
    _backfill_status,
    backfill_blank_completed_status,
)

_MIRROR_TABLE = "tracer_eval_logger"


class Command(BaseCommand):
    help = (
        "Stamp 'completed' on legacy blank-status EvalLogger rows that hold a "
        "successful result, and re-run the errored/skipped correction pass."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows would change without writing.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=5000,
            help="Rows per UPDATE batch (each batch commits independently).",
        )
        parser.add_argument(
            "--optimize-mirror",
            action="store_true",
            help=(
                "After the backfill, force-merge the ClickHouse PeerDB mirror "
                "(OPTIMIZE TABLE ... FINAL) to collapse duplicate row-versions. "
                "Run only once PeerDB has replicated the backfill's updates."
            ),
        )
        parser.add_argument(
            "--optimize-mirror-only",
            action="store_true",
            help="Skip the Postgres backfill and only merge the ClickHouse mirror.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]
        optimize_mirror = options["optimize_mirror"] or options["optimize_mirror_only"]

        if not options["optimize_mirror_only"]:
            completed = backfill_blank_completed_status(
                batch_size=batch_size, dry_run=dry_run
            )
            if dry_run:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"dry_run=True would_stamp_completed={completed} "
                        f"(errored/skipped pass and mirror merge skipped in dry-run)"
                    )
                )
                return

            errored_or_skipped = _backfill_status(batch_size)
            self.stdout.write(
                self.style.SUCCESS(
                    f"stamped_completed={completed} "
                    f"errored_or_skipped_corrected={errored_or_skipped}"
                )
            )

        if optimize_mirror and not dry_run:
            self._optimize_mirror()

    def _optimize_mirror(self):
        """Collapse duplicate row-versions in the ClickHouse mirror. ``count()``
        is metadata-only in ClickHouse, so the before/after delta is a cheap
        report of how many stale versions the merge removed."""
        from tfc.utils.clickhouse import ClickHouseClientSingleton

        ch = ClickHouseClientSingleton()
        before = ch.execute(f"SELECT count() FROM {_MIRROR_TABLE}")[0][0]
        self.stdout.write(
            f"mirror row-versions before merge: {before} — running OPTIMIZE "
            f"TABLE {_MIRROR_TABLE} FINAL (may take a while on a large table)"
        )
        ch.execute(f"OPTIMIZE TABLE {_MIRROR_TABLE} FINAL")
        after = ch.execute(f"SELECT count() FROM {_MIRROR_TABLE}")[0][0]
        self.stdout.write(
            self.style.SUCCESS(
                f"mirror merged: row_versions {before} -> {after} "
                f"(removed {before - after} stale duplicates)"
            )
        )
