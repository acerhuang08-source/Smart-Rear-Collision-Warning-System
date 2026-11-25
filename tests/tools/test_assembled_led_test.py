"""Hardware-free tests for the assembled six-lamp diagnostic."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "tools" / "assembled_led_test.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assembled_led_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool() -> Iterator[ModuleType]:
    module = _load_tool()
    yield module


def _write_wiring(path: Path, *, layout: str = "shared_3", conflict: bool = False) -> None:
    rows = (
        ("A紅", "red", 17, 11), ("A黃", "yellow", 27, 13),
        ("A綠", "green", 22, 15), ("B紅", "red" if layout == "shared_3" else "b-red", 17 if layout == "shared_3" else 5, 11 if layout == "shared_3" else 29),
        ("B黃", "yellow" if layout == "shared_3" else "b-yellow", 27 if layout == "shared_3" else 6, 13 if layout == "shared_3" else 31),
        ("B綠", "green" if layout == "shared_3" else "b-green", 22 if layout == "shared_3" else 12, 15 if layout == "shared_3" else 32),
    )
    lines = [f'layout = "{layout}"', "gpio_chip = 4", ""]
    for index, (name, controller, bcm, physical) in enumerate(rows):
        if conflict and index == 0:
            bcm, physical = 15, 10
        lines.extend(
            (
                "[[led]]", f'name = "{name}"', f'installed_position = "位置{name}"',
                'lamp_type = "離散 LED"', 'supply = "3.3 V"',
                'drive = "GPIO 經限流電阻"', 'current_limit = "220 ohm"',
                f"bcm = {bcm}", f"physical_pin = {physical}",
                "active_high = true", f'controller = "{controller}"', "",
            )
        )
    path.write_text("\n".join(lines), encoding="utf-8")


class _FakeBank:
    def __init__(self) -> None:
        self.actions: list[tuple[str, object]] = []

    def set_active(self, controller: str, active: bool) -> None:
        self.actions.append((controller, active))

    def all_off(self) -> None:
        self.actions.append(("all_off", True))

    def close(self) -> None:
        self.actions.append(("close", True))


def test_shared_layout_has_one_controller_per_colour(tmp_path: Path, tool: ModuleType) -> None:
    path = tmp_path / "wiring.toml"
    _write_wiring(path)
    wiring = tool._load_wiring(path)

    assert wiring.layout == "shared_3"
    assert wiring.controllers == ("red", "yellow", "green")
    assert len(wiring.controllers) == 3


def test_independent_layout_has_six_controllers(tmp_path: Path, tool: ModuleType) -> None:
    path = tmp_path / "wiring.toml"
    _write_wiring(path, layout="independent_6")
    wiring = tool._load_wiring(path)

    assert len(wiring.controllers) == 6
    assert wiring.controllers == ("red", "b-red", "yellow", "b-yellow", "green", "b-green")


def test_sensor_pin_conflict_is_rejected(tmp_path: Path, tool: ModuleType) -> None:
    path = tmp_path / "wiring.toml"
    _write_wiring(path, conflict=True)

    with pytest.raises(ValueError, match="UART RX"):
        tool._load_wiring(path)


def test_check_config_has_zero_gpio_access(tmp_path: Path, tool: ModuleType) -> None:
    path = tmp_path / "wiring.toml"
    _write_wiring(path)
    tool._BANK_FACTORY = lambda wiring: pytest.fail("GPIO must not open")

    assert tool.main(["--wiring", str(path), "--check-config"]) == 0


def test_success_records_visual_checks_and_cleanup(tmp_path: Path, tool: ModuleType) -> None:
    path = tmp_path / "wiring.toml"
    results = tmp_path / "results"
    _write_wiring(path)
    bank = _FakeBank()
    answers = iter(("", "y", "y", "n", "y", "y", "n", "y", "y", "n"))
    tool._INPUT = lambda prompt: next(answers)
    tool._SLEEP = lambda seconds: None
    tool._BANK_FACTORY = lambda wiring: bank
    tool._WALL_CLOCK = lambda: datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)

    assert tool.main(["--wiring", str(path), "--output-root", str(results)]) == 0

    assert bank.actions[-2:] == [("all_off", True), ("close", True)]
    payload = __import__("json").loads(next(results.rglob("result.json")).read_text())
    assert payload["status"] == "PASS"
    assert len(payload["observations"]) == 3
    assert payload["all_off_succeeded"] is True
    assert payload["gpio_close_succeeded"] is True


def test_ctrl_c_during_sequence_turns_everything_off(tmp_path: Path, tool: ModuleType) -> None:
    path = tmp_path / "wiring.toml"
    results = tmp_path / "results"
    _write_wiring(path)
    bank = _FakeBank()
    tool._INPUT = lambda prompt: ""
    tool._SLEEP = lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt())
    tool._BANK_FACTORY = lambda wiring: bank

    assert tool.main(["--wiring", str(path), "--output-root", str(results)]) == 130
    assert bank.actions[-2:] == [("all_off", True), ("close", True)]
    payload = __import__("json").loads(next(results.rglob("result.json")).read_text())
    assert payload["status"] == "未驗證"
    assert payload["interrupted"] is True
