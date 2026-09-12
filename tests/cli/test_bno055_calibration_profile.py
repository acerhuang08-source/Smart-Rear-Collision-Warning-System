from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from rear_warning.cli import bno055_calibration_profile as cli
from rear_warning.sensors.bno055 import (
    BNO055CalibrationData,
    BNO055CalibrationProfile,
    BNO055CalibrationProfileError,
    BNO055CalibrationTimeoutError,
    BNO055ProfileReadbackError,
    BNO055ProfileStorageError,
)


def profile() -> BNO055CalibrationProfile:
    return BNO055CalibrationProfile(
        sensor_label="rear-imu-primary",
        chip_id=0xA0,
        accelerometer_id=0xFB,
        magnetometer_id=0x32,
        gyroscope_id=0x0F,
        software_revision=0x0308,
        bootloader_revision=0x15,
        unit_sel=0,
        power_mode=0,
        operation_mode=0x0C,
        axis_map_config=0x24,
        axis_map_sign=0,
        calibration=BNO055CalibrationData(1, -2, 3, -4, 5, -6, 7, -8, 9, 1000, 480),
        created_at_utc="2026-09-08T12:34:56.000000Z",
    )


class FakeStore:
    def __init__(self, *, load_error: BaseException | None = None, save_error: BaseException | None = None) -> None:
        self.load_error = load_error
        self.save_error = save_error
        self.loads: list[tuple[Path, str]] = []
        self.saves: list[tuple[Path, BNO055CalibrationProfile]] = []

    def load(self, path: Path, *, expected_sensor_label: str) -> BNO055CalibrationProfile:
        self.loads.append((path, expected_sensor_label))
        if self.load_error:
            raise self.load_error
        return profile()

    def save(self, path: Path, value: BNO055CalibrationProfile) -> None:
        self.saves.append((path, value))
        if self.save_error:
            raise self.save_error


class FakeRegisterIO:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.close_error = close_error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise self.close_error


class FakeAdapterFactory:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.register_io = FakeRegisterIO()

    def __call__(self, bus: int) -> FakeRegisterIO:
        self.calls.append(bus)
        return self.register_io


class FakeDevice:
    def __init__(
        self,
        *,
        capture_error: BaseException | None = None,
        restore_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.capture_error = capture_error
        self.restore_error = restore_error
        self.close_error = close_error
        self.capture_calls: list[dict[str, object]] = []
        self.restore_calls: list[tuple[BNO055CalibrationProfile, dict[str, object]]] = []
        self.close_calls = 0

    def capture_calibration_profile(self, **kwargs: object) -> BNO055CalibrationProfile:
        self.capture_calls.append(kwargs)
        if self.capture_error:
            raise self.capture_error
        return profile()

    def restore_calibration_profile(
        self, value: BNO055CalibrationProfile, **kwargs: object
    ) -> object:
        self.restore_calls.append((value, kwargs))
        if self.restore_error:
            raise self.restore_error
        return object()

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise self.close_error


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: FakeStore | None = None,
    device: FakeDevice | None = None,
    adapter: FakeAdapterFactory | None = None,
) -> tuple[FakeStore, FakeDevice, FakeAdapterFactory]:
    selected_store = store or FakeStore()
    selected_device = device or FakeDevice()
    selected_adapter = adapter or FakeAdapterFactory()
    monkeypatch.setattr(cli, "_STORE_FACTORY", lambda: selected_store)
    monkeypatch.setattr(cli, "_ADAPTER_FACTORY", selected_adapter)
    monkeypatch.setattr(cli, "_DEVICE_FACTORY", lambda _io, **_kwargs: selected_device)
    return selected_store, selected_device, selected_adapter


def export_args(*extra: str) -> list[str]:
    return [
        "export",
        "--sensor-label",
        "rear-imu-primary",
        "--profile-output",
        "/unused/profile.json",
        *extra,
    ]


def restore_args(*extra: str) -> list[str]:
    return [
        "restore",
        "--sensor-label",
        "rear-imu-primary",
        "--profile-input",
        "/unused/profile.json",
        *extra,
    ]


@pytest.mark.parametrize("args", [export_args(), restore_args()])
def test_unconfirmed_dry_run_is_exit_2_with_zero_file_and_hardware_io(
    args: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_STORE_FACTORY", lambda: (_ for _ in ()).throw(AssertionError("file access")))
    monkeypatch.setattr(cli, "_ADAPTER_FACTORY", lambda _bus: (_ for _ in ()).throw(AssertionError("hardware access")))
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert "profile_registers=page_0:0x55-0x6a" in captured.out
    assert "--confirm-hardware is required" in captured.err


def test_export_success_cleanup_and_publish_are_separate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, device, adapter = install(monkeypatch)
    assert cli.main(export_args("--confirm-hardware")) == 0
    output = capsys.readouterr().out
    assert adapter.calls == [1]
    assert device.close_calls == 1
    assert len(store.saves) == 1
    assert "device_cleanup_completed=true" in output
    assert "export_success" in output


def test_restore_validates_before_adapter_and_reports_all_success_markers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, device, adapter = install(monkeypatch)
    assert cli.main(restore_args("--confirm-hardware")) == 0
    output = capsys.readouterr().out
    assert store.loads == [(Path("/unused/profile.json"), "rear-imu-primary")]
    assert adapter.calls == [1]
    assert device.close_calls == 1
    for marker in (
        "profile_validation_success",
        "read_back_verified",
        "measurement_verified",
        "device_cleanup_completed=true",
        "restore_success",
    ):
        assert marker in output


def test_invalid_profile_fails_before_adapter_creation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore(load_error=BNO055CalibrationProfileError("hash mismatch"))
    _, device, adapter = install(monkeypatch, store=store)
    assert cli.main(restore_args("--confirm-hardware")) == 1
    assert adapter.calls == []
    assert device.close_calls == 0
    captured = capsys.readouterr()
    assert "hash mismatch" in captured.err
    assert "device_cleanup_completed=false" in captured.out


@pytest.mark.parametrize(
    "error",
    [
        BNO055CalibrationTimeoutError("calibration timeout"),
        BNO055ProfileReadbackError(register=0x57, expected=1, actual=2),
        RuntimeError("measurement quality failure"),
    ],
)
def test_device_failures_are_nonzero_and_cleanup_runs(
    error: BaseException,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    is_export = isinstance(error, BNO055CalibrationTimeoutError)
    device = FakeDevice(capture_error=error if is_export else None, restore_error=None if is_export else error)
    store, _, _ = install(monkeypatch, device=device)
    args = export_args("--confirm-hardware") if is_export else restore_args("--confirm-hardware")
    assert cli.main(args) == 1
    captured = capsys.readouterr()
    assert device.close_calls == 1
    assert "device_cleanup_completed=true" in captured.out
    assert "export_success" not in captured.out
    assert "restore_success" not in captured.out
    assert store.saves == []
    assert "Traceback" not in captured.err


def test_export_cleanup_failure_prevents_profile_publish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, device, _ = install(
        monkeypatch, device=FakeDevice(close_error=RuntimeError("cleanup failed"))
    )
    assert cli.main(export_args("--confirm-hardware")) == 1
    captured = capsys.readouterr()
    assert store.saves == []
    assert device.close_calls == 1
    assert "cleanup failed" in captured.err
    assert "device_cleanup_completed=false" in captured.out
    assert "export_success" not in captured.out


def test_primary_and_cleanup_errors_are_both_preserved(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    device = FakeDevice(
        restore_error=BNO055ProfileReadbackError(register=0x55, expected=1, actual=0),
        close_error=RuntimeError("CONFIGMODE cleanup failed"),
    )
    install(monkeypatch, device=device)
    assert cli.main(restore_args("--confirm-hardware")) == 1
    captured = capsys.readouterr()
    assert "read-back mismatch" in captured.err
    assert "CONFIGMODE cleanup failed" in captured.err


def test_ctrl_c_returns_130_after_cleanup_even_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    device = FakeDevice(
        restore_error=KeyboardInterrupt(),
        close_error=RuntimeError("cleanup also failed"),
    )
    install(monkeypatch, device=device)
    assert cli.main(restore_args("--confirm-hardware")) == 130
    captured = capsys.readouterr()
    assert "Ctrl+C" in captured.err
    assert "cleanup also failed" in captured.err
    assert device.close_calls == 1


def test_ctrl_c_during_publication_returns_130_without_false_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore(save_error=KeyboardInterrupt())
    _, device, _ = install(monkeypatch, store=store)
    assert cli.main(export_args("--confirm-hardware")) == 130
    captured = capsys.readouterr()
    assert device.close_calls == 1
    assert "publication interrupted" in captured.err
    assert "KeyboardInterrupt" in captured.err
    assert "profile_cleanup_completed=true" in captured.out
    assert "export_success" not in captured.out


@pytest.mark.parametrize("cleanup_count", [1, 3])
def test_publication_ctrl_c_keeps_exit_130_and_prints_all_cleanup_notes(
    cleanup_count: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    interrupted = KeyboardInterrupt()
    for index in range(cleanup_count):
        interrupted.add_note(f"profile cleanup failed: OSError: cleanup {index}")
    store = FakeStore(save_error=interrupted)
    install(monkeypatch, store=store)
    assert cli.main(export_args("--confirm-hardware")) == 130
    captured = capsys.readouterr()
    assert "KeyboardInterrupt" in captured.err
    for index in range(cleanup_count):
        detail = f"profile cleanup failed: OSError: cleanup {index}"
        assert captured.err.count(detail) == 1
    assert "profile_cleanup_completed=false" in captured.out
    assert "export_success" not in captured.out
    assert "Traceback" not in captured.err


def test_storage_error_prints_primary_and_each_note_once_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = OSError("primary publish failure")
    failure.add_note("profile cleanup failed: OSError: temp unlink failure")
    failure.add_note("profile cleanup failed: OSError: final unlink failure")
    store = FakeStore(save_error=failure)
    install(monkeypatch, store=store)
    assert cli.main(export_args("--confirm-hardware")) == 1
    captured = capsys.readouterr()
    assert captured.err.count("OSError: primary publish failure") == 1
    assert captured.err.count("temp unlink failure") == 1
    assert captured.err.count("final unlink failure") == 1
    assert "profile_cleanup_completed=false" in captured.out
    assert "Traceback" not in captured.err
    assert "export_success" not in captured.out


def test_keyboard_interrupt_during_cleanup_keeps_primary_and_returns_130(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    primary = OSError("publish failed")
    cleanup_interrupt = KeyboardInterrupt()
    failure = BNO055ProfileStorageError(
        "profile save failed: primary=OSError: publish failed; "
        "cleanup=KeyboardInterrupt",
        primary=primary,
        cleanup_errors=(cleanup_interrupt,),
    )
    install(monkeypatch, store=FakeStore(save_error=failure))
    assert cli.main(export_args("--confirm-hardware")) == 130
    captured = capsys.readouterr()
    assert captured.err.count("primary=OSError: publish failed") == 1
    assert captured.err.count("cleanup=KeyboardInterrupt") == 1
    assert "profile_cleanup_completed=false" in captured.out
    assert "export_success" not in captured.out
    assert "Traceback" not in captured.err


def test_adapter_construction_failure_closes_raw_adapter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore()
    adapter = FakeAdapterFactory()
    monkeypatch.setattr(cli, "_STORE_FACTORY", lambda: store)
    monkeypatch.setattr(cli, "_ADAPTER_FACTORY", adapter)
    monkeypatch.setattr(cli, "_DEVICE_FACTORY", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("construction failed")))
    assert cli.main(export_args("--confirm-hardware")) == 1
    assert adapter.register_io.close_calls == 1
    assert "construction failed" in capsys.readouterr().err


def test_console_module_unconfirmed_does_not_import_smbus_or_open_profile(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[2] / "src"
    target = tmp_path / "must-not-exist.json"
    blocker = tmp_path / "sitecustomize.py"
    blocker.write_text(
        """
import importlib.abc
import sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'smbus2' or fullname.startswith('smbus2.'):
            raise ModuleNotFoundError('blocked smbus2', name=fullname)
        return None
sys.meta_path.insert(0, Block())
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(blocker.parent), str(source_root)))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rear_warning.cli.bno055_calibration_profile",
            "export",
            "--sensor-label",
            "rear-imu-primary",
            "--profile-output",
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 2
    assert "--confirm-hardware is required" in result.stderr
    assert not target.exists()
    assert "Traceback" not in result.stderr


def test_installed_console_unconfirmed_is_hardware_and_file_free(tmp_path: Path) -> None:
    executable = Path(sys.prefix) / "bin" / "bno055-calibration-profile"
    assert executable.is_file()
    target = tmp_path / "must-not-exist.json"
    report = tmp_path / "loaded.txt"
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        """
import atexit
import importlib.abc
import os
import sys
PACKAGES = ('smbus2', 'gpiozero', 'lgpio')
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == item or fullname.startswith(item + '.') for item in PACKAGES):
            raise ModuleNotFoundError(f'blocked {fullname}', name=fullname)
        return None
sys.meta_path.insert(0, Block())
def record_loaded():
    loaded = sorted(name for name in sys.modules if any(name == item or name.startswith(item + '.') for item in PACKAGES))
    with open(os.environ['BNO_PROFILE_IMPORT_REPORT'], 'w', encoding='utf-8') as stream:
        stream.write('\\n'.join(loaded))
atexit.register(record_loaded)
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(tmp_path)
    environment["BNO_PROFILE_IMPORT_REPORT"] = str(report)
    result = subprocess.run(
        [
            executable,
            "export",
            "--sensor-label",
            "rear-imu-primary",
            "--profile-output",
            str(target),
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


def test_help_and_console_entry_point_are_declared() -> None:
    parser = cli._build_parser()
    help_text = parser.format_help()
    assert "export" in help_text and "restore" in help_text
    pyproject = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert "bno055-calibration-profile" in pyproject
