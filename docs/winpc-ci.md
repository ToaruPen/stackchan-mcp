# WinPCでCI/CDを実行する

`ToaruPen/stackchan-mcp` のCI/CDを、WinPC上の2つのGitHub Actions runnerに割り当てます。LinuxジョブはWSL2、Windows固有のDLL検証とwheel作成はWindowsネイティブで実行します。

`STACKCHAN_SELF_HOSTED_CI` が未設定なら、既存のGitHub-hosted runnerを使います。WinPCがオフラインの状態では切り替えず、以下の準備と確認を行ってください。

## WinPCを準備する

1. WinPCに専用の実行アカウントを用意します。runnerの作業ディレクトリに普段の開発checkoutを指定しないでください。
2. WSL2にUbuntu 24.04 LTS x64を用意し、Linux runner用のディレクトリをLinux側のファイルシステムに作成します。Windows runnerとは別の作業ディレクトリを使います。
3. WSL側にGit、Python 3、CMake、C++コンパイラ、make、libopus、Docker CLIを用意します。Ubuntuのパッケージは管理者が初回にインストールします。ジョブ内ではself-hosted runnerに対して`sudo`を実行しません。

   ```bash
   sudo apt-get update
   sudo apt-get install -y git python3 cmake build-essential libopus0 libopus-dev
   ```

4. Docker DesktopのWSL integration、またはWSL内のDocker Engineを設定します。runnerの実行ユーザーで、Linuxコンテナを使えることを確認します。

   ```bash
   docker info --format '{{.OSType}}'
   docker run --rm --user "$(id -u):$(id -g)" --env HOME=/tmp \
     espressif/idf:v5.5.2 idf.py --version
   python3 -c "import ctypes; from ctypes.util import find_library; name = find_library('opus'); assert name, 'libopus is missing'; ctypes.CDLL(name)"
   ```

   それぞれ`linux`、ESP-IDF v5.5.2、Pythonの正常終了を確認します。ファームウェアの成果物はrunnerと同じUIDで作成されます。host testのビルド先は実行IDと再試行番号で分離されます。

5. Windows側にGit、PowerShell 7、Visual Studio Build ToolsのC++デスクトップ開発ワークロードとWindows SDKを用意します。vcpkgの`opus:x64-windows`をビルドできる構成が必要です。uvとNode.jsは各workflowのsetup actionが用意します。
6. 両方のrunnerからGitHub、PyPI、npm、vcpkgの依存取得先、Espressifの依存取得先に接続できることを確認します。runnerのディスクにはESP-IDFイメージ、依存コンポーネント、ビルド成果物を保持できる空きを用意します。

## runnerを登録する

対象リポジトリの **Settings > Actions > Runners > New self-hosted runner** から、その時点のダウンロードと登録コマンドを取得します。登録トークンをリポジトリやログに保存しないでください。

- WSL内にLinux x64 runnerを登録し、標準の`self-hosted`、`Linux`、`X64`に加えて`stackchan-ci`ラベルを付けます。
- Windows上にWindows x64 runnerを登録し、標準の`self-hosted`、`Windows`、`X64`に加えて`stackchan-ci`ラベルを付けます。

このラベルのrunnerはこのリポジトリ専用にします。WindowsサービスやWSLの起動方法を設定し、PCを再起動した後も両方のrunnerがOnlineになることを確認します。WSLやDockerが停止していればLinuxジョブは実行できません。

PyPI公開にはLinuxとDockerが必要です。Windows runnerだけでは`pypa/gh-action-pypi-publish`を実行できません。既存の`pypi`環境とTrusted Publisherの設定はそのまま使います。PyPI publish actionのself-hosted対応はbest effortです。手動実行では公開処理を検証できないため、実際のタグ公開時のOIDC認証とアップロードは別途確認が必要です。

## 有効にして検証する

公開リポジトリでは、外部のコードがrunnerで実行され得る点を確認してから有効にしてください。このworkflowの通常のPR実行先はGitHub-hostedですが、PRでworkflow自体を書き換えられるため、イベント条件だけで個人PCを隔離できるわけではありません。GitHub側のfork workflow承認設定を確認し、workflow変更を含む未信頼の実行を承認しない運用と、個人データや常駐サービスからのrunnerの分離が必要です。

このリポジトリでは、fork workflowの承認対象をすべての外部コントリビューターに設定します。初回の投稿者だけを対象にする設定では、過去に投稿した外部ユーザーのworkflowが自動実行され得ます。

1. 変更を含むworkflowをGitHubへ反映し、両方のrunnerがOnlineになっていることを確認します。
2. **Settings > Secrets and variables > Actions > Variables** で、リポジトリ変数`STACKCHAN_SELF_HOSTED_CI`を`true`に設定します。環境変数やsecretではなく、リポジトリ変数を使います。
3. **Actions > Build > Run workflow** を実行します。各ジョブのrunner名を開き、LinuxジョブはWSL、Windows smokeはWindows側に割り当てられたことを確認します。Secretlint、ファームウェアのビルドと成果物検査、host test、Gatewayのruffとpytest、WindowsのOpus検証がすべて成功することを確認します。
4. **Publish gateway to PyPI** と **Firmware Release** をそれぞれ手動実行します。wheelとファームウェアのartifactを確認します。手動実行では公開ジョブはスキップされるため、リリースは発生しません。
5. 通常のPR実行がGitHub-hosted runnerを使用し、従来のチェック名とチェック内容が保たれていることを確認します。

変数が`true`で、リポジトリが`ToaruPen/stackchan-mcp`かつイベントが`push`または`workflow_dispatch`の場合だけ、WinPCを選びます。他のリポジトリや通常のPRではGitHub-hostedを使います。タグの検証、公開条件、権限は変更していません。

変数を有効にした後でWinPCが停止すると、ジョブはrunner待ちになります。GitHub-hostedへの自動切り替えはありません。

## ToaruPenでの配置と起動

2026年10月23日の導入先は次のとおりです。

| 項目 | WSL | Windows |
|---|---|---|
| runner名 | `stackchan-toarupen-wsl` | `stackchan-toarupen-windows` |
| 配置先 | `/opt/stackchan-ci/runner` | `%LOCALAPPDATA%\stackchan-ci\runner` |
| 実行ユーザー | 専用の`stackchan-ci` | Windowsのログインユーザー |
| 起動方式 | systemdサービス | ログイン時の`StackchanCIRunner`スタートアップ |

WSLでは`actions.runner.ToaruPen-stackchan-mcp.stackchan-toarupen-wsl.service`を有効にしています。Ubuntu起動時にDockerとrunnerが起動します。Windows側の既存の`WSL-Ubuntu-RemoteAccess`タスクは変更していません。

Windows側の起動スクリプトは`%LOCALAPPDATA%\stackchan-ci\start-runner.ps1`、ログは同じフォルダの`runner.log`です。スタートアップ登録は`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`の`StackchanCIRunner`です。Windows側はログインが必要です。既存の別プロジェクト用runnerサービスは変更していません。

Windows runnerは普段のログインユーザーの権限で動きます。WSLの専用ユーザーもDockerへのアクセス権を持つため、いずれも未信頼コードの隔離環境ではありません。fork workflowの承認時には実行する変更を確認してください。

PCのスリープ設定は変更していません。CI実行中はPCを起動・接続した状態にしてください。WSLの再起動で処理中のジョブが失われた場合は、そのGitHub Actions実行をキャンセルして新しく実行します。

## GitHub-hostedに戻す

`STACKCHAN_SELF_HOSTED_CI`を削除するか`false`に設定します。すでに待機中の実行はキャンセルし、新しいworkflow実行を開始してください。実行途中のジョブの移動や、同じ実行の再試行による切り替えには頼らないでください。

## 参照

- [GitHubのself-hosted runnerラベル](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/use-in-a-workflow)
- [EspressifのDocker実行方法](https://docs.espressif.com/projects/esp-idf/en/v5.5.2/esp32/api-guides/tools/idf-docker-image.html)
- [PyPI publish actionの実行環境](https://github.com/pypa/gh-action-pypi-publish)
