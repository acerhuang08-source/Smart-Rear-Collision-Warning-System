# Changelog

## Unreleased

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
  or sensor faults, successful cleanup, and restored `tfmini.service`.
- Note observed state oscillation at the 1.49/1.50 m and 3.00/3.01 m boundaries;
  distance filtering or hysteresis remains future work.
- Move gpiozero and lgpio into an optional `hardware` extra and lazy-load them
  only after explicit hardware confirmation, keeping software-only installs and
  imports portable.
- Treat LED and integrated diagnostic cleanup failures as command failures while
  preserving both primary and cleanup error details.
- Preserve the original GPIO adapter construction error when best-effort cleanup
  also fails, attaching cleanup diagnostics as an exception note.
