# esp_video DQBUF パッチ互換対応 設計

## 目的

`firmware/scripts/release.py` が、依存として固定された `esp_video 1.3.1` の現行配布ソースに対しても、StackChan用の有限な DQBUF 待ち時間パッチを再現可能に適用できるようにする。

## 背景

同じ `esp_video 1.3.1` でも、DQBUF待ち時間の表現には次の既知形状がある。

- `portMAX_DELAY` を呼び出しへ直接渡す従来形
- `uint32_t ticks = portMAX_DELAY;` を宣言し、`ticks` を渡す現行配布形
- `uint32_t ticks = pdMS_TO_TICKS(ESP_VIDEO_DQBUF_TIMEOUT_MS);` を宣言し、`ticks` を渡す旧ローカル修正形
- `ESP_VIDEO_DQBUF_WAIT_TICKS` を呼び出しへ渡す正式パッチ適用済み形

現在の処理は現行配布形の宣言を認識できず、クリーンなフルビルドが `esp_video dequeue declaration changed` で停止する。

## 設計

ソース文字列の変換を、副作用のない内部関数として `release.py` に分離する。この関数は既知の4形状を正式パッチ適用済み形へ正規化する。

`apply_esp_video_dqbuf_timeout()` は引き続き次を担当する。

- `esp_video` のバージョンが `1.3.1` であることの検証
- 対象ソースとCMakeファイルの読み書き
- 純粋関数によるソース変換の呼び出し
- パッチ用includeディレクトリの追加

変換後は次の状態へ統一する。

- `esp_video_dqbuf_timeout.h` を一度だけincludeする
- DQBUF関数内に一時的な `ticks` 宣言を残さない
- `esp_video_recv_element(..., ESP_VIDEO_DQBUF_WAIT_TICKS)` を使用する

## エラー処理

既知形状に一致しないinclude、DQBUF宣言、受信呼び出し、またはCMake構造は安全に推測せず、現在と同様に説明付き `RuntimeError` で停止する。対象バージョンも `1.3.1` のまま固定する。

## テスト

`firmware/host_test/test_release.py` に以下を追加する。

- 現行配布形を正式パッチ形へ変換できること
- 従来形と旧ローカル修正形も同じ結果へ収束すること
- 適用済み形を再適用しても結果が変わらないこと
- 未知のDQBUF形状を拒否すること

Redフェーズでは、現行配布形のテストが既存実装に対して失敗することを確認する。Green後はホストテスト全体を実行し、最後にDockerの正式コマンドでStackChanフルビルドを行う。

## 対象外

- `esp_video` の将来バージョンを曖昧な正規表現で許容すること
- upstreamコンポーネント自体の追跡またはvendor化
- タイムアウト値やカメラストリーミング仕様の変更
- 実機へのフラッシュ
