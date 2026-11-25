"""Acceptance tests for the hardware-free competition demonstration."""

from __future__ import annotations

import builtins
import json
import stat
from pathlib import Path

import pytest

from rear_warning.demo.cli import main
from rear_warning.demo.render import render_demo_html
from rear_warning.demo.scenarios import build_demo_bundle


def bundle() -> dict[str, object]:
    return build_demo_bundle(
        git_head="3d6d71b5f1eb9d980d6a976d6ddf0ab4da9c0685",
        git_dirty=True,
        generated_at_utc="2026-10-04T00:00:00+00:00",
    )


def scenarios_by_id() -> dict[str, dict[str, object]]:
    result = bundle()["scenarios"]
    assert isinstance(result, list)
    return {scenario["id"]: scenario for scenario in result}


def test_scenario_a_requires_baseline_and_target_persistence() -> None:
    scenario = scenarios_by_id()["A"]
    frames = scenario["frames"]
    targets = [
        frame
        for frame in frames
        if frame["output"]["classification"] == "target_approach"
    ]
    candidates = [
        frame
        for frame in frames
        if frame["output"]["candidate_classification"] == "target_approach"
    ]

    assert candidates
    assert targets
    assert all(
        frame["output"]["stationary_baseline_ready"] for frame in targets
    )
    assert (
        targets[0]["timestamp_seconds"] - candidates[0]["timestamp_seconds"]
        >= 0.4 - 1e-9
    )
    assert frames[-1]["output"]["classification"] == "uncertain"
    assert not frames[-1]["output"]["stationary_baseline_ready"]


def test_scenario_b_retains_self_motion_semantics() -> None:
    frames = scenarios_by_id()["B"]["frames"]
    self_approach = [
        frame
        for frame in frames
        if frame["output"]["classification"] == "static_or_self_approach"
    ]

    assert self_approach
    assert any(
        frame["output"]["translation_detected"] for frame in self_approach
    )
    assert any(frame["output"]["rotation_detected"] for frame in self_approach)
    assert all(
        frame["output"]["classification"] != "target_approach"
        for frame in frames
    )


def test_scenario_c_never_claims_target_approach_without_baseline() -> None:
    frames = scenarios_by_id()["C"]["frames"]

    assert any(frame["output"]["distance_trend_ready"] for frame in frames)
    assert any(frame["output"]["own_motion_ready"] for frame in frames)
    assert all(
        frame["output"]["classification"] == "uncertain" for frame in frames
    )
    assert all(
        not frame["output"]["stationary_baseline_ready"] for frame in frames
    )


def test_scenario_d_clears_and_rebuilds_baseline_without_polluting_trend() -> None:
    frames = scenarios_by_id()["D"]["frames"]
    stale = [
        frame
        for frame in frames
        if frame["input"]["sync_status"] == "stale_motion_sample"
    ]
    invalid = next(
        frame
        for frame in frames
        if frame["input"]["sync_status"] == "invalid_distance_sample"
    )
    invalid_index = frames.index(invalid)

    assert frames[30]["output"]["stationary_baseline_ready"]
    assert stale
    assert all(frame["output"]["classification"] == "uncertain" for frame in stale)
    assert all(not frame["output"]["stationary_baseline_ready"] for frame in stale)
    assert frames[invalid_index - 1]["output"]["stationary_baseline_ready"]
    assert (
        frames[invalid_index - 1]["output"]["candidate_classification"]
        == "target_approach"
    )
    assert invalid["output"]["classification"] == "uncertain"
    assert invalid["output"]["candidate_classification"] is None
    assert not invalid["output"]["stationary_baseline_ready"]
    assert invalid["output"]["distance_trend_ready"]
    assert (
        invalid["output"]["closing_rate_mps"]
        == frames[invalid_index - 1]["output"]["closing_rate_mps"]
    )
    assert any(
        frame["output"]["stationary_baseline_ready"]
        for frame in frames[invalid_index + 1 :]
    )


def test_fresh_scenario_state_and_replay_are_deterministic() -> None:
    first = bundle()["scenarios"]
    second = bundle()["scenarios"]

    assert first == second
    assert first[0]["frames"][0]["output"]["classification"] == "uncertain"
    assert first[1]["frames"][0]["output"]["classification"] == "uncertain"
    assert first[2]["frames"][0]["output"]["classification"] == "uncertain"
    assert first[3]["frames"][0]["output"]["classification"] == "uncertain"


def test_trace_json_is_strict_and_contains_no_non_finite_values() -> None:
    serialized = json.dumps(bundle(), allow_nan=False, ensure_ascii=False)
    decoded = json.loads(
        serialized,
        parse_constant=lambda value: pytest.fail(f"non-finite value: {value}"),
    )

    assert decoded["schema_version"] == 1
    assert decoded["version"]["git_dirty"] is True
    assert len(decoded["scenarios"]) == 4


def test_html_is_standalone_chinese_player_and_speed_is_presentation_only() -> None:
    html = render_demo_html(bundle())

    assert "合成資料模擬" in html
    assert "距離警示模擬（獨立於融合分類）" in html
    assert "播放" in html and "重播" in html and "情境" in html
    assert "https://" not in html and "http://" not in html
    assert "src=\"/" not in html and "href=\"/" not in html
    assert "sample_interval_seconds*1000/Number(speedSelect.value)" in html
    assert '"timestamp_seconds":0.05' in html


def test_scenario_generation_never_imports_hardware_libraries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__
    blocked = {"serial", "smbus2", "gpiozero", "lgpio"}

    def guarded_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name.split(".", 1)[0] in blocked:
            raise AssertionError(f"hardware library imported: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    generated = bundle()

    assert len(generated["scenarios"]) == 4


def test_cli_writes_both_outputs_and_refuses_unconfirmed_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html_path = tmp_path / "demo.html"
    trace_path = tmp_path / "trace.json"
    monkeypatch.setattr(
        "rear_warning.demo.cli.read_git_metadata",
        lambda: ("a" * 40, True),
    )

    assert main(["--output", str(html_path), "--trace-output", str(trace_path)]) == 0
    original_html = html_path.read_text(encoding="utf-8")
    assert trace_path.is_file()
    assert stat.S_IMODE(html_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(trace_path.stat().st_mode) == 0o644
    assert main(["--output", str(html_path), "--trace-output", str(trace_path)]) == 1
    assert html_path.read_text(encoding="utf-8") == original_html
    assert main(
        [
            "--output",
            str(html_path),
            "--trace-output",
            str(trace_path),
            "--force",
        ]
    ) == 0
