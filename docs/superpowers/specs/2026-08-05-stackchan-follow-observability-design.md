# StackChan人物追従の観測性設計

## 目的

ユーザーが承認した `operator-present-face-follow-60s-1` を挙動の基準として
固定する。制御器、最大ステップ4度、ばね動作、auto-sleep設定、active call
上限を変えず、カメラ経路とコマンド経路の律速オーナーを段階別の有限集計で
特定できるようにする。

## 基準Run

基準はRun ID `operator-present-face-follow-60s-1` とし、ユーザー主観の
「素晴らしい。回帰はありませんでした」を一次受入条件として固定する。

| 項目 | 合格値 |
|---|---:|
| frames | 477 |
| target | 424（88.9%） |
| moves | 284 |
| command update | 284/284成功、error 0 |
| active／pending最大 | 1／1 |
| replaced／stale／overlap／post-stop dispatch | すべて0 |
| capture→command p95 | 27ms |
| device dispatch | p95 71ms、最大208ms |
| 観測周期 | 実効7.81fps、p50 126ms、最大183ms |
| inference p95 | 20.1ms |
| 初回target | 1.329秒 |
| 最大target loss gap | 1.611秒 |
| 4度上限到達 | 172/284（約61%） |
| 終了状態 | yaw 0／pitch 33、camera stopped、auto-sleep false |

合格Runは61.06秒間に477フレームを処理し、device sequence 953で終了した。
処理したsequence間には合計475の差分がある。合格時runtimeの観測周期は125ms
で、観測間隔中央値126msおよび実効7.81fpsはconsumer timerと一致する。一方、
sequenceからはStackChan producerがlatest-only gateway経路へ約15.6fpsで
フレームを届けていたと推定できる。

gatewayは250msごとにcamera creditを4つ送る。要求20fpsでは200msでcreditを
消費し、次の補充まで約50msのcaptureを破棄し得る。この条件から約16fpsの
producer上限が予測され、合格Runの推定値と整合する。これは8Hzのconsumer
timerに対しては二次的であり、最初の目標10Hzを上回る余力は残る。

## データフロー

JPEGは次の段階を通る。

1. firmwareがV4L2 dequeueでcaptureを待つ。
2. firmwareがYUYVフレームをJPEGへencodeする。
3. firmwareがSCL1で包み、1件だけのlatest-only送信slotへpublishする。
4. firmwareがSCU1 chunkをUDPで送る。専用camera WebSocketはmedia sessionの
   認証とUDP endpointの折衝だけを担当する。
5. gatewayが最新のSCU1フレーム1件をreassembleする。
6. gatewayがSCL1を検証し、最新JPEG1件だけをメモリへpublishする。
7. 人物追従consumerが観測timerから最新frameを取得する。今後はgateway内serviceが
   `LatestCameraFrameStore` を直接読む。

head commandは別のWebSocket MCP経路を通る。変更前のgatewayは任意の
`mcpStageUs` を受理できたが、firmwareが値を出していなかった。そのため、
device dispatchのp95 71ms／最大208msをmain-task queue滞留、tool実行、
reply enqueueに分離できなかった。

## 計測

計測は有限かつ集計のみとする。

- SCL1 metadataへ `deviceCaptureWaitUs` と `deviceEncodeUs` を追加する。
- delivery creditがないため破棄したfirmware frameを数える。
- firmware capture周期、capture wait、encode時間、gateway UDP assembly時間、
  gateway受信周期、latest-frame waitをcount/p50/p95/p99/maxとして集計する。
- latency sampleは有効数字2桁の上側bucketへまとめる。段階判定に必要な精度を
  残しつつ、histogramの取り得るkey数を有限にする。
- 新しいtiming fieldを持たない旧SCL1も配信する。ただし欠損値を0や推定値で
  正確なstage histogramへ混入させない。
- latest-frame readの即時応答、wait後応答、timeoutを数える。
- 成功したfirmware tool resultへ `receiveToApply`、`toolApply`、
  `applyToReplyEnqueue`、`schedulerHops` を含む `mcpStageUs` を追加する。
  reply enqueueはJSON-RPC payloadを準備した後、既存のmain-task同一turn送信の
  直前に採時する。

通常Runではフレーム単位のtraceを保持せず、画像をdiskへ書かない。ユーザーが明示承認
した立ち上がりloss診断に限り、
`2026-08-05-stackchan-loss-frame-diagnostic-design.md` の境界に従って最大5枚を一時保存し、
解析後に削除する。worker、queue、retry、controller state、planned-pose state、
2本目のactive device callは追加しない。

## 修正オーナーの判定規則

- firmware capture周期が10Hz未満、またはcapture waitに100ms級のtailがあれば、
  camera/driver段階を律速オーナーとする。
- encode時間に100ms級のtailがあれば、firmware JPEG encodeをオーナーとする。
- captureとencodeが速く、no-credit dropがcadence低下を説明する場合はgateway
  credit policyをオーナーとする。
- UDP assemblyまたはgateway受信周期に100ms級のtailがあればmedia transportを
  オーナーとする。
- upstreamが10Hz超を維持し、latest waitが125msのcaller周期と揃う場合は人物追従
  runtime schedulingを観測7.81fpsのオーナーとする。
- 初回捕捉と再捕捉について、StackChan側で判断できるのはframe到着の有無まで。
  frame到着後の「候補なし」と「association不採用」は、controller変更前にgateway内
  人物追従serviceで分類する。
- command dispatchは `receiveToApply` でfirmware main-task queue滞留、
  `toolApply` でtool callbackを判定する。残るgateway round tripはtransport／
  reply deliveryとして扱う。active callは1を維持する。

## 安全なA/B手順

ユーザーの在席を明示確認した後に限り、次の順で実施する。

1. 動かさない60秒のcamera-only基準を取り、新しいstatusを読む。
2. gateway内へ移植した合格済み125ms設定を変更せずface-followする。
3. gateway人物追従serviceのobservation intervalだけを125から100へ変更する。
4. 最大ステップ4度、gain、spring parameter、speed、active call、auto-sleepは
   変更しない。
5. 各Run後にyaw 0／pitch 33、camera stopped、auto-sleep falseを実測する。
   pitch 23未満は指令しない。
6. 観測9.5fps以上、failure/stale/post-stop dispatch 0、active/pending最大1を
   要求する。`replacement` はdevice dispatch tail中にpending 1件を最新targetへ
   coalesceした回数として計測し、0にするため新しい観測を捨てない。方向反転後も
   最新targetがlaneへ渡ることを要求する。さらに滑らかさ、初動、逆方向、急跳び、
   頷き、固まりについてユーザー主観の回帰がないことを一次条件とする。
7. 100ms Runが不合格なら125msへ戻し、次は計測で特定した段階だけを1変数で
   変更する。

## 対象外

- controller、最大ステップ、spring motion、gain、lookaheadを変更しない。
- gateway active callを1より増やさない。
- 未確認planned poseを累積しない。
- auto-sleepを変更しない。
- 新しい在席確認なしにlive hardwareのflash、reset、reboot、移動を行わない。
- commit、push、upstream向けPR作成を行わない。
