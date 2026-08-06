# StackChan target loss時home recovery設計

## 目的

顔／頭を一定時間検出できない場合、最後の姿勢を長時間維持せず、予測可能なdefault姿勢
yaw 0度／pitch 33度へ戻る。探索、body fallback、過去boxや未確認poseの蓄積を追加せず、
既知の通常追従を変えない。

## 根拠

hold-only A/Bの60秒反復Runでは、599フレーム中188フレームが `no_candidate`、最長lossは
6.001秒だった。観測なしcommandは0だったが、ユーザーは「見失った際にずっと上を向いて
いることや、左を向いていることがあった」と評価した。homeへ勝手に動く回帰は除去できた
一方、最後の姿勢で固まるため一次受入条件を満たさなかった。

## controller契約

優先順位は `顔 > 頭 > 短時間hold > home recovery` とする。

- targetを一度も取得していない間は `acquire` のままholdする。
- 最後の顔／頭targetから750ms未満は、最後のconfirmed poseでholdする。
- 750ms到達後は `recover` へ遷移し、yaw 0度／pitch 33度を絶対目標としてhead laneへ渡す。
- head laneは既存の最大4度、10Hz、active call 1でhomeまで段階的に移動する。
- confirmed poseがhomeに到達した後は `recover` のままholdし、探索しない。
- recovery途中でも顔／頭が戻れば、現在のconfirmed pose基準で既存の通常追従へ戻る。
- 明示的なservice stop時のhome approach、camera停止、auto-sleep非変更は維持する。

ここで「一度だけ戻る」とは、loss episodeごとに一つのhome recoveryを開始するという意味
である。実機へ大角度を一度に指令せず、既存head laneが4度ずつ移動する。home到達後に
同じ指令を繰り返さない。

## 実装境界

`gateway/stackchan_mcp/face_follow.py` のpure controllerと、そのcontrollerへ観測結果を渡す
service内tickだけを変更する。

- `LOST_HOME_TIMEOUT_MS = 750` を追加する。
- `AttentionMode` に `recover` を追加する。`scan` は復活させない。
- no-candidate分岐を `acquire`、750ms未満の `hold`、750ms以後の `recover` に分ける。
- `_recover_home(current_pose)` はhome以外なら `AttentionEffect("move", home)`、homeなら
  `AttentionEffect("hold")` を返す。
- frame timeout、stale frame、推論errorもtargetなしの観測としてcontrollerを進め、
  confirmed pose基準で750ms recoveryを発火させる。

observation interval 100ms、camera fps/quality、detector、face/head優先度、threshold、gain、
dead zone、最大ステップ4度、move speed 90度/秒、pitch下限23度、latest-only lane、
active call上限1、auto-sleepには触れない。body detectionやtarget association stateを
追加しない。

## テスト

- target取得後749msではhold、750msではrecoverを要求する。
- recoverのhome目標がyaw 0度／pitch 33度であることを要求する。
- confirmed poseがhomeならrecover中もmoveを生成しないことを要求する。
- 初回target前は時間に関係なくacquire holdであることを要求する。
- recovery途中の再検出が現在のconfirmed poseから既存最大4度以内の通常追従へ戻ることを
  要求する。
- service stopのhomeとcamera停止に関する既存testを維持する。
- frame timeout、stale frame、推論errorが続いても750msでrecoverすることを要求する。
- stop呼び出し元がcancelされてもlane停止、home、camera releaseを完了することを要求する。
- focused pytest、全gateway pytest、Ruff、`git diff --check` を通す。

## 実機A/B

ユーザー在席時に正面中央から60秒Runを行い、自然な立つ・座る動作を複数回繰り返す。
statusを250ms間隔で記録し、loss開始から最初のrecovery commandまでが750ms以上かつ
観測粒度を含めて1.1秒未満であることを確認する。

通常追従の滑らかさ、逆方向、急跳び、頷き、固まりを一次条件とする。特に、loss時に
上や左を向いたまま残らず、homeへ予測可能に戻ることを確認する。active/pending最大1、
error/stale/post-stop dispatch 0を要求し、終了後にyaw 0度相当／pitch 33度、camera
stopped、auto-sleep falseを実測する。device dispatchが100msを超えたときの
`replaced` は、pending 1件を最新targetへcoalesceするlatest-only契約の結果として許容し、
回数を記録する。replacedを0にするために新しい観測を捨ててはならず、方向反転時も
最新targetがlaneへ渡ることを回帰testで固定する。

## 対象外

- body追従、body相対の推定頭位置、optical flowを追加しない。
- 探索のためのyaw/pitch sweepや複数home候補を追加しない。
- planned poseをcontrollerへ保持しない。
- 設計検証中はcommit、push、PRを行わない。その後の明示依頼があれば別gateとして扱う。
