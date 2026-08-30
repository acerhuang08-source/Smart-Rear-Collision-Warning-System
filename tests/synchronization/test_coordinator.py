from __future__ import annotations

import csv
import io
import math
from pathlib import Path

import pytest

from rear_warning.sensors.bno055.models import (
    BNO055Calibration, BNO055Measurement, EulerAngles, Quaternion, Vector3,
)
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement
from rear_warning.synchronization import (
    InvalidMonotonicClock, MotionDataReadyTimeout, SamplingSchedule,
    SamplingStatistics, SchedulerIterationLimit, SensorSamplingCoordinator,
    SensorSynchronizer, TimedDistanceMeasurement, TimedMotionMeasurement,
)
from rear_warning.synchronization.csv_sink import (
    CsvSinkError, SynchronizedSampleCsvSink,
)


def valid_motion(*, zero: bool = False) -> BNO055Measurement:
    return BNO055Measurement(
        0.0, EulerAngles(0, 0, 0),
        Quaternion(0, 0, 0, 0) if zero else Quaternion(1, 0, 0, 0),
        Vector3(0, 0, 0), Vector3(0, 0, 0 if zero else 9.8), 25,
        BNO055Calibration(0, 0, 0, 0), 0x0C, 0x05, 0x00,
    )


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class DistanceDevice:
    def __init__(self, clock: Clock, *, empty: bool = False) -> None:
        self.clock = clock
        self.empty = empty
        self.calls = 0

    def read_measurements(self) -> list[TFMiniPlusMeasurement]:
        self.calls += 1
        self.clock.value += 0.01
        return [] if self.empty else [TFMiniPlusMeasurement(100, 500, 25.0)]


class MotionDevice:
    def __init__(self, values: list[BNO055Measurement] | None = None) -> None:
        self.values = list(values or [valid_motion()])
        self.calls = 0

    def read_measurement(self) -> BNO055Measurement:
        self.calls += 1
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def test_100hz_distance_and_10hz_motion_use_deadlines_without_drift() -> None:
    clock = Clock()
    distance = DistanceDevice(clock)
    motion = MotionDevice()
    coordinator = SensorSamplingCoordinator(
        distance, motion,
        schedule=SamplingSchedule(maximum_iterations=1000),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    stats, elapsed = coordinator.run(duration_seconds=0.31, maximum_samples=None)
    assert 29 <= stats.total_records <= 31
    assert motion.calls == 4  # startup, then 0.1/0.2/0.3 deadlines
    assert stats.matched_samples == stats.total_records
    assert elapsed >= 0.31


def test_updated_motion_resets_age_and_one_motion_matches_many_distances() -> None:
    clock = Clock()
    ages: list[float] = []
    coordinator = SensorSamplingCoordinator(
        DistanceDevice(clock), MotionDevice(),
        schedule=SamplingSchedule(maximum_iterations=100),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    stats, _ = coordinator.run(
        duration_seconds=None, maximum_samples=25,
        on_sample=lambda _sequence, sample: ages.append(sample.motion_age_seconds or 0.0),
    )
    assert stats.bno055_measurements >= 2
    assert stats.startup_motion_reads == 1
    assert any(later < earlier for earlier, later in zip(ages, ages[1:]))
    assert stats.matched_samples == 25


@pytest.mark.parametrize("backwards", [False, True])
def test_constant_or_backward_clock_is_bounded_by_iteration_limit(backwards: bool) -> None:
    class NonAdvancingClock:
        def __init__(self) -> None:
            self.value = 10.0
        def monotonic(self) -> float:
            result = self.value
            if backwards:
                self.value = max(0.0, self.value - 0.001)
            return result
    clock = NonAdvancingClock()
    coordinator = SensorSamplingCoordinator(
        DistanceDevice(Clock(), empty=True), MotionDevice(),
        schedule=SamplingSchedule(maximum_iterations=5),
        monotonic=clock.monotonic, sleep=lambda _seconds: None,
    )
    expected = InvalidMonotonicClock if backwards else SchedulerIterationLimit
    with pytest.raises(expected):
        coordinator.run(duration_seconds=1.0, maximum_samples=None)


def test_data_ready_constant_clock_noop_sleep_has_finite_attempts() -> None:
    clock = Clock()
    motion = MotionDevice([valid_motion(zero=True)])
    schedule = SamplingSchedule(
        data_ready_timeout_seconds=0.05,
        data_ready_poll_interval_seconds=0.02,
        maximum_iterations=10,
    )
    coordinator = SensorSamplingCoordinator(
        DistanceDevice(clock), motion, schedule=schedule,
        monotonic=clock.monotonic, sleep=lambda _seconds: None,
    )
    with pytest.raises(MotionDataReadyTimeout):
        coordinator.run(duration_seconds=None, maximum_samples=1)
    assert motion.calls == math.ceil(0.05 / 0.02) + 1


def test_startup_zero_motion_recovers_and_is_not_counted() -> None:
    clock = Clock()
    coordinator = SensorSamplingCoordinator(
        DistanceDevice(clock), MotionDevice([valid_motion(zero=True), valid_motion()]),
        schedule=SamplingSchedule(maximum_iterations=10),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    stats, _ = coordinator.run(duration_seconds=None, maximum_samples=1)
    assert stats.startup_motion_discards == 1
    assert stats.bno055_measurements == 0
    assert stats.startup_motion_reads == 2
    assert stats.matched_samples == 1


@pytest.mark.parametrize(
    ("name", "value"),
    [("bno055_interval_seconds", True), ("maximum_bno055_age_seconds", "0.2"),
     ("data_ready_timeout_seconds", math.nan), ("idle_sleep_seconds", 0),
     ("maximum_iterations", 0)],
)
def test_schedule_intrinsic_validation(name: str, value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        SamplingSchedule(**{name: value})  # type: ignore[arg-type]


def test_duration_and_max_samples_are_both_bounded() -> None:
    clock = Clock()
    coordinator = SensorSamplingCoordinator(
        DistanceDevice(clock), MotionDevice(),
        schedule=SamplingSchedule(maximum_iterations=10),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    stats, _ = coordinator.run(duration_seconds=100.0, maximum_samples=2)
    assert stats.total_records == 2


def test_data_ready_time_does_not_consume_duration_or_skip_first_uart_read() -> None:
    clock = Clock()

    class SlowReadyMotion(MotionDevice):
        def read_measurement(self) -> BNO055Measurement:
            clock.value += 2.0
            return super().read_measurement()

    distance = DistanceDevice(clock)
    coordinator = SensorSamplingCoordinator(
        distance, SlowReadyMotion(),
        schedule=SamplingSchedule(maximum_iterations=10),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    stats, elapsed = coordinator.run(duration_seconds=0.001, maximum_samples=None)
    assert distance.calls == 1
    assert stats.total_records == 1
    assert elapsed == pytest.approx(0.01)


def test_wrapper_timestamps_are_read_completed_times_from_shared_clock() -> None:
    clock = Clock(5.0)

    class SlowMotion(MotionDevice):
        def read_measurement(self) -> BNO055Measurement:
            clock.value += 0.03
            return valid_motion()

    distance = DistanceDevice(clock)
    samples = []
    coordinator = SensorSamplingCoordinator(
        distance, SlowMotion(),
        schedule=SamplingSchedule(maximum_iterations=10),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    coordinator.run(
        duration_seconds=None, maximum_samples=1,
        on_sample=lambda _sequence, sample: samples.append(sample),
    )
    sample = samples[0]
    assert sample.motion_measurement is not None
    assert sample.motion_measurement.monotonic_timestamp == 0.0
    assert sample.motion_timestamp == pytest.approx(5.03)
    assert sample.distance_timestamp == pytest.approx(5.04)
    assert sample.motion_age_seconds == pytest.approx(0.01)


def test_csv_uses_wrapper_timestamp_and_age_not_raw_bno_timestamp(
    tmp_path: Path,
) -> None:
    clock = Clock(5.0)
    raw_measurement = valid_motion()

    class SlowMotion(MotionDevice):
        def read_measurement(self) -> BNO055Measurement:
            clock.value += 0.03
            return raw_measurement

    path = tmp_path / "canonical.csv"
    sink = SynchronizedSampleCsvSink(path)
    coordinator = SensorSamplingCoordinator(
        DistanceDevice(clock), SlowMotion(),
        schedule=SamplingSchedule(maximum_iterations=10),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    coordinator.run(duration_seconds=None, maximum_samples=1, sink=sink)
    sink.close()
    with path.open(newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["bno055_timestamp"] == "5.03"
    assert float(row["distance_timestamp"]) == pytest.approx(5.04)
    assert float(row["motion_age_seconds"]) == pytest.approx(0.01)
    assert row["bno055_timestamp"] != "0.0"
    assert raw_measurement.monotonic_timestamp == 0.0


def test_blocking_uart_lateness_skips_periods_without_catchup_burst() -> None:
    clock = Clock()

    class BlockingDistance(DistanceDevice):
        def read_measurements(self) -> list[TFMiniPlusMeasurement]:
            self.calls += 1
            self.clock.value += 0.35
            return [TFMiniPlusMeasurement(100, 500, 25.0)]

    motion = MotionDevice()
    coordinator = SensorSamplingCoordinator(
        BlockingDistance(clock), motion,
        schedule=SamplingSchedule(maximum_iterations=10),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    stats, _ = coordinator.run(duration_seconds=None, maximum_samples=2)
    assert motion.calls == 2  # startup plus one runtime read, never a catch-up burst
    assert stats.bno055_deadline_checks == 1
    assert stats.missed_bno055_periods == 2
    assert stats.bno055_deadline_lateness_maximum == pytest.approx(0.25)


class FailingSink:
    def __init__(self, fail_on: int) -> None:
        self.fail_on = fail_on
        self.calls = 0

    def write(self, _sequence: int, _sample: object) -> None:
        self.calls += 1
        if self.calls == self.fail_on:
            raise CsvSinkError("write/flush failed")


@pytest.mark.parametrize("fail_on,committed", [(1, 0), (2, 1)])
def test_csv_failure_does_not_commit_failed_record(
    fail_on: int, committed: int
) -> None:
    clock = Clock()
    coordinator = SensorSamplingCoordinator(
        DistanceDevice(clock), MotionDevice(),
        schedule=SamplingSchedule(maximum_iterations=10),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    with pytest.raises(CsvSinkError):
        coordinator.run(
            duration_seconds=None, maximum_samples=3,
            sink=FailingSink(fail_on),
        )
    assert coordinator.statistics.total_records == committed
    assert coordinator.statistics.matched_samples == committed


def test_unmatched_csv_failure_does_not_increment_unmatched_counter() -> None:
    clock = Clock()
    coordinator = SensorSamplingCoordinator(
        DistanceDevice(clock), MotionDevice(),
        schedule=SamplingSchedule(
            maximum_bno055_age_seconds=0.005,
            maximum_iterations=10,
        ),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    with pytest.raises(CsvSinkError):
        coordinator.run(
            duration_seconds=None, maximum_samples=1,
            sink=FailingSink(1),
        )
    assert coordinator.statistics.total_records == 0
    assert coordinator.statistics.stale_motion_count == 0


def test_csv_flush_failure_does_not_commit_record() -> None:
    clock = Clock()

    class FlushFailsAfterHeader(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flushes = 0

        def flush(self) -> None:
            self.flushes += 1
            if self.flushes >= 2:
                raise OSError("row flush failed")
            super().flush()

    stream = FlushFailsAfterHeader()
    sink = SynchronizedSampleCsvSink(
        "unused.csv", opener=lambda *_args, **_kwargs: stream
    )
    coordinator = SensorSamplingCoordinator(
        DistanceDevice(clock), MotionDevice(),
        schedule=SamplingSchedule(maximum_iterations=10),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    with pytest.raises(CsvSinkError, match="write"):
        coordinator.run(
            duration_seconds=None, maximum_samples=1, sink=sink
        )
    assert coordinator.statistics.total_records == 0
    assert coordinator.statistics.matched_samples == 0


def test_max_samples_counts_committed_matched_and_unmatched_records() -> None:
    clock = Clock()
    coordinator = SensorSamplingCoordinator(
        DistanceDevice(clock), MotionDevice(),
        schedule=SamplingSchedule(
            maximum_bno055_age_seconds=0.005,
            maximum_iterations=10,
        ),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    stats, _ = coordinator.run(duration_seconds=None, maximum_samples=2)
    assert stats.total_records == 2
    assert stats.stale_motion_count == 2
    assert stats.matched_samples == 0


def test_max_samples_stops_on_mixed_matched_and_stale_committed_records() -> None:
    clock = Clock()
    distance = DistanceDevice(clock)
    coordinator = SensorSamplingCoordinator(
        distance, MotionDevice(),
        schedule=SamplingSchedule(
            maximum_bno055_age_seconds=0.015,
            maximum_iterations=10,
        ),
        monotonic=clock.monotonic, sleep=clock.advance,
    )
    stats, _ = coordinator.run(duration_seconds=None, maximum_samples=2)
    assert stats.total_records == 2
    assert stats.matched_samples == 1
    assert stats.stale_motion_count == 1
    assert distance.calls == 2


def test_statistics_classify_matched_stale_and_no_motion_records() -> None:
    measurement = TFMiniPlusMeasurement(100, 500, 25.0)
    no_motion = SensorSynchronizer().associate(
        TimedDistanceMeasurement(measurement, 1.0)
    )
    synchronizer = SensorSynchronizer(maximum_motion_age_seconds=0.2)
    synchronizer.update_motion(TimedMotionMeasurement(valid_motion(), 1.0))
    matched = synchronizer.associate(TimedDistanceMeasurement(measurement, 1.1))
    stale = synchronizer.associate(TimedDistanceMeasurement(measurement, 1.3))
    stats = SamplingStatistics()
    for sample in (matched, stale, no_motion):
        stats.record(sample)
    assert stats.total_records == 3
    assert stats.matched_samples == 1
    assert stats.stale_motion_count == 1
    assert stats.no_motion_count == 1
