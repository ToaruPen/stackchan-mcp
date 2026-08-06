"""Bounded in-memory camera stream primitives.

StackChan camera frames use authenticated latest-only datagrams, isolated from
MCP control and raw Opus audio. Camera payloads retain the binary ``SCL1``
envelope for bounded validation and device identity checks at the gateway.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import logging
import time
from typing import Any, Protocol

from .camera_metrics import BoundedLatencyHistogram
from .wifi_power_save import acquire_wifi_power_save, release_wifi_power_save


_SCL1_MAGIC = b"SCL1"
_CAMERA_FRAME_KIND = 1
_HEADER_OFFSET = 8
CAMERA_STREAM_IDLE_TIMEOUT_S = 30.0
logger = logging.getLogger(__name__)


class CameraFrameProtocolError(ValueError):
    """Raised when a payload claims to be SCL1 but violates its contract."""


@dataclass(frozen=True, slots=True)
class CameraFrame:
    """One validated JPEG frame and its device/gateway timing metadata."""

    sequence: int
    device_id: str
    captured_at_ms: int
    encoded_at_ms: int
    received_at_ms: int
    width: int
    height: int
    quality: int
    jpeg: bytes
    capture_wait_us: int | None = None
    encode_us: int | None = None
    received_monotonic_ms: int | None = None
    gateway_sequence: int = 0


def parse_camera_frame(
    payload: bytes,
    *,
    max_frame_bytes: int,
    received_at_ms: int | None = None,
    received_monotonic_ms: int | None = None,
) -> CameraFrame | None:
    """Parse one SCL1 camera frame, returning ``None`` for non-camera binary."""
    if not payload.startswith(_SCL1_MAGIC):
        return None
    if len(payload) < _HEADER_OFFSET:
        raise CameraFrameProtocolError("SCL1 envelope is truncated")

    kind = payload[4]
    if kind != _CAMERA_FRAME_KIND:
        raise CameraFrameProtocolError("SCL1 kind is unsupported")
    if payload[5] != 0:
        raise CameraFrameProtocolError("SCL1 reserved byte must be zero")

    header_length = int.from_bytes(payload[6:8], "big")
    if header_length == 0:
        raise CameraFrameProtocolError("SCL1 header is empty")
    header_end = _HEADER_OFFSET + header_length
    if header_end > len(payload):
        raise CameraFrameProtocolError("SCL1 header exceeds payload size")

    try:
        header_value = json.loads(payload[_HEADER_OFFSET:header_end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CameraFrameProtocolError("SCL1 header is not valid JSON") from exc
    if not isinstance(header_value, dict):
        raise CameraFrameProtocolError("SCL1 header must be an object")

    jpeg = payload[header_end:]
    if len(jpeg) > max_frame_bytes:
        raise CameraFrameProtocolError(
            "SCL1 JPEG exceeds the configured byte limit"
        )
    if len(jpeg) < 4 or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(
        b"\xff\xd9"
    ):
        raise CameraFrameProtocolError("SCL1 payload is not a JPEG")

    sequence = _required_integer(header_value, "seq", minimum=0)
    device_id = header_value.get("deviceId")
    if not isinstance(device_id, str) or not device_id:
        raise CameraFrameProtocolError("SCL1 deviceId is invalid")
    captured_at_ms = _required_integer(
        header_value,
        "captureTimestampMs",
        minimum=0,
    )
    encoded_at_ms = _required_integer(
        header_value,
        "deviceEncodedAtMs",
        minimum=captured_at_ms,
    )
    capture_wait_us = _optional_integer(
        header_value,
        "deviceCaptureWaitUs",
        minimum=0,
    )
    encode_us = _optional_integer(
        header_value,
        "deviceEncodeUs",
        minimum=0,
    )
    width = _required_integer(header_value, "width", minimum=1)
    height = _required_integer(header_value, "height", minimum=1)
    quality = _required_integer(header_value, "quality", minimum=1, maximum=100)
    byte_length = _required_integer(header_value, "byteLength", minimum=1)
    if byte_length != len(jpeg):
        raise CameraFrameProtocolError("SCL1 JPEG byteLength does not match payload")
    if header_value.get("mimeType") != "image/jpeg":
        raise CameraFrameProtocolError("SCL1 mimeType is unsupported")
    if header_value.get("transport") != "binary":
        raise CameraFrameProtocolError("SCL1 transport is unsupported")

    return CameraFrame(
        sequence=sequence,
        device_id=device_id,
        captured_at_ms=captured_at_ms,
        encoded_at_ms=encoded_at_ms,
        received_at_ms=(
            received_at_ms
            if received_at_ms is not None
            else time.time_ns() // 1_000_000
        ),
        width=width,
        height=height,
        quality=quality,
        jpeg=jpeg,
        capture_wait_us=capture_wait_us,
        encode_us=encode_us,
        received_monotonic_ms=(
            received_monotonic_ms
            if received_monotonic_ms is not None
            else time.monotonic_ns() // 1_000_000
        ),
    )


def _required_integer(
    values: dict[str, Any],
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CameraFrameProtocolError(f"SCL1 {name} is invalid")
    if maximum is not None and value > maximum:
        raise CameraFrameProtocolError(f"SCL1 {name} is invalid")
    return value


def _optional_integer(
    values: dict[str, Any],
    name: str,
    *,
    minimum: int,
) -> int | None:
    if name not in values:
        return None
    return _required_integer(values, name, minimum=minimum)


class LatestCameraFrameStore:
    """Keep only the newest camera frame and bounded aggregate counters."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._latest: CameraFrame | None = None
        self._latest_device_sequence: int | None = None
        self._last_gateway_sequence = -1
        self._received_frames = 0
        self._replaced_frames = 0
        self._stale_frames = 0
        self._max_jpeg_bytes = 0
        self._previous_captured_at_ms: int | None = None
        self._previous_received_monotonic_ms: int | None = None
        self._device_capture_interval_ms = BoundedLatencyHistogram(
            maximum_bucket=30_000
        )
        self._device_capture_wait_us = BoundedLatencyHistogram(
            maximum_bucket=10_000_000
        )
        self._device_encode_us = BoundedLatencyHistogram(
            maximum_bucket=10_000_000
        )
        self._gateway_receive_interval_ms = BoundedLatencyHistogram(
            maximum_bucket=30_000
        )
        self._latest_wait_ms = BoundedLatencyHistogram(maximum_bucket=30_000)
        self._wait_calls = 0
        self._immediate_deliveries = 0
        self._waited_deliveries = 0
        self._wait_timeouts = 0

    async def publish(self, frame: CameraFrame) -> bool:
        """Adopt ``frame`` when it is newer than the current generation."""
        async with self._condition:
            if (
                self._latest_device_sequence is not None
                and frame.sequence <= self._latest_device_sequence
            ):
                self._stale_frames += 1
                return False
            if self._latest is not None:
                self._replaced_frames += 1
            self._last_gateway_sequence = max(
                self._last_gateway_sequence + 1,
                frame.sequence,
            )
            self._latest = replace(
                frame,
                gateway_sequence=self._last_gateway_sequence,
            )
            self._latest_device_sequence = frame.sequence
            self._received_frames += 1
            self._max_jpeg_bytes = max(self._max_jpeg_bytes, len(frame.jpeg))
            if (
                self._previous_captured_at_ms is not None
                and frame.captured_at_ms >= self._previous_captured_at_ms
            ):
                self._device_capture_interval_ms.add(
                    frame.captured_at_ms - self._previous_captured_at_ms
                )
            if (
                self._previous_received_monotonic_ms is not None
                and frame.received_monotonic_ms is not None
                and frame.received_monotonic_ms
                >= self._previous_received_monotonic_ms
            ):
                self._gateway_receive_interval_ms.add(
                    frame.received_monotonic_ms
                    - self._previous_received_monotonic_ms
                )
            self._previous_captured_at_ms = frame.captured_at_ms
            self._previous_received_monotonic_ms = frame.received_monotonic_ms
            if frame.capture_wait_us is not None:
                self._device_capture_wait_us.add(frame.capture_wait_us)
            if frame.encode_us is not None:
                self._device_encode_us.add(frame.encode_us)
            self._condition.notify_all()
            return True

    async def wait_for_frame(
        self,
        *,
        after_sequence: int | None,
        timeout_s: float,
    ) -> CameraFrame | None:
        """Return a newer frame immediately or after a bounded wait."""
        started_at = time.monotonic()
        async with self._condition:
            self._wait_calls += 1
            frame = self._matching_frame(after_sequence)
            if frame is not None:
                self._immediate_deliveries += 1
                self._latest_wait_ms.add((time.monotonic() - started_at) * 1_000)
                return frame
            if timeout_s <= 0:
                self._wait_timeouts += 1
                self._latest_wait_ms.add((time.monotonic() - started_at) * 1_000)
                return None
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: self._matching_frame(after_sequence) is not None
                    ),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                self._wait_timeouts += 1
                self._latest_wait_ms.add((time.monotonic() - started_at) * 1_000)
                return None
            frame = self._matching_frame(after_sequence)
            if frame is not None:
                self._waited_deliveries += 1
            else:
                self._wait_timeouts += 1
            self._latest_wait_ms.add((time.monotonic() - started_at) * 1_000)
            return frame

    async def clear(self) -> None:
        """Discard the retained JPEG while preserving aggregate counters."""
        async with self._condition:
            self._latest = None
            self._latest_device_sequence = None
            self._previous_captured_at_ms = None
            self._previous_received_monotonic_ms = None
            self._condition.notify_all()

    def status(self) -> dict[str, Any]:
        """Return image-free status suitable for diagnostics."""
        latest = self._latest
        return {
            "available": latest is not None,
            "sequence": None if latest is None else latest.gateway_sequence,
            "received_frames": self._received_frames,
            "replaced_frames": self._replaced_frames,
            "stale_frames": self._stale_frames,
            "max_jpeg_bytes": self._max_jpeg_bytes,
            "timing": {
                "device_capture_interval_ms": (
                    self._device_capture_interval_ms.status()
                ),
                "device_capture_wait_us": self._device_capture_wait_us.status(),
                "device_encode_us": self._device_encode_us.status(),
                "gateway_receive_interval_ms": (
                    self._gateway_receive_interval_ms.status()
                ),
                "latest_wait_ms": self._latest_wait_ms.status(),
            },
            "wait": {
                "calls": self._wait_calls,
                "immediate_deliveries": self._immediate_deliveries,
                "waited_deliveries": self._waited_deliveries,
                "timeouts": self._wait_timeouts,
            },
        }

    def _matching_frame(self, after_sequence: int | None) -> CameraFrame | None:
        latest = self._latest
        if latest is None:
            return None
        if (
            after_sequence is not None
            and latest.gateway_sequence <= after_sequence
        ):
            return None
        return latest


class CameraStreamDevice(Protocol):
    """Minimal device boundary required by the stream lifecycle."""

    supports_camera_stream: bool

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> tuple[Any, dict[str, Any] | None]: ...

    async def begin_camera_datagram_stream(self) -> None: ...

    async def end_camera_datagram_stream(self) -> None: ...

    def camera_datagram_status(self) -> dict[str, Any]: ...


class CameraStreamService:
    """Reference-count one physical stream across concurrent local consumers."""

    def __init__(
        self,
        device: CameraStreamDevice,
        frames: LatestCameraFrameStore,
        *,
        idle_timeout_s: float = CAMERA_STREAM_IDLE_TIMEOUT_S,
    ) -> None:
        if idle_timeout_s <= 0:
            raise ValueError("camera stream idle timeout must be positive")
        self._device = device
        self._frames = frames
        self._idle_timeout_s = idle_timeout_s
        self._lock = asyncio.Lock()
        self._subscribers = 0
        self._fps: int | None = None
        self._quality: int | None = None
        self._physical_running = False
        self._stop_pending = False
        self._wifi_lease_active = False
        self._idle_generation = 0
        self._idle_expiry_handle: asyncio.TimerHandle | None = None
        self._datagram_lease_active = False

    async def acquire(self, *, fps: int, quality: int) -> dict[str, Any]:
        _require_stream_integer(fps, "fps", 1, 20)
        _require_stream_integer(quality, "quality", 1, 100)

        async with self._lock:
            if self._subscribers > 0:
                if fps != self._fps or quality != self._quality:
                    raise RuntimeError(
                        "camera stream is already running with a different configuration"
                    )
                self._subscribers += 1
                self.touch()
                return self.status()

            if self._stop_pending:
                try:
                    await self._stop_physical_stream()
                except BaseException as exc:
                    raise RuntimeError(
                        "previous camera stream stop is still pending"
                    ) from exc

            if not self._device.supports_camera_stream:
                raise RuntimeError("device does not advertise camera streaming")

            await self._start_physical_stream(fps=fps, quality=quality)
            self._subscribers = 1
            self._fps = fps
            self._quality = quality
            self.touch()
            return self.status()

    async def release(self) -> dict[str, Any]:
        async with self._lock:
            if self._subscribers == 0:
                if self._stop_pending:
                    await self._stop_physical_stream()
                return self.status()
            self._subscribers -= 1
            if self._subscribers > 0:
                self.touch()
                return self.status()

            self._fps = None
            self._quality = None
            self._cancel_idle_expiry()
            await self._stop_physical_stream()
            return self.status()

    async def stop_all(self) -> None:
        """Stop the producer and discard retained image bytes."""
        async with self._lock:
            was_running = self._subscribers > 0
            self._subscribers = 0
            self._fps = None
            self._quality = None
            self._cancel_idle_expiry()
            if was_running or self._stop_pending:
                await self._stop_physical_stream()
            else:
                await self._frames.clear()

    async def on_device_disconnected(self) -> None:
        """Discard the old session's frame while preserving logical leases."""
        async with self._lock:
            self._physical_running = False
            if self._datagram_lease_active:
                try:
                    await self._device.end_camera_datagram_stream()
                finally:
                    self._datagram_lease_active = False
            await self._frames.clear()
            if self._wifi_lease_active:
                await release_wifi_power_save(self._device)
                self._wifi_lease_active = False

    async def on_device_ready(self) -> None:
        """Re-establish a subscribed physical stream after device reconnect."""
        async with self._lock:
            if self._stop_pending:
                await self._stop_physical_stream()
            if self._subscribers == 0:
                return
            if self._fps is None or self._quality is None:
                raise RuntimeError("camera stream subscription configuration is missing")
            await self._start_physical_stream(
                fps=self._fps,
                quality=self._quality,
            )
            self.touch()

    def can_accept_frames(self) -> bool:
        """Return whether the active device may publish completed frames."""
        return self._physical_running and self._subscribers > 0

    def touch(self) -> None:
        """Renew the bounded logical lease after authenticated client activity."""
        if self._subscribers == 0:
            return
        self._idle_generation += 1
        generation = self._idle_generation
        if self._idle_expiry_handle is not None:
            self._idle_expiry_handle.cancel()
        loop = asyncio.get_running_loop()
        self._idle_expiry_handle = loop.call_later(
            self._idle_timeout_s,
            lambda: self._schedule_idle_expiry(generation),
        )

    def _cancel_idle_expiry(self) -> None:
        self._idle_generation += 1
        if self._idle_expiry_handle is not None:
            self._idle_expiry_handle.cancel()
            self._idle_expiry_handle = None

    def _schedule_idle_expiry(self, generation: int) -> None:
        if generation != self._idle_generation:
            return
        self._idle_expiry_handle = None
        asyncio.create_task(self._expire_idle_lease(generation))

    async def _expire_idle_lease(self, generation: int) -> None:
        async with self._lock:
            if generation != self._idle_generation or self._subscribers == 0:
                return
            self._subscribers = 0
            self._fps = None
            self._quality = None
            self._cancel_idle_expiry()
            try:
                await self._stop_physical_stream()
            except BaseException as exc:
                logger.warning("camera stream idle lease cleanup failed: %s", exc)

    async def _start_physical_stream(self, *, fps: int, quality: int) -> None:
        await acquire_wifi_power_save(self._device)
        self._wifi_lease_active = True
        self._stop_pending = True
        try:
            _result, error = await self._device.call_tool(
                "self.camera.start_stream",
                {"fps": fps, "quality": quality},
            )
            if error:
                raise RuntimeError(
                    str(error.get("message", "camera stream start failed"))
                )
            self._datagram_lease_active = True
            await self._device.begin_camera_datagram_stream()
            self._physical_running = True
        except BaseException:
            self._physical_running = False
            try:
                await self._stop_physical_stream()
            except BaseException as cleanup_error:
                logger.warning(
                    "camera stream cleanup after failed start failed: %s",
                    cleanup_error,
                )
            raise

    async def _stop_physical_stream(self) -> None:
        """Attempt every cleanup action and report the first failure."""
        self._physical_running = False
        failures: list[BaseException] = []
        if self._datagram_lease_active:
            try:
                await self._device.end_camera_datagram_stream()
            except BaseException as exc:
                failures.append(exc)
            finally:
                self._datagram_lease_active = False
        if self._stop_pending:
            try:
                _result, error = await self._device.call_tool(
                    "self.camera.stop_stream",
                    {},
                )
                if error:
                    failures.append(
                        RuntimeError(
                            str(error.get("message", "camera stream stop failed"))
                        )
                    )
                else:
                    self._stop_pending = False
            except BaseException as exc:
                failures.append(exc)

        try:
            await self._frames.clear()
        except BaseException as exc:
            failures.append(exc)

        if self._wifi_lease_active:
            try:
                await release_wifi_power_save(self._device)
            except BaseException as exc:
                failures.append(exc)
            finally:
                self._wifi_lease_active = False

        if failures:
            for secondary in failures[1:]:
                logger.warning(
                    "secondary camera stream cleanup failure: %s",
                    secondary,
                )
            raise failures[0]

    def status(self) -> dict[str, Any]:
        return {
            "running": self._subscribers > 0,
            "subscribers": self._subscribers,
            "fps": self._fps,
            "quality": self._quality,
            "physical_running": self._physical_running,
            "cleanup_pending": self._stop_pending,
            "idle_timeout_s": self._idle_timeout_s,
            "datagram_lease_active": self._datagram_lease_active,
            "datagram": self._device.camera_datagram_status(),
            "frame": self._frames.status(),
        }


def _require_stream_integer(
    value: int,
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
        raise ValueError(
            f"{name} must be an integer in {minimum}..{maximum}"
        )
