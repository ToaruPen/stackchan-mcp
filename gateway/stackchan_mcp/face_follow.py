"""Gateway-owned StackChan face-follow controller and lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
import os
import time
from typing import Any, Literal, Protocol

from .camera_metrics import BoundedLatencyHistogram
from .camera_stream import CameraFrame
from .face_follow_detector import (
    AttentionDetection,
    AttentionDetector,
    load_pinto_face_detector,
    select_attention_target,
)


OBSERVATION_INTERVAL_MS = 100
MAX_FRAME_AGE_MS = 180
CAMERA_FPS = 20
CAMERA_QUALITY = 60
COMMAND_RATE_HZ = 10.0
MAX_PENDING_AGE_MS = 180
HOME_YAW = 0
HOME_PITCH = 33
TRACKING_PITCH_MIN = 23
DEAD_ZONE = 0.10
DEAD_ZONE_RELEASE = 0.14
YAW_GAIN_DEG = 44.0
PITCH_GAIN_DEG = 15.0
YAW_DIRECTION = 1
PITCH_DIRECTION = -1
MAX_STEP_DEG = 4.0
MOVE_SPEED_DPS = 90
LOST_HOME_TIMEOUT_MS = 750

AttentionMode = Literal["acquire", "track", "hold", "recover"]
EffectKind = Literal["hold", "move"]
FaceFollowOutcome = Literal[
    "target_selected",
    "no_candidate",
    "association_rejected",
    "frame_wait_timeout",
    "frame_stale",
    "inference_error",
    "tick_overlap_suppressed",
]

OUTCOMES: tuple[FaceFollowOutcome, ...] = (
    "target_selected",
    "no_candidate",
    "association_rejected",
    "frame_wait_timeout",
    "frame_stale",
    "inference_error",
    "tick_overlap_suppressed",
)
STAGE_NAMES = (
    "observation_interval_ms",
    "frame_wait_ms",
    "inference_ms",
    "capture_to_decision_ms",
    "capture_to_command_ms",
    "tick_total_ms",
)
VERTICAL_BUCKETS = ("0_20", "20_40", "40_60", "60_80", "80_100")


@dataclass(frozen=True, slots=True)
class HeadPose:
    yaw: int
    pitch: int


@dataclass(frozen=True, slots=True)
class AttentionState:
    mode: AttentionMode = "acquire"
    centered: bool = False
    last_target_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class AttentionEffect:
    kind: EffectKind
    pose: HeadPose | None = None


@dataclass(frozen=True, slots=True)
class TargetObservation:
    label: str
    confidence: float
    center_x: float
    center_y: float
    horizontal_error: float
    vertical_error: float
    centered: bool


@dataclass(frozen=True, slots=True)
class AttentionTransition:
    state: AttentionState
    effect: AttentionEffect
    target: TargetObservation | None = None


def advance_attention(
    state: AttentionState,
    *,
    now_ms: int,
    current_pose: HeadPose,
    detections: Sequence[AttentionDetection],
    observed_at_ms: int | None = None,
) -> AttentionTransition:
    """Advance the accepted controller using only the confirmed head pose."""
    target = select_attention_target(detections)
    if target is not None:
        return _track_target(
            state,
            target=target,
            now_ms=now_ms,
            observed_at_ms=observed_at_ms if observed_at_ms is not None else now_ms,
            current_pose=current_pose,
        )
    if state.last_target_at_ms is None:
        return AttentionTransition(
            state=AttentionState(mode="acquire"),
            effect=AttentionEffect("hold"),
        )
    if now_ms - state.last_target_at_ms >= LOST_HOME_TIMEOUT_MS:
        return _recover_home(state, current_pose=current_pose)
    return AttentionTransition(
        state=AttentionState(
            mode="hold",
            centered=state.centered,
            last_target_at_ms=state.last_target_at_ms,
        ),
        effect=AttentionEffect("hold"),
    )


def _recover_home(
    state: AttentionState,
    *,
    current_pose: HeadPose,
) -> AttentionTransition:
    home_pose = HeadPose(HOME_YAW, HOME_PITCH)
    return AttentionTransition(
        state=AttentionState(
            mode="recover",
            centered=state.centered,
            last_target_at_ms=state.last_target_at_ms,
        ),
        effect=(
            AttentionEffect("hold")
            if current_pose == home_pose
            else AttentionEffect("move", home_pose)
        ),
    )


def _track_target(
    state: AttentionState,
    *,
    target: AttentionDetection,
    now_ms: int,
    observed_at_ms: int,
    current_pose: HeadPose,
) -> AttentionTransition:
    del now_ms
    box = target.bounding_box
    center_x = (box.x_min + box.x_max) / 2
    center_y = (box.y_min + box.y_max) / 2
    horizontal_error = center_x - 0.5
    vertical_error = center_y - 0.5
    threshold = DEAD_ZONE_RELEASE if state.centered else DEAD_ZONE
    centered = (
        abs(horizontal_error) <= threshold and abs(vertical_error) <= threshold
    )
    observation = TargetObservation(
        label=target.label,
        confidence=target.confidence,
        center_x=center_x,
        center_y=center_y,
        horizontal_error=horizontal_error,
        vertical_error=vertical_error,
        centered=centered,
    )
    next_state = AttentionState(
        mode="track",
        centered=centered,
        last_target_at_ms=observed_at_ms,
    )
    if centered:
        return AttentionTransition(
            state=next_state,
            effect=AttentionEffect("hold"),
            target=observation,
        )

    yaw_step = _bounded_step(
        _correction(horizontal_error, YAW_GAIN_DEG, YAW_DIRECTION)
    )
    pitch_step = _bounded_step(
        _correction(vertical_error, PITCH_GAIN_DEG, PITCH_DIRECTION)
    )
    pose = HeadPose(
        yaw=_bounded_angle(current_pose.yaw + yaw_step, -90, 90),
        pitch=_bounded_angle(
            current_pose.pitch + pitch_step,
            TRACKING_PITCH_MIN,
            85,
        ),
    )
    effect = (
        AttentionEffect("hold")
        if pose == current_pose
        else AttentionEffect("move", pose)
    )
    return AttentionTransition(state=next_state, effect=effect, target=observation)


def _correction(error: float, gain: float, direction: int) -> float:
    beyond_dead_zone = math.copysign(max(0.0, abs(error) - DEAD_ZONE), error)
    return beyond_dead_zone * gain * direction


def _bounded_step(value: float) -> int:
    clamped = min(MAX_STEP_DEG, max(-MAX_STEP_DEG, value))
    return math.floor(clamped + 0.5)


def _bounded_angle(value: int, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, value))


@dataclass(slots=True)
class FaceFollowMetrics:
    """Image-free finite metrics for acquisition and reacquisition ownership."""

    started_at_ms: int
    _outcomes: dict[FaceFollowOutcome, int] = field(init=False)
    _stage_ms: dict[str, BoundedLatencyHistogram] = field(init=False)
    _first_target_at_ms: int | None = None
    _initial_outcomes: dict[FaceFollowOutcome, int] = field(default_factory=dict)
    _last_target_at_ms: int | None = None
    _current_loss_outcomes: dict[FaceFollowOutcome, int] = field(default_factory=dict)
    _episodes: int = 0
    _longest_gap_ms: int = 0
    _longest_outcomes: dict[FaceFollowOutcome, int] = field(default_factory=dict)
    _vertical_center_buckets: dict[str, int] = field(init=False)
    _loss_start_vertical_buckets: dict[str, int] = field(init=False)
    _yaw_step_limit_frames: int = 0
    _pitch_step_limit_frames: int = 0
    _loss_after_pitch_step_limit: int = 0
    _last_target: dict[str, int] | None = None
    _last_vertical_bucket: str | None = None
    _last_pitch_step_limited: bool = False
    _loss_origin_recorded: bool = True

    def __post_init__(self) -> None:
        self._outcomes = {outcome: 0 for outcome in OUTCOMES}
        self._stage_ms = {
            name: BoundedLatencyHistogram(maximum_bucket=30_000)
            for name in STAGE_NAMES
        }
        self._vertical_center_buckets = {name: 0 for name in VERTICAL_BUCKETS}
        self._loss_start_vertical_buckets = {
            name: 0 for name in VERTICAL_BUCKETS
        }

    def record_target_diagnostics(
        self,
        target: TargetObservation,
        *,
        current_pose: HeadPose,
        effect: AttentionEffect,
    ) -> None:
        """Record finite geometry needed to diagnose vertical target loss."""
        pose = effect.pose if effect.kind == "move" else None
        yaw_step = 0 if pose is None else pose.yaw - current_pose.yaw
        pitch_step = 0 if pose is None else pose.pitch - current_pose.pitch
        bucket = _vertical_bucket(target.center_y)
        yaw_limited = abs(yaw_step) >= MAX_STEP_DEG
        pitch_limited = abs(pitch_step) >= MAX_STEP_DEG
        self._vertical_center_buckets[bucket] += 1
        self._yaw_step_limit_frames += int(yaw_limited)
        self._pitch_step_limit_frames += int(pitch_limited)
        self._last_target = {
            "current_pitch": current_pose.pitch,
            "commanded_pitch": (
                current_pose.pitch if pose is None else pose.pitch
            ),
            "yaw_step": yaw_step,
            "pitch_step": pitch_step,
        }
        self._last_vertical_bucket = bucket
        self._last_pitch_step_limited = pitch_limited
        self._loss_origin_recorded = False

    def record_outcome(self, outcome: FaceFollowOutcome, *, now_ms: int) -> None:
        if outcome not in self._outcomes:
            raise ValueError("unknown face-follow outcome")
        self._outcomes[outcome] += 1
        if outcome == "target_selected":
            if self._first_target_at_ms is None:
                self._first_target_at_ms = now_ms
            elif self._current_loss_outcomes and self._last_target_at_ms is not None:
                gap_ms = max(0, now_ms - self._last_target_at_ms)
                self._episodes += 1
                if gap_ms > self._longest_gap_ms:
                    self._longest_gap_ms = gap_ms
                    self._longest_outcomes = dict(self._current_loss_outcomes)
                self._current_loss_outcomes.clear()
            self._last_target_at_ms = now_ms
            return

        if (
            outcome != "tick_overlap_suppressed"
            and self._last_vertical_bucket is not None
            and not self._loss_origin_recorded
        ):
            self._loss_start_vertical_buckets[self._last_vertical_bucket] += 1
            self._loss_after_pitch_step_limit += int(
                self._last_pitch_step_limited
            )
            self._loss_origin_recorded = True

        if outcome == "tick_overlap_suppressed":
            return
        if self._first_target_at_ms is None:
            _increment(self._initial_outcomes, outcome)
        elif self._last_target_at_ms is not None:
            _increment(self._current_loss_outcomes, outcome)

    def add_stage(self, name: str, milliseconds: float) -> None:
        histogram = self._stage_ms.get(name)
        if histogram is None:
            raise ValueError("unknown face-follow stage")
        histogram.add(milliseconds)

    def status(self, *, now_ms: int) -> dict[str, Any]:
        current_gap_ms = (
            max(0, now_ms - self._last_target_at_ms)
            if self._last_target_at_ms is not None and self._current_loss_outcomes
            else 0
        )
        return {
            "outcomes": dict(self._outcomes),
            "initial_acquisition": {
                "target_acquired": self._first_target_at_ms is not None,
                "elapsed_ms": (
                    max(0, now_ms - self.started_at_ms)
                    if self._first_target_at_ms is None
                    else max(0, self._first_target_at_ms - self.started_at_ms)
                ),
                "outcomes": _nonzero(self._initial_outcomes),
            },
            "reacquisition": {
                "episodes": self._episodes,
                "longest_gap_ms": self._longest_gap_ms,
                "longest_outcomes": _nonzero(self._longest_outcomes),
                "current_gap_ms": current_gap_ms,
                "current_outcomes": _nonzero(self._current_loss_outcomes),
            },
            "stage_ms": {
                name: histogram.status()
                for name, histogram in self._stage_ms.items()
            },
            "target_geometry": {
                "vertical_center_buckets": dict(
                    self._vertical_center_buckets
                ),
                "loss_start_vertical_buckets": dict(
                    self._loss_start_vertical_buckets
                ),
                "yaw_step_limit_frames": self._yaw_step_limit_frames,
                "pitch_step_limit_frames": self._pitch_step_limit_frames,
                "loss_after_pitch_step_limit": (
                    self._loss_after_pitch_step_limit
                ),
                "last_target": (
                    None
                    if self._last_target is None
                    else dict(self._last_target)
                ),
            },
        }


def _increment(counts: dict[FaceFollowOutcome, int], outcome: FaceFollowOutcome) -> None:
    counts[outcome] = counts.get(outcome, 0) + 1


def _nonzero(counts: Mapping[FaceFollowOutcome, int]) -> dict[str, int]:
    return {name: count for name, count in counts.items() if count}


def _vertical_bucket(center_y: float) -> str:
    index = min(len(VERTICAL_BUCKETS) - 1, max(0, int(center_y * 5)))
    return VERTICAL_BUCKETS[index]


class FaceFollowDevice(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> tuple[Any, dict[str, Any] | None]: ...


class FaceFollowCameraStream(Protocol):
    async def acquire(self, *, fps: int, quality: int) -> dict[str, Any]: ...

    async def release(self) -> dict[str, Any]: ...

    def touch(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


class FaceFollowFrameStore(Protocol):
    async def wait_for_frame(
        self,
        *,
        after_sequence: int | None,
        timeout_s: float,
    ) -> CameraFrame | None: ...

    def status(self) -> dict[str, Any]: ...


class FaceFollowHeadLane(Protocol):
    async def start(
        self,
        *,
        rate_hz: float,
        max_step_deg: float,
        max_pending_age_ms: int,
        speed_dps: int,
    ) -> dict[str, Any]: ...

    async def update(
        self,
        lease_id: str,
        sequence: int,
        yaw: int,
        pitch: int,
    ) -> dict[str, Any]: ...

    async def clear(self, lease_id: str) -> dict[str, Any]: ...

    async def stop(self, lease_id: str | None = None) -> dict[str, Any]: ...

    def status(self, lease_id: str | None = None) -> dict[str, Any]: ...


class FaceFollowService:
    """Own camera, detector, controller, and the single active servo lane."""

    def __init__(
        self,
        *,
        device: FaceFollowDevice,
        camera_stream: FaceFollowCameraStream,
        frames: FaceFollowFrameStore,
        head_lane: FaceFollowHeadLane,
        model_path: Callable[[], str] = lambda: os.getenv(
            "STACKCHAN_FACE_FOLLOW_MODEL", ""
        ),
        detector_loader: Callable[[str], AttentionDetector] = (
            load_pinto_face_detector
        ),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._device = device
        self._camera_stream = camera_stream
        self._frames = frames
        self._head_lane = head_lane
        self._model_path = model_path
        self._detector_loader = detector_loader
        self._now = monotonic
        self._sleep = sleep
        self._lifecycle_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._phase = "stopped"
        self._task: asyncio.Task[None] | None = None
        self._detector: AttentionDetector | None = None
        self._lease_id: str | None = None
        self._lane_status: dict[str, Any] | None = None
        self._lane_started = False
        self._camera_acquired = False
        self._generation = 0
        self._state = AttentionState()
        self._metrics = FaceFollowMetrics(started_at_ms=self._now_ms())
        self._last_frame_sequence: int | None = None
        self._next_target_sequence = 0
        self._pending_target = False
        self._frames_processed = 0
        self._target_frames = 0
        self._centered_frames = 0
        self._active_observation_ticks = 0
        self._maximum_active_observation_ticks = 0
        self._observation_ticks_started = 0
        self._observation_ticks_completed = 0
        self._last_observation_started_at: float | None = None
        self._errors: list[str] = []
        self._home_commanded = False

    async def start(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            if self._phase != "stopped":
                raise RuntimeError("face follow is already running")
            self._phase = "starting"
            self._home_commanded = False
            model_path = self._model_path().strip()
            if not model_path:
                self._phase = "stopped"
                raise RuntimeError("STACKCHAN_FACE_FOLLOW_MODEL is required")

            try:
                detector = await asyncio.to_thread(
                    self._detector_loader,
                    model_path,
                )
                await self._move_head(HeadPose(HOME_YAW, HOME_PITCH))
                await self._camera_stream.acquire(
                    fps=CAMERA_FPS,
                    quality=CAMERA_QUALITY,
                )
                self._camera_acquired = True
                lane_status = await self._head_lane.start(
                    rate_hz=COMMAND_RATE_HZ,
                    max_step_deg=MAX_STEP_DEG,
                    max_pending_age_ms=MAX_PENDING_AGE_MS,
                    speed_dps=MOVE_SPEED_DPS,
                )
                self._lane_started = True
                lease_id = lane_status.get("lease_id")
                if not isinstance(lease_id, str) or not lease_id:
                    raise RuntimeError("head target lane did not return a lease ID")
            except BaseException:
                await self._rollback_start()
                self._record_error("start_failed")
                self._phase = "stopped"
                raise

            self._detector = detector
            self._lease_id = lease_id
            self._lane_status = lane_status
            self._state = AttentionState()
            self._metrics = FaceFollowMetrics(started_at_ms=self._now_ms())
            self._last_frame_sequence = None
            self._next_target_sequence = 0
            self._pending_target = False
            self._frames_processed = 0
            self._target_frames = 0
            self._centered_frames = 0
            self._active_observation_ticks = 0
            self._maximum_active_observation_ticks = 0
            self._observation_ticks_started = 0
            self._observation_ticks_completed = 0
            self._last_observation_started_at = None
            self._errors = []
            self._generation += 1
            generation = self._generation
            self._stop_event.clear()
            self._phase = "running"
            self._task = asyncio.create_task(self._run_loop(generation))
            return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            if self._phase == "stopped":
                return self.status()
            cleanup_task = asyncio.create_task(self._stop_locked())
            try:
                return await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise

    async def _stop_locked(self) -> dict[str, Any]:
        self._phase = "stopping"
        self._generation += 1
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

        lease_id = self._lease_id
        lane_stopped = lease_id is None
        if lease_id is not None:
            if self._pending_target:
                try:
                    self._lane_status = await self._head_lane.clear(lease_id)
                except Exception:
                    self._record_error("lane_clear_failed")
            try:
                self._lane_status = await self._head_lane.stop(lease_id)
                lane_stopped = True
            except Exception:
                self._record_error("lane_stop_failed")
            finally:
                self._lease_id = None
                self._lane_started = False
                self._pending_target = False

        if lane_stopped:
            try:
                await self._move_head(HeadPose(HOME_YAW, HOME_PITCH + 4))
                await self._sleep(0.25)
                await self._move_head(HeadPose(HOME_YAW, HOME_PITCH))
                self._home_commanded = True
            except Exception:
                self._record_error("home_failed")

        if self._camera_acquired:
            try:
                await self._camera_stream.release()
            except Exception:
                self._record_error("camera_release_failed")
            finally:
                self._camera_acquired = False

        self._detector = None
        self._phase = "stopped"
        return self.status()

    def status(self) -> dict[str, Any]:
        lane_status = (
            self._head_lane.status(self._lease_id)
            if self._lease_id is not None
            else self._lane_status
        )
        detector_status: dict[str, Any] = {}
        detector = self._detector
        if detector is not None:
            status_method = getattr(detector, "status", None)
            if callable(status_method):
                detector_status = dict(status_method())
        return {
            "phase": self._phase,
            "frames_processed": self._frames_processed,
            "target_frames": self._target_frames,
            "centered_frames": self._centered_frames,
            "attention": {
                "mode": self._state.mode,
                "target_visible": self._state.mode == "track",
            },
            "flow": {
                "observation_ticks_started": self._observation_ticks_started,
                "observation_ticks_completed": self._observation_ticks_completed,
                "active_observation_ticks": self._active_observation_ticks,
                "maximum_active_observation_ticks": (
                    self._maximum_active_observation_ticks
                ),
                "last_accepted_frame_sequence": self._last_frame_sequence,
            },
            "metrics": self._metrics.status(now_ms=self._now_ms()),
            "head_target_lane": (
                dict(lane_status)
                if lane_status is not None
                else self._head_lane.status()
            ),
            "camera": self._camera_stream.status(),
            "camera_frames": self._frames.status(),
            "detector": detector_status,
            "safety": {
                "home_commanded": self._home_commanded,
                "home_yaw": HOME_YAW,
                "home_pitch": HOME_PITCH,
                "tracking_pitch_min": TRACKING_PITCH_MIN,
                "auto_sleep_changed": False,
            },
            "errors": list(self._errors),
        }

    async def _run_loop(self, generation: int) -> None:
        interval_s = OBSERVATION_INTERVAL_MS / 1_000
        deadline = self._now() + interval_s
        try:
            while self._is_current(generation):
                stopped = await self._sleep_until_stopped(
                    max(0.0, deadline - self._now())
                )
                if stopped:
                    return
                if not self._is_current(generation):
                    return
                try:
                    await self._run_tick(generation)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._is_current(generation):
                        self._record_error("tick_failed")
                deadline += interval_s
                while deadline <= self._now():
                    self._metrics.record_outcome(
                        "tick_overlap_suppressed",
                        now_ms=self._now_ms(),
                    )
                    deadline += interval_s
        except asyncio.CancelledError:
            raise

    async def _sleep_until_stopped(self, delay_s: float) -> bool:
        if self._stop_event.is_set():
            return True
        sleep_task = asyncio.create_task(self._sleep(delay_s))
        stop_task = asyncio.create_task(self._stop_event.wait())
        done, pending = await asyncio.wait(
            (sleep_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if stop_task in done:
            return True
        await sleep_task
        return self._stop_event.is_set()

    async def _run_tick(self, generation: int) -> None:
        if not self._is_current(generation):
            return
        tick_started = self._now()
        if self._last_observation_started_at is not None:
            self._metrics.add_stage(
                "observation_interval_ms",
                (tick_started - self._last_observation_started_at) * 1_000,
            )
        self._last_observation_started_at = tick_started
        self._observation_ticks_started += 1
        self._active_observation_ticks += 1
        self._maximum_active_observation_ticks = max(
            self._maximum_active_observation_ticks,
            self._active_observation_ticks,
        )
        try:
            self._camera_stream.touch()
            wait_started = self._now()
            frame = await self._frames.wait_for_frame(
                after_sequence=self._last_frame_sequence,
                timeout_s=OBSERVATION_INTERVAL_MS / 1_000,
            )
            self._metrics.add_stage(
                "frame_wait_ms",
                (self._now() - wait_started) * 1_000,
            )
            if not self._is_current(generation):
                return
            if frame is None:
                await self._advance_without_target(
                    "frame_wait_timeout",
                    generation=generation,
                )
                return
            if not self._frame_is_fresh(frame):
                await self._advance_without_target(
                    "frame_stale",
                    generation=generation,
                )
                return

            detector = self._detector
            if detector is None:
                raise RuntimeError("face follow detector is unavailable")
            inference_started = self._now()
            try:
                detections = await detector.detect(frame.jpeg)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._advance_without_target(
                    "inference_error",
                    generation=generation,
                )
                return
            self._metrics.add_stage(
                "inference_ms",
                (self._now() - inference_started) * 1_000,
            )
            if not self._is_current(generation):
                return
            if not self._frame_is_fresh(frame):
                await self._advance_without_target(
                    "frame_stale",
                    generation=generation,
                )
                return

            lease_id = self._require_lease()
            pose = self._refresh_lane_pose(lease_id)
            observed_at_ms = self._now_ms()
            transition = advance_attention(
                self._state,
                now_ms=observed_at_ms,
                observed_at_ms=observed_at_ms,
                current_pose=pose,
                detections=detections,
            )
            outcome: FaceFollowOutcome = (
                "target_selected"
                if transition.target is not None
                else (
                    "no_candidate" if not detections else "association_rejected"
                )
            )
            if transition.target is not None:
                self._metrics.record_target_diagnostics(
                    transition.target,
                    current_pose=pose,
                    effect=transition.effect,
                )
            self._metrics.record_outcome(outcome, now_ms=observed_at_ms)
            self._metrics.add_stage(
                "capture_to_decision_ms",
                observed_at_ms - _frame_received_monotonic_ms(frame),
            )
            self._last_frame_sequence = frame.gateway_sequence
            self._frames_processed += 1
            self._state = transition.state
            if transition.target is not None:
                self._target_frames += 1
                if transition.target.centered:
                    self._centered_frames += 1
            if not self._is_current(generation):
                return
            command_submitted = await self._apply_effect(
                transition.effect,
                lease_id,
            )
            if command_submitted:
                self._metrics.add_stage(
                    "capture_to_command_ms",
                    self._now_ms() - _frame_received_monotonic_ms(frame),
                )
        finally:
            self._active_observation_ticks -= 1
            self._observation_ticks_completed += 1
            self._metrics.add_stage(
                "tick_total_ms",
                (self._now() - tick_started) * 1_000,
            )

    async def _advance_without_target(
        self,
        outcome: FaceFollowOutcome,
        *,
        generation: int,
    ) -> None:
        now_ms = self._now_ms()
        self._metrics.record_outcome(outcome, now_ms=now_ms)
        if not self._is_current(generation):
            return
        lease_id = self._require_lease()
        transition = advance_attention(
            self._state,
            now_ms=now_ms,
            current_pose=self._refresh_lane_pose(lease_id),
            detections=[],
        )
        self._state = transition.state
        if self._is_current(generation):
            await self._apply_effect(transition.effect, lease_id)

    def _refresh_lane_pose(self, lease_id: str) -> HeadPose:
        self._lane_status = dict(self._head_lane.status(lease_id))
        self._pending_target = self._lane_status.get("pending_depth") == 1
        return _confirmed_pose(self._lane_status)

    async def _apply_effect(
        self,
        effect: AttentionEffect,
        lease_id: str,
    ) -> bool:
        if effect.kind == "hold":
            if self._pending_target:
                self._lane_status = await self._head_lane.clear(lease_id)
                self._pending_target = False
            return False
        if effect.pose is None:
            raise RuntimeError("move effect is missing a pose")
        self._next_target_sequence += 1
        await self._head_lane.update(
            lease_id,
            self._next_target_sequence,
            effect.pose.yaw,
            effect.pose.pitch,
        )
        self._lane_status = dict(self._head_lane.status(lease_id))
        self._pending_target = self._lane_status.get("pending_depth") == 1
        return True

    async def _move_head(self, pose: HeadPose) -> None:
        if pose.pitch < TRACKING_PITCH_MIN:
            raise ValueError("face follow refuses to command pitch below 23 degrees")
        result, error = await self._device.call_tool(
            "self.robot.set_head_angles",
            {
                "yaw": pose.yaw,
                "pitch": pose.pitch,
                "speed_dps": MOVE_SPEED_DPS,
            },
        )
        _require_device_success(result, error)

    async def _rollback_start(self) -> None:
        if self._lane_started:
            try:
                await self._head_lane.stop(self._lease_id)
            except Exception:
                self._record_error("lane_stop_failed")
            self._lease_id = None
            self._lane_started = False
        if self._camera_acquired:
            try:
                await self._camera_stream.release()
            except Exception:
                self._record_error("camera_release_failed")
            self._camera_acquired = False
        self._detector = None

    def _frame_is_fresh(self, frame: CameraFrame) -> bool:
        received_at = frame.received_monotonic_ms
        if received_at is None:
            return False
        age_ms = self._now_ms() - received_at
        return (
            frame.gateway_sequence
            > (-1 if self._last_frame_sequence is None else self._last_frame_sequence)
            and 0 <= age_ms <= MAX_FRAME_AGE_MS
        )

    def _is_current(self, generation: int) -> bool:
        return self._phase == "running" and self._generation == generation

    def _require_lease(self) -> str:
        if self._lease_id is None:
            raise RuntimeError("face follow head target lease is unavailable")
        return self._lease_id

    def _record_error(self, code: str) -> None:
        self._errors = [*self._errors, code][-8:]

    def _now_ms(self) -> int:
        return int(self._now() * 1_000)


def _confirmed_pose(status: Mapping[str, Any]) -> HeadPose:
    value = status.get("confirmed_pose")
    if not isinstance(value, Mapping):
        raise RuntimeError("head target lane confirmed pose is unavailable")
    yaw = value.get("yaw")
    pitch = value.get("pitch")
    if (
        isinstance(yaw, bool)
        or not isinstance(yaw, int)
        or isinstance(pitch, bool)
        or not isinstance(pitch, int)
        or not -90 <= yaw <= 90
        or not 5 <= pitch <= 85
    ):
        raise RuntimeError("head target lane confirmed pose is invalid")
    return HeadPose(yaw, pitch)


def _frame_received_monotonic_ms(frame: CameraFrame) -> int:
    if frame.received_monotonic_ms is None:
        raise RuntimeError("camera frame monotonic timestamp is missing")
    return frame.received_monotonic_ms


def _require_device_success(
    result: Any,
    error: Mapping[str, Any] | None,
) -> None:
    if error:
        raise RuntimeError(str(error.get("message", "device tool failed")))
    if not isinstance(result, Mapping) or result.get("isError") is True:
        raise RuntimeError("set_head_angles failed")
    payload: Mapping[str, Any] = result
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, Mapping) and isinstance(first.get("text"), str):
            try:
                decoded = json.loads(first["text"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("set_head_angles returned invalid JSON") from exc
            if isinstance(decoded, Mapping):
                payload = decoded
    for field_name in ("isError", "ok", "servo_init_ok", "servo_ok"):
        value = payload.get(field_name)
        if field_name == "isError" and value is True:
            raise RuntimeError("set_head_angles reported isError")
        if field_name != "isError" and value is False:
            raise RuntimeError(f"set_head_angles reported {field_name}=false")
