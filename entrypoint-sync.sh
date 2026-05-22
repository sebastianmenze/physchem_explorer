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

# Write the sync+restart wrapper with DB path and active-days baked in.
# Unquoted heredoc: $DB and $ACTIVE_DAYS expand now (at container start);
# \$(...) and \$CTRS are escaped so they expand later when the script runs.
cat > /app/run_sync.sh << EOF
#!/bin/sh
set -e
echo "=== Sync started at \$(date -u) ==="
python /app/sync_physchem.py --db "$DB" --active-days "$ACTIVE_DAYS"
echo "=== Sync finished at \$(date -u), restarting streamlit ==="
CTRS=\$(docker ps -q --filter "label=com.docker.compose.service=streamlit" 2>/dev/null)
if [ -n "\$CTRS" ]; then
    echo "\$CTRS" | xargs docker restart && echo "Streamlit containers restarted."
else
    echo "Warning: no streamlit containers found (docker socket mounted?)."
fi
EOF
chmod +x /app/run_sync.sh

# Write crontab — PATH ensures cron finds python and docker in /usr/local/bin;
# output goes to PID 1 stdout so docker logs captures it.
printf 'PATH=/usr/local/bin:/usr/bin:/bin\n%s root /app/run_sync.sh > /proc/1/fd/1 2>&1\n\n' \
    "$SCHEDULE" > /etc/cron.d/physchem-sync
chmod 0644 /etc/cron.d/physchem-sync

# Optional immediate run on container start
if [ "${SYNC_ON_START:-false}" = "true" ]; then
    echo "Running initial sync on start..."
    /app/run_sync.sh
fi

exec cron -f
