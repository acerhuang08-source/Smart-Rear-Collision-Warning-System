"""Hardware-free tests for the physical LED diagnostic CLI."""

from __future__ import annotations

import pytest

from rear_warning.cli import led_diagnostic
from rear_warning.outputs import LedPins


class _FakeOutput:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.writes: list[tuple[int, bool]] = []
        self.close_calls = 0
        self.close_error = close_error

    def write(self, pin: int, active: bool) -> None:
        self.writes.append((pin, active))

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _FakeFactory:
    def __init__(self, output: _FakeOutput) -> None:
        self.output = output
        self.calls: list[tuple[LedPins, int]] = []

    def __call__(self, pins: LedPins, *, chip: int) -> _FakeOutput:
        self.calls.append((pins, chip))
        return self.output


def test_confirmation_is_required_before_adapter_creation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = _FakeFactory(_FakeOutput())
    monkeypatch.setattr(led_diagnostic, "_OUTPUT_FACTORY", factory)

    result = led_diagnostic.main([])

    assert result == 2
    assert factory.calls == []
    assert "--confirm-hardware is required" in capsys.readouterr().err


def test_confirmed_diagnostic_runs_all_patterns_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _FakeOutput()
    factory = _FakeFactory(output)
    sleeps: list[float] = []
    monkeypatch.setattr(led_diagnostic, "_OUTPUT_FACTORY", factory)
    monkeypatch.setattr(led_diagnostic, "_SLEEP", sleeps.append)

    result = led_diagnostic.main(
        ["--confirm-hardware", "--step-seconds", "0.25"]
    )

    assert result == 0
    assert factory.calls == [(LedPins(red=17, yellow=27, green=22), 0)]
    assert sleeps == [0.25, 0.25, 0.25, 0.25]
    assert output.writes == [
        (17, True), (27, False), (22, False),
        (17, False), (27, True), (22, False),
        (17, False), (27, False), (22, True),
        (17, True), (27, True), (22, False),
        (17, False), (27, False), (22, False),
    ]
    assert output.close_calls == 1
    assert "SENSOR_FAULT" in capsys.readouterr().out


def test_exception_still_closes_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _FakeOutput()
    factory = _FakeFactory(output)
    monkeypatch.setattr(led_diagnostic, "_OUTPUT_FACTORY", factory)
    monkeypatch.setattr(
        led_diagnostic,
        "_SLEEP",
        lambda _seconds: (_ for _ in ()).throw(RuntimeError("sleep failed")),
    )

    result = led_diagnostic.main(["--confirm-hardware"])

    assert result == 1
    assert output.close_calls == 1


def test_close_failure_returns_error_without_success_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _FakeOutput(close_error=RuntimeError("close failed"))
    monkeypatch.setattr(led_diagnostic, "_OUTPUT_FACTORY", _FakeFactory(output))
    monkeypatch.setattr(led_diagnostic, "_SLEEP", lambda _seconds: None)

    result = led_diagnostic.main(["--confirm-hardware"])

    captured = capsys.readouterr()
    assert result == 1
    assert "LED cleanup error: close failed" in captured.err
    assert "completed" not in captured.out


def test_execution_and_close_errors_are_both_reported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _FakeOutput(close_error=RuntimeError("close failed"))
    monkeypatch.setattr(led_diagnostic, "_OUTPUT_FACTORY", _FakeFactory(output))
    monkeypatch.setattr(
        led_diagnostic,
        "_SLEEP",
        lambda _seconds: (_ for _ in ()).throw(RuntimeError("run failed")),
    )

    result = led_diagnostic.main(["--confirm-hardware"])

    captured = capsys.readouterr()
    assert result == 1
    assert "LED diagnostic failed: run failed" in captured.err
    assert "LED cleanup error: close failed" in captured.err
    assert "completed" not in captured.out


def test_interrupt_and_close_error_does_not_return_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _FakeOutput(close_error=RuntimeError("close failed"))
    monkeypatch.setattr(led_diagnostic, "_OUTPUT_FACTORY", _FakeFactory(output))
    monkeypatch.setattr(
        led_diagnostic,
        "_SLEEP",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    result = led_diagnostic.main(["--confirm-hardware"])

    captured = capsys.readouterr()
    assert result == 1
    assert "interrupted" in captured.out
    assert "LED cleanup error: close failed" in captured.err
    assert "completed" not in captured.out
