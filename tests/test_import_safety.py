"""Software-only import and optional-hardware dependency tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rear_warning.cli import (
    bno055_diagnostic, led_diagnostic, rear_warning_diagnostic,
    sensor_sync_diagnostic,
)
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
import rear_warning.synchronization
import rear_warning.cli.sensor_sync_diagnostic
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


def test_unconfirmed_sensor_sync_cli_does_not_load_hardware_or_create_csv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "not-created.csv"
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_load_hardware_factories",
        lambda: (_ for _ in ()).throw(AssertionError("hardware imported")),
    )
    assert sensor_sync_diagnostic.main(
        ["--duration", "1", "--csv-output", str(target)]
    ) == 2
    assert not target.exists()


def test_synchronization_core_and_cli_import_without_serial_or_smbus2() -> None:
    source_root = Path(__file__).parents[1] / "src"
    script = """
import importlib.abc
import sys

class BlockSensorHardware(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        packages = ("serial", "smbus2", "gpiozero", "lgpio")
        if any(fullname == package or fullname.startswith(package + ".") for package in packages):
            raise ModuleNotFoundError(f"blocked {fullname}", name=fullname)
        return None

sys.meta_path.insert(0, BlockSensorHardware())
import rear_warning.warning
import rear_warning.synchronization
import rear_warning.cli.sensor_sync_diagnostic
assert "serial" not in sys.modules
assert "smbus2" not in sys.modules
assert "gpiozero" not in sys.modules
assert "lgpio" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=source_root,
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_real_sensor_sync_console_unconfirmed_is_hardware_free(
    tmp_path: Path,
) -> None:
    executable = shutil.which("sensor-sync-diagnostic")
    assert executable is not None
    target = tmp_path / "must-not-exist.csv"
    report = tmp_path / "loaded.txt"
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        """
import atexit
import importlib.abc
import os
import sys

PACKAGES = ("serial", "smbus2", "gpiozero", "lgpio")

class BlockHardware(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == item or fullname.startswith(item + ".") for item in PACKAGES):
            raise ModuleNotFoundError(f"blocked {fullname}", name=fullname)
        return None

sys.meta_path.insert(0, BlockHardware())

def record_loaded():
    loaded = sorted(
        name for name in sys.modules
        if any(name == item or name.startswith(item + ".") for item in PACKAGES)
    )
    with open(os.environ["SENSOR_SYNC_IMPORT_REPORT"], "w", encoding="utf-8") as stream:
        stream.write("\\n".join(loaded))

atexit.register(record_loaded)
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(tmp_path)
    environment["SENSOR_SYNC_IMPORT_REPORT"] = str(report)
    result = subprocess.run(
        [
            executable, "--duration", "1", "--csv-output", str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 2
    assert "--confirm-hardware is required" in result.stderr
    assert not target.exists()
    assert report.read_text(encoding="utf-8") == ""


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
