#!/usr/bin/env python3
"""
Angry Metal Guy tracker (incremental).

Pulls posts from angrymetalguy.com's public WordPress REST API, keeps them in
amg_data.json, and writes index.html: a phone-friendly page where you can
change the genre / minimum score / AND-OR logic and choose which kinds of
posts to show:

  Reviews              shown ONLY if tagged with the genre OR scored >= min score
  Stuck in the Filter  always shown
  Album/Record of the Month (AMG: "Records o' the Month")  always shown
  Year-end lists       always shown
  Other articles       news, interviews, anything that isn't a review: always shown

(With --both, reviews need the genre tag AND the minimum score.)

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
TAG_RE = re.compile(r"<[^>]+>")
STATE_VERSION = 4
SHORT = {"filter": "SitF", "aotm": "AOTM", "yearend": "LIST", "other": "ART"}

# AMG now rates with WORDS (older reviews used numbers like 4.0/5.0):
#   5.0 Iconic, 4.5 Excellent, 4.0 Great, 3.5 Very Good, 3.0 Good, 2.5 Mixed,
#   2.0 Disappointing, 1.5 Bad, 1.0 Embarrassing, 0.5 Unlistenable
LEVELS = {"iconic": 5.0, "excellent": 4.5, "great": 4.0, "very good": 3.5, "good": 3.0,
          "mixed": 2.5, "disappointing": 2.0, "bad": 1.5, "embarrassing": 1.0, "unlistenable": 0.5}
NUM_RATING_RE = re.compile(r"Rating\s*:\s*([0-5](?:\.\d)?)(?!\d)", re.I)
WORD_RATING_RE = re.compile(
    r"Rating\s*:\s*(very good|" + "|".join(k for k in LEVELS if k != "very good") + r")\b", re.I)
SCORE_TAG_RE = re.compile(r"^[0-5]\.[05]$")  # AMG tags every review with its score, e.g. "3.5"

# Very specific column names; these win over everything else.
FEATURE_TITLE_RULES = [
    ("filter", re.compile(r"stuck in the filter", re.I)),
    ("aotm", re.compile(r"records?\s+o[’'`]?\s+the\s+month|album of the month|\baotm\b|\brotm\b", re.I)),
]
YEAREND_RE = re.compile(
    r"year.?end|best of 20\d\d|top \d+|albums? of the year|albums of 20\d\d"
    r"|honou?rable mentions?|staff (picks|lists?)|(best|top) [^|]{0,40}20\d\d",
    re.I)
REVIEW_TITLE_RE = re.compile(r"\breview\s*$", re.I)   # AMG review titles end in "Review"
REVIEW_TERM_RE = re.compile(r"\breviews?\b", re.I)    # category/tag "Reviews"


def strip_html(s):
    return html.unescape(TAG_RE.sub(" ", s or "")).strip()


def classify(title, terms, has_rating_line):
    """Return the post type: review, filter, aotm, yearend or other."""
    for name, rx in FEATURE_TITLE_RULES:
        if rx.search(title):
            return name
    if has_rating_line or REVIEW_TITLE_RE.search(title):
        return "review"
    if YEAREND_RE.search(title):
        return "yearend"
    if any(REVIEW_TERM_RE.search(t) for t in terms):
        return "review"
    blob = " ".join(terms)
    for name, rx in FEATURE_TITLE_RULES + [("yearend", YEAREND_RE)]:
        if rx.search(blob):
            return name
    return "other"


def extract_score(text, terms):
    """Numeric score of a review, or None if it can't be determined.
    The score tag is the most reliable source; fall back to the Rating line
    (a number for older reviews, a word like "Great" for newer ones)."""
    for t in terms:
        t = t.strip()
        if SCORE_TAG_RE.match(t) and 0.5 <= float(t) <= 5.0:
            return float(t)
    nums = NUM_RATING_RE.findall(text)
    if nums:
        return float(nums[-1])
    words = WORD_RATING_RE.findall(text)
    if words:
        return LEVELS[words[-1].lower()]
    return None


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
    terms = sorted({
        t["name"]
        for group in p.get("_embedded", {}).get("wp:term", [])
        for t in group
    })
    has_line = bool(NUM_RATING_RE.search(text) or WORD_RATING_RE.search(text))
    kind = classify(title, terms, has_line)
    score = extract_score(text, terms) if kind == "review" else None
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
    """Does a post pass the filters? Mirrors passes() in the page's JavaScript."""
    if r["type"] != "review":
        return True  # every non-review post is shown
    gen = bool(genre) and any(genre in t.lower() for t in r["tags"])
    good = r["score"] is not None and r["score"] >= min_score
    if not genre:
        return good
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
<div class="mut" id="hint"></div>
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
const TYPES = [["review","Reviews","REV"],["filter","Stuck in the Filter","SitF"],["aotm","Records / Album of the Month","AOTM"],["yearend","Year-end lists","LIST"],["other","Other articles","ART"]];
let saved = {};
try { saved = JSON.parse(localStorage.getItem("amgPrefs2") || "{}"); } catch (e) {}
const init = Object.assign({}, DATA.defaults, saved);
const on = Object.assign({review:true,filter:true,aotm:true,yearend:true,other:true}, saved.types || {});

$("genre").value = init.genre; $("min").value = init.min;
$("mode").value = init.mode; $("sort").value = init.sort; $("q").value = "";

$("meta").textContent = "Last updated " + DATA.updated + " \\u00b7 " + DATA.posts.length + " posts stored";
const labelText = {};
for (const [key, label] of TYPES) {
  const l = document.createElement("label");
  const c = document.createElement("input"); c.type = "checkbox"; c.checked = on[key]; c.dataset.type = key;
  c.addEventListener("change", () => { on[key] = c.checked; render(); });
  const span = document.createElement("span"); labelText[key] = span;
  l.append(c, span);
  $("types").append(l);
}

// Which posts pass the genre/score rules?
//   reviews: tagged with the genre OR score >= min (AND in "both" mode)
//   everything that is not a review: always shown
function passes(r, genre, min, both) {
  if (r.type !== "review") return true;
  const gen = !!genre && r.tags.some(t => t.toLowerCase().includes(genre));
  const good = r.score != null && r.score >= min;
  return !genre ? good : (both ? good && gen : good || gen);
}

function render() {
  const genre = $("genre").value.trim().toLowerCase();
  const min = parseFloat($("min").value) || 0;
  const both = $("mode").value === "and";
  const sort = $("sort").value;
  const q = $("q").value.trim().toLowerCase();
  try { localStorage.setItem("amgPrefs2", JSON.stringify({genre: $("genre").value, min, mode: $("mode").value, sort, types: on})); } catch (e) {}

  const g = genre || "any genre";
  $("hint").textContent = "Reviews: only those tagged " + g + (both ? " AND rated " : " OR rated ") + min.toFixed(1) + "+. " +
    "Everything that isn't a review is always shown.";

  const base = DATA.posts.filter(r => passes(r, genre, min, both) && (!q || r.title.toLowerCase().includes(q)));
  const counts = {};
  base.forEach(r => counts[r.type] = (counts[r.type] || 0) + 1);
  for (const [key, label] of TYPES) labelText[key].textContent = label + " (" + (counts[key] || 0) + ")";
  let rows = base.filter(r => on[r.type]);
  rows.sort((a, b) => sort === "score"
    ? ((b.score ?? -1) - (a.score ?? -1)) || b.date.localeCompare(a.date)
    : b.date.localeCompare(a.date));

  $("count").textContent = rows.length + " matches";
  const list = $("list"); list.textContent = "";
  for (const r of rows) {
    const d = document.createElement("div"); d.className = "item";
    const s = document.createElement("div");
    if (r.type === "review") { s.className = "score"; s.textContent = r.score == null ? "\\u2013" : r.score.toFixed(1); }
    else { s.className = "score small"; s.textContent = TYPES.find(t => t[0] === r.type)[2]; }
    const body = document.createElement("div");
    const a = document.createElement("a"); a.href = r.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = r.title;
    const t = document.createElement("div"); t.className = "tags"; t.textContent = r.date + " \\u00b7 " + r.tags.filter(x => !/^[0-5]\\.\\d$|^\\d{4}$|^reviews?$/i.test(x)).slice(0, 5).join(", ");
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
        label = SHORT[r["type"]] if r["type"] != "review" else (f"{r['score']:.1f}" if r["score"] is not None else "REV")
        print(f"  {label:<5} {r['date']}  {r['title']}\n        {r['url']}")
    print(f"\nWrote {out / 'index.html'}")


if __name__ == "__main__":
    main()
