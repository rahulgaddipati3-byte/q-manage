#!/usr/bin/env sh
set -e

echo "Preparing DB..."
cd /app/backend

# IMPORTANT: wipe sqlite so migrations run clean on Render
rm -f db.sqlite3

echo "Running migrations..."
python manage.py migrate --noinput

echo "Collecting static..."
python manage.py collectstatic --noinput

echo "Starting server..."
gunicorn qmanage.wsgi:application --bind 0.0.0.0:${PORT:-8000}
