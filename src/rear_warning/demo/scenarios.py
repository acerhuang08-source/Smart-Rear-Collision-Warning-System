"""Deterministic, hardware-free scenarios using the production fusion chain."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict
from typing import Any, Callable

from rear_warning.fusion import (
    DistanceTrendThresholds,
    OwnMotionThresholds,
    RelativeMotionAssessment,
    RelativeMotionClass,
    RelativeMotionFusionAdapter,
    RelativeMotionThresholds,
)
from rear_warning.sensors.bno055.models import (
    BNO055Calibration,
    BNO055Measurement,
    EulerAngles,
    Quaternion,
    Vector3,
)
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement
from rear_warning.synchronization import (
    SensorSynchronizer,
    SensorSyncStatus,
    TimedDistanceMeasurement,
    TimedMotionMeasurement,
)
from rear_warning.warning import (
    DistanceThresholds,
    HysteresisThresholds,
    WarningPolicy,
    WarningState,
    WarningStateStabilizer,
)

_SAMPLE_INTERVAL_SECONDS = 0.05
_MOTION_INTERVAL_TICKS = 2
_SOURCE_LABEL = "合成資料模擬"

_CLASS_LABELS = {
    RelativeMotionClass.STABLE_OR_RECEDING: "距離穩定或遠離",
    RelativeMotionClass.STATIC_OR_SELF_APPROACH: "接近可能由自身動作造成",
    RelativeMotionClass.TARGET_APPROACH: "符合目標接近判定條件",
    RelativeMotionClass.UNCERTAIN: "資料不足或情況不確定",
}

_REASON_LABELS = {
    "distance_trend_not_ready": "距離趨勢仍在累積",
    "own_motion_not_ready": "自身動作證據仍在累積",
    "sync_unhealthy": "感測資料時間關聯或輸入狀態異常",
    "motion_stale": "動作資料已過期",
    "ambiguous_closing": "距離縮短速率未達明確條件",
    "stationary_baseline_not_established": "尚未建立有效近期靜止基準",
    "self_translational_evidence": "平移動作證據達判定條件",
    "self_rotational_evidence": "旋轉動作證據達判定條件",
    "self_translational_and_rotational_evidence": "平移與旋轉動作證據皆達判定條件",
    "stable_or_receding_range": "距離穩定或增加",
    "target_approach_evidence": "近期靜止基準、接近速率與持續時間條件成立",
    "candidate_persistence_not_satisfied": "候選分類持續時間尚未成立",
    "timestamp_discontinuity": "樣本時間不連續",
}

_SYNC_LABELS = {
    SensorSyncStatus.MATCHED: "距離與動作資料已配對",
    SensorSyncStatus.NO_MOTION_SAMPLE: "尚無動作資料",
    SensorSyncStatus.STALE_MOTION_SAMPLE: "動作資料過期",
    SensorSyncStatus.FUTURE_MOTION_SAMPLE: "動作資料時間晚於距離資料",
    SensorSyncStatus.INVALID_DISTANCE_SAMPLE: "距離數值無效（軟體注入）",
    SensorSyncStatus.INVALID_MOTION_SAMPLE: "動作資料無效",
}

_WARNING_LABELS = {
    WarningState.SAFE: "安全距離",
    WarningState.WARNING: "警戒距離",
    WarningState.DANGER: "危險距離",
    WarningState.SENSOR_FAULT: "距離感測異常",
}


def _round(value: float) -> float:
    """Keep trace JSON compact without changing algorithm input timestamps."""
    return round(value, 6)


def _motion(
    timestamp: float,
    *,
    acceleration_x: float = 0.0,
    rotation_radians: float = 0.0,
) -> BNO055Measurement:
    half_angle = rotation_radians / 2.0
    return BNO055Measurement(
        monotonic_timestamp=timestamp,
        euler=EulerAngles(
            heading=math.degrees(rotation_radians),
            roll=0.0,
            pitch=0.0,
        ),
        quaternion=Quaternion(
            w=math.cos(half_angle),
            x=0.0,
            y=0.0,
            z=math.sin(half_angle),
        ),
        linear_acceleration=Vector3(acceleration_x, 0.0, 0.0),
        gravity=Vector3(0.0, 0.0, 9.8),
        temperature_c=25,
        calibration=BNO055Calibration(0, 3, 0, 0),
        operation_mode=0x0C,
        system_status=0x05,
        system_error=0x00,
    )


def _distance(distance_cm: int) -> TFMiniPlusMeasurement:
    return TFMiniPlusMeasurement(
        distance_cm=distance_cm,
        strength=500,
        chip_temperature_c=24.5,
    )


def _frame(
    *,
    scenario_id: str,
    tick: int,
    sample_status: SensorSyncStatus,
    distance_measurement: TFMiniPlusMeasurement,
    motion_updated: bool,
    motion_measurement: BNO055Measurement | None,
    motion_timestamp: float | None,
    motion_age_seconds: float | None,
    assessment: RelativeMotionAssessment,
    warning_state: WarningState,
    phase: str,
) -> dict[str, Any]:
    trend = assessment.distance_trend
    own_motion = assessment.own_motion_evidence
    valid_distance = sample_status is not SensorSyncStatus.INVALID_DISTANCE_SAMPLE
    candidate = assessment.candidate_classification
    return {
        "scenario_id": scenario_id,
        "sample_index": tick,
        "timestamp_seconds": _round(tick * _SAMPLE_INTERVAL_SECONDS),
        "phase": phase,
        "input": {
            "distance_cm": distance_measurement.distance_cm if valid_distance else None,
            "distance_input_status": (
                "valid" if valid_distance else "invalid_numeric_value"
            ),
            "raw_distance_cm": distance_measurement.distance_cm,
            "motion_updated": motion_updated,
            "motion_timestamp": (
                None if motion_timestamp is None else _round(motion_timestamp)
            ),
            "motion_age_seconds": (
                None if motion_age_seconds is None else _round(motion_age_seconds)
            ),
            "linear_acceleration_mps2": (
                None
                if motion_measurement is None
                else [
                    _round(motion_measurement.linear_acceleration.x),
                    _round(motion_measurement.linear_acceleration.y),
                    _round(motion_measurement.linear_acceleration.z),
                ]
            ),
            "quaternion": (
                None
                if motion_measurement is None
                else [
                    _round(motion_measurement.quaternion.w),
                    _round(motion_measurement.quaternion.x),
                    _round(motion_measurement.quaternion.y),
                    _round(motion_measurement.quaternion.z),
                ]
            ),
            "sync_status": sample_status.value,
            "sync_status_zh_tw": _SYNC_LABELS[sample_status],
        },
        "output": {
            "distance_m": (
                _round(distance_measurement.distance_cm / 100.0)
                if valid_distance
                else None
            ),
            "closing_rate_mps": (
                None
                if trend.closing_rate_mps is None
                else _round(trend.closing_rate_mps)
            ),
            "distance_trend_ready": trend.is_ready,
            "distance_trend_reason": (
                None if trend.reason is None else trend.reason.value
            ),
            "own_motion_ready": own_motion.is_ready,
            "own_motion_reason": (
                None if own_motion.reason is None else own_motion.reason.value
            ),
            "translation_detected": own_motion.translational_motion_detected,
            "rotation_detected": own_motion.rotational_motion_detected,
            "classification": assessment.classification.value,
            "classification_zh_tw": _CLASS_LABELS[assessment.classification],
            "candidate_classification": (
                None if candidate is None else candidate.value
            ),
            "candidate_classification_zh_tw": (
                None if candidate is None else _CLASS_LABELS[candidate]
            ),
            "stationary_baseline_ready": assessment.stationary_baseline_ready,
            "assessment_valid": assessment.is_valid,
            "assessment_ready": assessment.is_ready,
            "assessment_reason": assessment.reason.value,
            "assessment_reason_zh_tw": _REASON_LABELS[assessment.reason.value],
            "warning_state": warning_state.value,
            "warning_state_zh_tw": _WARNING_LABELS[warning_state],
        },
    }


def _summary(frames: list[dict[str, Any]]) -> dict[str, Any]:
    class_counts = Counter(
        frame["output"]["classification"] for frame in frames
    )
    sync_counts = Counter(frame["input"]["sync_status"] for frame in frames)
    first_occurrence: dict[str, float] = {}
    for frame in frames:
        classification = frame["output"]["classification"]
        first_occurrence.setdefault(classification, frame["timestamp_seconds"])
    return {
        "sample_count": len(frames),
        "duration_seconds": frames[-1]["timestamp_seconds"],
        "classification_counts": dict(sorted(class_counts.items())),
        "sync_status_counts": dict(sorted(sync_counts.items())),
        "first_classification_occurrence_seconds": first_occurrence,
        "baseline_ever_ready": any(
            frame["output"]["stationary_baseline_ready"] for frame in frames
        ),
        "target_approach_ever_committed": any(
            frame["output"]["classification"]
            == RelativeMotionClass.TARGET_APPROACH.value
            for frame in frames
        ),
    }


def _run_scenario(
    *,
    scenario_id: str,
    name: str,
    description: str,
    total_ticks: int,
    distance_for_tick: Callable[[int], int],
    motion_for_tick: Callable[[int, float], BNO055Measurement],
    should_update_motion: Callable[[int], bool],
    phase_for_tick: Callable[[int], str],
    expected_result: str,
) -> dict[str, Any]:
    synchronizer = SensorSynchronizer()
    adapter = RelativeMotionFusionAdapter()
    policy = WarningPolicy()
    stabilizer = WarningStateStabilizer(policy.thresholds)
    frames: list[dict[str, Any]] = []

    for tick in range(total_ticks + 1):
        timestamp = tick * _SAMPLE_INTERVAL_SECONDS
        motion_updated = should_update_motion(tick)
        if motion_updated:
            synchronizer.update_motion(
                TimedMotionMeasurement(
                    motion_for_tick(tick, timestamp),
                    timestamp,
                )
            )
        distance_measurement = _distance(distance_for_tick(tick))
        synchronized = synchronizer.associate(
            TimedDistanceMeasurement(distance_measurement, timestamp)
        )
        assessment = adapter.update(synchronized)
        warning_candidate = policy.classify(distance_measurement)
        warning_state = stabilizer.stabilize(
            warning_candidate,
            distance_measurement.distance_cm,
        )
        frames.append(
            _frame(
                scenario_id=scenario_id,
                tick=tick,
                sample_status=synchronized.status,
                distance_measurement=distance_measurement,
                motion_updated=motion_updated,
                motion_measurement=synchronized.motion_measurement,
                motion_timestamp=synchronized.motion_timestamp,
                motion_age_seconds=synchronized.motion_age_seconds,
                assessment=assessment,
                warning_state=warning_state,
                phase=phase_for_tick(tick),
            )
        )

    return {
        "id": scenario_id,
        "name": name,
        "source_label": _SOURCE_LABEL,
        "description": description,
        "expected_result_note": expected_result,
        "sample_interval_seconds": _SAMPLE_INTERVAL_SECONDS,
        "frames": frames,
        "summary": _summary(frames),
    }


def _scenario_a() -> dict[str, Any]:
    stable_ticks = 30
    return _run_scenario(
        scenario_id="A",
        name="先穩定，再持續接近",
        description=(
            "先以穩定距離建立近期靜止基準，再讓距離持續縮短；"
            "可觀察暖機、候選持續、目標接近成立與基準到期。"
        ),
        total_ticks=70,
        distance_for_tick=lambda tick: (
            360
            if tick <= stable_ticks
            else 360 - round((tick - stable_ticks) * 3.0)
        ),
        motion_for_tick=lambda _tick, timestamp: _motion(timestamp),
        should_update_motion=lambda tick: tick % _MOTION_INTERVAL_TICKS == 0,
        phase_for_tick=lambda tick: (
            "穩定距離／建立基準" if tick <= stable_ticks else "持續接近"
        ),
        expected_result=(
            "基準與持續時間成立後短暫進入 TARGET_APPROACH；"
            "近期基準到期後忠實回到 UNCERTAIN。"
        ),
    )


def _scenario_b() -> dict[str, Any]:
    def moving_motion(tick: int, timestamp: float) -> BNO055Measurement:
        motion_index = tick // _MOTION_INTERVAL_TICKS
        return _motion(
            timestamp,
            acceleration_x=0.8,
            rotation_radians=math.radians(3.0 * motion_index),
        )

    return _run_scenario(
        scenario_id="B",
        name="自身移動造成距離縮短",
        description=(
            "距離縮短時，同步提供達門檻的平移與旋轉證據。"
            "這是合成資料，不代表已完成真實移動平台驗證。"
        ),
        total_ticks=40,
        distance_for_tick=lambda tick: 360 - round(tick * 2.5),
        motion_for_tick=moving_motion,
        should_update_motion=lambda tick: tick % _MOTION_INTERVAL_TICKS == 0,
        phase_for_tick=lambda _tick: "距離縮短＋自身動作證據",
        expected_result="持續條件成立後進入 STATIC_OR_SELF_APPROACH。",
    )


def _scenario_c() -> dict[str, Any]:
    return _run_scenario(
        scenario_id="C",
        name="接近途中才啟動",
        description=(
            "從第一筆起距離就持續縮短；雖無明顯自身動作證據，"
            "但缺少事先建立的靜止基準。"
        ),
        total_ticks=40,
        distance_for_tick=lambda tick: 360 - round(tick * 2.5),
        motion_for_tick=lambda _tick, timestamp: _motion(timestamp),
        should_update_motion=lambda tick: tick % _MOTION_INTERVAL_TICKS == 0,
        phase_for_tick=lambda _tick: "啟動時已在接近",
        expected_result="全程不得進入 TARGET_APPROACH，維持 UNCERTAIN。",
    )


def _scenario_d() -> dict[str, Any]:
    def should_update(tick: int) -> bool:
        if 31 <= tick <= 37:
            return False
        return tick % _MOTION_INTERVAL_TICKS == 0

    def phase(tick: int) -> str:
        if tick <= 30:
            return "有效資料／建立基準"
        if tick <= 37:
            return "暫停更新動作資料／形成 stale"
        if tick <= 68:
            return "有效資料恢復／重新建立基準"
        if tick <= 77:
            return "持續接近／形成目標候選"
        if tick == 78:
            return "注入無效距離數值"
        return "有效距離恢復／再次累積證據"

    def distance_for_tick(tick: int) -> int:
        if tick == 78:
            return 0
        if tick <= 68:
            return 250
        if tick <= 77:
            return 250 - (tick - 68) * 3
        return 223

    return _run_scenario(
        scenario_id="D",
        name="資料失效與恢復",
        description=(
            "先建立基準，接著停止更新動作資料以形成 stale；恢復後"
            "重新建立基準，再注入無效距離並再次恢復。"
        ),
        total_ticks=110,
        distance_for_tick=distance_for_tick,
        motion_for_tick=lambda _tick, timestamp: _motion(timestamp),
        should_update_motion=should_update,
        phase_for_tick=phase,
        expected_result=(
            "stale 與 invalid distance 都立即退回 UNCERTAIN 並清除基準；"
            "有效資料恢復後必須重新累積。"
        ),
    )


def build_demo_bundle(
    *,
    git_head: str,
    git_dirty: bool,
    generated_at_utc: str,
) -> dict[str, Any]:
    """Return all deterministic scenarios and their actual default parameters."""
    if not git_head.strip():
        raise ValueError("git_head must not be empty")
    if not isinstance(git_dirty, bool):
        raise TypeError("git_dirty must be a bool")
    if not generated_at_utc.strip():
        raise ValueError("generated_at_utc must not be empty")

    scenarios = [_scenario_a(), _scenario_b(), _scenario_c(), _scenario_d()]
    return {
        "schema_version": 1,
        "title": "智慧型後方接近警示系統｜融合判斷展示",
        "data_source": _SOURCE_LABEL,
        "generated_at_utc": generated_at_utc,
        "version": {
            "git_head": git_head,
            "git_dirty": git_dirty,
            "reproducibility_note": (
                "dirty=true 代表產生時工作樹含未提交修改；"
                "不可只以 HEAD 宣稱完全重現。"
            ),
        },
        "parameters": {
            "distance_sample_interval_seconds": _SAMPLE_INTERVAL_SECONDS,
            "motion_sample_interval_seconds": (
                _SAMPLE_INTERVAL_SECONDS * _MOTION_INTERVAL_TICKS
            ),
            "synchronizer": {"maximum_motion_age_seconds": 0.2},
            "distance_trend": asdict(DistanceTrendThresholds()),
            "own_motion": asdict(OwnMotionThresholds()),
            "relative_motion": asdict(RelativeMotionThresholds()),
            "distance_warning": asdict(DistanceThresholds()),
            "warning_hysteresis": asdict(HysteresisThresholds()),
        },
        "classification_explanations": {
            "stable_or_receding": "距離穩定或遠離；不等於道路環境安全。",
            "static_or_self_approach": (
                "接近可能由自身動作造成；不證明目標一定靜止。"
            ),
            "target_approach": (
                "符合目標接近判定條件；不代表辨識到車種或絕對車速。"
            ),
            "uncertain": "資料不足或情況不確定；畫面會忠實保留。",
        },
        "scenarios": scenarios,
    }


__all__ = ["build_demo_bundle"]
