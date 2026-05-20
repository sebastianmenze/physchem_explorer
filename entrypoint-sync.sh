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

# Restart all Streamlit containers so they connect to the fresh DB.
# Requires /var/run/docker.sock mounted from the host.
restart_streamlit() {
    CTRS=$(docker ps -q --filter "label=com.docker.compose.service=streamlit" 2>/dev/null)
    if [ -z "$CTRS" ]; then
        echo "No streamlit containers found (docker socket available?), skipping restart."
        return
    fi
    echo "Restarting streamlit containers: $CTRS"
    echo "$CTRS" | xargs docker restart
    echo "Streamlit restart done."
}

# Write crontab — calls run_sync.sh which syncs then restarts streamlit.
# PATH line ensures cron finds python and docker in /usr/local/bin.
cat > /app/run_sync.sh <<'SCRIPT'
#!/bin/sh
set -e
echo "=== Sync started at $(date -u) ==="
python /app/sync_physchem.py --db "$DB_PATH" --active-days "${SYNC_ACTIVE_DAYS:-365}"
echo "=== Sync finished at $(date -u), restarting streamlit ==="
CTRS=$(docker ps -q --filter "label=com.docker.compose.service=streamlit" 2>/dev/null)
if [ -n "$CTRS" ]; then
    echo "$CTRS" | xargs docker restart && echo "Streamlit containers restarted."
else
    echo "Warning: no streamlit containers found."
fi
SCRIPT
chmod +x /app/run_sync.sh

printf 'PATH=/usr/local/bin:/usr/bin:/bin\n%s root /app/run_sync.sh > /proc/1/fd/1 2>&1\n\n' \
    "$SCHEDULE" > /etc/cron.d/physchem-sync
chmod 0644 /etc/cron.d/physchem-sync

# Optional immediate run on container start
if [ "${SYNC_ON_START:-false}" = "true" ]; then
    echo "Running initial sync on start..."
    python sync_physchem.py --db "$DB" --active-days "$ACTIVE_DAYS"
    restart_streamlit
fi

exec cron -f
