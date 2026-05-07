# Physchem CTD Explorer

An interactive web application for exploring oceanographic CTD and bottle-sample data from the Norwegian Institute of Marine Research (IMR) [PhysChemDB](https://physchem-api.hi.no).

---

## Features

- **Interactive map** — search by drawing a bounding box or polygon, or enter coordinates manually
- **Time and cruise filters** — filter by date range and/or cruise number (combinable with spatial filters)
- **Platform and data-type filters** — filter by vessel and choose between CTD profiles, bottle values, or both
- **T/S profile viewer** — temperature, salinity (prefers `PSAL_ADJUSTED`), and oxygen plotted against depth
- **CTD + BOT overlay** — CTD data shown as lines, bottle samples as scatter points on the same axes
- **Data export** — download results as CSV, NetCDF (CF-1.8), or Excel with units in column headers
- **Automated sync** — a companion cron container keeps the local DuckDB up to date from the API

---

## Architecture

```
Browser
  │
  ▼
nginx (port 8080)          — reverse proxy, sticky sessions, WebSocket support
  │
  ├── streamlit ×4         — Streamlit app (read-only DuckDB access)
  │
  └── sync (cron)          — nightly incremental sync from PhysChemDB API (read-write)

./data/physchem_all.duckdb — shared data volume
```

---

## Quick start

### Prerequisites

- Docker and Docker Compose
- A populated DuckDB file at `./data/physchem_all.duckdb` (see [Initial data load](#initial-data-load))

### Run

```bash
git clone https://github.com/sebastianmenze/physchem_explorer
cd physchem_explorer
mkdir -p data
docker-compose up -d
```

The app is available at **http://localhost:8080/physchem-explorer**

---

## Initial data load

The sync script performs an incremental update but also works for a full initial download.
Run it once before starting the stack:

```bash
pip install requests duckdb tqdm
python sync_physchem.py --db data/physchem_all.duckdb
```

Or trigger it from inside Docker immediately after starting:

```bash
docker-compose up -d
docker-compose run --rm -e SYNC_ON_START=true sync
```

---

## Configuration

### docker-compose environment variables

#### `streamlit` service

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/data/physchem_all.duckdb` | Path to the DuckDB file inside the container |

#### `sync` service

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/data/physchem_all.duckdb` | Path to the DuckDB file |
| `SYNC_SCHEDULE` | `0 2 * * *` | Cron expression for the sync schedule (default: 02:00 UTC daily) |
| `SYNC_ACTIVE_DAYS` | `365` | How many days back to re-check active missions for new operations |
| `SYNC_ON_START` | `false` | Set to `true` to run a sync immediately when the container starts |

### nginx

The app is served under the `/physchem-explorer` subpath. Edit `nginx.conf` to change routing or add SSL termination.

---

## Sync service

The `sync` container runs `sync_physchem.py` on a cron schedule. It:

1. **Phase 1** — probes mission IDs above the current DB maximum, downloads any new missions found
2. **Phase 2** — re-checks recently active missions for new operations
3. Clones the live DB to a temp file, syncs into the clone, then atomically renames it back so readers are never exposed to a partially-written file

Streamlit containers automatically reconnect to the new DB file on the next user interaction (mtime-based detection, no restart needed).

### Useful commands

```bash
# Watch sync logs
docker-compose logs -f sync

# Trigger a one-off sync immediately
docker-compose run --rm -e SYNC_ON_START=true sync

# Check the installed crontab inside the sync container
docker-compose exec sync cat /etc/cron.d/physchem-sync
```

---

## Local development

```bash
pip install streamlit folium streamlit-folium plotly duckdb pandas numpy openpyxl xarray h5netcdf h5py

# Point at a local DB
DB_PATH=data/physchem_all.duckdb streamlit run ctd_explorer.py
```

---

## File overview

| File | Description |
|---|---|
| `ctd_explorer.py` | Main Streamlit application |
| `sync_physchem.py` | Incremental sync script (PhysChemDB API → DuckDB) |
| `Dockerfile` | Image for the Streamlit app |
| `Dockerfile.sync` | Lightweight image for the sync cron service |
| `entrypoint-sync.sh` | Entrypoint for the sync container — writes crontab and starts cron |
| `docker-compose.yml` | Service definitions (streamlit ×4, sync, nginx) |
| `nginx.conf` | nginx reverse proxy config with WebSocket and subpath support |
| `data/` | Mount point for the DuckDB file (git-ignored) |
