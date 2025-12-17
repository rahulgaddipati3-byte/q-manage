from django.db import migrations
from django.utils import timezone


def backfill(apps, schema_editor):
    Token = apps.get_model("core", "Token")

    # Set service_date = created_at.date() where missing
    qs = Token.objects.filter(service_date__isnull=True).only("id", "created_at").order_by("created_at")

    batch = []
    for t in qs.iterator(chunk_size=1000):
        t.service_date = t.created_at.date()
        batch.append(t)

        if len(batch) >= 1000:
            Token.objects.bulk_update(batch, ["service_date"])
            batch = []

    if batch:
        Token.objects.bulk_update(batch, ["service_date"])

    # Now assign sequence per (counter_id, service_date) in created_at order
    # Note: counter can be NULL; we treat NULL as its own group.
    rows = (
        Token.objects
        .values_list("id", "counter_id", "service_date")
        .order_by("counter_id", "service_date", "created_at", "id")
    )

    current_key = None
    seq = 0
    batch = []

    for token_id, counter_id, service_date in rows.iterator(chunk_size=2000):
        key = (counter_id, service_date)
        if key != current_key:
            current_key = key
            seq = 1
        else:
            seq += 1

        batch.append(Token(id=token_id, sequence=seq))

        if len(batch) >= 2000:
            Token.objects.bulk_update(batch, ["sequence"])
            batch = []

    if batch:
        Token.objects.bulk_update(batch, ["sequence"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_backfill_token_sequence"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
