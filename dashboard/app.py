"""Tablero BI: rutas, infracciones y Driver Score de la flota.

streamlit run dashboard/app.py

Se conecta con el usuario de base de datos del rol elegido en la barra lateral
(analista o administrador). El enmascaramiento de IDs lo hace PostgreSQL en las
vistas ``analytics.*``; el tablero solo muestra lo que la base le devuelve.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import folium
import httpx
import pandas as pd
import psycopg
import pydeck as pdk
import streamlit as st
from streamlit.delta_generator import DeltaGenerator
from streamlit_folium import st_folium

from simon_geo.config import DbRole, get_settings
from simon_geo.dashboard import charts, data
from simon_geo.dashboard.basemap import POSITRON_STYLE_URL, hybrid_style
from simon_geo.dashboard.views import (
    HEATMAP_COLORS,
    HOTSPOT_COLOR,
    SEVERITY_COLORS,
    ZONE_COLORS,
    ZONE_LABELS,
    build_legend,
    compact_vehicle_labels,
    feature_collection,
    fleet_summary,
    geofences_on_paths,
    legend_html,
    line_path,
    reconcile_selection,
    vehicle_label,
    view_for,
    with_colors,
)
from simon_geo.parameters import PipelineParameters
from simon_geo.routing import (
    Place,
    RoutePlan,
    RoutingError,
    in_service_area,
    next_points,
    plan_route,
)
from simon_geo.sandbox import SandboxError, SandboxRequest, purge
from simon_geo.sandbox import run as run_sandbox
from simon_geo.seed import bogota
from simon_geo.spatial.geofencing import GeofenceIndex

ROLES = {"Analista BI (IDs enmascarados)": DbRole.ANALYST, "Administrador": DbRole.ADMIN}
# Un color por vehículo (10). Okabe-Ito sin amarillo ni naranja (reservados a la
# severidad de las alertas), completado buscando la mayor distancia OKLab con visión
# normal, deuteranopía y protanopía: ΔE mínimo 7,6. Por debajo de 8 se exige una
# segunda codificación, y la hay: leyenda, tooltip con el nombre, resaltado por clic y
# "Solo el vehículo seleccionado". Luminosidad media: se ve sobre CARTO y la ortofoto.
TRACK_COLORS = [
    [0, 114, 178],  # #0072b2
    [86, 180, 233],  # #56b4e9
    [0, 158, 115],  # #009e73
    [204, 121, 167],  # #cc79a7
    [140, 86, 75],  # #8c564b
    [182, 232, 128],  # #b6e880
    [99, 110, 250],  # #636efa
    [44, 160, 44],  # #2ca02c
    [93, 109, 126],  # #5d6d7e
    [0, 204, 150],  # #00cc96
]
SEVERITY_WEIGHT = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
CACHE_TTL_S = 30
ALERT_LABELS = {"SPEEDING": "Exceso de velocidad", "LOW_FUEL_RANGE": "Combustible insuficiente"}
SEVERITY_SHORT = {"LOW": "baja", "MEDIUM": "media", "HIGH": "alta", "CRITICAL": "crítica"}
# La estructura va en la plantilla y los datos como texto plano: deck.gl escapa los
# valores que interpola, así que un <b> dentro del dato se vería literal.
TOOLTIP = {
    "html": "<b>{title}</b><div style='white-space:pre-line;margin-top:2px'>{body}</div>",
    "style": {"fontSize": "13px", "maxWidth": "320px"},
}
LOCAL_TZ = "America/Bogota"
SANDBOX_COLOR = [255, 0, 200]  # los vehículos simulados no cambian el color de la flota
MAP_ROUTE_MODE = "En el mapa"
PROFILE_LABELS = {"compliant": "Cumplido", "moderate": "Moderado", "aggressive": "Agresivo"}
# Mapa base con ortofoto: estilo MapLibre servido por la propia app (dashboard/static,
# ver .streamlit/config.toml) que pide teselas al ImageServer de Catastro/IDECA.
STATIC_DIR = Path(__file__).parent / "static"
HYBRID_STYLE_FILE = "ortofoto_hibrida.json"  # se genera al arrancar (no se versiona)
ORTHO_ONLY_STYLE_FILE = "ortofoto_bogota_2025.json"  # respaldo sin internet
MAP_CSS = """
<style>
/* Interruptor de mapa base flotando sobre la esquina superior izquierda del mapa */
[data-testid="stColumn"]:has(.st-key-basemap_switch) { position: relative; }
.st-key-basemap_switch {
  position: absolute; top: 10px; left: 10px; z-index: 5; width: auto !important;
  background: rgba(24, 26, 31, 0.82); border-radius: 8px; padding: 4px 12px 2px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.35);
}
.st-key-basemap_switch p { color: #fff !important; font-weight: 600; }
</style>
"""

st.set_page_config(page_title="Simón Geo · Flota", page_icon="🗺️", layout="wide")


# ------------------------------------------------------------------- datos
@st.cache_resource
def connection(role: DbRole) -> psycopg.Connection:
    return psycopg.connect(get_settings().conninfo(role), autocommit=True)


def conn_for(role: DbRole) -> psycopg.Connection:
    """Conexión cacheada, verificada antes de usarla: si la base se reinició, se reabre."""
    conn = connection(role)
    try:
        conn.execute("SELECT 1")
    except psycopg.Error:
        connection.clear()
        conn = connection(role)
    return conn


@st.cache_resource(show_spinner=False)
def ortho_style_url() -> str:
    """Genera una vez el estilo híbrido (ortofoto + vías y nombres de CARTO). Sin
    internet, o si CARTO cambia su estilo, queda la ortofoto sola."""
    try:
        base = httpx.get(POSITRON_STYLE_URL, timeout=5).raise_for_status().json()
        style = hybrid_style(base)
        (STATIC_DIR / HYBRID_STYLE_FILE).write_text(json.dumps(style), encoding="utf-8")
    except (httpx.HTTPError, ValueError, KeyError, OSError):
        return f"/app/static/{ORTHO_ONLY_STYLE_FILE}"
    return f"/app/static/{HYBRID_STYLE_FILE}"


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_days(role: DbRole) -> list[date]:
    return data.available_days(conn_for(role))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_day(role: DbRole, day: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = conn_for(role)
    alerts = data.infractions(conn, day)
    if len(alerts):
        alerts["event_ts"] = pd.to_datetime(alerts["event_ts"], utc=True).dt.tz_convert(LOCAL_TZ)
    return data.daily_kpis(conn, day), alerts


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_profile(role: DbRole, day: date, vehicle_key: str) -> pd.DataFrame:
    profile = data.vehicle_profile(conn_for(role), day, vehicle_key)
    if len(profile):
        profile["event_ts"] = pd.to_datetime(profile["event_ts"], utc=True).dt.tz_convert(LOCAL_TZ)
    return profile


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_sandbox_keys(role: DbRole) -> set[str]:
    return data.sandbox_vehicle_keys(conn_for(role))


@st.cache_resource(show_spinner=False)
def sandbox_catalog() -> tuple[list, GeofenceIndex]:
    return bogota.routes(), GeofenceIndex(bogota.geofences())


@st.cache_data(ttl=300, show_spinner=False)
def load_static(role: DbRole) -> tuple[pd.DataFrame, pd.DataFrame, tuple[str, bool]]:
    conn = conn_for(role)
    return data.geofences(conn), data.infraction_hotspots(conn), data.whoami(conn)


# ------------------------------------------------------------------- capas
def geofence_layer(geofences: pd.DataFrame) -> pdk.Layer:
    fc = feature_collection(
        geofences.to_dict("records"), "geojson", ("code", "name", "zone_type", "speed_limit_kmh")
    )
    for f in fc["features"]:
        p = f["properties"]
        p["fill"] = list(ZONE_COLORS.get(p["zone_type"], [150, 150, 150, 40]))
        limit = f"{p['speed_limit_kmh']} km/h" if p["speed_limit_kmh"] else "sin límite"
        f["title"] = p["name"]
        f["body"] = f"{ZONE_LABELS.get(p['zone_type'], p['zone_type'])} · {limit}"
    return pdk.Layer(
        "GeoJsonLayer",
        fc,
        id="geofences",
        stroked=True,
        filled=True,
        get_fill_color="properties.fill",
        get_line_color=[60, 60, 60, 160],
        line_width_min_pixels=1,
        pickable=True,
    )


def track_layer(tracks: list[dict]) -> pdk.Layer:
    return pdk.Layer(
        "PathLayer",
        tracks,
        id="tracks",
        get_path="path",
        get_color="color",
        get_width="line_px",
        # pydeck convierte todo texto en expresión ("@@=pixels" → indefinido, y la ruta
        # se dibuja en unidades del mundo, gigante): hay que marcarlo como literal
        width_units=pdk.types.String("pixels"),
        joint_rounded=True,
        cap_rounded=True,
        pickable=True,
    )


def heatmap_layer(alerts: pd.DataFrame) -> pdk.Layer:
    weighted = alerts.assign(weight=alerts["severity"].map(SEVERITY_WEIGHT).fillna(1))
    return pdk.Layer(
        "HeatmapLayer",
        weighted[["lon", "lat", "weight"]],
        id="heatmap",
        get_position=["lon", "lat"],
        get_weight="weight",
        radius_pixels=40,
        opacity=0.6,
        color_range=[list(c) for c in HEATMAP_COLORS],
    )


def unit(alert_type: str) -> str:
    return " km/h" if alert_type == "SPEEDING" else ""


def alert_layer(alerts: pd.DataFrame) -> pdk.Layer:
    colored = with_colors(alerts, "severity", SEVERITY_COLORS)
    colored["title"] = [
        f"{ALERT_LABELS.get(r.alert_type, r.alert_type)} · "
        f"severidad {SEVERITY_SHORT.get(r.severity, r.severity)}"
        for r in colored.itertuples()
    ]
    colored["body"] = [
        "\n".join(
            (
                vehicle_label(r.vehicle_id, r.vehicle_key),
                f"{r.event_ts:%H:%M:%S} · {r.geofence_name or 'Fuera de geocerca'}",
                f"Observado {r.observed_value}{unit(r.alert_type)} · "
                f"umbral {r.threshold_value}{unit(r.alert_type)}",
            )
        )
        for r in colored.itertuples()
    ]
    colored["event_ts"] = colored["event_ts"].astype(str)
    return pdk.Layer(
        "ScatterplotLayer",
        colored[["lon", "lat", "color", "title", "body"]],
        id="alerts",
        get_position=["lon", "lat"],
        get_fill_color="color",
        get_radius=45,
        radius_min_pixels=4,
        stroked=True,
        get_line_color=[255, 255, 255],
        pickable=True,
    )


def hotspot_layers(hotspots: pd.DataFrame) -> list[pdk.Layer]:
    """Zonas calientes: polígono con opacidad según infracciones y su número de ranking."""
    records = hotspots.to_dict("records")
    fc = feature_collection(
        records,
        "geojson",
        ("hotspot_rank", "infractions", "severe_infractions", "vehicles", "area_km2"),
    )
    peak = max((r["infractions"] for r in records), default=1)
    for f in fc["features"]:
        p = f["properties"]
        p["fill"] = [*HOTSPOT_COLOR, int(40 + 110 * p["infractions"] / peak)]
        f["title"] = f"Zona caliente #{p['hotspot_rank']}"
        f["body"] = (
            f"{p['infractions']} excesos ({p['severe_infractions']} graves) · "
            f"{p['vehicles']} vehículo(s)\n{p['area_km2']} km² · histórico acumulado"
        )
    labels = [
        {"position": [r["label_lon"], r["label_lat"]], "text": f"#{r['hotspot_rank']}"}
        for r in records
    ]
    return [
        pdk.Layer(
            "GeoJsonLayer",
            fc,
            id="hotspots",
            filled=True,
            stroked=True,
            get_fill_color="properties.fill",
            get_line_color=[*HOTSPOT_COLOR, 230],
            line_width_min_pixels=2,
            pickable=True,
        ),
        pdk.Layer(
            "TextLayer",
            labels,
            id="hotspot-labels",
            get_position="position",
            get_text="text",
            get_size=15,
            get_color=[255, 255, 255],
            outline_width=3,
            outline_color=[*HOTSPOT_COLOR, 255],
            font_weight=700,
            font_settings={"sdf": True},
        ),
    ]


# --------------------------------------------------------------------- UI
def sidebar() -> tuple[DbRole, dict[str, bool], DeltaGenerator]:
    st.sidebar.title("Simón Geo")
    role = ROLES[st.sidebar.radio("Ver como", list(ROLES))]
    st.sidebar.caption(
        "El mismo tablero, dos usuarios de base de datos. El enmascaramiento ocurre en "
        "PostgreSQL (vistas `analytics.*`), no aquí."
    )
    simulator_slot = st.sidebar.container()  # el panel se llena cuando ya se conoce el día
    st.sidebar.subheader("Capas")
    layers = {
        "geofences": st.sidebar.checkbox("Geocercas", value=True),
        "schools_on_route": st.sidebar.toggle(
            "Zonas escolares: solo las de las rutas",
            value=True,
            help="Hay ~2.200 zonas escolares reales (una por sede, datos abiertos de "
            "Bogotá). Apágalo para verlas todas.",
        ),
        "tracks": st.sidebar.checkbox("Rutas del día", value=True),
        "heatmap": st.sidebar.checkbox("Mapa de calor de infracciones", value=True),
        "alerts": st.sidebar.checkbox("Puntos de infracción", value=True),
        "hotspots": st.sidebar.checkbox("Zonas calientes (histórico)", value=False),
        "only_focus": st.sidebar.toggle(
            "Solo el vehículo seleccionado",
            help="Oculta en el mapa las rutas, alertas y el mapa de calor de los demás "
            "vehículos. El vehículo se elige con clic en el ranking (por defecto, el de "
            "menor Driver Score).",
        ),
    }
    return role, layers, simulator_slot


def render_kpis(kpis: pd.DataFrame, alerts: pd.DataFrame) -> None:
    s = fleet_summary(kpis, alerts)
    cols = st.columns(5)
    cols[0].metric("Vehículos", s.vehicles)
    cols[1].metric("Distancia", f"{s.distance_km:,.1f} km")
    cols[2].metric("Driver Score medio", f"{s.avg_driver_score:.1f}" if s.avg_driver_score else "—")
    cols[3].metric("Alertas de velocidad", s.speeding_alerts)
    cols[4].metric("Alertas de combustible", s.fuel_alerts)
    if s.worst_vehicle:
        st.caption(f"Menor Driver Score del día: **{s.worst_vehicle}**")


def simulator_panel(role: DbRole, day: date, simulated: int) -> None:
    """Formulario "este vehículo va por esta ruta". Solo se muestra al administrador; la
    base además solo permite crear y borrar vehículos marcados como simulados."""
    routes, index = sandbox_catalog()
    by_code = {r.code: r for r in routes}
    with st.expander("Simular vehículo", icon=":material/add_road:"):
        # Fuera del formulario: los campos de la ruta cambian apenas se elige el modo
        if st.session_state.pop("sandbox_close_picker", False):
            st.session_state["sandbox_route_mode"] = "Existente"
        new_route = (
            st.radio(
                "Ruta", ["Existente", MAP_ROUTE_MODE], horizontal=True, key="sandbox_route_mode"
            )
            == MAP_ROUTE_MODE
        )
        with st.form("sandbox_form"):
            plate = st.text_input("Placa", value="VHC-1234-XYZ", max_chars=12)
            if new_route:
                st.caption("Marca el inicio y el fin en el mapa de la derecha.")
            else:
                route = st.selectbox(
                    "Ruta existente",
                    list(by_code),
                    format_func=lambda c: f"{c} · {by_code[c].name}",
                )
            profile = st.selectbox(
                "Conductor", list(PROFILE_LABELS), format_func=PROFILE_LABELS.get
            )
            fuel = st.slider("Combustible inicial (%)", 1, 100, 60)
            tank_col, use_col = st.columns(2)
            tank = tank_col.number_input("Tanque (L)", 10, 1000, 150, step=10)
            consumption = use_col.number_input("L/100 km", 3, 100, 30)
            departure = st.time_input("Salida (Bogotá)", value=time(8, 0), step=300)
            st.caption(f"Se simula el {day:%Y-%m-%d}.")
            submitted = st.form_submit_button("Simular", type="primary", width="stretch")
        if submitted:
            try:
                vehicle_id = plate.strip().upper()
                request = SandboxRequest(
                    vehicle_id=vehicle_id,
                    route_code=f"SBX-{vehicle_id}" if new_route else route,
                    profile=profile,
                    fuel_pct=float(fuel),
                    tank_l=float(tank),
                    consumption_l_100km=float(consumption),
                    start=datetime.combine(day, departure, tzinfo=ZoneInfo(LOCAL_TZ)),
                )
                custom_route = None
                if new_route:
                    points = st.session_state.get("sandbox_points", [])
                    if len(points) < 2:
                        raise RoutingError("Marca el inicio y el fin en el mapa")
                    with st.spinner("Calculando la ruta por las vías…"):
                        custom_route = load_route_plan(*points).planned(request.route_code)
                with st.spinner("API → Bronce → worker → Plata → Oro…"):
                    report = run_sandbox(
                        request, routes, index, get_settings(), custom_route=custom_route
                    )
            except (SandboxError, RoutingError) as exc:
                st.error(str(exc))
            else:
                # Se guarda la clave seudónima, no la placa: el aviso también lo ve el analista
                st.session_state["sandbox_last"] = {
                    "key": data.vehicle_key_of(conn_for(role), report.vehicle_id, day),
                    "route": custom_route.name if custom_route else report.route_code,
                    "packets": report.packets,
                    "elapsed_s": report.elapsed_s,
                }
                st.session_state.pop("ranking", None)  # el foco pasa al vehículo nuevo
                st.session_state["sandbox_points"] = []
                st.session_state["sandbox_close_picker"] = True  # el resultado queda a la vista
                st.cache_data.clear()
                st.rerun()
        if simulated and st.button(f"Borrar simulados ({simulated})", width="stretch"):
            purge(get_settings())
            st.session_state.pop("sandbox_last", None)
            st.session_state.pop("ranking", None)
            st.cache_data.clear()
            st.rerun()


@st.cache_data(ttl=3600, show_spinner=False)
def load_route_plan(origin: Place, destination: Place) -> RoutePlan:
    return plan_route(origin, destination)


def clear_route_points() -> None:
    st.session_state["sandbox_points"] = []


@st.fragment
def route_picker() -> None:
    """Mapa para marcar inicio y fin. Es un fragmento: un clic no recarga todo el tablero.

    Usa Folium porque el mapa de pydeck en Streamlit no entrega la coordenada de un clic
    libre, solo los objetos de sus capas.
    """
    points: list[Place] = st.session_state.setdefault("sandbox_points", [])
    plan, problem = None, st.session_state.pop("sandbox_click_problem", None)
    if len(points) == 2 and not problem:
        try:
            with st.spinner("Calculando la ruta por las vías…"):
                plan = load_route_plan(*points)
        except RoutingError as exc:
            problem = str(exc)

    with st.container(border=True):
        text_col, clear_col = st.columns([5, 1], vertical_alignment="center")
        if plan:
            text_col.markdown(
                f"**Ruta lista:** {plan.name} · **{plan.length_km} km**. Completa el formulario "
                "y pulsa **Simular**."
            )
        elif points:
            text_col.markdown("**2.** Haz clic en el mapa donde **termina** la ruta.")
        else:
            text_col.markdown("**1.** Haz clic en el mapa donde **inicia** la ruta.")
        clear_col.button(
            "Limpiar", width="stretch", disabled=not points, on_click=clear_route_points
        )
        if problem:
            st.warning(problem)

        base = folium.Map(location=[4.66, -74.11], zoom_start=11, control_scale=True)
        marks = folium.FeatureGroup(name="Ruta")
        if plan:
            folium.PolyLine(
                [(lat, lon) for lon, lat in plan.line.coords],
                color="#ff00c8",
                weight=5,
                opacity=0.9,
            ).add_to(marks)
        for place, color, name in zip(
            points, ("#00a651", "#d7263d"), ("Inicio", "Fin"), strict=False
        ):
            folium.CircleMarker(
                (place.lat, place.lon),
                radius=9,
                color="#ffffff",
                weight=3,
                fill=True,
                fill_color=color,
                fill_opacity=1,
                tooltip=name,
            ).add_to(marks)
        state = st_folium(
            base,
            key="route_picker",
            height=400,
            use_container_width=True,
            returned_objects=["last_clicked"],
            feature_group_to_add=marks,
        )

    click = (state or {}).get("last_clicked")
    if not click:
        return
    # El componente repite el último clic en cada recarga: solo cuenta si cambió
    coordinates = (round(click["lng"], 6), round(click["lat"], 6))
    if coordinates == st.session_state.get("sandbox_last_click"):
        return
    st.session_state["sandbox_last_click"] = coordinates
    place = Place(*coordinates)
    if in_service_area(place):
        st.session_state["sandbox_points"] = next_points(points, place)
    else:
        st.session_state["sandbox_click_problem"] = "Ese punto está fuera del área de Bogotá."
    st.rerun(scope="fragment")


def set_vehicle_filter(keys: list[str]) -> None:
    st.session_state["vehicle_filter"] = keys


def vehicle_picker(kpis: pd.DataFrame, simulated: set[str]) -> set[str]:
    """Filtro de un clic por vehículo, con atajos para encender o apagar todos."""
    short = compact_vehicle_labels(zip(kpis["vehicle_id"], kpis["vehicle_key"], strict=True))
    labels = {k: v + (" (sim)" if k in simulated else "") for k, v in short.items()}
    options = sorted(labels, key=labels.get)
    st.session_state["vehicle_filter"] = reconcile_selection(
        options,
        st.session_state.get("vehicle_filter", options),
        st.session_state.get("vehicle_known", ()),
    )
    st.session_state["vehicle_known"] = {*st.session_state.get("vehicle_known", ()), *options}

    st.sidebar.markdown(
        f"**Vehículos** · {len(st.session_state['vehicle_filter'])} de {len(options)}"
    )
    all_col, none_col = st.sidebar.columns(2)
    all_col.button("Todos", width="stretch", on_click=set_vehicle_filter, args=(options,))
    none_col.button("Ninguno", width="stretch", on_click=set_vehicle_filter, args=([],))
    chosen = st.sidebar.pills(
        "Vehículos",
        options,
        selection_mode="multi",
        format_func=labels.get,
        key="vehicle_filter",
        label_visibility="collapsed",
    )
    return set(chosen)


def render_sandbox_result(kpis: pd.DataFrame, alerts: pd.DataFrame) -> None:
    last = st.session_state.get("sandbox_last")
    row = kpis[kpis["vehicle_key"] == last["key"]] if last else kpis.iloc[0:0]
    if row.empty:
        return
    r = row.iloc[0]
    own = alerts[alerts["vehicle_key"] == r["vehicle_key"]]
    speeding = own[own["alert_type"] == "SPEEDING"]
    critical = int((speeding["severity"] == "CRITICAL").sum())
    fuel = "no alcanza" if (own["alert_type"] == "LOW_FUEL_RANGE").any() else "alcanza"
    st.success(
        f"**{r['label']}** por {last['route']}: Driver Score **{float(r['driver_score']):.1f}** · "
        f"{float(r['distance_km']):.1f} km · {len(speeding)} excesos ({critical} críticos) · "
        f"combustible para la ruta: **{fuel}** · {last['packets']} paquetes procesados en "
        f"{last['elapsed_s']} s",
        icon=":material/check_circle:",
    )


def main() -> None:
    role, enabled, simulator_slot = sidebar()
    try:
        days = load_days(role)
        geofences, hotspots, (db_user, is_admin) = load_static(role)
    except psycopg.OperationalError as exc:
        st.error(
            "No hay conexión con PostGIS. Levanta la base (`docker compose up -d --wait`, o "
            "sin Docker `python -m simon_geo.bootstrap`) y procesa datos con "
            f"`python -m simon_geo.processing`.\n\n`{exc}`"
        )
        st.stop()

    st.title("Flota · rutas, infracciones y Driver Score")
    st.caption(
        f"Conectado como `{db_user}` · "
        + ("IDs reales (administrador)" if is_admin else "IDs enmascarados (analista)")
    )
    if not days:
        st.info("Aún no hay datos en la capa Oro. Ejecuta el simulador y el worker (SETUP.md).")
        st.stop()

    day = st.sidebar.selectbox("Día (hora de Bogotá)", days, format_func=lambda d: f"{d:%Y-%m-%d}")
    kpis, alerts = load_day(role, day)
    # El color sigue al vehículo (orden estable por clave), no a su posición en el
    # ranking: filtrar vehículos no repinta a los demás, y mapa y gráficos coinciden.
    simulated = load_sandbox_keys(role)
    if is_admin:
        with simulator_slot:
            simulator_panel(role, day, len(simulated))
    track_color = {
        key: TRACK_COLORS[i % len(TRACK_COLORS)]
        for i, key in enumerate(sorted(set(kpis["vehicle_key"]) - simulated))
    } | {key: SANDBOX_COLOR for key in simulated}
    kpis = kpis.assign(
        label=[
            vehicle_label(r.vehicle_id, r.vehicle_key)
            + (" · simulado" if r.vehicle_key in simulated else "")
            for r in kpis.itertuples()
        ]
    )
    render_sandbox_result(kpis, alerts)
    if is_admin and st.session_state.get("sandbox_route_mode") == MAP_ROUTE_MODE:
        route_picker()
    keys = vehicle_picker(kpis, simulated)
    kpis = kpis[kpis["vehicle_key"].isin(keys)]
    alerts = alerts[alerts["vehicle_key"].isin(keys)]

    render_kpis(kpis, alerts)
    if kpis.empty:
        st.info("Elige al menos un vehículo en la barra lateral.")
        st.stop()

    # Vehículo enfocado: el que se eligió con clic en el ranking (el estado del gráfico
    # ya está disponible al principio de la recarga) o, si no hay, el de peor score.
    picked = [
        p["vehicle_key"]
        for p in st.session_state.get("ranking", {}).get("selection", {}).get(charts.SELECTION, [])
        if p.get("vehicle_key") in keys
    ]
    last_simulated = st.session_state.get("sandbox_last", {}).get("key")
    if not picked and last_simulated in keys:
        picked = [last_simulated]
    focus = picked[0] if picked else kpis["vehicle_key"].iloc[0]

    # En el mapa: o se resalta el vehículo enfocado, o se deja solo él
    solo = enabled["only_focus"]
    map_kpis = kpis[kpis["vehicle_key"] == focus] if solo else kpis
    map_alerts = alerts[alerts["vehicle_key"] == focus] if solo else alerts
    highlight = picked and not solo
    tracks = [
        {
            "name": r.label,
            "path": line_path(r.track_geojson),
            "color": [
                *track_color[r.vehicle_key],
                255 if not highlight or r.vehicle_key == focus else 70,
            ],
            "line_px": 6 if highlight and r.vehicle_key == focus else 4,
            "title": r.label,
            "body": f"{r.distance_km} km recorridos · Driver Score {r.driver_score}",
        }
        for r in map_kpis.itertuples()
        if r.track_geojson
    ]
    layers = []
    shown_geofences = (
        geofences_on_paths(geofences, (t["path"] for t in tracks))
        if enabled["schools_on_route"]
        else geofences
    )
    if enabled["geofences"] and len(shown_geofences):
        layers.append(geofence_layer(shown_geofences))
    if enabled["hotspots"] and len(hotspots):
        layers.extend(hotspot_layers(hotspots))
    if enabled["heatmap"] and len(map_alerts):
        layers.append(heatmap_layer(map_alerts))
    if enabled["tracks"] and tracks:
        layers.append(track_layer(tracks))
    if enabled["alerts"] and len(map_alerts):
        layers.append(alert_layer(map_alerts))

    view = view_for((t["path"] for t in tracks), map_alerts)
    map_col, legend_col = st.columns([4, 1])
    with map_col:
        st.html(MAP_CSS)
        with st.container(key="basemap_switch"):
            ortho = st.toggle(
                "Ortofoto 2025",
                help="Ortofoto urbana 2025 de Bogotá (Catastro / IDECA) bajo las vías y los "
                "nombres del mapa; se ve a partir de zoom 10.",
            )
        map_style = ortho_style_url() if ortho else "light"
        st.pydeck_chart(
            pdk.Deck(
                layers=layers,
                initial_view_state=pdk.ViewState(**view),
                tooltip=TOOLTIP,
                map_provider="carto",
                map_style=map_style,
            ),
            height=560,
        )
    with legend_col:
        st.markdown("**Leyenda**")
        legend = build_legend(
            enabled,
            zones=zip(
                shown_geofences["zone_type"], shown_geofences["speed_limit_kmh"], strict=True
            ),
            tracks=((t["name"], t["color"][:3]) for t in tracks),
            severities=map_alerts["severity"],
        )
        st.html(legend_html(legend))

    hex_colors = {k: charts.hex_color(c) for k, c in track_color.items()}
    rank_col, profile_col = st.columns([2, 3], gap="large")
    with rank_col:
        st.subheader("Ranking de Driver Score")
        st.caption("Clic en una barra para ver su perfil y resaltar su ruta · doble clic: quitar.")
        st.altair_chart(
            charts.score_ranking(kpis, hex_colors),
            key="ranking",
            on_select="rerun",
            width="stretch",
        )
    with profile_col:
        focus_label = kpis.loc[kpis["vehicle_key"] == focus, "label"].iloc[0]
        st.subheader(f"Perfil del día · {focus_label}")
        st.caption(
            "Arrastra para desplazar, rueda del mouse para hacer zoom, doble clic para volver."
        )
        profile = load_profile(role, day, focus)
        if profile.empty:
            st.info("Sin telemetría enriquecida para este vehículo en el día.")
        else:
            st.altair_chart(
                charts.vehicle_profile(
                    profile, hex_colors[focus], PipelineParameters().fuel_safety_factor
                )
            )

    if len(alerts):
        zone_col, time_col = st.columns(2, gap="large")
        with zone_col:
            st.subheader("Infracciones por zona")
            zone_view = st.radio(
                "Agrupar infracciones",
                ["Por tipo de zona", f"Top {charts.TOP_ZONES} geocercas"],
                horizontal=True,
                label_visibility="collapsed",
                key="zone_view",
            )
            st.altair_chart(
                charts.infractions_by_zone_type(alerts)
                if zone_view == "Por tipo de zona"
                else charts.infractions_by_zone(alerts)
            )
        with time_col:
            st.subheader("Infracciones en el tiempo")
            st.altair_chart(charts.infractions_over_time(alerts))

    with st.expander("Tablas de detalle"):
        st.markdown("**Driver Score por vehículo**")
        st.dataframe(
            kpis.drop(columns=["track_geojson", "label"]),
            hide_index=True,
            column_config={
                "driver_score": st.column_config.ProgressColumn(
                    "Driver Score", min_value=0, max_value=100, format="%.1f"
                ),
                "speeding_time_pct": st.column_config.NumberColumn("% tiempo en exceso"),
            },
        )
        st.markdown("**Infracciones del día**")
        st.dataframe(
            alerts[
                [
                    "event_ts",
                    "vehicle_id",
                    "alert_type",
                    "severity",
                    "geofence_name",
                    "observed_value",
                    "threshold_value",
                ]
            ],
            hide_index=True,
        )


DATOS_ABIERTOS = "https://datosabiertos.bogota.gov.co/dataset"
# (capa, fuente, enlace, licencia)
CREDITS = (
    (
        "Rutas, corredores y mapa del selector de puntos",
        "© colaboradores de OpenStreetMap; rutas calculadas con OSRM",
        "https://www.openstreetmap.org/copyright",
        "ODbL",
    ),
    (
        "Perímetro urbano (POT)",
        "Secretaría Distrital de Planeación",
        f"{DATOS_ABIERTOS}/perimetro-urbano-bogota-d-c",
        "CC BY 4.0",
    ),
    (
        "Zonas escolares (100 m por sede)",
        "Colegios Bogotá D.C. 12/2025, Secretaría de Educación del Distrito",
        f"{DATOS_ABIERTOS}/colegios-bogota-d-c",
        "CC BY-SA 4.0",
    ),
    (
        "Tramos y puntos críticos de siniestralidad 2022–2025",
        "Oficina de Seguridad Vial, Secretaría Distrital de Movilidad",
        "https://sig.simur.gov.co/arcgis/rest/services/DatosAbiertos",
        "CC BY 4.0",
    ),
    (
        "Ortofoto urbana 2025",
        "Secretaría Distrital de Planeación, publicada por IDECA – UAECD",
        "https://serviciosgis.catastrobogota.gov.co/image/rest/services/"
        "imagenesfunciones/orthourbana2025funcion/ImageServer",
        "Servicio público de IDECA",
    ),
    ("Mapa base", "© CARTO, © OpenStreetMap", "https://carto.com/attributions", "—"),
)


def render_credits() -> None:
    st.divider()
    with st.expander("Fuentes de datos y licencias", icon=":material/info:"):
        rows = [
            f"| {layer} | [{source}]({url}) | {license_} |"
            for layer, source, url, license_ in CREDITS
        ]
        st.markdown("\n".join(["| Capa | Fuente | Licencia |", "|---|---|---|", *rows]))
        st.caption("La flota, la telemetría y el patio logístico son simulados.")


main()
render_credits()
