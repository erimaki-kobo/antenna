# antenna

えりまき工房のアンテナ。世の中・金の流れ・主軸（LLM推論まわり）の3つの箱で、毎朝1ページのダイジェストを出す。

- 見る場所: https://erimaki-kobo.github.io/antenna/
- ① `src/collect.py` — 3時間ごと（`.github/workflows/collect.yml`）。`src/sources.yaml` の情報源を取り、`data/YYYY-MM-DD.jsonl` に見出し・URL・日付・数字だけを貯める。本文は保存しない
- ② `src/score.py` — 毎朝 6:40 JST（`.github/workflows/digest.yml`）。直近24時間を Claude API で採点し、`digest/YYYY-MM-DD.md`（機械が読む）と `docs/YYYY-MM-DD.html`（人が読む）を書く
- ③ 考えるのは Claude の定刻タスク（このリポジトリの外）。`digest/` を読んで、問いの一覧・気になる一覧・日誌に行き先を決める

設定（Settings）: Secrets に `ANTHROPIC_API_KEY`。Variables に `ANTENNA_MODEL`（省略時 claude-haiku-5-5）。Pages の Source は「GitHub Actions」。

情報源の追加・削除は `src/sources.yaml` だけ。取れなかった情報源は `data/status.json` と `data/status_history.jsonl` に残る。

守ること: robots.txt と利用規約に従う。API があるものは API。1情報源 1回/3時間。本文は保存しない。要約は自分の言葉で短く。

## API の使用量と予算
- 採点のたびに `data/usage.jsonl` に1行（日付・モデル・件数・入出力トークン・推定ドル）を足す。ダイジェストの冒頭にも今月の使用量が出る
- 月の予算（このリポジトリのキーの分だけ）を残りの日数で割り、その日に採点してよい件数を決める。超えそうなら、数字の大きい記事から順に残して減らす
- 設定は Settings → Secrets and variables → Actions → **Variables**：
  - `ANTENNA_MONTHLY_BUDGET_USD`（省略時 10）
  - `ANTENNA_PRICE_IN_PER_MTOK` / `ANTENNA_PRICE_OUT_PER_MTOK`（省略時 0.10 / 0.50。claude-haiku-5-5、プロンプト10万トークン以下の公表価格）
  - `ANTENNA_MODEL`（省略時 claude-haiku-5-5）
- 他のプロジェクトのキーの使用量はここでは数えない。全体は Console の Usage（キーごとに分けて見られる）で見る
