# esp_video DQBUF Patch Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `esp_video 1.3.1` の現行配布ソースを含む既知の4形状へ、StackChanの有限DQBUF待ち時間パッチを冪等に適用できるようにする。

**Architecture:** ソース文字列の正規化を副作用のない内部関数へ分離し、既存の `apply_esp_video_dqbuf_timeout()` はバージョン検証とファイル入出力を担当する。既知形状だけを完全一致で受け入れ、未知形状は従来どおり `RuntimeError` で停止する。

**Tech Stack:** Python 3、標準ライブラリ `unittest`、ESP-IDF v5.5.2 Dockerビルド

**Commit policy:** リポジトリの `AGENTS.md` に従い、明示的に依頼されない限りコミットしない。

---

## File map

- Modify: `firmware/host_test/test_release.py` — 既知形状、冪等性、未知形状拒否の回帰テスト
- Modify: `firmware/scripts/release.py` — 純粋なソース正規化関数と既存ファイル操作からの呼び出し
- Verify: `firmware/managed_components/espressif__esp_video/src/esp_video_ioctl.c` — クリーン取得される現行配布形。追跡・恒久編集はしない

### Task 1: 回帰テストをRedにする

**Files:**
- Modify: `firmware/host_test/test_release.py`

- [x] **Step 1: 既知形状を生成するテストヘルパーを追加する**

```python
def _dqbuf_source(
    tick_declaration: str = "",
    wait_expression: str = "portMAX_DELAY",
    *,
    patched_include: bool = False,
) -> str:
    include = '#include "esp_video.h"\n'
    if patched_include:
        include += '#include "esp_video_dqbuf_timeout.h"\n'
    include += '#include "esp_video_vfs.h"'
    tick_line = f"    {tick_declaration}\n" if tick_declaration else ""
    return (
        f"{include}\n\n"
        "static esp_err_t esp_video_ioctl_dqbuf("
        "struct esp_video *video, struct v4l2_buffer *vbuf)\n"
        "{\n"
        "    esp_err_t ret;\n"
        f"{tick_line}"
        "    struct esp_video_buffer_info info;\n"
        "    struct esp_video_buffer_element *element;\n\n"
        f"    element = esp_video_recv_element(video, vbuf->type, {wait_expression});\n"
        "    return ESP_OK;\n"
        "}\n"
    )
```

- [x] **Step 2: 4形状の収束、冪等性、未知形状拒否をテストする**

```python
class EspVideoDqbufPatchTest(unittest.TestCase):
    def test_known_source_shapes_converge_to_patched_source(self) -> None:
        expected = _dqbuf_source(
            wait_expression="ESP_VIDEO_DQBUF_WAIT_TICKS",
            patched_include=True,
        )
        sources = {
            "direct-port-max-delay": _dqbuf_source(),
            "current-upstream-ticks": _dqbuf_source(
                "uint32_t ticks = portMAX_DELAY;",
                "ticks",
            ),
            "legacy-local-timeout": _dqbuf_source(
                "uint32_t ticks = pdMS_TO_TICKS(ESP_VIDEO_DQBUF_TIMEOUT_MS);",
                "ticks",
            ),
            "already-patched": expected,
        }

        for name, source in sources.items():
            with self.subTest(name=name):
                self.assertEqual(
                    release._patch_esp_video_dqbuf_source(source),
                    expected,
                )

    def test_patched_source_is_idempotent(self) -> None:
        source = _dqbuf_source(
            wait_expression="ESP_VIDEO_DQBUF_WAIT_TICKS",
            patched_include=True,
        )

        once = release._patch_esp_video_dqbuf_source(source)

        self.assertEqual(release._patch_esp_video_dqbuf_source(once), once)

    def test_unknown_declaration_is_rejected(self) -> None:
        source = _dqbuf_source(
            "const TickType_t ticks = portMAX_DELAY;",
            "ticks",
        )

        with self.assertRaisesRegex(RuntimeError, "dequeue declaration changed"):
            release._patch_esp_video_dqbuf_source(source)
```

- [x] **Step 3: 対象テストを実行し、未実装関数による失敗を確認する**

Run:

```bash
cd firmware
python3 -m unittest host_test.test_release.EspVideoDqbufPatchTest -v
```

Expected: `AttributeError: module 'release' has no attribute '_patch_esp_video_dqbuf_source'` を原因として失敗する。

### Task 2: 最小実装でGreenにする

**Files:**
- Modify: `firmware/scripts/release.py`
- Test: `firmware/host_test/test_release.py`

- [x] **Step 1: ソース正規化関数を追加する**

```python
def _patch_esp_video_dqbuf_source(source_text: str) -> str:
    source_include = '#include "esp_video.h"\n#include "esp_video_vfs.h"'
    patched_include = (
        '#include "esp_video.h"\n'
        '#include "esp_video_dqbuf_timeout.h"\n'
        '#include "esp_video_vfs.h"'
    )
    source_declaration = (
        "static esp_err_t esp_video_ioctl_dqbuf("
        "struct esp_video *video, struct v4l2_buffer *vbuf)\n"
        "{\n"
        "    esp_err_t ret;\n"
        "    struct esp_video_buffer_info info;"
    )
    tick_declarations = (
        "    uint32_t ticks = portMAX_DELAY;\n",
        "    uint32_t ticks = pdMS_TO_TICKS(ESP_VIDEO_DQBUF_TIMEOUT_MS);\n",
    )
    source_declarations = tuple(
        source_declaration.replace(
            "    struct esp_video_buffer_info info;",
            tick_declaration + "    struct esp_video_buffer_info info;",
        )
        for tick_declaration in tick_declarations
    )
    source_dequeue = "esp_video_recv_element(video, vbuf->type, portMAX_DELAY)"
    legacy_dequeue = "esp_video_recv_element(video, vbuf->type, ticks)"
    patched_dequeue = (
        "esp_video_recv_element("
        "video, vbuf->type, ESP_VIDEO_DQBUF_WAIT_TICKS)"
    )

    if source_include in source_text:
        source_text = source_text.replace(source_include, patched_include, 1)
    elif patched_include not in source_text:
        raise RuntimeError("esp_video include context changed; update the override")

    for declaration in source_declarations:
        if declaration in source_text:
            source_text = source_text.replace(declaration, source_declaration, 1)
            break
    else:
        if source_declaration not in source_text:
            raise RuntimeError(
                "esp_video dequeue declaration changed; update the override"
            )

    if source_dequeue in source_text:
        source_text = source_text.replace(source_dequeue, patched_dequeue, 1)
    elif legacy_dequeue in source_text:
        source_text = source_text.replace(legacy_dequeue, patched_dequeue, 1)
    elif patched_dequeue not in source_text:
        raise RuntimeError("esp_video dequeue context changed; update the override")

    return source_text
```

- [x] **Step 2: 既存のファイル操作から正規化関数を呼ぶ**

`apply_esp_video_dqbuf_timeout()` からinclude、宣言、dequeue呼び出しを直接変換するブロックを除き、次へ置き換える。

```python
    source_text = _patch_esp_video_dqbuf_source(
        source.read_text(encoding="utf-8")
    )
    source.write_text(source_text, encoding="utf-8")
```

CMakeの `private_include` 変換と `esp_video 1.3.1` のバージョン検証は変更しない。

- [x] **Step 3: 対象テストを実行してGreenを確認する**

Run:

```bash
cd firmware
python3 -m unittest host_test.test_release.EspVideoDqbufPatchTest -v
```

Expected: 9 tests pass。

- [x] **Step 4: ホストテスト全体を実行する**

Run:

```bash
cd firmware
python3 -m unittest discover -s host_test -p 'test_*.py' -v
```

Expected: 全Pythonホストテストがpassし、failure/errorが0件。

### Task 3: クリーン依存からフルビルドを検証する

**Files:**
- Verify: `firmware/scripts/release.py`
- Verify: `firmware/managed_components/espressif__esp_video/src/esp_video_ioctl.c`
- Verify: `firmware/build/xiaozhi.bin`
- Verify: `firmware/build/merged-binary.bin`
- Verify: `firmware/releases/v2.2.6_stackchan.zip`

- [x] **Step 1: 既存の生成物とesp_video管理コンポーネントを一時ディレクトリへ退避する**

`mktemp -d` で退避先を作り、`build`、対象ZIP、`managed_components/espressif__esp_video` を削除せず移動する。アバターの `avatar_images.local.cc/.h` は残す。

- [x] **Step 2: 正式なDockerコマンドでフルビルドする**

Run from repository root:

```bash
docker run --rm --cpus=4 --ulimit nofile=65536:65536 \
  -v "$PWD":/project -w /project/firmware \
  espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
```

Expected: exit code 0、`2220/2220`まで完了し、`v2.2.6_stackchan.zip` が生成される。クリーン取得された現行配布形で `esp_video dequeue declaration changed` が発生しない。

- [x] **Step 3: 成果物とアバター組み込みを検査する**

Run:

```bash
ls -lh firmware/build/xiaozhi.bin firmware/build/merged-binary.bin firmware/releases/v2.2.6_stackchan.zip
unzip -t firmware/releases/v2.2.6_stackchan.zip
rg -n "avatar_images\\.local\\.cc" firmware/build/build.ninja
shasum -a 256 firmware/build/xiaozhi.bin firmware/build/merged-binary.bin firmware/releases/v2.2.6_stackchan.zip
git diff --check
git status --short
```

Expected: 3成果物が存在し、ZIP検査にエラーがなく、ローカルアバターソースがリンク対象で、差分に空白エラーがない。追跡差分は仕様書、計画書、`release.py`、`test_release.py`だけである。

- [x] **Step 4: 実機操作を行っていないことを確認する**

フラッシュ、リセット、シリアル接続は実行しない。
