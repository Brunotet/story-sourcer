"""
Fetches new (never-before-used) BRAND / MARKETING / BUSINESS stories from Wikipedia.

Sibling of fetch_experiments.py (which stays untouched and keeps feeding the psychology
channel). This file REUSES its Wikipedia helpers (retry/backoff, category walk, slugify) and
only changes WHAT is looked for:

  * SEED categories are grouped by story type (GROUPS below). n8n's Guard node rotates the
    group name (it must match a key of GROUPS exactly) and sends it as --group.
  * Quality filters are about business stories instead of experiments: a real event
    (launch, campaign, recall, rebrand...), a year, and a concrete result (money, %, sales,
    reaction). Biographies, stubs and plain concept pages are rejected.
  * If the chosen group yields nothing new, the other groups are tried before hard-failing.

Dedup is external, exactly like the psychology flow: the Google Sheet history is passed in
as --exclude (comma-separated titles or slugs).

Usage:
    python scripts/fetch_stories.py --out data/latest_stories.json \
        --exclude "new-coke,streisand-effect" --limit 1 --group "Brand Blunders"

Extend the pool by adding category names to GROUPS - no other change needed. A category that
does not exist (or is empty) is simply skipped; the run log prints how many titles were found.
"""

import argparse
import datetime
import json
import random
import re
import sys
import urllib.parse

import fetch_experiments as fe   # same folder: reuses _wiki_get, fetch_category_members, slugify

# Group name -> Wikipedia categories (walked + their subcategories). Names must match n8n's Guard node.
GROUPS = {
    "Brand Blunders": [
        "Category:Corporate scandals", "Category:Product recalls", "Category:Consumer boycotts",
        "Category:Advertising controversies", "Category:Product failures", "Category:Marketing failures",
        "Category:Business failures", "Category:Discontinued products",
    ],
    "Marketing Genius": [
        "Category:Advertising campaigns", "Category:Viral marketing", "Category:Marketing techniques",
        "Category:Guerrilla marketing", "Category:Advertising slogans", "Category:Brand management",
        "Category:Product placement",
    ],
    "Rebrands & Comebacks": [
        "Category:Rebranding", "Category:Brand management", "Category:Corporate rebranding",
        "Category:Brand names", "Category:Company turnarounds", "Category:Product relaunches",
    ],
    "Startup Origins": [
        "Category:Startup companies", "Category:Entrepreneurship", "Category:Business models",
        "Category:Unicorn startup companies", "Category:Y Combinator companies",
    ],
    "Pricing & Persuasion": [
        "Category:Pricing", "Category:Pricing strategies", "Category:Consumer behaviour",
        "Category:Persuasion", "Category:Sales", "Category:Marketing strategy",
    ],
    "Online Business & Web": [
        "Category:Internet marketing", "Category:Web design", "Category:E-commerce",
        "Category:Dot-com bubble", "Category:Online advertising", "Category:Search engine optimization",
        "Category:Social media", "Category:Digital marketing",
    ],
}

MAX_SUBCATEGORY_DEPTH = 2
MAX_CATEGORIES_PER_GROUP = 40
MAX_FETCH_ATTEMPTS_PER_GROUP = 40     # bounds the run time if most candidates get rejected
MIN_CHARS = 1200                      # shorter articles are stubs: not enough material for a script
MAX_CHARS = 4500                      # same cap as the psychology sourcer

BUSINESS_SIGNAL_WORDS = [
    "company", "brand", "campaign", "customers", "sales", "revenue", "market", "founded", "advertis",
    "marketing", "product", "consumer", "business", "profit", "slogan", "retail", "store", "commercial",
    "launched", "sold",
]
EVENT_WORDS = [
    "launched", "introduced", "announced", "released", "campaign", "acquired", "filed", "recalled",
    "sued", "founded", "rebrand", "relaunch", "withdrew", "discontinued", "reintroduced", "unveiled",
    "decided", "replaced", "backlash", "boycott",
]
RESULT_PATTERNS = [
    r"[$£€]\s?\d", r"\d+(\.\d+)?\s*(%|percent)", r"\b\d[\d,.]*\s+(million|billion|thousand)\b",
    r"\b(million|billion)\b", r"\b(sales|revenue|profits?|market share)\b.{0,60}\b(rose|fell|dropped|increased|decreased|doubled|tripled|surged|plunged)\b",
    r"\b(bankrupt|bankruptcy|recall|recalled|boycott|backlash|lawsuit|withdrew|withdrawn|discontinued|reintroduced|relaunched)\b",
]
BIOGRAPHY_RE = re.compile(r"\((?:born\b|c\. ?\d{4}|\d{1,2} \w+ \d{4}|\w+ \d{1,2}, \d{4})")


def looks_like_biography(extract: str) -> bool:
    """Wikipedia biographies open with the birth date in brackets. Stories about a person are fine,
    but the page itself must be about an event / campaign / company, not a life story."""
    return bool(BIOGRAPHY_RE.search(extract[:400]))


def looks_like_a_business_story(extract: str) -> bool:
    low = extract.lower()
    signals = sum(1 for w in BUSINESS_SIGNAL_WORDS if w in low)
    has_event = any(w in low for w in EVENT_WORDS)
    has_year = re.search(r"\b(19|20)\d{2}\b", extract) is not None
    return signals >= 3 and has_event and has_year


def has_concrete_result(extract: str) -> bool:
    low = extract.lower()
    return any(re.search(p, low) for p in RESULT_PATTERNS)


def discover_titles(group: str, categories: list) -> list:
    """Walk the group's categories + subcategories; returns a shuffled, de-duplicated title pool."""
    seen_cats, titles = set(), set()

    def walk(cat: str, depth: int) -> None:
        if cat in seen_cats or len(seen_cats) >= MAX_CATEGORIES_PER_GROUP:
            return
        seen_cats.add(cat)
        try:
            titles.update(fe.fetch_category_members(cat, "page"))
        except RuntimeError as e:
            print(f"SKIP CATEGORY (pages): {e} [{cat}]", file=sys.stderr)
        if depth >= MAX_SUBCATEGORY_DEPTH:
            return
        try:
            subs = fe.fetch_category_members(cat, "subcat")
        except RuntimeError as e:
            print(f"SKIP CATEGORY (subcats): {e} [{cat}]", file=sys.stderr)
            subs = []
        for sub in subs:
            walk(sub, depth + 1)

    for c in categories:
        walk(c, 1)
    pool = [t for t in titles if not t.lower().startswith(("list of", "category:", "template:", "wikipedia:"))]
    random.shuffle(pool)
    print(f"[{group}] {len(seen_cats)} categories visited, {len(pool)} candidate titles", file=sys.stderr)
    return pool


def fetch_story(title: str, group: str) -> dict:
    data = fe._wiki_get({
        "action": "query", "prop": "extracts", "explaintext": "1", "format": "json",
        "titles": title, "redirects": "1",
    })
    pages = data.get("query", {}).get("pages", {})
    if not pages:
        raise RuntimeError(f"no pages returned for '{title}'")
    page = next(iter(pages.values()))
    extract = (page.get("extract") or "").strip()
    if len(extract) > MAX_CHARS:
        cut = extract[:MAX_CHARS]
        last = cut.rfind(". ")
        extract = cut[: last + 1] if last > 0 else cut
    if len(extract) < MIN_CHARS:
        raise RuntimeError(f"SKIP-TOO-SHORT: '{title}' ({len(extract)} chars)")
    if looks_like_biography(extract):
        raise RuntimeError(f"SKIP-BIOGRAPHY: '{title}' is a life story, not a brand/business story")
    if not looks_like_a_business_story(extract):
        raise RuntimeError(f"SKIP-NOT-A-STORY: '{title}' has no clear event (launch/campaign/recall...) with a year")
    if not has_concrete_result(extract):
        raise RuntimeError(f"SKIP-NO-RESULT: '{title}' has no concrete result (money, percent, sales, reaction)")
    canonical = page.get("title", title)
    return {
        "id": fe.slugify(canonical),
        "name": canonical,
        "source_url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(canonical.replace(' ', '_'))}",
        "summary": extract,
        "group": group,
        "fetched_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--exclude", default="", help="comma-separated titles or slugs already used (from the Sheet)")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--group", default="", help="story type; must be a key of GROUPS (empty = random)")
    args = ap.parse_args()

    excluded = fe.parse_exclude(args.exclude)
    if args.group and args.group not in GROUPS:
        raise RuntimeError(f"HARD FAIL: unknown group '{args.group}'. Valid: {', '.join(GROUPS)}")
    first = args.group or random.choice(list(GROUPS))
    order = [first] + [g for g in GROUPS if g != first]      # fall back to the other groups if one runs dry

    results = []
    for group in order:
        if len(results) >= args.limit:
            break
        attempts = 0
        for title in discover_titles(group, GROUPS[group]):
            if len(results) >= args.limit or attempts >= MAX_FETCH_ATTEMPTS_PER_GROUP:
                break
            if fe.slugify(title) in excluded:
                continue
            attempts += 1
            try:
                rec = fetch_story(title, group)
            except RuntimeError as e:
                print(f"SKIP: {e}", file=sys.stderr)
                continue
            if rec["id"] in excluded or any(r["id"] == rec["id"] for r in results):
                continue
            results.append(rec)
        if len(results) < args.limit:
            print(f"[{group}] gave {len(results)}/{args.limit}; trying the next group", file=sys.stderr)

    if not results:
        raise RuntimeError(
            "HARD FAIL: no new stories found in any group - either the exclude list covers everything "
            "discoverable, every candidate failed the story filters, or Wikipedia is failing."
        )
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(results)} new stories to {args.out} (first: {results[0]['name']} / {results[0]['group']})", file=sys.stderr)


if __name__ == "__main__":
    main()
