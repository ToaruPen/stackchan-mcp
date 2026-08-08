# Secretlint設定設計

## 背景

リポジトリの共通指示では、`npx secretlint .`による秘密情報検査を
リポジトリ管理のpre-commitで強制すると定めている。一方、現状のルートには
Secretlintの設定、Node.js依存定義、pre-commitフック、CIジョブがなく、
コマンドは設定不在で失敗する。

## 目的

- 新規cloneで依存を再現可能に導入できるようにする。
- 開発者が`npm run secretlint`または`npx secretlint .`で全体を検査できるようにする。
- commit前とGitHub Actionsの両方で同じSecretlint設定を強制する。
- 管理対象のソース、設定、文書は走査し、管理外の第三者コードだけを除外する。

## 採用構成

ルートをprivateなnpmツールプロジェクトとして扱う。Node.js 22以上を要求し、
`secretlint`、推奨preset、Huskyを正確なバージョンでdevDependencyへ固定する。
lockfileはnpmで生成し、ローカルとCIは同じ依存グラフを使用する。

| ファイル | 責務 |
|---|---|
| `package.json` | Node.js要件、固定した開発依存、SecretlintとHuskyのscripts |
| `package-lock.json` | 再現可能なnpm依存グラフ |
| `.gitignore` | ローカルに導入した`node_modules`をGit管理から除外 |
| `.secretlintrc.json` | `@secretlint/secretlint-rule-preset-recommend`の有効化 |
| `.secretlintignore` | 管理外のfirmwareサブモジュールだけを除外 |
| `.husky/pre-commit` | commit直前にリポジトリ全体の秘密情報を検査 |
| `.github/workflows/build.yml` | pull requestとmainへのpushでSecretlintを必須実行 |
| `CONTRIBUTING.md` | 必要なNode.js版、導入、手動実行、フック動作を説明 |

採用バージョンは`secretlint` 13.0.4、
`@secretlint/secretlint-rule-preset-recommend` 13.0.4、Husky 9.1.7とする。
Secretlint 13の要件に合わせ、CIはNode.js 22を使う。

## 走査範囲

Secretlint 13は`.gitignore`を既定で尊重する。そのため`.env`、build成果物、
ローカルsdkconfig、仮想環境などは既存方針のまま除外される。`node_modules`は
Secretlintの組み込み規則に加え、Gitの作業treeへ現れないよう`.gitignore`でも除外する。
`.secretlintignore`にはGit submoduleである
`firmware/components/smooth_ui_toolkit/**`だけを明記する。所有するソース、文書、
workflow、lockfileは除外しない。

`gateway/tests/test_cli.py`には、Basic Authを出力前にredactする挙動を検証するための
ダミーURLが2件ある。ファイル全体は除外せず、Basic Auth子ruleの`allows`へ検出部分の
完全リテラルだけを登録する。これにより、同じファイル内の別の認証情報は引き続き
検出される。

## pre-commit

`npm install`または`npm ci`の`prepare` scriptでHuskyを設定する。
`.husky/pre-commit`は`npm run secretlint`を実行し、検出時の終了コード1をそのまま
commit失敗として扱う。CIでは`HUSKY=0`を指定し、フック設定だけを抑止する。
Secretlint本体は別stepで必ず実行する。

## CI

既存のBuild workflowへ独立した`secretlint` jobを追加する。checkout時の認証情報を
作業treeへ残さず、Node.js 22をnpm cache付きで設定し、`npm ci`の後に
`npm run secretlint`を実行する。firmwareやgatewayのビルドとは独立させ、失敗原因を
Secretlintのjobへ限定して表示できるようにする。

## 検証

- 設定追加前に`npm run secretlint`が設定不在で失敗することを確認する。
- `npm ci`と`npm run secretlint`が終了コード0になることを確認する。
- stdinへ実行時に組み立てたcanary tokenを渡し、終了コード1になることを確認する。
- Husky hookが実行可能で、同じnpm scriptを呼ぶことを確認する。
- workflowのYAML構文、JSON構文、`git diff --check`を検証する。
- 既存のgateway lint/testとfirmware host testを再実行する。

## 対象外

- Git履歴全体の秘密情報走査
- 既存の秘密情報を自動失効・削除する処理
- 第三者firmwareサブモジュール内の指摘修正
- firmwareまたはgatewayの実行時動作の変更
