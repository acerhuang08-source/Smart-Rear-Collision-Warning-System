#!/usr/bin/env python3
"""Confirmation-gated six-lamp diagnostic for the assembled motorcycle model."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, TextIO


_SUCCESS = 0
_FAILED = 1
_USAGE = 2
_INTERRUPTED = 130
_EXPECTED_NAMES = ("A紅", "A黃", "A綠", "B紅", "B黃", "B綠")
_COLOUR_BY_NAME = {
    "A紅": "紅", "A黃": "黃", "A綠": "綠",
    "B紅": "紅", "B黃": "黃", "B綠": "綠",
}
_PHYSICAL_PIN_BY_BCM = {
    0: 27, 1: 28, 2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26,
    8: 24, 9: 21, 10: 19, 11: 23, 12: 32, 13: 33, 14: 8, 15: 10,
    16: 36, 17: 11, 18: 12, 19: 35, 20: 38, 21: 40, 22: 15,
    23: 16, 24: 18, 25: 22, 26: 37, 27: 13,
}
_RESERVED_SENSOR_BCMS = {2: "I2C SDA", 3: "I2C SCL", 14: "UART TX", 15: "UART RX"}


class _PinBank(Protocol):
    def set_active(self, controller: str, active: bool) -> None: ...
    def all_off(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Lamp:
    name: str
    installed_position: str
    lamp_type: str
    supply: str
    drive: str
    current_limit: str
    bcm: int
    physical_pin: int
    active_high: bool
    controller: str

    @property
    def active_level(self) -> str:
        return "HIGH (3.3 V logic)" if self.active_high else "LOW"


@dataclass(frozen=True, slots=True)
class Wiring:
    layout: str
    gpio_chip: int
    lamps: tuple[Lamp, ...]

    @property
    def controllers(self) -> tuple[str, ...]:
        order = {"紅": 0, "黃": 1, "綠": 2}
        return tuple(
            sorted(
                {lamp.controller for lamp in self.lamps},
                key=lambda item: (
                    order[_controller_lamps(self, item)[0].name[-1]],
                    _EXPECTED_NAMES.index(_controller_lamps(self, item)[0].name),
                ),
            )
        )


@dataclass(frozen=True, slots=True)
class Observation:
    controller: str
    expected_lamps: tuple[str, ...]
    lamp_confirmations: dict[str, bool]
    unexpected_lamp: bool


_INPUT: Callable[[str], str] = input
_SLEEP: Callable[[float], None] = time.sleep
_WALL_CLOCK: Callable[[], datetime] = lambda: datetime.now().astimezone()
_BANK_FACTORY: Callable[[Wiring], _PinBank] | None = None


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _require_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _load_wiring(path: Path) -> Wiring:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read wiring file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("wiring root must be a TOML table")
    layout = _require_text(raw.get("layout"), "layout")
    if layout not in {"independent_6", "shared_3"}:
        raise ValueError("layout must be independent_6 or shared_3")
    gpio_chip = _require_int(raw.get("gpio_chip"), "gpio_chip")
    if gpio_chip < 0:
        raise ValueError("gpio_chip must be non-negative")
    entries = raw.get("led")
    if not isinstance(entries, list) or len(entries) != 6:
        raise ValueError("exactly six [[led]] entries are required")

    lamps: list[Lamp] = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"led[{index}] must be a table")
        prefix = f"led[{index}]"
        active_high = entry.get("active_high")
        if not isinstance(active_high, bool):
            raise ValueError(f"{prefix}.active_high must be a boolean")
        bcm = _require_int(entry.get("bcm"), f"{prefix}.bcm")
        physical_pin = _require_int(entry.get("physical_pin"), f"{prefix}.physical_pin")
        if bcm not in _PHYSICAL_PIN_BY_BCM:
            raise ValueError(f"{prefix}.bcm is not a 40-pin-header GPIO")
        expected_pin = _PHYSICAL_PIN_BY_BCM[bcm]
        if physical_pin != expected_pin:
            raise ValueError(
                f"{prefix}: BCM{bcm} is physical Pin {expected_pin}, not Pin {physical_pin}"
            )
        if bcm in _RESERVED_SENSOR_BCMS:
            raise ValueError(
                f"{prefix}: BCM{bcm} conflicts with {_RESERVED_SENSOR_BCMS[bcm]}"
            )
        lamps.append(
            Lamp(
                name=_require_text(entry.get("name"), f"{prefix}.name"),
                installed_position=_require_text(
                    entry.get("installed_position"), f"{prefix}.installed_position"
                ),
                lamp_type=_require_text(entry.get("lamp_type"), f"{prefix}.lamp_type"),
                supply=_require_text(entry.get("supply"), f"{prefix}.supply"),
                drive=_require_text(entry.get("drive"), f"{prefix}.drive"),
                current_limit=_require_text(
                    entry.get("current_limit"), f"{prefix}.current_limit"
                ),
                bcm=bcm,
                physical_pin=physical_pin,
                active_high=active_high,
                controller=_require_text(entry.get("controller"), f"{prefix}.controller"),
            )
        )

    names = tuple(lamp.name for lamp in lamps)
    if set(names) != set(_EXPECTED_NAMES) or len(set(names)) != 6:
        raise ValueError("lamp names must be exactly A紅,A黃,A綠,B紅,B黃,B綠")
    wiring = Wiring(layout, gpio_chip, tuple(lamps))
    _validate_control_layout(wiring)
    return wiring


def _controller_lamps(wiring: Wiring, controller: str) -> tuple[Lamp, ...]:
    return tuple(lamp for lamp in wiring.lamps if lamp.controller == controller)


def _validate_control_layout(wiring: Wiring) -> None:
    groups = {
        controller: _controller_lamps(wiring, controller)
        for controller in {lamp.controller for lamp in wiring.lamps}
    }
    expected_group_count = 6 if wiring.layout == "independent_6" else 3
    if len(groups) != expected_group_count:
        raise ValueError(
            f"{wiring.layout} requires {expected_group_count} distinct controllers"
        )
    seen_bcm: dict[int, str] = {}
    for controller, lamps in groups.items():
        expected_size = 1 if wiring.layout == "independent_6" else 2
        if len(lamps) != expected_size:
            raise ValueError(f"controller {controller!r} must control {expected_size} lamp(s)")
        if len({lamp.bcm for lamp in lamps}) != 1:
            raise ValueError(f"controller {controller!r} has inconsistent BCM pins")
        if len({lamp.active_high for lamp in lamps}) != 1:
            raise ValueError(f"controller {controller!r} has inconsistent active levels")
        colours = {_COLOUR_BY_NAME[lamp.name] for lamp in lamps}
        if len(colours) != 1:
            raise ValueError(f"controller {controller!r} mixes lamp colours")
        bcm = lamps[0].bcm
        other = seen_bcm.get(bcm)
        if other is not None:
            raise ValueError(f"BCM{bcm} is assigned to both {other!r} and {controller!r}")
        seen_bcm[bcm] = controller
    if wiring.layout == "shared_3":
        for colour in ("紅", "黃", "綠"):
            members = [lamp for lamp in wiring.lamps if _COLOUR_BY_NAME[lamp.name] == colour]
            if members[0].controller != members[1].controller:
                raise ValueError(f"shared_3 requires both {colour} lamps to share one controller")


class _GpioZeroPinBank:
    def __init__(self, wiring: Wiring) -> None:
        from gpiozero import OutputDevice
        from gpiozero.pins.lgpio import LGPIOFactory

        self._factory = LGPIOFactory(chip=wiring.gpio_chip)
        self._devices: dict[str, object] = {}
        self._closed = False
        try:
            for controller in wiring.controllers:
                lamp = _controller_lamps(wiring, controller)[0]
                self._devices[controller] = OutputDevice(
                    lamp.bcm,
                    active_high=lamp.active_high,
                    initial_value=False,
                    pin_factory=self._factory,
                )
        except BaseException:
            self.close()
            raise

    def set_active(self, controller: str, active: bool) -> None:
        if self._closed:
            raise RuntimeError("GPIO pin bank is closed")
        device = self._devices[controller]
        method = getattr(device, "on" if active else "off")
        method()

    def all_off(self) -> None:
        for device in self._devices.values():
            getattr(device, "off")()

    def close(self) -> None:
        if self._closed:
            return
        first_error: BaseException | None = None
        for device in self._devices.values():
            try:
                getattr(device, "off")()
            except BaseException as exc:
                first_error = first_error or exc
            try:
                getattr(device, "close")()
            except BaseException as exc:
                first_error = first_error or exc
        try:
            self._factory.close()
        except BaseException as exc:
            first_error = first_error or exc
        self._closed = True
        if first_error is not None:
            raise first_error


def _make_bank(wiring: Wiring) -> _PinBank:
    if _BANK_FACTORY is not None:
        return _BANK_FACTORY(wiring)
    return _GpioZeroPinBank(wiring)


def _yes(prompt: str) -> bool:
    try:
        return _INPUT(prompt).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _print_table(wiring: Wiring, *, output: TextIO) -> None:
    print(f"layout={wiring.layout}; gpio_chip={wiring.gpio_chip}", file=output)
    print(
        "名稱 | 安裝位置 | 燈型 | 供電 | 驅動 | 限流 | BCM/Pin | 有效電位 | 控制者",
        file=output,
    )
    for name in _EXPECTED_NAMES:
        lamp = next(item for item in wiring.lamps if item.name == name)
        print(
            f"{lamp.name} | {lamp.installed_position} | {lamp.lamp_type} | "
            f"{lamp.supply} | {lamp.drive} | {lamp.current_limit} | "
            f"BCM{lamp.bcm}/Pin {lamp.physical_pin} | {lamp.active_level} | "
            f"{lamp.controller}",
            file=output,
        )


def _allocate_result(root: Path) -> tuple[Path, datetime]:
    wall = _WALL_CLOCK()
    base = wall.strftime("led-%Y%m%dT%H%M%S.%f%z")
    root.mkdir(parents=True, exist_ok=True)
    for suffix in ("", *(f"-{index}" for index in range(1, 1000))):
        directory = root / f"{base}{suffix}"
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return directory, wall
    raise OSError("could not allocate a unique LED result directory")


def _save_result(directory: Path, payload: dict[str, object]) -> None:
    path = directory / "result.json"
    with path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test six installed lamps using an explicit, validated wiring table."
    )
    parser.add_argument("--wiring", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--output-root", type=Path, default=Path("hardware-results"))
    parser.add_argument("--check-config", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(None if argv is None else list(argv))
    if not math.isfinite(args.seconds) or args.seconds <= 0 or args.seconds > 10:
        print("error: --seconds must be finite and in (0, 10]", file=sys.stderr)
        return _USAGE
    try:
        wiring = _load_wiring(args.wiring)
    except ValueError as exc:
        print(f"wiring_error: {exc}", file=sys.stderr)
        return _USAGE
    _print_table(wiring, output=sys.stdout)
    print(
        "檢查要求：限流／驅動／負載已按上表由操作員確認；GPIO 僅為 3.3 V 邏輯，"
        "不可輸入 5 V。啟動時先全關，每個控制者只建立一次，結束時全關並釋放。"
    )
    if args.check_config:
        print("config_check=PASS; zero GPIO access")
        return _SUCCESS
    try:
        _INPUT("確認上表為現地實際接法後按 Enter 開 GPIO；Ctrl+C 取消：")
    except EOFError:
        print("cancelled: EOF; zero GPIO access", file=sys.stderr)
        return _USAGE
    except KeyboardInterrupt:
        print("\ncancelled: zero GPIO access", file=sys.stderr)
        return _INTERRUPTED

    try:
        directory, started = _allocate_result(args.output_root)
    except OSError as exc:
        print(f"result_error: {exc}; zero GPIO access", file=sys.stderr)
        return _FAILED

    bank: _PinBank | None = None
    observations: list[Observation] = []
    errors: list[str] = []
    cleanup_errors: list[str] = []
    interrupted = False
    all_off_succeeded = False
    close_succeeded = False
    try:
        bank = _make_bank(wiring)
        bank.all_off()
        for controller in wiring.controllers:
            expected = _controller_lamps(wiring, controller)
            for other in wiring.controllers:
                bank.set_active(other, False)
            expected_names = tuple(lamp.name for lamp in expected)
            print(
                f"LED step: controller={controller}; expected={','.join(expected_names)}; "
                f"ON {args.seconds:g}s; all others OFF",
                flush=True,
            )
            bank.set_active(controller, True)
            _SLEEP(args.seconds)
            bank.set_active(controller, False)
            confirmations = {
                lamp.name: _yes(f"目視確認 {lamp.name}（{lamp.installed_position}）有亮且顏色正確？[y/N] ")
                for lamp in expected
            }
            unexpected = _yes("此步驟是否有任何非預期燈亮起？[y/N] ")
            observations.append(
                Observation(controller, expected_names, confirmations, unexpected)
            )
    except KeyboardInterrupt:
        interrupted = True
    except BaseException as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if bank is not None:
            try:
                bank.all_off()
                all_off_succeeded = True
            except BaseException as exc:
                cleanup_errors.append(f"all_off: {type(exc).__name__}: {exc}")
            try:
                bank.close()
                close_succeeded = True
            except BaseException as exc:
                cleanup_errors.append(f"close: {type(exc).__name__}: {exc}")

    every_lamp_confirmed = (
        len(observations) == len(wiring.controllers)
        and all(all(item.lamp_confirmations.values()) for item in observations)
    )
    no_unexpected = all(not item.unexpected_lamp for item in observations)
    status = (
        "PASS"
        if every_lamp_confirmed and no_unexpected and not errors
        and not cleanup_errors and all_off_succeeded and close_succeeded
        else "未驗證" if interrupted or len(observations) < len(wiring.controllers)
        else "FAIL"
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "started_at": started.isoformat(timespec="microseconds"),
        "ended_at": _WALL_CLOCK().isoformat(timespec="microseconds"),
        "layout": wiring.layout,
        "gpio_chip": wiring.gpio_chip,
        "wiring": [asdict(lamp) for lamp in wiring.lamps],
        "observations": [asdict(item) for item in observations],
        "interrupted": interrupted,
        "errors": errors,
        "cleanup_errors": cleanup_errors,
        "all_off_succeeded": all_off_succeeded,
        "gpio_close_succeeded": close_succeeded,
        "status": status,
    }
    try:
        _save_result(directory, payload)
    except OSError as exc:
        print(f"result_error: {exc}", file=sys.stderr)
        return _FAILED
    print(f"LED_result={status}")
    print(f"result_path={directory / 'result.json'}")
    print(f"cleanup_all_off={str(all_off_succeeded).lower()}")
    print(f"cleanup_gpio_close={str(close_succeeded).lower()}")
    for error in errors + cleanup_errors:
        print(f"LED_error: {error}", file=sys.stderr)
    if interrupted:
        return _INTERRUPTED
    return _SUCCESS if status == "PASS" else _FAILED


if __name__ == "__main__":
    raise SystemExit(main())
