# 設計ノート

[English](design.md)

## コンポーネント

| コンポーネント | モジュール | 役割 |
|---|---|---|
| A2A Orchestrator | `orchestrator/workflow.py`, `client.py`, `launcher.py`, `gate.py` | Git 状態のスナップショット、エージェントの起動・接続、ループの制御、テスト実行、Git 整合性の検査、修正の適用、品質ゲートの評価 |
| Codex A2A Agent | `agents/backends.py`（`CodexBackend`） | `codex exec` による `review_changes`、`fix_findings`、`verify_changes` |
| Opus A2A Agent | `agents/backends.py`（`ClaudeBackend`） | `claude -p` による `review_design`、`review_simplification` |
| 共通 A2A サーバー | `agents/executor.py`, `server.py`, `cards.py` | `AgentExecutor`、Agent Card、Bearer 認証付きのループバック HTTP サーバー |

プロトコル処理はすべて `a2a-sdk` 1.3.0 を使っています（`AgentExecutor`、
`TaskUpdater`、`DefaultRequestHandler`、`InMemoryTaskStore`、
`create_agent_card_routes`、`create_jsonrpc_routes`、`A2ACardResolver`、
`ClientFactory`）。独自のエージェント間プロトコルはありません。
プロジェクト固有の取り決めは `DataPart` に入れる JSON の形だけで、
これは Agent Card の拡張（`urn:cross-model-review:ext:skill-io:v1`）として宣言し、
JSON Schema として公開しています。

## ループ

```
for i in 1..max_iterations:
    codex.review_changes            -> Critical/High（と失敗テスト）を修正 -> codex.verify_changes
    opus.review_design + opus.review_simplification（並列、読み取り専用）
                                     -> codex.fix_findings -> codex.verify_changes
    テストコマンドを実行
    何も変わらずテストも失敗していなければ終了。修正で何も変わらなかった場合も終了
直近の 3 つのレビューがすべて最終ツリーを見ていなければ:
    レビューのみのラウンドをもう 1 回実行
```

品質ゲートは「最終ツリーに対する直近の完全なレビュー」の指摘だけを使います。
指摘にはレビューごとに新しい ID が振られ、反復をまたいだ同一性の照合はしません。
それ以前の指摘は、修正・検証の結果とともにレポートに残ります。

## 設計判断とトレードオフ

**既定は worktree、in-place は任意。** エージェントは、スナップショット
コミットから作った一時的な detached worktree に書き込みます。実行中は
ユーザーの作業ツリーが完全に守られ、結果はレビュー可能なパッチになります。
代わりに ignore 対象のファイル（仮想環境、`node_modules`、ビルド成果物）が
存在しないため、テストには `workspace.setup_commands` か `--mode in-place` が
必要な場合があります。in-place モードは分離ではなく検出（フィンガープリント、
HEAD/index の検査）に頼ります。

**品質ゲートが失敗しても修正は適用する。** 他の問題が残っていても、
Critical/High の修正そのものは改善です。ただし適用するのは作業ツリーが
スナップショットとバイト単位で一致する場合だけで、中止・キャンセル・Git の
問題があったときは適用しません。適用は常に unstaged な変更として行います。
パッチとスナップショット ref があるので元に戻せます（`git apply -R fixes.patch`）。
パッチだけ欲しい場合は `workspace.apply_fixes = false` にします。

**一時 index によるスナップショット。** `git stash` はユーザーの index と
作業ツリーに触れてしまいます。index をコピーし、そのコピーに対して
`git add -A` / `write-tree` を実行すれば、追跡・未追跡・staged の状態を
副作用なしに記録できます。`GIT_OPTIONAL_LOCKS=0` で読み取りコマンドによる
index の書き換えを防ぎ、すべての Git 呼び出しで hooks を無効化しています。

**エージェントは別プロセス。** 各エージェントサーバーは独自のプロセス
グループで動く子プロセスで、`127.0.0.1:0` に bind し、URL を stdout に出力します。
stdin が閉じると終了するため、オーケストレーターが異常終了した場合も残りません。
リモートエージェントの配置形態と同じ構造であり、CLI のクラッシュが
オーケストレーターに波及しません。

**待たせずに拒否する。** ワークスペースロックが取れない場合は
`TASK_STATE_REJECTED`（`workspace_busy`）を返します。30 分続くこともある
CLI セッションの後ろに並ばせると、バグが隠れ、タイムアウトも伸びるためです。

**テストはオーケストレーターが実行する。** 必須テストはゲートの判定に使うため、
モデルの自己申告ではなくオーケストレーター自身が実行します。必須テストが
失敗すると High の指摘になり、次のラウンドで Codex に修正を依頼します。

**タスクが 1 つでも失敗したら中止する。** リトライはしません。失敗・
タイムアウト・拒否・不正な結果のタスクがあると最終状態を完全にレビュー
できないため、実行を止めてゲートを失敗にします。再実行はユーザーに委ねます。

**ストリーミング + `GetTask`。** オーケストレーターは SSE ストリームで
タスク ID を早めに受け取り（`CancelTask` に必要）、その後に最終的な `Task` を
取得し、その正本の Artifact を検証します。

**Pydantic から導出した strict な出力スキーマ。** `codex --output-schema` は
strict な structured output の方言を要求するため、CLI 用スキーマでは長さや
パターンのキーワードを除き、すべてのプロパティを必須にします。完全な制約
（長さ、列挙値、ID の一意性、リポジトリ相対パス）は、エージェント側と
オーケストレーター側の両方で Pydantic が再検証します。

**既定で CLI の設定を分離する。** `codex --ignore-user-config` と
`claude --restricted --strict-mcp-config` により、ユーザーの MCP サーバー、
プロファイル、緩いサンドボックス既定値、プロジェクト設定がレビューセッションに
入り込まないようにしています。認証がそれらに依存する場合は設定で緩められます。

## キャンセルの経路

1. Ctrl-C / SIGTERM / SIGHUP でオーケストレーターのメインタスクをキャンセルします。
2. 実行中の各 `AgentClient.call` が A2A の `CancelTask` を送ります（最初の
   イベントがまだ届いていなければ、タスク ID の到着を短時間待ちます）。
3. SDK が `AgentExecutor.cancel` を呼び、バックエンドのジョブをキャンセルして終了を待ちます。
4. `run_process` が CLI のプロセスグループに SIGTERM を送り、猶予後に SIGKILL を
   送ってから、キャンセルの伝播を続けます。
5. エグゼキューターが `TASK_STATE_CANCELED` を報告し、ワークスペースロックを解放します。
6. オーケストレーターがエージェントサーバーを停止し、worktree を削除し、
   `cancelled` のレポートを書き出します。

タイムアウトも同じ経路をたどります。CLI のタイムアウトはエージェント自身が
強制し（`cli_timeout`）、オーケストレーターはさらに `task_timeout_margin_s`
待ってから自分でタスクをキャンセルします。

## リモートエージェント（将来）

準備はしてありますが、有効にはしていません。

- エージェントは URL（`agents.*.url`）で指定し、Agent Card で発見します。
  必須スキルとストリーミング対応を確認します。
- 認証方式は Agent Card に宣言します（HTTP bearer）。
- `WorkspaceRef.kind` は判別子で、現在は `local_path` だけです。リモートの
  エージェントには、転送可能なワークスペース（例: `FilePart` に入れた Git bundle）と、
  パッチを Artifact として返す仕組みが必要です。
- `a2a.allow_remote` は存在しますが `false` に固定しています。ループバック以外の
  URL は、設定でも Agent Card のインターフェースでも拒否します。

## 既知の制約

- **信頼できないリポジトリ。** `<repo>/.cross-model-review.toml` で CLI パスや
  コマンドを指定できます。テストコマンドはサンドボックスなしで実行されます。
  `--no-config-discovery --config <file>` は指定したファイルだけを読み、
  `--review-only` は setup コマンドもテストコマンドも実行しません。
- **プロセスの逃走。** 自分で新しいセッションを作るプロセス（`setsid`、
  デーモン）にはプロセスグループ単位の kill が届きません。
- **サンドボックスの可用性。** Codex のサンドボックスは OS に依存します
  （macOS は Seatbelt、Linux は Landlock/bubblewrap）。利用できない場合は Codex が
  失敗して実行は中止されます。サンドボックスなしのモードに切り替えることはありません。
- **設定の分離と認証。** `--ignore-user-config` は Codex の独自モデルプロバイダ設定を、
  `--restricted` は Claude Code の設定ファイル（例: `apiKeyHelper`）を無視します。
  `ignore_user_config = false` / `restricted = false` にすれば戻りますが、その場合は
  ユーザーの MCP サーバー、hooks、設定も適用されます（Codex 0.154 では
  `-c mcp_servers={}` で設定済みのサーバーを無効化できませんでした）。
- **モデルの判断。** ゲートの信頼性はレビューの質に依存します。見逃した欠陥は
  検出できず、誤った指摘もあり得ます。
- **大きな変更。** 各プロンプトに埋め込む diff は `max_diff_bytes` で切り詰めます
  （エージェントはファイルを直接読めます）。大きな未追跡ファイルもスナップショットに
  含まれます。
- **リポジトリの状態。** コミットのないリポジトリ、未マージのパス、進行中の
  merge/rebase は拒否します。サブモジュール内部の変更はレビューしません
  （gitlink の更新のみ）。
- **テストの副作用。** worktree モードでは、テストや setup コマンドが作った
  未追跡ファイル（例: `.gitignore` のないリポジトリでの `__pycache__/`）を削除し、
  警告として報告します。既存ファイルの変更・削除は中止します。in-place モードでは
  ファイルを 1 つ作っただけでも中止します（削除するとユーザーの作業を消す恐れが
  あるため）。生成ファイルは `.gitignore` に追加してください。
- **修正に含まれる生成ファイル。** 修正タスクがワークスペースに残したものは
  すべて修正パッチに入ります。通常は Codex のサンドボックスと `.gitignore` により
  キャッシュは入りませんが、パッチを確認してください。
- **スナップショット ref の蓄積。** `git for-each-ref refs/cross-model-review` で一覧し、
  `git update-ref -d <ref>` で削除します。`workspace.keep_snapshot_ref = false` も使えます。
- **プラットフォーム。** macOS と Linux のみです。

## プロトコル互換性の確認

A2A TCK（`a2aproject/a2a-tck`、JSON-RPC、`--level must`）を
`serve-agent codex --insecure-no-auth` に対して実行しました。結果は
**62 passed、5 failed、168 skipped** です。失敗した 5 件（`DM-ART-001` ×4、
`DM-MSG-001`）は、TCK 独自のシナリオ用プロンプトに対して text/file/data の
Artifact や直接の `Message` を返すことを期待するテストです。本エージェントは
skill-request の `DataPart` だけを受け付け、それ以外は設計上拒否するため、
これらのシナリオテストは通りません。プロトコルレベルの失敗は報告されていません。
A2A Inspector（対話型の Web UI）は実行していません。
