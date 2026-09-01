# stack-chan 自動瞬き初期化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** gateway と ESP32 の接続成立時に、明示的な無効化を尊重しながら firmware 既存の自動瞬きを有効化する。

**Architecture:** `ESP32Connection` に接続単位の瞬き操作送信フラグを追加し、`ESP32Manager._init_device` がツール発見後に自動 idle 表示と独立して `set_blink(true)` を best-effort 送信する。表情描画、瞬き周期、口パク中の抑制と復帰は firmware の既存実装をそのまま利用する。

**Tech Stack:** Python 3.10+、asyncio、pytest/pytest-asyncio、Ruff、ESP32 MCP tools

---

## ファイル構成

- Modify: `gateway/stackchan_mcp/esp32_client.py` — 接続単位の操作追跡と接続初期化を所有する。
- Modify: `gateway/tests/test_esp32_client.py` — MCP 呼び出し追跡、自動初期化、失敗時継続を検証する。
- Reference: `docs/superpowers/specs/2026-08-31-stackchan-auto-blink-design.md` — 受け入れ条件の正本。

firmware と公開ツール定義は既に必要な機能を持つため変更しない。

### Task 1: 接続単位の瞬き操作追跡をTDDで追加する

**Files:**
- Modify: `gateway/tests/test_esp32_client.py`
- Modify: `gateway/stackchan_mcp/esp32_client.py:79-80,176-205,290-300`

- [x] **Step 1: 失敗するテストを書く**

`ESP32Connection.call_tool` の既存テスト群に、通常ツールではフラグが立たず、`self.display.set_blink` では引数が `true` でも `false` でもフラグが立つテストを追加する。

```python
@pytest.mark.asyncio
async def test_call_tool_tracks_explicit_blink_control():
    ws = _AutoMcpWebSocket()
    connection = ESP32Connection(ws, session_id="session-blink")
    ws.connection = connection

    assert connection.blink_control_sent is False
    await connection.call_tool("self.display.set_blink", {"enabled": False})
    assert connection.blink_control_sent is True
```

- [x] **Step 2: テストを実行してRedを確認する**

Run:

```bash
cd gateway && uv run pytest tests/test_esp32_client.py::test_call_tool_tracks_explicit_blink_control -q
```

Expected: `ESP32Connection` に `blink_control_sent` がないため FAIL。

- [x] **Step 3: 最小実装を追加する**

`esp32_client.py` にツール名、状態、読み取りプロパティ、送信時の記録を追加する。

```python
_SET_AVATAR_TOOL = "self.display.set_avatar"
_SET_BLINK_TOOL = "self.display.set_blink"

# ESP32Connection.__init__
self._avatar_render_sent = False
self._blink_control_sent = False

@property
def blink_control_sent(self) -> bool:
    return self._blink_control_sent

# ESP32Connection.call_tool
if name == _SET_AVATAR_TOOL:
    self._avatar_render_sent = True
if name == _SET_BLINK_TOOL:
    self._blink_control_sent = True
```

- [x] **Step 4: 対象テストを実行してGreenを確認する**

Run:

```bash
cd gateway && uv run pytest tests/test_esp32_client.py::test_call_tool_tracks_explicit_blink_control -q
```

Expected: `1 passed`。

### Task 2: 接続初期化時の自動瞬きをTDDで追加する

**Files:**
- Modify: `gateway/tests/test_esp32_client.py:817-1000`
- Modify: `gateway/stackchan_mcp/esp32_client.py:1433-1482`

- [x] **Step 1: テスト用接続を瞬き対応にする**

`_InitDeviceConnection` に `blink_control_sent`、ツール別エラー、ツール別例外を持たせ、`discover_tools` の返却に `self.display.set_blink` を加える。

```python
def __init__(
    self,
    *,
    avatar_render_sent: bool = False,
    blink_control_sent: bool = False,
    discover_ok: bool = True,
    tool_errors: dict[str, dict] | None = None,
    tool_exceptions: dict[str, Exception] | None = None,
) -> None:
    self.avatar_render_sent = avatar_render_sent
    self.blink_control_sent = blink_control_sent
    self.tool_errors = tool_errors or {}
    self.tool_exceptions = tool_exceptions or {}
```

`call_tool` は呼び出しを記録して該当フラグを立て、ツール別の例外またはエラーを返す。

```python
async def call_tool(self, name: str, arguments: dict):
    self.call_tool_calls.append((name, arguments))
    if name == "self.display.set_avatar":
        self.avatar_render_sent = True
    if name == "self.display.set_blink":
        self.blink_control_sent = True
    if exception := self.tool_exceptions.get(name):
        raise exception
    return {"content": [{"type": "text", "text": "true"}]}, self.tool_errors.get(name)
```

- [x] **Step 2: 正常系とユーザー操作尊重の失敗テストを書く**

次をテストする。

```python
assert connection.call_tool_calls == [
    ("self.display.set_avatar", {"face": "idle"}),
    ("self.display.set_blink", {"enabled": True}),
]
```

- 通常接続では idle の後に blink を送る。
- `avatar_render_sent=True` では idle を送らず blink のみ送る。
- `blink_control_sent=True` では blink を送らず、未送信なら idle のみ送る。
- 再接続では新しい各接続に idle と blink を送る。
- ツール発見失敗時はどちらも送らない。

- [x] **Step 3: 失敗系の失敗テストを書く**

次を独立して検証する。

- `set_avatar` が MCP エラーまたは例外でも `set_blink(true)` を送って ready まで進む。
- `set_blink` が MCP エラーまたは例外でも warning を記録して ready まで進む。

Run:

```bash
cd gateway && uv run pytest tests/test_esp32_client.py -k 'auto_idle or auto_blink or reconnect_auto or explicit_blink or tracks_explicit_blink' -q
```

Expected: 自動瞬き実装がないため、blink 呼び出しを期待するテストが FAIL。

- [x] **Step 4: 自動瞬きの最小実装を追加する**

`_init_device` で自動 idle の直後に新しい補助メソッドを呼ぶ。

```python
await self._auto_render_idle_avatar(connection, device_id)
await self._auto_enable_blink(connection, device_id)
await self._ensure_camera_stream_ready(connection)
```

補助メソッドは明示操作を尊重し、例外と MCP エラーを warning に留める。

```python
async def _auto_enable_blink(
    self, connection: ESP32Connection, device_id: str
) -> None:
    """Best-effort blink enable after a fresh device session init."""
    if connection.blink_control_sent:
        return

    logger.info("auto-enabling avatar blink: device=%s", device_id)
    try:
        _result, error = await connection.call_tool(
            _SET_BLINK_TOOL,
            {"enabled": True},
        )
    except Exception as exc:
        logger.warning(
            "auto-enabling avatar blink failed: device=%s error=%s",
            device_id,
            exc,
        )
        return

    if error:
        logger.warning(
            "auto-enabling avatar blink failed: device=%s error=%s",
            device_id,
            error,
        )
```

- [x] **Step 5: 対象テストを実行してGreenを確認する**

Run:

```bash
cd gateway && uv run pytest tests/test_esp32_client.py -k 'auto_idle or auto_blink or reconnect_auto or explicit_blink or tracks_explicit_blink' -q
```

Expected: 選択されたテストがすべて PASS。

### Task 3: 回帰検証とローカル反映を行う

**Files:**
- Verify: `gateway/stackchan_mcp/esp32_client.py`
- Verify: `gateway/tests/test_esp32_client.py`

- [x] **Step 1: gateway 全体を検証する**

Run:

```bash
cd gateway && uv run pytest
cd gateway && uv run ruff check .
```

Expected: 全テスト PASS、Ruff `All checks passed!`。

- [x] **Step 2: 差分の構文・スコープを検査する**

Run:

```bash
git diff --check
rg -n "_SET_BLINK_TOOL|blink_control_sent|auto-enabling avatar blink" gateway/stackchan_mcp/esp32_client.py gateway/tests/test_esp32_client.py
git status --short
```

Expected: whitespace error なし。新識別子は接続管理実装とそのテストにだけ現れる。変更ファイルは仕様書、計画書、実装、テストに限定される。

- [x] **Step 3: ローカルgatewayへ反映する**

現在動作中の gateway の起動元を読み取りで特定し、リポジトリ外のユーザーデータを変更せず、既存の導入方式に合わせてローカルパッケージを再導入する。起動元が `uv tool` の場合は次を使う。

```bash
uv tool install --force ./gateway
```

その後、既存のホストまたは LaunchAgent の方法で gateway を再起動する。直接プロセスを停止しただけでは stdio MCP は自動再起動しないため、起動元を確認してから行う。

- [x] **Step 4: 実機確認可能な状態を検証する**

Run:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
pgrep -fl stackchan_mcp
```

Expected: gateway が `:8765` で LISTEN し、ESP32 が再接続する。接続ログに idle 自動表示と blink 自動有効化が記録される。

最終的な目視確認は、ユーザーが画面を約6秒観察して瞬きすること、別表情でも瞬きすること、口パク後に瞬きが戻ることを確認する。

## コミット方針

このリポジトリの `AGENTS.md` は明示的な依頼がある場合にだけコミットを許可している。今回はコミット依頼がないため、検証済みの変更を未コミットで提示する。
