# 実行例

[English](README.md)

| ファイル | 内容 |
|---|---|
| [demo.sh](demo.sh) | バグを仕込んだ使い捨てリポジトリを作り、実際にレビューを実行します（Codex と Claude Code を呼び出します） |
| [demo-config.toml](demo-config.toml) | デモ用の設定。必須テスト 1 件、反復 2 回、短めのタイムアウト、Claude の呼び出しごとの予算上限 |
| [sample-report-passed.md](sample-report-passed.md) | 実際のデモ実行のレポート |
| [sample-report-invalid-output.md](sample-report-invalid-output.md) | エージェントの出力が検証に失敗して中止した実行のレポート |

## デモの流れ

未コミットの変更では、スタブを
`[items[i:i + size] for i in range(0, len(items) - size, size)]` に置き換えており、
最後のチャンクが落ちます。[成功した実行](sample-report-passed.md)では次のように進みました。

1. 反復 1: Codex の `review_changes` が High の指摘（「Range stop excludes the
   final chunk」）を報告し、`fix_findings` が range の終端を `len(items)` に修正し、
   `verify_changes` が解決を確認しました。続いて Opus が設計と簡素化の観点で
   レビューしました（Medium/Low/Info の指摘のみ）。
2. 反復 2: 修正後のコードに対して 3 つのレビューがすべて実行され、Critical/High の
   指摘がなかったためループが収束しました。
3. 必須テストが成功し、ゲートは `PASSED` になり、修正は unstaged な変更として
   作業ツリーに適用されました。コミットはしていません。

テストコマンドが `__pycache__/` を作りました（デモリポジトリには `.gitignore` が
ありません）。これは worktree から削除され、警告として報告されています。

[中止した実行](sample-report-invalid-output.md)は、CLI 出力スキーマから `title`
フィールドが抜けていた以前のビルドで記録したものです。Codex はそのスキーマに従って
出力し、エージェントがその出力を不正として拒否し、オーケストレーターはリポジトリを
変更せずにゲート失敗で停止しました。不正なエージェント出力の扱いの例として残しています。

デモを自分で実行する場合（チェックアウトしたリポジトリで `uv tool install .` の後）:

```bash
sh examples/demo.sh
```
