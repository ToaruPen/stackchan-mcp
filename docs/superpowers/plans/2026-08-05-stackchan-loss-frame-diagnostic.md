# StackChan立ち上がり時loss画像診断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 立ち上がり時のhead/face候補消失を、画角外・画質・モデル／前処理のどこに属するか、最大5枚の一時JPEGで判定する。

**Architecture:** productionコードは変更せず、認証済みの既存MCP lifecycleと `/camera/latest` を使う一回限りのローカル診断clientを実行する。計測中のJPEGは最大512フレームのメモリだけに保持し、追従停止後に同じONNXモデルで再推論して最長lossの境界5枚だけを一時保存する。

**Tech Stack:** Python 3.10+、asyncio、httpx、MCP Python client、ONNX Runtime、Pillow、NumPy、既存gateway HTTP/MCP API。

---

## Task 1: 実機とgatewayのpreflightを固定する

**Files:**
- Read: `/Users/monsoon/.pico/stackchan-mcp/local-gateway.env`
- Read: `/Users/monsoon/.pico/models/pinto-441-s/yolox_s_body_head_hand_face_dist_0189_0.4952_post_1x3x256x320.onnx`
- Create at runtime: `/Users/monsoon/.pico/field-runs/stackchan-loss-frame-diagnostic-100ms-1-*/stackchan-loss-frame-diagnostic-100ms-1.json`

- [ ] **Step 1: gateway processが最新worktreeから起動中であることを確認する**

Run:

```bash
ps -axo pid=,command= | rg 'uv run --project gateway.*stackchan-mcp serve'
```

Expected: `/Users/monsoon/.codex/worktrees/ac57/stackchan-mcp` のgateway processが1件だけ表示される。

- [ ] **Step 2: 認証済みMCP clientで安全状態を読む**

Call `get_status`、`get_head_angles`、`get_auto_sleep`、`camera_stream status`、
`get_camera_device_stream_status`、`stackchan_face_follow status`。

Expected: device connected、pitch 33度、yaw 0度相当、auto-sleep false、camera stopped、face-follow stopped。

- [ ] **Step 3: ユーザーの在席と手順を確認する**

座位・正面中央から開始し、開始10秒後のMacのチャイム1回で、その場に正面を向いたまま立つと伝える。探索動作は行わない。

## Task 2: productionコードを変えずに30秒Runを取得する

**Files:**
- Create at runtime: Run directory内の権限700 `loss-frames/` directory
- Create at runtime: `loss-frames/` 内の権限600 JPEG、最大5枚

- [ ] **Step 1: 一回限りの診断clientを起動する**

診断clientは次の不変条件をassertする。

```python
MAX_MEMORY_FRAMES = 512
RUN_DURATION_S = 30
STAND_CHIME_AT_S = 10
MAX_SAVED_JPEGS = 5
```

`stackchan_face_follow start` 後、
`f"/camera/latest?after_sequence={sequence}&timeout_ms=1000"`
をlong-pollし、sequence、capture/encode/receive timestamp、JPEGを最大512件だけメモリへ
保持する。10秒時点で `/System/Library/Sounds/Glass.aiff` を1回再生する。

- [ ] **Step 2: 30秒後にface-followを必ず停止する**

正常系・例外系とも `finally` から `stackchan_face_follow stop` を呼ぶ。停止後1秒待ち、
`get_head_angles`、`get_auto_sleep`、`camera_stream status`、
`get_camera_device_stream_status` を再読する。

Expected: active/pending最大1、post-stop dispatch 0、pitch 33度、yaw 0度相当、
camera stopped、device camera stopped、auto-sleep false。

## Task 3: 最長lossを選び最大5枚だけ一時保存する

**Files:**
- Read: `gateway/stackchan_mcp/face_follow_detector.py`
- Create at runtime: 権限600の選択JPEG、最大5枚

- [ ] **Step 1: メモリ上の全JPEGを同じONNX modelで逐次再推論する**

既存 `create_pinto_preprocessor()` と `parse_pinto_detections()` を用い、各sequenceの
head/face target有無を算出する。同時にclass ID 0/1/2/3の最大confidenceを有限な整数
percentageとして記録する。

- [ ] **Step 2: 最長の連続head/face候補なし区間を決定する**

初回target前の区間は除外し、target検出後から最終targetまでの連続lossだけを対象にする。
最長区間について `pre/start/middle/end/post` のindexを作り、重複を除く。

- [ ] **Step 3: 選択JPEGを最大5枚だけ保存する**

filenameは `f"{order:02d}-{role}-seq-{sequence}.jpg"` とし、directoryは700、各JPEGは
600にする。
Run JSONへsequence、role、brightness mean、black/white clipping percentage、class別最大
confidenceだけを記録し、画像bytesや秘密値は書かない。

## Task 4: 画像を確認して原因オーナーを確定する

**Files:**
- Read: Task 3で選んだ最大5枚
- Modify at runtime: Run JSONの有限診断結果

- [ ] **Step 1: 最大5枚をsequence順に視覚確認する**

各画像について、顔の全体／一部／画角外、上端接触、肉眼で分かる動体ブレ、露出崩れ、
bodyの可視性を判定する。

- [ ] **Step 2: 判定規則を適用する**

画角外ならcamera pose/crop、強いブレ・露出ならcapture条件、顔が判別可能なのに出力なし
ならmodel/preprocessをオーナーとする。bodyはloss中もclass 0が連続して残る場合だけ後続A/B
候補とし、このRunでは追従targetへ使わない。

- [ ] **Step 3: ユーザー主観を一次条件として記録する**

固まり、再捕捉の遅れ、急跳び、頷き、逆方向の有無をRun JSONへ追記する。ユーザーが
直前に報告した「固まってから遅れてこちらを向いた」を前回Runの評価として分離する。

## Task 5: 一時画像を削除し証拠を検証する

**Files:**
- Delete: Task 3の一時JPEGとその専用directory
- Validate: Run JSON

- [ ] **Step 1: 削除対象を正確に列挙する**

Run専用directoryが `/Users/monsoon/.pico/field-runs/stackchan-loss-frame-diagnostic-100ms-1-`
prefixであり、その直下の `loss-frames/` にあるJPEGが5枚以下で、Run JSONがRun専用
directory直下に存在することをread-only checkする。

- [ ] **Step 2: 一時JPEGを削除する**

明示的に列挙したJPEGだけを削除し、空になった一時画像directoryを削除する。削除後に
JPEGが0枚であることを確認する。

- [ ] **Step 3: 永続証拠とworktreeを検証する**

Run:

```bash
find /Users/monsoon/.pico/field-runs -maxdepth 2 -type f \
  -name 'stackchan-loss-frame-diagnostic-100ms-1.json' -exec jq empty {} \;
git diff --check
git status --short
```

Expected: JSON valid、画像なし、production code差分なし、既存の未commit変更だけが残る。
このタスクではcommit、push、PRを行わない。
