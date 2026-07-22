"""
sync_physchem.py

Incrementally sync new data from the Physchem API into a local DuckDB.

Two-phase strategy
------------------
Phase 1 – NEW MISSIONS
    Probe mission IDs above the current maximum stored in the database.
    Stop after STOP_AFTER_MISSES consecutive 404s.
    Download full nested data (operations → instruments → parameters →
    readings) for every new mission found.

Phase 2 – NEW OPERATIONS IN EXISTING MISSIONS
    Re-check "active" missions (mission_stop IS NULL or within the last
    ACTIVE_WINDOW_DAYS days) for operations not yet in the database.
    For each such mission, fetch the lightweight operation list to discover
    new operation IDs, then download only those new operations with their
    full nested tree.

Phase 3 – CHECKPOINT
    Flush DuckDB WAL to disk and print a delta summary (rows added this run).

Usage:
    pip install requests duckdb tqdm
    python sync_physchem.py                   # default: active window = 365 days
    python sync_physchem.py --active-days 90  # only recheck last 90 days
    python sync_physchem.py --active-days 0   # skip Phase 2 (new missions only)
"""

import argparse
import duckdb
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from tqdm import tqdm
import os
import shutil

API_BASE          = "https://physchem-api.hi.no"
DB_PATH           = "data/physchem_all.duckdb"
STOP_AFTER_MISSES = 500   # stop probing after this many consecutive 404s above max ID
MAX_RETRIES       = 3
RETRY_DELAY       = 5     # seconds between retries on network/server errors
CONNECT_TIMEOUT   = 30    # seconds to establish TCP connection
READ_TIMEOUT      = 90    # seconds to wait for server to send data
PHASE2_MAX_HOURS  = 3     # abort Phase 2 after this many hours to avoid overnight hangs
PHASE2_WORKERS       = 20    # parallel threads for lightweight mission checks in Phase 2a
PHASE2_FETCH_WORKERS = 1     # sequential Phase 2b — server is single-threaded per op, parallelism hurts


# ── schema ────────────────────────────────────────────────────────────────────

def init_db(con: duckdb.DuckDBPyConnection):
    con.execute("""
        CREATE TABLE IF NOT EXISTS missions (
            mission_id       BIGINT PRIMARY KEY,
            mission_type     VARCHAR,
            start_year       INTEGER,
            platform         VARCHAR,
            platform_name    VARCHAR,
            mission_number   INTEGER,
            cruise           VARCHAR,
            mission_name     VARCHAR,
            chief_scientist  VARCHAR,
            purpose          VARCHAR,
            responsible_lab  VARCHAR,
            mission_start    TIMESTAMP,
            mission_stop     TIMESTAMP,
            downloaded_at    TIMESTAMP DEFAULT current_timestamp
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS operations (
            operation_id      BIGINT PRIMARY KEY,
            mission_id        BIGINT,
            operation_number  INTEGER,
            operation_type    VARCHAR,
            station_type      VARCHAR,
            time_start        TIMESTAMP,
            time_end          TIMESTAMP,
            latitude_start    DOUBLE,
            longitude_start   DOUBLE,
            latitude_end      DOUBLE,
            longitude_end     DOUBLE,
            bottom_depth      DOUBLE,
            operation_comment VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS instruments (
            instrument_id            BIGINT PRIMARY KEY,
            operation_id             BIGINT,
            instrument_number        INTEGER,
            instrument_type          VARCHAR,
            instrument_serial_number VARCHAR,
            instrument_model         VARCHAR,
            instrument_data_owner    VARCHAR,
            project                  VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS parameters (
            parameter_id         BIGINT PRIMARY KEY,
            instrument_id        BIGINT,
            parameter_code       VARCHAR,
            ordinal              INTEGER,
            units                VARCHAR,
            reference_scale      VARCHAR,
            processing_level     VARCHAR,
            sensor_serial_number VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            reading_id     BIGINT PRIMARY KEY,
            parameter_id   BIGINT,
            sample_number  INTEGER,
            value_datetime TIMESTAMP,
            value_dec      DOUBLE,
            value_str      VARCHAR,
            value_int      INTEGER,
            uncertainty    DOUBLE,
            quality        VARCHAR
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_ops_mission   ON operations  (mission_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_inst_op       ON instruments (operation_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_param_inst    ON parameters  (instrument_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_reading_param ON readings    (parameter_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ops_time      ON operations  (time_start)")


# ── HTTP helper ───────────────────────────────────────────────────────────────

def get_json(url: str, params: dict | None = None):
    """GET with retries. Returns parsed JSON, None on 404, raises on other errors."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            if resp.status_code == 404:
                return None
            if resp.status_code in (401, 403):
                tqdm.write(f"  HTTP {resp.status_code} for {url} — skipping")
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
            else:
                raise RuntimeError(f"Failed after {MAX_RETRIES} attempts: {url}") from e
    return None


# ── insert helpers ────────────────────────────────────────────────────────────

def insert_mission(con, m: dict):
    con.execute("""
        INSERT OR REPLACE INTO missions VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)
    """, [
        m.get("id"),            m.get("missionType"),
        m.get("startYear"),     m.get("platform"),
        m.get("platformName"),  m.get("missionNumber"),
        m.get("cruise"),        m.get("missionName"),
        m.get("chiefScientist"),m.get("purpose"),
        m.get("responsibleLaboratory"),
        m.get("missionStartDate"), m.get("missionStopDate"),
    ])


def insert_operation(con, op: dict, mission_id: int):
    con.execute("""
        INSERT OR REPLACE INTO operations VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        op.get("id"),               mission_id,
        op.get("operationNumber"),  op.get("operationType"),
        op.get("stationType"),      op.get("timeStart"),
        op.get("timeEnd"),          op.get("latitudeStart"),
        op.get("longitudeStart"),   op.get("latitudeEnd"),
        op.get("longitudeEnd"),     op.get("bottomDepthStart"),
        op.get("operationComment"),
    ])


def insert_instrument(con, inst: dict, operation_id: int):
    con.execute("""
        INSERT OR REPLACE INTO instruments VALUES (?,?,?,?,?,?,?,?)
    """, [
        inst.get("id"),                     operation_id,
        inst.get("instrumentNumber"),       inst.get("instrumentType"),
        inst.get("instrumentSerialNumber"), inst.get("instrumentModel"),
        inst.get("instrumentDataOwner"),    inst.get("project"),
    ])


def insert_parameter(con, param: dict, instrument_id: int):
    con.execute("""
        INSERT OR REPLACE INTO parameters VALUES (?,?,?,?,?,?,?,?)
    """, [
        param.get("id"),              instrument_id,
        param.get("parameterCode"),   param.get("ordinal"),
        param.get("units"),           param.get("referenceScale"),
        param.get("processingLevel"), param.get("sensorSerialNumber"),
    ])
    rows = [
        [
            r.get("id"),           param.get("id"),
            r.get("sampleNumber"), r.get("valueDateTime"),
            r.get("valueDec"),     r.get("valueStr"),
            r.get("valueInt"),     r.get("uncertainty"),
            r.get("quality"),
        ]
        for r in (param.get("reading") or [])
        if r.get("id") is not None
    ]
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?,?,?,?)",
            rows
        )


def store_operation_tree(con, op: dict, mission_id: int) -> dict:
    """Insert one operation and its full nested tree. Returns row counts."""
    insert_operation(con, op, mission_id)
    n_inst = n_params = n_readings = 0
    for inst in (op.get("instrument") or []):
        insert_instrument(con, inst, op["id"])
        n_inst += 1
        for param in (inst.get("parameter") or []):
            n_readings += len(param.get("reading") or [])
            insert_parameter(con, param, inst["id"])
            n_params += 1
    return {"inst": n_inst, "params": n_params, "readings": n_readings}


# ── row-count snapshot (for delta reporting) ──────────────────────────────────

def row_counts(con) -> dict:
    rows = con.execute("""
        SELECT 'missions'    AS t, COUNT(*) FROM missions    UNION ALL
        SELECT 'operations',         COUNT(*) FROM operations  UNION ALL
        SELECT 'instruments',        COUNT(*) FROM instruments UNION ALL
        SELECT 'parameters',         COUNT(*) FROM parameters  UNION ALL
        SELECT 'readings',           COUNT(*) FROM readings
    """).fetchall()
    return {t: n for t, n in rows}


# ── Phase 1: new missions ─────────────────────────────────────────────────────

def sync_new_missions(con: duckdb.DuckDBPyConnection) -> dict:
    """
    Probe mission IDs above the current DB maximum.
    Download full nested data for every new ID found.
    Returns totals of newly inserted rows.
    """
    max_id = con.execute("SELECT COALESCE(MAX(mission_id), 0) FROM missions").fetchone()[0]
    print(f"\n── Phase 1: new missions (probing above ID {max_id}) ──")

    totals = {"missions": 0, "ops": 0, "inst": 0, "params": 0, "readings": 0}
    misses  = 0
    current = max_id + 1
    new_ids = []

    pbar = tqdm(desc="Probing IDs", unit="id")
    while misses < STOP_AFTER_MISSES:
        try:
            data = get_json(f"{API_BASE}/mission/{current}")
        except RuntimeError as e:
            tqdm.write(f"  Network error at ID {current}: {e}")
            current += 1
            pbar.update(1)
            continue

        if data is None:
            misses += 1
            pbar.set_postfix(found=len(new_ids), misses=f"{misses}/{STOP_AFTER_MISSES}")
        else:
            new_ids.append((current, data))
            misses = 0
            pbar.set_postfix(found=len(new_ids), last_hit=current, misses=0)

        current += 1
        pbar.update(1)
    pbar.close()

    if not new_ids:
        print("  No new mission IDs found.")
        return totals

    print(f"  Found {len(new_ids)} new mission(s). Downloading...")

    for mission_id, mission_data in tqdm(new_ids, desc="New missions", unit="mission"):
        try:
            insert_mission(con, mission_data)
            totals["missions"] += 1

            operations = get_json(
                f"{API_BASE}/mission/{mission_id}/operation/list",
                params={"extend": "true"},
            ) or []

            for op in operations:
                counts = store_operation_tree(con, op, mission_id)
                totals["ops"]      += 1
                totals["inst"]     += counts["inst"]
                totals["params"]   += counts["params"]
                totals["readings"] += counts["readings"]

        except Exception as e:
            tqdm.write(f"  !! Failed mission {mission_id}: {e}")

    return totals


# ── Phase 2: new operations in existing missions ──────────────────────────────

def find_active_mission_ids(con: duckdb.DuckDBPyConnection, active_window_days: int) -> list[int]:
    """Return IDs of missions that may still be receiving new operations."""
    cutoff = datetime.utcnow() - timedelta(days=active_window_days)
    rows = con.execute("""
        SELECT mission_id FROM missions
        WHERE mission_stop IS NULL
           OR mission_stop >= ?
        ORDER BY mission_id
    """, [cutoff]).fetchall()
    return [r[0] for r in rows]


def _check_one_mission(mission_id: int, known_op_ids: set) -> tuple[int, dict]:
    """Worker: lightweight fetch to find new operations for one mission.

    Returns {op_id: op_metadata_dict} for operations not already in the DB.
    The metadata dict comes from the mission operation-list — the same source
    that populated all existing (searchable) operations — so it reliably
    contains timeStart / latitudeStart / longitudeStart. The per-operation
    endpoint used later for readings does NOT always include these fields.
    """
    remote_ops = get_json(f"{API_BASE}/mission/{mission_id}/operation/list") or []
    new_ops = {
        op["id"]: op
        for op in remote_ops
        if op.get("id") is not None and op["id"] not in known_op_ids
    }
    return mission_id, new_ops


def _fetch_operation(op_id: int) -> dict | None:
    """Try to fetch a single operation with full readings.
    Returns None if the per-operation endpoint doesn't exist (404)."""
    return get_json(f"{API_BASE}/operation/{op_id}", params={"extend": "true"})


# Operation-level metadata fields the app relies on for search/display.
_OP_META_FIELDS = (
    "operationNumber", "operationType", "stationType",
    "timeStart", "timeEnd",
    "latitudeStart", "longitudeStart", "latitudeEnd", "longitudeEnd",
    "bottomDepthStart", "operationComment",
)


def _merge_op_metadata(meta: dict | None, op: dict) -> dict:
    """Overlay authoritative operation metadata from the operation-list onto the
    per-operation payload (which carries the instrument/reading tree but may lack
    operation-level fields). ``meta`` values win for the metadata fields when
    present; everything else (id, instrument tree, ...) is kept from ``op``."""
    if not meta:
        return op
    merged = dict(op)
    for k in _OP_META_FIELDS:
        v = meta.get(k)
        if v is not None:
            merged[k] = v
    return merged


def sync_active_missions(con: duckdb.DuckDBPyConnection, active_window_days: int) -> dict:
    """
    For each active/recent mission, fetch the lightweight operation list,
    compare against the DB, and download only the new operations.
    Returns totals of newly inserted rows.
    """
    totals = {"missions": 0, "ops": 0, "inst": 0, "params": 0, "readings": 0}

    if active_window_days <= 0:
        print("\n── Phase 2: skipped (--active-days 0) ──")
        return totals

    mission_ids = find_active_mission_ids(con, active_window_days)
    print(f"\n── Phase 2: checking {len(mission_ids)} active/recent mission(s) ──")

    if not mission_ids:
        print("  No active missions to check.")
        return totals

    # Load all known operation IDs in one query instead of N separate queries
    ids_sql = ",".join(str(m) for m in mission_ids)
    known_ops: dict[int, set] = {}
    for mission_id, op_id in con.execute(
        f"SELECT mission_id, operation_id FROM operations WHERE mission_id IN ({ids_sql})"
    ).fetchall():
        known_ops.setdefault(mission_id, set()).add(op_id)

    phase2_deadline = datetime.utcnow() + timedelta(hours=PHASE2_MAX_HOURS)

    # ── Phase 2a: parallel lightweight checks ────────────────────────────────
    # Run PHASE2_WORKERS concurrent GETs; each fetches the operation list
    # (metadata, no readings) and returns {op_id: metadata} for new operations.
    missions_to_fetch: dict[int, dict] = {}
    futures = {}
    with ThreadPoolExecutor(max_workers=PHASE2_WORKERS) as pool:
        for mid in mission_ids:
            futures[pool.submit(_check_one_mission, mid, known_ops.get(mid, set()))] = mid

        for future in tqdm(as_completed(futures), total=len(futures),
                           desc="Checking missions", unit="mission"):
            mid = futures[future]
            try:
                _, new_ops = future.result()
                if new_ops:
                    missions_to_fetch[mid] = new_ops
                    tqdm.write(f"  Mission {mid}: {len(new_ops)} new operation(s) found")
            except Exception as e:
                tqdm.write(f"  !! Mission {mid} check failed: {e}")

    print(f"  {len(missions_to_fetch)} mission(s) have new operations — fetching now.")

    # ── Phase 2b: fetch and store new operations (sequential) ────────────────
    # The API server processes ~19 readings/sec per connection regardless of
    # parallelism — concurrent requests share that capacity and make each one
    # slower. Sequential is fastest for large operations.

    def _ts():
        return datetime.utcnow().strftime("%H:%M:%S")

    per_op_endpoint_works = True
    fallback_missions: set[int] = set()

    for mission_id, new_ops in missions_to_fetch.items():
        if datetime.utcnow() > phase2_deadline:
            print(f"  !! Phase 2 time limit ({PHASE2_MAX_HOURS}h) reached — stopping early.", flush=True)
            break
        try:
            stored = set()

            if per_op_endpoint_works:
                for op_id in sorted(new_ops):
                    url = f"{API_BASE}/operation/{op_id}?extend=true"
                    print(f"  [{_ts()}] GET {url}", flush=True)
                    t0 = datetime.utcnow()
                    op = _fetch_operation(op_id)
                    elapsed = (datetime.utcnow() - t0).seconds
                    if op and op.get("id"):
                        # The per-op endpoint reliably returns the instrument /
                        # reading tree but may omit operation-level metadata
                        # (timeStart, latitudeStart, ...). Merge the authoritative
                        # metadata from the operation-list over it so lat/lon/time
                        # are always populated — otherwise the operation is stored
                        # with NULL coords/time and is invisible to the app search.
                        op = _merge_op_metadata(new_ops.get(op_id), op)
                        counts = store_operation_tree(con, op, mission_id)
                        totals["ops"]      += 1
                        totals["inst"]     += counts["inst"]
                        totals["params"]   += counts["params"]
                        totals["readings"] += counts["readings"]
                        stored.add(op_id)
                        print(f"    → {counts['readings']} readings in {elapsed}s  (total {totals['readings']:,})", flush=True)
                    elif op is None:
                        per_op_endpoint_works = False
                        fallback_missions.add(mission_id)
                        print(f"    → 404 — switching to full-mission fetch for mission {mission_id}", flush=True)
                        break
                    else:
                        print(f"    → empty response", flush=True)

            # Full-mission fallback if /operation/{id} returned 404
            remaining = set(new_ops) - stored
            if remaining:
                url = f"{API_BASE}/mission/{mission_id}/operation/list?extend=true"
                print(f"  [{_ts()}] GET {url}  (ops needed: {sorted(remaining)})", flush=True)
                t0 = datetime.utcnow()
                full_ops = get_json(f"{API_BASE}/mission/{mission_id}/operation/list",
                                    params={"extend": "true"}) or []
                elapsed = (datetime.utcnow() - t0).seconds
                print(f"    → response in {elapsed}s, {len(full_ops)} ops in payload", flush=True)
                for op in full_ops:
                    if op.get("id") not in remaining:
                        continue
                    counts = store_operation_tree(con, op, mission_id)
                    totals["ops"]      += 1
                    totals["inst"]     += counts["inst"]
                    totals["params"]   += counts["params"]
                    totals["readings"] += counts["readings"]
                    print(f"    → stored op={op.get('id')} ({counts['readings']} readings)", flush=True)

        except Exception as e:
            print(f"  !! Failed mission {mission_id}: {e}", flush=True)

    print(f"  Done. {totals['ops']} new operation(s), {totals['readings']:,} new reading(s).")
    return totals


# ── repair: backfill operations stored with missing metadata ──────────────────

def repair_missing_metadata(con: duckdb.DuckDBPyConnection) -> int:
    """Backfill operations that were stored without location/time metadata.

    Operations fetched via the per-operation endpoint before the metadata-merge
    fix landed may have NULL time_start / latitude_start / longitude_start, which
    makes them invisible to the app search (its WHERE clause requires those
    fields). Re-fetch the affected missions' operation lists (cheap, no readings)
    and update just the metadata columns. Readings/instruments are untouched.
    """
    broken = con.execute("""
        SELECT mission_id, operation_id
        FROM operations
        WHERE time_start IS NULL
           OR latitude_start IS NULL
           OR longitude_start IS NULL
    """).fetchall()
    if not broken:
        print("\n── Repair: no operations with missing metadata ──")
        return 0

    by_mission: dict[int, set] = {}
    for mission_id, op_id in broken:
        by_mission.setdefault(mission_id, set()).add(op_id)

    print(f"\n── Repair: {len(broken)} operation(s) missing metadata "
          f"across {len(by_mission)} mission(s) ──")

    fixed = 0
    for mission_id, op_ids in by_mission.items():
        try:
            ops = get_json(f"{API_BASE}/mission/{mission_id}/operation/list") or []
        except Exception as e:
            print(f"  !! Mission {mission_id}: fetch failed ({e})", flush=True)
            continue
        by_id = {op.get("id"): op for op in ops if op.get("id") is not None}
        for op_id in op_ids:
            meta = by_id.get(op_id)
            if not meta:
                continue
            con.execute("""
                UPDATE operations SET
                    operation_number  = COALESCE(?, operation_number),
                    operation_type    = COALESCE(?, operation_type),
                    station_type      = COALESCE(?, station_type),
                    time_start        = COALESCE(?, time_start),
                    time_end          = COALESCE(?, time_end),
                    latitude_start    = COALESCE(?, latitude_start),
                    longitude_start   = COALESCE(?, longitude_start),
                    latitude_end      = COALESCE(?, latitude_end),
                    longitude_end     = COALESCE(?, longitude_end),
                    bottom_depth      = COALESCE(?, bottom_depth),
                    operation_comment = COALESCE(?, operation_comment)
                WHERE operation_id = ?
            """, [
                meta.get("operationNumber"), meta.get("operationType"),
                meta.get("stationType"),     meta.get("timeStart"),
                meta.get("timeEnd"),         meta.get("latitudeStart"),
                meta.get("longitudeStart"),  meta.get("latitudeEnd"),
                meta.get("longitudeEnd"),    meta.get("bottomDepthStart"),
                meta.get("operationComment"), op_id,
            ])
            fixed += 1
        print(f"  Mission {mission_id}: repaired {len(op_ids)} operation(s)", flush=True)

    print(f"  Repair complete: {fixed} operation(s) updated.")
    return fixed


# ── summary ───────────────────────────────────────────────────────────────────

def print_summary(con: duckdb.DuckDBPyConnection):
    print("\n=== DB TOTALS ===")
    rows = con.execute("""
        SELECT 'missions'    AS tbl, COUNT(*) AS n FROM missions    UNION ALL
        SELECT 'operations',          COUNT(*)     FROM operations  UNION ALL
        SELECT 'instruments',         COUNT(*)     FROM instruments UNION ALL
        SELECT 'parameters',          COUNT(*)     FROM parameters  UNION ALL
        SELECT 'readings',            COUNT(*)     FROM readings
    """).fetchall()
    for tbl, n in rows:
        print(f"  {tbl:<14} {n:>12,}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Incrementally sync new PhyschemDB data into local DuckDB."
    )
    parser.add_argument(
        "--active-days", type=int, default=365, metavar="N",
        help="Re-check missions with stop date within last N days (default: 365). "
             "Use 0 to skip Phase 2.",
    )
    parser.add_argument(
        "--db", default=DB_PATH, metavar="PATH",
        help=f"Path to DuckDB file (default: {DB_PATH})",
    )
    args = parser.parse_args()

    print(f"DB     : {args.db}")
    print(f"API    : {API_BASE}")
    print(f"Active : {args.active_days} day window for Phase 2")
    print(f"Started: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    print('Cloning local DUCK DB')
    newdatabase = os.path.join(os.path.dirname(os.path.abspath(args.db)), 'physchem_new.duckdb')
    dest = shutil.copyfile(args.db, newdatabase)

    con = duckdb.connect(newdatabase)
    init_db(con)

    before = row_counts(con)

    # ── Phase 1: new missions
    delta1 = sync_new_missions(con)
    con.execute("CHECKPOINT")

    # ── Phase 2: new operations in active missions
    delta2 = sync_active_missions(con, args.active_days)
    con.execute("CHECKPOINT")

    # ── Repair: backfill any operations stored without location/time metadata
    repair_missing_metadata(con)
    con.execute("CHECKPOINT")

    # ── Delta summary
    after = row_counts(con)
    print("\n=== SYNC COMPLETE ===")
    print(f"  Finished : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("\n  Rows added this run:")
    for key in ("missions", "operations", "instruments", "parameters", "readings"):
        db_key  = key.rstrip("s") if key != "readings" else key  # align with delta dict
        added   = after.get(key, 0) - before.get(key, 0)
        print(f"    {key:<14} +{added:>10,}")

    print_summary(con)
    con.close()

    # Atomic rename: replaces the live DB in one syscall so readers never see
    # a partially-written file.  os.replace() requires both paths on the same
    # filesystem (guaranteed here since newdatabase sits next to args.db).
    os.replace(newdatabase, args.db)
    print(f"Swapped {newdatabase} -> {args.db}")



if __name__ == "__main__":
    main()
