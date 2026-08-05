"""Repair feed occurrence counts inflated by the non-idempotent increment.

``assign_to_cluster`` incremented ``error_count`` on every assignment while the
two writes it counted — the issue's cluster FK and the junction row — were both
idempotent. Re-scanning a trace therefore bumped the "occurrences" the Feed
renders without adding a member. Measured on live data: a fifth of rendered
scanner entries were wrong, the worst reading 33 occurrences against 3 issues
across 3 traces, while the trace count beside it stayed correct because it was
already derived rather than incremented.

The code now derives both counts, so this repairs the rows written before that.

Only scanner clusters that still have issue rows are touched. Eval clusters
carry no ``TraceScanIssue`` rows at all — their members live in the junction —
so recomputing from issues would zero them. Scanner clusters with zero issues
are legacy pre-revamp rows that ``_base_qs`` already excludes from the feed;
leaving them alone keeps this migration to the rows a user can actually see.
"""

from django.db import migrations
from django.db.models import Count, Q


def repair_error_counts(apps, schema_editor):
    TraceErrorGroup = apps.get_model("tracer", "TraceErrorGroup")

    qs = (
        TraceErrorGroup.objects.filter(source="scanner", deleted=False)
        .annotate(
            actual=Count(
                "scan_issues",
                filter=Q(scan_issues__deleted=False),
                distinct=True,
            )
        )
        .exclude(actual=0)
    )

    batch, BATCH_SIZE = [], 500
    for cluster in qs.iterator(chunk_size=BATCH_SIZE):
        if cluster.error_count == cluster.actual:
            continue
        cluster.error_count = cluster.actual
        batch.append(cluster)
        if len(batch) >= BATCH_SIZE:
            TraceErrorGroup.objects.bulk_update(batch, ["error_count"])
            batch = []
    if batch:
        TraceErrorGroup.objects.bulk_update(batch, ["error_count"])


class Migration(migrations.Migration):
    dependencies = [
        ("tracer", "0095_merge_20260722_1400"),
    ]

    # The old values are drift, not data — there is nothing to restore them to.
    operations = [
        migrations.RunPython(repair_error_counts, migrations.RunPython.noop),
    ]
