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

# Write the sync wrapper with DB path and active-days baked in.
# Unquoted heredoc: $DB and $ACTIVE_DAYS expand now (at container start);
# \$(...) and \$VARS are escaped so they expand later when the script runs.
#
# Streamlit MUST be restarted after the swap. The sync replaces the DB file
# with os.replace() (a new inode), but on this deployment's storage backend
# (a network/HPC filesystem) that swap is NOT propagated into already-running
# containers — an in-process reconnect still reads the stale inode. Only a
# fresh container mount sees the new file. We restart the streamlit replicas
# one at a time (rolling) so the cluster stays available during the restart.
cat > /app/run_sync.sh << EOF
#!/bin/sh
set -e
echo "=== Sync started at \$(date -u) ==="
python /app/sync_physchem.py --db "$DB" --active-days "$ACTIVE_DAYS"
echo "=== Sync finished at \$(date -u), restarting streamlit (rolling) ==="
CTRS=\$(docker ps -q --filter "label=com.docker.compose.service=streamlit" 2>/dev/null)
if [ -n "\$CTRS" ]; then
    for c in \$CTRS; do
        docker restart "\$c" >/dev/null && echo "  restarted \$c"
        sleep 5   # let the replica come back before taking the next one down
    done
    echo "Streamlit containers restarted — new data is now live."
else
    echo "Warning: no streamlit containers found (is the docker socket mounted?)."
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
