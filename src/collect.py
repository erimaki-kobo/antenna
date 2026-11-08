#!/usr/bin/env python3
"""アンテナ①：情報源を機械的に取り、重複を除いて data/YYYY-MM-DD.jsonl に貯める。判断はしない。

使い方: python3 src/collect.py [--sources src/sources.yaml] [--data data] [--only id1,id2]
1行＝1記事。保存するのは見出し・URL・日付・数字だけ（本文は保存しない）。
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

UA = "erimaki-kobo-antenna/0.1 (+https://erimaki-kobo.github.io/antenna/)"
TIMEOUT = 20
JST = dt.timezone(dt.timedelta(hours=9))


def now_jst() -> dt.datetime:
    return dt.datetime.now(tz=JST)


def norm_url(u: str) -> str:
    u = (u or "").strip()
    u = re.sub(r"[?&](utm_[a-z]+|ref|source)=[^&#]*", "", u)
    u = u.rstrip("?&")
    return u


def item_id(url: str, title: str) -> str:
    key = norm_url(url) or title.strip().lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def clean(s: str | None, n: int = 300) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def to_iso(ts) -> str | None:
    """epoch秒・struct_time・ISO文字列を ISO8601(UTC) に。分からなければ None。"""
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()
        if isinstance(ts, time.struct_time):  # feedparser の *_parsed は UTC
            return dt.datetime.fromtimestamp(calendar.timegm(ts), tz=dt.timezone.utc).isoformat()
        if isinstance(ts, str):
            s = ts.strip().replace("Z", "+00:00")
            return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc).isoformat()
    except Exception:
        return None
    return None


def mk(source: dict, title: str, url: str, published=None, score=None, comments=None, extra: dict | None = None) -> dict | None:
    title = clean(title, 200)
    url = norm_url(url)
    if not title or not url:
        return None
    rec = {
        "id": item_id(url, title),
        "source": source["id"],
        "source_name": source["name"],
        "box_hint": source.get("box_hint", "world"),
        "title": title,
        "url": url,
        "published": to_iso(published),
        "score": score,
        "comments": comments,
        "seen": now_jst().isoformat(timespec="seconds"),
    }
    if extra:
        rec.update({k: v for k, v in extra.items() if v not in (None, "", [])})
    return rec


# ---------- parsers ----------

def p_rss(src, resp):
    if feedparser is None:
        raise RuntimeError("feedparser がない")
    feed = feedparser.parse(resp.content)
    out = []
    for e in feed.entries[:80]:
        pub = e.get("published_parsed") or e.get("updated_parsed")
        out.append(mk(src, e.get("title"), e.get("link"), pub, extra={"summary": clean(e.get("summary"), 240)}))
    return out


def p_hn_algolia(src, resp):
    out = []
    for h in resp.json().get("hits", []):
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append(mk(src, h.get("title"), url, h.get("created_at"), h.get("points"), h.get("num_comments"),
                      extra={"hn": f"https://news.ycombinator.com/item?id={h.get('objectID')}"}))
    return out


def p_lobsters(src, resp):
    out = []
    for s in resp.json():
        out.append(mk(src, s.get("title"), s.get("url") or s.get("short_id_url"), s.get("created_at"), s.get("score"), s.get("comment_count"),
                      extra={"tags": s.get("tags")}))
    return out


def p_gh_trending(src, resp):
    out = []
    txt = resp.text
    for m in re.finditer(r'<h2 class="h3 lh-condensed">\s*<a href="/([^"]+)"', txt):
        repo = m.group(1).strip()
        # 説明と★
        seg = txt[m.end(): m.end() + 3000]
        desc = re.search(r'<p class="col-9[^>]*>(.*?)</p>', seg, re.S)
        stars_today = re.search(r"([\d,]+) stars today", seg)
        out.append(mk(src, repo, f"https://github.com/{repo}", None,
                      int(stars_today.group(1).replace(",", "")) if stars_today else None,
                      extra={"summary": clean(desc.group(1), 200) if desc else ""}))
    return out


def p_hf_models(src, resp):
    out = []
    for m in resp.json():
        mid = m.get("modelId") or m.get("id")
        out.append(mk(src, mid, f"https://huggingface.co/{mid}", m.get("createdAt"), m.get("likes"), m.get("downloads"),
                      extra={"pipeline": m.get("pipeline_tag")}))
    return out


def p_devto(src, resp):
    out = []
    for a in resp.json():
        out.append(mk(src, a.get("title"), a.get("url"), a.get("published_at"), a.get("public_reactions_count"), a.get("comments_count"),
                      extra={"tags": a.get("tag_list")}))
    return out


def p_discourse(src, resp):
    out = []
    base = src["url"].split("/latest.json")[0]
    for t in resp.json().get("topic_list", {}).get("topics", []):
        out.append(mk(src, t.get("title"), f"{base}/t/{t.get('slug')}/{t.get('id')}", t.get("created_at"), t.get("like_count"), t.get("posts_count")))
    return out


def p_stackexchange(src, resp):
    out = []
    for q in resp.json().get("items", []):
        out.append(mk(src, q.get("title"), q.get("link"), q.get("creation_date"), q.get("score"), q.get("answer_count"),
                      extra={"tags": q.get("tags"), "answered": q.get("is_answered")}))
    return out


def p_reddit(src, resp):
    out = []
    for c in resp.json().get("data", {}).get("children", []):
        d = c.get("data", {})
        out.append(mk(src, d.get("title"), "https://www.reddit.com" + d.get("permalink", ""), d.get("created_utc"), d.get("score"), d.get("num_comments"),
                      extra={"flair": d.get("link_flair_text"), "ext": d.get("url") if not d.get("is_self") else None}))
    return out


def p_gh_issues(src, resp):
    out = []
    for i in resp.json():
        if "pull_request" in i:
            continue
        out.append(mk(src, i.get("title"), i.get("html_url"), i.get("created_at"), None, i.get("comments"),
                      extra={"labels": [l.get("name") for l in i.get("labels", [])]}))
    return out


def p_qiita(src, resp):
    out = []
    for a in resp.json():
        out.append(mk(src, a.get("title"), a.get("url"), a.get("created_at"), a.get("likes_count"), a.get("comments_count"),
                      extra={"tags": [t.get("name") for t in a.get("tags", [])]}))
    return out


def p_kaggle(src, resp):
    out = []
    for c in resp.json():
        out.append(mk(src, c.get("title"), c.get("url") or f"https://www.kaggle.com/c/{c.get('ref')}", c.get("enabledDate"), None, None,
                      extra={"reward": c.get("reward"), "deadline": c.get("deadline")}))
    return out


def p_wiki_top(src, resp):
    out = []
    items = resp.json().get("items", [])
    arts = items[0].get("articles", []) if items else []
    lang = "ja" if "ja.wikipedia" in src["url"] else "en"
    for a in arts[:60]:
        name = a.get("article", "")
        if name.startswith("特別:") or name.startswith("Special:") or name in ("メインページ", "Main_Page"):
            continue
        out.append(mk(src, name.replace("_", " "), f"https://{lang}.wikipedia.org/wiki/{name}", None, a.get("views"), None, extra={"rank": a.get("rank")}))
    return out


PARSERS = {
    "rss": p_rss, "hn_algolia": p_hn_algolia, "lobsters": p_lobsters, "gh_trending": p_gh_trending,
    "hf_models": p_hf_models, "devto": p_devto, "discourse": p_discourse, "stackexchange": p_stackexchange,
    "reddit": p_reddit, "gh_issues": p_gh_issues, "qiita": p_qiita, "kaggle": p_kaggle, "wiki_top": p_wiki_top,
}


def expand_url(u: str) -> str:
    y = now_jst() - dt.timedelta(days=1)
    return (u.replace("{Y}", f"{y:%Y}").replace("{m}", f"{y:%m}").replace("{d_yesterday}", f"{y:%d}"))


def fetch(src: dict) -> list[dict]:
    headers = {"User-Agent": UA, "Accept": "application/json, application/rss+xml, application/atom+xml, text/html;q=0.9, */*;q=0.8"}
    if src["id"].startswith("gh_issues") and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    resp = requests.get(expand_url(src["url"]), headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    parser = PARSERS[src.get("parser", src["type"])]
    return [r for r in parser(src, resp) if r]


def load_seen(data_dir: Path, days: int = 14) -> set[str]:
    seen = set()
    cutoff = (now_jst() - dt.timedelta(days=days)).date()
    for p in sorted(data_dir.glob("*.jsonl")):
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d < cutoff:
            continue
        with p.open(encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["id"])
                except Exception:
                    pass
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="src/sources.yaml")
    ap.add_argument("--data", default="data")
    ap.add_argument("--only", default="")
    ap.add_argument("--sleep", type=float, default=1.0, help="情報源の間に空ける秒数")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.sources).read_text(encoding="utf-8"))
    sources = [s for s in cfg["sources"] if s.get("enabled", True)]
    if a.only:
        want = set(a.only.split(","))
        sources = [s for s in sources if s["id"] in want]

    data_dir = Path(a.data)
    data_dir.mkdir(parents=True, exist_ok=True)
    seen = load_seen(data_dir)
    today = data_dir / f"{now_jst():%Y-%m-%d}.jsonl"
    status = {"run_at": now_jst().isoformat(timespec="seconds"), "sources": {}}

    new_total = 0
    with today.open("a", encoding="utf-8") as out:
        for s in sources:
            t0 = time.time()
            try:
                recs = fetch(s)
                fresh = [r for r in recs if r["id"] not in seen]
                for r in fresh:
                    out.write(json.dumps(r, ensure_ascii=False) + "\n")
                    seen.add(r["id"])
                new_total += len(fresh)
                status["sources"][s["id"]] = {"ok": True, "got": len(recs), "new": len(fresh), "sec": round(time.time() - t0, 1)}
                print(f"ok   {s['id']:<20} got={len(recs):<3} new={len(fresh)}")
            except Exception as e:  # 1つ落ちても続ける
                status["sources"][s["id"]] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}", "sec": round(time.time() - t0, 1)}
                print(f"FAIL {s['id']:<20} {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            time.sleep(a.sleep)

    status["new_total"] = new_total
    (data_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    # 失敗の履歴（情報源の見直し用）
    hist = data_dir / "status_history.jsonl"
    with hist.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"run_at": status["run_at"], "new_total": new_total,
                            "failed": [k for k, v in status["sources"].items() if not v["ok"]]}, ensure_ascii=False) + "\n")
    print(f"new_total={new_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
