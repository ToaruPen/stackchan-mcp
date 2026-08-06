# StackChan人物追従・観測性整備の実装計画

> エージェントが実装する場合は `superpowers:executing-plans` を使い、taskごとに
> 実行する。ユーザーの明示依頼なしにcommitしない。

**目的:** 合格済みのジンバル挙動とlive device stateを変えず、camera経路と
firmware MCP経路へ有限の段階別計測を追加する。

**構成:** 既存SCL1 envelopeへtimingを2項目追加し、既存のfirmware／gateway
境界で集計する。既存のtool resultとcamera statusを再利用し、worker、queue、
trace、controller stateは追加しない。

**技術:** C++17、ESP-IDF、GoogleTest/CTest、Python 3.10+、asyncio、pytest。

---

## Task 1: camera timing contractを定義する

**対象:**

- `firmware/host_test/test_camera_stream_protocol.cc`
- `gateway/tests/test_camera_stream.py`
- `gateway/tests/test_camera_datagram.py`

- [x] firmware fixtureへ `capture_wait_us` と `encode_us` を追加し、SCL1 headerの
  `deviceCaptureWaitUs`／`deviceEncodeUs` を要求するfailing testを書く。
- [x] gatewayで両fieldのparseとstatus集計を要求するfailing testを書く。
- [x] 100msに最初のSCU1 chunk、107msに最後のchunkを渡し、
  `assembly_ms.p95 == 7` を要求するdeterministic testを書く。
- [x] firmware camera host testとgateway camera testを実行し、fieldとstatusが
  未実装でREDになることを確認する。

## Task 2: 有限のcamera metricsを実装する

**対象:**

- 新規 `gateway/stackchan_mcp/camera_metrics.py`
- `firmware/main/camera_stream_protocol.h`
- `firmware/main/boards/common/esp_video.h`
- `firmware/main/boards/common/esp_video.cc`
- `gateway/stackchan_mcp/camera_stream.py`
- `gateway/stackchan_mcp/camera_datagram.py`

- [x] sampleを保持せずbucket countだけを保持する `BoundedLatencyHistogram` を
  実装する。bucketは有効数字2桁の上側境界へまとめ、最大値も有限にする。
- [x] 旧SCL1で欠けているtimingは0や推定値にせず、正確なhistogramから除外する。
- [x] `esp_timer_get_time()` でV4L2 dequeue waitとJPEG encodeを測り、SCL1へ
  格納する。
- [x] credit不足で捨てたcaptureを `noCreditDrops` としてstream開始時にresetし、
  statusへ出す。
- [x] gatewayでdevice capture周期、capture wait、encode、gateway受信周期、
  latest wait、およびwait分岐数を集計する。受信周期にはmonotonic clockを使う。
- [x] SCU1 assemblerでfirst-chunkからcompletionまでとcompleted-frame周期を
  集計する。
- [x] focused testを再実行しGREENを確認する。

## Task 3: 既存のfirmware MCP stage contractを満たす

**対象:**

- `firmware/host_test/test_mcp_message_dispatch.cc`
- `firmware/main/mcp_message_dispatch.h`
- `firmware/main/mcp_server.h`
- `firmware/main/mcp_server.cc`

- [x] 成功tool resultへ `receiveToApply`、`toolApply`、
  `applyToReplyEnqueue`、`schedulerHops` を加えるpure helperのfailing testを書く。
- [x] clock逆行時の差分を0へclampするtestを書く。
- [x] request受信を `tools/call` validation前、apply開始／終了を既存main-task
  callback内で採時する。
- [x] decorated resultと外側JSON-RPC responseを準備した後にreply enqueueを
  採時し、既存のmain-task同一turn送信へ渡す。scheduler hopは1のままにする。
- [x] 実際のpayload準備後にclockが呼ばれ、直後にsenderへ渡るproduction-path
  testを追加してGREENを確認する。

## Task 4: 計測だけの変更であることを検証する

- [x] `cd gateway && uv run pytest && uv run ruff check .`
- [x] `cmake -S firmware/host_test -B firmware/host_test/build`
- [x] `cmake --build firmware/host_test/build -j2`
- [x] `ctest --test-dir firmware/host_test/build --output-on-failure`
- [x] `git diff --check` と新規identifierの `rg` scope search。
- [x] controller parameter、最大ステップ、active-call上限、auto-sleep、合格時runtime
  configに変更がないことを確認する。
- [x] DockerのESP-IDF v5.5.2でStackChan board buildを通す。
- [x] firmwareのflash／reset、live gateway再起動、実機移動の前で停止する。

`scripts/check-dotfiles.sh` はこのrepositoryに存在しないため対象外とし、利用可能な
diff/static checkを実行する。

## 実行記録

- SCL1 field、gateway timing aggregate、SCU1 assembly timing、firmware status
  counter、MCP timing envelopeが未実装の状態でREDを確認した。
- 最終focused testはgateway camera/metrics 65件、firmware camera protocol
  19件、firmware MCP dispatch 5件がpassした。
- 全gateはgateway 865件pass／5件skip、Ruff pass、firmware host 89件pass。
- `git diff --check` とidentifier scope searchはpassした。
- untouchedの比較用worktreeにある既知良好なcached `esp_video 1.3.1` componentを
  利用してStackChan ESP-IDF release buildを通した。同じversion labelのfresh
  registry取得物はdequeue declarationが変わっており、repositoryのpre-build
  override guardがproduction compile前に停止した。
- legacy-frame、monotonic clock、finite bucket、reply enqueue境界の最終修正後、
  Dockerのincremental ESP-IDF buildで `build/xiaozhi.bin` を再生成した。app
  partitionの空きは26%。
- 独立code reviewの指摘を修正して再testした。独立security reviewは認証済み
  home-LAN threat modelでsecurity findingなし、risk acceptableと評価した。
- firmwareをflashせず、live gateway／device stateを変更していない。
