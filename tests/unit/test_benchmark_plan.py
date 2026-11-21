"""Lectura de planes EXPLAIN y configuración del benchmark."""

from __future__ import annotations

from datetime import date

import pytest

from simon_geo.benchmark.plan import PlanSummary, median_ms, summarize
from simon_geo.benchmark.runner import BenchmarkConfig, scenarios


def plan(root: dict, execution_ms: float = 12.5) -> list[dict]:
    return [{"Plan": root, "Planning Time": 0.4, "Execution Time": execution_ms}]


PRUNED_BITMAP = plan(
    {
        "Node Type": "Aggregate",
        "Actual Rows": 1,
        "Shared Hit Blocks": 120,
        "Shared Read Blocks": 30,
        "Plans": [
            {
                "Node Type": "Append",
                "Subplans Removed": 6,
                "Plans": [
                    {
                        "Node Type": "Bitmap Heap Scan",
                        "Schema": "bench",
                        "Relation Name": "telemetry_raw_p20260920",
                        "Plans": [
                            {
                                "Node Type": "Bitmap Index Scan",
                                "Index Name": "telemetry_raw_p20260920_geom_idx",
                            }
                        ],
                    },
                    {
                        "Node Type": "Seq Scan",
                        "Schema": "ref",
                        "Relation Name": "geofence",
                    },
                ],
            }
        ],
    }
)


class TestSummarize:
    def test_timings_and_buffers(self) -> None:
        s = summarize(PRUNED_BITMAP)
        assert (s.execution_ms, s.planning_ms) == (12.5, 0.4)
        assert (s.shared_hit, s.shared_read, s.buffers) == (120, 30, 150)
        assert s.rows == 1

    def test_partitions_scanned_and_pruned(self) -> None:
        s = summarize(PRUNED_BITMAP, partitioned_table="telemetry_raw")
        assert s.partitions_scanned == 1  # ref.geofence no es partición
        assert s.partitions_pruned == 6

    def test_without_table_filter_counts_every_relation(self) -> None:
        assert summarize(PRUNED_BITMAP).partitions_scanned == 2

    def test_access_paths_use_parent_index_name(self) -> None:
        s = summarize(PRUNED_BITMAP, partitioned_table="telemetry_raw")
        assert s.access_paths == (
            "Bitmap Heap Scan",
            "Bitmap Index Scan (telemetry_raw_geom_idx)",
            "Seq Scan",
        )

    def test_access_paths_use_given_parent_names(self) -> None:
        s = summarize(
            PRUNED_BITMAP,
            index_names={"telemetry_raw_p20260920_geom_idx": "telemetry_raw_geom_gist"},
        )
        assert "Bitmap Index Scan (telemetry_raw_geom_gist)" in s.access_paths

    def test_rejects_non_analyze_output(self) -> None:
        with pytest.raises(ValueError):
            summarize([{"Query": "x"}])
        with pytest.raises(ValueError):
            summarize([])

    def test_median(self) -> None:
        summaries = [summarize(plan({"Node Type": "Result"}, ms)) for ms in (30.0, 10.0, 20.0)]
        assert median_ms(summaries) == 20.0
        with pytest.raises(ValueError):
            median_ms([])

    def test_summary_is_immutable(self) -> None:
        s = summarize(PRUNED_BITMAP)
        assert isinstance(s, PlanSummary)
        with pytest.raises(AttributeError):
            s.execution_ms = 0  # type: ignore[misc]


class TestBenchmarkConfig:
    def test_expected_rows(self) -> None:
        assert BenchmarkConfig(vehicles=500, days=7, interval_s=60).expected_rows == 5_040_000

    def test_mid_day(self) -> None:
        cfg = BenchmarkConfig(days=7, first_day=date(2026, 9, 1))
        assert cfg.mid_day == date(2026, 9, 4)

    @pytest.mark.parametrize("field", ["vehicles", "days", "interval_s", "repetitions"])
    def test_rejects_non_positive(self, field: str) -> None:
        with pytest.raises(ValueError):
            BenchmarkConfig(**{field: 0})

    def test_vehicle_ids_have_four_digits(self) -> None:
        with pytest.raises(ValueError):
            BenchmarkConfig(vehicles=10_000)

    def test_scenarios_filter_by_the_mid_day(self) -> None:
        cfg = BenchmarkConfig(days=7, first_day=date(2026, 9, 1))
        zone = next(s for s in scenarios(cfg) if s.key == "zone")
        assert "'2026-09-04'" in zone.variants[0].query
        assert zone.variants[1].drop_index is not None  # variante sin GiST
