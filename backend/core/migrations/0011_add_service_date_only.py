from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_..."),  # <-- replace with your latest migration file name number
    ]

    operations = [
        migrations.AddField(
            model_name="token",
            name="service_date",
            field=models.DateField(null=True, blank=True, db_index=True),
        ),
    ]
