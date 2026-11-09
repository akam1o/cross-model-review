# cross-model-review

[English](README.md)

Claude Code 向けの、複数モデルによる自動コードレビューです。普段どおり開発し、
実装が終わったら `/cross-model-review` を実行します。

1. Git の未コミット変更（staged / unstaged / untracked）をスナップショットとして確定します。
2. **Codex** が読み取り専用で独立した欠陥レビューを行います。
3. **Codex** が Critical / High の欠陥を修正します（サンドボックス内、書き込み可）。
4. **Claude (Opus)** が設計と不要な複雑さをレビューします（読み取り専用）。
5. 必要なら Opus の指摘を **Codex** が修正し、修正結果を検証します。
6. 2〜5 を繰り返し（既定では修正ラウンド最大 3 回）、最後に最終状態をもう一度レビューします。
7. 品質ゲートを評価してレポートを返します。コミットや push は一切行いません。

3 つのコンポーネントは公式 [`a2a-sdk`](https://pypi.org/project/a2a-sdk/)
（1.3.0 に固定、プロトコル 1.0、JSON-RPC バインディング）を使い、
[Agent2Agent (A2A) プロトコル](https://a2a-protocol.org/) で通信します。

```
          Claude Code  ──/cross-model-review──▶  cross-model-review run
                                                    （A2A オーケストレーター）
                                         ┌──────────────┴──────────────┐
                                    A2A  │ JSON-RPC + SSE, 127.0.0.1   │  A2A
                                         ▼                             ▼
                              Codex A2A Agent                  Opus A2A Agent
                         review_changes / fix_findings      review_design /
                              / verify_changes             review_simplification
                                         │                             │
                              codex exec（サンドボックス）  claude -p（読み取り専用ツール）
```

## 動作要件

| ツール | 検証済みバージョン |
|---|---|
| Python | 3.12 以上 |
| [uv](https://docs.astral.sh/uv/) | 0.12 |
| Git | `git worktree` が使える 2.x |
| Codex CLI（`codex`） | 0.154.0（最小バージョンとして検査。設定で変更可） |
| Claude Code（`claude`） | 2.1.277（最小 2.1.0 を検査。設定で変更可） |

どちらの CLI もログイン済みである必要があります。cross-model-review は CLI の
認証情報や設定を変更しません。対応 OS は macOS と Linux です（POSIX の
プロセスグループと `flock` を使用しています）。

## インストール

```bash
git clone <このリポジトリ> cross-model-review
cd cross-model-review
uv tool install .                     # `cross-model-review` コマンドを PATH に追加
cross-model-review install-skill      # ~/.claude/skills/cross-model-review/SKILL.md を作成
cross-model-review doctor             # git / codex / claude を確認
```

特定のプロジェクトだけで使う場合は `install-skill --dest <repo>/.claude/skills` を指定します。

## 使い方

Claude Code で:

```
/cross-model-review
/cross-model-review --base main          # main との merge base 以降のブランチ全体をレビュー
/cross-model-review --max-iterations 2
/cross-model-review --review-only        # レビューのみ。コードは変更しない
```

ターミナルから:

```bash
cross-model-review run --repo . [--base REF] [--config FILE] [--mode worktree|in-place] [--json]
                       [--review-only] [--no-config-discovery]
```

### レビュー専用モード

`--review-only`（または `review_only = true`）を指定すると、同じスナップショットに
対して Codex の欠陥レビューと Opus の設計・簡素化レビューだけを行い、何も変更しません。

- `fix_findings` / `verify_changes` タスクは実行せず、パッチも適用しません
  （`in-place` モードでも同じ）。
- 設定されたコマンドはコードを変更し得るため、`workspace.setup_commands` と
  `tests.commands` は実行しません。そのため `required_tests` は `UNVERIFIED` になります。
- 読み取り専用タスクはスナップショットを変更できないため、1 ラウンドで終了します。
  `--max-iterations` は上限であり、繰り返し回数ではありません。
  `--max-iterations 2` でも 1 ラウンドです。
- Critical / High の指摘は `unresolved` のまま残り、ゲートは失敗します。指摘がなくても
  判定は `unverified`（終了コード 2）で、`passed` にはなりません。
- レポートには `"review_only": true` と `"status": "reviewed"` が入ります。

`--no-config-discovery` を指定すると `--config` のファイルだけを読み（このオプションでは
`--config` が必須）、ユーザー設定とリポジトリ設定は読みません。`.json` で終わる設定
ファイルは JSON として読みます（例: `Config(...).model_dump_json()` の出力）。
呼び出し側が信頼できる設定を渡し、リポジトリやホームディレクトリのファイルで CLI パスや
コマンドを変更させたくない場合に使います。

```bash
cross-model-review run --repo . --review-only --max-iterations 2 \
    --no-config-discovery --config trusted.json --json
```

終了コード: `0` 成功（またはレビュー対象なし）、`1` 失敗、`2` 未検証（unverified）、
`3` 使い方・設定のエラー、`130` キャンセル。Ctrl-C を押すとすべての A2A タスクが
キャンセルされ、その通知は CLI プロセスとその子プロセスまで届きます。

実行ごとに `<git-common-dir>/cross-model-review/runs/<run-id>/` に出力します。

| ファイル | 内容 |
|---|---|
| `report.md`, `report.json` | 人間向け / 機械向けレポート（[スキーマ](schemas/report.schema.json)） |
| `fixes.patch` | 元の変更に対するすべての修正（バイナリ対応の Git パッチ） |
| `logs/<agent>/server.stderr.jsonl` | エージェントサーバーの構造化ログ |
| `logs/<agent>/<task-id>/` | CLI の stdout と stderr（別ファイル）、出力スキーマ、最終メッセージ |
| `logs/tests-<n>/` | 反復ごとのテストコマンド出力 |

設定例とレポート例は [examples/](examples/) にあります。

## 設定

[config/cross-model-review.example.toml](config/cross-model-review.example.toml) を
`~/.config/cross-model-review/config.toml` または `<repo>/.cross-model-review.toml`
にコピーして編集します。モデル名、CLI パス、タイムアウト、テストコマンド、
反復回数、ワークスペースモードを設定できます。最小構成のプロジェクト設定の例:

```toml
[[tests.commands]]
name = "unit"
command = ["uv", "run", "pytest", "-q"]
```

> リポジトリ内の設定ファイルでは、手元で実行される CLI パスやコマンドを
> 指定できます。信頼できるリポジトリでのみ実行するか、
> `--no-config-discovery --config <信頼できるファイル>` を使ってください。

## 品質ゲート

| チェック | 合格条件 |
|---|---|
| `critical_findings` | 最終レビューで Critical の指摘が 0 件 |
| `high_findings` | 最終レビューで High の指摘が 0 件 |
| `required_tests` | 最終状態ですべての必須テストコマンドが終了コード 0 |
| `final_review` | 3 種類のレビューがすべてコードの最終状態に対して実行済み |
| `git_integrity` | HEAD・ブランチ・index が不変、読み取り専用タスクが何も書き込んでいない、実行中に作業ツリーが変更されていない、修正パッチが正確に適用された |
| `a2a_tasks` | すべての A2A タスクが有効な Artifact を伴って `completed` に到達 |

判定: いずれかのチェックが失敗なら `failed`。失敗はないが検証できなかった
チェックがあれば `unverified`（例: テストコマンド未設定）。それ以外は `passed`。
Medium / Low の指摘はレポートに載りますが、ゲートは止めません。

## 既存の変更を守る仕組み

- **stash ではなくスナップショット。** 作業ツリーを、index のコピー
  （`GIT_INDEX_FILE`）を使って `refs/cross-model-review/<run-id>` に記録します。
  この処理で実際の index、HEAD、ファイルが変更されることはありません。
- **分離された worktree（既定）。** エージェントは一時的な detached worktree で
  作業します。修正は `git apply`（`--index` は使わない）で戻すため、unstaged
  な変更として現れ、staged の内容には触れません。実行中に作業ツリーが変わった
  場合は何も適用せず、パッチだけを残します。
- **アクセス制御はプロンプトではなく CLI 側で強制します。**
  - Codex: レビュー/検証は `--sandbox read-only`、修正はネットワークなしの
    `--sandbox workspace-write`。加えて `approval_policy="never"`、`--ephemeral`、
    `--ignore-user-config`（`~/.codex/config.toml` の緩い `sandbox_mode`、
    MCP サーバー、プロファイルを適用させない）。
  - Claude Code: `--tools Read,Grep,Glob`、`--permission-mode dontAsk`、
    `--restricted`（ファイルアクセスをワークスペース内に限定）、
    `--strict-mcp-config`（MCP サーバーなし）、`--disable-slash-commands`、
    `--no-session-persistence`。
  - さらにオーケストレーターが各タスクの前後でワークスペースのフィンガー
    プリントを取ります。読み取り専用タスクが何かを変更した場合や、修正タスクが
    HEAD を動かした場合は実行を中止します。
- **書き込みは常に 1 つだけ。** `flock` によるワークスペースロックを使います。
  書き込みタスクは排他ロック、読み取りタスクは共有ロックを取り、同じ
  リポジトリでの 2 つ目の実行は拒否します。ロック中のリクエストは待たせずに
  拒否します。
- **コミット、push、デプロイは一切行いません。**

## A2A の詳細

- Agent Card: [agent-cards/](agent-cards/)（`cross-model-review agent-card codex|opus` で生成）
- スキル呼び出し 1 回が A2A の `Task` 1 つに対応します。リクエストの `Message`
  には `DataPart` が 1 つだけ入ります（[skill-request スキーマ](schemas/skill-request.schema.json)）。
  完了したタスクは `<skill>-result` という名前の `Artifact` をちょうど 1 つ持ち、
  その中身は `DataPart` 1 つです（[skill-result スキーマ](schemas/skill-result.schema.json)）。
  失敗時は `TASK_STATE_FAILED` / `TASK_STATE_REJECTED` とし、ステータスメッセージに
  エラーの `DataPart` を入れます。`CancelTask` は CLI のプロセスグループごと停止します。
- サーバーは `127.0.0.1` にのみ bind し、`Host` ヘッダを検査し、実行ごとに
  生成するランダムな Bearer トークンを要求します（Agent Card に HTTP bearer の
  セキュリティスキームとして宣言）。オーケストレーターはループバック以外の
  エージェント URL を拒否します。
- エージェントを単体で起動する例（A2A Inspector や TCK で確認する場合など）:

  ```bash
  CROSS_MODEL_REVIEW_A2A_TOKEN=secret cross-model-review serve-agent codex \
      --port 9999 --allowed-root /path/to/repo
  ```

  オーケストレーターから使う場合は `agents.codex.url = "http://127.0.0.1:9999/"`
  を設定し、同じトークンを `CROSS_MODEL_REVIEW_A2A_TOKEN` に指定します。

## 開発

```bash
uv sync
uv run pytest -q          # 単体テスト + E2E テスト（Mock エージェントとフェイク CLI。モデル呼び出しなし）
uv run ruff check src tests
uv run cross-model-review export-schemas schemas   # モデルを変更した後に実行
```

設計判断、トレードオフ、既知の制約は [docs/design.ja.md](docs/design.ja.md) を参照してください。

## ライセンス

[Apache License 2.0](LICENSE)
