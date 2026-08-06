# StackChan立ち上がり時loss画像診断設計

## 目的

正面中央での立ち上がり時に観測された約1.2秒の `no_candidate` が、カメラ画像の
動体ブレ・露出・画角端での欠けに起因するのか、十分な画像に対するhead/face
検出モデルの失敗なのかを切り分ける。

## 診断方法

productionの追従コードは変更しない。gateway-owned face-followを既知良好な
100ms観測、最大ステップ4度、active call 1のまま実行し、別のローカル診断clientが
既存の認証済み `/camera/latest` をlong-pollする。取得JPEGは計測中だけ有界な
メモリに保持し、disk I/Oを追従ループへ追加しない。

追従停止後、同じPINTOモデルをメモリ上のJPEGへオフライン適用し、最長の
head/face候補なし区間を選ぶ。その区間について次の最大5枚だけを権限600で一時保存
する。

1. loss直前のtarget検出フレーム1枚
2. loss開始フレーム1枚
3. loss中央フレーム1枚
4. loss終了直前フレーム1枚
5. 再捕捉フレーム1枚

候補なし区間が短い場合は重複を除き、5枚未満とする。保存画像にはtoken、env、
device identifierなどを含めない。

## 判定

- 顔が画像外または大きく欠けていれば、画角・カメラ姿勢・入力cropを修正候補とする。
- 顔が残っていて強いブレや露出崩れがあれば、camera capture条件を修正候補とする。
- 顔が判別可能なのにhead/face出力がなければ、モデルまたは前処理を修正候補とする。
- loss中もbody候補が安定して残る場合だけ、確認済みbody観測を一時的な補助にする案を
  後続の1変数A/B候補とする。bodyを通常targetへ置き換えない。

## 安全・プライバシー

- ユーザーの在席確認後にのみlive Runを開始する。
- 探索動作、lookahead、planned pose累積、閾値変更、controller変更を行わない。
- pitch 23度未満を指令せず、auto-sleepを変更しない。
- Run後にyaw 0度相当／pitch 33度、camera stopped、auto-sleep falseを実測する。
- 画像はローカルの権限700 directoryに権限600で保存し、視覚確認と数値抽出後に削除する。
- 永続証拠には画像を含めず、blur・露出・edge clipping・各classの有無などの有限な
  判定結果とRun JSONだけを残す。

## 受入条件

- 既知良好な追従パラメータとdevice call concurrencyが不変である。
- 最大5枚の選択が再現可能なframe sequenceと対応する。
- 画像から原因候補を少なくとも「画角外／画質／モデル・前処理」のいずれかへ絞る。
- 一時画像を削除し、camera stopped、auto-sleep falseを確認して終了する。
- commit、push、upstream PRは行わない。
