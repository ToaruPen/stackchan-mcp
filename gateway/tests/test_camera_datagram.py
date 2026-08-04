from __future__ import annotations

import asyncio

import pytest

from stackchan_mcp.camera_datagram import (
    CameraDatagramProtocolError,
    CameraDatagramSession,
    CreditGrant,
    FrameChunk,
    LatestFrameAssembler,
    SessionHello,
    encode_credit,
    encode_hello,
    parse_datagram,
    peek_token,
    split_frame,
)


TOKEN = bytes(range(16))
FRAME = b"abc"
FRAME_DATAGRAM = bytes.fromhex(
    "534355310101"
    "000102030405060708090a0b0c0d0e0f"
    "000000070000000100000003352441c2"
    "616263"
)
HELLO_DATAGRAM = bytes.fromhex(
    "534355310103000102030405060708090a0b0c0d0e0f"
)
CREDIT_DATAGRAM = bytes.fromhex(
    "534355310102000102030405060708090a0b0c0d0e0f04"
)


def test_scu1_golden_vectors_are_stable() -> None:
    assert split_frame(token=TOKEN, sequence=7, frame=FRAME) == (
        FRAME_DATAGRAM,
    )
    assert encode_hello(TOKEN) == HELLO_DATAGRAM
    assert encode_credit(TOKEN, 4) == CREDIT_DATAGRAM


def test_parse_frame_chunk_returns_declared_metadata() -> None:
    parsed = parse_datagram(FRAME_DATAGRAM)

    assert parsed == FrameChunk(
        token=TOKEN,
        sequence=7,
        chunk_index=0,
        chunk_count=1,
        frame_length=3,
        frame_crc32=0x352441C2,
        payload=b"abc",
    )
    assert peek_token(FRAME_DATAGRAM) == TOKEN


def test_parse_credit_and_hello() -> None:
    assert parse_datagram(CREDIT_DATAGRAM) == CreditGrant(
        token=TOKEN,
        credits=4,
    )
    assert parse_datagram(HELLO_DATAGRAM) == SessionHello(token=TOKEN)


def test_split_frame_never_exceeds_datagram_limit() -> None:
    frame = bytes(range(256)) * 12

    datagrams = split_frame(token=TOKEN, sequence=9, frame=frame)

    assert len(datagrams) == 3
    assert all(len(datagram) <= 1_200 for datagram in datagrams)
    chunks = tuple(parse_datagram(datagram) for datagram in datagrams)
    assert all(isinstance(chunk, FrameChunk) for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks if isinstance(chunk, FrameChunk)] == [
        0,
        1,
        2,
    ]
    assert {chunk.chunk_count for chunk in chunks if isinstance(chunk, FrameChunk)} == {
        3
    }


@pytest.mark.parametrize("token", [b"", bytes(15), bytes(17)])
def test_encoders_reject_invalid_token_length(token: bytes) -> None:
    with pytest.raises(ValueError, match="token"):
        encode_hello(token)
    with pytest.raises(ValueError, match="token"):
        encode_credit(token, 1)
    with pytest.raises(ValueError, match="token"):
        split_frame(token=token, sequence=1, frame=b"x")


@pytest.mark.parametrize("credits", [0, 5, -1, True])
def test_credit_encoder_rejects_values_outside_one_to_four(
    credits: int,
) -> None:
    with pytest.raises(ValueError, match="credits"):
        encode_credit(TOKEN, credits)


@pytest.mark.parametrize(
    ("datagram", "message"),
    [
        (b"", "truncated"),
        (b"BAD!" + HELLO_DATAGRAM[4:], "magic"),
        (HELLO_DATAGRAM[:4] + b"\x02" + HELLO_DATAGRAM[5:], "version"),
        (HELLO_DATAGRAM[:5] + b"\x09" + HELLO_DATAGRAM[6:], "kind"),
        (HELLO_DATAGRAM + b"x", "length"),
        (CREDIT_DATAGRAM[:-1], "length"),
        (CREDIT_DATAGRAM[:-1] + b"\x00", "credits"),
        (FRAME_DATAGRAM[:30], "truncated"),
    ],
)
def test_parse_datagram_rejects_invalid_envelopes(
    datagram: bytes,
    message: str,
) -> None:
    with pytest.raises(CameraDatagramProtocolError, match=message):
        parse_datagram(datagram)


def test_split_frame_rejects_empty_or_oversized_frame() -> None:
    with pytest.raises(ValueError, match="frame"):
        split_frame(token=TOKEN, sequence=1, frame=b"")
    with pytest.raises(ValueError, match="frame"):
        split_frame(
            token=TOKEN,
            sequence=1,
            frame=bytes(5 * 1024 * 1024 + 1),
        )


def test_parse_frame_rejects_inconsistent_chunk_count() -> None:
    datagram = bytearray(FRAME_DATAGRAM)
    datagram[28:30] = b"\x00\x02"

    with pytest.raises(CameraDatagramProtocolError, match="chunk count"):
        parse_datagram(bytes(datagram))


def test_parse_datagram_rejects_payload_above_mtu() -> None:
    oversized = FRAME_DATAGRAM + bytes(1_200 - len(FRAME_DATAGRAM) + 1)

    with pytest.raises(CameraDatagramProtocolError, match="1200"):
        parse_datagram(oversized)


def test_assembler_completes_one_frame_from_out_of_order_chunks() -> None:
    assembler = LatestFrameAssembler(max_age_ms=500)
    frame = bytes(range(256)) * 12
    chunks = split_frame(token=TOKEN, sequence=8, frame=frame)

    completed = None
    for chunk in reversed(chunks):
        completed = assembler.push(chunk, now_ms=1)

    assert completed == frame
    assert assembler.status() == {
        "pending": False,
        "completed_frames": 1,
        "replaced_incomplete_frames": 0,
        "stale_chunks": 0,
        "expired_frames": 0,
        "invalid_frames": 0,
    }


def test_assembler_counts_duplicate_chunk_only_once() -> None:
    assembler = LatestFrameAssembler()
    chunks = split_frame(token=TOKEN, sequence=2, frame=bytes(2_000))

    assert assembler.push(chunks[0], now_ms=0) is None
    assert assembler.push(chunks[0], now_ms=1) is None
    assert assembler.push(chunks[1], now_ms=2) == bytes(2_000)
    assert assembler.status()["completed_frames"] == 1


def test_assembler_replaces_incomplete_frame_with_newer_sequence() -> None:
    assembler = LatestFrameAssembler()
    old = split_frame(token=TOKEN, sequence=8, frame=bytes(2_000))
    new = split_frame(token=TOKEN, sequence=9, frame=b"new")

    assert assembler.push(old[0], now_ms=0) is None
    assert assembler.push(new[0], now_ms=1) == b"new"
    assert assembler.push(old[1], now_ms=2) is None
    assert assembler.status() == {
        "pending": False,
        "completed_frames": 1,
        "replaced_incomplete_frames": 1,
        "stale_chunks": 1,
        "expired_frames": 0,
        "invalid_frames": 0,
    }


def test_assembler_expires_incomplete_frame_without_background_worker() -> None:
    assembler = LatestFrameAssembler(max_age_ms=500)
    expired = split_frame(token=TOKEN, sequence=4, frame=bytes(2_000))
    newest = split_frame(token=TOKEN, sequence=5, frame=b"newest")

    assert assembler.push(expired[0], now_ms=0) is None
    assert assembler.push(newest[0], now_ms=501) == b"newest"
    assert assembler.status()["expired_frames"] == 1
    assert assembler.status()["replaced_incomplete_frames"] == 0


def test_assembler_rejects_crc_mismatch_and_continues_with_next_frame() -> None:
    assembler = LatestFrameAssembler()
    corrupt = bytearray(split_frame(token=TOKEN, sequence=6, frame=b"bad")[0])
    corrupt[-1] ^= 0x01

    assert assembler.push(bytes(corrupt), now_ms=0) is None
    assert assembler.status()["invalid_frames"] == 1
    assert assembler.push(
        split_frame(token=TOKEN, sequence=7, frame=b"good")[0],
        now_ms=1,
    ) == b"good"


def test_assembler_reset_clears_frame_bytes_and_counters() -> None:
    assembler = LatestFrameAssembler()
    chunks = split_frame(token=TOKEN, sequence=3, frame=bytes(2_000))
    assembler.push(chunks[0], now_ms=0)

    assembler.reset()

    assert assembler.status() == {
        "pending": False,
        "completed_frames": 0,
        "replaced_incomplete_frames": 0,
        "stale_chunks": 0,
        "expired_frames": 0,
        "invalid_frames": 0,
    }


def test_session_binds_only_matching_token_and_source_ip() -> None:
    session = CameraDatagramSession(token=TOKEN, expected_ip="127.0.0.1")

    assert session.accept(
        encode_hello(bytes(reversed(TOKEN))),
        ("127.0.0.1", 41_000),
        now_ms=0,
    ) is None
    assert session.ready is False
    assert session.accept(
        encode_hello(TOKEN),
        ("127.0.0.2", 41_000),
        now_ms=1,
    ) is None
    assert session.ready is False
    assert session.accept(
        encode_hello(TOKEN),
        ("127.0.0.1", 41_000),
        now_ms=2,
    ) is None
    assert session.ready is True
    assert session.peer == ("127.0.0.1", 41_000)
    assert session.status()["source_mismatch_packets"] == 1


def test_session_pins_first_authenticated_hello_when_control_source_is_proxied() -> None:
    session = CameraDatagramSession(token=TOKEN, expected_ip=None)

    assert session.accept(
        encode_hello(bytes(reversed(TOKEN))),
        ("192.0.2.20", 41_000),
        now_ms=0,
    ) is None
    assert session.ready is False

    assert session.accept(
        encode_hello(TOKEN),
        ("192.0.2.20", 41_000),
        now_ms=1,
    ) is None
    assert session.ready is True
    assert session.peer == ("192.0.2.20", 41_000)
    session.begin_stream()

    frame = split_frame(token=TOKEN, sequence=1, frame=b"frame")[0]
    assert session.accept(frame, ("192.0.2.21", 41_000), now_ms=2) is None
    assert session.accept(frame, ("192.0.2.20", 41_000), now_ms=3) == b"frame"
    assert session.status()["source_mismatch_packets"] == 1


def test_session_rejects_different_endpoint_after_binding() -> None:
    session = CameraDatagramSession(token=TOKEN, expected_ip="127.0.0.1")
    session.accept(encode_hello(TOKEN), ("127.0.0.1", 41_000), now_ms=0)
    session.begin_stream()
    frame = split_frame(token=TOKEN, sequence=1, frame=b"frame")[0]

    assert session.accept(frame, ("127.0.0.1", 41_001), now_ms=1) is None
    assert session.accept(frame, ("127.0.0.1", 41_000), now_ms=2) == b"frame"
    assert session.status()["source_mismatch_packets"] == 1


def test_session_begin_stream_accepts_device_sequence_restart() -> None:
    session = CameraDatagramSession(token=TOKEN, expected_ip="127.0.0.1")
    peer = ("127.0.0.1", 41_000)
    session.accept(encode_hello(TOKEN), peer, now_ms=0)
    session.begin_stream()
    frame_40 = split_frame(token=TOKEN, sequence=40, frame=b"previous")[0]
    assert session.accept(frame_40, peer, now_ms=1) == b"previous"

    session.begin_stream()

    frame_1 = split_frame(token=TOKEN, sequence=1, frame=b"current")[0]
    assert session.accept(frame_1, peer, now_ms=2) == b"current"
    assert session.status()["completed_frames"] == 1
    assert session.status()["stale_chunks"] == 0


def test_session_end_stream_discards_pending_and_rejects_frame_chunks() -> None:
    session = CameraDatagramSession(token=TOKEN, expected_ip="127.0.0.1")
    peer = ("127.0.0.1", 41_000)
    session.accept(encode_hello(TOKEN), peer, now_ms=0)
    session.begin_stream()
    assert session.accept(
        split_frame(token=TOKEN, sequence=4, frame=b"complete")[0],
        peer,
        now_ms=1,
    ) == b"complete"
    pending = split_frame(token=TOKEN, sequence=5, frame=bytes(2_000))
    assert session.accept(pending[0], peer, now_ms=2) is None
    assert session.status()["pending"] is True

    session.end_stream()

    assert session.status()["pending"] is False
    assert session.status()["completed_frames"] == 1
    assert session.accept(
        split_frame(token=TOKEN, sequence=6, frame=b"inactive")[0],
        peer,
        now_ms=3,
    ) is None
    assert session.status()["completed_frames"] == 1

    session.begin_stream()
    assert session.accept(
        split_frame(token=TOKEN, sequence=1, frame=b"restarted")[0],
        peer,
        now_ms=4,
    ) == b"restarted"


def test_session_counts_malformed_frame_from_bound_endpoint() -> None:
    session = CameraDatagramSession(token=TOKEN, expected_ip="127.0.0.1")
    peer = ("127.0.0.1", 41_000)
    session.accept(encode_hello(TOKEN), peer, now_ms=0)
    session.begin_stream()
    malformed = split_frame(token=TOKEN, sequence=1, frame=b"frame")[0][:-1]

    assert session.accept(malformed, peer, now_ms=1) is None
    assert session.status()["invalid_frames"] == 1


def test_session_sends_credit_only_to_bound_endpoint() -> None:
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class RecordingEndpoint:
        def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
            sent.append((data, addr))

    session = CameraDatagramSession(token=TOKEN, expected_ip="127.0.0.1")
    with pytest.raises(ConnectionError, match="not ready"):
        session.send_credit(RecordingEndpoint(), 4)  # type: ignore[arg-type]

    session.accept(
        encode_hello(TOKEN),
        ("127.0.0.1", 41_000),
        now_ms=0,
    )
    session.send_credit(RecordingEndpoint(), 4)  # type: ignore[arg-type]

    assert sent == [(CREDIT_DATAGRAM, ("127.0.0.1", 41_000))]


@pytest.mark.asyncio
async def test_session_wait_ready_and_close_clear_state() -> None:
    session = CameraDatagramSession(token=TOKEN, expected_ip="127.0.0.1")
    waiter = asyncio.create_task(session.wait_ready(timeout_s=0.1))
    await asyncio.sleep(0)

    session.accept(encode_hello(TOKEN), ("127.0.0.1", 41_000), now_ms=0)
    await waiter
    session.begin_stream()
    session.accept(
        split_frame(token=TOKEN, sequence=2, frame=bytes(2_000))[0],
        ("127.0.0.1", 41_000),
        now_ms=1,
    )

    session.close()

    assert session.ready is False
    assert session.peer is None
    assert session.status() == {
        "ready": False,
        "pending": False,
        "completed_frames": 0,
        "replaced_incomplete_frames": 0,
        "stale_chunks": 0,
        "expired_frames": 0,
        "invalid_frames": 0,
        "source_mismatch_packets": 0,
    }


@pytest.mark.asyncio
async def test_session_wait_ready_times_out() -> None:
    session = CameraDatagramSession(token=TOKEN, expected_ip="127.0.0.1")

    with pytest.raises(TimeoutError):
        await session.wait_ready(timeout_s=0.001)
