from __future__ import annotations

import asyncio
import json

import pytest

from stackchan_mcp.camera_stream import (
    CameraFrameProtocolError,
    CameraStreamService,
    LatestCameraFrameStore,
    parse_camera_frame,
)
from stackchan_mcp import wifi_power_save


JPEG = b"\xff\xd8camera-frame\xff\xd9"


@pytest.fixture(autouse=True)
def clear_wifi_power_save_state() -> None:
    wifi_power_save._clear_for_tests()
    yield
    wifi_power_save._clear_for_tests()


def scl1_frame(
    *,
    sequence: int = 7,
    captured_at_ms: int = 1000,
    encoded_at_ms: int = 1012,
    width: int = 320,
    height: int = 240,
    quality: int = 60,
    device_id: str = "stackchan-test",
    jpeg: bytes = JPEG,
) -> bytes:
    header = json.dumps(
        {
            "frameId": str(sequence),
            "deviceId": device_id,
            "mimeType": "image/jpeg",
            "width": width,
            "height": height,
            "byteLength": len(jpeg),
            "transport": "binary",
            "seq": sequence,
            "captureTimestampMs": captured_at_ms,
            "deviceEncodedAtMs": encoded_at_ms,
            "quality": quality,
        },
        separators=(",", ":"),
    ).encode()
    return b"SCL1" + bytes((1, 0)) + len(header).to_bytes(2, "big") + header + jpeg


def test_parse_camera_frame_returns_none_for_non_scl1_binary() -> None:
    assert parse_camera_frame(b"raw-opus", max_frame_bytes=1024) is None


def test_parse_camera_frame_decodes_scl1_metadata_and_jpeg() -> None:
    frame = parse_camera_frame(
        scl1_frame(),
        max_frame_bytes=1024,
        received_at_ms=1040,
    )

    assert frame is not None
    assert frame.sequence == 7
    assert frame.device_id == "stackchan-test"
    assert frame.captured_at_ms == 1000
    assert frame.encoded_at_ms == 1012
    assert frame.received_at_ms == 1040
    assert frame.width == 320
    assert frame.height == 240
    assert frame.quality == 60
    assert frame.jpeg == JPEG


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"SCL1", "envelope is truncated"),
        (b"SCL1" + bytes((2, 0, 0, 0)), "kind is unsupported"),
        (b"SCL1" + bytes((1, 0, 0, 0)), "header is empty"),
        (
            b"SCL1" + bytes((1, 0, 0, 10)) + b"{}",
            "header exceeds payload size",
        ),
        (
            b"SCL1" + bytes((1, 0, 0, 1)) + b"{",
            "header is not valid JSON",
        ),
        (
            scl1_frame(jpeg=b"not-jpeg"),
            "payload is not a JPEG",
        ),
        (
            scl1_frame(device_id=""),
            "deviceId is invalid",
        ),
    ],
)
def test_parse_camera_frame_rejects_malformed_scl1(
    payload: bytes,
    message: str,
) -> None:
    with pytest.raises(CameraFrameProtocolError, match=message):
        parse_camera_frame(payload, max_frame_bytes=1024)


def test_parse_camera_frame_rejects_oversized_jpeg() -> None:
    with pytest.raises(CameraFrameProtocolError, match="configured byte limit"):
        parse_camera_frame(scl1_frame(), max_frame_bytes=len(JPEG) - 1)


@pytest.mark.asyncio
async def test_latest_store_replaces_only_with_newer_sequence() -> None:
    store = LatestCameraFrameStore()
    first = parse_camera_frame(scl1_frame(sequence=4), max_frame_bytes=1024)
    stale = parse_camera_frame(scl1_frame(sequence=3), max_frame_bytes=1024)
    newest = parse_camera_frame(scl1_frame(sequence=5), max_frame_bytes=1024)
    assert first is not None
    assert stale is not None
    assert newest is not None

    assert await store.publish(first) is True
    assert await store.publish(stale) is False
    assert await store.publish(newest) is True

    latest = await store.wait_for_frame(after_sequence=None, timeout_s=0)
    assert latest is not None
    assert latest.sequence == 5
    assert store.status() == {
        "available": True,
        "sequence": 5,
        "received_frames": 2,
        "replaced_frames": 1,
        "stale_frames": 1,
        "max_jpeg_bytes": len(JPEG),
    }


@pytest.mark.asyncio
async def test_latest_store_waits_for_a_newer_frame_and_clear_discards_bytes() -> None:
    store = LatestCameraFrameStore()
    first = parse_camera_frame(scl1_frame(sequence=11), max_frame_bytes=1024)
    second = parse_camera_frame(scl1_frame(sequence=12), max_frame_bytes=1024)
    assert first is not None
    assert second is not None
    await store.publish(first)

    waiter = asyncio.create_task(
        store.wait_for_frame(after_sequence=11, timeout_s=0.25)
    )
    await asyncio.sleep(0)
    assert not waiter.done()

    await store.publish(second)
    waited = await waiter
    assert waited is not None
    assert waited.sequence == second.sequence
    assert waited.gateway_sequence == 12

    await store.clear()
    assert await store.wait_for_frame(after_sequence=None, timeout_s=0) is None
    assert store.status()["available"] is False


@pytest.mark.asyncio
async def test_latest_store_cursor_remains_monotonic_across_stream_restart() -> None:
    store = LatestCameraFrameStore()
    before_restart = parse_camera_frame(
        scl1_frame(sequence=40),
        max_frame_bytes=1024,
    )
    after_restart = parse_camera_frame(
        scl1_frame(sequence=1),
        max_frame_bytes=1024,
    )
    assert before_restart is not None
    assert after_restart is not None

    assert await store.publish(before_restart) is True
    first = await store.wait_for_frame(after_sequence=None, timeout_s=0)
    assert first is not None
    first_cursor = first.gateway_sequence

    await store.clear()
    assert await store.publish(after_restart) is True
    restarted = await store.wait_for_frame(
        after_sequence=first_cursor,
        timeout_s=0,
    )

    assert restarted is not None
    assert restarted.sequence == 1
    assert restarted.gateway_sequence == first_cursor + 1


@pytest.mark.asyncio
async def test_latest_store_timeout_is_bounded() -> None:
    store = LatestCameraFrameStore()

    assert (
        await store.wait_for_frame(after_sequence=99, timeout_s=0.001)
        is None
    )


class RecordingCameraDevice:
    def __init__(
        self,
        *,
        supported: bool = True,
        tool_error: dict[str, object] | None = None,
        raise_on_tool: str | None = None,
    ) -> None:
        self.supports_camera_stream = supported
        self.tool_error = tool_error
        self.raise_on_tool = raise_on_tool
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.datagram_events: list[str] = []
        self.raise_on_datagram_begin = False
        self.datagram_status = {
            "ready": True,
            "pending": False,
            "completed_frames": 3,
            "replaced_incomplete_frames": 1,
            "stale_chunks": 2,
            "expired_frames": 0,
            "invalid_frames": 0,
            "source_mismatch_packets": 0,
        }

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        self.calls.append((name, arguments))
        if name == self.raise_on_tool:
            raise RuntimeError(f"{name} transport failed")
        if name == "self.wifi.set_power_save":
            mode = arguments["mode"]
            return {
                "ok": True,
                "previous": "min_modem" if mode == "none" else "none",
                "current": mode,
            }, None
        return {"content": []}, self.tool_error

    async def begin_camera_datagram_stream(self) -> None:
        self.datagram_events.append("begin")
        if self.raise_on_datagram_begin:
            raise ConnectionError("camera datagram transport failed")

    async def end_camera_datagram_stream(self) -> None:
        self.datagram_events.append("end")

    def camera_datagram_status(self) -> dict[str, int | bool]:
        return dict(self.datagram_status)


@pytest.mark.asyncio
async def test_stream_service_starts_once_and_stops_after_last_release() -> None:
    device = RecordingCameraDevice()
    store = LatestCameraFrameStore()
    service = CameraStreamService(device, store)

    first = await service.acquire(fps=20, quality=60)
    second = await service.acquire(fps=20, quality=60)

    assert first["subscribers"] == 1
    assert second["subscribers"] == 2
    assert service.can_accept_frames() is True
    assert device.calls == [
        ("self.wifi.set_power_save", {"mode": "none"}),
        ("self.camera.start_stream", {"fps": 20, "quality": 60})
    ]
    assert device.datagram_events == ["begin"]
    assert service.status()["datagram"] == device.datagram_status

    frame = parse_camera_frame(scl1_frame(sequence=22), max_frame_bytes=1024)
    assert frame is not None
    await store.publish(frame)

    still_running = await service.release()
    assert still_running["running"] is True
    assert still_running["subscribers"] == 1
    assert store.status()["available"] is True

    stopped = await service.release()
    assert stopped["running"] is False
    assert stopped["subscribers"] == 0
    assert service.can_accept_frames() is False
    assert device.datagram_events == ["begin", "end"]
    assert device.calls[-2:] == [
        ("self.camera.stop_stream", {}),
        ("self.wifi.set_power_save", {"mode": "min_modem"}),
    ]
    assert store.status()["available"] is False


@pytest.mark.asyncio
async def test_stream_service_cleans_up_when_datagram_lease_start_fails() -> None:
    device = RecordingCameraDevice()
    device.raise_on_datagram_begin = True
    service = CameraStreamService(device, LatestCameraFrameStore())

    with pytest.raises(ConnectionError, match="camera datagram transport failed"):
        await service.acquire(fps=20, quality=60)

    assert service.status()["subscribers"] == 0
    assert device.datagram_events == ["begin", "end"]
    assert device.calls[-2:] == [
        ("self.camera.stop_stream", {}),
        ("self.wifi.set_power_save", {"mode": "min_modem"}),
    ]


@pytest.mark.asyncio
async def test_stream_service_rejects_conflicting_or_invalid_configuration() -> None:
    service = CameraStreamService(
        RecordingCameraDevice(),
        LatestCameraFrameStore(),
    )

    for fps, quality, message in (
        (0, 60, "fps must be an integer in 1..20"),
        (21, 60, "fps must be an integer in 1..20"),
        (20, 0, "quality must be an integer in 1..100"),
        (20, 101, "quality must be an integer in 1..100"),
    ):
        with pytest.raises(ValueError, match=message):
            await service.acquire(fps=fps, quality=quality)

    await service.acquire(fps=20, quality=60)
    with pytest.raises(RuntimeError, match="different configuration"):
        await service.acquire(fps=15, quality=60)


@pytest.mark.asyncio
async def test_stream_service_requires_device_feature_and_propagates_tool_error() -> None:
    unsupported = CameraStreamService(
        RecordingCameraDevice(supported=False),
        LatestCameraFrameStore(),
    )
    with pytest.raises(RuntimeError, match="does not advertise camera streaming"):
        await unsupported.acquire(fps=20, quality=60)

    rejected_device = RecordingCameraDevice(
        tool_error={"code": -32000, "message": "camera unavailable"}
    )
    rejected = CameraStreamService(rejected_device, LatestCameraFrameStore())
    with pytest.raises(RuntimeError, match="camera unavailable"):
        await rejected.acquire(fps=20, quality=60)
    assert rejected.status()["subscribers"] == 0
    assert rejected_device.calls == [
        ("self.wifi.set_power_save", {"mode": "none"}),
        ("self.camera.start_stream", {"fps": 20, "quality": 60}),
        ("self.camera.stop_stream", {}),
        ("self.wifi.set_power_save", {"mode": "min_modem"}),
    ]


@pytest.mark.asyncio
async def test_stream_service_restores_wifi_when_start_transport_raises() -> None:
    device = RecordingCameraDevice(raise_on_tool="self.camera.start_stream")
    service = CameraStreamService(device, LatestCameraFrameStore())

    with pytest.raises(RuntimeError, match="start_stream transport failed"):
        await service.acquire(fps=20, quality=60)

    assert service.status()["subscribers"] == 0
    assert device.calls == [
        ("self.wifi.set_power_save", {"mode": "none"}),
        ("self.camera.start_stream", {"fps": 20, "quality": 60}),
        ("self.camera.stop_stream", {}),
        ("self.wifi.set_power_save", {"mode": "min_modem"}),
    ]


@pytest.mark.asyncio
async def test_stream_service_clears_frame_and_restores_wifi_when_stop_raises() -> None:
    device = RecordingCameraDevice()
    store = LatestCameraFrameStore()
    service = CameraStreamService(device, store)
    await service.acquire(fps=20, quality=60)
    frame = parse_camera_frame(scl1_frame(sequence=33), max_frame_bytes=1024)
    assert frame is not None
    await store.publish(frame)
    device.raise_on_tool = "self.camera.stop_stream"

    with pytest.raises(RuntimeError, match="stop_stream transport failed"):
        await service.release()

    assert service.status()["subscribers"] == 0
    assert service.can_accept_frames() is False
    assert store.status()["available"] is False
    assert device.calls[-2:] == [
        ("self.camera.stop_stream", {}),
        ("self.wifi.set_power_save", {"mode": "min_modem"}),
    ]

    device.raise_on_tool = None
    retried = await service.release()

    assert retried["running"] is False
    assert device.calls[-1] == ("self.camera.stop_stream", {})


@pytest.mark.asyncio
async def test_stream_service_stop_all_clears_frame_when_stop_raises() -> None:
    device = RecordingCameraDevice()
    store = LatestCameraFrameStore()
    service = CameraStreamService(device, store)
    await service.acquire(fps=20, quality=60)
    frame = parse_camera_frame(scl1_frame(sequence=34), max_frame_bytes=1024)
    assert frame is not None
    await store.publish(frame)
    device.raise_on_tool = "self.camera.stop_stream"

    with pytest.raises(RuntimeError, match="stop_stream transport failed"):
        await service.stop_all()

    assert service.status()["subscribers"] == 0
    assert store.status()["available"] is False
    assert device.calls[-2:] == [
        ("self.camera.stop_stream", {}),
        ("self.wifi.set_power_save", {"mode": "min_modem"}),
    ]


@pytest.mark.asyncio
async def test_stream_service_restarts_active_subscription_after_reconnect() -> None:
    device = RecordingCameraDevice()
    store = LatestCameraFrameStore()
    service = CameraStreamService(device, store)
    await service.acquire(fps=20, quality=60)
    frame = parse_camera_frame(scl1_frame(sequence=35), max_frame_bytes=1024)
    assert frame is not None
    await store.publish(frame)

    await service.on_device_disconnected()

    assert service.status()["subscribers"] == 1
    assert service.status()["running"] is True
    assert service.can_accept_frames() is False
    assert store.status()["available"] is False
    assert device.calls[-1] == (
        "self.wifi.set_power_save",
        {"mode": "min_modem"},
    )

    await service.on_device_ready()

    assert service.can_accept_frames() is True
    assert device.calls[-3:] == [
        ("self.camera.stop_stream", {}),
        ("self.wifi.set_power_save", {"mode": "none"}),
        ("self.camera.start_stream", {"fps": 20, "quality": 60}),
    ]
    assert device.datagram_events == ["begin", "end", "begin"]


@pytest.mark.asyncio
async def test_stream_service_idle_lease_expires_and_clears_latest_frame() -> None:
    device = RecordingCameraDevice()
    store = LatestCameraFrameStore()
    service = CameraStreamService(device, store, idle_timeout_s=0.02)
    await service.acquire(fps=20, quality=60)
    frame = parse_camera_frame(scl1_frame(sequence=36), max_frame_bytes=1024)
    assert frame is not None
    await store.publish(frame)

    await asyncio.sleep(0.01)
    service.touch()
    await asyncio.sleep(0.015)
    assert service.status()["running"] is True

    for _ in range(20):
        if service.status()["running"] is False:
            break
        await asyncio.sleep(0.005)

    assert service.status()["running"] is False
    assert service.status()["subscribers"] == 0
    assert service.can_accept_frames() is False
    assert store.status()["available"] is False
    assert device.calls[-2:] == [
        ("self.camera.stop_stream", {}),
        ("self.wifi.set_power_save", {"mode": "min_modem"}),
    ]


@pytest.mark.asyncio
async def test_stream_service_release_is_idempotent_at_zero() -> None:
    device = RecordingCameraDevice()
    service = CameraStreamService(device, LatestCameraFrameStore())

    status = await service.release()

    assert status["subscribers"] == 0
    assert status["running"] is False
    assert device.calls == []
