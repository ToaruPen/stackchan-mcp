# Secretlint Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** リポジトリ全体の秘密情報をローカル、pre-commit、GitHub Actionsで同じ固定済みSecretlint設定により検査できるようにする。

**Architecture:** ルートにprivateなnpmツールプロジェクトを置き、Secretlint 13.0.4と推奨presetをlockfileで固定する。Huskyは同じnpm scriptをpre-commitから呼び、CIはNode.js 22上で独立jobとして同じscriptを実行する。

**Tech Stack:** Node.js 22、npm、Secretlint 13.0.4、Husky 9.1.7、GitHub Actions

---

### Task 1: 設計と計画を記録する

**Files:**
- Create: `docs/superpowers/specs/2026-08-08-secretlint-configuration-design.md`
- Create: `docs/superpowers/plans/2026-08-08-secretlint-configuration.md`

- [ ] **Step 1: 設計書と実装計画を確認する**

Run:

```bash
git diff --check
git diff -- docs/superpowers/specs/2026-08-08-secretlint-configuration-design.md docs/superpowers/plans/2026-08-08-secretlint-configuration.md
```

Expected: whitespace errorがなく、承認済みのnpm、Husky、CI構成だけが記載されている。

- [ ] **Step 2: 設計文書をcommitする**

```bash
git add docs/superpowers/specs/2026-08-08-secretlint-configuration-design.md docs/superpowers/plans/2026-08-08-secretlint-configuration.md
git commit -m "docs: plan Secretlint enforcement"
```

### Task 2: Secretlint不在の失敗を確認する

**Files:**
- Test: repository root command behavior

- [ ] **Step 1: 設定追加前のコマンドを実行する**

Run:

```bash
npm run secretlint
```

Expected: `package.json`が存在しないため非zeroで失敗し、追加対象の挙動がまだないことを確認できる。

### Task 3: ルートSecretlintとpre-commitを実装する

**Files:**
- Create: `package.json`
- Create: `package-lock.json`
- Modify: `.gitignore`
- Create: `.secretlintrc.json`
- Create: `.secretlintignore`
- Create: `.husky/pre-commit`

- [ ] **Step 1: npmツール定義を追加する**

Create `package.json`:

```json
{
  "name": "stackchan-mcp-repository-tools",
  "private": true,
  "engines": {
    "node": ">=22.0.0"
  },
  "scripts": {
    "prepare": "husky",
    "secretlint": "secretlint ."
  },
  "devDependencies": {
    "@secretlint/secretlint-rule-preset-recommend": "13.0.4",
    "husky": "9.1.7",
    "secretlint": "13.0.4"
  }
}
```

- [ ] **Step 2: Secretlint設定と限定的なignoreを追加する**

Create `.secretlintrc.json`:

```json
{
  "rules": [
    {
      "id": "@secretlint/secretlint-rule-preset-recommend",
      "rules": [
        {
          "id": "@secretlint/secretlint-rule-basicauth",
          "options": {
            "allows": [
              "https://user:pass@example.com",
              "https://signer:topsecret@example.com"
            ]
          }
        }
      ]
    }
  ]
}
```

Create `.secretlintignore`:

```text
# Third-party Git submodule; scan repository-owned files only.
firmware/components/smooth_ui_toolkit/**
```

Add to `.gitignore`:

```text
# Node.js
node_modules/
```

- [ ] **Step 3: repository管理のpre-commit hookを追加する**

Create `.husky/pre-commit`:

```sh
npm run secretlint
```

Run:

```bash
chmod +x .husky/pre-commit
npm install
```

Expected: `package-lock.json`が生成され、Huskyが現在のworktreeへ設定される。

- [ ] **Step 4: Secretlintの正常系を確認する**

Run:

```bash
npm run secretlint
```

Expected: 終了コード0で、管理対象ファイルから秘密情報が検出されない。

- [ ] **Step 5: Secretlintの検出系をcanaryで確認する**

Run:

```bash
set +e
printf '%s%s\n' 'ghp_' '0123456789abcdefghijklmnopqrstuvwxyz' \
  | npx secretlint --stdinFileName=secretlint-canary.txt
secretlint_canary_status=$?
set -e
test "$secretlint_canary_status" -eq 1
```

Expected: 検出時の規定終了コード1になる。出力は秘密値が既定でmaskされる。

### Task 4: CIとcontributor向け手順を追加する

**Files:**
- Modify: `.github/workflows/build.yml`
- Modify: `CONTRIBUTING.md`

- [ ] **Step 1: GitHub Actionsへ独立jobを追加する**

Add under `jobs:` in `.github/workflows/build.yml`:

```yaml
  secretlint:
    name: Secretlint
    runs-on: ubuntu-latest
    env:
      HUSKY: 0

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          persist-credentials: false

      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: npm

      - name: Install repository tooling
        run: npm ci

      - name: Run Secretlint
        run: npm run secretlint
```

- [ ] **Step 2: Setup、CI、Secret Scanning手順を更新する**

Update `CONTRIBUTING.md` so Setup lists Node.js 22 + npm, CI lists Secretlint,
and a Secret Scanning section includes:

```bash
npm ci
npm run secretlint
```

Document that `prepare` installs the tracked Husky hook, CI uses `HUSKY=0`,
and `.gitignore` plus the narrow `.secretlintignore` define the scan boundary.

### Task 5: 全体検証、commit、PR公開を行う

**Files:**
- Verify: all changed files

- [ ] **Step 1: 構文とSecretlintを検証する**

Run:

```bash
npm ci
npm run secretlint
test -x .husky/pre-commit
node -e "JSON.parse(require('node:fs').readFileSync('package.json')); JSON.parse(require('node:fs').readFileSync('.secretlintrc.json'))"
gateway/.venv/bin/python -c "import yaml; yaml.safe_load(open('.github/workflows/build.yml', encoding='utf-8'))"
git diff --check
```

Expected: 全commandが終了コード0になる。

- [ ] **Step 2: 既存CI相当の回帰確認を再実行する**

Run:

```bash
cmake -S firmware/host_test -B /tmp/stackchan-secretlint-host-test
cmake --build /tmp/stackchan-secretlint-host-test
ctest --test-dir /tmp/stackchan-secretlint-host-test --output-on-failure
cd gateway
uv run ruff check .
uv run pytest
```

Expected: firmware host test 89件とgateway lint/testが成功する。

- [ ] **Step 3: 実装をcommitする**

```bash
git add package.json package-lock.json .gitignore .secretlintrc.json .secretlintignore .husky/pre-commit .github/workflows/build.yml CONTRIBUTING.md docs/superpowers/specs/2026-08-08-secretlint-configuration-design.md docs/superpowers/plans/2026-08-08-secretlint-configuration.md
git commit -m "ci: enforce repository secret scanning"
```

- [ ] **Step 4: branchをpushしてdraft PRを作成する**

```bash
git push -u origin codex/configure-secretlint
gh pr create --draft --base main --head codex/configure-secretlint \
  --title "ci: enforce repository secret scanning" \
  --body $'## Summary\n\n- Pin Secretlint and its recommended preset at the repository root.\n- Enforce the same scan in Husky pre-commit and GitHub Actions.\n- Document Node.js 22 setup and the scan boundary.\n\n## Test plan\n\n- [x] `npm ci`\n- [x] `npm run secretlint`\n- [x] canary secret returns exit code 1\n- [x] firmware host tests\n- [x] gateway ruff and pytest\n\n### Hardware\n\n- [x] Not applicable; repository tooling only.\n\n## Breaking changes\n\nNone.'
```

Expected: `ToaruPen/stackchan-mcp`にmain向けdraft PRが作成される。
