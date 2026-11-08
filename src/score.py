#!/usr/bin/env python3
"""アンテナ②：直近24時間の記事を Claude API でふるい、日次ダイジェストを書く。

出力:
  digest/YYYY-MM-DD.md   （定刻タスクが読む。箱ごとに上位、全件は末尾の表）
  docs/YYYY-MM-DD.html   （Lizがスマホで眺める）
  docs/index.html        （最新と過去30日の一覧）
  data/scored/YYYY-MM-DD.jsonl （採点つきの全件）

環境変数: ANTHROPIC_API_KEY（必須）、ANTENNA_MODEL（省略時 claude-haiku-5-5）、ANTENNA_MAX_ITEMS（省略時 900）
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

JST = dt.timezone(dt.timedelta(hours=9))
API = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ANTENNA_MODEL", "claude-haiku-5-5")
MAX_ITEMS = int(os.environ.get("ANTENNA_MAX_ITEMS", "900"))
BATCH = 15
BOXES = ["main", "money", "world", "drop"]
BOX_JA = {"main": "主軸に関係（LLM推論・GPU・Python・再現性・トークナイザ）", "money": "金の匂い（誰かが払い始めた・困って探している）", "world": "意外・知らない話（主軸と無関係でよい）", "drop": "捨てる"}

SYSTEM = """あなたは「えりまき工房」のアンテナの採点係です。工房は、英語圏の技術者が探しているのに答えが弱い問い（LLM推論・量子化・VRAM・GPU・Python性能・推論の再現性・日本語トークナイザ）に、一番いい答え（測定ハーネス・比較表・実測・計算ツール）を出し続けて、検索とAI回答から人が来る資産を作ろうとしています。オーナーは LLM推論基盤のエンジニアで、RTX 5080 を持ち、人と話す仕事はしません。

渡された記事（見出し・URL・数字・出典）を1件ずつ採点し、JSON の配列だけを返してください。各要素:
{"id": "...", "box": "main|money|world|drop", "ja": "日本語で1行の要約（40字以内、見出しの翻訳ではなく何が起きたか）", "novelty": 1-3, "kobo": "工房とのつながりを1行（なければ空文字）", "why": "boxの理由を10字程度"}

採点の基準:
- main: 工房の題材に直接関係し、問い・ツール・実測の種になる。すでに飽和している話題（VRAM計算ツール、tok/s計算、損益分岐、Q4/Q5比較、GPU別おすすめ、エンジン比較の総論）は novelty 1。
- money: 誰かが新しく金を払い始めた・値段が付いた・困って探している話。分野は問わない。
- world: 主軸と無関係でも、世の中の動きとして知る価値がある（急上昇・大きな出来事・新しい仕組み）。
- drop: 宣伝だけ、個人の雑談、情報量がない、既知の定番。
- novelty: 3=2026年の新しい出来事・数字で、一般的なAIの知識にない。2=新しいが予想の範囲。1=既知・定番。
- 推測で中身を膨らませない。見出しと数字から読めることだけ。URLの中身は読めない前提。"""


def now_jst():
    return dt.datetime.now(tz=JST)


def load_recent(data_dir: Path, hours: int = 26) -> list[dict]:
    cutoff = now_jst() - dt.timedelta(hours=hours)
    items = {}
    for p in sorted(data_dir.glob("*.jsonl"))[-3:]:
        with p.open(encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                try:
                    seen = dt.datetime.fromisoformat(r["seen"])
                except Exception:
                    continue
                if seen >= cutoff:
                    items[r["id"]] = r
    return list(items.values())


def prefilter(items: list[dict]) -> list[dict]:
    """情報源ごとに数字の大きい順で上限を付け、全体を MAX_ITEMS に収める。"""
    by_src = defaultdict(list)
    for r in items:
        by_src[r["source"]].append(r)
    kept = []
    for src, rs in by_src.items():
        rs.sort(key=lambda r: ((r.get("score") or 0) + 2 * (r.get("comments") or 0)), reverse=True)
        kept.extend(rs[:60])
    if len(kept) > MAX_ITEMS:
        kept.sort(key=lambda r: ((r.get("score") or 0) + 2 * (r.get("comments") or 0)), reverse=True)
        kept = kept[:MAX_ITEMS]
    return kept


def call_claude(batch: list[dict]) -> list[dict]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY がない")
    lines = []
    for r in batch:
        lines.append(json.dumps({"id": r["id"], "source": r["source_name"], "title": r["title"], "url": r["url"],
                                 "score": r.get("score"), "comments": r.get("comments"), "summary": (r.get("summary") or "")[:160]}, ensure_ascii=False))
    body = {
        "model": MODEL, "max_tokens": 8000, "system": SYSTEM,
        "messages": [{"role": "user", "content": "記事:\n" + "\n".join(lines) + "\n\nJSON配列だけを返す。"}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for attempt in range(4):
        resp = requests.post(API, headers=headers, json=body, timeout=120)
        if resp.status_code in (429, 500, 502, 503, 529):
            time.sleep(5 * (attempt + 1))
            continue
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))
        return parse_array(text, truncated=(data.get("stop_reason") == "max_tokens"))
    raise RuntimeError("Claude API が応答しない")


def parse_array(text: str, truncated: bool = False) -> list[dict]:
    """JSON配列を読む。コードフェンスや途中切れがあっても、完全なオブジェクトだけ拾う。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
    t = t.replace("```", "")
    s, e = t.find("["), t.rfind("]")
    if s >= 0 and e > s and not truncated:
        try:
            return json.loads(t[s:e + 1])
        except json.JSONDecodeError:
            pass
    # 途中切れ：{...} を1つずつ拾う
    out = []
    depth, start = 0, None
    for i, ch in enumerate(t):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    out.append(json.loads(t[start:i + 1]))
                except json.JSONDecodeError:
                    pass
                start = None
    if not out:
        raise ValueError("JSON配列が見つからない: " + text[:200])
    if truncated:
        print(f"warn: 出力が途中で切れた。{len(out)}件だけ拾った", file=sys.stderr)
    return out


def score_all(items: list[dict]) -> list[dict]:
    by_id = {r["id"]: r for r in items}
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        try:
            res = call_claude(batch)
        except Exception as e:
            print(f"batch {i//BATCH} failed: {e}", file=sys.stderr)
            for r in batch:  # 採点できなかった物は「捨てる」側に置き、箱を汚さない
                r.setdefault("box", "drop"); r.setdefault("ja", r["title"][:40]); r.setdefault("novelty", 1); r.setdefault("kobo", ""); r["why"] = "採点失敗"
            continue
        for s in res:
            r = by_id.get(s.get("id"))
            if not r:
                continue
            r["box"] = s.get("box") if s.get("box") in BOXES else "world"
            r["ja"] = str(s.get("ja", ""))[:60]
            r["novelty"] = int(s.get("novelty", 1)) if str(s.get("novelty", "1")).isdigit() else 1
            r["kobo"] = str(s.get("kobo", ""))[:120]
            r["why"] = str(s.get("why", ""))[:40]
        for r in batch:
            r.setdefault("box", "drop"); r.setdefault("ja", r["title"][:40]); r.setdefault("novelty", 1); r.setdefault("kobo", ""); r.setdefault("why", "")
        time.sleep(0.5)
    return items


def rank(r: dict) -> float:
    return r.get("novelty", 1) * 100 + (r.get("score") or 0) + 2 * (r.get("comments") or 0)


def write_digest_md(day: str, items: list[dict], path: Path, status: dict | None):
    L = [f"# アンテナ ダイジェスト {day}", "",
         f"集めた {len(items)} 件を採点。箱ごとに上位。全件は末尾。採点モデル {MODEL}。", ""]
    if status:
        failed = [k for k, v in status.get("sources", {}).items() if not v.get("ok")]
        L.append(f"直近の収集: {status.get('run_at','')} 新着 {status.get('new_total','?')} 件。取れなかった情報源: {', '.join(failed) if failed else 'なし'}")
        L.append("")
    for box in ["main", "money", "world"]:
        rs = sorted([r for r in items if r.get("box") == box], key=rank, reverse=True)
        L.append(f"## {BOX_JA[box]}（{len(rs)}件）")
        L.append("")
        for r in rs[:15]:
            num = " ".join(x for x in [f"{r['score']}pt" if r.get("score") is not None else "", f"{r['comments']}c" if r.get("comments") is not None else ""] if x)
            kobo = f" → 工房: {r['kobo']}" if r.get("kobo") else ""
            L.append(f"- [{r['novelty']}] {r['ja']} — [{r['title'][:80]}]({r['url']}) ({r['source_name']}{', ' + num if num else ''}){kobo}")
        L.append("")
    L.append("## 全件（捨てた物も含む）")
    L.append("")
    L.append("| 箱 | 新 | 要約 | 見出し | 出典 | 数字 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for r in sorted(items, key=lambda r: (BOXES.index(r.get("box", "drop")), -rank(r))):
        num = f"{r.get('score') if r.get('score') is not None else ''}/{r.get('comments') if r.get('comments') is not None else ''}"
        L.append(f"| {r.get('box')} | {r.get('novelty')} | {r.get('ja','').replace('|','／')} | [{r['title'][:60].replace('|','／')}]({r['url']}) | {r['source_name']} | {num} |")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


CSS = """body{font-family:system-ui,-apple-system,'Hiragino Sans','Noto Sans JP',sans-serif;max-width:760px;margin:0 auto;padding:16px;line-height:1.55;color:#222;background:#fafaf7}
h1{font-size:1.3rem;margin:.2em 0}h2{font-size:1.05rem;margin:1.4em 0 .4em;padding-bottom:.2em;border-bottom:1px solid #ddd}
.item{padding:.5em 0;border-bottom:1px dashed #e4e4e0}.ja{font-weight:600}.meta{font-size:.82rem;color:#666}.kobo{font-size:.85rem;color:#2a5d9f}
.n3{border-left:4px solid #e0892b;padding-left:.6em}.n2{border-left:4px solid #c9c9c0;padding-left:.6em}.n1{border-left:4px solid #eee;padding-left:.6em}
a{color:#1a4a8a;text-decoration:none}a:hover{text-decoration:underline}.small{font-size:.8rem;color:#777}details summary{cursor:pointer;color:#555}
nav a{margin-right:1em}"""


def esc(s):
    return html.escape(str(s or ""))


def write_digest_html(day: str, items: list[dict], path: Path):
    H = [f"<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'><title>アンテナ {day}</title><style>{CSS}</style></head><body>",
         f"<nav><a href='./'>一覧</a></nav><h1>アンテナ {day}</h1><p class='small'>{len(items)}件を採点。左の色：橙＝新しい、灰＝予想の範囲。見出しをチャットに貼れば掘ります。</p>"]
    for box in ["main", "money", "world"]:
        rs = sorted([r for r in items if r.get("box") == box], key=rank, reverse=True)
        H.append(f"<h2>{esc(BOX_JA[box])}（{len(rs)}）</h2>")
        for r in rs[:20]:
            num = " ".join(x for x in [f"{r['score']}pt" if r.get("score") is not None else "", f"{r['comments']}c" if r.get("comments") is not None else ""] if x)
            H.append(f"<div class='item n{r.get('novelty',1)}'><div class='ja'>{esc(r.get('ja'))}</div>"
                     f"<div><a href='{esc(r['url'])}' target='_blank' rel='noopener'>{esc(r['title'][:100])}</a></div>"
                     f"<div class='meta'>{esc(r['source_name'])} {esc(num)}</div>"
                     + (f"<div class='kobo'>→ {esc(r['kobo'])}</div>" if r.get("kobo") else "") + "</div>")
        if len(rs) > 20:
            H.append("<details><summary>残り %d 件</summary>" % (len(rs) - 20))
            for r in rs[20:]:
                H.append(f"<div class='item n{r.get('novelty',1)}'><span class='ja'>{esc(r.get('ja'))}</span> <a href='{esc(r['url'])}' target='_blank' rel='noopener'>{esc(r['title'][:80])}</a> <span class='meta'>{esc(r['source_name'])}</span></div>")
            H.append("</details>")
    drops = [r for r in items if r.get("box") == "drop"]
    H.append(f"<h2>捨てた（{len(drops)}）</h2><details><summary>開く</summary>")
    for r in drops:
        H.append(f"<div class='item n1'><a href='{esc(r['url'])}' target='_blank' rel='noopener'>{esc(r['title'][:80])}</a> <span class='meta'>{esc(r['source_name'])} {esc(r.get('why'))}</span></div>")
    H.append("</details></body></html>")
    path.write_text("".join(H), encoding="utf-8")


def write_index(docs: Path):
    days = sorted([p.stem for p in docs.glob("20??-??-??.html")], reverse=True)
    H = [f"<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'><title>えりまき工房 アンテナ</title><style>{CSS}</style></head><body>",
         "<h1>えりまき工房 アンテナ</h1><p class='small'>毎朝1ページ。世の中・金の流れ・主軸の3つの箱。</p><ul>"]
    for d in days[:60]:
        H.append(f"<li><a href='{d}.html'>{d}</a></li>")
    H.append("</ul></body></html>")
    (docs / "index.html").write_text("".join(H), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--digest", default="digest")
    ap.add_argument("--docs", default="docs")
    ap.add_argument("--day", default=now_jst().strftime("%Y-%m-%d"))
    ap.add_argument("--dry", action="store_true", help="APIを呼ばず、箱は box_hint で埋める（検査用）")
    a = ap.parse_args()
    data, digest, docs = Path(a.data), Path(a.digest), Path(a.docs)
    for p in (digest, docs, data / "scored"):
        p.mkdir(parents=True, exist_ok=True)

    items = prefilter(load_recent(data))
    print(f"items={len(items)}")
    if a.dry:
        for r in items:
            r.update({"box": r.get("box_hint", "world") if r.get("box_hint") != "jp" else "world", "ja": r["title"][:40], "novelty": 2, "kobo": "", "why": "dry"})
    else:
        items = score_all(items)

    with (data / "scored" / f"{a.day}.jsonl").open("w", encoding="utf-8") as f:
        for r in items:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    status = None
    sp = data / "status.json"
    if sp.exists():
        status = json.loads(sp.read_text(encoding="utf-8"))
    write_digest_md(a.day, items, digest / f"{a.day}.md", status)
    write_digest_html(a.day, items, docs / f"{a.day}.html")
    write_index(docs)
    counts = {b: sum(1 for r in items if r.get("box") == b) for b in BOXES}
    print("boxes=", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
