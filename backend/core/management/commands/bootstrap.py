import os
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from core.models import Counter


class Command(BaseCommand):
    help = "Create initial superuser and seed default counters (idempotent)."

    def handle(self, *args, **options):
        # ---- Superuser (optional via env vars) ----
        username = os.getenv("DJANGO_SUPERUSER_USERNAME")
        email = os.getenv("DJANGO_SUPERUSER_EMAIL", "")
        password = os.getenv("DJANGO_SUPERUSER_PASSWORD")

        if username and password:
            User = get_user_model()
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": email, "is_staff": True, "is_superuser": True},
            )
            if created:
                user.set_password(password)
                user.save()
                self.stdout.write(self.style.SUCCESS(f"Created superuser: {username}"))
            else:
                # Ensure flags are correct
                changed = False
                if not user.is_staff:
                    user.is_staff = True
                    changed = True
                if not user.is_superuser:
                    user.is_superuser = True
                    changed = True
                if email and user.email != email:
                    user.email = email
                    changed = True
                if changed:
                    user.save()
                self.stdout.write(self.style.WARNING(f"Superuser exists: {username}"))
        else:
            self.stdout.write(self.style.WARNING("Superuser env vars not set; skipping superuser creation."))

        # ---- Seed counters (default A1/A2 if none exist) ----
        if Counter.objects.count() == 0:
            defaults = [("A1", "Counter 1"), ("A2", "Counter 2")]
            for code, name in defaults:
                Counter.objects.create(code=code, name=name)
            self.stdout.write(self.style.SUCCESS("Seeded default counters: A1, A2"))
        else:
            self.stdout.write(self.style.WARNING("Counters already exist; skipping seeding."))
