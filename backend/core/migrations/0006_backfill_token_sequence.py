from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_counter_alter_token_expires_at_token_counter"),
    ]

    operations = [
        # Tell Django "service_date exists" (state), but don't try to create it again in DB.
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name="token",
                    name="service_date",
                    field=models.DateField(null=True, blank=True, db_index=True),
                ),
            ],
        ),

        # Add sequence column in DB (safe-ish) + state.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE core_token "
                        "ADD COLUMN sequence INT UNSIGNED NOT NULL DEFAULT 0;"
                    ),
                    reverse_sql="ALTER TABLE core_token DROP COLUMN sequence;",
                )
            ],
            state_operations=[
                migrations.AddField(
                    model_name="token",
                    name="sequence",
                    field=models.PositiveIntegerField(default=0, db_index=True),
                ),
            ],
        ),
    ]
