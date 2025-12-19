#!/usr/bin/env sh
set -e

cd /app/backend

echo "Running migrations..."
python manage.py migrate --noinput

echo "Creating superuser (if needed)..."
python manage.py createsu

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting server..."
gunicorn qmanage.wsgi:application --bind 0.0.0.0:$PORT
