"""Tests for the stateful, hardware-free warning hysteresis."""

from __future__ import annotations

import math

import pytest

from rear_warning.warning import (
    DistanceThresholds,
    HysteresisThresholds,
    WarningPolicy,
    WarningState,
    WarningStateStabilizer,
)


def _apply_distances(distances_cm: list[object]) -> list[WarningState]:
    policy = WarningPolicy()
    stabilizer = WarningStateStabilizer(policy.thresholds)
    return [
        stabilizer.stabilize(
            policy.classify_distance_cm(distance_cm), distance_cm
        )
        for distance_cm in distances_cm
    ]


def test_safe_boundary_requires_distance_above_release_threshold() -> None:
    assert _apply_distances([301, 300, 301, 320, 321]) == [
        WarningState.SAFE,
        WarningState.WARNING,
        WarningState.WARNING,
        WarningState.WARNING,
        WarningState.SAFE,
    ]


def test_danger_boundary_requires_distance_at_release_threshold() -> None:
    assert _apply_distances([150, 149, 150, 169, 170]) == [
        WarningState.WARNING,
        WarningState.DANGER,
        WarningState.DANGER,
        WarningState.DANGER,
        WarningState.WARNING,
    ]


def test_danger_release_uses_both_release_thresholds() -> None:
    policy = WarningPolicy()

    results = []
    for distance_cm in (169, 170, 301, 320, 321):
        stabilizer = WarningStateStabilizer(
            policy.thresholds, initial_state=WarningState.DANGER
        )
        results.append(
            stabilizer.stabilize(
                policy.classify_distance_cm(distance_cm), distance_cm
            )
        )

    assert results == [
        WarningState.DANGER,
        WarningState.WARNING,
        WarningState.WARNING,
        WarningState.WARNING,
        WarningState.SAFE,
    ]


@pytest.mark.parametrize("initial", [WarningState.SAFE, WarningState.WARNING])
def test_danger_escalation_is_immediate(initial: WarningState) -> None:
    stabilizer = WarningStateStabilizer(
        DistanceThresholds(), initial_state=initial
    )

    assert stabilizer.stabilize(WarningState.DANGER, 149) is WarningState.DANGER


def test_safe_escalates_to_warning_at_original_entry_boundary() -> None:
    stabilizer = WarningStateStabilizer(
        DistanceThresholds(), initial_state=WarningState.SAFE
    )

    assert stabilizer.stabilize(WarningState.WARNING, 300) is WarningState.WARNING


@pytest.mark.parametrize("initial", list(WarningState))
def test_sensor_fault_is_immediate_from_every_state(initial: WarningState) -> None:
    stabilizer = WarningStateStabilizer(
        DistanceThresholds(), initial_state=initial
    )

    assert (
        stabilizer.stabilize(WarningState.SENSOR_FAULT, None)
        is WarningState.SENSOR_FAULT
    )


@pytest.mark.parametrize(
    ("distance_cm", "expected"),
    [
        (400, WarningState.SAFE),
        (240, WarningState.WARNING),
        (120, WarningState.DANGER),
    ],
)
def test_sensor_fault_recovers_directly_from_first_valid_measurement(
    distance_cm: int, expected: WarningState
) -> None:
    policy = WarningPolicy()
    stabilizer = WarningStateStabilizer(policy.thresholds)

    assert (
        stabilizer.stabilize(
            policy.classify_distance_cm(distance_cm), distance_cm
        )
        is expected
    )


def test_release_thresholds_are_overridable() -> None:
    policy = WarningPolicy()
    stabilizer = WarningStateStabilizer(
        policy.thresholds,
        HysteresisThresholds(danger_release_m=1.6, safe_release_m=3.1),
    )

    assert stabilizer.stabilize(WarningState.WARNING, 150) is WarningState.WARNING
    assert stabilizer.stabilize(WarningState.DANGER, 149) is WarningState.DANGER
    assert stabilizer.stabilize(WarningState.WARNING, 160) is WarningState.WARNING
    assert stabilizer.stabilize(WarningState.SAFE, 310) is WarningState.WARNING
    assert stabilizer.stabilize(WarningState.SAFE, 311) is WarningState.SAFE


@pytest.mark.parametrize(
    ("danger_release_m", "safe_release_m"),
    [
        (math.nan, 3.2),
        (1.7, math.nan),
        (-math.inf, 3.2),
        (1.7, -math.inf),
        (math.inf, 3.2),
        (1.7, math.inf),
        (True, 3.2),
        (1.7, False),
        ("1.7", 3.2),
        (1.7, "3.2"),
        (-1.0, 3.2),
        (1.7, -1.0),
        (3.3, 3.2),
    ],
)
def test_intrinsically_invalid_release_thresholds_fail_during_construction(
    danger_release_m: object,
    safe_release_m: object,
) -> None:
    with pytest.raises(ValueError, match="hysteresis thresholds|must not exceed"):
        HysteresisThresholds(danger_release_m, safe_release_m)


@pytest.mark.parametrize(
    ("entry", "release"),
    [
        (DistanceThresholds(), HysteresisThresholds(1.4, 3.2)),
        (DistanceThresholds(), HysteresisThresholds(1.7, 2.9)),
        (DistanceThresholds(), HysteresisThresholds(1.7, 12.0)),
        (DistanceThresholds(), HysteresisThresholds(1.7, 12.1)),
        (
            DistanceThresholds(2.0, 4.0, 15.0),
            HysteresisThresholds(1.9, 4.2),
        ),
        (
            DistanceThresholds(2.0, 4.0, 15.0),
            HysteresisThresholds(2.2, 3.9),
        ),
        (
            DistanceThresholds(2.0, 4.0, 15.0),
            HysteresisThresholds(2.2, 15.0),
        ),
    ],
)
def test_release_thresholds_are_validated_against_policy_thresholds(
    entry: DistanceThresholds,
    release: HysteresisThresholds,
) -> None:
    with pytest.raises(ValueError):
        WarningStateStabilizer(entry, release)


def test_non_fault_candidate_requires_valid_distance() -> None:
    stabilizer = WarningStateStabilizer(DistanceThresholds())

    with pytest.raises(ValueError, match="positive finite distance"):
        stabilizer.stabilize(WarningState.SAFE, None)


def test_entry_and_release_thresholds_are_exposed_as_immutable_config() -> None:
    entry = DistanceThresholds()
    release = HysteresisThresholds()
    stabilizer = WarningStateStabilizer(entry, release)

    assert stabilizer.entry_thresholds is entry
    assert stabilizer.release_thresholds is release
