from __future__ import annotations

import asyncio

import pytest

from stackchan_mcp.face_follow import (
    AttentionEffect,
    AttentionState,
    FaceFollowMetrics,
    FaceFollowService,
    HeadPose,
    TargetObservation,
    advance_attention,
)
from stackchan_mcp.camera_stream import CameraFrame
from stackchan_mcp.face_follow_detector import AttentionDetection, BoundingBox


def _detection(center_x: float, center_y: float) -> AttentionDetection:
    return AttentionDetection(
        label="face",
        confidence=0.9,
        bounding_box=BoundingBox(
            center_x - 0.05,
            center_y - 0.05,
            center_x + 0.05,
            center_y + 0.05,
        ),
    )


def test_controller_uses_confirmed_pose_dead_zone_gain_direction_and_four_degrees() -> None:
    transition = advance_attention(
        AttentionState(),
        now_ms=1_000,
        observed_at_ms=990,
        current_pose=HeadPose(yaw=10, pitch=30),
        detections=[_detection(0.8, 0.8)],
    )

    assert transition.state.mode == "track"
    assert transition.state.last_target_at_ms == 990
    assert transition.target is not None
    assert transition.target.horizontal_error == pytest.approx(0.3)
    assert transition.effect.kind == "move"
    assert transition.effect.pose == HeadPose(yaw=14, pitch=27)


def test_controller_holds_inside_release_zone_after_becoming_centered() -> None:
    centered = advance_attention(
        AttentionState(),
        now_ms=100,
        observed_at_ms=100,
        current_pose=HeadPose(0, 33),
        detections=[_detection(0.5, 0.5)],
    )
    still_centered = advance_attention(
        centered.state,
        now_ms=225,
        observed_at_ms=225,
        current_pose=HeadPose(0, 33),
        detections=[_detection(0.63, 0.5)],
    )

    assert centered.state.centered is True
    assert still_centered.state.centered is True
    assert still_centered.effect.kind == "hold"


def test_controller_rounds_small_corrections_and_never_crosses_pitch_23() -> None:
    rounded = advance_attention(
        AttentionState(),
        now_ms=100,
        observed_at_ms=100,
        current_pose=HeadPose(0, 24),
        detections=[_detection(0.62, 0.9)],
    )

    assert rounded.effect.kind == "move"
    assert rounded.effect.pose == HeadPose(yaw=1, pitch=23)


def test_controller_holds_for_749ms_then_recovers_home_at_750ms() -> None:
    tracked = advance_attention(
        AttentionState(),
        now_ms=100,
        observed_at_ms=100,
        current_pose=HeadPose(20, 40),
        detections=[_detection(0.5, 0.5)],
    )
    held = advance_attention(
        tracked.state,
        now_ms=849,
        current_pose=HeadPose(20, 40),
        detections=[],
    )
    recovered = advance_attention(
        held.state,
        now_ms=850,
        current_pose=HeadPose(20, 40),
        detections=[],
    )

    assert held.state.mode == "hold"
    assert held.effect == AttentionEffect("hold")
    assert recovered.state.mode == "recover"
    assert recovered.effect == AttentionEffect("move", HeadPose(0, 33))
    assert recovered.state.last_target_at_ms == 100


def test_controller_holds_at_home_after_loss_recovery() -> None:
    tracked = advance_attention(
        AttentionState(),
        now_ms=100,
        observed_at_ms=100,
        current_pose=HeadPose(20, 40),
        detections=[_detection(0.5, 0.5)],
    )
    recovered = advance_attention(
        tracked.state,
        now_ms=850,
        current_pose=HeadPose(0, 33),
        detections=[],
    )

    assert recovered.state.mode == "recover"
    assert recovered.effect == AttentionEffect("hold")


def test_controller_reacquires_from_confirmed_pose_during_home_recovery() -> None:
    tracked = advance_attention(
        AttentionState(),
        now_ms=100,
        observed_at_ms=100,
        current_pose=HeadPose(20, 40),
        detections=[_detection(0.5, 0.5)],
    )
    recovered = advance_attention(
        tracked.state,
        now_ms=850,
        current_pose=HeadPose(20, 40),
        detections=[],
    )
    reacquired = advance_attention(
        recovered.state,
        now_ms=900,
        observed_at_ms=900,
        current_pose=HeadPose(16, 36),
        detections=[_detection(0.9, 0.9)],
    )

    assert reacquired.state.mode == "track"
    assert reacquired.effect == AttentionEffect("move", HeadPose(20, 32))


def test_controller_stays_in_acquire_without_a_target() -> None:
    transition = advance_attention(
        AttentionState(),
        now_ms=10_000,
        current_pose=HeadPose(0, 33),
        detections=[],
    )

    assert transition.state.mode == "acquire"
    assert transition.effect == AttentionEffect("hold")


def test_controller_rebases_each_move_on_supplied_confirmed_pose() -> None:
    first = advance_attention(
        AttentionState(),
        now_ms=100,
        observed_at_ms=100,
        current_pose=HeadPose(0, 33),
        detections=[_detection(0.9, 0.5)],
    )
    second = advance_attention(
        first.state,
        now_ms=225,
        observed_at_ms=225,
        current_pose=HeadPose(0, 33),
        detections=[_detection(0.9, 0.5)],
    )

    assert first.effect.pose == HeadPose(4, 33)
    assert second.effect.pose == HeadPose(4, 33)


def test_metrics_decompose_initial_acquisition_and_longest_reacquisition_gap() -> None:
    metrics = FaceFollowMetrics(started_at_ms=0)
    metrics.record_outcome("frame_wait_timeout", now_ms=125)
    metrics.record_outcome("no_candidate", now_ms=250)
    metrics.record_outcome("target_selected", now_ms=375)
    metrics.record_outcome("no_candidate", now_ms=500)
    metrics.record_outcome("association_rejected", now_ms=625)
    metrics.record_outcome("target_selected", now_ms=875)
    metrics.record_outcome("frame_stale", now_ms=1_000)
    metrics.record_outcome("target_selected", now_ms=1_125)
    metrics.add_stage("inference_ms", 20.1)

    status = metrics.status(now_ms=1_125)

    assert status["outcomes"] == {
        "target_selected": 3,
        "no_candidate": 2,
        "association_rejected": 1,
        "frame_wait_timeout": 1,
        "frame_stale": 1,
        "inference_error": 0,
        "tick_overlap_suppressed": 0,
    }
    assert status["initial_acquisition"] == {
        "target_acquired": True,
        "elapsed_ms": 375,
        "outcomes": {"frame_wait_timeout": 1, "no_candidate": 1},
    }
    assert status["reacquisition"] == {
        "episodes": 2,
        "longest_gap_ms": 500,
        "longest_outcomes": {
            "no_candidate": 1,
            "association_rejected": 1,
        },
        "current_gap_ms": 0,
        "current_outcomes": {},
    }
    assert status["stage_ms"]["inference_ms"] == {
        "count": 1,
        "p50": 21,
        "p95": 21,
        "p99": 21,
        "max": 21,
    }


def test_metrics_record_vertical_loss_origin_and_axis_step_limits_once() -> None:
    metrics = FaceFollowMetrics(started_at_ms=0)
    target = TargetObservation(
        label="face",
        confidence=0.9,
        center_x=0.5,
        center_y=0.1,
        horizontal_error=0.0,
        vertical_error=-0.4,
        centered=False,
    )
    metrics.record_target_diagnostics(
        target,
        current_pose=HeadPose(0, 33),
        effect=AttentionEffect("move", HeadPose(0, 37)),
    )
    metrics.record_outcome("target_selected", now_ms=100)
    metrics.record_outcome("no_candidate", now_ms=200)
    metrics.record_outcome("no_candidate", now_ms=300)

    geometry = metrics.status(now_ms=300)["target_geometry"]

    assert geometry == {
        "vertical_center_buckets": {
            "0_20": 1,
            "20_40": 0,
            "40_60": 0,
            "60_80": 0,
            "80_100": 0,
        },
        "loss_start_vertical_buckets": {
            "0_20": 1,
            "20_40": 0,
            "40_60": 0,
            "60_80": 0,
            "80_100": 0,
        },
        "yaw_step_limit_frames": 0,
        "pitch_step_limit_frames": 1,
        "loss_after_pitch_step_limit": 1,
        "last_target": {
            "current_pitch": 33,
            "commanded_pitch": 37,
            "yaw_step": 0,
            "pitch_step": 4,
        },
    }


class FakeDetector:
    def __init__(self, detections: list[AttentionDetection] | None = None) -> None:
        self.detections = detections or []
        self.frames: list[bytes] = []

    async def detect(self, jpeg: bytes) -> list[AttentionDetection]:
        self.frames.append(jpeg)
        return self.detections

    def status(self) -> dict[str, int]:
        return {"frames": len(self.frames)}


class FakeCameraStream:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events
        self.running = False

    async def acquire(self, *, fps: int, quality: int) -> dict[str, object]:
        self.events.append(("camera_acquire", fps, quality))
        self.running = True
        return self.status()

    async def release(self) -> dict[str, object]:
        self.events.append(("camera_release",))
        self.running = False
        return self.status()

    def touch(self) -> None:
        self.events.append(("camera_touch",))

    def status(self) -> dict[str, object]:
        return {"running": self.running}


class FakeFrameStore:
    def __init__(self, frames: list[CameraFrame | None] | None = None) -> None:
        self.frames = frames or []
        self.after_sequences: list[int | None] = []

    async def wait_for_frame(
        self, *, after_sequence: int | None, timeout_s: float
    ) -> CameraFrame | None:
        assert timeout_s == pytest.approx(0.100)
        self.after_sequences.append(after_sequence)
        return self.frames.pop(0) if self.frames else None

    def status(self) -> dict[str, object]:
        return {"available": bool(self.frames)}


class FakeHeadLane:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events
        self.lease_id = "face-follow-lease"
        self.pose = {"yaw": 0, "pitch": 33}
        self.running = False
        self.updates: list[tuple[str, int, int, int]] = []
        self.clears = 0

    async def start(self, **config: object) -> dict[str, object]:
        self.events.append(("lane_start", config))
        self.running = True
        return self.status(self.lease_id)

    async def update(
        self, lease_id: str, sequence: int, yaw: int, pitch: int
    ) -> dict[str, object]:
        self.events.append(("lane_update", sequence, yaw, pitch))
        self.updates.append((lease_id, sequence, yaw, pitch))
        return {
            "accepted": True,
            "replaced": False,
            "pending_depth": 1,
            "accepted_count": len(self.updates),
            "replaced_count": 0,
            "last_accepted_sequence": sequence,
        }

    async def clear(self, lease_id: str) -> dict[str, object]:
        assert lease_id == self.lease_id
        self.events.append(("lane_clear",))
        self.clears += 1
        return self.status(lease_id)

    async def stop(self, lease_id: str | None = None) -> dict[str, object]:
        assert lease_id in (None, self.lease_id)
        self.events.append(("lane_stop",))
        self.running = False
        return self.status(lease_id)

    def status(self, lease_id: str | None = None) -> dict[str, object]:
        return {
            "phase": "running" if self.running else "stopped",
            "lease_id": self.lease_id if self.running else None,
            "lease_id_match": lease_id == self.lease_id if lease_id else None,
            "confirmed_pose": dict(self.pose),
            "accepted": len(self.updates),
            "replaced": 0,
            "dispatched": len(self.updates),
            "confirmed": len(self.updates),
            "failed": 0,
            "stale_discarded": 0,
            "active_calls": 0,
            "pending_depth": 0,
            "maximum_active_calls": min(1, len(self.updates)),
            "maximum_pending_depth": min(1, len(self.updates)),
            "post_stop_dispatches": 0,
        }


class PendingHeadLane(FakeHeadLane):
    def status(self, lease_id: str | None = None) -> dict[str, object]:
        status = super().status(lease_id)
        status["pending_depth"] = int(bool(self.updates))
        return status


class FakeDevice:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events

    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> tuple[dict[str, object], None]:
        self.events.append(("device", name, dict(arguments)))
        return {"ok": True}, None


def _frame(*, sequence: int, received_monotonic_ms: int) -> CameraFrame:
    return CameraFrame(
        sequence=sequence,
        device_id="stackchan",
        captured_at_ms=0,
        encoded_at_ms=0,
        received_at_ms=0,
        width=320,
        height=240,
        quality=60,
        jpeg=b"jpeg",
        received_monotonic_ms=received_monotonic_ms,
        gateway_sequence=sequence,
    )


async def _blocking_loop_sleep(seconds: float) -> None:
    if seconds == pytest.approx(0.25):
        return
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_service_owns_fixed_camera_lane_and_safe_home_lifecycle() -> None:
    events: list[tuple[object, ...]] = []
    camera = FakeCameraStream(events)
    frames = FakeFrameStore()
    lane = FakeHeadLane(events)
    device = FakeDevice(events)
    detector = FakeDetector()
    service = FaceFollowService(
        device=device,
        camera_stream=camera,
        frames=frames,
        head_lane=lane,
        model_path=lambda: "/configured/model.onnx",
        detector_loader=lambda path: detector if path.endswith(".onnx") else None,
        sleep=_blocking_loop_sleep,
    )

    started = await service.start()
    await asyncio.sleep(0)

    assert started["phase"] == "running"
    assert events[:3] == [
        (
            "device",
            "self.robot.set_head_angles",
            {"yaw": 0, "pitch": 33, "speed_dps": 90},
        ),
        ("camera_acquire", 20, 60),
        (
            "lane_start",
            {
                "rate_hz": 10.0,
                "max_step_deg": 4.0,
                "max_pending_age_ms": 180,
                "speed_dps": 90,
            },
        ),
    ]

    stopped = await service.stop()
    assert stopped["phase"] == "stopped"
    assert [(item[0], item[2].get("pitch") if item[0] == "device" else None) for item in events[-4:]] == [
        ("lane_stop", None),
        ("device", 37),
        ("device", 33),
        ("camera_release", None),
    ]
    assert stopped["safety"]["home_commanded"] is True
    assert stopped["camera"]["running"] is False
    assert await service.stop() == stopped


@pytest.mark.asyncio
async def test_service_load_failure_touches_no_hardware_resource() -> None:
    events: list[tuple[object, ...]] = []

    def fail_loader(_path: str) -> FakeDetector:
        raise RuntimeError("bad model")

    service = FaceFollowService(
        device=FakeDevice(events),
        camera_stream=FakeCameraStream(events),
        frames=FakeFrameStore(),
        head_lane=FakeHeadLane(events),
        model_path=lambda: "/bad/model.onnx",
        detector_loader=fail_loader,
        sleep=_blocking_loop_sleep,
    )

    with pytest.raises(RuntimeError, match="bad model"):
        await service.start()

    assert events == []
    assert service.status()["phase"] == "stopped"


@pytest.mark.asyncio
async def test_service_tick_uses_latest_fresh_frame_and_submits_one_absolute_target() -> None:
    events: list[tuple[object, ...]] = []
    lane = FakeHeadLane(events)
    detector = FakeDetector([_detection(0.9, 0.5)])
    service = FaceFollowService(
        device=FakeDevice(events),
        camera_stream=FakeCameraStream(events),
        frames=FakeFrameStore([_frame(sequence=7, received_monotonic_ms=1_000)]),
        head_lane=lane,
        model_path=lambda: "/configured/model.onnx",
        detector_loader=lambda _path: detector,
        monotonic=lambda: 1.100,
        sleep=_blocking_loop_sleep,
    )
    await service.start()

    await service._run_tick(service._generation)

    assert detector.frames == [b"jpeg"]
    assert lane.updates == [(lane.lease_id, 1, 4, 33)]
    status = service.status()
    assert status["frames_processed"] == 1
    assert status["target_frames"] == 1
    assert status["metrics"]["outcomes"]["target_selected"] == 1
    assert status["metrics"]["target_geometry"]["vertical_center_buckets"]["40_60"] == 1
    assert status["metrics"]["target_geometry"]["yaw_step_limit_frames"] == 1
    assert status["detector"] == {"frames": 1}
    assert status["flow"]["maximum_active_observation_ticks"] == 1
    await service.stop()


@pytest.mark.asyncio
async def test_service_classifies_timeout_stale_and_no_candidate_separately() -> None:
    events: list[tuple[object, ...]] = []
    frames = FakeFrameStore(
        [None, _frame(sequence=1, received_monotonic_ms=800), _frame(sequence=2, received_monotonic_ms=1_050)]
    )
    service = FaceFollowService(
        device=FakeDevice(events),
        camera_stream=FakeCameraStream(events),
        frames=frames,
        head_lane=FakeHeadLane(events),
        model_path=lambda: "/configured/model.onnx",
        detector_loader=lambda _path: FakeDetector(),
        monotonic=lambda: 1.100,
        sleep=_blocking_loop_sleep,
    )
    await service.start()

    await service._run_tick(service._generation)
    await service._run_tick(service._generation)
    await service._run_tick(service._generation)

    outcomes = service.status()["metrics"]["outcomes"]
    assert outcomes["frame_wait_timeout"] == 1
    assert outcomes["frame_stale"] == 1
    assert outcomes["no_candidate"] == 1
    assert outcomes["association_rejected"] == 0
    await service.stop()


@pytest.mark.parametrize(
    "loss_outcome",
    ["frame_wait_timeout", "frame_stale", "inference_error"],
)
@pytest.mark.asyncio
async def test_service_recovers_home_when_observations_are_unavailable(
    loss_outcome: str,
) -> None:
    events: list[tuple[object, ...]] = []
    clock_ms = 100

    class Detector:
        def __init__(self) -> None:
            self.calls = 0

        async def detect(self, _jpeg: bytes) -> list[AttentionDetection]:
            self.calls += 1
            if self.calls == 2 and loss_outcome == "inference_error":
                raise RuntimeError("inference failed")
            return [_detection(0.5, 0.5)]

    loss_frame = {
        "frame_wait_timeout": None,
        "frame_stale": _frame(sequence=2, received_monotonic_ms=600),
        "inference_error": _frame(sequence=2, received_monotonic_ms=850),
    }[loss_outcome]
    lane = FakeHeadLane(events)
    lane.pose = {"yaw": 20, "pitch": 40}
    service = FaceFollowService(
        device=FakeDevice(events),
        camera_stream=FakeCameraStream(events),
        frames=FakeFrameStore(
            [_frame(sequence=1, received_monotonic_ms=100), loss_frame]
        ),
        head_lane=lane,
        model_path=lambda: "/configured/model.onnx",
        detector_loader=lambda _path: Detector(),
        monotonic=lambda: clock_ms / 1_000,
        sleep=_blocking_loop_sleep,
    )
    await service.start()
    await service._run_tick(service._generation)

    clock_ms = 850
    await service._run_tick(service._generation)

    status = service.status()
    assert lane.updates == [(lane.lease_id, 1, 0, 33)]
    assert status["attention"] == {"mode": "recover", "target_visible": False}
    assert status["metrics"]["outcomes"][loss_outcome] == 1
    await service.stop()


@pytest.mark.asyncio
async def test_service_coalesces_a_reversed_target_into_the_pending_lane() -> None:
    events: list[tuple[object, ...]] = []
    clock_ms = 100
    lane = PendingHeadLane(events)

    class ReversingDetector:
        def __init__(self) -> None:
            self.calls = 0

        async def detect(self, _jpeg: bytes) -> list[AttentionDetection]:
            self.calls += 1
            return [_detection(0.9 if self.calls == 1 else 0.1, 0.5)]

    service = FaceFollowService(
        device=FakeDevice(events),
        camera_stream=FakeCameraStream(events),
        frames=FakeFrameStore(
            [
                _frame(sequence=1, received_monotonic_ms=100),
                _frame(sequence=2, received_monotonic_ms=200),
            ]
        ),
        head_lane=lane,
        model_path=lambda: "/configured/model.onnx",
        detector_loader=lambda _path: ReversingDetector(),
        monotonic=lambda: clock_ms / 1_000,
        sleep=_blocking_loop_sleep,
    )
    await service.start()
    await service._run_tick(service._generation)

    clock_ms = 200
    await service._run_tick(service._generation)

    assert lane.updates == [
        (lane.lease_id, 1, 4, 33),
        (lane.lease_id, 2, -4, 33),
    ]
    assert service.status()["metrics"]["stage_ms"]["capture_to_command_ms"][
        "count"
    ] == 2
    await service.stop()


@pytest.mark.asyncio
async def test_service_stop_drains_inference_before_any_post_stop_dispatch() -> None:
    events: list[tuple[object, ...]] = []
    inference_started = asyncio.Event()
    inference_release = asyncio.Event()

    class BlockingDetector:
        async def detect(self, _jpeg: bytes) -> list[AttentionDetection]:
            inference_started.set()
            await inference_release.wait()
            return [_detection(0.9, 0.5)]

    async def immediate_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    lane = FakeHeadLane(events)
    service = FaceFollowService(
        device=FakeDevice(events),
        camera_stream=FakeCameraStream(events),
        frames=FakeFrameStore([_frame(sequence=3, received_monotonic_ms=1_000)]),
        head_lane=lane,
        model_path=lambda: "/configured/model.onnx",
        detector_loader=lambda _path: BlockingDetector(),
        monotonic=lambda: 1.100,
        sleep=immediate_sleep,
    )
    await service.start()
    await asyncio.wait_for(inference_started.wait(), timeout=1)

    stop_task = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    assert stop_task.done() is False
    inference_release.set()
    stopped = await asyncio.wait_for(stop_task, timeout=1)

    assert lane.updates == []
    assert stopped["head_target_lane"]["post_stop_dispatches"] == 0
    assert stopped["flow"]["maximum_active_observation_ticks"] == 1


@pytest.mark.asyncio
async def test_service_stop_finishes_cleanup_when_caller_is_cancelled() -> None:
    events: list[tuple[object, ...]] = []
    inference_started = asyncio.Event()
    inference_release = asyncio.Event()

    class BlockingDetector:
        async def detect(self, _jpeg: bytes) -> list[AttentionDetection]:
            inference_started.set()
            await inference_release.wait()
            return [_detection(0.9, 0.5)]

    async def immediate_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    service = FaceFollowService(
        device=FakeDevice(events),
        camera_stream=FakeCameraStream(events),
        frames=FakeFrameStore([_frame(sequence=3, received_monotonic_ms=1_000)]),
        head_lane=FakeHeadLane(events),
        model_path=lambda: "/configured/model.onnx",
        detector_loader=lambda _path: BlockingDetector(),
        monotonic=lambda: 1.100,
        sleep=immediate_sleep,
    )
    await service.start()
    await asyncio.wait_for(inference_started.wait(), timeout=1)

    stop_task = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    stop_task.cancel()
    inference_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stop_task, timeout=1)
    status = service.status()
    assert status["phase"] == "stopped"
    assert status["camera"]["running"] is False
    assert status["safety"]["home_commanded"] is True
