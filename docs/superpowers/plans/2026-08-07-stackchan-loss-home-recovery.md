# StackChan target loss時home recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 顔／頭を750ms見失ったら、探索せずyaw 0度／pitch 33度へ最大4度ずつ戻り、default姿勢で待機する。

**Architecture:** gatewayのpure face-follow controllerに `recover` modeと750ms境界を追加する。既存head laneへhomeの絶対目標を渡し、lane側の10Hz・最大4度・active call 1をそのまま利用する。

**Tech Stack:** Python 3.10+、pytest、Ruff、既存MCP live diagnostic。

**実行状況（2026-08-07）:** Task 1〜3と60秒の実機Runは完了した。Run artifactは
`/Users/monsoon/.pico/field-runs/stackchan-loss-home-recovery-100ms-1-Sv9ZNUR7/stackchan-loss-home-recovery-100ms-1.json`。
ユーザーは主観挙動を採用し、その後PR作成を明示的に依頼した。実機Runではpending置換が
3件発生した。独立pre-PR reviewで、置換を0にするため新しい観測を捨てると方向反転が
遅れることが判明したため、latest-only coalescingを維持し、方向反転の回帰testを追加した。
この回帰test追加後の実機再試験は行っていない。

---

## Task 1: 750ms home recovery契約をREDで固定する

**Files:**
- Modify: `gateway/tests/test_face_follow.py`
- Test: `gateway/tests/test_face_follow.py`

- [x] **Step 1: hold-only testを749ms／750ms境界testへ置き換える**

```python
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
```

- [x] **Step 2: home到着後はrecoverでholdするtestを追加する**

```python
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
```

- [x] **Step 3: recovery途中の再検出がconfirmed pose基準へ戻るtestを追加する**

```python
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
```

- [x] **Step 4: focused testがhold-only実装に対してREDになることを確認する**

Run:

```bash
cd gateway
uv run pytest -q \
  tests/test_face_follow.py::test_controller_holds_for_749ms_then_recovers_home_at_750ms \
  tests/test_face_follow.py::test_controller_holds_at_home_after_loss_recovery \
  tests/test_face_follow.py::test_controller_reacquires_from_confirmed_pose_during_home_recovery
```

Expected: recovery modeとhome moveが未実装のためfailure。

## Task 2: pure controllerへ最小home recoveryを実装する

**Files:**
- Modify: `gateway/stackchan_mcp/face_follow.py`
- Test: `gateway/tests/test_face_follow.py`

- [x] **Step 1: modeとtimeoutを追加する**

```python
LOST_HOME_TIMEOUT_MS = 750

AttentionMode = Literal["acquire", "track", "hold", "recover"]
```

- [x] **Step 2: no-candidate分岐をacquire／hold／recoverへ分ける**

```python
    if state.last_target_at_ms is None:
        return AttentionTransition(
            state=AttentionState(mode="acquire"),
            effect=AttentionEffect("hold"),
        )
    if now_ms - state.last_target_at_ms < LOST_HOME_TIMEOUT_MS:
        return AttentionTransition(
            state=AttentionState(
                mode="hold",
                centered=state.centered,
                last_target_at_ms=state.last_target_at_ms,
            ),
            effect=AttentionEffect("hold"),
        )
    return _recover_home(state, current_pose=current_pose)
```

- [x] **Step 3: home recovery helperを追加する**

```python
def _recover_home(
    state: AttentionState,
    *,
    current_pose: HeadPose,
) -> AttentionTransition:
    home = HeadPose(HOME_YAW, HOME_PITCH)
    return AttentionTransition(
        state=AttentionState(
            mode="recover",
            last_target_at_ms=state.last_target_at_ms,
        ),
        effect=(
            AttentionEffect("hold")
            if current_pose == home
            else AttentionEffect("move", home)
        ),
    )
```

- [x] **Step 4: focused face-follow testをGREENにする**

Run: `cd gateway && uv run pytest -q tests/test_face_follow.py`

Expected: 20 tests pass（pre-PR regressionを含む）。

## Task 3: 非実機gateを通す

**Files:**
- Validate: `gateway/stackchan_mcp/face_follow.py`
- Validate: `gateway/tests/test_face_follow.py`

- [x] **Step 1: gateway全testを実行する**

Run: `cd gateway && uv run pytest -q`

Expected: 899 tests pass、5 tests skip、failure 0。

- [x] **Step 2: Ruffとdiffを検証する**

Run:

```bash
cd gateway
uv run ruff check stackchan_mcp/face_follow.py tests/test_face_follow.py
cd ..
git diff --check
rg -n 'LOST_HOLD_MS|SCAN_DWELL_MS|scan_arrived_at_ms|mode="scan"|def _scan' \
  gateway/stackchan_mcp/face_follow.py gateway/tests/test_face_follow.py
```

Expected: Ruffとdiff errorなし、旧scan identifierなし。

- [x] **Step 3: service安全contractを再確認する**

既存 `test_service_owns_fixed_camera_lane_and_safe_home_lifecycle` が、stop時のlane停止、
pitch 37度approach、pitch 33度home、camera releaseの順を維持することを確認する。

## Task 4: 60秒反復live A/Bを行う

**Files:**
- Create at runtime: `/Users/monsoon/.pico/field-runs/stackchan-loss-home-recovery-ab-750ms-1-*/stackchan-loss-home-recovery-ab-750ms-1.json`

- [x] **Step 1: 最新コードを読み込むためgatewayを安全に再起動する**

face-followとcamera stopped、auto-sleep false、pitch 23度以上を確認して既存gatewayをSIGINTで
停止する。既存envとPINTO modelを値を表示せず再読込し、ESP32 readyとcamera datagram readyを
確認する。

- [x] **Step 2: 正面中央で立つ・座る動作を複数回行う**

100ms観測、max step 4度、active call 1のまま60秒Runを開始する。開始3秒のチャイム1回で、
自然な速度の立つ・2〜3秒維持・座る・2〜3秒維持を約5回行う。終了時はチャイム2回とする。

- [x] **Step 3: 250ms時系列でrecovery境界を判定する**

elapsed time、attention mode、reacquisition current gap、lane accepted/dispatched/confirmed poseを
保存する。各 `recover` episodeの最初のsampleでcurrent gapが750ms以上かつ1.1秒未満であり、
poseが各軸最大4度ずつhomeへ近づき、home到着後はcommandが増えないことを要求する。

- [x] **Step 4: 安全停止を確認する**

yaw 0／pitch 33、camera stopped、auto-sleep false、active/pending最大1、error 0、
post-stop dispatch 0は確認済み。`replaced=3` はdevice dispatch tail中にpendingを
最新targetへcoalesceした回数として記録する。pre-PRで、置換を避けるため観測を捨てず、
方向反転後の最新targetをlaneへ渡す回帰testを追加した。再実機試験は未実施。

finallyでface-followをstopし、yaw 0度相当／pitch 33度、camera stopped、device camera stopped、
auto-sleep false、active/pending最大1、error/stale/post-stop dispatch 0を確認する。
`replaced` はlatest-only coalescingとして回数を記録し、方向反転後の最新targetが
採用されることを確認する。

## Task 5: 主観評価と採用可否を記録する

**Files:**
- Modify: `docs/superpowers/plans/2026-08-05-stackchan-gateway-face-follow.md`
- Validate: Run JSON

- [x] **Step 1: ユーザー主観を一次条件として確認する**

通常追従の滑らかさ、逆方向、急跳び、頷き、固まり、およびloss時に上や左を向いたまま残る
挙動が解消したかを確認する。

- [x] **Step 2: 数値と主観で採用またはrevertを決める**

recovery境界と安全条件が合格し、ユーザー主観に回帰がなければ採用候補とする。不合格なら
hold-onlyとhome recoveryを混ぜず、既知の基準へ戻す。

- [x] **Step 3: commitしないまま証拠を検証する**

Run JSONへ `jq empty`、repositoryへ `git diff --check` を実行する。commit、push、PRを行わず、
Codex管理worktreeを保持する。この時点のgate完了後、ユーザーから明示的にcommit、push、
PR作成が依頼された。
