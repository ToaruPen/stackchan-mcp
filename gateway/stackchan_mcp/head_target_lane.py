"""Asynchronous latest-only head target dispatch for StackChan."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import math
import secrets
import time
from typing import Any, Protocol

from .wifi_power_save import acquire_wifi_power_save, release_wifi_power_save


SERVO_YAW_MIN, SERVO_YAW_MAX = -90, 90
SERVO_PITCH_MIN, SERVO_PITCH_MAX = 5, 85
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_REPORTED_LATENCY_MS = 10_000
MAX_REPORTED_STAGE_US = 10_000_000
MAX_REPORTED_SCHEDULER_HOPS = 64
MCP_STAGE_TIMING_FIELDS = (
    "receiveToApply",
    "toolApply",
    "applyToReplyEnqueue",
)


class HeadTargetDevice(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[Any, dict[str, Any] | None]: ...

    async def acquire_head_target_lane(self) -> None: ...

    async def release_head_target_lane(self) -> None: ...

    async def read_head_target_pose(
        self,
    ) -> tuple[Any, dict[str, Any] | None]: ...

    async def call_head_target_tool(
        self,
        arguments: dict[str, Any],
    ) -> tuple[Any, dict[str, Any] | None]: ...


@dataclass(frozen=True, slots=True)
class HeadTargetLaneConfig:
    rate_hz: float
    max_step_deg: float
    max_pending_age_ms: int
    speed_dps: int


@dataclass(frozen=True, slots=True)
class PendingHeadTarget:
    sequence: int
    yaw: int
    pitch: int
    accepted_at: float


@dataclass(frozen=True, slots=True)
class DispatchedHeadTarget:
    target: PendingHeadTarget
    pose: dict[str, int]
    started_at: float


class _LatencyHistogram:
    """Bounded aggregate latency distribution without retaining samples."""

    def __init__(self, *, maximum: int = MAX_REPORTED_LATENCY_MS) -> None:
        self._maximum_bucket = maximum
        self._counts: dict[int, int] = {}
        self._count = 0
        self._maximum = 0

    def add(self, milliseconds: float) -> None:
        bucket = max(
            0,
            min(self._maximum_bucket, int(math.ceil(milliseconds))),
        )
        self._counts[bucket] = self._counts.get(bucket, 0) + 1
        self._count += 1
        self._maximum = max(self._maximum, bucket)

    def status(self) -> dict[str, int]:
        return {
            "count": self._count,
            "p50": self._percentile(0.5),
            "p95": self._percentile(0.95),
            "p99": self._percentile(0.99),
            "max": self._maximum,
        }

    def _percentile(self, percentile: float) -> int:
        if self._count == 0:
            return 0
        rank = max(1, math.ceil(self._count * percentile))
        seen = 0
        for bucket in sorted(self._counts):
            seen += self._counts[bucket]
            if seen >= rank:
                return bucket
        return self._maximum


class _FirmwareMcpStageMetrics:
    """Aggregate optional firmware same-clock stage timing metadata."""

    def __init__(self) -> None:
        self._count = 0
        self._timings = {
            field: _LatencyHistogram(maximum=MAX_REPORTED_STAGE_US)
            for field in MCP_STAGE_TIMING_FIELDS
        }
        self._scheduler_hops: dict[int, int] = {}

    def add(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        timings = [value.get(field) for field in MCP_STAGE_TIMING_FIELDS]
        scheduler_hops = value.get("schedulerHops")
        if any(
            not _is_bounded_integer(item, maximum=MAX_REPORTED_STAGE_US)
            for item in timings
        ) or not _is_bounded_integer(
            scheduler_hops,
            maximum=MAX_REPORTED_SCHEDULER_HOPS,
        ):
            return

        self._count += 1
        for field, timing in zip(MCP_STAGE_TIMING_FIELDS, timings, strict=True):
            self._timings[field].add(timing)
        self._scheduler_hops[scheduler_hops] = (
            self._scheduler_hops.get(scheduler_hops, 0) + 1
        )

    def status(self) -> dict[str, Any]:
        return {
            "count": self._count,
            **{
                field: _stage_histogram_status(self._timings[field])
                for field in MCP_STAGE_TIMING_FIELDS
            },
            "schedulerHops": {
                str(hops): count for hops, count in sorted(self._scheduler_hops.items())
            },
        }


class HeadTargetLane:
    """Own a bounded servo pipeline and one replaceable absolute target."""

    def __init__(
        self,
        device: HeadTargetDevice,
        *,
        monotonic_now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lease_id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
        device_call_timeout_seconds: float = 10.0,
    ) -> None:
        if device_call_timeout_seconds <= 0:
            raise ValueError("device call timeout must be positive")
        self._device = device
        self._now = monotonic_now
        self._sleep = sleep
        self._lease_id_factory = lease_id_factory
        self._device_call_timeout_seconds = device_call_timeout_seconds
        self._lifecycle_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._pending_event = asyncio.Event()
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._worker: asyncio.Task[None] | None = None
        self._phase = "stopped"
        self._lease_id: str | None = None
        self._last_lease_id: str | None = None
        self._config: HeadTargetLaneConfig | None = None
        self._pending: PendingHeadTarget | None = None
        self._wifi_lease_active = False
        self._device_lane_acquired = False
        self._last_dispatch_started_at: float | None = None
        self._confirmed_pose: dict[str, int] | None = None
        self._planned_pose: dict[str, int] | None = None
        self._last_accepted_sequence: int | None = None
        self._last_confirmed_sequence: int | None = None
        self._accepted = 0
        self._replaced = 0
        self._dispatched = 0
        self._confirmed = 0
        self._failed = 0
        self._stale_discarded = 0
        self._no_op_discarded = 0
        self._active_calls = 0
        self._maximum_active_calls = 0
        self._maximum_pending_depth = 0
        self._post_stop_dispatches = 0
        self._last_error: str | None = None
        self._update_acknowledgment_ms = _LatencyHistogram()
        self._pending_age_ms = _LatencyHistogram()
        self._device_dispatch_latency_ms = _LatencyHistogram()
        self._successful_reply_latency_ms = _LatencyHistogram()
        self._firmware_mcp_stage_us = _FirmwareMcpStageMetrics()

    async def start(
        self,
        *,
        rate_hz: float,
        max_step_deg: float,
        max_pending_age_ms: int,
        speed_dps: int,
    ) -> dict[str, Any]:
        async with self._lifecycle_lock:
            return await self._start(
                rate_hz=rate_hz,
                max_step_deg=max_step_deg,
                max_pending_age_ms=max_pending_age_ms,
                speed_dps=speed_dps,
            )

    async def _start(
        self,
        *,
        rate_hz: float,
        max_step_deg: float,
        max_pending_age_ms: int,
        speed_dps: int,
    ) -> dict[str, Any]:
        config = _validate_config(
            rate_hz=rate_hz,
            max_step_deg=max_step_deg,
            max_pending_age_ms=max_pending_age_ms,
            speed_dps=speed_dps,
        )
        async with self._lock:
            if self._phase != "stopped":
                raise RuntimeError("head target lane is already running")
            self._phase = "starting"

        acquired_device_lane = False
        acquired_wifi = False
        try:
            await self._device.acquire_head_target_lane()
            acquired_device_lane = True
            confirmed_pose = await self._read_confirmed_pose()
            await acquire_wifi_power_save(self._device)
            acquired_wifi = True
            lease_id = self._lease_id_factory()
            if not isinstance(lease_id, str) or not lease_id:
                raise RuntimeError("head target lane lease ID is invalid")

            async with self._lock:
                self._reset_for_start(
                    config=config,
                    lease_id=lease_id,
                    confirmed_pose=confirmed_pose,
                )
                self._device_lane_acquired = True
                self._wifi_lease_active = True
                self._phase = "running"
                self._worker = asyncio.create_task(self._run_worker())
                return self.status(lease_id)
        except BaseException:
            if acquired_wifi:
                await release_wifi_power_save(self._device)
            if acquired_device_lane:
                await self._device.release_head_target_lane()
            async with self._lock:
                self._wifi_lease_active = False
                self._device_lane_acquired = False
                self._phase = "stopped"
            raise

    async def update(
        self,
        lease_id: str,
        sequence: int,
        yaw: int,
        pitch: int,
    ) -> dict[str, Any]:
        started_at = self._now()
        _require_safe_integer(sequence, "sequence", 0, MAX_SAFE_INTEGER)
        _require_safe_integer(yaw, "yaw", SERVO_YAW_MIN, SERVO_YAW_MAX)
        _require_safe_integer(pitch, "pitch", SERVO_PITCH_MIN, SERVO_PITCH_MAX)

        async with self._lock:
            self._require_running_lease(lease_id)
            if (
                self._last_accepted_sequence is not None
                and sequence <= self._last_accepted_sequence
            ):
                raise ValueError("sequence must be strictly increasing")
            replaced = self._pending is not None
            self._pending = PendingHeadTarget(
                sequence=sequence,
                yaw=yaw,
                pitch=pitch,
                accepted_at=started_at,
            )
            self._last_accepted_sequence = sequence
            self._accepted += 1
            if replaced:
                self._replaced += 1
            self._maximum_pending_depth = max(self._maximum_pending_depth, 1)
            self._idle_event.clear()
            self._pending_event.set()
            self._update_acknowledgment_ms.add((self._now() - started_at) * 1000)
            return {
                "accepted": True,
                "replaced": replaced,
                "pending_depth": 1,
                "accepted_count": self._accepted,
                "replaced_count": self._replaced,
                "last_accepted_sequence": sequence,
            }

    async def clear(self, lease_id: str) -> dict[str, Any]:
        async with self._lock:
            self._require_running_lease(lease_id)
            self._pending = None
            if self._active_calls == 0:
                self._idle_event.set()
            return self.status(lease_id)

    async def motion_status(self, lease_id: str) -> dict[str, Any]:
        async with self._lock:
            self._require_running_lease(lease_id)

        result, error = await self._device.call_tool(
            "self.robot.get_head_angles",
            {"cached_motion_state": True},
        )
        if error:
            raise RuntimeError(_bounded_error(error))
        payload = _decode_payload(result)
        if payload is None:
            raise RuntimeError("cached motion state returned an invalid result")
        expected_fields = {
            "yaw",
            "pitch",
            "target_yaw",
            "target_pitch",
            "moving",
        }
        if set(payload) != expected_fields:
            raise RuntimeError("cached motion state fields are invalid")
        _require_safe_integer(
            payload["yaw"],
            "cached yaw",
            SERVO_YAW_MIN,
            SERVO_YAW_MAX,
        )
        _require_safe_integer(
            payload["pitch"],
            "cached pitch",
            SERVO_PITCH_MIN,
            SERVO_PITCH_MAX,
        )
        _require_safe_integer(
            payload["target_yaw"],
            "cached target yaw",
            SERVO_YAW_MIN,
            SERVO_YAW_MAX,
        )
        _require_safe_integer(
            payload["target_pitch"],
            "cached target pitch",
            SERVO_PITCH_MIN,
            SERVO_PITCH_MAX,
        )
        if not isinstance(payload["moving"], bool):
            raise RuntimeError("cached moving flag is invalid")

        async with self._lock:
            self._require_running_lease(lease_id)
        return payload

    def status(self, lease_id: str | None = None) -> dict[str, Any]:
        expected_lease_id = self._lease_id or self._last_lease_id
        return {
            "phase": self._phase,
            "lease_id": self._lease_id,
            "lease_id_match": (
                None if lease_id is None else lease_id == expected_lease_id
            ),
            "accepted": self._accepted,
            "replaced": self._replaced,
            "dispatched": self._dispatched,
            "confirmed": self._confirmed,
            "confirmed_apply_replies": self._confirmed,
            "failed": self._failed,
            "stale_discarded": self._stale_discarded,
            "no_op_discarded": self._no_op_discarded,
            "active_calls": self._active_calls,
            "pending_depth": 1 if self._pending is not None else 0,
            "maximum_active_calls": self._maximum_active_calls,
            "maximum_pending_depth": self._maximum_pending_depth,
            "post_stop_dispatches": self._post_stop_dispatches,
            "last_accepted_sequence": self._last_accepted_sequence,
            "last_confirmed_sequence": self._last_confirmed_sequence,
            "confirmed_pose": (
                None if self._confirmed_pose is None else dict(self._confirmed_pose)
            ),
            "update_acknowledgment_ms": (self._update_acknowledgment_ms.status()),
            "pending_age_ms": self._pending_age_ms.status(),
            "device_dispatch_latency_ms": (self._device_dispatch_latency_ms.status()),
            "successful_reply_latency_ms": (self._successful_reply_latency_ms.status()),
            "firmware_mcp_stage_us": self._firmware_mcp_stage_us.status(),
            "last_error": self._last_error,
        }

    async def stop(self, lease_id: str | None = None) -> dict[str, Any]:
        async with self._lifecycle_lock:
            return await self._stop(lease_id)

    async def _stop(self, lease_id: str | None = None) -> dict[str, Any]:
        async with self._lock:
            if self._phase == "stopped":
                self._require_stopped_lease(lease_id)
                return self.status(lease_id)
            self._require_current_lease(lease_id)
            self._phase = "stopping"
            self._pending = None
            self._pending_event.set()
            worker = self._worker

        timeout_error: RuntimeError | None = None
        cancellation_error: asyncio.CancelledError | None = None
        try:
            try:
                if worker is not None:
                    await asyncio.wait_for(
                        asyncio.shield(worker),
                        timeout=self._device_call_timeout_seconds,
                    )
            except TimeoutError as exc:
                if worker is not None:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                async with self._lock:
                    self._phase = "failed"
                    self._last_error = "head target lane stop timed out"
                timeout_error = RuntimeError("head target lane stop timed out")
                timeout_error.__cause__ = exc
            await self._release_resources()
        except asyncio.CancelledError as exc:
            cancellation_error = exc
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await self._release_resources()

        async with self._lock:
            self._last_lease_id = self._lease_id
            self._lease_id = None
            self._worker = None
            self._phase = "stopped"
            self._idle_event.set()
            stopped = self.status(lease_id)
        if cancellation_error is not None:
            raise cancellation_error
        if timeout_error is not None:
            raise timeout_error
        return stopped

    async def _release_resources(self) -> None:
        try:
            if self._wifi_lease_active:
                await release_wifi_power_save(self._device)
                self._wifi_lease_active = False
        finally:
            if self._device_lane_acquired:
                await self._device.release_head_target_lane()
                self._device_lane_acquired = False

    async def wait_idle(self) -> None:
        await self._idle_event.wait()

    async def _run_worker(self) -> None:
        try:
            while True:
                await self._pending_event.wait()
                self._pending_event.clear()

                async with self._lock:
                    if self._phase != "running":
                        if self._active_calls == 0:
                            self._idle_event.set()
                            return
                        continue
                    if self._pending is None:
                        if self._active_calls == 0:
                            self._idle_event.set()
                        continue
                    config = self._require_config()
                    last_dispatch = self._last_dispatch_started_at

                if last_dispatch is not None:
                    remaining = (1 / config.rate_hz) - (self._now() - last_dispatch)
                    if remaining > 0:
                        await self._sleep(remaining)

                async with self._lock:
                    if self._phase != "running":
                        if self._active_calls == 0:
                            self._idle_event.set()
                            return
                        continue
                    target = self._pending
                    if target is None:
                        if self._active_calls == 0:
                            self._idle_event.set()
                        continue
                    pending_age_ms = (self._now() - target.accepted_at) * 1000
                    if pending_age_ms > config.max_pending_age_ms:
                        self._pending = None
                        self._pending_age_ms.add(pending_age_ms)
                        self._stale_discarded += 1
                        if self._active_calls == 0:
                            self._idle_event.set()
                        continue
                    planned_pose = self._planned_pose
                    if planned_pose is None:
                        raise RuntimeError("head target lane planned pose is missing")
                    dispatched_pose = self._clamp_from_pose(target, planned_pose)
                    if dispatched_pose == planned_pose:
                        if self._active_calls > 0:
                            continue
                        self._pending = None
                        self._pending_age_ms.add(pending_age_ms)
                        self._no_op_discarded += 1
                        self._idle_event.set()
                        continue
                    self._pending = None
                    self._pending_age_ms.add(pending_age_ms)
                    self._active_calls += 1
                    self._maximum_active_calls = max(
                        self._maximum_active_calls,
                        self._active_calls,
                    )
                    self._dispatched += 1
                    dispatch_started_at = self._now()
                    self._last_dispatch_started_at = dispatch_started_at
                    self._planned_pose = dispatched_pose
                    dispatch = DispatchedHeadTarget(
                        target=target,
                        pose=dispatched_pose,
                        started_at=dispatch_started_at,
                    )
                await self._execute_dispatch(dispatch, config.speed_dps)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                async with self._lock:
                    self._pending = None
                    self._active_calls = 0
                    self._idle_event.set()
                raise
            async with self._lock:
                self._phase = "failed"
                self._pending = None
                self._active_calls = 0
                self._last_error = _bounded_error(exc)
                self._idle_event.set()

    async def _execute_dispatch(
        self,
        dispatch: DispatchedHeadTarget,
        speed_dps: int,
    ) -> None:
        payload: dict[str, Any] | None = None
        error_message: str | None = None
        cancelled = False
        try:
            result, error = await self._device.call_head_target_tool(
                {
                    "yaw": dispatch.pose["yaw"],
                    "pitch": dispatch.pose["pitch"],
                    "speed_dps": speed_dps,
                }
            )
            reply_resolved_at = self._now()
            payload = _require_successful_device_result(result, error)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            reply_resolved_at = self._now()
            error_message = _bounded_error(exc)
        finally:
            async with self._lock:
                self._device_dispatch_latency_ms.add(
                    (self._now() - dispatch.started_at) * 1000
                )
                self._active_calls -= 1
                if cancelled:
                    self._pending_event.set()
                elif error_message is not None:
                    self._failed += 1
                    self._phase = "failed"
                    self._pending = None
                    self._last_error = error_message
                else:
                    self._confirmed += 1
                    self._confirmed_pose = dispatch.pose
                    self._last_confirmed_sequence = dispatch.target.sequence
                    self._successful_reply_latency_ms.add(
                        (reply_resolved_at - dispatch.started_at) * 1000
                    )
                    if payload is not None:
                        self._firmware_mcp_stage_us.add(payload.get("mcpStageUs"))
                self._pending_event.set()

    async def _read_confirmed_pose(self) -> dict[str, int]:
        result, error = await self._device.read_head_target_pose()
        if error:
            raise RuntimeError(_bounded_error(error))
        payload = _decode_payload(result)
        if payload is None:
            raise RuntimeError("get_head_angles returned an invalid result")
        yaw = payload.get("yaw")
        pitch = payload.get("pitch")
        _require_safe_integer(yaw, "seed yaw", SERVO_YAW_MIN, SERVO_YAW_MAX)
        _require_safe_integer(
            pitch,
            "seed pitch",
            SERVO_PITCH_MIN,
            SERVO_PITCH_MAX,
        )
        return {"yaw": yaw, "pitch": pitch}

    def _clamp_from_pose(
        self,
        target: PendingHeadTarget,
        base_pose: dict[str, int],
    ) -> dict[str, int]:
        config = self._require_config()
        return {
            "yaw": _step_clamp(
                target.yaw,
                base_pose["yaw"],
                config.max_step_deg,
            ),
            "pitch": _step_clamp(
                target.pitch,
                base_pose["pitch"],
                config.max_step_deg,
            ),
        }

    def _reset_for_start(
        self,
        *,
        config: HeadTargetLaneConfig,
        lease_id: str,
        confirmed_pose: dict[str, int],
    ) -> None:
        self._config = config
        self._lease_id = lease_id
        self._last_lease_id = None
        self._pending = None
        self._last_dispatch_started_at = None
        self._confirmed_pose = confirmed_pose
        self._planned_pose = confirmed_pose
        self._last_accepted_sequence = None
        self._last_confirmed_sequence = None
        self._accepted = 0
        self._replaced = 0
        self._dispatched = 0
        self._confirmed = 0
        self._failed = 0
        self._stale_discarded = 0
        self._no_op_discarded = 0
        self._active_calls = 0
        self._maximum_active_calls = 0
        self._maximum_pending_depth = 0
        self._post_stop_dispatches = 0
        self._last_error = None
        self._update_acknowledgment_ms = _LatencyHistogram()
        self._pending_age_ms = _LatencyHistogram()
        self._device_dispatch_latency_ms = _LatencyHistogram()
        self._successful_reply_latency_ms = _LatencyHistogram()
        self._firmware_mcp_stage_us = _FirmwareMcpStageMetrics()
        self._pending_event.clear()
        self._idle_event.set()

    def _require_running_lease(self, lease_id: str) -> None:
        self._require_current_lease(lease_id)
        if self._phase != "running":
            raise RuntimeError("head target lane is not running")

    def _require_current_lease(self, lease_id: str | None) -> None:
        if lease_id is not None and lease_id != self._lease_id:
            raise ValueError("head target lane lease ID does not match")

    def _require_stopped_lease(self, lease_id: str | None) -> None:
        if lease_id is not None and lease_id != self._last_lease_id:
            raise ValueError("head target lane lease ID does not match")

    def _require_config(self) -> HeadTargetLaneConfig:
        if self._config is None:
            raise RuntimeError("head target lane config is missing")
        return self._config


def _validate_config(
    *,
    rate_hz: float,
    max_step_deg: float,
    max_pending_age_ms: int,
    speed_dps: int,
) -> HeadTargetLaneConfig:
    _require_finite_number(rate_hz, "rate_hz", 1, 10)
    _require_finite_number(max_step_deg, "max_step_deg", 0, 30, exclusive_min=True)
    _require_safe_integer(
        max_pending_age_ms,
        "max_pending_age_ms",
        50,
        500,
    )
    _require_safe_integer(speed_dps, "speed_dps", 15, 240)
    return HeadTargetLaneConfig(
        rate_hz=float(rate_hz),
        max_step_deg=float(max_step_deg),
        max_pending_age_ms=max_pending_age_ms,
        speed_dps=speed_dps,
    )


def _require_finite_number(
    value: Any,
    name: str,
    minimum: float,
    maximum: float,
    *,
    exclusive_min: bool = False,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be a finite number")
    below_minimum = numeric <= minimum if exclusive_min else numeric < minimum
    if below_minimum or numeric > maximum:
        operator = ">" if exclusive_min else ">="
        raise ValueError(f"{name} must be {operator} {minimum} and <= {maximum}")


def _require_safe_integer(
    value: Any,
    name: str,
    minimum: int,
    maximum: int,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")


def _decode_payload(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if "content" not in result:
        return result
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return None
    first = content[0]
    if not isinstance(first, dict):
        return None
    text = first.get("text")
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _require_successful_device_result(
    result: Any,
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    if error:
        raise RuntimeError(_bounded_error(error))
    if isinstance(result, dict) and result.get("isError"):
        raise RuntimeError("set_head_angles reported isError")
    payload = _decode_payload(result)
    if payload is None:
        raise RuntimeError("set_head_angles returned an invalid result")
    if isinstance(result, dict) and "mcpStageUs" in result:
        payload = {**payload, "mcpStageUs": result["mcpStageUs"]}
    for field in ("isError", "ok", "servo_init_ok", "servo_ok"):
        value = payload.get(field)
        if field == "isError" and value is True:
            raise RuntimeError("set_head_angles payload reported isError")
        if field != "isError" and value is False:
            raise RuntimeError(f"set_head_angles payload reported {field}=false")
    return payload


def _is_bounded_integer(value: Any, *, maximum: int) -> bool:
    return (
        not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= maximum
    )


def _stage_histogram_status(histogram: _LatencyHistogram) -> dict[str, int]:
    status = histogram.status()
    return {
        "count": status["count"],
        "p50": status["p50"],
        "p95": status["p95"],
        "max": status["max"],
    }


def _step_clamp(target: int, confirmed: int, maximum_step: float) -> int:
    delta = max(-maximum_step, min(maximum_step, target - confirmed))
    return int(round(confirmed + delta))


def _bounded_error(error: object) -> str:
    return str(error).replace("\n", " ")[:240]
