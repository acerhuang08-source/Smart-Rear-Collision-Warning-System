"""Injectable single-threaded dual-sensor sampling coordinator."""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from rear_warning.sensors.bno055.quality import assess_measurement_quality

from .models import (
    SensorSyncStatus, SynchronizedSensorSample, TimedDistanceMeasurement,
    TimedMotionMeasurement,
)
from .synchronizer import InvalidMotionMeasurement, SensorSynchronizer


class DistanceDevice(Protocol):
    def read_measurements(self) -> list[object]: ...


class MotionDevice(Protocol):
    def read_measurement(self) -> object: ...


class SampleSink(Protocol):
    def write(self, sequence: int, sample: SynchronizedSensorSample) -> None: ...


class SensorSamplingError(Exception):
    """A coordinated sampling session could not continue safely."""


class MotionDataReadyTimeout(SensorSamplingError):
    """No quality-valid BNO055 sample arrived within both finite bounds."""


class InvalidMonotonicClock(SensorSamplingError):
    """The injected monotonic clock returned a non-finite or negative value."""


class SchedulerIterationLimit(SensorSamplingError):
    """The scheduler's safety iteration limit was reached before its bound."""


class DistanceSamplingError(SensorSamplingError):
    """The TFMini device failed while sampling."""


class MotionSamplingError(SensorSamplingError):
    """The BNO055 device failed while sampling."""


def _positive_finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


@dataclass(frozen=True, slots=True)
class SamplingSchedule:
    bno055_interval_seconds: float = 0.1
    maximum_bno055_age_seconds: float = 0.2
    data_ready_timeout_seconds: float = 2.0
    data_ready_poll_interval_seconds: float = 0.02
    idle_sleep_seconds: float = 0.001
    maximum_iterations: int = 100_001

    def __post_init__(self) -> None:
        for name in (
            "bno055_interval_seconds",
            "maximum_bno055_age_seconds",
            "data_ready_timeout_seconds",
            "data_ready_poll_interval_seconds",
            "idle_sleep_seconds",
        ):
            object.__setattr__(self, name, _positive_finite(name, getattr(self, name)))
        if isinstance(self.maximum_iterations, bool) or not isinstance(
            self.maximum_iterations, int
        ):
            raise TypeError("maximum_iterations must be an integer")
        if self.maximum_iterations <= 0:
            raise ValueError("maximum_iterations must be positive")

    @property
    def maximum_data_ready_attempts(self) -> int:
        return (
            math.ceil(
                self.data_ready_timeout_seconds
                / self.data_ready_poll_interval_seconds
            )
            + 1
        )


@dataclass(slots=True)
class SamplingStatistics:
    valid_distance_measurements: int = 0
    bno055_measurements: int = 0
    matched_samples: int = 0
    no_motion_count: int = 0
    stale_motion_count: int = 0
    future_motion_count: int = 0
    invalid_distance_count: int = 0
    invalid_motion_count: int = 0
    uart_empty_reads: int = 0
    startup_motion_discards: int = 0
    startup_motion_reads: int = 0
    total_records: int = 0
    bno055_deadline_lateness_sum: float = 0.0
    bno055_deadline_lateness_maximum: float = 0.0
    bno055_deadline_checks: int = 0
    missed_bno055_periods: int = 0
    motion_age_sum: float = 0.0
    motion_age_minimum: float | None = None
    motion_age_maximum: float | None = None

    def record(self, sample: SynchronizedSensorSample) -> None:
        self.total_records += 1
        if sample.status is not SensorSyncStatus.INVALID_DISTANCE_SAMPLE:
            self.valid_distance_measurements += 1
        if sample.status is SensorSyncStatus.MATCHED:
            self.matched_samples += 1
            age = sample.motion_age_seconds
            assert age is not None
            self.motion_age_sum += age
            self.motion_age_minimum = (
                age
                if self.motion_age_minimum is None
                else min(self.motion_age_minimum, age)
            )
            self.motion_age_maximum = (
                age
                if self.motion_age_maximum is None
                else max(self.motion_age_maximum, age)
            )
        elif sample.status is SensorSyncStatus.NO_MOTION_SAMPLE:
            self.no_motion_count += 1
        elif sample.status is SensorSyncStatus.STALE_MOTION_SAMPLE:
            self.stale_motion_count += 1
        elif sample.status is SensorSyncStatus.FUTURE_MOTION_SAMPLE:
            self.future_motion_count += 1
        elif sample.status is SensorSyncStatus.INVALID_DISTANCE_SAMPLE:
            self.invalid_distance_count += 1
        elif sample.status is SensorSyncStatus.INVALID_MOTION_SAMPLE:
            self.invalid_motion_count += 1

    @property
    def motion_age_average(self) -> float | None:
        return (
            None
            if self.matched_samples == 0
            else self.motion_age_sum / self.matched_samples
        )

    def record_bno055_deadline(self, lateness: float, missed_periods: int) -> None:
        self.bno055_deadline_checks += 1
        self.bno055_deadline_lateness_sum += lateness
        self.bno055_deadline_lateness_maximum = max(
            self.bno055_deadline_lateness_maximum, lateness
        )
        self.missed_bno055_periods += missed_periods

    @property
    def bno055_deadline_lateness_average(self) -> float | None:
        if self.bno055_deadline_checks == 0:
            return None
        return self.bno055_deadline_lateness_sum / self.bno055_deadline_checks


class SensorSamplingCoordinator:
    """Read both devices in one thread and associate on one monotonic clock."""

    def __init__(
        self,
        distance_device: DistanceDevice,
        motion_device: MotionDevice,
        *,
        schedule: SamplingSchedule = SamplingSchedule(),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._distance_device = distance_device
        self._motion_device = motion_device
        self.schedule = schedule
        self._monotonic = monotonic
        self._sleep = sleep
        self._synchronizer = SensorSynchronizer(
            maximum_motion_age_seconds=schedule.maximum_bno055_age_seconds
        )
        self.statistics = SamplingStatistics()
        self.sampling_started_at: float | None = None
        self.sampling_finished_at: float | None = None
        self._last_now: float | None = None

    def _now(self) -> float:
        value = self._monotonic()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise InvalidMonotonicClock(
                "monotonic clock must return a finite non-negative number"
            )
        result = float(value)
        if self._last_now is not None and result < self._last_now:
            raise InvalidMonotonicClock("monotonic clock moved backwards")
        self._last_now = result
        return result

    @property
    def sampling_elapsed_seconds(self) -> float:
        if self.sampling_started_at is None or self.sampling_finished_at is None:
            return 0.0
        return max(self.sampling_finished_at - self.sampling_started_at, 0.0)

    def _commit_motion(self, measurement: object, captured: float) -> None:
        try:
            self._synchronizer.update_motion(
                TimedMotionMeasurement(measurement, captured)  # type: ignore[arg-type]
            )
        except InvalidMotionMeasurement as exc:
            raise SensorSamplingError(str(exc)) from exc

    def wait_for_motion_ready(self, stats: SamplingStatistics) -> float:
        started = self._now()
        deadline = started + self.schedule.data_ready_timeout_seconds
        last_issues: tuple[str, ...] = ()
        for attempt in range(1, self.schedule.maximum_data_ready_attempts + 1):
            try:
                measurement = self._motion_device.read_measurement()
            except Exception as exc:
                raise MotionSamplingError("BNO055 measurement read failed") from exc
            captured = self._now()
            stats.startup_motion_reads += 1
            assessment = assess_measurement_quality(measurement)  # type: ignore[arg-type]
            if assessment.state_issues:
                raise SensorSamplingError(
                    "invalid BNO055 fusion state: "
                    + "; ".join(assessment.state_issues)
                )
            if not assessment.data_issues:
                self._commit_motion(measurement, captured)
                return captured
            stats.startup_motion_discards += 1
            last_issues = assessment.data_issues
            now = self._now()
            if now >= deadline or attempt >= self.schedule.maximum_data_ready_attempts:
                raise MotionDataReadyTimeout(
                    f"BNO055 data ready timeout after {attempt} attempts: "
                    + "; ".join(last_issues)
                )
            self._sleep(
                min(self.schedule.data_ready_poll_interval_seconds, deadline - now)
            )
        raise MotionDataReadyTimeout("BNO055 data ready attempt bound exhausted")

    def run(
        self,
        *,
        duration_seconds: float | None,
        maximum_samples: int | None,
        sink: SampleSink | None = None,
        on_sample: Callable[[int, SynchronizedSensorSample], None] | None = None,
    ) -> tuple[SamplingStatistics, float]:
        if duration_seconds is None and maximum_samples is None:
            raise ValueError("duration_seconds or maximum_samples is required")
        if duration_seconds is not None:
            duration_seconds = _positive_finite("duration_seconds", duration_seconds)
        if maximum_samples is not None:
            if isinstance(maximum_samples, bool) or not isinstance(maximum_samples, int):
                raise TypeError("maximum_samples must be an integer")
            if maximum_samples <= 0:
                raise ValueError("maximum_samples must be positive")

        stats = SamplingStatistics()
        self.statistics = stats
        self.wait_for_motion_ready(stats)
        started = self._now()
        self.sampling_started_at = started
        next_motion_deadline = started + self.schedule.bno055_interval_seconds
        completed_by_bound = False
        attempted_distance_read = False

        try:
            for _iteration in range(self.schedule.maximum_iterations):
                now = self._now()
                if (
                    attempted_distance_read
                    and duration_seconds is not None
                    and now - started >= duration_seconds
                ):
                    completed_by_bound = True
                    break
                if maximum_samples is not None and stats.total_records >= maximum_samples:
                    completed_by_bound = True
                    break

                if now >= next_motion_deadline:
                    lateness = now - next_motion_deadline
                    missed_periods = math.floor(
                        lateness / self.schedule.bno055_interval_seconds
                    )
                    stats.record_bno055_deadline(lateness, missed_periods)
                    try:
                        measurement = self._motion_device.read_measurement()
                    except Exception as exc:
                        raise MotionSamplingError("BNO055 measurement read failed") from exc
                    captured = self._now()
                    assessment = assess_measurement_quality(measurement)  # type: ignore[arg-type]
                    if not assessment.is_valid:
                        raise SensorSamplingError(
                            "invalid runtime BNO055 measurement: "
                            + "; ".join(assessment.state_issues + assessment.data_issues)
                        )
                    self._commit_motion(measurement, captured)
                    stats.bno055_measurements += 1
                    next_motion_deadline += (
                        (missed_periods + 1)
                        * self.schedule.bno055_interval_seconds
                    )

                attempted_distance_read = True
                try:
                    measurements = self._distance_device.read_measurements()
                except Exception as exc:
                    raise DistanceSamplingError("TFMini measurement read failed") from exc
                if not measurements:
                    stats.uart_empty_reads += 1
                    self._sleep(self.schedule.idle_sleep_seconds)
                    continue
                for measurement in measurements:
                    captured = self._now()
                    sample = self._synchronizer.associate(
                        TimedDistanceMeasurement(measurement, captured)  # type: ignore[arg-type]
                    )
                    sequence = stats.total_records + 1
                    if sink is not None:
                        sink.write(sequence, sample)
                    stats.record(sample)
                    if on_sample is not None:
                        on_sample(sequence, sample)
                    if maximum_samples is not None and sequence >= maximum_samples:
                        completed_by_bound = True
                        break
                if completed_by_bound:
                    break

            if not completed_by_bound:
                raise SchedulerIterationLimit(
                    "scheduler iteration safety limit reached before duration/max-samples"
                )
        finally:
            active_error = sys.exc_info()[0] is not None
            self.sampling_finished_at = self._last_now
            try:
                self.sampling_finished_at = self._now()
            except InvalidMonotonicClock:
                if not active_error:
                    raise
        return stats, self.sampling_elapsed_seconds
