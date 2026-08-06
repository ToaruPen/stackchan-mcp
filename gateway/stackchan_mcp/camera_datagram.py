"""Authenticated latest-only datagrams for StackChan camera frames."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import struct
from typing import Any, cast
import zlib

from .camera_metrics import BoundedLatencyHistogram

SCU1_MAGIC = b"SCU1"
SCU1_VERSION = 1
SCU1_FRAME = 1
SCU1_CREDIT = 2
SCU1_HELLO = 3
SCU1_TOKEN_BYTES = 16
SCU1_MAX_DATAGRAM_BYTES = 1_200
SCU1_MAX_FRAME_BYTES = 5 * 1024 * 1024

_PREFIX = struct.Struct("!4sBB16s")
_FRAME_HEADER = struct.Struct("!4sBB16sIHHII")
_CREDIT = struct.Struct("!4sBB16sB")
_FRAME_PAYLOAD_BYTES = SCU1_MAX_DATAGRAM_BYTES - _FRAME_HEADER.size


class CameraDatagramProtocolError(ValueError):
    """Raised when an SCU1 datagram violates the wire contract."""


@dataclass(frozen=True, slots=True)
class FrameChunk:
    token: bytes
    sequence: int
    chunk_index: int
    chunk_count: int
    frame_length: int
    frame_crc32: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class CreditGrant:
    token: bytes
    credits: int


@dataclass(frozen=True, slots=True)
class SessionHello:
    token: bytes


@dataclass(slots=True)
class _PendingFrame:
    chunk: FrameChunk
    started_at_ms: int
    chunks: dict[int, bytes]


class LatestFrameAssembler:
    """Reassemble at most one newest frame without waiting for an older one."""

    def __init__(
        self,
        *,
        max_age_ms: int = 500,
        max_frame_bytes: int = SCU1_MAX_FRAME_BYTES,
    ) -> None:
        if max_age_ms <= 0:
            raise ValueError("camera datagram max age must be positive")
        if not 1 <= max_frame_bytes <= SCU1_MAX_FRAME_BYTES:
            raise ValueError("camera datagram max frame bytes are invalid")
        self._max_age_ms = max_age_ms
        self._max_frame_bytes = max_frame_bytes
        self.reset()

    def reset(self) -> None:
        self._pending: _PendingFrame | None = None
        self._latest_sequence: int | None = None
        self._completed_frames = 0
        self._replaced_incomplete_frames = 0
        self._stale_chunks = 0
        self._expired_frames = 0
        self._invalid_frames = 0
        self._assembly_ms = BoundedLatencyHistogram(maximum_bucket=30_000)
        self._completed_interval_ms = BoundedLatencyHistogram(
            maximum_bucket=30_000
        )
        self._last_completed_at_ms: int | None = None

    def clear_stream_state(self) -> None:
        """Discard frame bytes and ordering while preserving diagnostics."""
        self._pending = None
        self._latest_sequence = None
        self._last_completed_at_ms = None

    def status(self) -> dict[str, Any]:
        return {
            "pending": self._pending is not None,
            "completed_frames": self._completed_frames,
            "replaced_incomplete_frames": self._replaced_incomplete_frames,
            "stale_chunks": self._stale_chunks,
            "expired_frames": self._expired_frames,
            "invalid_frames": self._invalid_frames,
            "assembly_ms": self._assembly_ms.status(),
            "completed_interval_ms": self._completed_interval_ms.status(),
        }

    def push(self, datagram: bytes, *, now_ms: int) -> bytes | None:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("camera datagram timestamp must be non-negative")
        try:
            parsed = parse_datagram(datagram)
        except CameraDatagramProtocolError:
            self._invalid_frames += 1
            return None
        if not isinstance(parsed, FrameChunk):
            self._invalid_frames += 1
            return None
        if parsed.frame_length > self._max_frame_bytes:
            self._invalid_frames += 1
            return None

        if (
            self._pending is not None
            and now_ms - self._pending.started_at_ms > self._max_age_ms
        ):
            self._pending = None
            self._expired_frames += 1

        latest = self._latest_sequence
        if latest is not None and parsed.sequence < latest:
            self._stale_chunks += 1
            return None
        if latest is not None and parsed.sequence == latest and self._pending is None:
            self._stale_chunks += 1
            return None

        if latest is None or parsed.sequence > latest:
            if self._pending is not None:
                self._replaced_incomplete_frames += 1
            self._latest_sequence = parsed.sequence
            self._pending = _PendingFrame(
                chunk=parsed,
                started_at_ms=now_ms,
                chunks={},
            )

        pending = self._pending
        if pending is None:
            self._stale_chunks += 1
            return None
        first = pending.chunk
        if (
            parsed.token != first.token
            or parsed.chunk_count != first.chunk_count
            or parsed.frame_length != first.frame_length
            or parsed.frame_crc32 != first.frame_crc32
        ):
            self._pending = None
            self._invalid_frames += 1
            return None

        existing = pending.chunks.get(parsed.chunk_index)
        if existing is not None:
            if existing != parsed.payload:
                self._pending = None
                self._invalid_frames += 1
            return None
        pending.chunks[parsed.chunk_index] = parsed.payload
        if len(pending.chunks) != parsed.chunk_count:
            return None

        frame = b"".join(
            pending.chunks[index] for index in range(parsed.chunk_count)
        )
        self._pending = None
        if (
            len(frame) != parsed.frame_length
            or zlib.crc32(frame) & 0xFFFFFFFF != parsed.frame_crc32
        ):
            self._invalid_frames += 1
            return None
        self._completed_frames += 1
        self._assembly_ms.add(now_ms - pending.started_at_ms)
        if (
            self._last_completed_at_ms is not None
            and now_ms >= self._last_completed_at_ms
        ):
            self._completed_interval_ms.add(now_ms - self._last_completed_at_ms)
        self._last_completed_at_ms = now_ms
        return frame


class CameraDatagramSession:
    """Bind one secret camera session to one authenticated UDP endpoint."""

    def __init__(self, *, token: bytes, expected_ip: str | None) -> None:
        self._token = _require_token(token)
        if expected_ip is not None and (
            not isinstance(expected_ip, str) or not expected_ip
        ):
            raise ValueError("camera datagram expected IP must be non-empty")
        self._expected_ip = expected_ip
        self._peer: tuple[str, int] | None = None
        self._ready_event = asyncio.Event()
        self._assembler = LatestFrameAssembler()
        self._source_mismatch_packets = 0
        self._invalid_packets = 0
        self._closed = False
        self._stream_active = False

    @property
    def token(self) -> bytes:
        return self._token

    @property
    def ready(self) -> bool:
        return not self._closed and self._peer is not None

    @property
    def peer(self) -> tuple[str, int] | None:
        return self._peer if self.ready else None

    async def wait_ready(self, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("camera datagram ready timeout must be positive")
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout_s)
        if not self.ready:
            raise ConnectionError("camera datagram session closed before ready")

    def begin_stream(self) -> None:
        """Reset frame ordering because device sequences restart per stream."""
        if not self.ready:
            raise ConnectionError("camera datagram session is not ready")
        self._assembler.reset()
        self._stream_active = True

    def end_stream(self) -> None:
        """Reject frame chunks and discard any incomplete frame bytes."""
        self._stream_active = False
        self._assembler.clear_stream_state()

    def send_credit(
        self,
        endpoint: CameraDatagramEndpoint,
        credits: int,
    ) -> None:
        peer = self.peer
        if peer is None:
            raise ConnectionError("camera datagram session is not ready")
        endpoint.sendto(encode_credit(self._token, credits), peer)

    def accept(
        self,
        data: bytes,
        addr: tuple[str, int],
        *,
        now_ms: int,
    ) -> bytes | None:
        if self._closed or peek_token(data) != self._token:
            return None
        if self._expected_ip is not None and addr[0] != self._expected_ip:
            self._source_mismatch_packets += 1
            return None
        try:
            parsed = parse_datagram(data)
        except CameraDatagramProtocolError:
            self._invalid_packets += 1
            return None
        if isinstance(parsed, SessionHello):
            if self._peer is None:
                self._peer = addr
                self._ready_event.set()
            elif addr != self._peer:
                self._source_mismatch_packets += 1
            return None
        if self._peer is None or addr != self._peer:
            self._source_mismatch_packets += 1
            return None
        if not isinstance(parsed, FrameChunk):
            self._invalid_packets += 1
            return None
        if not self._stream_active:
            return None
        return self._assembler.push(data, now_ms=now_ms)

    def close(self) -> None:
        self._closed = True
        self._peer = None
        self._ready_event.clear()
        self._stream_active = False
        self._assembler.reset()
        self._source_mismatch_packets = 0
        self._invalid_packets = 0

    def status(self) -> dict[str, Any]:
        assembler = self._assembler.status()
        assembler["invalid_frames"] = (
            int(assembler["invalid_frames"]) + self._invalid_packets
        )
        return {
            "ready": self.ready,
            **assembler,
            "source_mismatch_packets": self._source_mismatch_packets,
        }


class CameraDatagramEndpoint(asyncio.DatagramProtocol):
    """Thin asyncio UDP endpoint that delegates ownership upstream."""

    def __init__(
        self,
        on_datagram: Callable[[bytes, tuple[str, int]], None],
    ) -> None:
        self._on_datagram = on_datagram
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._on_datagram(data, addr)

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._transport is None or self._transport.is_closing():
            raise ConnectionError("camera datagram endpoint is not connected")
        self._transport.sendto(data, addr)

    def close(self) -> None:
        transport = self._transport
        self._transport = None
        if transport is not None:
            transport.close()


def _require_token(token: bytes) -> bytes:
    if not isinstance(token, bytes) or len(token) != SCU1_TOKEN_BYTES:
        raise ValueError("camera datagram token must contain exactly 16 bytes")
    return token


def _require_uint32(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name} must be an unsigned 32-bit integer")
    return value


def split_frame(*, token: bytes, sequence: int, frame: bytes) -> tuple[bytes, ...]:
    """Split one complete SCL1 envelope into bounded SCU1 frame chunks."""
    token = _require_token(token)
    sequence = _require_uint32(sequence, "frame sequence")
    if not isinstance(frame, bytes) or not 1 <= len(frame) <= SCU1_MAX_FRAME_BYTES:
        raise ValueError(
            f"camera frame must contain 1..{SCU1_MAX_FRAME_BYTES} bytes"
        )
    chunk_count = (len(frame) + _FRAME_PAYLOAD_BYTES - 1) // _FRAME_PAYLOAD_BYTES
    if chunk_count > 0xFFFF:
        raise ValueError("camera frame requires too many datagram chunks")
    checksum = zlib.crc32(frame) & 0xFFFFFFFF
    chunks: list[bytes] = []
    for chunk_index in range(chunk_count):
        offset = chunk_index * _FRAME_PAYLOAD_BYTES
        payload = frame[offset : offset + _FRAME_PAYLOAD_BYTES]
        chunks.append(
            _FRAME_HEADER.pack(
                SCU1_MAGIC,
                SCU1_VERSION,
                SCU1_FRAME,
                token,
                sequence,
                chunk_index,
                chunk_count,
                len(frame),
                checksum,
            )
            + payload
        )
    return tuple(chunks)


def encode_credit(token: bytes, credits: int) -> bytes:
    """Encode one bounded producer credit grant."""
    token = _require_token(token)
    if isinstance(credits, bool) or not isinstance(credits, int) or not 1 <= credits <= 4:
        raise ValueError("camera datagram credits must be between 1 and 4")
    return _CREDIT.pack(SCU1_MAGIC, SCU1_VERSION, SCU1_CREDIT, token, credits)


def encode_hello(token: bytes) -> bytes:
    """Encode the firmware-to-Gateway endpoint binding message."""
    token = _require_token(token)
    return _PREFIX.pack(SCU1_MAGIC, SCU1_VERSION, SCU1_HELLO, token)


def peek_token(data: bytes) -> bytes | None:
    """Read a validated SCU1 prefix token without allocating frame storage."""
    if not isinstance(data, bytes) or len(data) < _PREFIX.size:
        return None
    magic, version, kind, token = _PREFIX.unpack_from(data)
    if (
        magic != SCU1_MAGIC
        or version != SCU1_VERSION
        or kind not in {SCU1_FRAME, SCU1_CREDIT, SCU1_HELLO}
    ):
        return None
    return token


def parse_datagram(data: bytes) -> FrameChunk | CreditGrant | SessionHello:
    """Parse and structurally validate one SCU1 datagram."""
    if not isinstance(data, bytes) or len(data) < _PREFIX.size:
        raise CameraDatagramProtocolError("SCU1 datagram is truncated")
    if len(data) > SCU1_MAX_DATAGRAM_BYTES:
        raise CameraDatagramProtocolError("SCU1 datagram exceeds 1200 bytes")

    magic, version, kind, token = _PREFIX.unpack_from(data)
    if magic != SCU1_MAGIC:
        raise CameraDatagramProtocolError("SCU1 magic is invalid")
    if version != SCU1_VERSION:
        raise CameraDatagramProtocolError("SCU1 version is unsupported")

    if kind == SCU1_HELLO:
        if len(data) != _PREFIX.size:
            raise CameraDatagramProtocolError("SCU1 hello length is invalid")
        return SessionHello(token=token)

    if kind == SCU1_CREDIT:
        if len(data) != _CREDIT.size:
            raise CameraDatagramProtocolError("SCU1 credit length is invalid")
        *_, credits = _CREDIT.unpack(data)
        if not 1 <= credits <= 4:
            raise CameraDatagramProtocolError("SCU1 credits must be between 1 and 4")
        return CreditGrant(token=token, credits=credits)

    if kind != SCU1_FRAME:
        raise CameraDatagramProtocolError("SCU1 kind is unsupported")
    if len(data) < _FRAME_HEADER.size:
        raise CameraDatagramProtocolError("SCU1 frame chunk is truncated")

    (
        _,
        _,
        _,
        _,
        sequence,
        chunk_index,
        chunk_count,
        frame_length,
        frame_crc32,
    ) = _FRAME_HEADER.unpack_from(data)
    if not 1 <= frame_length <= SCU1_MAX_FRAME_BYTES:
        raise CameraDatagramProtocolError("SCU1 frame length is invalid")
    expected_chunk_count = (
        frame_length + _FRAME_PAYLOAD_BYTES - 1
    ) // _FRAME_PAYLOAD_BYTES
    if chunk_count != expected_chunk_count:
        raise CameraDatagramProtocolError("SCU1 chunk count is inconsistent")
    if chunk_index >= chunk_count:
        raise CameraDatagramProtocolError("SCU1 chunk index is out of range")
    offset = chunk_index * _FRAME_PAYLOAD_BYTES
    expected_payload_bytes = min(_FRAME_PAYLOAD_BYTES, frame_length - offset)
    payload = data[_FRAME_HEADER.size :]
    if len(payload) != expected_payload_bytes:
        raise CameraDatagramProtocolError("SCU1 chunk payload length is inconsistent")
    return FrameChunk(
        token=token,
        sequence=sequence,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        frame_length=frame_length,
        frame_crc32=frame_crc32,
        payload=payload,
    )
