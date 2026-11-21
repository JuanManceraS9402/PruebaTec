"""Gráficos interactivos del tablero (Altair / Vega-Lite).

Funciones puras: reciben DataFrames ya consultados y devuelven especificaciones de
gráfico, así se prueban sin base de datos ni navegador.

Reglas de color: el vehículo conserva el mismo color que su ruta en el mapa (el
color sigue a la entidad, no a su posición en el ranking) y la severidad usa la
misma escala que los puntos de infracción.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

import altair as alt
import pandas as pd

from simon_geo.dashboard.views import SEVERITY_COLORS

SEVERITY_ORDER: Final = list(SEVERITY_COLORS)
SEVERITY_SHORT: Final = {"LOW": "Baja", "MEDIUM": "Media", "HIGH": "Alta", "CRITICAL": "Crítica"}
NEUTRAL: Final = "#8a8f98"
ALERT_RED: Final = "#c8102e"
INK_MUTED: Final = "#5f6670"
SELECTION: Final = "vehiculo"  # nombre del parámetro de selección del ranking


def hex_color(rgba: Sequence[int]) -> str:
    r, g, b = rgba[:3]
    return f"#{r:02x}{g:02x}{b:02x}"


def _axis(**kw: object) -> alt.Axis:
    return alt.Axis(labelColor=INK_MUTED, titleColor=INK_MUTED, gridOpacity=0.35, **kw)


def _local_naive(ts: pd.Series) -> pd.Series:
    """ISO sin zona: Vega-Lite lo interpreta como hora local y lo muestra tal cual,
    sin volver a convertirlo a la zona horaria del navegador."""
    return pd.to_datetime(ts).dt.tz_localize(None).dt.strftime("%Y-%m-%dT%H:%M:%S")


def _severity_scale() -> alt.Scale:
    return alt.Scale(
        domain=[SEVERITY_SHORT[s] for s in SEVERITY_ORDER],
        range=[hex_color(SEVERITY_COLORS[s]) for s in SEVERITY_ORDER],
    )


# ------------------------------------------------------------- ranking de flota
def score_ranking(kpis: pd.DataFrame, colors: Mapping[str, str]) -> alt.Chart:
    """Barras de Driver Score por vehículo. Clic en una barra = seleccionar vehículo
    (parámetro ``vehiculo``); el tablero lo usa para el detalle y para resaltar la ruta.

    ``kpis`` necesita ``label``, ``vehicle_key`` y las columnas de KPIs; ``colors``
    mapea ``vehicle_key`` → color hex de su ruta.
    """
    frame = kpis.assign(
        color=kpis["vehicle_key"].map(colors).fillna(NEUTRAL),
        score=kpis["driver_score"].astype(float).round(1),
        speeding=kpis["speeding_time_pct"].astype(float).round(1),
        km=kpis["distance_km"].astype(float).round(1),
    )[["label", "vehicle_key", "color", "score", "speeding", "km", "harsh_events"]]
    pick = alt.selection_point(name=SELECTION, fields=["vehicle_key"], on="click", clear="dblclick")
    y = alt.Y("label:N", sort="-x", title=None, axis=alt.Axis(labelColor="#30343b", labelLimit=180))
    tooltip = [
        alt.Tooltip("label:N", title="Vehículo"),
        alt.Tooltip("score:Q", title="Driver Score", format=".1f"),
        alt.Tooltip("speeding:Q", title="% tiempo en exceso", format=".1f"),
        alt.Tooltip("km:Q", title="Distancia (km)", format=".1f"),
        alt.Tooltip("harsh_events:Q", title="Eventos bruscos"),
    ]
    base = alt.Chart(frame).encode(y=y, tooltip=tooltip)
    bars = (
        base.mark_bar(cornerRadiusEnd=4, height={"band": 0.62})
        .encode(
            x=alt.X(
                "score:Q",
                title="Driver Score (0–100)",
                scale=alt.Scale(domain=[0, 100]),
                axis=_axis(values=[0, 25, 50, 75, 100]),
            ),
            color=alt.Color("color:N", scale=None, legend=None),
            opacity=alt.condition(pick, alt.value(1.0), alt.value(0.3)),
        )
        .add_params(pick)
    )
    labels = base.mark_text(align="left", dx=5, color="#30343b", fontWeight=600).encode(
        x="score:Q", text=alt.Text("score:Q", format=".1f")
    )
    return (bars + labels).properties(height=alt.Step(34))


# ------------------------------------------------------------- infracciones
def _with_severity(alerts: pd.DataFrame) -> pd.DataFrame:
    return alerts.assign(
        sev=alerts["severity"].map(SEVERITY_SHORT).fillna(alerts["severity"]),
        sev_order=alerts["severity"].map({s: i for i, s in enumerate(SEVERITY_ORDER)}),
    )


def infractions_by_zone(alerts: pd.DataFrame) -> alt.Chart:
    """Infracciones del día por geocerca, apiladas por severidad."""
    frame = _with_severity(alerts).assign(zone=alerts["geofence_name"].fillna("Fuera de geocerca"))
    counts = frame.groupby(["zone", "sev", "sev_order"], as_index=False).size()
    return (
        alt.Chart(counts)
        .mark_bar(height={"band": 0.62}, stroke="white", strokeWidth=2)
        .encode(
            y=alt.Y("zone:N", sort="-x", title=None, axis=alt.Axis(labelLimit=200)),
            x=alt.X("sum(size):Q", title="Infracciones", axis=_axis(tickMinStep=1)),
            color=alt.Color(
                "sev:N",
                title="Severidad",
                scale=_severity_scale(),
                legend=alt.Legend(orient="top"),
            ),
            order=alt.Order("sev_order:Q"),
            tooltip=[
                alt.Tooltip("zone:N", title="Zona"),
                alt.Tooltip("sev:N", title="Severidad"),
                alt.Tooltip("size:Q", title="Infracciones"),
            ],
        )
        .properties(height=alt.Step(30))
    )


def time_bucket_minutes(span: pd.Timedelta) -> int:
    """Ancho de barra según el rango cubierto: ~12–30 barras en vez de una sola."""
    if span <= pd.Timedelta(hours=2):
        return 5
    if span <= pd.Timedelta(hours=8):
        return 15
    return 60


def infractions_over_time(alerts: pd.DataFrame) -> alt.Chart:
    """Infracciones del día en el tiempo (hora local), apiladas por severidad. El ancho
    de cada barra se adapta al rango del día: 5, 15 o 60 minutos."""
    ts = pd.to_datetime(alerts["event_ts"])
    minutes = time_bucket_minutes(ts.max() - ts.min()) if len(ts) else 60
    frame = _with_severity(alerts).assign(t=_local_naive(ts.dt.floor(f"{minutes}min")))
    counts = frame.groupby(["t", "sev", "sev_order"], as_index=False).size()
    return (
        alt.Chart(counts)
        .mark_bar(stroke="white", strokeWidth=1)
        .encode(
            x=alt.X(
                "t:T",
                title=f"Hora (Bogotá) · barras de {minutes} min",
                axis=_axis(format="%H:%M", labelOverlap=True),
                scale=alt.Scale(padding=12),
            ),
            # ancho de barra = el intervalo, sin depender del número de barras
            x2=alt.X2("t_end:T"),
            y=alt.Y("sum(size):Q", title="Infracciones", axis=_axis(tickMinStep=1)),
            color=alt.Color("sev:N", title="Severidad", scale=_severity_scale(), legend=None),
            order=alt.Order("sev_order:Q"),
            tooltip=[
                alt.Tooltip("t:T", title="Desde", format="%H:%M"),
                alt.Tooltip("sev:N", title="Severidad"),
                alt.Tooltip("size:Q", title="Infracciones"),
            ],
        )
        .transform_calculate(t_end=f"toDate(datum.t) + {minutes * 60_000}")
        .properties(height=190)
    )


# ------------------------------------------------------------- perfil de vehículo
def _timeline(
    frame: pd.DataFrame,
    *,
    series: Mapping[str, str],
    color: str,
    y_title: str,
    flag: str,
    flag_label: str,
    tooltip: Sequence[alt.Tooltip],
    hover_name: str,
    zoom: alt.Parameter,
    height: int,
) -> alt.LayerChart:
    """Dos series de la misma unidad en el tiempo (la medida, en el color del vehículo,
    y su referencia, gris discontinua). Donde ``flag`` es verdadero se sombrea en rojo
    la brecha entre ambas. Incluye cruz de lectura y zoom/paneo horizontal."""
    (measure, measure_label), (reference, reference_label) = series.items()
    domain = [measure_label, reference_label, flag_label]
    color_scale = alt.Scale(domain=domain, range=[color, NEUTRAL, ALERT_RED])
    x = alt.X("t:T", title=None, axis=_axis(format="%H:%M", labelOverlap=True))
    long = frame.melt(
        id_vars=["t"], value_vars=[measure, reference], var_name="key", value_name="value"
    ).assign(serie=lambda d: d["key"].map(series))
    lines = (
        alt.Chart(long)
        .mark_line(strokeWidth=2, interpolate="step-after")
        .encode(
            x=x,
            y=alt.Y("value:Q", title=y_title, axis=_axis()),
            color=alt.Color(
                "serie:N",
                title=None,
                scale=color_scale,
                legend=alt.Legend(
                    orient="top", symbolType="stroke", symbolStrokeWidth=3, symbolOpacity=1
                ),
            ),
            strokeDash=alt.StrokeDash(
                "serie:N",
                scale=alt.Scale(domain=domain[:2], range=[[1, 0], [5, 4]]),
                legend=None,
            ),
        )
    )
    # brecha: altura cero donde no hay alerta, así el área solo aparece en los tramos marcados
    gap = (
        alt.Chart(frame.assign(gap_to=frame[reference].where(frame[flag], frame[measure])))
        .transform_calculate(serie=f"'{flag_label}'")
        .mark_area(opacity=0.35, interpolate="step-after")
        .encode(
            x="t:T", y=f"{measure}:Q", y2="gap_to:Q", color=alt.Color("serie:N", scale=color_scale)
        )
    )
    hover = alt.selection_point(
        name=hover_name, nearest=True, on="pointerover", fields=["t"], empty=False
    )
    catcher = (
        alt.Chart(frame)
        .mark_rule(strokeWidth=12, opacity=0)  # objetivo de hover más ancho que la regla
        .encode(x="t:T", tooltip=list(tooltip))
        # los dos parámetros en la misma capa: Altair duplica las señales si se reparten
        # entre capas de un gráfico que luego va dentro de un vconcat
        .add_params(hover, zoom)
    )
    rule = (
        alt.Chart(frame)
        .mark_rule(color=INK_MUTED, strokeWidth=1)
        .encode(x="t:T", opacity=alt.condition(hover, alt.value(0.8), alt.value(0)))
    )
    dot = (
        alt.Chart(frame)
        .mark_circle(size=70, color=color, stroke="white", strokeWidth=2)
        .encode(x="t:T", y=f"{measure}:Q", opacity=alt.condition(hover, alt.value(1), alt.value(0)))
    )
    return alt.layer(gap, lines, catcher, rule, dot).properties(height=height)


def vehicle_profile(profile: pd.DataFrame, color: str, safety_factor: float) -> alt.VConcatChart:
    """Velocidad frente al límite de la geocerca y autonomía frente a la ruta restante,
    con el eje de tiempo compartido: arrastra para desplazar, rueda para hacer zoom
    (en las dos gráficas a la vez) y doble clic para volver."""
    frame = profile.assign(
        t=_local_naive(profile["event_ts"]),
        speed=profile["speed_kmh"].astype(float),
        limit=profile["applied_speed_limit_kmh"].astype(float),
        excess=profile["speed_excess_kmh"].astype(float).fillna(0).clip(lower=0),
        fuel=profile["fuel_level_pct"].astype(float),
        range_km=profile["est_range_km"].astype(float),
        need_km=profile["remaining_route_km"].astype(float) * safety_factor,
        speeding=profile["is_speeding"].fillna(False).astype(bool),
        fuel_risk=profile["is_low_fuel_risk"].fillna(False).astype(bool),
    )
    zoom = alt.selection_interval(name="zoom_speed", bind="scales", encodings=["x"])
    hour = alt.Tooltip("t:T", title="Hora", format="%H:%M:%S")
    speed = _timeline(
        frame,
        series={"speed": "Velocidad", "limit": "Límite de la geocerca"},
        color=color,
        y_title="km/h",
        flag="speeding",
        flag_label="Exceso de velocidad",
        tooltip=[
            hour,
            alt.Tooltip("speed:Q", title="Velocidad (km/h)", format=".0f"),
            alt.Tooltip("limit:Q", title="Límite (km/h)", format=".0f"),
            alt.Tooltip("excess:Q", title="Exceso (km/h)", format=".0f"),
        ],
        hover_name="hover_speed",
        zoom=zoom,
        height=210,
    )
    fuel = _timeline(
        frame,
        series={
            "range_km": "Autonomía estimada",
            "need_km": f"Ruta restante × {safety_factor:.2f}".replace(".", ","),
        },
        color=color,
        y_title="km",
        flag="fuel_risk",
        flag_label="No alcanza para la ruta",
        tooltip=[
            hour,
            alt.Tooltip("fuel:Q", title="Combustible (%)", format=".1f"),
            alt.Tooltip("range_km:Q", title="Autonomía (km)", format=".1f"),
            alt.Tooltip("need_km:Q", title="Necesario (km)", format=".1f"),
        ],
        hover_name="hover_fuel",
        zoom=alt.selection_interval(name="zoom_fuel", bind="scales", encodings=["x"]),
        height=170,
    )
    return alt.vconcat(speed, fuel, spacing=18).resolve_scale(x="shared", color="independent")
