"""Seed system-defined NodeTemplate records from agent_playground.templates.

Replaces the previous manual-only ``seed_node_templates`` management command
as the source of truth — templates are now seeded automatically on deploy.
Idempotent: existing templates only get their safe metadata fields
refreshed; the command remains available for manual re-seeding/dry-runs.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from agent_playground.management.commands.seed_node_templates import (
        seed_node_templates,
    )

    NodeTemplate = apps.get_model("agent_playground", "NodeTemplate")
    seed_node_templates(NodeTemplate)


class Migration(migrations.Migration):
    dependencies = [
        ("agent_playground", "0011_alter_prompttemplatenode_prompt_template_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, reverse_code=migrations.RunPython.noop),
    ]
