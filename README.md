# Q Manage

Q Manage is a simple queue / token management system for clinics, banks, offices, and service centers.

## Features
- Global daily token numbering (A001, A002, ...)
- Reception-style token issue
- Counter-based token calling (FIFO)
- Queue status APIs
- Dockerized backend

## Tech Stack
- Django
- MySQL
- Redis
- Docker

## Run Locally

```bash
git clone https://github.com/rahulgaddipati3-byte/q-manage.git
cd q-manage
docker compose up -d --build
