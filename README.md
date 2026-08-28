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
WarningPipeline -> WarningPolicy -> WarningStateStabilizer
                                      |
                                      v
                              WarningController -> LedDriver
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

Hysteresis is enabled after the stateless policy classification. Dangerous
states still enter at the original thresholds. `DANGER` remains below 1.7 m,
releases to `WARNING` from 1.7 m through 3.2 m, and may release directly to
`SAFE` only above 3.2 m. `WARNING` likewise releases to `SAFE` only above 3.2 m
(exactly 3.2 m remains `WARNING`). These release thresholds live in the
configurable `HysteresisThresholds`; the current 0.2 m margins are initial
values that still need physical tuning. No moving average, median filter, or
other distance filtering is applied.

> Hysteresis status: physical validation pending; pre-hysteresis baselines only.
> Distance filtering: disabled. Release margin: initial/tunable 0.2 m.

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
-> TFMiniPlusMeasurement -> WarningPolicy -> WarningStateStabilizer
-> WarningController
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

The existing 10-second and 30-minute physical validation results are
pre-hysteresis historical baselines. They verify the UART-to-LED chain, but do
not yet validate the current hysteresis behavior on physical hardware. The
30-minute run on 2026-08-24 recorded 176,609 valid measurements, no empty reads,
parser errors, or sensor faults, and successful cleanup. Boundary-state
oscillation was observed and is documented with the complete summary and
follow-up work in
[the warning-chain architecture document](docs/警示鏈架構.md#2026-08-24-30-分鐘實體整合驗證).

## BNO055 software device and diagnostic

The BNO055 support is an independent, headless sensor stack. Pure register
conversion functions create frozen measurement models; `BNO055Device` uses an
injected register-I/O interface; and the optional `smbus2` adapter is loaded
only after explicit hardware confirmation. It is not connected to
`WarningPolicy`, the warning controller, TFMini Plus, or the LEDs.

The confirmed module is a 3.3 V GY-BNO055 on I2C bus 1 at address `0x29`.
Read-only identification returned `A0/FB/32/0F` for the BNO055,
accelerometer, magnetometer, and gyroscope IDs in all 20 attempts, with
`ST_RESULT=0x0f`. Its power-on state was CONFIGMODE, and `SYS_ERR=0x09` was
observed before entering fusion mode. Two physical NDOF runs are recorded below.
All raw local logs are excluded from Git.

`bno055-diagnostic` requires `--duration` or `--max-samples`. Its optional
`--data-ready-timeout` defaults to 2 seconds. Without
`--confirm-hardware` it prints the bus, address, requested NDOF mode, and
planned register writes, returns code 2, and neither imports `smbus2` nor opens
an I2C bus. A future explicitly authorized physical run may use:

```bash
bno055-diagnostic --bus 1 --address 0x29 --duration 10 --confirm-hardware
```

This command writes CONFIGMODE, page 0, normal power, default SI/degree units,
and NDOF mode. After the minimum mode-switch delay it polls for at most two
seconds, every 20 ms, and proceeds only when mode is `0x0c`, system status is
`0x05`, and system error is `0x00`. Every measurement rechecks status and error.
Euler, quaternion, linear-acceleration, and gravity fields come from one
continuous 26-byte read; temperature, calibration, and system state are read
separately afterward. An I/O-free quality check also requires finite decoded
values, a quaternion norm from 0.5 through 1.5, and a gravity norm from 5 through
15 m/s². These broad limits reject incomplete startup frames; they do not require
non-zero Euler angles or linear acceleration, and they are not a calibration
threshold.

After NDOF readiness, a second bounded phase polls for the first quality-valid
measurement. The monotonic deadline and `ceil(timeout / 0.02) + 1` attempt cap
make this finite even with a stalled or backward injected clock. Startup frames
that have valid fusion state but incomplete quaternion/gravity are counted as
discarded and are not printed as measurements. Sampling duration begins with
the first quality-valid frame, which is sample 1. A later state or data-quality
failure is not discarded: it terminates the diagnostic with a non-zero result.
`Ctrl+C` reports interruption and returns 130; zero samples, I2C errors, and
cleanup failures also return non-zero. Cleanup returns to CONFIGMODE when
configuration began and always attempts to close the bus.

### 2026-08-28 first 10-second NDOF run

The authorized run used bus 1/address `0x29`, a 0.1-second interval, and a
10-second duration. Identity was `0xa0/0xfb/0x32/0x0f`; readiness reached
mode/status/error `0x0c/0x05/0x00`. The original CLI counted 94 samples over
10.028 seconds of sampling and 10.115 seconds total (`9.374 Hz`), with zero I2C
read errors, zero runtime-state errors, successful cleanup, exit code 0, and a
post-cleanup mode of `0x00`. `tfmini.service` remained active. Final calibration
was `(0,3,0,0)`; calibration 3 was not a success requirement.

The original first frame had all-zero quaternion and gravity. Frame 2 at
monotonic timestamp `6824.142549` was the first reasonable frame, and all
remaining frames were reasonable under the broad limits above, so 93 of the
original 94 frames were data-quality valid. The final frame had quaternion
`(0.999146,-0.003235,-0.041626,-0.000183)` and gravity
`(0.810,-0.060,9.770)`. Thus the run demonstrated working I2C communication,
NDOF entry, stable runtime fusion state, and CONFIGMODE cleanup, while revealing
the startup-frame validation gap. It was not a complete data-quality pass.

### 2026-08-28 quality-gate regression

The second authorized 10-second run used the corrected quality gate with a
2.0-second data-ready timeout and a maximum of 101 attempts. Readiness again
reached `0x0c/0x05/0x00`. Three startup frames had zero quaternion and gravity
norms and were discarded; the first valid frame arrived after 0.090 seconds.
The formal sample set contained 94 measurements over 10.019 seconds of sampling
and 10.194 seconds total (`9.382 Hz`). There were zero I2C read, runtime-state,
and runtime-data errors; the run was not interrupted, cleanup succeeded, exit
code was 0, post-cleanup mode was `0x00`, and `tfmini.service` remained active.

Every formal measurement was finite, had quaternion norm in `0.5–1.5`, gravity
norm in `5–15 m/s²`, and state `0x0c/0x05/0x00`. First and last quaternion norms
were both `1.000022166`; first and last gravity norms were both
`9.803846184 m/s²`. Calibration minimum, maximum, and final values were all
`(0,3,0,0)`. This bounded stationary desktop regression is a PASS for the
device-layer quality gate; it does not establish full calibration, road-dynamic
behavior, sensor fusion with TFMini Plus, or production service integration.

Do not run the confirmed form without reviewing wiring, bus ownership, expected
mode changes, and risks. The complete stage boundary and evidence are in
[the BNO055 stage completion report](docs/BNO055裝置層與實機驗證階段完成報告_2026-08-28.md).
See [the BNO055 architecture document](docs/BNO055軟體裝置層.md).
