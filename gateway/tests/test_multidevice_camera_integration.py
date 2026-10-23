"""Keep the default-device camera and head services isolated after routing changes."""

import asyncio
import json
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import websockets

from stackchan_mcp.camera_datagram import encode_hello

from stackchan_mcp.esp32_client import ESP32Connection, ESP32Manager
from stackchan_mcp.stdio_server import _dispatch_mcp_tool


def connected_device(device_id):
    connection = ESP32Connection(AsyncMock(), f"session-{device_id}")
    connection._initialized = True
    connection.device_id = device_id
    connection.call_tool = AsyncMock(return_value=({
        "content": [{"type": "text", "text": json.dumps({
            "ok": True, "yaw": 0, "pitch": 33,
        })}],
    }, None))
    return connection


@pytest.mark.asyncio
async def test_head_lane_reserves_only_its_device_and_blocks_default_switch():
    manager = ESP32Manager()
    first, second = connected_device("first"), connected_device("second")
    manager._register_connection(first, first.device_id)
    manager._register_connection(second, second.device_id)
    await manager.head_target_lane.start(
        rate_hz=10, max_step_deg=4, max_pending_age_ms=180, speed_dps=90,
    )
    try:
        assert first._tool_lane_locks["servo"].locked()
        assert not second._tool_lane_locks["servo"].locked()
        assert manager.set_default_device("second")["ok"] is False
        assert manager.default_device_id == "first"
        _, error = await asyncio.wait_for(manager.call_tool(
            "self.robot.get_head_angles", {"cached_motion_state": True},
        ), timeout=1)
        assert error is None
        _, error = await asyncio.wait_for(manager.call_tool(
            "self.robot.set_head_angles", {"yaw": 5, "pitch": 33}, device_id="second",
        ), timeout=1)
        assert error is None
        second.call_tool.assert_awaited_once()
    finally:
        await manager.head_target_lane.stop()
    assert not first._tool_lane_locks["servo"].locked()
    assert manager.set_default_device("second")["ok"] is True


@pytest.mark.asyncio
async def test_head_lane_releases_original_connection_after_default_disconnect():
    manager = ESP32Manager()
    first, second = connected_device("first"), connected_device("second")
    manager._register_connection(first, first.device_id)
    manager._register_connection(second, second.device_id)
    await manager.acquire_head_target_lane()
    first.disconnect()
    manager._unregister_connection(first, first.device_id)
    assert manager.default_device_id == "second"
    await manager.release_head_target_lane()
    assert not first._tool_lane_locks["servo"].locked()
    assert not second._tool_lane_locks["servo"].locked()


@pytest.mark.parametrize("running,cleanup_pending", [(True, False), (False, True)])
def test_default_switch_waits_for_camera_release(monkeypatch, running, cleanup_pending):
    manager = ESP32Manager()
    first, second = connected_device("first"), connected_device("second")
    manager._register_connection(first, first.device_id)
    manager._register_connection(second, second.device_id)
    monkeypatch.setattr(manager.camera_stream, "status", lambda: {
        "running": running, "cleanup_pending": cleanup_pending,
    })
    assert manager.set_default_device("second")["ok"] is False
    assert manager.default_device_id == "first"
    assert manager.set_default_device("first")["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [
    "camera_stream", "stackchan_face_follow", "stackchan_head_target_lane",
])
async def test_default_only_tools_reject_other_device(tool):
    gateway = SimpleNamespace(esp32=SimpleNamespace(default_device_id="first"))
    result = await _dispatch_mcp_tool(
        tool, {"action": "start", "device_id": "second"}, gateway,
    )
    assert json.loads(result[0].text) == {
        "error": "This tool operates on the default device only",
    }


@pytest.mark.asyncio
async def test_disconnected_default_does_not_route_stream_cleanup_to_other_device():
    manager = ESP32Manager()
    first, second = connected_device("first"), connected_device("second")
    manager._register_connection(first, first.device_id)
    manager._register_connection(second, second.device_id)
    first.disconnect()
    _, error = await manager.call_tool("self.wifi.set_power_save", {"mode": "min_modem"})
    assert error is not None
    second.call_tool.assert_not_awaited()


@pytest_asyncio.fixture
async def media_devices(monkeypatch):
    manager = ESP32Manager()

    async def initialize(connection, device_id):
        connection._initialized = True
        connection._tools_discovered = True
        await manager._ensure_camera_stream_ready(connection)

    monkeypatch.setattr(manager, "_init_device", initialize)
    await manager.start("127.0.0.1", 0)
    port = manager._server.sockets[0].getsockname()[1]
    async with AsyncExitStack() as stack:
        stack.push_async_callback(manager.stop)
        transports = []

        async def connect(device_id):
            control = await stack.enter_async_context(websockets.connect(
                f"ws://127.0.0.1:{port}",
                additional_headers={"Device-Id": device_id},
            ))
            await control.send(json.dumps({
                "type": "hello", "features": {
                    "mcp": True, "camera_stream": True, "camera_datagram_v1": True,
                },
            }))
            assert json.loads(await asyncio.wait_for(control.recv(), 1))["type"] == "hello"
            media = await stack.enter_async_context(websockets.connect(
                f"ws://127.0.0.1:{port}",
                additional_headers={"Device-Id": device_id, "Camera-Stream": "1"},
            ))
            config = json.loads(await asyncio.wait_for(media.recv(), 1))
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                asyncio.DatagramProtocol,
                remote_addr=("127.0.0.1", config["port"]),
            )
            transports.append(transport)
            token = bytes.fromhex(config["token"])
            transport.sendto(encode_hello(token))
            session = manager._camera_datagram_sessions[token]
            await session.wait_ready(timeout_s=1)
            return control, media, session

        try:
            yield manager, connect
        finally:
            for transport in transports:
                transport.close()


@pytest.mark.asyncio
async def test_two_real_media_handshakes_and_default_selection(media_devices, monkeypatch):
    manager, connect = media_devices
    first_control, first_media, first_session = await connect("first")
    second_control, second_media, second_session = await connect("second")
    for socket in (first_control, first_media, second_control, second_media):
        await asyncio.wait_for(await socket.ping(), 1)
    assert {device["device_id"] for device in manager.list_devices()} == {"first", "second"}
    assert manager.supports_camera_stream
    assert manager._camera_connection.datagram_session is first_session
    assert manager.set_default_device("second")["ok"]
    assert manager._camera_connection.datagram_session is second_session
    assert manager.supports_camera_stream

    jpeg = b"\xff\xd8frame\xff\xd9"

    def envelope(device_id):
        header = json.dumps({
            "frameId": "1", "deviceId": device_id, "mimeType": "image/jpeg",
            "width": 320, "height": 240, "byteLength": len(jpeg),
            "transport": "binary", "seq": 1, "captureTimestampMs": 1000,
            "deviceEncodedAtMs": 1010, "quality": 60,
        }).encode()
        return b"SCL1" + bytes((1, 0)) + len(header).to_bytes(2, "big") + header + jpeg

    monkeypatch.setattr(manager.camera_stream, "can_accept_frames", lambda: True)
    await manager._handle_camera_datagram_frame(envelope("first"), session=first_session)
    assert not manager.camera_frames.status()["available"]
    await manager._handle_camera_datagram_frame(envelope("second"), session=second_session)
    frame = await manager.camera_frames.wait_for_frame(after_sequence=None, timeout_s=0)
    assert frame.device_id == "second"


@pytest.mark.asyncio
async def test_nondefault_replacement_and_departure_preserve_selected_media(media_devices, monkeypatch):
    manager, connect = media_devices
    first_control, first_media, first_session = await connect("first")
    old_control, old_media, old_session = await connect("second")
    await manager.begin_camera_datagram_stream()
    credit_task = manager._camera_credit_task
    on_disconnect = AsyncMock()
    monkeypatch.setattr(manager.camera_stream, "on_device_disconnected", on_disconnect)

    new_control, new_media, new_session = await connect("second")
    await asyncio.wait_for(old_media.wait_closed(), 1)
    assert not old_session.ready
    assert old_session.token not in manager._camera_datagram_sessions
    await old_control.close()
    assert manager._camera_connections["second"].datagram_session is new_session
    assert manager._camera_connection.datagram_session is first_session
    assert manager._camera_credit_task is credit_task
    on_disconnect.assert_not_awaited()

    await new_control.close()
    await asyncio.wait_for(new_media.wait_closed(), 1)
    assert new_session.token not in manager._camera_datagram_sessions
    assert first_session.ready
    assert not credit_task.done()
    on_disconnect.assert_not_awaited()
    await asyncio.wait_for(await first_control.ping(), 1)
    await asyncio.wait_for(await first_media.ping(), 1)


@pytest.mark.asyncio
async def test_default_departure_promotes_remaining_media(media_devices):
    manager, connect = media_devices
    first_control, first_media, first_session = await connect("first")
    _, _, second_session = await connect("second")
    await first_control.close()
    await asyncio.wait_for(first_media.wait_closed(), 1)
    async with asyncio.timeout(1):
        while manager.default_device_id != "second":
            await asyncio.sleep(0)
    assert manager._camera_connection.datagram_session is second_session
    assert manager.supports_camera_stream
    assert first_session.token not in manager._camera_datagram_sessions


@pytest.mark.asyncio
async def test_nondefault_media_disconnect_does_not_interrupt_selected_stream(media_devices, monkeypatch):
    manager, connect = media_devices
    _, first_media, first_session = await connect("first")
    second_control, second_media, second_session = await connect("second")
    await manager.begin_camera_datagram_stream()
    credit_task = manager._camera_credit_task
    on_disconnect = AsyncMock()
    monkeypatch.setattr(manager.camera_stream, "on_device_disconnected", on_disconnect)

    await second_media.close()
    async with asyncio.timeout(1):
        while second_session.token in manager._camera_datagram_sessions:
            await asyncio.sleep(0)
    assert manager._camera_connection.datagram_session is first_session
    assert manager.supports_camera_stream
    assert manager._camera_credit_task is credit_task
    assert not credit_task.done()
    on_disconnect.assert_not_awaited()
    await asyncio.wait_for(await first_media.ping(), 1)
    await asyncio.wait_for(await second_control.ping(), 1)
