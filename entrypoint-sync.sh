#!/bin/sh
set -e

SCHEDULE="${SYNC_SCHEDULE:-0 2 * * *}"
DB="${DB_PATH:-/data/physchem_all.duckdb}"
ACTIVE_DAYS="${SYNC_ACTIVE_DAYS:-365}"

echo "=== Physchem sync service ==="
echo "Schedule   : $SCHEDULE"
echo "DB path    : $DB"
echo "Active days: $ACTIVE_DAYS"
echo ""

# Write crontab — set PATH so cron finds python in /usr/local/bin;
# output goes to PID 1 stdout so docker logs captures it.
printf 'PATH=/usr/local/bin:/usr/bin:/bin\n%s root cd /app && python sync_physchem.py --db %s --active-days %s > /proc/1/fd/1 2>&1\n\n' \
    "$SCHEDULE" "$DB" "$ACTIVE_DAYS" > /etc/cron.d/physchem-sync
chmod 0644 /etc/cron.d/physchem-sync

# Optional immediate run on container start
if [ "${SYNC_ON_START:-false}" = "true" ]; then
    echo "Running initial sync on start..."
    python sync_physchem.py --db "$DB" --active-days "$ACTIVE_DAYS"
fi

exec cron -f
