# antenna

えりまき工房のアンテナ。世の中・金の流れ・主軸（LLM推論まわり）の3つの箱で、毎朝1ページのダイジェストを出す。

- 見る場所: https://erimaki-kobo.github.io/antenna/
- ① `src/collect.py` — 3時間ごと（`.github/workflows/collect.yml`）。`src/sources.yaml` の情報源を取り、`data/YYYY-MM-DD.jsonl` に見出し・URL・日付・数字だけを貯める。本文は保存しない
- ② `src/score.py` — 毎朝 6:40 JST（`.github/workflows/digest.yml`）。直近24時間を Claude API で採点し、`digest/YYYY-MM-DD.md`（機械が読む）と `docs/YYYY-MM-DD.html`（人が読む）を書く
- ③ 考えるのは Claude の定刻タスク（このリポジトリの外）。`digest/` を読んで、問いの一覧・気になる一覧・日誌に行き先を決める

設定（Settings）: Secrets に `ANTHROPIC_API_KEY`。Variables に `ANTENNA_MODEL`（省略時 claude-haiku-5-5）。Pages の Source は「GitHub Actions」。

情報源の追加・削除は `src/sources.yaml` だけ。取れなかった情報源は `data/status.json` と `data/status_history.jsonl` に残る。

守ること: robots.txt と利用規約に従う。API があるものは API。1情報源 1回/3時間。本文は保存しない。要約は自分の言葉で短く。
