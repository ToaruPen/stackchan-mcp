"""Tests for ESP32 client connection management."""

import asyncio
import gc
import json
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio
import websockets
from websockets.frames import Close

from stackchan_mcp import esp32_client
from stackchan_mcp.camera_datagram import (
    CameraDatagramSession,
    encode_hello,
    split_frame,
)
from stackchan_mcp.esp32_client import (
    CameraMediaConnection,
    ESP32Connection,
    ESP32Manager,
    _hardware_lane,
)
from stackchan_mcp.head_target_lane import HeadTargetLane

_CONTROL_HEADERS = {"Device-Id": "device-test"}


def _camera_binary(sequence: int = 1) -> bytes:
    jpeg = b"\xff\xd8stream-frame\xff\xd9"
    header = json.dumps(
        {
            "frameId": str(sequence),
            "deviceId": "device-test",
            "mimeType": "image/jpeg",
            "width": 320,
            "height": 240,
            "byteLength": len(jpeg),
            "transport": "binary",
            "seq": sequence,
            "captureTimestampMs": 1000,
            "deviceEncodedAtMs": 1010,
            "quality": 60,
        },
        separators=(",", ":"),
    ).encode()
    return b"SCL1" + bytes((1, 0)) + len(header).to_bytes(2, "big") + header + jpeg


def _ready_datagram_session(
    *,
    token: bytes = bytes(16),
) -> CameraDatagramSession:
    session = CameraDatagramSession(token=token, expected_ip="127.0.0.1")
    session.accept(encode_hello(token), ("127.0.0.1", 41_000), now_ms=0)
    session.begin_stream()
    return session


@pytest_asyncio.fixture
async def manager():
    """Create and start an ESP32Manager on a free port."""
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0)  # Port 0 = OS picks a free port

    # Get the actual port
    server = mgr._server
    port = server.sockets[0].getsockname()[1]
    mgr._test_port = port

    yield mgr
    await mgr.stop()


@pytest_asyncio.fixture
async def manager_with_direct_camera_host():
    mgr = ESP32Manager()
    await mgr.start(
        "127.0.0.1",
        0,
        camera_datagram_host="192.0.2.10",
    )
    server = mgr._server
    mgr._test_port = server.sockets[0].getsockname()[1]

    yield mgr
    await mgr.stop()


class _FakeServeServer:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


@pytest.mark.asyncio
async def test_manager_owns_and_stops_head_target_lane() -> None:
    manager = ESP32Manager()

    assert isinstance(manager.head_target_lane, HeadTargetLane)
    await manager.stop()
    assert manager.head_target_lane.status()["phase"] == "stopped"


@pytest.mark.asyncio
async def test_manager_retains_camera_frame_tasks_until_completion() -> None:
    manager = ESP32Manager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle_frame() -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(handle_frame())
    manager._track_camera_frame_task(task)
    await started.wait()

    assert task in manager._camera_frame_tasks

    release.set()
    await task
    await asyncio.sleep(0)

    assert manager._camera_frame_tasks == set()


class _ClosingHandlerWebSocket:
    """Fake server-side WebSocket that raises a close exception from iteration."""

    def __init__(
        self,
        messages: list[str | bytes],
        close_exc: websockets.exceptions.ConnectionClosed,
    ) -> None:
        self._messages = messages
        self._close_exc = close_exc
        self.request = SimpleNamespace(headers={"Device-Id": "device-test"})
        self.sent: list[str | bytes] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise self._close_exc

    async def send(self, data):
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _GracefulCloseHandlerWebSocket:
    """Fake server-side WebSocket whose iterator exits after a graceful close."""

    def __init__(
        self,
        messages: list[str | bytes],
        close_code: int | None,
        close_reason: str | None,
    ) -> None:
        self._messages = messages
        self.close_code = close_code
        self.close_reason = close_reason
        self.request = SimpleNamespace(headers={"Device-Id": "device-test"})
        self.sent: list[str | bytes] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration

    async def send(self, data):
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _BlockingHelloResponseWebSocket:
    """Expose manager state while the firmware hello response is in flight."""

    def __init__(self) -> None:
        self.request = SimpleNamespace(headers={"Device-Id": "device-test"})
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()
        self._messages = [
            json.dumps(
                {
                    "type": "hello",
                    "version": 1,
                    "features": {"mcp": True, "camera_stream": True},
                }
            )
        ]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration

    async def send(self, _data) -> None:
        self.send_started.set()
        await self.release_send.wait()

    async def close(self) -> None:
        self.release_send.set()


@pytest.mark.asyncio
async def test_manager_starts_and_stops():
    """Manager can start and stop cleanly."""
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0)
    assert mgr._server is not None
    await mgr.stop()
    assert mgr._server is None


@pytest.mark.asyncio
async def test_manager_refuses_non_loopback_bind_without_token(monkeypatch):
    monkeypatch.delenv("STACKCHAN_TOKEN", raising=False)
    monkeypatch.delenv("BEARER_TOKEN", raising=False)

    async def fail_serve(*_args, **_kwargs):
        raise AssertionError("bind safety must run before opening the WebSocket")

    monkeypatch.setattr(websockets, "serve", fail_serve)
    mgr = ESP32Manager()

    with pytest.raises(ValueError, match="non-loopback"):
        await mgr.start("0.0.0.0", 8765)

    assert mgr._server is None


@pytest.mark.asyncio
async def test_manager_starts_and_stops_camera_datagram_listener():
    mgr = ESP32Manager()

    await mgr.start("127.0.0.1", 0)

    assert mgr._camera_datagram_endpoint is not None
    assert mgr._camera_datagram_port > 0

    await mgr.stop()

    assert mgr._camera_datagram_endpoint is None
    assert mgr._camera_datagram_port == 0


@pytest.mark.asyncio
async def test_manager_routes_udp_hello_only_to_matching_session():
    token = bytes(range(16))
    session = CameraDatagramSession(token=token, expected_ip="127.0.0.1")
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0)
    mgr._camera_datagram_sessions[token] = session
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol,
        remote_addr=("127.0.0.1", mgr._camera_datagram_port),
    )
    try:
        transport.sendto(encode_hello(bytes(reversed(token))))
        await asyncio.sleep(0.01)
        assert session.ready is False

        transport.sendto(encode_hello(token))
        await session.wait_ready(timeout_s=0.2)
        assert session.peer is not None
        assert session.peer[0] == "127.0.0.1"
    finally:
        transport.close()
        await mgr.stop()


@pytest.mark.asyncio
async def test_manager_stop_closes_server_when_camera_cleanup_fails(monkeypatch):
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0)

    async def fail_camera_cleanup() -> None:
        raise ConnectionError("device disconnected during camera cleanup")

    monkeypatch.setattr(mgr.camera_stream, "stop_all", fail_camera_cleanup)

    await mgr.stop()

    assert mgr._server is None


@pytest.mark.asyncio
async def test_manager_start_sets_explicit_websocket_keepalive(monkeypatch, caplog):
    """The gateway keeps websockets defaults explicit and visible in logs."""
    captured: dict[str, object] = {}
    fake_server = _FakeServeServer()

    async def fake_serve(handler, host, port, **kwargs):
        captured.update(
            {
                "handler": handler,
                "host": host,
                "port": port,
                "kwargs": kwargs,
            }
        )
        return fake_server

    monkeypatch.setattr(websockets, "serve", fake_serve)
    caplog.set_level(logging.INFO, logger="stackchan_mcp.esp32_client")
    mgr = ESP32Manager()

    await mgr.start("127.0.0.1", 8765)
    await mgr.stop()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    kwargs = captured["kwargs"]
    assert kwargs["ping_interval"] == 20
    assert kwargs["ping_timeout"] == 20
    assert fake_server.closed is True
    assert fake_server.waited is True
    assert "ping_interval=20 ping_timeout=20" in caplog.text


@pytest.mark.asyncio
async def test_no_device_connected():
    """call_tool returns error when no device is connected."""
    mgr = ESP32Manager()
    result, error = await mgr.call_tool("self.robot.set_head_angles", {"yaw": 0, "pitch": 0})
    assert result is None
    assert error is not None
    assert "not connected" in error["message"].lower() or "No ESP32" in error["message"]


@pytest.mark.asyncio
async def test_get_status_disconnected():
    """get_status returns disconnected state."""
    mgr = ESP32Manager()
    status = mgr.get_status()
    assert status["connected"] is False
    assert status["device_id"] is None


@pytest.mark.asyncio
async def test_control_websocket_rejects_missing_device_identity(manager):
    async with websockets.connect(
        f"ws://127.0.0.1:{manager._test_port}",
    ) as ws:
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), timeout=0.2)

    assert manager.device_connected is False


@pytest.mark.asyncio
async def test_control_is_registered_before_hello_response_allows_media_attach(
    monkeypatch,
):
    manager = ESP32Manager()
    ws = _BlockingHelloResponseWebSocket()

    async def skip_init(_connection, _device_id) -> None:
        return None

    monkeypatch.setattr(manager, "_init_device", skip_init)
    handler = asyncio.create_task(manager._handler(ws))  # type: ignore[arg-type]
    await asyncio.wait_for(ws.send_started.wait(), timeout=0.2)

    assert manager.connection is not None
    assert manager.connection.device_id == "device-test"

    ws.release_send.set()
    await asyncio.wait_for(handler, timeout=0.2)


@pytest.mark.asyncio
async def test_esp32_hello_handshake(manager):
    """ESP32 can connect and complete hello handshake."""
    port = manager._test_port

    async with websockets.connect(
        f"ws://127.0.0.1:{port}",
        additional_headers=_CONTROL_HEADERS,
    ) as ws:
        # Send hello
        hello = {
            "type": "hello",
            "version": 1,
            "features": {"mcp": True},
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
        await ws.send(json.dumps(hello))

        # Receive hello response
        resp_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        resp = json.loads(resp_raw)
        assert resp["type"] == "hello"
        assert resp["version"] == 1
        assert "session_id" in resp

        # Receive initialize request from gateway
        init_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        init_msg = json.loads(init_raw)
        assert init_msg["type"] == "mcp"
        assert init_msg["payload"]["method"] == "initialize"

        # Send initialize response
        init_resp = {
            "session_id": init_msg["session_id"],
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": init_msg["payload"]["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test-device", "version": "1.0.0"},
                },
            },
        }
        await ws.send(json.dumps(init_resp))

        # Receive tools/list request
        tools_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        tools_msg = json.loads(tools_raw)
        assert tools_msg["type"] == "mcp"
        assert tools_msg["payload"]["method"] == "tools/list"

        # Send tools/list response
        tools_resp = {
            "session_id": tools_msg["session_id"],
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": tools_msg["payload"]["id"],
                "result": {
                    "tools": [
                        {
                            "name": "self.robot.set_head_angles",
                            "description": "Set head angles",
                            "inputSchema": {"type": "object"},
                        }
                    ],
                    "nextCursor": "",
                },
            },
        }
        await ws.send(json.dumps(tools_resp))

        auto_msg = await _expect_auto_idle_avatar(ws)
        await _send_mcp_response(
            ws,
            auto_msg,
            result={"content": [{"type": "text", "text": "true"}], "isError": False},
        )
        blink_msg = await _expect_auto_blink(ws)
        await _send_mcp_response(
            ws,
            blink_msg,
            result={"content": [{"type": "text", "text": "true"}], "isError": False},
        )

        # Wait for manager to process
        await asyncio.sleep(0.2)

        # Verify connection is established
        assert manager.device_connected is True
        status = manager.get_status()
        assert status["connected"] is True
        assert status["tools_count"] == 1


@pytest.mark.asyncio
async def test_esp32_tool_call_relay(manager):
    """Gateway relays tool calls to ESP32."""
    port = manager._test_port

    async with websockets.connect(
        f"ws://127.0.0.1:{port}",
        additional_headers=_CONTROL_HEADERS,
    ) as ws:
        # Complete handshake
        await _complete_handshake(
            ws,
            tools=[
                {
                    "name": "self.robot.set_head_angles",
                    "description": "Set head",
                    "inputSchema": {},
                }
            ],
        )

        await asyncio.sleep(0.2)

        # Now call tool via manager
        call_task = asyncio.create_task(
            manager.call_tool("self.robot.set_head_angles", {"yaw": 45, "pitch": 10})
        )

        # ESP32 receives the request
        req_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        req_msg = json.loads(req_raw)
        assert req_msg["type"] == "mcp"
        assert req_msg["payload"]["method"] == "tools/call"
        assert req_msg["payload"]["params"]["name"] == "self.robot.set_head_angles"
        assert req_msg["payload"]["params"]["arguments"] == {"yaw": 45, "pitch": 10}

        # ESP32 sends response
        tool_resp = {
            "session_id": req_msg["session_id"],
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": req_msg["payload"]["id"],
                "result": {
                    "content": [{"type": "text", "text": "true"}],
                    "isError": False,
                },
            },
        }
        await ws.send(json.dumps(tool_resp))

        # Verify result
        result, error = await asyncio.wait_for(call_task, timeout=5.0)
        assert error is None
        assert result["content"][0]["text"] == "true"


@pytest.mark.asyncio
async def test_esp32_disconnect_handling(manager):
    """Manager handles ESP32 disconnection gracefully."""
    port = manager._test_port

    async with websockets.connect(
        f"ws://127.0.0.1:{port}",
        additional_headers=_CONTROL_HEADERS,
    ) as ws:
        await _complete_handshake(ws)
        await asyncio.sleep(0.2)
        assert manager.device_connected is True

    # Connection closed
    await asyncio.sleep(0.2)
    assert manager.device_connected is False


@pytest.mark.asyncio
async def test_active_disconnect_notifies_camera_stream(manager, monkeypatch):
    disconnected = asyncio.Event()

    async def record_disconnect() -> None:
        disconnected.set()

    monkeypatch.setattr(
        manager.camera_stream,
        "on_device_disconnected",
        record_disconnect,
    )

    async with websockets.connect(
        f"ws://127.0.0.1:{manager._test_port}",
        additional_headers=_CONTROL_HEADERS,
    ) as ws:
        await _complete_handshake(ws)
        await asyncio.sleep(0.2)

    await asyncio.wait_for(disconnected.wait(), timeout=1)


@pytest.mark.asyncio
async def test_handler_logs_graceful_close_details_once(monkeypatch, caplog):
    """Normal async-for completion still logs enriched close details once."""
    ticks = iter([100.0, 103.25, 105.5])
    monkeypatch.setattr(esp32_client, "_monotonic", lambda: next(ticks))
    ws = _GracefulCloseHandlerWebSocket(
        [json.dumps({"type": "noop"})],
        close_code=1000,
        close_reason="normal",
    )
    caplog.set_level(logging.INFO, logger="stackchan_mcp.esp32_client")

    await ESP32Manager()._handler(ws)  # type: ignore[arg-type]

    disconnect_logs = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("ESP32 disconnected:")
    ]
    assert disconnect_logs == [
        "ESP32 disconnected: device=device-test close_class=GracefulClose "
        "rcvd_code=1000 rcvd_reason='normal' sent_code=None sent_reason=None "
        "last_frame_age_s=2.250 lifetime_s=5.500"
    ]


@pytest.mark.asyncio
async def test_handler_logs_close_details_with_last_frame_elapsed(monkeypatch, caplog):
    """Disconnect logs include close class, close frames, and timing fields."""
    ticks = iter([100.0, 103.25, 105.5])
    monkeypatch.setattr(esp32_client, "_monotonic", lambda: next(ticks))
    close_exc = websockets.exceptions.ConnectionClosedOK(
        Close(1000, "normal"),
        Close(1000, "ack"),
        True,
    )
    ws = _ClosingHandlerWebSocket(
        [json.dumps({"type": "noop"})],
        close_exc,
    )
    caplog.set_level(logging.INFO, logger="stackchan_mcp.esp32_client")

    await ESP32Manager()._handler(ws)  # type: ignore[arg-type]

    assert "ESP32 disconnected: device=device-test" in caplog.text
    assert "close_class=ConnectionClosedOK" in caplog.text
    assert "rcvd_code=1000 rcvd_reason='normal'" in caplog.text
    assert "sent_code=1000 sent_reason='ack'" in caplog.text
    assert "last_frame_age_s=2.250" in caplog.text
    assert "lifetime_s=5.500" in caplog.text


@pytest.mark.asyncio
async def test_handler_logs_close_details_when_fields_are_missing(
    monkeypatch,
    caplog,
):
    """Missing close fields and missing inbound frames are logged safely."""
    ticks = iter([200.0, 204.75])
    monkeypatch.setattr(esp32_client, "_monotonic", lambda: next(ticks))
    close_exc = websockets.exceptions.ConnectionClosedError(
        Close(1006, "abnormal"),
        None,
        None,
    )
    ws = _ClosingHandlerWebSocket([], close_exc)
    caplog.set_level(logging.INFO, logger="stackchan_mcp.esp32_client")

    await ESP32Manager()._handler(ws)  # type: ignore[arg-type]

    assert "ESP32 disconnected: device=device-test" in caplog.text
    assert "close_class=ConnectionClosedError" in caplog.text
    assert "rcvd_code=1006 rcvd_reason='abnormal'" in caplog.text
    assert "sent_code=None sent_reason=None" in caplog.text
    assert "last_frame_age_s=None" in caplog.text
    assert "lifetime_s=4.750" in caplog.text


@pytest.mark.asyncio
async def test_auth_rejection(manager):
    """Unauthorized connections are rejected."""
    import os
    port = manager._test_port

    # Set token to require auth
    os.environ["STACKCHAN_TOKEN"] = "test-secret-token"
    try:
        # Try connecting without auth — should fail
        with pytest.raises(Exception):
            async with websockets.connect(
                f"ws://127.0.0.1:{port}",
                additional_headers={"Authorization": "Bearer wrong-token"},
            ) as ws:
                await ws.recv()
    finally:
        del os.environ["STACKCHAN_TOKEN"]


@pytest.mark.asyncio
async def test_camera_media_connection_uses_the_same_bearer_auth(manager, monkeypatch):
    monkeypatch.setenv("STACKCHAN_TOKEN", "test-secret-token")

    with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
        async with websockets.connect(
            f"ws://127.0.0.1:{manager._test_port}",
            additional_headers={
                "Authorization": "Bearer wrong-token",
                "Camera-Stream": "1",
                "Device-Id": "device-test",
            },
        ):
            pass

    assert exc_info.value.response.status_code == 401


# ---------------------------------------------------------------------------
# Parallel hardware-lane dispatch (Issue #73)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "lane"),
    [
        ("self.robot.set_head_angles", "servo"),
        ("self.led.set_many", "led"),
        ("self.port_b.ws2812.set_strip", "port_b"),
        ("self.port_c.ws2812.set_strip", "port_c"),
        ("self.display.set_avatar", "avatar"),
        ("self.screen.set_brightness", "display"),
        ("self.audio_speaker.set_volume", "audio"),
        ("self.camera.take_photo", "camera"),
        ("self.touch.get_touch_state", "touch"),
        ("self.get_device_status", "status"),
        ("self.unknown.experimental", "default"),
    ],
)
def test_hardware_lane_covers_gateway_tool_routes(tool_name, lane):
    """Gateway-routed ESP32 tools map to explicit hardware lanes."""
    assert _hardware_lane(tool_name) == lane


def test_cached_motion_state_uses_non_bus_telemetry_lane():
    """Cached interpolation state must not queue behind servo-bus commands."""
    assert (
        _hardware_lane(
            "self.robot.get_head_angles",
            {"cached_motion_state": True},
        )
        == "servo_telemetry"
    )
    assert _hardware_lane("self.robot.get_head_angles", {}) == "servo"


@pytest.mark.asyncio
async def test_connection_pipelines_concurrent_tool_calls_before_first_response():
    """Concurrent tools/call requests are sent before either response arrives."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-parallel")  # type: ignore[arg-type]

    servo_task = asyncio.create_task(
        conn.call_tool("self.robot.set_head_angles", {"yaw": 10, "pitch": 30})
    )
    led_task = asyncio.create_task(
        conn.call_tool("self.led.set_many", {"colors": "[[255, 0, 0]]"})
    )

    await asyncio.sleep(0)

    assert len(ws.sent) == 2
    sent_messages = [json.loads(message) for message in ws.sent]
    request_ids = [message["payload"]["id"] for message in sent_messages]
    assert [message["payload"]["method"] for message in sent_messages] == [
        "tools/call",
        "tools/call",
    ]
    assert [message["payload"]["params"]["name"] for message in sent_messages] == [
        "self.robot.set_head_angles",
        "self.led.set_many",
    ]

    conn.handle_response(
        {
            "jsonrpc": "2.0",
            "id": request_ids[1],
            "result": {"content": [{"type": "text", "text": "led"}]},
        }
    )
    conn.handle_response(
        {
            "jsonrpc": "2.0",
            "id": request_ids[0],
            "result": {"content": [{"type": "text", "text": "servo"}]},
        }
    )

    servo_result, led_result = await asyncio.gather(servo_task, led_task)
    assert servo_result[0]["content"][0]["text"] == "servo"
    assert servo_result[1] is None
    assert led_result[0]["content"][0]["text"] == "led"
    assert led_result[1] is None


@pytest.mark.asyncio
async def test_connection_removes_pending_request_when_call_is_cancelled():
    """Cancelling a tool call does not leave a stale pending response slot."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-cancel")  # type: ignore[arg-type]

    task = asyncio.create_task(
        conn.call_tool("self.robot.set_head_angles", {"yaw": 10, "pitch": 30})
    )

    await asyncio.sleep(0)
    assert len(ws.sent) == 1
    assert len(conn._pending) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert conn._pending == {}


# ---------------------------------------------------------------------------
# Auto idle avatar render after session initialization (Issue #77)
# ---------------------------------------------------------------------------


class _InitDeviceConnection:
    """Fake connection for exercising ESP32Manager._init_device."""

    def __init__(
        self,
        *,
        avatar_render_sent: bool = False,
        blink_control_sent: bool = False,
        discover_ok: bool = True,
        auto_error: dict | None = None,
        auto_exception: Exception | None = None,
        blink_error: dict | None = None,
        blink_exception: Exception | None = None,
    ) -> None:
        self.tools: list[dict] = []
        self.tools_discovered = False
        self.avatar_render_sent = avatar_render_sent
        self.blink_control_sent = blink_control_sent
        self.discover_ok = discover_ok
        self.auto_error = auto_error
        self.auto_exception = auto_exception
        self.blink_error = blink_error
        self.blink_exception = blink_exception
        self.initialize_calls = 0
        self.discover_calls = 0
        self.call_tool_calls: list[tuple[str, dict]] = []

    async def initialize(self, *, vision_url: str = "", vision_token: str = "") -> bool:
        self.initialize_calls += 1
        return True

    async def discover_tools(self) -> list[dict]:
        self.discover_calls += 1
        if not self.discover_ok:
            self.tools = []
            self.tools_discovered = False
            return self.tools

        self.tools = [
            {
                "name": "self.display.set_avatar",
                "description": "Set avatar",
                "inputSchema": {"type": "object"},
            },
            {
                "name": "self.display.set_blink",
                "description": "Set blink",
                "inputSchema": {"type": "object"},
            },
        ]
        self.tools_discovered = True
        return self.tools

    async def call_tool(self, name: str, arguments: dict):
        self.call_tool_calls.append((name, arguments))
        if name == "self.display.set_avatar":
            self.avatar_render_sent = True
            if self.auto_exception is not None:
                raise self.auto_exception
            error = self.auto_error
        elif name == "self.display.set_blink":
            self.blink_control_sent = True
            if self.blink_exception is not None:
                raise self.blink_exception
            error = self.blink_error
        else:
            error = None
        return {"content": [{"type": "text", "text": "true"}]}, error


class _AutoMcpWebSocket:
    """Fake WebSocket that responds to gateway MCP requests immediately."""

    def __init__(self) -> None:
        self.connection: ESP32Connection | None = None
        self.sent: list[str] = []
        self.tool_calls: list[tuple[str, dict]] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)
        message = json.loads(data)
        payload = message["payload"]
        method = payload["method"]

        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-device", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "self.display.set_avatar",
                        "description": "Set avatar",
                        "inputSchema": {"type": "object"},
                    },
                    {
                        "name": "self.display.set_blink",
                        "description": "Set blink",
                        "inputSchema": {"type": "object"},
                    },
                ],
                "nextCursor": "",
            }
        elif method == "tools/call":
            params = payload["params"]
            self.tool_calls.append((params["name"], params["arguments"]))
            result = {"content": [{"type": "text", "text": "true"}], "isError": False}
        else:
            raise AssertionError(f"unexpected MCP method: {method}")

        assert self.connection is not None
        self.connection.handle_response(
            {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        )


@pytest.mark.asyncio
async def test_call_tool_tracks_explicit_blink_control():
    """Either set_blink value records connection-scoped user intent."""
    ws = _AutoMcpWebSocket()
    connection = ESP32Connection(ws, session_id="session-blink")  # type: ignore[arg-type]
    ws.connection = connection

    assert connection.blink_control_sent is False

    await connection.call_tool("self.display.set_blink", {"enabled": False})

    assert connection.blink_control_sent is True


@pytest.mark.asyncio
async def test_init_auto_renders_idle_avatar_and_enables_blink_after_tools_list():
    """A successful initialize + tools/list sends idle then enables blink."""
    ws = _AutoMcpWebSocket()
    connection = ESP32Connection(ws, session_id="session-auto")  # type: ignore[arg-type]
    connection.device_id = "device-test"
    ws.connection = connection
    mgr = ESP32Manager()
    mgr._connection = connection
    mgr._camera_connection = CameraMediaConnection(
        _FakeWebSocket(),  # type: ignore[arg-type]
        device_id="device-test",
        datagram_session=_ready_datagram_session(),
    )
    ready_calls = 0

    async def record_ready() -> None:
        nonlocal ready_calls
        ready_calls += 1

    mgr.camera_stream.on_device_ready = record_ready  # type: ignore[method-assign]

    await mgr._init_device(connection, "device-test")

    assert ws.tool_calls == [
        ("self.display.set_avatar", {"face": "idle"}),
        ("self.display.set_blink", {"enabled": True}),
    ]
    assert connection.avatar_render_sent is True
    assert connection.blink_control_sent is True
    assert ready_calls == 1


@pytest.mark.asyncio
async def test_init_skips_auto_idle_avatar_when_avatar_already_sent():
    """An explicit face suppresses auto-idle without suppressing auto-blink."""
    mgr = ESP32Manager()
    connection = _InitDeviceConnection(avatar_render_sent=True)

    await mgr._init_device(connection, "device-test")  # type: ignore[arg-type]

    assert connection.initialize_calls == 1
    assert connection.discover_calls == 1
    assert connection.call_tool_calls == [
        ("self.display.set_blink", {"enabled": True})
    ]


@pytest.mark.asyncio
async def test_init_skips_auto_blink_when_blink_control_already_sent():
    """An explicit set_blink value is not overwritten during initialization."""
    mgr = ESP32Manager()
    connection = _InitDeviceConnection(blink_control_sent=True)

    await mgr._init_device(connection, "device-test")  # type: ignore[arg-type]

    assert connection.call_tool_calls == [
        ("self.display.set_avatar", {"face": "idle"})
    ]


@pytest.mark.asyncio
async def test_init_skips_auto_idle_avatar_when_tools_discovery_fails(caplog):
    """The auto-render path only runs after successful tools/list discovery."""
    caplog.set_level(logging.INFO, logger="stackchan_mcp.esp32_client")
    mgr = ESP32Manager()
    connection = _InitDeviceConnection(discover_ok=False)

    await mgr._init_device(connection, "device-test")  # type: ignore[arg-type]

    assert connection.initialize_calls == 1
    assert connection.discover_calls == 1
    assert connection.call_tool_calls == []
    assert "ESP32 ready: device=device-test" not in caplog.text


@pytest.mark.parametrize("failure_mode", ["error", "timeout"])
@pytest.mark.asyncio
async def test_init_continues_when_auto_idle_avatar_fails(failure_mode, caplog):
    """Auto-render failures are warnings and do not block ESP32 ready."""
    caplog.set_level(logging.INFO, logger="stackchan_mcp.esp32_client")
    if failure_mode == "error":
        connection = _InitDeviceConnection(
            auto_error={"code": -32000, "message": "device rejected set_avatar"}
        )
    else:
        connection = _InitDeviceConnection(
            auto_exception=asyncio.TimeoutError("set_avatar timed out")
        )
    mgr = ESP32Manager()

    await mgr._init_device(connection, "device-test")  # type: ignore[arg-type]

    assert connection.call_tool_calls == [
        ("self.display.set_avatar", {"face": "idle"}),
        ("self.display.set_blink", {"enabled": True}),
    ]
    assert "auto-rendering idle avatar failed" in caplog.text
    assert "ESP32 ready: device=device-test tools=2" in caplog.text


@pytest.mark.parametrize("failure_mode", ["error", "timeout"])
@pytest.mark.asyncio
async def test_init_continues_when_auto_blink_fails(failure_mode, caplog):
    """Auto-blink failures are warnings and do not block ESP32 ready."""
    caplog.set_level(logging.INFO, logger="stackchan_mcp.esp32_client")
    if failure_mode == "error":
        connection = _InitDeviceConnection(
            blink_error={"code": -32000, "message": "device rejected set_blink"}
        )
    else:
        connection = _InitDeviceConnection(
            blink_exception=asyncio.TimeoutError("set_blink timed out")
        )
    mgr = ESP32Manager()

    await mgr._init_device(connection, "device-test")  # type: ignore[arg-type]

    assert connection.call_tool_calls == [
        ("self.display.set_avatar", {"face": "idle"}),
        ("self.display.set_blink", {"enabled": True}),
    ]
    assert "auto-enabling avatar blink failed" in caplog.text
    assert "ESP32 ready: device=device-test tools=2" in caplog.text


@pytest.mark.asyncio
async def test_reconnect_auto_renders_idle_avatar_and_enables_blink_again():
    """A new ESP32Connection gets fresh avatar and blink flags."""
    first_ws = _AutoMcpWebSocket()
    first = ESP32Connection(first_ws, session_id="session-first")  # type: ignore[arg-type]
    first_ws.connection = first
    second_ws = _AutoMcpWebSocket()
    second = ESP32Connection(second_ws, session_id="session-second")  # type: ignore[arg-type]
    second_ws.connection = second
    mgr = ESP32Manager()

    await mgr._init_device(first, "device-test")
    await mgr._init_device(second, "device-test")

    expected_calls = [
        ("self.display.set_avatar", {"face": "idle"}),
        ("self.display.set_blink", {"enabled": True}),
    ]
    assert first_ws.tool_calls == expected_calls
    assert second_ws.tool_calls == expected_calls


@pytest.mark.asyncio
async def test_send_avatar_set_fetch_resolves_when_loaded_event_arrives():
    """avatar_set_loaded resolves the matching load_avatar_set waiter."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-avatar")  # type: ignore[arg-type]

    task = asyncio.create_task(
        conn.send_avatar_set_fetch(
            url="https://example.invalid/avatar-set.bin",
            token="test-token",
            mode="replace",
            checksum="sha256:avatar-set",
            expected_size=1234,
            timeout=30.0,
        )
    )

    await asyncio.sleep(0)
    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0]) == {
        "type": "avatar_set_fetch",
        "url": "https://example.invalid/avatar-set.bin",
        "token": "test-token",
        "mode": "replace",
        "checksum": "sha256:avatar-set",
        "expected_size": 1234,
    }

    payload = {
        "ok": True,
        "checksum": "sha256:avatar-set",
        "bytes": 1234,
    }
    conn.handle_avatar_set_loaded(payload)

    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == payload
    assert conn._avatar_set_waiters == {}


@pytest.mark.asyncio
async def test_send_avatar_set_fetch_returns_disconnected_when_connection_drops():
    """Disconnect wakes an in-flight avatar set fetch without waiting for timeout."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-avatar")  # type: ignore[arg-type]

    task = asyncio.create_task(
        conn.send_avatar_set_fetch(
            url="https://example.invalid/avatar-set.bin",
            token="test-token",
            mode="replace",
            checksum="sha256:avatar-set",
            expected_size=1234,
            timeout=30.0,
        )
    )

    await asyncio.sleep(0)
    assert len(ws.sent) == 1
    assert len(conn._avatar_set_waiters) == 1

    started_at = asyncio.get_running_loop().time()
    conn.disconnect()
    result = await asyncio.wait_for(task, timeout=1.0)
    elapsed = asyncio.get_running_loop().time() - started_at

    assert result == {
        "ok": False,
        "checksum": "sha256:avatar-set",
        "error": "disconnected",
    }
    assert elapsed < 1.0
    assert conn._avatar_set_waiters == {}


class _GateableConnection:
    """Fake initialized connection with per-tool release gates."""

    connected = True
    initialized = True

    def __init__(self, releases: dict[str, asyncio.Event]) -> None:
        self.releases = releases
        self.started: list[str] = []
        self.finished: list[str] = []
        self.all_started = asyncio.Event()

    async def call_tool(self, name, arguments):  # noqa: ARG002 - test fake
        self.started.append(name)
        if len(self.started) >= len(self.releases):
            self.all_started.set()
        await self.releases[name].wait()
        self.finished.append(name)
        return {"content": [{"type": "text", "text": name}]}, None


class _RecordingConnection:
    """Fake initialized connection that records exact tool arguments."""

    connected = True
    initialized = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "{}"}]}, None


@pytest.mark.asyncio
async def test_head_target_seed_uses_cached_motion_state() -> None:
    """Lane startup must not block the firmware main task on servo-bus reads."""
    connection = _RecordingConnection()
    mgr = ESP32Manager()
    mgr._connection = connection  # type: ignore[assignment]

    await mgr.acquire_head_target_lane()
    try:
        await mgr.read_head_target_pose()
    finally:
        await mgr.release_head_target_lane()

    assert connection.calls == [
        (
            "self.robot.get_head_angles",
            {"cached_motion_state": True},
        )
    ]


@pytest.mark.asyncio
async def test_manager_call_tools_dispatches_independent_lanes_in_parallel():
    """Servo, LED, and avatar calls start together instead of waiting in line."""
    releases = {
        "self.robot.set_head_angles": asyncio.Event(),
        "self.led.set_many": asyncio.Event(),
        "self.display.set_avatar": asyncio.Event(),
    }
    connection = _GateableConnection(releases)
    mgr = ESP32Manager()
    mgr._connection = connection  # type: ignore[assignment]

    task = asyncio.create_task(
        mgr.call_tools(
            [
                ("self.robot.set_head_angles", {"yaw": 0, "pitch": 45}),
                ("self.led.set_many", {"colors": "[]"}),
                ("self.display.set_avatar", {"face": "happy"}),
            ]
        )
    )

    await asyncio.wait_for(connection.all_started.wait(), timeout=1.0)
    assert connection.started == [
        "self.robot.set_head_angles",
        "self.led.set_many",
        "self.display.set_avatar",
    ]
    assert connection.finished == []

    for release in releases.values():
        release.set()
    results = await asyncio.wait_for(task, timeout=1.0)

    assert [result[0]["content"][0]["text"] for result in results] == [
        "self.robot.set_head_angles",
        "self.led.set_many",
        "self.display.set_avatar",
    ]
    assert [error for _, error in results] == [None, None, None]


@pytest.mark.asyncio
async def test_manager_call_tool_uses_lane_dispatch_for_existing_api():
    """Existing single-tool API can still overlap independent hardware lanes."""
    releases = {
        "self.robot.set_head_angles": asyncio.Event(),
        "self.led.set_many": asyncio.Event(),
    }
    connection = _GateableConnection(releases)
    mgr = ESP32Manager()
    mgr._connection = connection  # type: ignore[assignment]

    servo_task = asyncio.create_task(
        mgr.call_tool("self.robot.set_head_angles", {"yaw": 0, "pitch": 45})
    )
    led_task = asyncio.create_task(
        mgr.call_tool("self.led.set_many", {"colors": "[]"})
    )

    await asyncio.wait_for(connection.all_started.wait(), timeout=1.0)
    assert connection.started == [
        "self.robot.set_head_angles",
        "self.led.set_many",
    ]
    assert connection.finished == []

    for release in releases.values():
        release.set()
    results = await asyncio.wait_for(
        asyncio.gather(servo_task, led_task),
        timeout=1.0,
    )

    assert [result[0]["content"][0]["text"] for result in results] == [
        "self.robot.set_head_angles",
        "self.led.set_many",
    ]
    assert [error for _, error in results] == [None, None]


@pytest.mark.asyncio
async def test_manager_call_tools_serializes_calls_on_same_hardware_lane():
    """Two servo calls keep their relative order on the servo lane."""
    releases = {
        "self.robot.set_head_angles": asyncio.Event(),
        "self.robot.get_head_angles": asyncio.Event(),
    }
    connection = _GateableConnection(releases)
    mgr = ESP32Manager()
    mgr._connection = connection  # type: ignore[assignment]

    task = asyncio.create_task(
        mgr.call_tools(
            [
                ("self.robot.set_head_angles", {"yaw": 0, "pitch": 45}),
                ("self.robot.get_head_angles", {}),
            ]
        )
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert connection.started == ["self.robot.set_head_angles"]

    releases["self.robot.set_head_angles"].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert connection.started == [
        "self.robot.set_head_angles",
        "self.robot.get_head_angles",
    ]

    releases["self.robot.get_head_angles"].set()
    await asyncio.wait_for(task, timeout=1.0)
    assert connection.finished == [
        "self.robot.set_head_angles",
        "self.robot.get_head_angles",
    ]


@pytest.mark.asyncio
async def test_manager_pipelines_cached_motion_state_around_servo_bus_call():
    """Cached interpolation telemetry remains non-blocking during a servo call."""
    releases = {
        "self.robot.set_head_angles": asyncio.Event(),
        "self.robot.get_head_angles": asyncio.Event(),
    }
    connection = _GateableConnection(releases)
    mgr = ESP32Manager()
    mgr._connection = connection  # type: ignore[assignment]

    task = asyncio.create_task(
        mgr.call_tools(
            [
                ("self.robot.set_head_angles", {"yaw": 0, "pitch": 45}),
                (
                    "self.robot.get_head_angles",
                    {"cached_motion_state": True},
                ),
            ]
        )
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert connection.started == [
        "self.robot.set_head_angles",
        "self.robot.get_head_angles",
    ]

    for release in releases.values():
        release.set()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_head_target_reservation_pipelines_two_and_blocks_regular_servo():
    releases = {
        "self.robot.set_head_angles": asyncio.Event(),
        "self.led.set_many": asyncio.Event(),
    }
    connection = _GateableConnection(releases)
    mgr = ESP32Manager()
    mgr._connection = connection  # type: ignore[assignment]

    await mgr.acquire_head_target_lane()
    first = asyncio.create_task(mgr.call_head_target_tool({"yaw": 4, "pitch": 33}))
    second = asyncio.create_task(mgr.call_head_target_tool({"yaw": 8, "pitch": 33}))
    regular_servo = asyncio.create_task(
        mgr.call_tool("self.robot.set_head_angles", {"yaw": 12, "pitch": 33})
    )
    led = asyncio.create_task(mgr.call_tool("self.led.set_many", {"colors": "[]"}))
    for _ in range(10):
        if (
            connection.started.count("self.robot.set_head_angles") == 2
            and "self.led.set_many" in connection.started
        ):
            break
        await asyncio.sleep(0)

    assert connection.started.count("self.robot.set_head_angles") == 2
    assert "self.led.set_many" in connection.started
    assert not regular_servo.done()

    for release in releases.values():
        release.set()
    await asyncio.wait_for(asyncio.gather(first, second, led), timeout=1.0)
    assert connection.started.count("self.robot.set_head_angles") == 2

    await mgr.release_head_target_lane()
    await asyncio.wait_for(regular_servo, timeout=1.0)
    assert connection.started.count("self.robot.set_head_angles") == 3


@pytest.mark.asyncio
async def test_reserved_head_target_never_moves_to_replacement_connection():
    old_connection = _GateableConnection(
        {"self.robot.set_head_angles": asyncio.Event()}
    )
    new_connection = _GateableConnection(
        {"self.robot.set_head_angles": asyncio.Event()}
    )
    mgr = ESP32Manager()
    mgr._connection = old_connection  # type: ignore[assignment]
    await mgr.acquire_head_target_lane()
    mgr._connection = new_connection  # type: ignore[assignment]

    result, error = await mgr.call_head_target_tool({"yaw": 4, "pitch": 33})

    assert result is None
    assert error == {"code": -32000, "message": "ESP32 not connected"}
    assert old_connection.started == []
    assert new_connection.started == []
    await mgr.release_head_target_lane()


class _ImmediateHeadLaneConnection:
    connected = True
    initialized = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_tool(self, name, arguments):  # noqa: ARG002 - test fake
        self.calls.append(name)
        if name == "self.robot.get_head_angles":
            payload = {"yaw": 0, "pitch": 33}
        elif name == "self.wifi.set_power_save":
            payload = {"ok": True}
        else:
            payload = {"ok": True, "servo_ok": True}
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload),
                }
            ]
        }, None

    def disconnect(self, *, reason: str = "unspecified") -> None:
        del reason
        self.connected = False


@pytest.mark.asyncio
async def test_replacement_stops_lane_before_switching_reserved_connection():
    old_connection = _ImmediateHeadLaneConnection()
    new_connection = _ImmediateHeadLaneConnection()
    mgr = ESP32Manager()
    mgr._connection = old_connection  # type: ignore[assignment]
    await mgr.head_target_lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    assert mgr._head_target_lane_connection is old_connection
    assert mgr._tool_lane_locks["servo"].locked()

    registered = await mgr._register_connection(new_connection)  # type: ignore[arg-type]

    assert registered is True
    assert old_connection.connected is False
    assert mgr._connection is new_connection
    assert mgr._head_target_lane_connection is None
    assert not mgr._tool_lane_locks["servo"].locked()
    assert mgr.head_target_lane.status()["phase"] == "stopped"


# ---------------------------------------------------------------------------
# send_audio_frame (TTS pipeline egress, Issue #70 PR2)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal stand-in for websockets.ServerConnection used in unit tests."""

    def __init__(self) -> None:
        self.sent: list[bytes | str] = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self, **_kwargs):
        self.closed = True


@pytest.mark.asyncio
async def test_connection_send_audio_frame_sends_binary():
    """ESP32Connection.send_audio_frame writes the bytes to the underlying WS."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    await conn.send_audio_frame(b"opus_payload_bytes")

    assert ws.sent == [b"opus_payload_bytes"]


@pytest.mark.asyncio
async def test_connection_send_audio_frame_raises_after_disconnect():
    """A disconnected connection refuses to send rather than silently dropping."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    conn.disconnect()

    with pytest.raises(ConnectionError):
        await conn.send_audio_frame(b"opus_payload_bytes")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_manager_send_audio_frame_no_device():
    """ESP32Manager.send_audio_frame raises when no device is attached.

    The orchestrator turns this into a clean MCP error JSON; without
    this guard the call would AttributeError on a None connection.
    """
    mgr = ESP32Manager()

    with pytest.raises(ConnectionError):
        await mgr.send_audio_frame(b"opus_payload_bytes")


@pytest.mark.asyncio
async def test_camera_stream_requires_a_matching_dedicated_media_connection():
    control_ws = _FakeWebSocket()
    control = ESP32Connection(
        control_ws,
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "device-test"
    control.features = {
        "camera_stream": True,
        "camera_datagram_v1": True,
    }
    media_ws = _FakeWebSocket()
    manager = ESP32Manager()
    manager._connection = control

    assert manager.supports_camera_stream is False

    manager._camera_connection = CameraMediaConnection(
        media_ws,  # type: ignore[arg-type]
        device_id="another-device",
    )
    assert manager.supports_camera_stream is False

    unready = CameraDatagramSession(token=bytes(16), expected_ip="127.0.0.1")
    manager._camera_connection = CameraMediaConnection(
        media_ws,  # type: ignore[arg-type]
        device_id="device-test",
        datagram_session=unready,
    )
    assert manager.supports_camera_stream is False

    ready = CameraDatagramSession(token=bytes(16), expected_ip="127.0.0.1")
    ready.accept(encode_hello(bytes(16)), ("127.0.0.1", 41_000), now_ms=0)
    manager._camera_connection = CameraMediaConnection(
        media_ws,  # type: ignore[arg-type]
        device_id="device-test",
        datagram_session=ready,
    )
    assert manager.supports_camera_stream is True


@pytest.mark.asyncio
async def test_camera_stream_credit_lease_uses_only_the_datagram_endpoint(
    monkeypatch,
):
    control_ws = _FakeWebSocket()
    control = ESP32Connection(
        control_ws,
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "device-test"
    control.features = {
        "camera_stream": True,
        "camera_datagram_v1": True,
    }
    media_ws = _FakeWebSocket()
    manager = ESP32Manager()
    manager._connection = control
    session = _ready_datagram_session()
    manager._camera_connection = CameraMediaConnection(
        media_ws,  # type: ignore[arg-type]
        device_id="device-test",
        datagram_session=session,
    )
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class RecordingEndpoint:
        def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
            sent.append((data, addr))

    manager._camera_datagram_endpoint = RecordingEndpoint()  # type: ignore[assignment]
    monkeypatch.setattr(esp32_client, "CAMERA_DATAGRAM_CREDIT_INTERVAL_S", 0.001)

    await manager.begin_camera_datagram_stream()
    for _ in range(100):
        if len(sent) >= 2:
            break
        await asyncio.sleep(0.001)
    await manager.end_camera_datagram_stream()
    count_after_stop = len(sent)
    await asyncio.sleep(0.003)

    assert control_ws.sent == []
    assert media_ws.sent == []
    assert count_after_stop >= 2
    assert len(sent) == count_after_stop
    assert {addr for _, addr in sent} == {("127.0.0.1", 41_000)}
    assert all(data.endswith(b"\x04") for data, _ in sent)


@pytest.mark.asyncio
async def test_camera_stream_end_discards_pending_and_rejects_late_chunks(
    monkeypatch,
):
    manager = ESP32Manager()
    session = _ready_datagram_session()
    manager._camera_connection = CameraMediaConnection(
        _FakeWebSocket(),  # type: ignore[arg-type]
        device_id="device-test",
        datagram_session=session,
    )

    class RecordingEndpoint:
        def sendto(self, _data: bytes, _addr: tuple[str, int]) -> None:
            return None

    manager._camera_datagram_endpoint = RecordingEndpoint()  # type: ignore[assignment]
    monkeypatch.setattr(esp32_client, "CAMERA_DATAGRAM_CREDIT_INTERVAL_S", 1)
    await manager.begin_camera_datagram_stream()
    pending = split_frame(token=session.token, sequence=5, frame=bytes(2_000))
    assert session.accept(pending[0], ("127.0.0.1", 41_000), now_ms=1) is None
    assert session.status()["pending"] is True

    await manager.end_camera_datagram_stream()

    assert session.status()["pending"] is False
    assert session.accept(
        split_frame(token=session.token, sequence=6, frame=b"late")[0],
        ("127.0.0.1", 41_000),
        now_ms=2,
    ) is None
    assert session.status()["completed_frames"] == 0


@pytest.mark.asyncio
async def test_active_control_departure_retires_matching_camera_session():
    manager = ESP32Manager()
    control = ESP32Connection(
        _FakeWebSocket(),
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "device-test"
    manager._connection = control
    media_ws = _FakeWebSocket()
    session = _ready_datagram_session()
    media = CameraMediaConnection(
        media_ws,  # type: ignore[arg-type]
        device_id="device-test",
        datagram_session=session,
        control=control,
    )
    manager._camera_connection = media
    manager._camera_datagram_sessions[session.token] = session
    manager._camera_ready_pair = (control, media)

    await manager._detach_camera_media(control)

    assert manager._camera_connection is None
    assert manager._camera_ready_pair is None
    assert session.token not in manager._camera_datagram_sessions
    assert session.ready is False
    assert media_ws.closed is True


def test_camera_datagram_status_preserves_safe_counters_after_session_retirement():
    manager = ESP32Manager()
    session = _ready_datagram_session()
    envelope = _camera_binary(sequence=7)
    for packet in split_frame(token=session.token, sequence=7, frame=envelope):
        session.accept(packet, ("127.0.0.1", 41_000), now_ms=1)
    manager._camera_datagram_sessions[session.token] = session
    manager._camera_connection = CameraMediaConnection(
        _FakeWebSocket(),  # type: ignore[arg-type]
        device_id="device-test",
        datagram_session=session,
    )

    active = manager.camera_datagram_status()
    assert active["ready"] is True
    assert active["completed_frames"] == 1

    manager._retire_camera_datagram_session(session)

    retired = manager.camera_datagram_status()
    assert retired["ready"] is False
    assert retired["completed_frames"] == 1
    assert "token" not in retired
    assert "peer" not in retired
    assert "source_ip" not in retired

    manager._retire_camera_datagram_session(session)
    assert manager.camera_datagram_status()["completed_frames"] == 1


def test_camera_datagram_status_has_empty_histograms_before_session_preparation():
    status = ESP32Manager().camera_datagram_status()

    empty_histogram = {"count": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0}
    assert status["assembly_ms"] == empty_histogram
    assert status["completed_interval_ms"] == empty_histogram


@pytest.mark.asyncio
async def test_manager_stop_closes_the_dedicated_camera_connection():
    media_ws = _FakeWebSocket()
    manager = ESP32Manager()
    manager._camera_connection = CameraMediaConnection(
        media_ws,  # type: ignore[arg-type]
        device_id="device-test",
    )

    await manager.stop()

    assert media_ws.closed is True
    assert manager._camera_connection is None


@pytest.mark.asyncio
async def test_camera_media_websocket_configures_udp_and_routes_no_tcp_frames(
    manager,
):
    control_ws = _FakeWebSocket()
    control = ESP32Connection(
        control_ws,
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "device-test"
    control.features = {
        "camera_stream": True,
        "camera_datagram_v1": True,
    }
    manager._connection = control
    manager.camera_stream.can_accept_frames = lambda: True  # type: ignore[method-assign]

    async with websockets.connect(
        f"ws://127.0.0.1:{manager._test_port}",
        additional_headers={
            "Camera-Stream": "1",
            "Device-Id": "device-test",
        },
    ) as media_ws:
        config = json.loads(await asyncio.wait_for(media_ws.recv(), timeout=1.0))
        assert config["type"] == "camera_datagram_config"
        assert config["version"] == 1
        assert config["maxDatagramBytes"] == 1_200
        assert config["port"] == manager._camera_datagram_port
        token = bytes.fromhex(config["token"])
        assert len(token) == 16
        loop = asyncio.get_running_loop()
        udp_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            remote_addr=("127.0.0.1", config["port"]),
        )
        udp_transport.sendto(encode_hello(token))
        for _ in range(20):
            if manager.supports_camera_stream:
                break
            await asyncio.sleep(0.01)
        assert manager.supports_camera_stream is True
        await manager.begin_camera_datagram_stream()

        await media_ws.send(_camera_binary(sequence=6))
        await asyncio.sleep(0.01)
        assert manager.camera_frames.status()["available"] is False

        for datagram in split_frame(
            token=token,
            sequence=7,
            frame=_camera_binary(sequence=7),
        ):
            udp_transport.sendto(datagram)

        frame = await manager.camera_frames.wait_for_frame(
            after_sequence=None,
            timeout_s=1.0,
        )
        assert frame is not None
        assert frame.sequence == 7
        assert control_ws.sent == []
        udp_transport.close()

    for _ in range(20):
        if not manager.supports_camera_stream:
            break
        await asyncio.sleep(0.01)
    assert manager.supports_camera_stream is False


@pytest.mark.asyncio
async def test_camera_media_disconnect_preserves_logical_stream_lease(
    manager,
    monkeypatch,
):
    control = ESP32Connection(
        _FakeWebSocket(),
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "device-test"
    control.features = {
        "camera_stream": True,
        "camera_datagram_v1": True,
    }
    manager._connection = control
    disconnected = asyncio.Event()
    stop_all_calls = 0

    async def record_disconnect() -> None:
        disconnected.set()

    async def record_stop_all() -> None:
        nonlocal stop_all_calls
        stop_all_calls += 1

    monkeypatch.setattr(
        manager.camera_stream,
        "on_device_disconnected",
        record_disconnect,
    )
    monkeypatch.setattr(manager.camera_stream, "stop_all", record_stop_all)

    async with websockets.connect(
        f"ws://127.0.0.1:{manager._test_port}",
        additional_headers={
            "Camera-Stream": "1",
            "Device-Id": "device-test",
        },
    ) as media_ws:
        config = json.loads(await asyncio.wait_for(media_ws.recv(), timeout=0.2))
        token = bytes.fromhex(config["token"])
        loop = asyncio.get_running_loop()
        udp_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            remote_addr=("127.0.0.1", config["port"]),
        )
        udp_transport.sendto(encode_hello(token))
        for _ in range(20):
            if manager.supports_camera_stream:
                break
            await asyncio.sleep(0.01)
        assert manager.supports_camera_stream is True
        udp_transport.close()

    await asyncio.wait_for(disconnected.wait(), timeout=0.2)
    assert stop_all_calls == 0
    assert manager.supports_camera_stream is False


@pytest.mark.asyncio
async def test_camera_media_advertises_direct_udp_host_without_binding_to_proxy_ip(
    manager_with_direct_camera_host,
):
    manager = manager_with_direct_camera_host
    control_ws = _FakeWebSocket()
    control = ESP32Connection(
        control_ws,
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "device-test"
    control.features = {
        "camera_stream": True,
        "camera_datagram_v1": True,
    }
    manager._connection = control

    async with websockets.connect(
        f"ws://127.0.0.1:{manager._test_port}",
        additional_headers={
            "Camera-Stream": "1",
            "Device-Id": "device-test",
        },
    ) as media_ws:
        config = json.loads(await asyncio.wait_for(media_ws.recv(), timeout=1.0))
        assert config["host"] == "192.0.2.10"
        session = manager._camera_connection.datagram_session
        assert session is not None
        assert session.accept(
            encode_hello(bytes.fromhex(config["token"])),
            ("192.0.2.20", 41_000),
            now_ms=0,
        ) is None
        await asyncio.wait_for(session.wait_ready(timeout_s=0.1), timeout=0.2)
        assert manager.supports_camera_stream is True


@pytest.mark.asyncio
async def test_camera_media_attach_retries_ready_after_control_initialization(
    manager,
    monkeypatch,
):
    control = ESP32Connection(
        _FakeWebSocket(),
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "device-test"
    control.features = {"camera_stream": True}
    control._initialized = True
    control._tools_discovered = True
    manager._connection = control
    ready = asyncio.Event()

    async def record_ready() -> None:
        ready.set()

    monkeypatch.setattr(manager.camera_stream, "on_device_ready", record_ready)

    async with websockets.connect(
        f"ws://127.0.0.1:{manager._test_port}",
        additional_headers={
            "Camera-Stream": "1",
            "Device-Id": "device-test",
        },
    ) as media_ws:
        config = json.loads(await asyncio.wait_for(media_ws.recv(), timeout=0.2))
        token = bytes.fromhex(config["token"])
        loop = asyncio.get_running_loop()
        udp_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            remote_addr=("127.0.0.1", config["port"]),
        )
        udp_transport.sendto(encode_hello(token))
        await asyncio.wait_for(ready.wait(), timeout=0.2)
        udp_transport.close()


@pytest.mark.asyncio
async def test_camera_ready_runs_once_for_the_same_control_media_pair(monkeypatch):
    control = ESP32Connection(
        _FakeWebSocket(),
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "device-test"
    control.features = {
        "camera_stream": True,
        "camera_datagram_v1": True,
    }
    control._initialized = True
    control._tools_discovered = True
    media = CameraMediaConnection(
        _FakeWebSocket(),  # type: ignore[arg-type]
        device_id="device-test",
        datagram_session=_ready_datagram_session(),
    )
    manager = ESP32Manager()
    manager._connection = control
    manager._camera_connection = media
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def record_ready() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(manager.camera_stream, "on_device_ready", record_ready)

    first = asyncio.create_task(manager._ensure_camera_stream_ready(control))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    second = asyncio.create_task(manager._ensure_camera_stream_ready(control))
    release.set()
    await asyncio.gather(first, second)

    assert calls == 1


@pytest.mark.asyncio
async def test_camera_media_websocket_rejects_a_nonmatching_device(manager):
    control = ESP32Connection(
        _FakeWebSocket(),
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "device-test"
    control.features = {"camera_stream": True}
    manager._connection = control

    async with websockets.connect(
        f"ws://127.0.0.1:{manager._test_port}",
        additional_headers={
            "Camera-Stream": "1",
            "Device-Id": "different-device",
        },
    ) as media_ws:
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(media_ws.recv(), timeout=0.2)

    assert manager._camera_connection is None
    assert manager.supports_camera_stream is False


@pytest.mark.asyncio
async def test_datagram_camera_router_publishes_frame_without_per_frame_credit(
    monkeypatch,
):
    audio_frames: list[tuple[bytes, str]] = []

    async def record_audio(frame: bytes, session_id: str) -> None:
        audio_frames.append((frame, session_id))

    monkeypatch.setattr(esp32_client, "handle_audio_frame", record_audio)
    ws = _FakeWebSocket()
    connection = ESP32Connection(ws, session_id="camera-session")  # type: ignore[arg-type]
    connection.device_id = "device-test"
    connection.features = {
        "camera_stream": True,
        "camera_datagram_v1": True,
    }
    media_ws = _FakeWebSocket()
    manager = ESP32Manager()
    manager._connection = connection
    session = _ready_datagram_session()
    manager._camera_connection = CameraMediaConnection(
        media_ws,  # type: ignore[arg-type]
        device_id="device-test",
        datagram_session=session,
    )
    manager.camera_stream.can_accept_frames = lambda: True  # type: ignore[method-assign]

    await manager._handle_camera_datagram_frame(
        _camera_binary(sequence=8),
        session=session,
    )

    frame = await manager.camera_frames.wait_for_frame(
        after_sequence=None,
        timeout_s=0,
    )
    assert frame is not None
    assert frame.sequence == 8
    assert audio_frames == []
    assert ws.sent == []
    assert media_ws.sent == []
    assert manager.camera_stream.status()["datagram_lease_active"] is False


@pytest.mark.asyncio
async def test_datagram_camera_router_drops_mismatched_frame_identity():
    control = ESP32Connection(
        _FakeWebSocket(),
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "expected-device"
    control.features = {
        "camera_stream": True,
        "camera_datagram_v1": True,
    }
    media_ws = _FakeWebSocket()
    manager = ESP32Manager()
    manager._connection = control
    session = _ready_datagram_session()
    manager._camera_connection = CameraMediaConnection(
        media_ws,  # type: ignore[arg-type]
        device_id="expected-device",
        datagram_session=session,
    )
    manager.camera_stream.can_accept_frames = lambda: True  # type: ignore[method-assign]

    await manager._handle_camera_datagram_frame(
        _camera_binary(sequence=8),
        session=session,
    )

    assert manager.camera_frames.status()["available"] is False
    assert media_ws.sent == []


@pytest.mark.asyncio
async def test_datagram_camera_router_drops_frames_after_control_disconnect():
    control = ESP32Connection(
        _FakeWebSocket(),
        session_id="camera-session",
    )  # type: ignore[arg-type]
    control.device_id = "device-test"
    control.features = {
        "camera_stream": True,
        "camera_datagram_v1": True,
    }
    media_ws = _FakeWebSocket()
    manager = ESP32Manager()
    manager._connection = control
    session = _ready_datagram_session()
    manager._camera_connection = CameraMediaConnection(
        media_ws,  # type: ignore[arg-type]
        device_id="device-test",
        datagram_session=session,
    )
    manager.camera_stream.can_accept_frames = lambda: True  # type: ignore[method-assign]
    control.disconnect()

    await manager._handle_camera_datagram_frame(
        _camera_binary(sequence=9),
        session=session,
    )

    assert manager.camera_frames.status()["available"] is False
    assert media_ws.sent == []


@pytest.mark.asyncio
async def test_control_binary_router_drops_camera_frames_without_media_fallback():
    ws = _FakeWebSocket()
    connection = ESP32Connection(ws, session_id="camera-session")  # type: ignore[arg-type]
    manager = ESP32Manager()
    manager._connection = connection

    await manager._handle_binary_message(
        connection,
        _camera_binary(sequence=9),
        "camera-session",
    )

    assert manager.camera_frames.status()["available"] is False
    assert ws.sent == []


@pytest.mark.asyncio
async def test_binary_router_keeps_raw_opus_on_the_existing_audio_path(
    monkeypatch,
):
    audio_frames: list[tuple[bytes, str]] = []

    async def record_audio(frame: bytes, session_id: str) -> None:
        audio_frames.append((frame, session_id))

    monkeypatch.setattr(esp32_client, "handle_audio_frame", record_audio)
    ws = _FakeWebSocket()
    connection = ESP32Connection(ws, session_id="audio-session")  # type: ignore[arg-type]
    manager = ESP32Manager()

    await manager._handle_binary_message(
        connection,
        b"raw-opus-frame",
        "audio-session",
    )

    assert audio_frames == [(b"raw-opus-frame", "audio-session")]
    assert ws.sent == []
    assert manager.camera_frames.status()["available"] is False


@pytest.mark.asyncio
async def test_binary_router_drops_invalid_scl1_without_forwarding_to_audio(
    monkeypatch,
    caplog,
):
    audio_frames: list[tuple[bytes, str]] = []

    async def record_audio(frame: bytes, session_id: str) -> None:
        audio_frames.append((frame, session_id))

    monkeypatch.setattr(esp32_client, "handle_audio_frame", record_audio)
    ws = _FakeWebSocket()
    connection = ESP32Connection(ws, session_id="camera-session")  # type: ignore[arg-type]
    manager = ESP32Manager()
    caplog.set_level(logging.WARNING, logger="stackchan_mcp.esp32_client")

    await manager._handle_binary_message(
        connection,
        b"SCL1",
        "camera-session",
    )

    assert audio_frames == []
    assert ws.sent == []
    assert manager.camera_frames.status()["available"] is False
    assert "invalid SCL1 camera frame" in caplog.text


@pytest.mark.asyncio
async def test_connection_send_tts_state_sends_json():
    """ESP32Connection.send_tts_state writes a tts state JSON message."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-tts")  # type: ignore[arg-type]

    await conn.send_tts_state("start")

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload == {
        "session_id": "session-tts",
        "type": "tts",
        "state": "start",
    }


@pytest.mark.asyncio
async def test_connection_send_tts_state_raises_after_disconnect():
    """A disconnected connection refuses to send TTS notifications."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-tts")  # type: ignore[arg-type]

    conn.disconnect()

    with pytest.raises(ConnectionError):
        await conn.send_tts_state("stop")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_manager_send_tts_state_no_device():
    """ESP32Manager.send_tts_state raises when no device is attached."""
    mgr = ESP32Manager()

    with pytest.raises(ConnectionError):
        await mgr.send_tts_state("start")


# ---------------------------------------------------------------------------
# send_listen_state (STT pipeline, Issue #91)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_send_listen_state_start_includes_mode():
    """listen.start carries a mode field and omits the default voice profile."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-listen")  # type: ignore[arg-type]

    await conn.send_listen_state("start", mode="manual")

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload == {
        "session_id": "session-listen",
        "type": "listen",
        "state": "start",
        "mode": "manual",
    }


@pytest.mark.asyncio
async def test_connection_send_listen_state_raw_profile_includes_profile():
    """listen.start carries profile only when a non-default profile is requested."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-listen")  # type: ignore[arg-type]

    await conn.send_listen_state("start", mode="manual", profile="raw")

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload == {
        "session_id": "session-listen",
        "type": "listen",
        "state": "start",
        "mode": "manual",
        "profile": "raw",
    }


@pytest.mark.asyncio
async def test_connection_send_listen_state_stop_omits_mode():
    """listen.stop has no mode field — the wire shape mirrors the firmware.

    The firmware's ``OnIncomingJson`` listen handler only consults
    ``mode`` on ``state="start"``; sending it on stop would be noise.
    """
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-listen")  # type: ignore[arg-type]

    await conn.send_listen_state("stop")

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload == {
        "session_id": "session-listen",
        "type": "listen",
        "state": "stop",
    }


@pytest.mark.asyncio
async def test_connection_send_listen_state_raises_after_disconnect():
    """A disconnected connection refuses to send listen notifications."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-listen")  # type: ignore[arg-type]

    conn.disconnect()

    with pytest.raises(ConnectionError):
        await conn.send_listen_state("start", mode="manual")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_manager_send_listen_state_no_device():
    """ESP32Manager.send_listen_state raises when no device is attached."""
    mgr = ESP32Manager()

    with pytest.raises(ConnectionError):
        await mgr.send_listen_state("start")


def test_manager_listen_lock_is_same_as_tts_lock():
    """listen() and say() share a single audio-path lock per device.

    Without sharing, the firmware's ``HandleStartListeningEvent`` could
    abort an in-flight ``say()`` mid-utterance the moment a concurrent
    ``listen()`` arrived (state == kDeviceStateSpeaking →
    AbortSpeaking + SetListeningMode), and conversely TTS frames in
    flight would leak into a concurrent capture's buffer. Treating
    the audio path as a single serialised resource keeps the device's
    state machine observable from the gateway side.
    """
    mgr = ESP32Manager()
    assert mgr.tts_lock is mgr.listen_lock


class _FailingWebSocket:
    """WebSocket that raises a websockets-specific error on send()."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.send_calls = 0

    async def send(self, data):
        self.send_calls += 1
        raise self._exc


@pytest.mark.asyncio
async def test_send_audio_frame_translates_websockets_close_to_connection_error():
    """websockets.ConnectionClosed becomes ConnectionError + marks dead.

    Without translation the websockets-specific exception would
    bypass the orchestrator's ``except ConnectionError`` filter and
    leak as a stack trace through the MCP transport.
    """
    import websockets.exceptions

    closed = websockets.exceptions.ConnectionClosed(rcvd=None, sent=None)
    ws = _FailingWebSocket(closed)
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    with pytest.raises(ConnectionError, match="WebSocket send"):
        await conn.send_audio_frame(b"opus")

    # After the translated failure, the connection is marked dead so
    # subsequent sends fail fast without re-touching the dead socket.
    assert not conn.connected
    with pytest.raises(ConnectionError):
        await conn.send_audio_frame(b"more")
    assert ws.send_calls == 1


@pytest.mark.asyncio
async def test_send_tts_state_translates_oserror_to_connection_error():
    """OSError on send (e.g. broken pipe) is translated to ConnectionError."""
    ws = _FailingWebSocket(OSError("broken pipe"))
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    with pytest.raises(ConnectionError, match="WebSocket send"):
        await conn.send_tts_state("start")
    assert not conn.connected


@pytest.mark.asyncio
async def test_send_mcp_request_translates_send_failure_and_marks_disconnected():
    """tools/call send failures use the same connection-state handling as TTS."""
    ws = _FailingWebSocket(OSError("broken pipe"))
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    loop_errors = []
    previous_handler = loop.get_exception_handler()

    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        result, error = await conn.call_tool("self.robot.set_head_angles", {})
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert result is None
    assert error is not None
    assert "WebSocket send failed" in error["message"]
    assert not conn.connected
    assert conn._pending == {}
    assert ws.send_calls == 1
    assert loop_errors == []


def test_connection_default_protocol_version_is_one():
    """Fresh ESP32Connection defaults to WebSocket protocol v1.

    v1 is what the gateway's audio framing currently targets (raw
    Opus binary frames). v2/v3 wrap payloads in a BinaryProtocol
    header which this gateway does not yet emit; the hello handler
    logs a warning when a non-v1 device negotiates so operators know
    the TTS path may not work for them.
    """
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    assert conn.protocol_version == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _complete_handshake(ws, tools=None, *, consume_auto_avatar=True):
    """Complete the full ESP32 handshake sequence."""
    if tools is None:
        tools = []

    # Send hello
    hello = {
        "type": "hello",
        "version": 1,
        "features": {"mcp": True},
        "transport": "websocket",
    }
    await ws.send(json.dumps(hello))

    # Receive hello response
    await asyncio.wait_for(ws.recv(), timeout=5.0)

    # Receive and respond to initialize
    init_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    init_msg = json.loads(init_raw)
    init_resp = {
        "session_id": init_msg["session_id"],
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": init_msg["payload"]["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-device", "version": "1.0.0"},
            },
        },
    }
    await ws.send(json.dumps(init_resp))

    # Receive and respond to tools/list
    tools_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    tools_msg = json.loads(tools_raw)
    tools_resp = {
        "session_id": tools_msg["session_id"],
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": tools_msg["payload"]["id"],
            "result": {"tools": tools, "nextCursor": ""},
        },
    }
    await ws.send(json.dumps(tools_resp))
    if not consume_auto_avatar:
        return None

    auto_msg = await _expect_auto_idle_avatar(ws)
    await _send_mcp_response(
        ws,
        auto_msg,
        result={"content": [{"type": "text", "text": "true"}], "isError": False},
    )
    blink_msg = await _expect_auto_blink(ws)
    await _send_mcp_response(
        ws,
        blink_msg,
        result={"content": [{"type": "text", "text": "true"}], "isError": False},
    )
    return auto_msg


async def _expect_auto_idle_avatar(ws):
    """Receive and assert the automatic idle avatar tools/call."""
    auto_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    auto_msg = json.loads(auto_raw)
    assert auto_msg["type"] == "mcp"
    assert auto_msg["payload"]["method"] == "tools/call"
    assert auto_msg["payload"]["params"]["name"] == "self.display.set_avatar"
    assert auto_msg["payload"]["params"]["arguments"] == {"face": "idle"}
    return auto_msg


async def _expect_auto_blink(ws):
    """Receive and assert the automatic avatar blink tools/call."""
    auto_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    auto_msg = json.loads(auto_raw)
    assert auto_msg["type"] == "mcp"
    assert auto_msg["payload"]["method"] == "tools/call"
    assert auto_msg["payload"]["params"]["name"] == "self.display.set_blink"
    assert auto_msg["payload"]["params"]["arguments"] == {"enabled": True}
    return auto_msg


async def _send_mcp_response(ws, req_msg, *, result=None, error=None):
    """Send a JSON-RPC response for a gateway-originated MCP request."""
    payload = {
        "jsonrpc": "2.0",
        "id": req_msg["payload"]["id"],
    }
    if error is None:
        payload["result"] = result or {}
    else:
        payload["error"] = error

    await ws.send(
        json.dumps(
            {
                "session_id": req_msg["session_id"],
                "type": "mcp",
                "payload": payload,
            }
        )
    )


# --- Device-driven listen capture --------------------------------------------


@pytest_asyncio.fixture
async def manager_with_hook(monkeypatch):
    """ESP32Manager started with a configured audio hook URL.

    ``push_audio_capture`` is patched to record invocations into a
    shared list so tests can assert the hook was triggered without
    starting a real HTTP server. The recorded payload is the actual
    ``frames`` list the gateway captured for that listen window.
    """
    calls: list[dict] = []

    async def _fake_push(hook_url, token, frames, *, session_id="", timeout_s=10.0):
        calls.append(
            {
                "hook_url": hook_url,
                "token": token,
                "frames": list(frames),
                "session_id": session_id,
            }
        )
        return True

    monkeypatch.setattr(
        "stackchan_mcp.esp32_client.push_audio_capture", _fake_push
    )

    mgr = ESP32Manager()
    await mgr.start(
        "127.0.0.1",
        0,
        audio_hook_url="http://test/hook",
        audio_hook_token="test-token",
    )
    server = mgr._server
    mgr._test_port = server.sockets[0].getsockname()[1]

    try:
        yield mgr, calls
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_device_driven_listen_pushes_to_hook(manager_with_hook):
    """device → gateway listen.start/stop sequence forwards frames
    captured between the two messages to the audio hook."""
    from stackchan_mcp.audio_stream import is_recording

    mgr, calls = manager_with_hook
    port = mgr._test_port

    async with websockets.connect(
        f"ws://127.0.0.1:{port}",
        additional_headers=_CONTROL_HEADERS,
    ) as ws:
        await _complete_handshake(ws)

        # Device-initiated listen.start
        await ws.send(
            json.dumps(
                {
                    "session_id": "",  # device fills its own; ignored on receive
                    "type": "listen",
                    "state": "start",
                    "mode": "manual",
                }
            )
        )

        # Wait for gateway to open the recording slot. We can't observe
        # the gateway's internals through the WS, so poll the module
        # state for a short bounded time.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if is_recording():
                break
        assert is_recording(), "gateway did not open the recording slot"

        # Stream a couple of binary "audio" frames
        await ws.send(b"\xaa\xbb\xcc")
        await ws.send(b"\xdd\xee\xff")

        # Give the gateway a moment to buffer the frames
        await asyncio.sleep(0.1)

        # Device-initiated listen.stop
        await ws.send(
            json.dumps(
                {
                    "session_id": "",
                    "type": "listen",
                    "state": "stop",
                }
            )
        )

        # Wait for the push task to fire (asyncio.create_task in the
        # handler dispatches it eagerly; one event-loop tick is enough,
        # but we give it a few to absorb scheduling jitter).
        for _ in range(20):
            await asyncio.sleep(0.05)
            if calls:
                break

    assert len(calls) == 1
    assert calls[0]["hook_url"] == "http://test/hook"
    assert calls[0]["token"] == "test-token"
    assert calls[0]["frames"] == [b"\xaa\xbb\xcc", b"\xdd\xee\xff"]


@pytest.mark.asyncio
async def test_device_driven_listen_disabled_when_no_hook(manager):
    """Without STACKCHAN_AUDIO_HOOK_URL the gateway ignores inbound
    listen.start (no recording slot opens, no push fires)."""
    from stackchan_mcp.audio_stream import is_recording

    port = manager._test_port

    async with websockets.connect(
        f"ws://127.0.0.1:{port}",
        additional_headers=_CONTROL_HEADERS,
    ) as ws:
        await _complete_handshake(ws)

        await ws.send(
            json.dumps(
                {
                    "type": "listen",
                    "state": "start",
                    "mode": "manual",
                }
            )
        )
        # Give the gateway time to NOT do anything.
        await asyncio.sleep(0.2)
        assert not is_recording()


@pytest.mark.asyncio
async def test_device_driven_listen_cleanup_on_disconnect(manager_with_hook):
    """Disconnecting mid-capture drops the partial buffer rather than
    leaking it into the next connection's recording slot."""
    from stackchan_mcp.audio_stream import is_recording

    mgr, calls = manager_with_hook
    port = mgr._test_port

    async with websockets.connect(
        f"ws://127.0.0.1:{port}",
        additional_headers=_CONTROL_HEADERS,
    ) as ws:
        await _complete_handshake(ws)
        await ws.send(
            json.dumps(
                {
                    "type": "listen",
                    "state": "start",
                    "mode": "manual",
                }
            )
        )
        for _ in range(20):
            await asyncio.sleep(0.05)
            if is_recording():
                break
        assert is_recording()
        await ws.send(b"\x11\x22\x33")
        await asyncio.sleep(0.05)
        # Drop the connection without sending listen.stop.

    # Give the server-side handler's finally clause time to run.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if not is_recording():
            break
    assert not is_recording(), "recording slot was leaked across connections"
    # No push should have fired for the aborted capture.
    assert calls == []
