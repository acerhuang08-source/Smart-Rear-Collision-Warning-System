from __future__ import annotations

import pytest

from rear_warning.cli import bno055_diagnostic
from rear_warning.sensors.bno055 import BNO055Device
from rear_warning.sensors.bno055.registers import (
    ACC_ID, BL_REV_ID, CHIP_ID, EXPECTED_ACC_ID, EXPECTED_CHIP_ID,
    EXPECTED_GYR_ID, EXPECTED_MAG_ID, GYR_ID, MAG_ID, OPR_MODE, SW_REV_ID_LSB,
    SW_REV_ID_MSB, SYS_ERR, SYS_STATUS,
)


class ProgrammableBus:
    def __init__(
        self,
        *,
        fail_reads: set[int] | None = None,
        fail_writes: set[int] | None = None,
        close_error: bool = False,
    ) -> None:
        self.fail_reads = set(fail_reads or ())
        self.fail_writes = set(fail_writes or ())
        self.close_error = close_error
        self.read_count = 0
        self.write_count = 0
        self.events: list[tuple[object, ...]] = []
        self.values = {
            CHIP_ID: EXPECTED_CHIP_ID,
            ACC_ID: EXPECTED_ACC_ID,
            MAG_ID: EXPECTED_MAG_ID,
            GYR_ID: EXPECTED_GYR_ID,
            SW_REV_ID_LSB: 0x08,
            SW_REV_ID_MSB: 0x03,
            BL_REV_ID: 0x15,
            OPR_MODE: 0x0C,
            SYS_STATUS: 0x05,
            SYS_ERR: 0x00,
        }

    def read_byte_data(self, address: int, register: int) -> int:
        self.read_count += 1
        self.events.append(("read", self.read_count, address, register))
        if self.read_count in self.fail_reads:
            raise OSError(f"read failure {self.read_count}")
        return self.values[register]

    def read_i2c_block_data(
        self, address: int, register: int, length: int
    ) -> list[int]:
        raise AssertionError("measurement read was not expected")

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        self.write_count += 1
        self.events.append(
            ("write", self.write_count, address, register, value)
        )
        if self.write_count in self.fail_writes:
            raise OSError(f"write failure {self.write_count}")

    def close(self) -> None:
        self.events.append(("close",))
        if self.close_error:
            raise OSError("bus close failure")


def _run_with_real_device(
    monkeypatch: pytest.MonkeyPatch,
    bus: ProgrammableBus,
) -> int:
    monkeypatch.setattr(bno055_diagnostic, "_ADAPTER_FACTORY", lambda _bus: bus)
    monkeypatch.setattr(
        bno055_diagnostic,
        "_DEVICE_FACTORY",
        lambda register_io, address: BNO055Device(
            register_io,
            address=address,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        ),
    )
    monkeypatch.setattr(bno055_diagnostic, "_MONOTONIC", lambda: 0.0)
    return bno055_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    )


@pytest.mark.parametrize(
    ("failure_call", "primary_register"),
    [(1, "0x3d"), (2, "0x07"), (3, "0x3e"),
     (4, "0x3b"), (5, "0x3d")],
)
def test_each_initialization_write_failure_attempts_config_cleanup_and_close(
    failure_call: int,
    primary_register: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bus = ProgrammableBus(fail_writes={failure_call})
    assert _run_with_real_device(monkeypatch, bus) == 1
    captured = capsys.readouterr()
    assert f"failed to write register {primary_register}" in captured.err
    assert any(event[0] == "close" for event in bus.events)
    assert bus.write_count == failure_call + 1
    assert "cleanup_completed=false" not in captured.out


@pytest.mark.parametrize(
    ("failure_read", "primary_register"),
    [(8, "0x3d"), (9, "0x39"), (10, "0x3a")],
)
def test_each_readiness_read_failure_attempts_cleanup_and_close(
    failure_read: int,
    primary_register: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bus = ProgrammableBus(fail_reads={failure_read})
    assert _run_with_real_device(monkeypatch, bus) == 1
    captured = capsys.readouterr()
    assert f"failed to read register {primary_register}" in captured.err
    assert bus.write_count == 6
    assert bus.events[-1] == ("close",)


@pytest.mark.parametrize(
    ("cleanup_write_fails", "close_fails"),
    [(True, False), (False, True), (True, True)],
)
def test_primary_and_cleanup_failures_are_all_reported(
    cleanup_write_fails: bool,
    close_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed_writes = {2}
    if cleanup_write_fails:
        failed_writes.add(3)
    bus = ProgrammableBus(
        fail_writes=failed_writes,
        close_error=close_fails,
    )
    assert _run_with_real_device(monkeypatch, bus) == 1
    captured = capsys.readouterr()
    assert "failed to write register 0x07" in captured.err
    if cleanup_write_fails:
        assert "failed to write register 0x3d" in captured.err
    if close_fails:
        assert "bus close failure" in captured.err
    assert bus.events[-1] == ("close",)
    assert "cleanup_completed=false" in captured.out
