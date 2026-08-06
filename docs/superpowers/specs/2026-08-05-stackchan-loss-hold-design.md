# StackChan target loss時hold設計

> 状態: 反復実機A/B後のユーザー評価により不採用。
> `2026-08-07-stackchan-loss-home-recovery-design.md` が置き換える。

## 目的

head/face候補が1秒以上消えたとき、観測根拠なしにhomeへ移動する既存の `scan`
遷移を除去する。最後に確認された実機姿勢を維持し、立ち上がり時に顔が画面上端へ
抜けた後の逆方向移動と長時間の再捕捉遅延を防ぐ。

## 根拠

`stackchan-loss-frame-diagnostic-100ms-1` では、立ち上がり後の最長lossが8.306秒、
gateway観測では `no_candidate` が82回連続した。画像では顔が上端の外へ出る一方、
body候補が132フレーム中129フレームで残った。カメラ未到着、stale、association不採用、
推論errorは0だった。

現在のcontrollerは最後のtargetから1秒後に `scan` へ入り、confirmed poseがhome以外なら
yaw 0度／pitch 33度へ移動する。これは新しい視覚観測に基づかないため、探索動作を
行わないという受入条件と一致しない。

## 変更する契約

- targetをまだ一度も取得していない場合、controllerは `acquire` のままholdする。
- target取得後に候補がなくなった場合、経過時間に関係なく `hold` のままにする。
- no-candidate中はhead commandを生成せず、最後のconfirmed poseを実機上で維持する。
- targetが再び検出された時点で、既存のconfirmed pose基準controllerへそのまま戻る。
- lifecycleの明示的な `stop` は従来どおりlaneを停止し、yaw 0度／pitch 33度へhomeして
  camera leaseを解放する。

## 実装境界

`gateway/stackchan_mcp/face_follow.py` のpure controllerだけを変更する。

- `AttentionMode` から `scan` を除く。
- `AttentionState.scan_arrived_at_ms` を除く。
- `LOST_HOLD_MS`、`SCAN_DWELL_MS`、`_scan()` を除く。
- detectionなしの分岐を、初回は `acquire`、取得後は `hold` を返す単一のhold処理にする。

observation interval 100ms、camera fps/quality、head/face detector、threshold、gain、
dead zone、最大ステップ4度、move speed、latest-only lane、active call上限1、pitch下限23度、
auto-sleepには触れない。body検出による補助はこのA/Bへ含めない。

## テスト

- target取得後、1秒未満と1秒超の両方で `effect.kind == "hold"` かつmove poseなしを要求する。
- まだtargetを取得していないno-candidateではmodeが `acquire` のままであることを要求する。
- target再取得時は既存のconfirmed poseから最大4度以内のmoveになることを要求する。
- service stopがlane停止後にhomeを指令しcameraを止める既存testを維持する。
- gateway focused test、全pytest、Ruff、`git diff --check` を通す。

## 実機A/B

ユーザー在席時に、正面中央の座位から開始し、10秒後のチャイムでその場に立つ30秒Runを
実施する。通常の左右追従、滑らかさ、急跳び、頷き、固まり、再捕捉テンポを一次条件とする。

Run中のno-candidate区間でcommandが増えず、confirmed poseが最後のtarget姿勢からhomeへ
戻らないことを確認する。active/pending最大1、post-stop dispatch 0を維持し、終了後に
yaw 0度相当／pitch 33度、camera stopped、auto-sleep falseを実測する。

このA/Bでloss自体が残っても、body補助を同時には追加しない。home復帰除去の効果を確定した
後にだけ、上端へ接したbodyを縦方向の確認済み観測として使う別A/Bを設計する。

## リポジトリ操作

変更はStackChan fork内に限定する。明示依頼がないためcommit、push、PRを行わない。
