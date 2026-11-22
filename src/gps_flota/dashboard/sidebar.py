"""Barra lateral: rol, capas y filtro de vehículos."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from gps_flota.config import DbRole
from gps_flota.dashboard.views import compact_vehicle_labels, reconcile_selection

ROLES = {"Analista BI (IDs enmascarados)": DbRole.ANALYST, "Administrador": DbRole.ADMIN}


def sidebar() -> tuple[DbRole, dict[str, bool], DeltaGenerator]:
    st.sidebar.title("Seguimiento GPS flota vehicular")
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
