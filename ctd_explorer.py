"""
ctd_explorer.py  —  Physchem CTD Explorer
Interactive map with bounding-box + time filter, operation table, and T/S profile viewer.

Local run:
    conda activate rsk
    python -m streamlit run ctd_explorer.py

Docker run:
    docker compose up -d
    # or override DB path:
    DB_PATH=/data/physchem_all.duckdb docker compose up -d
"""

import io
import os
import duckdb
import pandas as pd
import numpy as np
import streamlit as st
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta, date

# ── config ────────────────────────────────────────────────────────────────────
# DB_PATH can be overridden via environment variable — used by Docker
DB_PATH = os.environ.get("DB_PATH", "physchem_all.duckdb")

st.set_page_config(
    page_title="Physchem CTD Explorer",
    page_icon="🌊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── minimal styling ────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .block-container { padding-top: 3.5rem; padding-bottom: 1rem; }
    .stMetric label { font-size: 0.75rem !important; }
    div[data-testid="stSidebarContent"] { padding-top: 1.5rem; }
    .op-meta-label { font-size: 0.72rem; color: #888; text-transform: uppercase;
                     letter-spacing: 0.04em; margin-bottom: 1px; }
    .op-meta-value { font-size: 0.92rem; margin-bottom: 10px; }
    .section-header { font-size: 0.78rem; font-weight: 600; text-transform: uppercase;
                      letter-spacing: 0.06em; color: #888; margin: 12px 0 6px; }
</style>
""", unsafe_allow_html=True)


# ── DB connection (cached) ────────────────────────────────────────────────────
@st.cache_resource
def get_con():
    return duckdb.connect(DB_PATH, read_only=True)


con = get_con()


# ── check DB has data ─────────────────────────────────────────────────────────
def check_db():
    n = con.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    return n > 0


# ── sidebar: filters ──────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🌊 CTD Explorer")
    st.markdown("---")

    st.markdown("### Cruise")
    cruises = ["— All —"] + sorted([
        r[0] for r in con.execute(
            "SELECT DISTINCT cruise FROM missions WHERE cruise IS NOT NULL ORDER BY 1"
        ).fetchall()
    ])
    cruise_filter = st.selectbox("Cruise number", cruises)
    cruise_active = cruise_filter != "— All —"

    st.markdown("### Time range")
    if cruise_active:
        st.caption("ℹ️ Date filter inactive while a cruise is selected.")
    default_end   = date.today()
    default_start = default_end - timedelta(days=365)
    date_start_str = st.text_input("From (YYYY-MM-DD)", value=str(default_start), disabled=cruise_active)
    date_end_str   = st.text_input("To   (YYYY-MM-DD)", value=str(default_end),   disabled=cruise_active)
    try:
        date_start = date.fromisoformat(date_start_str.strip())
    except ValueError:
        st.caption("Invalid start date")
        date_start = default_start
    try:
        date_end = date.fromisoformat(date_end_str.strip())
    except ValueError:
        st.caption("Invalid end date")
        date_end = default_end

    st.markdown("### Area filter")
    _poly  = st.session_state.get("drawn_polygon", [])
    _drawn = st.session_state.get("drawn_bbox", {})
    if _poly:
        st.caption(f"Polygon drawn ({len(_poly)} vertices) — bounding box inputs inactive.")
        # Derive bbox from polygon for the SQL pre-filter
        _poly_lats = [p[0] for p in _poly]
        _poly_lons = [p[1] for p in _poly]
        lat_min = float(min(_poly_lats));  lat_max = float(max(_poly_lats))
        lon_min = float(min(_poly_lons));  lon_max = float(max(_poly_lons))
        if st.button("✕  Clear polygon", width='stretch'):
            st.session_state.drawn_polygon = []
            st.rerun()
    else:
        st.caption("Draw a rectangle or polygon on the map, or enter coordinates manually")
        col1, col2 = st.columns(2)
        with col1:
            lat_min = st.number_input("Lat min", value=float(_drawn.get("lat_min", -90.0)), min_value=-90.0, max_value=90.0, step=0.5, format="%.2f")
            lon_min = st.number_input("Lon min", value=float(_drawn.get("lon_min", -180.0)), min_value=-180.0, max_value=180.0, step=0.5, format="%.2f")
        with col2:
            lat_max = st.number_input("Lat max", value=float(_drawn.get("lat_max", 90.0)), min_value=-90.0, max_value=90.0, step=0.5, format="%.2f")
            lon_max = st.number_input("Lon max", value=float(_drawn.get("lon_max", 180.0)), min_value=-180.0, max_value=180.0, step=0.5, format="%.2f")
        if _drawn:
            if st.button("✕  Clear drawn box", width='stretch'):
                st.session_state.drawn_bbox = {}
                st.rerun()

    st.markdown("### Platform filter")
    platforms = ["All"] + sorted([
        r[0] for r in con.execute(
            "SELECT DISTINCT platform_name FROM missions WHERE platform_name IS NOT NULL ORDER BY 1"
        ).fetchall()
    ])
    platform_filter = st.selectbox("Platform", platforms)

    st.markdown("---")
    search_clicked = st.button("🔍  Search", width='stretch', type="primary")


# Ensure drawn bbox always takes precedence over number input widget state.
# Streamlit caches widget values between reruns and may ignore a changed
# value= parameter, causing stale defaults to be used instead of the newly
# drawn rectangle. Override here so the search always uses the stored shape.
_db = st.session_state.get("drawn_bbox", {})
if _db and not st.session_state.get("drawn_polygon"):
    lat_min = float(_db["lat_min"])
    lat_max = float(_db["lat_max"])
    lon_min = float(_db["lon_min"])
    lon_max = float(_db["lon_max"])


# ── session state ─────────────────────────────────────────────────────────────
if "results"      not in st.session_state: st.session_state.results      = pd.DataFrame()
if "selected_op"  not in st.session_state: st.session_state.selected_op  = None
if "profile"      not in st.session_state: st.session_state.profile      = pd.DataFrame()
if "last_clicked" not in st.session_state: st.session_state.last_clicked = None
if "drawn_bbox"   not in st.session_state: st.session_state.drawn_bbox   = {}
if "drawn_polygon"  not in st.session_state: st.session_state.drawn_polygon  = []
if "map_center"   not in st.session_state: st.session_state.map_center   = None


# ── polygon point-in-polygon filter (ray-casting, no extra deps) ──────────────
def _pip(lat: float, lon: float, poly: list) -> bool:
    """Return True if (lat, lon) is inside the polygon [[lat,lon], ...]."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if ((xi > lon) != (xj > lon)) and (lat < (yj - yi) * (lon - xi) / (xj - xi) + yi):
            inside = not inside
        j = i
    return inside

def filter_by_polygon(df: pd.DataFrame, poly: list) -> pd.DataFrame:
    if not poly or df.empty:
        return df
    mask = [_pip(float(row.lat), float(row.lon), poly) for _, row in df.iterrows()]
    return df[mask].reset_index(drop=True)


# ── query ─────────────────────────────────────────────────────────────────────
def run_query(date_start, date_end, lat_min, lat_max, lon_min, lon_max, platform_filter, cruise_filter=""):
    cruise_filter = (cruise_filter or "").strip()
    cruise_active = bool(cruise_filter) and cruise_filter != "— All —"

    where_parts = [
        "o.latitude_start  BETWEEN ? AND ?",
        "o.longitude_start BETWEEN ? AND ?",
        "o.latitude_start  IS NOT NULL",
        "o.longitude_start IS NOT NULL",
    ]
    params = [lat_min, lat_max, lon_min, lon_max]

    if not cruise_active:
        where_parts.insert(0, "o.time_start BETWEEN ? AND ?")
        params = [str(date_start) + "T00:00:00", str(date_end) + "T23:59:59"] + params

    if platform_filter != "All":
        where_parts.append("m.platform_name = ?")
        params.append(platform_filter)

    if cruise_active:
        where_parts.append("m.cruise = ?")
        params.append(cruise_filter)

    return con.execute(f"""
        SELECT
            o.operation_id,
            o.operation_number,
            o.operation_type,
            o.station_type,
            o.time_start,
            o.time_end,
            ROUND(o.latitude_start,  4) AS lat,
            ROUND(o.longitude_start, 4) AS lon,
            ROUND(o.bottom_depth,    1) AS bottom_depth_m,
            o.operation_comment,
            m.mission_id,
            m.platform_name,
            m.cruise,
            m.mission_name,
            m.chief_scientist
        FROM operations o
        JOIN missions m USING (mission_id)
        WHERE {" AND ".join(where_parts)}
        ORDER BY o.time_start DESC
    """, params).df()


# ── fetch T/S profile for one operation ──────────────────────────────────────
def fetch_profile(operation_id: int) -> pd.DataFrame:
    return con.execute("""
        SELECT
            r.sample_number,
            MAX(CASE WHEN p.parameter_code = 'PRES'          THEN r.value_dec END) AS pressure,
            MAX(CASE WHEN p.parameter_code = 'DEPTH'         THEN r.value_dec END) AS depth,
            MAX(CASE WHEN p.parameter_code = 'TEMP'          THEN r.value_dec END) AS temperature,
            MAX(CASE WHEN p.parameter_code = 'PSAL'          THEN r.value_dec END) AS salinity,
            MAX(CASE WHEN p.parameter_code = 'PSAL_ADJUSTED' THEN r.value_dec END) AS salinity_adj,
            MAX(CASE WHEN p.parameter_code = 'DOXY'          THEN r.value_dec END) AS oxygen
        FROM readings r
        JOIN parameters  p USING (parameter_id)
        JOIN instruments i USING (instrument_id)
        WHERE i.operation_id = ?
        GROUP BY r.sample_number
        ORDER BY pressure NULLS LAST
    """, [operation_id]).df()


# ── build Folium map ──────────────────────────────────────────────────────────
def build_map(df: pd.DataFrame, selected_op_id=None, center=None):
    if center is not None:
        zoom = 8
    elif df.empty:
        center = [62.0, 5.0]
        zoom   = 5
    else:
        center = [df.lat.mean(), df.lon.mean()]
        zoom   = 6

    m = folium.Map(
        location=center,
        zoom_start=zoom,
        tiles="CartoDB positron",
        control_scale=True,
    )

    # Suppress the default blue click marker that Folium/Leaflet adds on click
    m.add_child(folium.Element("""
        <script>
        document.addEventListener("DOMContentLoaded", function() {
            setTimeout(function() {
                var map = Object.values(window).find(v => v && v._leaflet_id);
                if (map) { map.on('click', function(e) { e.originalEvent.stopPropagation(); }); }
            }, 500);
        });
        </script>
    """))

    # Draw toolbar — rectangle + polygon
    _shape_opts = {"color": "#1a73e8", "weight": 2, "fillOpacity": 0.05}
    Draw(
        export=False,
        draw_options={
            "rectangle": {"shapeOptions": _shape_opts},
            "polygon":   {"shapeOptions": _shape_opts},
            "polyline": False, "circle": False,
            "marker": False, "circlemarker": False,
        },
        edit_options={"edit": False, "remove": True},
    ).add_to(m)

    # Render bounding box when active (rectangle only — drawn polygon is kept
    # in the Leaflet.Draw layer, which persists via key="main_map"; adding a
    # second folium.Polygon overlay causes the map to pulsate/reload).
    _active_poly = st.session_state.get("drawn_polygon", [])
    if not _active_poly and not (lat_min == -90 and lat_max == 90 and lon_min == -180 and lon_max == 180):
        folium.Rectangle(
            bounds=[[lat_min, lon_min], [lat_max, lon_max]],
            color="#1a73e8", weight=1.5, fill=True, fill_opacity=0.04,
            dash_array="6",
        ).add_to(m)

    for _, row in df.iterrows():
        is_selected = (row.operation_id == selected_op_id)
        color  = "#e8453c" if is_selected else "#1a73e8"
        radius = 9          if is_selected else 6
        weight = 3          if is_selected else 1.5

        popup_html = f"""
        <div style='font-family:sans-serif;font-size:12px;min-width:180px'>
            <b>Operation {row.operation_id}</b><br>
            <span style='color:#555'>{row.platform_name}</span><br><br>
            <b>Time:</b> {str(row.time_start)[:16]}<br>
            <b>Type:</b> {row.operation_type or '—'}<br>
            <b>Depth:</b> {row.bottom_depth_m or '—'} m<br>
            <b>Cruise:</b> {row.cruise or '—'}<br><br>
            <a href='#' onclick="
              window.parent.document.dispatchEvent(
                new CustomEvent('op_select', {{detail: {row.operation_id}}})
              ); return false;"
              style='color:#1a73e8'>Load profile ↗</a>
        </div>
        """

        tooltip_html = (
            f"<div style='font-family:sans-serif;font-size:11px;line-height:1.5;min-width:160px'>"
            f"<b style='font-size:12px'>Op {row.operation_id}</b> &nbsp;"
            f"<span style='color:#1a73e8'>{row.platform_name or '—'}</span><br>"
            f"{str(row.time_start)[:16]} &nbsp;|&nbsp; {row.operation_type or '—'}<br>"
            f"Depth: {f'{row.bottom_depth_m} m' if pd.notna(row.bottom_depth_m) else '—'} &nbsp;|&nbsp; "
            f"{row.lat}, {row.lon}"
            f"</div>"
        )

        folium.CircleMarker(
            location=[row.lat, row.lon],
            radius=radius,
            color=color,
            weight=weight,
            fill=True,
            fill_color=color,
            fill_opacity=0.75,
            tooltip=folium.Tooltip(tooltip_html, sticky=True),
        ).add_to(m)

    return m


# ── build T/S profile chart ───────────────────────────────────────────────────
def build_profile_chart(df: pd.DataFrame, op_id: int):
    has_temp = df["temperature"].notna().any()
    has_oxy  = df["oxygen"].notna().any()

    # Prefer PSAL_ADJUSTED; fall back to PSAL
    use_sal_adj = "salinity_adj" in df.columns and df["salinity_adj"].notna().any()
    sal_col     = "salinity_adj" if use_sal_adj else "salinity"
    sal_label   = "Salinity adj. (PSU)" if use_sal_adj else "Salinity (PSU)"
    has_sal     = df[sal_col].notna().any()

    n_cols  = int(sum([has_temp, has_sal, has_oxy]))
    if n_cols == 0:
        return None

    titles = [t for t, h in [("Temperature (°C)", has_temp),
                               (sal_label,          has_sal),
                               ("Oxygen",           has_oxy)] if h]

    fig = make_subplots(rows=1, cols=n_cols, shared_yaxes=True, subplot_titles=titles,
                        horizontal_spacing=0.06)

    col = 1
    # Vertical axis: prefer DEPTH, fall back to PRES, then sample_number
    if "depth" in df.columns and df["depth"].notna().any():
        yaxis = df["depth"]
        yaxis_label = "Depth (m)"
    elif df["pressure"].notna().any():
        yaxis = df["pressure"]
        yaxis_label = "Pressure (dbar)"
    else:
        yaxis = df["sample_number"]
        yaxis_label = "Sample number"

    if has_temp:
        fig.add_trace(go.Scatter(
            x=df["temperature"], y=yaxis, mode="lines",
            line=dict(color="#e8453c", width=2), name="Temperature",
        ), row=1, col=col); col += 1

    if has_sal:
        fig.add_trace(go.Scatter(
            x=df[sal_col], y=yaxis, mode="lines",
            line=dict(color="#1a73e8", width=2), name=sal_label,
        ), row=1, col=col); col += 1

    if has_oxy:
        fig.add_trace(go.Scatter(
            x=df["oxygen"], y=yaxis, mode="lines",
            line=dict(color="#0f9d58", width=2), name="Oxygen",
        ), row=1, col=col)

    fig.update_yaxes(autorange="reversed", title_text=yaxis_label, row=1, col=1)
    fig.update_layout(
        height=420,
        margin=dict(l=10, r=10, t=40, b=10),
        showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(size=12),
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(0,0,0,0.06)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(0,0,0,0.06)")
    return fig


# ── main layout ───────────────────────────────────────────────────────────────
if not check_db():
    st.warning("⚠️  No operations found in the database. Run the download script first.")
    st.stop()

# ── run search ────────────────────────────────────────────────────────────────
if search_clicked:
    with st.spinner("Searching..."):
        _results = run_query(date_start, date_end, lat_min, lat_max, lon_min, lon_max, platform_filter, cruise_filter)
        if st.session_state.get("drawn_polygon"):
            _results = filter_by_polygon(_results, st.session_state.drawn_polygon)
        st.session_state.results     = _results
        st.session_state.selected_op = None
        st.session_state.profile     = pd.DataFrame()
        st.session_state.map_center  = None
        st.session_state.map_zoom    = None

df = st.session_state.results

# ── map (full width) ──────────────────────────────────────────────────────────
center = st.session_state.map_center

if df.empty and not search_clicked:
    st.info("Set filters and press **Search** to load operations.")
    folium_map = build_map(pd.DataFrame())
elif df.empty:
    st.warning("No operations found for the selected filters.")
    folium_map = build_map(pd.DataFrame())
else:
    folium_map = build_map(df, st.session_state.selected_op, center=center)

map_data = st_folium(
    folium_map,
    width=None,
    height=480,
    key="main_map",
    returned_objects=["last_object_clicked", "all_drawings"],
)

# handle drawn shapes (rectangle or polygon)
drawings = (map_data or {}).get("all_drawings")
if drawings:
    for feature in drawings:
        geom  = feature.get("geometry", {})
        props = feature.get("properties", {})
        if geom.get("type") == "Polygon":
            coords = geom["coordinates"][0]   # [[lon, lat], ...]
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            if props.get("type") == "rectangle":
                new_bbox = {
                    "lat_min": round(min(lats), 4),
                    "lat_max": round(max(lats), 4),
                    "lon_min": round(min(lons), 4),
                    "lon_max": round(max(lons), 4),
                }
                if new_bbox != st.session_state.get("drawn_bbox") or st.session_state.get("drawn_polygon"):
                    st.session_state.drawn_bbox    = new_bbox
                    st.session_state.drawn_polygon = []
                    st.rerun()
            else:
                # Free polygon — store as [[lat, lon], ...] (drop closing duplicate)
                new_poly = [[round(c[1], 4), round(c[0], 4)] for c in coords[:-1]]
                if new_poly != st.session_state.get("drawn_polygon"):
                    st.session_state.drawn_polygon = new_poly
                    st.session_state.drawn_bbox    = {}
                    st.rerun()

# detect marker click
clicked = (map_data or {}).get("last_object_clicked")
if clicked and not df.empty:
    clat = clicked.get("lat")
    clng = clicked.get("lng")
    if clat is not None and clng is not None:
        dist = ((df.lat - clat) ** 2 + (df.lon - clng) ** 2)
        closest_id = int(df.loc[dist.idxmin(), "operation_id"])
        if dist.min() < 0.01 and closest_id != st.session_state.last_clicked:
            op_row_click = df[df.operation_id == closest_id].iloc[0]
            st.session_state.map_center   = [float(op_row_click.lat), float(op_row_click.lon)]
            st.session_state.last_clicked = closest_id
            st.session_state.selected_op  = closest_id
            with st.spinner("Loading profile..."):
                st.session_state.profile = fetch_profile(closest_id)
            st.rerun()

# ── metadata + profile row (only when an op is selected) ─────────────────────
if st.session_state.selected_op is not None:
    meta_col, profile_col = st.columns([2, 3], gap="medium")
    op_id  = st.session_state.selected_op
    op_row = df[df.operation_id == op_id]

    with meta_col:
        if not op_row.empty:
            r = op_row.iloc[0]
            st.markdown(f"#### Operation {op_id}")

            def meta(label, value):
                st.markdown(
                    f"<div class='op-meta-label'>{label}</div>"
                    f"<div class='op-meta-value'>{value if pd.notna(value) and str(value) not in ['', 'None'] else '—'}</div>",
                    unsafe_allow_html=True
                )

            col_a, col_b = st.columns(2)
            with col_a:
                meta("Platform",      r.platform_name)
                meta("Cruise",        r.cruise)
                meta("Type",          r.operation_type)
                meta("Station",       r.station_type)
            with col_b:
                meta("Time start",    str(r.time_start)[:16])
                meta("Time end",      str(r.time_end)[:16] if pd.notna(r.time_end) else "—")
                meta("Bottom depth",  f"{r.bottom_depth_m} m" if pd.notna(r.bottom_depth_m) else "—")
                meta("Lat / Lon",     f"{r.lat}, {r.lon}")

            if r.operation_comment and str(r.operation_comment) not in ["None", ""]:
                meta("Comment", r.operation_comment)
            meta("Mission",         r.mission_name)
            meta("Chief scientist", r.chief_scientist)

    with profile_col:
        profile = st.session_state.profile
        if profile.empty:
            st.caption("No readings found for this operation.")
        else:
            st.markdown("<div class='section-header'>T/S Profile</div>", unsafe_allow_html=True)
            fig = build_profile_chart(profile, op_id)
            if fig:
                st.plotly_chart(fig, width='stretch', config={
                    "displayModeBar": True,
                    "modeBarButtonsToRemove": [
                        "autoScale2d", "lasso2d", "select2d",
                        "toggleSpikelines", "hoverClosestCartesian",
                        "hoverCompareCartesian",
                    ],
                    "modeBarButtonsToAdd": [],
                    "displaylogo": False,
                })
            else:
                st.caption("No TEMP/PSAL/PRES readings found for this operation.")
            with st.expander("Raw profile data"):
                st.dataframe(
                    profile.dropna(how="all", subset=["temperature","salinity","pressure"]).round(4),
                    width='stretch',
                    height=200,
                )


# ── export: fetch all readings for found operations with full metadata ─────────
@st.cache_data(show_spinner="Building export dataset...")
def build_export_df(operation_ids: tuple) -> pd.DataFrame:
    """
    Fetch all readings for the given operations and pivot so that each
    parameter (TEMP, PSAL, PRES, ...) becomes its own column.
    One row = one sample_number within one operation.
    Mission and operation metadata are repeated on every row.
    """
    if not operation_ids:
        return pd.DataFrame()

    ids_sql = ",".join(str(i) for i in operation_ids)

    # Fetch long-format readings
    raw = con.execute(f"""
        SELECT
            m.mission_id,
            m.mission_type,
            m.start_year,
            m.platform,
            m.platform_name,
            m.cruise,
            m.mission_name,
            m.chief_scientist,
            m.mission_start,
            m.mission_stop,
            o.operation_id,
            o.operation_number,
            o.operation_type,
            o.station_type,
            o.time_start,
            o.time_end,
            o.latitude_start,
            o.longitude_start,
            o.bottom_depth       AS bottom_depth_m,
            i.instrument_type,
            i.instrument_serial_number,
            i.instrument_model,
            p.parameter_code,
            p.units,
            r.sample_number,
            r.value_datetime,
            r.value_dec,
            r.quality
        FROM readings r
        JOIN parameters  p USING (parameter_id)
        JOIN instruments i USING (instrument_id)
        JOIN operations  o ON i.operation_id = o.operation_id
        JOIN missions    m USING (mission_id)
        WHERE o.operation_id IN ({ids_sql})
        ORDER BY o.operation_id, r.sample_number, p.parameter_code
    """).df()

    if raw.empty:
        return raw

    # Metadata columns repeated on every row (carried via merge, not used as pivot index)
    meta_cols = [
        "mission_id", "mission_type", "start_year", "platform", "platform_name",
        "cruise", "mission_name", "chief_scientist", "mission_start", "mission_stop",
        "operation_id", "operation_number", "operation_type", "station_type",
        "time_start", "time_end", "latitude_start", "longitude_start", "bottom_depth_m",
        "instrument_type", "instrument_serial_number", "instrument_model",
    ]

    # Deduplicate: keep first value per (operation_id, sample_number, parameter_code)
    raw = raw.drop_duplicates(subset=["operation_id", "sample_number", "parameter_code"])

    # Pivot value_dec using simple pivot (no aggregation needed after dedup)
    pivoted = raw.pivot(
        index=["operation_id", "sample_number"],
        columns="parameter_code",
        values="value_dec",
    ).reset_index()
    pivoted.columns.name = None

    # Pivot quality flags
    quality = raw.pivot(
        index=["operation_id", "sample_number"],
        columns="parameter_code",
        values="quality",
    ).reset_index()
    quality.columns.name = None
    param_codes = [c for c in quality.columns if c not in ["operation_id", "sample_number"]]
    quality = quality.rename(columns={c: f"{c}_QC" for c in param_codes})

    # Merge pivoted values + QC
    wide = pivoted.merge(quality, on=["operation_id", "sample_number"], how="left")

    # Bring in metadata (one row per operation_id — take first occurrence)
    meta = raw[["operation_id", "sample_number", "value_datetime"] + 
               [c for c in meta_cols if c != "operation_id"]
              ].drop_duplicates(subset=["operation_id", "sample_number"])

    result = meta.merge(wide, on=["operation_id", "sample_number"], how="right")

    # Order columns: meta first, then PRES/DEPTH, then other params, then QC flags
    param_value_cols = [c for c in wide.columns
                        if c not in ["operation_id", "sample_number"] and not c.endswith("_QC")]
    qc_cols          = [c for c in wide.columns if c.endswith("_QC")]
    priority         = [c for c in ["PRES", "DEPTH", "DEPH"] if c in param_value_cols]
    rest             = sorted([c for c in param_value_cols if c not in priority])
    ordered_params   = priority + rest
    ordered_qc       = [f"{c}_QC" for c in ordered_params if f"{c}_QC" in qc_cols]

    final_cols = (["operation_id", "sample_number", "value_datetime"] +
                  [c for c in meta_cols if c not in ["operation_id"]] +
                  ordered_params + ordered_qc)
    return result[[c for c in final_cols if c in result.columns]]


def to_netcdf_bytes(export_df: pd.DataFrame) -> bytes:
    """
    Convert wide-format export DataFrame to compact NetCDF4.

    Structure: flat 1D dimension "obs" (one element per row, like the CSV).
    Each profile is a group of obs rows sharing the same operation_id.
    This avoids the sparse 2D (operation x sample_number) matrix that caused
    the 445 MB file — no padding NaNs, no empty cells.

    Numeric variables use float32 (halves size vs float64) + zlib compression.
    String metadata stored as per-operation 1D variables on a separate
    "profile" dimension, linked via a profile_index coordinate on obs.
    """
    import xarray as xr
    import numpy as np

    non_param_cols = {
        "operation_id", "sample_number", "value_datetime",
        "mission_id", "mission_type", "start_year", "platform", "platform_name",
        "cruise", "mission_name", "chief_scientist", "mission_start", "mission_stop",
        "operation_number", "operation_type", "station_type",
        "time_start", "time_end", "latitude_start", "longitude_start", "bottom_depth_m",
        "instrument_type", "instrument_serial_number", "instrument_model",
    }
    param_cols = [c for c in export_df.columns
                  if c not in non_param_cols and not c.endswith("_QC")]

    # Sort so profiles are contiguous
    edf = export_df.sort_values(["operation_id", "sample_number"]).reset_index(drop=True)

    # Profile-level index
    op_ids_1d   = edf["operation_id"].values.astype(np.int64)
    unique_ops  = pd.unique(edf["operation_id"])          # order-preserving
    op_to_idx   = {op: i for i, op in enumerate(unique_ops)}
    profile_idx = np.array([op_to_idx[o] for o in op_ids_1d], dtype=np.int32)
    n_obs       = len(edf)
    n_profiles  = len(unique_ops)

    # compression settings applied to every numeric variable
    enc_num = {"dtype": "float32", "zlib": True, "complevel": 6, "_FillValue": -9999.0}
    enc_str = {"zlib": True, "complevel": 6}
    enc_int = {"dtype": "int32",   "zlib": True, "complevel": 6}

    encoding  = {}
    data_vars = {}

    # ── obs-dimension variables ───────────────────────────────────────────────
    data_vars["profile_index"] = ("obs", profile_idx)
    encoding["profile_index"]  = enc_int.copy()

    data_vars["sample_number"] = ("obs", edf["sample_number"].to_numpy(dtype=np.int32, na_value=-9999))
    encoding["sample_number"]  = enc_int.copy()

    if "value_datetime" in edf.columns:
        # store as seconds since epoch (compact int)
        dt_vals = pd.to_datetime(edf["value_datetime"], errors="coerce")
        epoch   = pd.Timestamp("1970-01-01")
        secs    = ((dt_vals - epoch).dt.total_seconds()
                   .to_numpy(dtype=float, na_value=float("nan"))
                   .astype(np.float64))
        data_vars["value_datetime_epoch"] = ("obs", secs)
        encoding["value_datetime_epoch"]  = {"dtype": "float64", "zlib": True, "complevel": 6}

    for col in param_cols:
        arr = edf[col].to_numpy(dtype=float, na_value=float("nan")).astype(np.float32)
        data_vars[col]  = ("obs", arr)
        encoding[col]   = enc_num.copy()
        qc_col = f"{col}_QC"
        if qc_col in edf.columns:
            # store QC as single-byte int (0-9); empty/unknown -> -1
            qc_arr = pd.to_numeric(edf[qc_col], errors="coerce").fillna(-1).astype(np.int8)
            data_vars[qc_col] = ("obs", qc_arr.values)
            encoding[qc_col]  = {"dtype": "int8", "zlib": True, "complevel": 6}

    # ── profile-dimension variables (metadata) ────────────────────────────────
    meta_num = {"latitude_start": np.float32, "longitude_start": np.float32,
                "bottom_depth_m": np.float32}
    meta_str = ["platform_name", "cruise", "operation_type", "mission_name",
                "chief_scientist", "operation_number"]
    meta_time = ["time_start", "time_end"]

    op_meta = (edf[["operation_id"] +
                   [c for c in list(meta_num) + meta_str + meta_time
                    if c in edf.columns]]
               .drop_duplicates("operation_id")
               .set_index("operation_id")
               .reindex(unique_ops))

    data_vars["operation_id"] = ("profile", unique_ops.astype(np.int64))
    encoding["operation_id"]  = {"dtype": "int64", "zlib": True, "complevel": 6}

    for col, dtype in meta_num.items():
        if col in op_meta.columns:
            data_vars[col] = ("profile", op_meta[col].to_numpy(dtype=float, na_value=float("nan")).astype(dtype))
            encoding[col]  = {"dtype": str(dtype().dtype), "zlib": True, "complevel": 6, "_FillValue": -9999.0}

    for col in meta_str:
        if col in op_meta.columns:
            arr = op_meta[col].fillna("").astype(str).values.astype("U")
            data_vars[col] = ("profile", arr)
            encoding[col]  = enc_str.copy()

    for col in meta_time:
        if col in op_meta.columns:
            dt  = pd.to_datetime(op_meta[col], errors="coerce")
            secs = ((dt - pd.Timestamp("1970-01-01")).dt.total_seconds()
                    .to_numpy(dtype=float, na_value=float("nan")).astype(np.float64))
            data_vars[f"{col}_epoch"] = ("profile", secs)
            encoding[f"{col}_epoch"]  = {"dtype": "float64", "zlib": True, "complevel": 6}

    ds = xr.Dataset(
        data_vars,
        coords={"obs": np.arange(n_obs, dtype=np.int64),
                "profile": np.arange(n_profiles, dtype=np.int32)},
        attrs={
            "Conventions": "CF-1.8",
            "featureType": "profile",
            "n_profiles": n_profiles,
            "n_obs": n_obs,
            "history": f"Exported from Physchem CTD Explorer",
        }
    )

    buf = io.BytesIO()
    ds.to_netcdf(buf, engine="h5netcdf", encoding=encoding)
    buf.seek(0)
    return buf.read()

# ── search results table ──────────────────────────────────────────────────────
if not df.empty:
    st.markdown("---")
    st.markdown(
        f"### Search results  <span style='font-size:0.85rem;color:#888;font-weight:400'>({len(df)} operations)</span>",
        unsafe_allow_html=True
    )
    display_cols = ["operation_id", "platform_name", "cruise", "operation_type",
                    "time_start", "lat", "lon", "bottom_depth_m"]
    display_df = df[display_cols].copy()
    display_df["time_start"] = display_df["time_start"].astype(str).str[:16]

    event = st.dataframe(
        display_df,
        width='stretch',
        height=320,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "operation_id":   st.column_config.NumberColumn("Op ID",   width="small"),
            "platform_name":  st.column_config.TextColumn("Platform"),
            "cruise":         st.column_config.TextColumn("Cruise"),
            "operation_type": st.column_config.TextColumn("Type",      width="small"),
            "time_start":     st.column_config.TextColumn("Time start"),
            "lat":            st.column_config.NumberColumn("Lat",     format="%.4f", width="small"),
            "lon":            st.column_config.NumberColumn("Lon",     format="%.4f", width="small"),
            "bottom_depth_m": st.column_config.NumberColumn("Depth m", width="small"),
        },
    )

    if event and event.selection and event.selection.rows:
        selected_row_idx = event.selection.rows[0]
        selected_op_id   = int(df.iloc[selected_row_idx]["operation_id"])
        if selected_op_id != st.session_state.selected_op:
            tbl_row = df[df.operation_id == selected_op_id].iloc[0]
            st.session_state.map_center  = [float(tbl_row.lat), float(tbl_row.lon)]
            st.session_state.selected_op = selected_op_id
            with st.spinner("Loading profile..."):
                st.session_state.profile = fetch_profile(selected_op_id)
            st.rerun()

# ── download panel ────────────────────────────────────────────────────────────
if not df.empty:
    st.markdown("---")
    st.markdown("### Download")

    op_ids = tuple(int(i) for i in df["operation_id"].tolist())
    export_key = f"export_{hash(op_ids)}"

    @st.cache_data(show_spinner=False)
    def get_reading_count(op_ids):
        ids_sql = ",".join(str(i) for i in op_ids)
        return con.execute(f"""
            SELECT COUNT(*) FROM readings r
            JOIN parameters p USING (parameter_id)
            JOIN instruments i USING (instrument_id)
            WHERE i.operation_id IN ({ids_sql})
        """).fetchone()[0]

    n_readings = get_reading_count(op_ids)

    # ── size estimates (before preparation) ───────────────────────────────────
    # Wide-format pivot means many NaN cells (only params present in each op
    # are non-null). Model accounts for sparsity:
    #   Metadata cols (~23): text, mostly non-null, ~10 bytes/cell
    #   Param value cells:   fill_rate * 6 bytes + (1-fill_rate) * 1 byte
    #   QC flag cells:       fill_rate * 2 bytes + (1-fill_rate) * 1 byte
    # Excel: xlsx zipped XML, ~25% of CSV for sparse numeric data
    # NetCDF: float32 + zlib6, ~10% of CSV
    n_ops      = len(df)
    n_param_est = con.execute(f"""
        SELECT COUNT(DISTINCT p.parameter_code)
        FROM parameters p
        JOIN instruments i USING (instrument_id)
        WHERE i.operation_id IN ({",".join(str(i) for i in op_ids)})
    """).fetchone()[0] or 5
    # pivoted rows ~ unique (operation_id, sample_number) combos
    n_rows_est  = con.execute(f"""
        SELECT COUNT(DISTINCT i.operation_id || '_' || r.sample_number)
        FROM readings r
        JOIN parameters p USING (parameter_id)
        JOIN instruments i USING (instrument_id)
        WHERE i.operation_id IN ({",".join(str(i) for i in op_ids)})
    """).fetchone()[0] or n_readings

    _META_COLS  = 23
    _FILL       = 0.55   # fraction of param/QC cells that are non-null
    _meta_bpr   = _META_COLS * 10
    _param_bpr  = n_param_est * (_FILL * 6 + (1 - _FILL) * 1)
    _qc_bpr     = n_param_est * (_FILL * 2 + (1 - _FILL) * 1)
    csv_est   = int(n_rows_est * (_meta_bpr + _param_bpr + _qc_bpr))
    excel_est = int(csv_est * 0.90)   # xlsx ≈ CSV size: XML verbosity offsets zip compression
    nc_est    = int(csv_est * 0.10)

    def fmt_size(b):
        if b < 1024:       return f"{b} B"
        elif b < 1024**2:  return f"{b/1024:.0f} KB"
        else:              return f"{b/1024**2:.1f} MB"

    st.caption(f"{n_ops:,} operations · ~{n_rows_est:,} rows · ~{n_param_est} parameters  ·  file sizes are estimates")

    size_c1, size_c2, size_c3, size_c4 = st.columns(4)
    size_c1.metric("Rows (est.)",    f"~{n_rows_est:,}")
    size_c2.metric("CSV (est.)",     f"~{fmt_size(csv_est)}")
    size_c3.metric("NetCDF (est.)",  f"~{fmt_size(nc_est)}")
    size_c4.metric("Excel (est.)",   f"~{fmt_size(excel_est)}")

    _limit = 500 * 1024 * 1024  # 500 MB
    if csv_est > _limit:
        st.warning(
            f"The estimated CSV size is **~{fmt_size(csv_est)}**, which is large and may be slow to "
            f"generate and download. Consider reducing your selection by: "
            f"narrowing the **date range**, drawing a smaller **bounding box** on the map, "
            f"or filtering by a specific **platform**."
        )
    elif excel_est > _limit:
        st.warning(
            f"The estimated Excel size is **~{fmt_size(excel_est)}**. "
            f"Excel handles large files poorly — consider using CSV or NetCDF instead, "
            f"or reduce the selection."
        )

    # Check NetCDF deps once
    _nc_missing = []
    try: import xarray
    except ImportError: _nc_missing.append("xarray")
    try: import h5netcdf
    except ImportError:
        try: import scipy
        except ImportError: _nc_missing.append("h5netcdf")

    prep_col, _ = st.columns([1, 3])
    with prep_col:
        if st.button("Prepare downloads", type="primary", width='stretch', key="prep_dl"):
            # Clear any previous export for this result set
            for suffix in ["_csv", "_nc", "_nc_err", "_xl", "_edf"]:
                st.session_state.pop(export_key + suffix, None)

            # Step 1: build the base dataframe
            with st.spinner("Building export dataset..."):
                edf = build_export_df(op_ids)
                st.session_state[export_key + "_edf"] = edf
            st.rerun()  # show CSV button immediately

    # Render columns for whatever is ready so far
    dl_col1, dl_col2, dl_col3 = st.columns(3)

    edf = st.session_state.get(export_key + "_edf")

    # ── CSV ───────────────────────────────────────────────────────────────────
    with dl_col1:
        if edf is None:
            st.button("Download CSV", disabled=True, width='stretch')
        elif export_key + "_csv" not in st.session_state:
            st.session_state[export_key + "_csv"] = edf.to_csv(index=False).encode("utf-8")
            st.rerun()
        else:
            csv_bytes = st.session_state[export_key + "_csv"]
            st.download_button(
                label=f"Download CSV  ({fmt_size(len(csv_bytes))})",
                data=csv_bytes,
                file_name="physchem_export.csv",
                mime="text/csv",
                width='stretch',
            )

    # ── NetCDF ────────────────────────────────────────────────────────────────
    with dl_col2:
        if _nc_missing:
            st.button("Download NetCDF", disabled=True, width='stretch')
            st.caption(f"Run: `pip install {' '.join(_nc_missing)}`")
        elif edf is None or export_key + "_csv" not in st.session_state:
            st.button("Download NetCDF", disabled=True, width='stretch')
        elif export_key + "_nc_err" in st.session_state:
            st.button("Download NetCDF", disabled=True, width='stretch')
            st.caption(f"Error: {st.session_state[export_key + '_nc_err']}")
        elif export_key + "_nc" not in st.session_state:
            try:
                st.session_state[export_key + "_nc"] = to_netcdf_bytes(edf)
            except Exception as e:
                st.session_state[export_key + "_nc_err"] = str(e)
            st.rerun()
        else:
            nc_bytes = st.session_state[export_key + "_nc"]
            st.download_button(
                label=f"Download NetCDF  ({fmt_size(len(nc_bytes))})",
                data=nc_bytes,
                file_name="physchem_export.nc",
                mime="application/octet-stream",
                width='stretch',
            )

    # ── Excel ─────────────────────────────────────────────────────────────────
    with dl_col3:
        if edf is None or (export_key + "_nc" not in st.session_state and export_key + "_nc_err" not in st.session_state):
            st.button("Download Excel", disabled=True, width='stretch')
        elif export_key + "_xl" not in st.session_state:
            xl_buf = io.BytesIO()
            with pd.ExcelWriter(xl_buf, engine="openpyxl") as writer:
                edf.to_excel(writer, sheet_name="Readings", index=False)
            st.session_state[export_key + "_xl"] = xl_buf.getvalue()
            st.rerun()
        else:
            xl_bytes = st.session_state[export_key + "_xl"]
            st.download_button(
                label=f"Download Excel  ({fmt_size(len(xl_bytes))})",
                data=xl_bytes,
                file_name="physchem_export.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width='stretch',
            )

    # Summary caption once all ready
    if all(export_key + s in st.session_state for s in ["_csv", "_xl"]):
        edf = st.session_state[export_key + "_edf"]
        st.caption(f"{len(edf):,} rows × {len(edf.columns)} columns")
