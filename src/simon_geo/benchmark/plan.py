"""Resumen de planes ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` (sin E/S)."""

from __future__ import annotations

import statistics
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Nodos que leen datos: son los que dicen qué estrategia de acceso eligió el planner.
_SCAN_NODES = frozenset(
    {
        "Seq Scan",
        "Index Scan",
        "Index Only Scan",
        "Bitmap Heap Scan",
        "Bitmap Index Scan",
    }
)


@dataclass(frozen=True, slots=True)
class PlanSummary:
    execution_ms: float
    planning_ms: float
    shared_hit: int
    shared_read: int
    partitions_scanned: int
    partitions_pruned: int
    access_paths: tuple[str, ...]
    """Estrategias de acceso distintas, p. ej. ``Bitmap Index Scan (telemetry_raw_geom_gist)``."""
    rows: int

    @property
    def buffers(self) -> int:
        """Páginas de 8 KB tocadas (caché + disco)."""
        return self.shared_hit + self.shared_read


def _walk(node: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    yield node
    for child in node.get("Plans", ()):
        yield from _walk(child)


def _strip_partition_suffix(index_name: str) -> str:
    """``telemetry_raw_p20261118_geom_idx`` → índice padre aproximado.

    Las particiones heredan índices con nombres generados; para el resumen basta
    con saber el tipo de acceso, así que se conserva el nombre tal cual salvo que
    sea el de una partición (contiene ``_p`` + 8 dígitos).
    """
    parts = index_name.split("_")
    kept = [p for p in parts if not (p.startswith("p") and len(p) == 9 and p[1:].isdigit())]
    return "_".join(kept)


def summarize(
    explain_json: Sequence[Mapping[str, Any]],
    partitioned_table: str | None = None,
    index_names: Mapping[str, str] | None = None,
) -> PlanSummary:
    """Resume la salida de ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)``.

    ``partitioned_table``: nombre (sin esquema) de la tabla particionada; solo sus
    particiones (``<tabla>_p…`` / ``<tabla>_default``) cuentan como escaneadas.
    ``index_names``: índice de partición → índice padre (nombre legible en el informe).
    """
    aliases = index_names or {}
    if not explain_json or "Plan" not in explain_json[0]:
        raise ValueError("Se esperaba la salida JSON de EXPLAIN (ANALYZE, ...)")
    top = explain_json[0]
    root = top["Plan"]

    relations: set[str] = set()
    pruned = 0
    paths: list[str] = []
    for node in _walk(root):
        pruned += int(node.get("Subplans Removed", 0))
        node_type = node.get("Node Type", "")
        if node_type not in _SCAN_NODES:
            continue
        relation = node.get("Relation Name")
        if relation and (partitioned_table is None or relation.startswith(f"{partitioned_table}_")):
            relations.add(f"{node.get('Schema', '')}.{relation}")
        label = node_type
        if "Index Name" in node:
            name = node["Index Name"]
            label += f" ({aliases.get(name) or _strip_partition_suffix(name)})"
        if label not in paths:
            paths.append(label)

    return PlanSummary(
        execution_ms=float(top["Execution Time"]),
        planning_ms=float(top.get("Planning Time", 0.0)),
        shared_hit=int(root.get("Shared Hit Blocks", 0)),
        shared_read=int(root.get("Shared Read Blocks", 0)),
        partitions_scanned=len(relations),
        partitions_pruned=pruned,
        access_paths=tuple(paths),
        rows=int(root.get("Actual Rows", 0)),
    )


def median_ms(summaries: Sequence[PlanSummary]) -> float:
    if not summaries:
        raise ValueError("Sin mediciones")
    return statistics.median(s.execution_ms for s in summaries)
