"""Panel "Simular vehículo" y selector de inicio y fin en el mapa."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from simon_geo.config import DbRole, get_settings
from simon_geo.dashboard import data
from simon_geo.dashboard.loaders import LOCAL_TZ, conn_for, load_route_plan, sandbox_catalog
from simon_geo.routing import Place, RoutingError, in_service_area, next_points
from simon_geo.sandbox import SandboxError, SandboxRequest, purge
from simon_geo.sandbox import run as run_sandbox

MAP_ROUTE_MODE = "En el mapa"
PROFILE_LABELS = {"compliant": "Cumplido", "moderate": "Moderado", "aggressive": "Agresivo"}


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
