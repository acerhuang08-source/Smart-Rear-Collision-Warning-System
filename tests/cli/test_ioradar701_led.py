"""Hardware-boundary tests for the opt-in radar LED command."""

from __future__ import annotations

from pathlib import Path

import pytest

from rear_warning.cli import ioradar701_led


class _Stream:
    def __init__(self, *, tty: bool, line: str = "") -> None:
        self._tty = tty
        self._line = line
        self.text = ""

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        return self._line

    def write(self, value: str) -> int:
        self.text += value
        return len(value)

    def flush(self) -> None:
        pass


class _FakeGpio:
    def __init__(self) -> None:
        self.closed = False
        self.states: dict[int, bool] = {}

    def write(self, pin: int, active: bool) -> None:
        self.states[pin] = active

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--confirm-hardware"],
        ["--enable-led"],
    ],
)
def test_both_opt_in_flags_are_required_before_any_gpio_or_sensor_io(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ioradar701_led.sys, "stderr", _Stream(tty=True))
    monkeypatch.setattr(
        ioradar701_led,
        "_OUTPUT_FACTORY",
        lambda *_args, **_kwargs: calls.append("gpio"),
    )
    monkeypatch.setattr(
        ioradar701_led.ioradar701_shadow,
        "_run_live",
        lambda *_args, **_kwargs: calls.append("sensors"),
    )

    assert ioradar701_led.main(argv) == 2
    assert calls == []


def test_non_tty_and_nonempty_confirmation_are_zero_hardware_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ioradar701_led.sys, "stdin", _Stream(tty=False))
    monkeypatch.setattr(ioradar701_led.sys, "stdout", _Stream(tty=False))
    monkeypatch.setattr(ioradar701_led.sys, "stderr", _Stream(tty=False))
    monkeypatch.setattr(
        ioradar701_led,
        "_OUTPUT_FACTORY",
        lambda *_args, **_kwargs: calls.append("gpio"),
    )
    assert ioradar701_led.main(["--confirm-hardware", "--enable-led"]) == 2
    assert calls == []

    monkeypatch.setattr(ioradar701_led.sys, "stdin", _Stream(tty=True, line="yes\n"))
    monkeypatch.setattr(ioradar701_led.sys, "stdout", _Stream(tty=True))
    assert ioradar701_led.main(["--confirm-hardware", "--enable-led"]) == 2
    assert calls == []


def test_default_output_root_is_new_hardware_result_tree() -> None:
    options = ioradar701_led._build_parser().parse_args([])
    assert options.output_root == Path("hardware-results/radar-led")
    assert options.enable_led is False
    assert options.start_delay == 0.0


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_invalid_start_delay_is_rejected_before_hardware_io(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ioradar701_led.sys, "stderr", _Stream(tty=True))
    monkeypatch.setattr(
        ioradar701_led,
        "_OUTPUT_FACTORY",
        lambda *_args, **_kwargs: calls.append("gpio"),
    )

    with pytest.raises(SystemExit) as raised:
        ioradar701_led.main(
            [
                "--confirm-hardware",
                "--enable-led",
                "--start-delay",
                value,
            ]
        )

    assert raised.value.code == 2
    assert calls == []


def test_start_delay_finishes_before_any_hardware_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    gpio = _FakeGpio()

    monkeypatch.setattr(ioradar701_led.sys, "stdin", _Stream(tty=True, line="\n"))
    monkeypatch.setattr(ioradar701_led.sys, "stdout", _Stream(tty=True))
    monkeypatch.setattr(ioradar701_led.sys, "stderr", _Stream(tty=True))
    monkeypatch.setattr(
        ioradar701_led,
        "_SLEEP",
        lambda seconds: calls.append(("sleep", seconds)),
    )
    monkeypatch.setattr(
        ioradar701_led,
        "_OUTPUT_FACTORY",
        lambda *_args, **_kwargs: calls.append("gpio") or gpio,
    )
    monkeypatch.setattr(
        ioradar701_led.ioradar701_shadow,
        "_run_live",
        lambda *_args, **_kwargs: calls.append("sensors") or 0,
    )

    assert (
        ioradar701_led.main(
            ["--confirm-hardware", "--enable-led", "--start-delay", "5"]
        )
        == 0
    )
    assert calls == [
        ("sleep", 1.0),
        ("sleep", 1.0),
        ("sleep", 1.0),
        ("sleep", 1.0),
        ("sleep", 1.0),
        "gpio",
        "sensors",
    ]
    assert gpio.closed


def test_ctrl_c_during_start_delay_is_zero_hardware_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def interrupt_sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ioradar701_led.sys, "stdin", _Stream(tty=True, line="\n"))
    monkeypatch.setattr(ioradar701_led.sys, "stdout", _Stream(tty=True))
    monkeypatch.setattr(ioradar701_led.sys, "stderr", _Stream(tty=True))
    monkeypatch.setattr(ioradar701_led, "_SLEEP", interrupt_sleep)
    monkeypatch.setattr(
        ioradar701_led,
        "_OUTPUT_FACTORY",
        lambda *_args, **_kwargs: calls.append("gpio"),
    )

    assert (
        ioradar701_led.main(
            ["--confirm-hardware", "--enable-led", "--start-delay", "5"]
        )
        == 130
    )
    assert calls == []


def test_opted_in_entry_marks_gpio_and_disables_t4_phase_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gpio = _FakeGpio()
    received: dict[str, object] = {}

    def fake_run(_options: object, **kwargs: object) -> int:
        received.update(kwargs)
        return 0

    monkeypatch.setattr(ioradar701_led.sys, "stdin", _Stream(tty=True, line="\n"))
    monkeypatch.setattr(ioradar701_led.sys, "stdout", _Stream(tty=True))
    monkeypatch.setattr(ioradar701_led.sys, "stderr", _Stream(tty=True))
    monkeypatch.setattr(
        ioradar701_led,
        "_OUTPUT_FACTORY",
        lambda *_args, **_kwargs: gpio,
    )
    monkeypatch.setattr(ioradar701_led.ioradar701_shadow, "_run_live", fake_run)

    assert ioradar701_led.main(["--confirm-hardware", "--enable-led"]) == 0
    assert received["gpio_enabled"] is True
    assert received["t4_phase_prompts"] is False
    assert gpio.closed
