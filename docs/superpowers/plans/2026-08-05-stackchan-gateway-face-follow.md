# StackChan gateway内人物追従の実装計画

> `superpowers:executing-plans` と `superpowers:test-driven-development` に従う。
> ユーザーの明示依頼がないためcommit / pushは行わない。

**目的:** Mocoを含む任意のMCP hostがlifecycle toolだけで利用できる人物追従を、
合格済み125ms挙動を変えず `stackchan-mcp` gateway内へ閉じ込める。

**構成:** 既存のcamera latest storeとhead target laneをgateway process内の単一runtimeが
composeする。純粋なPINTO parser / controllerと、resourceを所有するasync serviceを分離し、
画像やframe traceは保持しない。

**技術:** Python 3.10+、asyncio、ONNX Runtime（optional）、NumPy（optional）、
Pillow（optional）、pytest、Ruff。

---

## Task 1: 仕様と所有境界を固定する

- [x] MocoはMCP hostであり、直接integrationしないことを仕様化する。
- [x] Picoをruntime ownerから履歴上の移植元へ変更する。
- [x] 合格済みcontroller定数、停止順、安全条件、観測contractを記録する。

## Task 2: PINTO detectorをTDDで移植する

**対象:**

- 新規 `gateway/tests/test_face_follow_detector.py`
- 新規 `gateway/stackchan_mcp/face_follow_detector.py`

- [x] output shape、class、threshold、座標、無効boxのRED testを書く。
- [x] face優先と同label rankのRED testを書く。
- [x] PillowによるRGB resizeとBGR NCHW変換、ONNX session呼出しをfakeでtestする。
- [x] optional dependencyをlazy importする最小実装でGREENにする。

## Task 3: 合格済みcontrollerと再捕捉集計をTDDで移植する

**対象:**

- 新規 `gateway/tests/test_face_follow.py`
- 新規 `gateway/stackchan_mcp/face_follow.py`

- [x] dead zone、release hysteresis、gain、方向、4° clamp、round、pitch 23°下限をREDにする。
- [x] confirmed pose基準、hold 1000ms、home-only scan、pending clearをREDにする。
- [x] 初回targetと最長loss episodeをoutcome別に分解するRED testを書く。
- [x] 有限counter / histogramだけの実装でGREENにする。

## Task 4: lifecycle serviceをTDDで実装する

**対象:**

- `gateway/tests/test_face_follow.py`
- `gateway/stackchan_mcp/face_follow.py`
- `gateway/stackchan_mcp/gateway.py`

- [x] model load前失敗がhardware resourceを取得しないことをtestする。
- [x] camera 20fps/quality60、lane 10Hz/max step4/age180/speed90をtestする。
- [x] 同時tick最大1、freshness 180ms、latest sequence、outcome分類をtestする。
- [x] stopがtask→lane→home 37→33→cameraの順で、停止後dispatch 0になることをtestする。
- [x] start途中の失敗を逆順cleanupし、stopをidempotentにする。
- [x] Gateway.stopがESP32Manager.stopより先にserviceを停止することをtestする。

## Task 5: MCP lifecycle toolとoptional extraを追加する

**対象:**

- `gateway/tests/test_stdio_server.py`
- `gateway/stackchan_mcp/stdio_server.py`
- `gateway/pyproject.toml`
- `README.md`
- `README.ja.md`

- [x] `stackchan_face_follow(start|status|stop)` の厳密schemaとdispatchをREDにする。
- [x] tool handlerをserviceへ接続してGREENにする。
- [x] `[face-follow]` extraとmodel envを両READMEへ同期して記載する。
- [x] Moco固有dependency、path、schema fieldがないことをscope searchする。

## Task 6: 非実機gateを通す

- [x] focused pytestを各taskでRed→Green記録する。
- [x] `cd gateway && uv run pytest && uv run ruff check .`
- [x] `git diff --check`
- [x] 新規identifier / fixed constantの `rg` scope search。
- [x] optional dependencyが未導入でもbase importと既存tool一覧が動くことを確認する。
- [x] controller定数、active最大1、auto-sleep非変更、Moco/Pico未変更をdiffで確認する。
- [x] commit / push / live hardware操作を行わず、実機A/B前で停止する。

## Task 7: 125ms基準Runと接続設定を実機検証する

- [x] ユーザー在席、auto-sleep false、camera停止、pitch 23°以上を開始前に確認する。
- [x] 通常envでcamera UDP helloがtimeoutする境界を、HTTP captureとは別の
  `VISION_HOST` fallbackへ切り分ける。
- [x] `VISION_URL`明示時は未指定のcamera UDP hostをcontrol peerへ委ねるRED testを
  追加し、分岐1か所の修正でGREENにする。
- [x] 125ms／4°／active 1のまま60秒Runを行い、段階別metricsと停止後状態を保存する。
- [x] 通常envへ戻した再起動後、5秒camera-only probeで80 framesを受信し、停止を確認する。
- [x] ユーザー主観を確認してから、100msの1変数A/Bへ進むか決める。

## Task 8: observation 100msを1変数A/Bする

- [x] 125ms基準Runについてユーザーから「特に問題はありませんでした」を得た。
- [x] frame wait 100msのRED testを確認し、observation定数だけを100msへ変更する。
- [x] focused 11件、gateway全889件、RuffをGREENにする。
- [x] 60秒Runと停止後安全確認を行い、125ms基準との差分を保存する。
- [x] 100ms Runのユーザー主観を確認し、採用または125msへrevertする。

## 実行記録

- detector module未実装、controller / lifecycle未実装、MCP tool未登録の各状態で
  focused testがREDになることを確認してから実装した。
- gateway全体は889件pass / 5件skip、Ruff pass。
- firmware host testは89件pass。既存のcamera / command段階別観測パッチも含めて再確認した。
- `[face-follow]` extraでPillow / NumPyのBGR NCHW前処理testを実行し、実際の合格時
  PINTO modelをloadしてsynthetic JPEGを1回推論できた。camera / servo接続は行っていない。
- isolated base installではONNX Runtime / NumPy / Pillowが存在しない状態でも
  `stackchan_mcp.gateway` をimportできた。
- `git diff --check` とidentifier scope searchはpassした。Moco / Picoは編集していない。
- ユーザー在席確認後、`gateway-baseline-125ms-2` を実行した。60秒で476 processed
  frames（7.93fps）、raw camera 944 frames（15.73fps）、target 381、command
  277/277成功だった。active / pending最大1、replaced / stale / post-stop dispatchは0。
- raw cameraのdevice capture周期はp50 50ms / p95 100ms、gateway受信周期は
  p50 56ms / p95 110ms、capture waitはp95 5.9ms、JPEG encodeはp95 34ms、
  UDP assemblyはp95 29msだった。125ms observationがprocessed cadenceの律速である。
- 初回targetは268ms（開始toolのmodel loadを含む呼び出し全体は1.159秒）。最長lossは
  3.874秒で、その区間は`no_candidate` 30回、frame timeout / stale / association reject
  ではなかった。
- device dispatchはp95 66ms / max 116ms。pending ageはp95 1ms / max 18ms、
  firmware `receiveToApply` はp95 4.57msで、queue滞留はtail ownerではなかった。
- 終了後はcameraとdevice producer停止、auto-sleep false、pitch 33を確認した。
  yawはhome 0°指令を再送しても実測1°で、旧合格JSONのrun終了時-1°と同じ±1°の
  サーボ読取量子化範囲だが、厳密な0°条件としては未達と記録した。
- 通常envでのcamera-only probeは5秒で80 frames、受信周期p50 60ms / p95 120ms。
  `VISION_URL`とcamera UDP hostの分離修正後は一時的な起動overrideなしで成立した。
- 100ms A/Bは60秒で593 frames（9.88fps）、target 520、first target 216ms、
  longest loss 2.007秒だった。active / pending最大1、errors / stale / post-stopは0。
  一方でpending replacement 1、pending age p95 62ms、device dispatch p95 84ms / max
  212msを観測した。その後ユーザーから「特に問題はありませんでした」と評価され、
  100msを採用した。replacementはlatest-only coalescingとして記録し、失敗とは扱わない。
- commit / push、firmware flash、Moco / Picoの変更、100ms以外のcontroller tuningは
  行っていない。
