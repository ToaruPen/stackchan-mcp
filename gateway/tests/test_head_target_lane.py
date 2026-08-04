from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from stackchan_mcp import wifi_power_save
from stackchan_mcp.head_target_lane import HeadTargetLane


@pytest.fixture(autouse=True)
def clear_wifi_power_save_state() -> None:
    wifi_power_save._clear_for_tests()
    yield
    wifi_power_save._clear_for_tests()


def _tool_result(payload: dict[str, object]) -> dict[str, object]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload),
            }
        ]
    }


class GatedDevice:
    def __init__(
        self,
        *,
        seed: dict[str, int],
        gate_first_move: bool = True,
        gate_all_moves: bool = False,
        fail_move_numbers: set[int] | None = None,
        motion_state: dict[str, object] | None = None,
        move_payloads: dict[int, dict[str, object]] | None = None,
        move_outer_stages: dict[int, dict[str, object]] | None = None,
        move_gates: dict[int, asyncio.Event] | None = None,
        acquire_gate: asyncio.Event | None = None,
    ) -> None:
        self.now = 0.0
        self.seed = seed
        self.fail_move_numbers = fail_move_numbers or set()
        self.motion_state = motion_state or {
            "yaw": seed["yaw"],
            "pitch": seed["pitch"],
            "target_yaw": seed["yaw"],
            "target_pitch": seed["pitch"],
            "moving": False,
        }
        self.move_payloads = move_payloads or {}
        self.move_outer_stages = move_outer_stages or {}
        self.move_gates = move_gates or {}
        self.acquire_gate = acquire_gate
        self.acquire_started = asyncio.Event()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.moves: list[tuple[str, dict[str, Any]]] = []
        self.move_started_at: list[float] = []
        self.move_started = asyncio.Event()
        self._move_release = asyncio.Event()
        self._gate_first_move = gate_first_move
        self._gate_all_moves = gate_all_moves
        self.head_target_lane_acquired = False
        if not gate_first_move:
            self._move_release.set()

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, object], None]:
        copied = (name, dict(arguments))
        self.calls.append(copied)

        if name == "self.robot.get_head_angles":
            if arguments == {"cached_motion_state": True}:
                return _tool_result(self.motion_state), None
            return _tool_result(self.seed), None
        if name == "self.wifi.set_power_save":
            mode = arguments["mode"]
            previous = "max_modem" if mode == "none" else "none"
            return _tool_result(
                {"ok": True, "previous": previous, "current": mode}
            ), None
        if name != "self.robot.set_head_angles":
            raise AssertionError(f"unexpected device tool: {name}")

        self.moves.append(copied)
        move_number = len(self.moves)
        self.move_started_at.append(self.now)
        move_gate = self.move_gates.get(move_number)
        if move_gate is not None:
            self.move_started.set()
            await move_gate.wait()
        elif self._gate_all_moves or self._gate_first_move:
            self._gate_first_move = False
            self.move_started.set()
            await self._move_release.wait()
        if move_number in self.fail_move_numbers:
            return _tool_result({"ok": False}), None
        payload = self.move_payloads.get(
            move_number,
            {"ok": True, "servo_ok": True},
        )
        result = _tool_result(payload)
        outer_stage = self.move_outer_stages.get(move_number)
        if outer_stage is not None:
            result["mcpStageUs"] = outer_stage
        return result, None

    async def acquire_head_target_lane(self) -> None:
        assert not self.head_target_lane_acquired
        self.acquire_started.set()
        if self.acquire_gate is not None:
            await self.acquire_gate.wait()
        self.head_target_lane_acquired = True

    async def release_head_target_lane(self) -> None:
        assert self.head_target_lane_acquired
        self.head_target_lane_acquired = False

    async def read_head_target_pose(
        self,
    ) -> tuple[dict[str, object], None]:
        assert self.head_target_lane_acquired
        return _tool_result(self.seed), None

    async def call_head_target_tool(
        self,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, object], None]:
        assert self.head_target_lane_acquired
        return await self.call_tool("self.robot.set_head_angles", arguments)

    def finish_move(self) -> None:
        self._move_release.set()

    def advance_ms(self, milliseconds: int) -> None:
        self.now += milliseconds / 1000


def create_lane(device: GatedDevice) -> HeadTargetLane:
    return HeadTargetLane(
        device,
        monotonic_now=device.clock,
        sleep=device.sleep,
        lease_id_factory=lambda: "lease-a",
    )


@pytest.mark.asyncio
async def test_update_acknowledges_before_device_and_replaces_pending() -> None:
    device = GatedDevice(seed={"yaw": 0, "pitch": 33})
    lane = create_lane(device)
    started = await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    first = await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    second = await lane.update("lease-a", 2, 8, 33)
    third = await lane.update("lease-a", 3, 12, 33)

    assert started["lease_id"] == "lease-a"
    assert started["confirmed_pose"] == {"yaw": 0, "pitch": 33}
    assert first["accepted"] is True
    assert first["accepted_count"] == 1
    assert second["pending_depth"] == 1
    assert second["accepted_count"] == 2
    assert third["replaced"] is True
    assert third["replaced_count"] == 1
    assert lane.status()["maximum_pending_depth"] == 1

    device.finish_move()
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    assert [call[1]["yaw"] for call in device.moves] == [4, 8]
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_reads_cached_motion_state_without_servo_bus_polling() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
        motion_state={
            "yaw": 7,
            "pitch": 33,
            "target_yaw": 12,
            "target_pitch": 33,
            "moving": True,
        },
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    assert await lane.motion_status("lease-a") == {
        "yaw": 7,
        "pitch": 33,
        "target_yaw": 12,
        "target_pitch": 33,
        "moving": True,
    }
    assert device.calls[-1] == (
        "self.robot.get_head_angles",
        {"cached_motion_state": True},
    )
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_rejects_duplicate_and_reverse_sequences() -> None:
    device = GatedDevice(seed={"yaw": 0, "pitch": 33})
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 2, 4, 33)
    with pytest.raises(ValueError, match="sequence"):
        await lane.update("lease-a", 2, 4, 33)
    with pytest.raises(ValueError, match="sequence"):
        await lane.update("lease-a", 1, 4, 33)

    device.finish_move()
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_clear_discards_only_pending_target() -> None:
    device = GatedDevice(seed={"yaw": 0, "pitch": 33})
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    await lane.update("lease-a", 2, 8, 33)
    cleared = await lane.clear("lease-a")

    assert cleared["pending_depth"] == 0
    device.finish_move()
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)
    assert len(device.moves) == 1
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_start_rejects_a_second_active_lease() -> None:
    device = GatedDevice(seed={"yaw": 0, "pitch": 33})
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    with pytest.raises(RuntimeError, match="already running"):
        await lane.start(
            rate_hz=10,
            max_step_deg=4,
            max_pending_age_ms=180,
            speed_dps=90,
        )

    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_stop_waits_for_in_progress_start_and_fully_unwinds_it() -> None:
    acquire_gate = asyncio.Event()
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        acquire_gate=acquire_gate,
    )
    lane = create_lane(device)

    starting = asyncio.create_task(
        lane.start(
            rate_hz=10,
            max_step_deg=4,
            max_pending_age_ms=180,
            speed_dps=90,
        )
    )
    await asyncio.wait_for(device.acquire_started.wait(), timeout=0.1)
    stopping = asyncio.create_task(lane.stop())
    await asyncio.sleep(0)

    assert not stopping.done()

    acquire_gate.set()
    started = await asyncio.wait_for(starting, timeout=0.1)
    stopped = await asyncio.wait_for(stopping, timeout=0.1)

    assert started["phase"] == "running"
    assert stopped["phase"] == "stopped"
    assert lane.status()["phase"] == "stopped"
    assert device.head_target_lane_acquired is False


@pytest.mark.asyncio
async def test_failure_requires_stop_and_restart_before_reclamping() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
        fail_move_numbers={1},
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)
    with pytest.raises(RuntimeError, match="not running"):
        await lane.update("lease-a", 2, 12, 33)

    assert lane.status()["phase"] == "failed"
    await lane.stop("lease-a")
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )
    await lane.update("lease-a", 1, 12, 33)
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    assert [call[1]["yaw"] for call in device.moves] == [4, 4]
    assert lane.status()["failed"] == 0
    assert lane.status()["confirmed_pose"] == {"yaw": 4, "pitch": 33}
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_discards_expired_pending_without_dispatch() -> None:
    device = GatedDevice(seed={"yaw": 0, "pitch": 33})
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    await lane.update("lease-a", 2, 8, 33)
    device.advance_ms(181)
    device.finish_move()
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    assert len(device.moves) == 1
    assert lane.status()["stale_discarded"] == 1
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_dispatch_starts_respect_rate_limit_and_depth_bounds() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=500,
        speed_dps=90,
    )

    for sequence, yaw in enumerate((4, 8, 12), start=1):
        await lane.update("lease-a", sequence, yaw, 33)
        await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    assert device.move_started_at == pytest.approx([0.0, 0.1, 0.2])
    assert lane.status()["maximum_active_calls"] == 1
    assert lane.status()["maximum_pending_depth"] == 1
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_keeps_one_wire_call_and_only_one_latest_pending() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_all_moves=True,
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=500,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    await lane.update("lease-a", 2, 8, 33)
    for _ in range(10):
        if len(device.moves) == 2:
            break
        await asyncio.sleep(0)
    await lane.update("lease-a", 3, 12, 33)
    await lane.update("lease-a", 4, 16, 33)

    status = lane.status("lease-a")
    assert [call[1]["yaw"] for call in device.moves] == [4]
    assert status["active_calls"] == 1
    assert status["maximum_active_calls"] == 1
    assert status["pending_depth"] == 1
    assert status["replaced"] == 2

    device.finish_move()
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    assert [call[1]["yaw"] for call in device.moves] == [4, 8]
    assert lane.status()["confirmed"] == 2
    assert lane.status()["last_confirmed_sequence"] == 4
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_dispatches_second_only_after_first_reply_completes() -> None:
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
        move_gates={1: first_release, 2: second_release},
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=500,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    for _ in range(10):
        if len(device.moves) == 1:
            break
        await asyncio.sleep(0)
    await lane.update("lease-a", 2, 8, 33)
    for _ in range(10):
        if len(device.moves) == 2:
            break
        await asyncio.sleep(0)

    assert [call[1]["yaw"] for call in device.moves] == [4]
    assert lane.status()["active_calls"] == 1
    assert lane.status()["pending_depth"] == 1
    assert lane.status()["confirmed"] == 0

    second_release.set()
    first_release.set()
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)
    assert [call[1]["yaw"] for call in device.moves] == [4, 8]
    assert lane.status()["confirmed"] == 2
    assert lane.status()["last_confirmed_sequence"] == 2
    assert lane.status()["confirmed_pose"] == {"yaw": 8, "pitch": 33}
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_first_failure_fail_closes_without_dispatching_pending() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_all_moves=True,
        fail_move_numbers={1},
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=500,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    await lane.update("lease-a", 2, 8, 33)
    await lane.update("lease-a", 3, 12, 33)

    device.finish_move()
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    status = lane.status("lease-a")
    assert [call[1]["yaw"] for call in device.moves] == [4]
    assert status["phase"] == "failed"
    assert status["active_calls"] == 0
    assert status["pending_depth"] == 0
    assert status["failed"] == 1
    assert status["confirmed"] == 0
    assert device.head_target_lane_acquired is True

    stopped = await lane.stop("lease-a")
    assert stopped["phase"] == "stopped"
    assert device.head_target_lane_acquired is False


@pytest.mark.asyncio
async def test_stopped_status_reports_successful_reply_stage_metrics() -> None:
    device = GatedDevice(seed={"yaw": 0, "pitch": 33})
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    device.advance_ms(37)
    device.finish_move()
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)
    stopped = await lane.stop("lease-a")

    assert stopped["confirmed_apply_replies"] == 1
    assert stopped["failed"] == 0
    assert stopped["successful_reply_latency_ms"] == {
        "count": 1,
        "p50": 37,
        "p95": 37,
        "p99": 37,
        "max": 37,
    }
    assert stopped["maximum_active_calls"] == 1
    assert stopped["maximum_pending_depth"] == 1


@pytest.mark.asyncio
async def test_aggregates_optional_firmware_mcp_stages_without_resetting() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
        move_payloads={
            1: {
                "ok": True,
                "servo_ok": True,
                "mcpStageUs": {
                    "receiveToApply": 100,
                    "toolApply": 20,
                    "applyToReplyEnqueue": 7,
                    "schedulerHops": 0,
                },
            },
            2: {
                "ok": True,
                "servo_ok": True,
                "mcpStageUs": {
                    "receiveToApply": 300,
                    "toolApply": 40,
                    "applyToReplyEnqueue": 9,
                    "schedulerHops": 2,
                },
            },
        },
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    for sequence, yaw in enumerate((4, 8, 12), start=1):
        await lane.update("lease-a", sequence, yaw, 33)
        await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    expected = {
        "count": 2,
        "receiveToApply": {"count": 2, "p50": 100, "p95": 300, "max": 300},
        "toolApply": {"count": 2, "p50": 20, "p95": 40, "max": 40},
        "applyToReplyEnqueue": {"count": 2, "p50": 7, "p95": 9, "max": 9},
        "schedulerHops": {"0": 1, "2": 1},
    }
    assert lane.status()["firmware_mcp_stage_us"] == expected
    stopped = await lane.stop("lease-a")
    assert stopped["firmware_mcp_stage_us"] == expected
    assert lane.status("lease-a")["firmware_mcp_stage_us"] == expected


@pytest.mark.asyncio
async def test_aggregates_firmware_stage_from_json_rpc_tool_result_envelope() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
        move_outer_stages={
            1: {
                "receiveToApply": 210,
                "toolApply": 31,
                "applyToReplyEnqueue": 8,
                "schedulerHops": 1,
            }
        },
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    assert lane.status()["firmware_mcp_stage_us"] == {
        "count": 1,
        "receiveToApply": {"count": 1, "p50": 210, "p95": 210, "max": 210},
        "toolApply": {"count": 1, "p50": 31, "p95": 31, "max": 31},
        "applyToReplyEnqueue": {"count": 1, "p50": 8, "p95": 8, "max": 8},
        "schedulerHops": {"1": 1},
    }
    await lane.stop("lease-a")


@pytest.mark.parametrize(
    "mcp_stage",
    [
        {
            "receiveToApply": True,
            "toolApply": 20,
            "applyToReplyEnqueue": 7,
            "schedulerHops": 0,
        },
        {
            "receiveToApply": 100,
            "toolApply": -1,
            "applyToReplyEnqueue": 7,
            "schedulerHops": 0,
        },
        {
            "receiveToApply": 100,
            "toolApply": 20,
            "applyToReplyEnqueue": float("inf"),
            "schedulerHops": 0,
        },
        {
            "receiveToApply": 10**30,
            "toolApply": 20,
            "applyToReplyEnqueue": 7,
            "schedulerHops": 0,
        },
    ],
)
@pytest.mark.asyncio
async def test_ignores_invalid_firmware_mcp_stages(
    mcp_stage: dict[str, object],
) -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
        move_payloads={
            1: {
                "ok": True,
                "servo_ok": True,
                "mcpStageUs": mcp_stage,
            }
        },
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)
    status = await lane.stop("lease-a")

    assert status["confirmed_apply_replies"] == 1
    assert status["successful_reply_latency_ms"]["count"] == 1
    assert status["firmware_mcp_stage_us"]["count"] == 0
    assert status["firmware_mcp_stage_us"]["schedulerHops"] == {}


@pytest.mark.asyncio
async def test_discards_target_matching_seed_confirmed_pose() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 0, 33)
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    status = lane.status()
    assert device.moves == []
    assert status["no_op_discarded"] == 1
    assert status["dispatched"] == 0
    assert status["confirmed"] == 0
    assert status["failed"] == 0
    assert status["active_calls"] == 0
    assert status["confirmed_pose"] == {"yaw": 0, "pitch": 33}
    assert status["last_confirmed_sequence"] is None
    assert status["device_dispatch_latency_ms"]["count"] == 0
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_discards_same_absolute_target_pending_during_success() -> None:
    device = GatedDevice(seed={"yaw": 0, "pitch": 33})
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    await lane.update("lease-a", 2, 4, 33)
    device.finish_move()
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    status = lane.status()
    assert [call[1]["yaw"] for call in device.moves] == [4]
    assert status["no_op_discarded"] == 1
    assert status["dispatched"] == 1
    assert status["confirmed"] == 1
    assert status["failed"] == 0
    assert status["confirmed_pose"] == {"yaw": 4, "pitch": 33}
    assert status["last_confirmed_sequence"] == 1
    assert status["device_dispatch_latency_ms"]["count"] == 1
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_discards_same_target_pending_after_failure() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        fail_move_numbers={1},
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    await lane.update("lease-a", 2, 4, 33)
    device.finish_move()
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    status = lane.status()
    assert [call[1]["yaw"] for call in device.moves] == [4]
    assert status["no_op_discarded"] == 0
    assert status["dispatched"] == 1
    assert status["confirmed"] == 0
    assert status["confirmed_apply_replies"] == 0
    assert status["failed"] == 1
    assert status["phase"] == "failed"
    assert status["pending_depth"] == 0
    assert status["confirmed_pose"] == {"yaw": 0, "pitch": 33}
    assert status["last_confirmed_sequence"] is None
    assert status["device_dispatch_latency_ms"]["count"] == 1
    assert status["successful_reply_latency_ms"]["count"] == 0
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_latest_wins_a_b_a_discards_pending_a_after_success() -> None:
    device = GatedDevice(seed={"yaw": 0, "pitch": 33})
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    await lane.update("lease-a", 2, 8, 33)
    await lane.update("lease-a", 3, 4, 33)
    device.finish_move()
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    status = lane.status()
    assert [call[1]["yaw"] for call in device.moves] == [4]
    assert status["replaced"] == 1
    assert status["no_op_discarded"] == 1
    assert status["dispatched"] == 1
    assert status["confirmed"] == 1
    assert status["last_accepted_sequence"] == 3
    assert status["last_confirmed_sequence"] == 1
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_discards_target_quantized_to_confirmed_pose() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=0.4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 1, 33)
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    status = lane.status()
    assert device.moves == []
    assert status["no_op_discarded"] == 1
    assert status["dispatched"] == 0
    assert status["confirmed"] == 0
    assert status["confirmed_pose"] == {"yaw": 0, "pitch": 33}
    assert status["device_dispatch_latency_ms"]["count"] == 0
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_far_absolute_target_advances_one_step_per_update() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, -35, 33)
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    expected_dispatches = [
        (
            "self.robot.set_head_angles",
            {"yaw": -4, "pitch": 33, "speed_dps": 90},
        ),
        (
            "self.robot.set_head_angles",
            {"yaw": -8, "pitch": 33, "speed_dps": 90},
        ),
        (
            "self.robot.set_head_angles",
            {"yaw": -12, "pitch": 33, "speed_dps": 90},
        ),
    ]
    assert device.moves == expected_dispatches[:1]
    assert lane.status()["active_calls"] == 0
    assert lane.status()["pending_depth"] == 0

    await asyncio.sleep(0)
    assert device.moves == expected_dispatches[:1]

    for sequence in (2, 3):
        await lane.update("lease-a", sequence, -35, 33)
        await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    assert device.moves == expected_dispatches
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_stop_discards_pending_drains_active_and_is_idempotent() -> None:
    device = GatedDevice(seed={"yaw": 0, "pitch": 33})
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )
    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    await lane.update("lease-a", 2, 8, 33)

    stopping = asyncio.create_task(lane.stop("lease-a"))
    await asyncio.sleep(0)
    assert not stopping.done()
    assert lane.status()["phase"] == "stopping"

    device.finish_move()
    stopped = await asyncio.wait_for(stopping, timeout=0.1)
    repeated = await lane.stop("lease-a")

    assert stopped["phase"] == "stopped"
    assert repeated["phase"] == "stopped"
    assert len(device.moves) == 1
    assert stopped["post_stop_dispatches"] == 0
    wifi_modes = [
        arguments["mode"]
        for name, arguments in device.calls
        if name == "self.wifi.set_power_save"
    ]
    assert wifi_modes == ["none", "max_modem"]


@pytest.mark.asyncio
async def test_stop_drains_one_active_before_releasing_servo_reservation() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_all_moves=True,
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=500,
        speed_dps=90,
    )

    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)
    await lane.update("lease-a", 2, 8, 33)
    await lane.update("lease-a", 3, 12, 33)

    stopping = asyncio.create_task(lane.stop("lease-a"))
    await asyncio.sleep(0)
    assert not stopping.done()
    assert lane.status()["phase"] == "stopping"
    assert lane.status()["active_calls"] == 1
    assert lane.status()["pending_depth"] == 0
    assert device.head_target_lane_acquired is True

    device.finish_move()
    stopped = await asyncio.wait_for(stopping, timeout=0.1)

    assert [call[1]["yaw"] for call in device.moves] == [4]
    assert stopped["active_calls"] == 0
    assert stopped["post_stop_dispatches"] == 0
    assert device.head_target_lane_acquired is False


@pytest.mark.asyncio
async def test_stop_timeout_normalizes_state_for_idempotent_stop_and_restart() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_all_moves=True,
    )
    lane = HeadTargetLane(
        device,
        monotonic_now=device.clock,
        sleep=device.sleep,
        lease_id_factory=lambda: "lease-a",
        device_call_timeout_seconds=0.01,
    )
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )
    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)

    with pytest.raises(RuntimeError, match="stop timed out"):
        await lane.stop("lease-a")

    assert lane.status("lease-a")["phase"] == "stopped"
    assert lane.status()["active_calls"] == 0
    assert device.head_target_lane_acquired is False
    assert (await lane.stop("lease-a"))["phase"] == "stopped"

    restarted = await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )
    assert restarted["phase"] == "running"
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_cancelled_stop_drains_dispatch_normalizes_and_can_restart() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_all_moves=True,
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )
    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(device.move_started.wait(), timeout=0.1)

    stopping = asyncio.create_task(lane.stop("lease-a"))
    await asyncio.sleep(0)
    assert lane.status()["phase"] == "stopping"

    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert lane.status("lease-a")["phase"] == "stopped"
    assert lane.status()["active_calls"] == 0
    assert device.head_target_lane_acquired is False
    assert (await lane.stop("lease-a"))["phase"] == "stopped"

    restarted = await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )
    assert restarted["phase"] == "running"
    await lane.stop("lease-a")


@pytest.mark.asyncio
async def test_status_retains_only_bounded_aggregates() -> None:
    device = GatedDevice(
        seed={"yaw": 0, "pitch": 33},
        gate_first_move=False,
    )
    lane = create_lane(device)
    await lane.start(
        rate_hz=10,
        max_step_deg=4,
        max_pending_age_ms=180,
        speed_dps=90,
    )
    await lane.update("lease-a", 1, 4, 33)
    await asyncio.wait_for(lane.wait_idle(), timeout=0.1)

    encoded = json.dumps(lane.status("lease-a"), sort_keys=True)
    for prohibited in (
        "targets",
        "series",
        "history",
        "center_x",
        "center_y",
        "image",
        "jpeg",
    ):
        assert prohibited not in encoded
    await lane.stop("lease-a")
