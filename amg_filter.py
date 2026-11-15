#!/usr/bin/env python3
"""
Angry Metal Guy tracker (incremental).

Pulls posts from angrymetalguy.com's public WordPress REST API, keeps them in
amg_data.json, and writes index.html: a phone-friendly page where you can
change the genre / minimum score / AND-OR logic and choose which kinds of
posts to show:

  Reviews              scored reviews (genre + score filters apply to these)
  Stuck in the Filter  the regular roundup column
  Album of the Month   monthly picks
  Year-end lists       best-of / top albums / album of the year posts
  Other articles       everything else (news, interviews, features...)

Non-review posts are always shown when their type is ticked; the genre and
score filters only narrow down reviews.

Each run only fetches posts newer than the last run, so run it as often as
you like. Needs:  pip install requests

    python amg_filter.py                      # first run backfills 20 pages (~2000 posts)
    python amg_filter.py                      # later runs fetch only new posts
    python amg_filter.py --pages 60           # first run: go further back
    python amg_filter.py --full --pages 60    # rescan everything
    python amg_filter.py --out docs           # write files into ./docs
"""
import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://www.angrymetalguy.com/wp-json/wp/v2/posts"
HEADERS = {"User-Agent": "amg-review-tracker/1.0 (personal use)"}
RATING_RE = re.compile(r"Rating:\s*([0-5](?:\.\d+)?)", re.I)
TAG_RE = re.compile(r"<[^>]+>")
STATE_VERSION = 2
SHORT = {"filter": "SitF", "aotm": "AOTM", "yearend": "LIST", "other": "ART"}

# Post types, checked in order against the post TITLE (and, for unscored posts,
# the category/tag names). Edit these patterns if something gets mis-sorted.
TYPE_RULES = [
    ("filter", re.compile(r"stuck in the filter", re.I)),
    ("aotm", re.compile(r"album of the month|\baotm\b", re.I)),
    ("yearend", re.compile(
        r"year.?end|best of 20\d\d|top \d+|albums? of the year|albums of 20\d\d"
        r"|honou?rable mentions?|staff (picks|lists?)|(best|top) [^|]{0,40}20\d\d",
        re.I)),
]


def strip_html(s):
    return html.unescape(TAG_RE.sub(" ", s or "")).strip()


def classify(title, terms, score):
    """Return (type, score). Scores are only kept for real reviews."""
    for name, rx in TYPE_RULES:
        # A post with a Rating line is a review, even if the album title
        # happens to look like a year-end list ("Best of 2008"); only the
        # very specific column names override that.
        if name == "yearend" and score is not None:
            continue
        if rx.search(title):
            return name, None
    if score is not None:
        return "review", score
    blob = " ".join(terms)
    for name, rx in TYPE_RULES:
        if rx.search(blob):
            return name, None
    return "other", None


def fetch_page(page, per_page=100, after=None):
    params = {
        "page": page,
        "per_page": per_page,
        "_embed": "wp:term",
        "_fields": "id,link,date,title,content,_embedded",
    }
    if after:
        params["after"] = after
    r = requests.get(API, params=params, headers=HEADERS, timeout=30)
    if r.status_code == 400:  # WordPress answers 400 when paging past the end
        return []
    r.raise_for_status()
    return r.json()


def parse_post(p):
    title = strip_html(p["title"]["rendered"])
    text = strip_html(p["content"]["rendered"])
    m = RATING_RE.search(text)
    terms = sorted({
        t["name"]
        for group in p.get("_embedded", {}).get("wp:term", [])
        for t in group
    })
    kind, score = classify(title, terms, float(m.group(1)) if m else None)
    return {
        "id": p["id"],
        "title": title,
        "url": p["link"],
        "date": p["date"][:10],
        "type": kind,
        "score": score,
        "tags": terms,
    }


def is_hit(r, genre, min_score, both):
    """Does a post match the CLI filters? Non-reviews always do."""
    if r["type"] != "review":
        return True
    good = r["score"] >= min_score
    if not genre:
        return good
    gen = any(genre in t.lower() for t in r["tags"])
    return (good and gen) if both else (good or gen)


def update(state, pages, full):
    """Fetch new posts into state. Returns list of newly added posts."""
    after = None if full else state.get("newest")
    max_pages = pages if not after else 100
    new, newest, completed = [], state.get("newest"), True

    for page in range(1, max_pages + 1):
        try:
            posts = fetch_page(page, after=after)
        except requests.RequestException as e:
            print(f"Request failed on page {page}: {e}", file=sys.stderr)
            completed = False
            break
        if not posts:
            break
        for p in posts:
            # Posts come newest-first; remember the newest date seen
            if newest is None or p["date"] > newest:
                newest = p["date"]
            rec = parse_post(p)
            key = str(rec["id"])
            if key not in state["posts"]:
                new.append(rec)
            state["posts"][key] = rec
        print(f"Fetched page {page} ({len(state['posts'])} posts stored)", file=sys.stderr)
        time.sleep(1)  # be polite

    # Only advance the checkpoint if the run finished, so a failed run
    # can't cause newer-but-unfetched posts to be skipped next time.
    if completed:
        state["newest"] = newest
    state["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return new


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AMG tracker</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#6b6b6b;--card:#f4f4f4;--acc:#b3261e;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--mut:#9a9a9a;--card:#1e1e1e;--acc:#ff6b60}}
*{box-sizing:border-box}
body{margin:0;padding:12px;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif;max-width:720px;margin-inline:auto}
h1{font-size:1.2rem;margin:.2rem 0}
.mut{color:var(--mut);font-size:.85rem}
.ctl{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0}
.ctl label{display:flex;flex-direction:column;font-size:.8rem;color:var(--mut)}
.ctl input,.ctl select{font:inherit;padding:8px;border-radius:8px;border:1px solid var(--mut);background:var(--card);color:var(--fg)}
.ctl .wide{grid-column:1/-1}
.types{display:flex;flex-wrap:wrap;gap:6px 14px;margin:8px 0;font-size:.9rem}
.types label{display:flex;align-items:center;gap:5px}
.item{display:flex;gap:10px;background:var(--card);border-radius:10px;padding:10px;margin:8px 0}
.score{font-weight:700;color:var(--acc);min-width:2.6rem;font-size:1.1rem}
.score.small{font-size:.7rem;padding-top:.25rem;letter-spacing:.03em}
.item a{color:var(--fg);text-decoration:none;font-weight:600}
.tags{font-size:.78rem;color:var(--mut)}
</style></head><body>
<h1>Angry Metal Guy tracker</h1>
<div class="mut" id="meta"></div>
<div class="types" id="types"></div>
<div class="ctl">
  <label>Genre tag (reviews)<input id="genre" type="text" autocapitalize="none"></label>
  <label>Min score (reviews)<input id="min" type="number" step="0.1" min="0" max="5"></label>
  <label>Logic<select id="mode"><option value="or">Genre OR score</option><option value="and">Genre AND score</option></select></label>
  <label>Sort<select id="sort"><option value="date">Newest</option><option value="score">Highest score</option></select></label>
  <label class="wide">Search title<input id="q" type="search"></label>
</div>
<div class="mut" id="count"></div>
<div id="list"></div>
<script>
const DATA = __DATA__;
const $ = id => document.getElementById(id);
const TYPES = [["review","Reviews","REV"],["filter","Stuck in the Filter","SitF"],["aotm","Album of the Month","AOTM"],["yearend","Year-end lists","LIST"],["other","Other articles","ART"]];
let saved = {};
try { saved = JSON.parse(localStorage.getItem("amgPrefs2") || "{}"); } catch (e) {}
const init = Object.assign({}, DATA.defaults, saved);
const on = Object.assign({review:true,filter:true,aotm:true,yearend:true,other:true}, saved.types || {});

$("genre").value = init.genre; $("min").value = init.min;
$("mode").value = init.mode; $("sort").value = init.sort; $("q").value = "";

const counts = {};
DATA.posts.forEach(p => counts[p.type] = (counts[p.type] || 0) + 1);
$("meta").textContent = "Last updated " + DATA.updated + " \\u00b7 " + DATA.posts.length + " posts stored";
for (const [key, label] of TYPES) {
  const l = document.createElement("label");
  const c = document.createElement("input"); c.type = "checkbox"; c.checked = on[key]; c.dataset.type = key;
  c.addEventListener("change", () => { on[key] = c.checked; render(); });
  l.append(c, document.createTextNode(label + " (" + (counts[key] || 0) + ")"));
  $("types").append(l);
}

function render() {
  const genre = $("genre").value.trim().toLowerCase();
  const min = parseFloat($("min").value) || 0;
  const both = $("mode").value === "and";
  const sort = $("sort").value;
  const q = $("q").value.trim().toLowerCase();
  try { localStorage.setItem("amgPrefs2", JSON.stringify({genre: $("genre").value, min, mode: $("mode").value, sort, types: on})); } catch (e) {}

  let rows = DATA.posts.filter(r => {
    if (!on[r.type]) return false;
    if (q && !r.title.toLowerCase().includes(q)) return false;
    if (r.type !== "review") return true;   // genre/score only narrow reviews
    const good = r.score >= min;
    const gen = genre && r.tags.some(t => t.toLowerCase().includes(genre));
    return !genre ? good : (both ? good && gen : good || gen);
  });
  rows.sort((a, b) => sort === "score"
    ? ((b.score ?? -1) - (a.score ?? -1)) || b.date.localeCompare(a.date)
    : b.date.localeCompare(a.date));

  $("count").textContent = rows.length + " matches";
  const list = $("list"); list.textContent = "";
  for (const r of rows) {
    const d = document.createElement("div"); d.className = "item";
    const s = document.createElement("div");
    if (r.type === "review") { s.className = "score"; s.textContent = r.score.toFixed(1); }
    else { s.className = "score small"; s.textContent = TYPES.find(t => t[0] === r.type)[2]; }
    const body = document.createElement("div");
    const a = document.createElement("a"); a.href = r.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = r.title;
    const t = document.createElement("div"); t.className = "tags"; t.textContent = r.date + " \\u00b7 " + r.tags.slice(0, 5).join(", ");
    body.append(a, t); d.append(s, body); list.append(d);
  }
}
["genre", "min", "mode", "sort", "q"].forEach(id => $(id).addEventListener("input", render));
render();
</script></body></html>
"""


def write_html(state, out, defaults):
    data = {
        "updated": state["updated"],
        "defaults": defaults,
        "posts": sorted(state["posts"].values(), key=lambda r: r["date"], reverse=True),
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (out / "index.html").write_text(PAGE.replace("__DATA__", blob), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-score", type=float, default=4.0, help="default score cutoff (4.0)")
    ap.add_argument("--genre", default="death metal", help="default genre tag")
    ap.add_argument("--both", action="store_true", help="default to genre AND score instead of OR")
    ap.add_argument("--pages", type=int, default=20, help="pages of 100 posts for a first run / --full (default 20)")
    ap.add_argument("--full", action="store_true", help="ignore saved progress and rescan")
    ap.add_argument("--out", default=".", help="output folder (default: current)")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data_path = out / "amg_data.json"
    state = {"version": STATE_VERSION, "posts": {}, "newest": None, "updated": None}
    if data_path.exists():
        old = json.loads(data_path.read_text(encoding="utf-8"))
        # Files from the reviews-only version lack non-review posts, so start fresh
        if old.get("version") == STATE_VERSION:
            state.update(old)

    new = update(state, args.pages, args.full)
    data_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    defaults = {"genre": args.genre, "min": args.min_score,
                "mode": "and" if args.both else "or", "sort": "date"}
    write_html(state, out, defaults)

    g = args.genre.lower()
    hits = [r for r in new if is_hit(r, g, args.min_score, args.both)]
    print(f"\n{len(new)} new posts this run, {len(hits)} match your filters:")
    for r in sorted(hits, key=lambda r: (r["type"] != "review", -(r["score"] or 0))):
        label = f"{r['score']:.1f}" if r["type"] == "review" else SHORT[r["type"]]
        print(f"  {label:<5} {r['date']}  {r['title']}\n        {r['url']}")
    print(f"\nWrote {out / 'index.html'}")


if __name__ == "__main__":
    main()
