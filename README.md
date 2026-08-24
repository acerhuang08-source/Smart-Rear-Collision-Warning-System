# Smart Rear Collision Warning System

A Raspberry Pi 5 based smart rear collision warning system using LiDAR and IMU sensor fusion.

## Minimal warning chain

The current headless core connects the TFMini Plus Light Detection and Ranging
(LiDAR／光達距離感測器) to a Measurement Model（量測資料模型）, Warning Policy
（警示策略，將距離分類）, and replaceable Light-Emitting Diode Driver
(LED Driver／發光二極體驅動層):

```text
TFMiniPlusSerialDevice -> TFMiniPlusParser -> TFMiniPlusMeasurement
                                               |
                                               v
WarningPipeline -> WarningPolicy -> WarningController -> LedDriver
```

The default policy uses these boundaries:

| Distance | State | LED pattern |
| --- | --- | --- |
| `0 < distance < 1.5 m` | `DANGER` | red |
| `1.5 m <= distance <= 3.0 m` | `WARNING` | yellow |
| `3.0 m < distance <= 12.0 m` | `SAFE` | green |
| missing, non-numeric, non-finite, `<= 0`, or `> 12.0 m` | `SENSOR_FAULT` | red + yellow |

The thresholds live in `DistanceThresholds` and may be overridden in tests or
deployment configuration. Invalid data is never treated as safe.

`WarningPolicy` performs no input/output (I/O／輸入輸出). `ThreeColorLedDriver`
only maps states to an injected `DigitalOutput`; it does not import a GPIO
library. The physical `GpioZeroDigitalOutput` adapter uses
`gpiozero.OutputDevice` with an explicit `LGPIOFactory(chip=0)` backend.

The documented active-high BCM assignment is centralized in
`LedPins.raspberry_pi_default()`:

| LED | BCM GPIO | Physical pin | Initial value |
| --- | ---: | ---: | --- |
| Red | 17 | 11 | off |
| Yellow | 27 | 13 | off |
| Green | 22 | 15 | off |

Each GPIO connects through its own current-limiting resistor to the LED anode;
each cathode connects to GND.

## Tests and diagnostics

Install the development dependencies and run all hardware-free tests:

```bash
python -m pip install -e ".[dev]"
pytest
```

This software-only installation does not require gpiozero or lgpio. On the
Raspberry Pi that will run the physical diagnostics, install the optional
hardware dependencies as well:

```bash
python -m pip install -e ".[dev,hardware]"
```

The tests use fake serial, gpiozero-device, pin-factory, and digital-output
implementations, including a full valid UART frame to LED-state test. `pytest`
never opens a real GPIO chip, so no Raspberry Pi or LED is required.

The existing TFMini Plus UART diagnostic opens a real serial device and must be
run only after confirming the device path is not owned by another process:

```bash
tfmini-plus-diagnostic --port /dev/serial0 --duration 10
```

The physical LED diagnostic shows red, yellow, green, then `SENSOR_FAULT`
(red + yellow), with all outputs off initially and after completion. It refuses
to create the GPIO adapter unless the explicit confirmation flag is present:

```bash
led-diagnostic --confirm-hardware
led-diagnostic --confirm-hardware --step-seconds 2
```

Before running it, verify the wiring above and ensure no other process owns
GPIO17, GPIO27, or GPIO22. This command performs real GPIO switching; do not run
it as part of automated tests. `Ctrl+C`, errors, normal completion, and process
exit all trigger best-effort LED shutdown and resource release.

## Physical warning-chain diagnostic

`rear-warning-diagnostic` runs the bounded, headless end-to-end chain:

```text
/dev/ttyAMA0 -> TFMiniPlusSerialDevice -> TFMiniPlusParser
-> TFMiniPlusMeasurement -> WarningPolicy -> WarningController
-> ThreeColorLedDriver -> GpioZeroDigitalOutput -> physical LEDs
```

It accepts `--port`, `--baudrate`, `--timeout`, `--duration`,
`--max-samples`, `--red-pin`, `--yellow-pin`, `--green-pin`,
`--gpio-chip`, and `--confirm-hardware`. At least one of `--duration` or
`--max-samples` is required, so the command cannot run unbounded by default.

Without `--confirm-hardware`, the command prints the selected UART, GPIO,
threshold, and LED mapping, then exits without constructing either hardware
adapter. With confirmation, gpiozero is given an explicit lgpio factory; setting
`GPIOZERO_PIN_FACTORY=lgpio` is also recommended for consistency with other
gpiozero utilities.

The gpiozero/lgpio modules are loaded lazily only after hardware confirmation.
If the `hardware` extra is absent, the command exits with an installation hint
instead of a `ModuleNotFoundError` traceback. Any UART or GPIO cleanup failure
makes the diagnostic fail; only `cleanup_completed=true` represents a fully
successful integrated run.

The existing `tfmini.service` may already own `/dev/ttyAMA0`. After checking the
wiring and confirming that interrupting that service is safe, the operator can
manually run the following commands. These are examples only and are never run
by the automated test suite:

```bash
sudo systemctl stop tfmini.service

GPIOZERO_PIN_FACTORY=lgpio rear-warning-diagnostic \
  --port /dev/ttyAMA0 \
  --baudrate 115200 \
  --timeout 0.1 \
  --duration 10 \
  --confirm-hardware

sudo systemctl start tfmini.service
```

The diagnostic reports state changes instead of every sensor frame and prints a
final statistics/cleanup summary. Empty reads and rejected/incomplete frames are
fail-safe `SENSOR_FAULT`, not `SAFE`. Parser and serial exceptions are reported,
then the command attempts to close UART, turn off all LEDs, close all output
devices, and release the lgpio factory. `Ctrl+C` performs the same cleanup.
`cleanup_completed=true` is emitted only if both UART and GPIO cleanup succeed.

At startup its logical state is `SENSOR_FAULT`, because no valid distance has
yet been observed, while all three physical LEDs remain off. The first valid
measurement produces the first policy-driven LED pattern and a transition such
as `SENSOR_FAULT -> SAFE`. A timeout before that measurement displays the
red-plus-yellow `SENSOR_FAULT` pattern without falsely reporting a safe state.

The full physical chain completed a 30-minute validation on 2026-08-24 with
176,609 valid measurements, no empty reads, parser errors, or sensor faults, and
successful cleanup. Boundary-state oscillation was observed and is documented
with the complete summary and follow-up work in
[the warning-chain architecture document](docs/警示鏈架構.md#2026-08-24-30-分鐘實體整合驗證).
