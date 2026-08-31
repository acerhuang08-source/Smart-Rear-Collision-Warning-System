# Changelog

## Unreleased

- Add frozen/slotted timed distance, motion, and synchronized-sample models plus
  an I/O-free causal latest-motion synchronizer. Future and stale BNO055 samples
  are never counted as matched; the initial 0.1-second motion interval and
  0.2-second maximum age remain tunable and physically unvalidated.
- Add an injectable single-threaded sampling coordinator with monotonic
  deadlines, finite iteration/data-ready bounds, shared capture timestamps, and
  synchronization quality statistics without modifying the existing TFMini
  parser or BNO055 device behavior.
- Add the confirmation-gated `sensor-sync-diagnostic` CLI and exclusive-create
  structured CSV output. Unconfirmed use performs zero UART/I2C/CSV I/O and
  does not load serial or SMBus hardware modules.
- Add hardware-free synchronization, scheduler, CSV, cleanup, CLI, and import
  safety tests. Closing speed, TTC, filtering, WarningPolicy, and LED integration
  remain out of scope.
- Define read-completed wrapper timestamps as the canonical matching/CSV clock,
  start duration after BNO055 data-ready, and separate sampling from total
  elapsed time. Limit the synchronization UART timeout to the BNO interval and
  report BNO deadline lateness and missed periods without claiming hard
  real-time scheduling.
- Define `--max-samples` as committed matched plus unmatched records. CSV rows
  are counted only after write/flush success; a failed write may leave a partial
  evidence file. Reject invalid motion before replacing the latest valid sample
  and tolerate only absolute floating-point noise at the maximum-age boundary.
- Record the 2026-08-30 stationary 10-second dual-sensor capture. The diagnostic
  and log pipeline succeeded with 994/994 matched records, 99 runtime BNO reads,
  no sensor or CSV errors, 10.009 seconds sampling elapsed, 99.310 Hz distance
  rate and 9.891 Hz BNO rate. Offline verification found 994 complete,
  consecutive, causal, age-valid and quality-valid rows using 100 distinct
  canonical motion timestamps. The wrapper script itself returned 1 after its
  post-restore UART-holder check; later user-provided read-only checks confirmed
  the service/UART holder and BNO055 CONFIGMODE, without altering the raw log.

- Add an independent BNO055 software stack with frozen measurement models,
  pure register conversion, injectable register I/O, verified identity,
  CONFIGMODE-to-NDOF initialization, bounded readiness polling, continuous
  26-byte measurement reads, and explicit cleanup result reporting.
- Add `smbus2` to the optional `hardware` extra with lazy loading so core and
  `.[dev]` imports remain hardware-free.
- Add the confirmation-gated, bounded `bno055-diagnostic` CLI and comprehensive
  fake-bus tests. Runtime fusion state is validated for every sample, Ctrl+C
  returns 130, and zero-sample sessions cannot pass. The first physical NDOF
  diagnostic and its subsequent startup-quality correction are recorded below.
- Document the verified GY-BNO055 at bus 1/address `0x29`, 20 stable identity
  reads, `ST_RESULT=0x0f`, and the pre-fusion `SYS_ERR=0x09` observation without
  claiming that NDOF fusion has passed.
- Add I/O-free BNO055 measurement-quality validation with configurable broad
  quaternion (`0.5–1.5`) and gravity (`5–15 m/s²`) norm limits. Add bounded
  startup data-ready polling so incomplete startup frames are discarded before
  sampling begins, while any later state/data-quality failure stops the CLI.
- Record the 2026-08-28 physical 10-second NDOF run at address `0x29`: readiness
  `0x0c/0x05/0x00`, 94 originally counted samples at 9.374 Hz, no I2C or runtime
  state errors, successful CONFIGMODE cleanup, and an all-zero quaternion and
  gravity only in the first frame. This passed the control path but exposed a
  startup data-quality gap, so it is not recorded as a complete quality pass.
- Record the corrected 10-second NDOF regression as PASS: three zero-norm startup
  frames were discarded within 0.090 seconds, followed by 94 finite formal
  measurements at 9.382 Hz whose quaternion/gravity norms and fusion states all
  passed. I2C, runtime-state, and runtime-data errors were zero; cleanup returned
  the device to CONFIGMODE and `tfmini.service` remained active.
- Record the separate 2026-08-31 780-second BNO055 calibration observation:
  7,323 formal samples at 9.388 Hz, three startup discards, no read/state/data
  errors, successful cleanup, and all four calibration fields observed at level
  3 for the final 477.026 seconds. This does not validate calibration persistence
  or angle accuracy, and its B-side pose did not supply the intended opposite
  direction evidence.
- Record the focused 300-second upright-pose follow-up: four confirmed 20-second
  windows, 2,816 formal samples at 9.383 Hz, no read/state/data errors, successful
  logging and cleanup, opposite dominant gravity signs for TFMini-up/down, and a
  return close to the initial flat gravity direction. The comparison is
  descriptive and does not establish vehicle coordinates, axis remap, or angle
  accuracy; raw evidence and the wrapper tool remain local and outside Git.

- Add configurable distance warning policy with explicit boundary handling.
- Treat missing, invalid, and out-of-range measurements as `SENSOR_FAULT`.
- Add an injectable three-colour LED driver and centralized pin configuration.
- Add a minimal UART-to-warning-output pipeline and hardware-free integration tests.
- Add a gpiozero `OutputDevice` adapter using an explicit lgpio pin factory and
  the documented BCM GPIO17/GPIO27/GPIO22 active-high LED assignment.
- Add a confirmation-gated `led-diagnostic` command for red, yellow, green, and
  `SENSOR_FAULT`, with safe shutdown on completion and failure.
- Add hardware-free adapter and diagnostic CLI tests using fakes.
- Add the bounded, confirmation-gated `rear-warning-diagnostic` CLI connecting
  the TFMini Plus UART stack to the existing warning policy and physical LED
  adapter, with transition statistics and fail-safe cleanup.
- Document UART ownership, BCM-to-physical-pin wiring, lgpio environment setup,
  and manual `tfmini.service` stop/restore steps.
- Make the integrated diagnostic start logically in `SENSOR_FAULT` while LEDs
  remain off, preventing a pre-measurement `SAFE` assumption.
- Record the successful 2026-08-24 30-minute physical UART-to-LED integration
  run: 176,609 valid measurements at 98.110 Hz, no empty reads, parser errors,
  or sensor faults, successful cleanup, and restored `tfmini.service`. The
  existing 10-second and 30-minute results are pre-hysteresis historical
  baselines and do not validate the new hysteresis on physical hardware.
- Note observed state oscillation at the 1.49/1.50 m and 3.00/3.01 m boundaries
  in the historical physical run.
- Add a configurable, I/O-free warning-state hysteresis component. Original
  entry thresholds remain unchanged; `DANGER` releases to `WARNING` from
  1.7 m through 3.2 m, and normal states become `SAFE` only above 3.2 m.
  Distance filtering is not included, and the initial 0.2 m release margins
  still require physical tuning.
- Hysteresis status: physical validation pending; pre-hysteresis baselines only.
  Distance filtering: disabled. Release margin: initial/tunable 0.2 m.
- Move gpiozero and lgpio into an optional `hardware` extra and lazy-load them
  only after explicit hardware confirmation, keeping software-only installs and
  imports portable.
- Treat LED and integrated diagnostic cleanup failures as command failures while
  preserving both primary and cleanup error details.
- Preserve the original GPIO adapter construction error when best-effort cleanup
  also fails, attaching cleanup diagnostics as an exception note.
