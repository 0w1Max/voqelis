from __future__ import annotations

from pathlib import Path

from voqelis.planner.config import PlannerConfig


def test_checked_in_planner_config_matches_code_defaults():
    config_path = Path(__file__).resolve().parents[1] / "config" / "planner.json"

    assert config_path.is_file()
    assert PlannerConfig.from_json_file(config_path) == PlannerConfig()
