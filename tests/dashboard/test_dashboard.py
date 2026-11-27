from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

from rear_warning.cli.ioradar701_shadow import _Capture
from rear_warning.dashboard.server import DashboardServer
from rear_warning.dashboard.source import CaptureDashboardSource


def shadow(timestamp: float, *, target: bool = True) -> dict[str, object]:
    selected = None if not target else {
        "target_id": 2, "distance_m": 1.5, "x_m": 1.4, "y_m": -0.2,
        "vx_mps": 0.3, "energy": 80,
    }
    return {
        "timestamp_seconds": timestamp,
        "availability": "live_selected" if target else "no_targets",
        "reason": "ok" if target else "status declared zero targets",
        "selection": {
            "candidate_ids": [2] if target else [],
            "candidates": [selected] if target else [],
            "selected_target": selected,
            "reason": "held current target ID 2" if target else "no targets",
        },
        "fusion": None,
        "distance_warning_candidate": "warning" if target else None,
        "distance_warning_stabilized": None,
    }


def write_line(path: Path, value: dict[str, object], *, append: bool = False) -> None:
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as output:
        output.write(json.dumps(value) + "\n")


def test_existing_complete_capture_is_stopped_not_live(tmp_path: Path) -> None:
    run = tmp_path / "live-20260101T000000"
    run.mkdir()
    write_line(run / "shadow.jsonl", shadow(10.0))
    (run / "bno055.jsonl").write_text("", encoding="utf-8")
    (run / "metadata.json").write_text(
        json.dumps({"mode": "LIVE", "recording_complete": True}), encoding="utf-8"
    )
    source = CaptureDashboardSource(tmp_path, monotonic=lambda: 100.0)
    source.poll_once()
    result = source.snapshot()
    assert result["source_state"] == "stopped"
    assert result["radar"]["age_seconds"] is None


def test_growing_capture_becomes_stale_without_refreshing_old_timestamp(tmp_path: Path) -> None:
    now = [100.0]
    run = tmp_path / "live-20260101T000000"
    run.mkdir()
    write_line(run / "shadow.jsonl", shadow(99.0))
    (run / "bno055.jsonl").write_text("", encoding="utf-8")
    source = CaptureDashboardSource(tmp_path, monotonic=lambda: now[0])
    source.poll_once()
    write_line(run / "shadow.jsonl", shadow(100.0), append=True)
    source.poll_once()
    assert source.snapshot()["source_state"] == "live"
    now[0] = 102.0
    source.poll_once()
    result = source.snapshot()
    assert result["source_state"] == "stale"
    assert result["radar"]["age_seconds"] == 2.0


def test_zero_targets_are_null_and_history_is_bounded(tmp_path: Path) -> None:
    run = tmp_path / "live-20260101T000000"
    run.mkdir()
    write_line(run / "shadow.jsonl", shadow(1.0, target=False))
    (run / "bno055.jsonl").write_text("", encoding="utf-8")
    source = CaptureDashboardSource(tmp_path, history_limit=2, monotonic=lambda: 1.0)
    source.poll_once()
    result = source.snapshot()
    assert result["radar"]["target_count"] == 0
    assert result["radar"]["selected_target"] is None
    assert result["distance_history"][-1]["distance_m"] is None
    for timestamp in (2.0, 3.0, 4.0):
        write_line(run / "shadow.jsonl", shadow(timestamp), append=True)
        source.poll_once()
    assert len(source.snapshot()["distance_history"]) == 2


def test_run_switch_clears_history(tmp_path: Path) -> None:
    first = tmp_path / "live-20260101T000000"
    first.mkdir()
    write_line(first / "shadow.jsonl", shadow(1.0))
    (first / "bno055.jsonl").write_text("", encoding="utf-8")
    source = CaptureDashboardSource(tmp_path, monotonic=lambda: 2.0)
    source.poll_once()
    second = tmp_path / "live-20260101T000001"
    second.mkdir()
    write_line(second / "shadow.jsonl", shadow(2.0, target=False))
    (second / "bno055.jsonl").write_text("", encoding="utf-8")
    source.poll_once()
    result = source.snapshot()
    assert result["run_id"] == second.name
    assert len(result["distance_history"]) == 1
    assert result["distance_history"][0]["distance_m"] is None


def test_multiple_http_clients_share_snapshot_without_hardware(tmp_path: Path) -> None:
    source = CaptureDashboardSource(tmp_path)
    source.poll_once()
    server = DashboardServer(("127.0.0.1", 0), source)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for _ in range(2):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/api/snapshot")
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read())["source_state"] == "not_started"
            connection.close()
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_assets_are_local_and_capture_streams_are_line_buffered(tmp_path: Path) -> None:
    html = Path("src/rear_warning/dashboard/static/index.html").read_text(encoding="utf-8")
    assert "實驗資料顯示／未啟用實體警示" in html
    assert "cdn" not in html.casefold()
    directory = tmp_path / "capture"
    directory.mkdir()
    capture = _Capture(directory, raw=False)
    try:
        assert capture.motion.line_buffering
        assert capture.shadow.line_buffering
    finally:
        assert capture.close() == []
