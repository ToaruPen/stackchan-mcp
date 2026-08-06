# StackChan gateway内人物追従の設計

## 目的

ユーザーが `operator-present-face-follow-60s-1` で「素晴らしい。回帰はありませんでした」
と評価した挙動を基準として固定し、人物追従の所有者を `stackchan-mcp` gatewayへ
移す。Mocoは通常のMCPクライアントとして人物追従の開始、状態取得、停止だけを行い、
画像処理、モデル、制御器、カメラURL、head-target leaseを知らない。

この変更では観測周期を125msのまま維持する。100ms（実効10Hz）へのA/Bは、gateway内
実装が125msで主観・安全・観測性設計の現行A/B契約を再現した後にのみ行う。

## 所有境界

```text
Moco / その他のMCP host
  └─ stackchan_face_follow(action=start|status|stop)
       └─ stackchan-mcp gateway
            ├─ CameraStreamService（20fps要求、quality 60）
            ├─ LatestCameraFrameStore（JPEG latest-only）
            ├─ PINTO ONNX detector（画像はメモリ上のみ）
            ├─ 合格済みattention controller（125ms）
            ├─ HeadTargetLane（10Hz、max step 4°、active最大1）
            └─ ESP32 firmware（camera / servo）
```

Pico実装は制御定数と判断順序を移植するための履歴資料に限る。PicoやMocoへ追従用
コード、設定、依存関係を追加しない。Mocoが将来利用するときも、MCP serverとして
`stackchan-mcp` を登録し、このlifecycle toolを呼ぶだけとする。

## MCP契約

新しいtool名は `stackchan_face_follow` とし、引数は次の厳密なlifecycle契約にする。

- `start`: 追従を開始する。制御値を引数に公開しない。
- `status`: 画像を含まない有限集計と現在のphaseを返す。
- `stop`: 観測を停止し、head laneをdrainして停止し、homeへ戻してからcamera leaseを
  解放する。

モデルpathはgateway起動環境の `STACKCHAN_FACE_FOLLOW_MODEL` から取得する。存在しない
path、モデルload失敗、optional dependency不足はstartを失敗させ、cameraやservoを取得
しない。モデルを自動downloadせず、別モデルへのfallbackもしない。

## 固定する合格済み挙動

| 項目 | 固定値 |
|---|---:|
| camera request | 20fps、JPEG quality 60 |
| observation interval | 125ms baseline / 100ms A/B candidate |
| maximum frame age | 180ms |
| command rate | 10Hz |
| maximum pending age | 180ms |
| home | yaw 0°、pitch 33° |
| tracking pitch lower bound | 23° |
| dead zone / release | 0.10 / 0.14 |
| target filter | disabled |
| yaw / pitch gain | 44° / 15° |
| yaw / pitch direction | +1 / -1 |
| maximum step | 4° |
| quantization | round |
| move speed | 90°/s |
| lost-target hold | 750ms |
| loss recovery | yaw 0°、pitch 33°への4°段階移動 |
| scan | disabled |
| head lane active call | 最大1 |

補正量はdead zoneを差し引いた正規化誤差へgainと方向を掛け、±4°へclampして丸める。
絶対目標は毎tickでhead laneの `confirmed_pose` を基準に算出し、未確認planned poseを
controllerで累積しない。centeredまたはholdではpending targetをclearする。

無期限hold契約は反復実機A/Bで不採用となり、
`2026-08-07-stackchan-loss-home-recovery-design.md` の750ms後home recoveryが置き換える。
scanは復活させず、明示的なservice stop時のhomeも維持する。

PINTO出力は末尾dimension 7のrowとして検証する。class 1をhead、class 3をfaceとし、
thresholdはhead 0.35、face 0.40とする。座標は320x256で正規化し、無効boxを除外する。
faceをheadより優先し、同label内は `confidence * sqrt(area)` が最大の候補を選ぶ。

## runtimeと停止安全性

runtimeの制御はgateway process内の単一async taskとする。tickは同時実行せず、固定
intervalのdeadlineを過ぎた場合は重なった回数を集計して次のfuture deadlineへ進む。
同期ONNX load / inferenceだけはevent loop上のcamera・command処理を塞がないよう
`asyncio.to_thread` へ退避するが、同時推論は1件とし、専用queue、retry、2本目のdevice
callを追加しない。

開始順は、モデルload、camera lease取得、head lane開始、観測task開始とする。開始途中の
失敗は取得済みresourceだけを逆順で解放する。

停止順は、新しいtickを無効化して実行中の観測taskをdrain、pending clear、head lane停止、yaw 0 / pitch 37への
approach、250ms待機、yaw 0 / pitch 33へのhome、camera lease解放とする。home指令は
speed 90°/sを維持し、pitch 23°未満を送らない。auto-sleepの変更toolは呼ばない。
stop statusはlaneとcameraの最終状態およびhome command結果を返し、停止途中の個別失敗を
有限のerror codeとして保持する。

Gateway全体のstopでも、ESP32 managerを閉じる前に人物追従serviceを停止する。

## 観測と再捕捉分解

`status` はsample列、画像、box座標、個人識別情報を保持せず、次をcounterとbounded
histogramで返す。

- outcome: `target_selected`、`no_candidate`、`association_rejected`、
  `frame_wait_timeout`、`frame_stale`、`inference_error`、`tick_overlap_suppressed`
- stage: frame wait、inference、capture-to-decision、tick total
- flow: tick started/completed、frame processed、target frame、move、active tick最大値
- acquisition: 初回targetまでの時間と、その間のoutcome内訳
- reacquisition: target喪失episode数、最長gap、そのgap内のoutcome内訳
- head lane: accepted/replaced/dispatched/confirmed/failed/stale、active/pending最大値、
  device dispatchおよびfirmware stage集計
- camera: 既存のcapture wait、encode、UDP assembly、gateway receive、latest wait集計

現在のselectorはvalid candidateがあれば必ず1件を採用するため、
`association_rejected` は通常0になる。それでも `no_candidate` と別contractにし、今後
associationを導入する場合に再捕捉の所有者を同じstatusで判定できるようにする。

## 依存関係とprivacy

人物追従は `stackchan-mcp[face-follow]` optional extraとし、ONNX Runtime、NumPy、Pillow
を含める。通常install時にはimportせず、他toolの起動へ影響させない。

JPEGは既存latest storeから読み、推論後に参照を破棄する。diskへ画像、frame単位trace、
detection一覧を保存しない。statusへ最後の顔位置やboxを出さない。

## 非実機受入条件

- pure parser、target selection、controller、freshness、episode集計をdeterministic testで
  Red→Greenにする。
- lifecycle testでstart途中失敗のrollback、二重start、idempotent stop、停止後dispatch 0、
  camera release、home 37→33、active tick最大1を確認する。
- MCP schemaとdispatchをtestし、Moco固有moduleのimportや引数がないことを確認する。
- base installでONNX/Pillow/NumPyがなくても通常importと既存testが通ることを確認する。
- gateway全pytest、Ruff、`git diff --check` を通す。

## 実機A/B

新しい在席確認を得た後に限り次の順で行う。

1. camera-only 60秒で段階別cadenceを確認する。
2. gateway内125ms追従を実行し、合格Runとの主観・安全contract回帰がないことを確認する。
3. 125msが合格した後に、observation intervalだけを100msへ変更してA/Bする。
4. 各Run後にyaw 0 / pitch 33、camera stopped、auto-sleep falseを実測する。
5. 100ms Runが不合格なら125msへ戻し、計測で特定したownerだけを1変数で変更する。

実効fpsだけでは合格にしない。滑らかさ、初動、逆方向、急跳び、頷き、固まりに関する
ユーザー主観を一次受入条件とする。gateway active=2、7° lookahead、比例補正全量の
絶対目標化は試さない。

## 対象外

- PicoまたはMocoの変更
- Mocoへの画像処理・モデル・controllerの直組み込み
- upstream向けPR
- 明示依頼前のcommit / push
- 新しい在席確認前のflash、reset、gateway再起動、実機移動
