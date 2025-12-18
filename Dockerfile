FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    default-libmysqlclient-dev gcc pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy backend code
COPY backend /app/backend

# collect static files
RUN python backend/manage.py collectstatic --noinput

# start django using render's por
CMD gunicorn qmanage.wsgi:application --chdir backend --bind 0.0.0.0:$PORT
# copy backend code
COPY backend /app/backend

# start script
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]
