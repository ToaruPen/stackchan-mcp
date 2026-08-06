# StackChan target loss時hold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** target lossが1秒を超えても観測根拠なしにhomeへ動かず、最後のconfirmed poseをholdする。

**Architecture:** gatewayのpure face-follow controllerにあるhome-only `scan` 分岐だけを除去する。service lifecycleの明示的なstop時home、安全定数、detector、head lane、camera経路は変更しない。

**Tech Stack:** Python 3.10+、pytest、Ruff、既存MCP live diagnostic。

---

### Task 1: loss時hold契約をREDで固定する

**Files:**
- Modify: `gateway/tests/test_face_follow.py`
- Test: `gateway/tests/test_face_follow.py`

- [ ] **Step 1: 既存scan testを時間制限なしhold testへ置き換える**

```python
def test_controller_holds_without_unobserved_home_move_after_target_loss() -> None:
    tracked = advance_attention(
        AttentionState(),
        now_ms=100,
        observed_at_ms=100,
        current_pose=HeadPose(20, 40),
        detections=[_detection(0.5, 0.5)],
    )
    held_before_old_timeout = advance_attention(
        tracked.state,
        now_ms=1_099,
        current_pose=HeadPose(20, 40),
        detections=[],
    )
    held_after_old_timeout = advance_attention(
        held_before_old_timeout.state,
        now_ms=10_000,
        current_pose=HeadPose(20, 40),
        detections=[],
    )

    assert held_before_old_timeout.state.mode == "hold"
    assert held_before_old_timeout.effect == AttentionEffect("hold")
    assert held_after_old_timeout.state.mode == "hold"
    assert held_after_old_timeout.effect == AttentionEffect("hold")
    assert held_after_old_timeout.state.last_target_at_ms == 100
```

- [ ] **Step 2: 初回取得前はacquireのままholdするtestを追加する**

```python
def test_controller_stays_in_acquire_without_a_target() -> None:
    transition = advance_attention(
        AttentionState(),
        now_ms=10_000,
        current_pose=HeadPose(0, 33),
        detections=[],
    )

    assert transition.state.mode == "acquire"
    assert transition.effect == AttentionEffect("hold")
```

- [ ] **Step 3: focused testが旧scan挙動に対してREDになることを確認する**

Run:

```bash
cd gateway
uv run pytest -q \
  tests/test_face_follow.py::test_controller_holds_without_unobserved_home_move_after_target_loss \
  tests/test_face_follow.py::test_controller_stays_in_acquire_without_a_target
```

Expected: 2 failures。旧実装がそれぞれ `scan` とhome moveを返すため失敗する。

### Task 2: pure controllerからscanを除去してGREENにする

**Files:**
- Modify: `gateway/stackchan_mcp/face_follow.py`
- Test: `gateway/tests/test_face_follow.py`

- [ ] **Step 1: scan専用の型・定数・stateを除去する**

`AttentionMode` を `Literal["acquire", "track", "hold"]` に変更し、
`LOST_HOLD_MS`、`SCAN_DWELL_MS`、`AttentionState.scan_arrived_at_ms` を削除する。

- [ ] **Step 2: no-candidate分岐を観測根拠のないmoveを作らないholdへ置き換える**

```python
    mode: AttentionMode = (
        "hold" if state.last_target_at_ms is not None else "acquire"
    )
    return AttentionTransition(
        state=AttentionState(
            mode=mode,
            centered=state.centered,
            last_target_at_ms=state.last_target_at_ms,
            last_tracked_pose=state.last_tracked_pose,
        ),
        effect=AttentionEffect("hold"),
    )
```

`_scan()` を削除する。targetありの `_track_target()`、4度clamp、confirmed pose基準は変更しない。

- [ ] **Step 3: focused testをGREENにする**

Run:

```bash
cd gateway
uv run pytest -q tests/test_face_follow.py
```

Expected: all tests pass。

- [ ] **Step 4: scan専用identifierがproductionとtestから消えたことを確認する**

Run:

```bash
rg -n 'LOST_HOLD_MS|SCAN_DWELL_MS|scan_arrived_at_ms|mode="scan"|def _scan' \
  gateway/stackchan_mcp/face_follow.py gateway/tests/test_face_follow.py
```

Expected: no matches。

### Task 3: 非実機gateを通す

**Files:**
- Validate: `gateway/stackchan_mcp/face_follow.py`
- Validate: `gateway/tests/test_face_follow.py`

- [ ] **Step 1: gateway全testを実行する**

Run: `cd gateway && uv run pytest -q`

Expected: 892 tests pass、5 tests skip、failure 0。

- [ ] **Step 2: Ruffとdiffを検証する**

Run:

```bash
cd gateway
uv run ruff check stackchan_mcp/face_follow.py tests/test_face_follow.py
cd ..
git diff --check
```

Expected: errorなし。

- [ ] **Step 3: 変更範囲を確認する**

Run:

```bash
git diff -- gateway/stackchan_mcp/face_follow.py gateway/tests/test_face_follow.py
```

Expected: no-candidate時scan除去と対応test以外にcontroller定数・service lifecycle変更なし。

### Task 4: 30秒live A/Bを行う

**Files:**
- Create at runtime: `/Users/monsoon/.pico/field-runs/stackchan-loss-hold-ab-100ms-1-*/stackchan-loss-hold-ab-100ms-1.json`

- [ ] **Step 1: 最新コードを読み込むためgatewayを安全に再起動する**

face-followとcameraがstopped、auto-sleep false、pitch 23度以上を確認してから既存gatewayを
SIGINTで停止する。既存envとPINTO modelを値を表示せず再読込し、同じstreamable HTTP endpointで
起動する。ESP32 readyとcamera datagram readyを確認する。

- [ ] **Step 2: 正面中央の座位から30秒Runを開始する**

100ms観測、max step 4度、active call 1のまま開始する。開始10秒後にMacのチャイムを1回
鳴らし、ユーザーはその場で正面を向いたまま立つ。statusを250ms間隔で読み、elapsed time、
outcomes、attention mode、lane accepted/dispatched/confirmed poseをRun JSONへ保存する。

- [ ] **Step 3: loss中にhome moveがないことを判定する**

no-candidate counterが増えてtarget_visible falseになった区間で、再捕捉前にlane acceptedが
増えず、confirmed poseがyaw 0／pitch 33へ変化しないことを要求する。target再検出後のmoveは
既存controllerの出力として許可する。

- [ ] **Step 4: 安全停止を確認する**

finallyでface-followをstopし、yaw 0度相当／pitch 33度、camera stopped、device camera
stopped、auto-sleep false、active/pending最大1、post-stop dispatch 0を確認する。

### Task 5: A/B結果を記録して判断を止める

**Files:**
- Modify: `docs/superpowers/plans/2026-08-05-stackchan-gateway-face-follow.md`
- Validate: Run JSON

- [ ] **Step 1: 数値とユーザー主観を記録する**

frames、target率、最長loss、no-candidate、command件数、dispatch tail、camera cadence、
初動、停止後安全状態を記録する。ユーザーに固まり、遅れて向く動作、急跳び、頷き、逆方向、
通常左右追従の回帰を確認する。

- [ ] **Step 2: このA/Bではbody補助を実装しない**

home move除去だけの効果を採用／不採用として判定する。lossが残る場合は、確認済みbodyの
上端接触を用いる別設計へ進み、同じdiffへ混ぜない。

- [ ] **Step 3: commitしないまま証拠を検証する**

Run JSONへ `jq empty`、repositoryへ `git diff --check` を実行する。commit、push、PRは
行わず、Codex管理worktreeを保持する。
