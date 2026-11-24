#!/bin/sh
# Daily PostgreSQL backup for a Docker Compose deployment (see README, "Despliegue en VPS").
#   deploy/backup.sh                 # writes backups/mountain-intelligence-YYYY-MM-DD_HHMM.dump
#   KEEP_DAYS=30 deploy/backup.sh    # retention in days (default 14)
# Run from cron as the deploy user, e.g.:
#   15 3 * * * /opt/mountain-intelligence-mcp/deploy/backup.sh >> /var/log/mountain-intelligence-backup.log 2>&1
set -eu

cd "$(dirname "$0")/.."
mkdir -p backups
chmod 700 backups

out="backups/mountain-intelligence-$(date +%Y-%m-%d_%H%M).dump"
tmp="$out.partial"

docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$tmp"
# A custom-format dump that pg_restore cannot list is not a backup.
docker compose exec -T postgres pg_restore --list < "$tmp" > /dev/null
mv "$tmp" "$out"
chmod 600 "$out"

find backups -name 'mountain-intelligence-*.dump' -mtime +"${KEEP_DAYS:-14}" -delete
echo "$(date -Iseconds) backup ok: $out ($(wc -c < "$out") bytes)"
