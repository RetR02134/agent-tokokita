#!/bin/bash
# Satu worker + banyak thread: state sesi & rate limit disimpan di memori proses.
# .env dibaca app (python-dotenv) saat start; di server sebaiknya set lewat systemd/Docker env.
exec gunicorn \
  --bind="${HOST:-0.0.0.0}:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --worker-class gthread --threads "${THREADS:-16}" \
  --timeout 180 --graceful-timeout 30 --keep-alive 30 \
  --access-logfile - \
  app:app
