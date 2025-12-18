#!/usr/bin/env sh
set -e

echo "Running migrations..."
cd /app/backend
python manage.py migrate --noinput

echo "Starting server..."
gunicorn qmanage.wsgi:application --bind 0.0.0.0:${PORT}
