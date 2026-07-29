from django.db import migrations


def _backfill(apps, schema_editor):
    from model_hub.management.commands.backfill_queue_item_source_preview import (
        backfill_queue_item_source_previews,
    )

    # Fails open internally — a ClickHouse hiccup leaves rows NULL and they keep
    # rendering via the live fallback, rather than breaking the deploy.
    backfill_queue_item_source_previews()


def _noop(apps, schema_editor):
    # Irreversible data backfill; nothing to undo (leaves values in place).
    pass


class Migration(migrations.Migration):
    # Chunked, per-project backfill that reads ClickHouse — not a single
    # transaction.
    atomic = False

    dependencies = [
        ("model_hub", "0121_queueitem_source_preview"),
    ]

    operations = [
        migrations.RunPython(_backfill, _noop),
    ]
