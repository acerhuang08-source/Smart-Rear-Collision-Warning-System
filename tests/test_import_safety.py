"""Software-only import and optional-hardware dependency tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from rear_warning.cli import bno055_diagnostic, led_diagnostic, rear_warning_diagnostic
from rear_warning.outputs.hardware import HardwareDependenciesNotInstalled


def test_software_modules_import_when_gpio_packages_are_blocked() -> None:
    source_root = Path(__file__).parents[1] / "src"
    script = """
import importlib.abc
import sys

class BlockHardware(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "gpiozero" or fullname.startswith("gpiozero."):
            raise ModuleNotFoundError(f"blocked {fullname}", name=fullname)
        if fullname == "lgpio" or fullname.startswith("lgpio."):
            raise ModuleNotFoundError(f"blocked {fullname}", name=fullname)
        if fullname == "smbus2" or fullname.startswith("smbus2."):
            raise ModuleNotFoundError(f"blocked {fullname}", name=fullname)
        return None

sys.meta_path.insert(0, BlockHardware())
import rear_warning
import rear_warning.warning
import rear_warning.outputs
import rear_warning.outputs.led
import rear_warning.cli.tfmini_plus_diagnostic
import rear_warning.cli.led_diagnostic
import rear_warning.cli.rear_warning_diagnostic
import rear_warning.sensors.bno055
import rear_warning.sensors.bno055.models
import rear_warning.sensors.bno055.registers
import rear_warning.sensors.bno055.device
import rear_warning.cli.bno055_diagnostic
assert "gpiozero" not in sys.modules
assert "lgpio" not in sys.modules
assert "smbus2" not in sys.modules
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=source_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_unconfirmed_led_cli_does_not_load_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(led_diagnostic, "_OUTPUT_FACTORY", None)
    monkeypatch.setattr(
        led_diagnostic,
        "load_gpiozero_output_factory",
        lambda: (_ for _ in ()).throw(AssertionError("hardware imported")),
    )

    assert led_diagnostic.main([]) == 2


def test_unconfirmed_integrated_cli_does_not_load_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rear_warning_diagnostic, "_OUTPUT_FACTORY", None)
    monkeypatch.setattr(
        rear_warning_diagnostic,
        "load_gpiozero_output_factory",
        lambda: (_ for _ in ()).throw(AssertionError("hardware imported")),
    )

    assert rear_warning_diagnostic.main(["--duration", "1"]) == 2


def test_unconfirmed_bno055_cli_does_not_load_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bno055_diagnostic, "_ADAPTER_FACTORY", None)
    monkeypatch.setattr(
        bno055_diagnostic,
        "load_smbus_register_io",
        lambda: (_ for _ in ()).throw(AssertionError("hardware imported")),
    )
    assert bno055_diagnostic.main(["--max-samples", "1"]) == 2


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (led_diagnostic, ["--confirm-hardware"]),
        (
            rear_warning_diagnostic,
            ["--duration", "1", "--confirm-hardware"],
        ),
    ],
)
def test_confirmed_cli_reports_missing_hardware_extra(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module: object,
    argv: list[str],
) -> None:
    monkeypatch.setattr(module, "_OUTPUT_FACTORY", None)
    monkeypatch.setattr(
        module,
        "load_gpiozero_output_factory",
        lambda: (_ for _ in ()).throw(
            HardwareDependenciesNotInstalled(
                "Raspberry Pi GPIO dependencies are not installed.\n"
                'Install with: pip install -e ".[hardware]"'
            )
        ),
    )

    result = module.main(argv)  # type: ignore[attr-defined]

    assert result == 1
    captured = capsys.readouterr()
    assert "Raspberry Pi GPIO dependencies are not installed." in captured.err
    assert 'pip install -e ".[hardware]"' in captured.err
    assert "Traceback" not in captured.err
