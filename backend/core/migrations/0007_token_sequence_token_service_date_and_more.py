from django.db import migrations, models


def backfill(apps, schema_editor):
    Token = apps.get_model("core", "Token")

    # Fill service_date from created_at (only when missing)
    qs = (
        Token.objects
        .filter(service_date__isnull=True)
        .only("id", "created_at")
        .order_by("created_at", "id")
    )

    batch = []
    for t in qs.iterator(chunk_size=1000):
        # created_at should exist in your model
        t.service_date = t.created_at.date()
        batch.append(t)
        if len(batch) >= 1000:
            Token.objects.bulk_update(batch, ["service_date"])
            batch = []
    if batch:
        Token.objects.bulk_update(batch, ["service_date"])

    # Assign sequence per (counter_id, service_date)
    rows = (
        Token.objects
        .values_list("id", "counter_id", "service_date", "created_at")
        .order_by("counter_id", "service_date", "created_at", "id")
    )

    current_key = None
    seq = 0
    batch = []

    for token_id, counter_id, service_date, created_at in rows.iterator(chunk_size=2000):
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
        # ✅ Ensure columns EXIST before RunPython (this is what was breaking on Render)
        migrations.AddField(
            model_name="token",
            name="service_date",
            field=models.DateField(null=True, blank=True, db_index=True),
        ),
        migrations.AddField(
            model_name="token",
            name="sequence",
            field=models.PositiveIntegerField(null=True, blank=True),
        ),

        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
