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
  * SOURCES are tried in this order (--sources): wikipedia -> wikisource -> gutenberg. Wikipedia is
    always first and is searched across ALL groups before any other source is touched. The other two
    are safety nets: every error inside them is caught and logged, so they can never break a run
    that Wikipedia could have served.
      - wikisource: public-domain texts transcribed on Wikisource (MediaWiki search + same filters).
      - gutenberg:  old public-domain books (Project Gutenberg, via the free Gutendex catalogue).
                    A book is huge, so the best ~4,000-character excerpt is chosen with the same
                    business-story scoring (event + year + concrete result).
    Library of Congress newspapers are NOT included: their OCR text is full of misspelled names and
    numbers, which would break the "every fact is checkable" rule of the script prompt.

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
import time
import urllib.error
import urllib.parse
import urllib.request

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

SOURCES_DEFAULT = "wikipedia,wikisource,gutenberg"      # order = priority (Wikipedia first)
WIKISOURCE_API = "https://en.wikisource.org/w/api.php"
GUTENDEX = "https://gutendex.com/books"
GUTENBERG_TOPICS = [          # Gutenberg subjects/bookshelves (Gutendex ?topic=) with real business stories
    "advertising", "salesmanship", "business", "merchandising", "industries",
    "corporations", "businessmen", "capitalists",
]
GUTENBERG_MAX_BOOKS = 40      # candidate books examined per run
EXCERPT_MIN_CHARS = 1800
EXCERPT_MIN_SCORE = 18
WIKISOURCE_QUERIES = {        # search phrases per story type (Wikisource full-text search)
    "Brand Blunders": ["company recall product failure", "advertising scandal company sales"],
    "Marketing Genius": ["advertising campaign sales customers", "slogan product launched company"],
    "Rebrands & Comebacks": ["company changed name brand sales", "product relaunched company customers"],
    "Startup Origins": ["founded company first customers sales", "business began capital partners"],
    "Pricing & Persuasion": ["price sales customers merchant", "salesman persuaded customers sales"],
    "Online Business & Web": ["telegraph telephone company customers sales", "mail order catalogue company sales"],
}

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
    has_year = re.search(r"\b(1[5-9]|20)\d{2}\b", extract) is not None   # 1500-2099: old books count too
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


def _http_get(url: str, timeout: int = 40, max_bytes: int = 6_000_000, retries: int = 3) -> bytes:
    """Plain HTTP GET with retries (used for Gutendex and the Gutenberg text files)."""
    req = urllib.request.Request(url, headers={"User-Agent": fe.WIKI_USER_AGENT})
    last = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(max_bytes)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(2 * attempt)
    raise RuntimeError(f"HTTP GET failed for {url}: {last}")


def _wiki_call(api: str, params: dict) -> dict:
    """fe._wiki_get (retry/backoff/pacing) pointed at another MediaWiki site (Wikisource)."""
    old = fe.WIKI_API
    fe.WIKI_API = api
    try:
        return fe._wiki_get(params)
    finally:
        fe.WIKI_API = old


def check_story_text(title: str, extract: str, check_biography: bool = True) -> None:
    """Raises RuntimeError('SKIP-...') unless the text is a usable business story."""
    if len(extract) < MIN_CHARS:
        raise RuntimeError(f"SKIP-TOO-SHORT: '{title}' ({len(extract)} chars)")
    if check_biography and looks_like_biography(extract):
        raise RuntimeError(f"SKIP-BIOGRAPHY: '{title}' is a life story, not a brand/business story")
    if not looks_like_a_business_story(extract):
        raise RuntimeError(f"SKIP-NOT-A-STORY: '{title}' has no clear event (launch/campaign/recall...) with a year")
    if not has_concrete_result(extract):
        raise RuntimeError(f"SKIP-NO-RESULT: '{title}' has no concrete result (money, percent, sales, reaction)")


def _cap(extract: str) -> str:
    if len(extract) > MAX_CHARS:
        cut = extract[:MAX_CHARS]
        last = cut.rfind(". ")
        return cut[: last + 1] if last > 0 else cut
    return extract


def _record(name, source_url, summary, group, source) -> dict:
    return {
        "id": fe.slugify(name), "name": name, "source_url": source_url, "summary": summary,
        "group": group, "source": source, "fetched_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------- Wikipedia (first) and Wikisource
def fetch_story(title: str, group: str, source: str = "wikipedia") -> dict:
    params = {"action": "query", "prop": "extracts", "explaintext": "1", "format": "json",
              "titles": title, "redirects": "1"}
    if source == "wikisource":
        data, site = _wiki_call(WIKISOURCE_API, params), "https://en.wikisource.org/wiki/"
    else:
        data, site = fe._wiki_get(params), "https://en.wikipedia.org/wiki/"
    pages = data.get("query", {}).get("pages", {})
    if not pages:
        raise RuntimeError(f"no pages returned for '{title}'")
    page = next(iter(pages.values()))
    extract = _cap((page.get("extract") or "").strip())
    check_story_text(title, extract)
    canonical = page.get("title", title)
    return _record(canonical, site + urllib.parse.quote(canonical.replace(" ", "_")), extract, group, source)


def wikisource_titles(group: str) -> list:
    titles = set()
    for q in WIKISOURCE_QUERIES.get(group, []):
        try:
            data = _wiki_call(WIKISOURCE_API, {"action": "query", "list": "search", "srsearch": q,
                                               "srnamespace": "0", "srlimit": "30", "format": "json"})
            titles.update(h.get("title", "") for h in data.get("query", {}).get("search", []))
        except RuntimeError as e:
            print(f"SKIP WIKISOURCE SEARCH: {e} [{q}]", file=sys.stderr)
    pool = [t for t in titles if t]
    random.shuffle(pool)
    print(f"[wikisource/{group}] {len(pool)} candidate titles", file=sys.stderr)
    return pool


# ---------------------------------------------------------------- Project Gutenberg (old books)
def _author_name(raw: str) -> str:
    """'Barnum, P. T. (Phineas Taylor)' -> 'P. T. Barnum'"""
    raw = re.sub(r"\(.*?\)", "", raw or "").strip()
    if "," in raw:
        last, first = raw.split(",", 1)
        raw = f"{first.strip()} {last.strip()}"
    return raw.strip() or "Unknown author"


def gutenberg_books() -> list:
    """Candidate books from Gutendex (free catalogue API): English, with a plain-text download."""
    books = {}
    for topic in GUTENBERG_TOPICS:
        url = f"{GUTENDEX}?languages=en&mime_type=text%2Fplain&topic={urllib.parse.quote(topic)}"
        try:
            data = json.loads(_http_get(url, timeout=30).decode("utf-8", errors="replace"))
        except (RuntimeError, ValueError) as e:
            print(f"SKIP GUTENDEX: {e} [{topic}]", file=sys.stderr)
            continue
        for b in data.get("results", []):
            txt = next((u for k, u in (b.get("formats") or {}).items()
                        if k.startswith("text/plain") and not u.endswith(".zip")), None)
            if b.get("id") and b.get("title") and txt:
                authors = b.get("authors") or []
                books[b["id"]] = {"id": b["id"], "title": b["title"].split("\n")[0].strip(),
                                  "author": _author_name(authors[0].get("name", "") if authors else ""),
                                  "url": txt}
        time.sleep(0.5)
    pool = list(books.values())
    random.shuffle(pool)
    print(f"[gutenberg] {len(pool)} candidate books", file=sys.stderr)
    return pool[:GUTENBERG_MAX_BOOKS]


def strip_gutenberg_boilerplate(text: str) -> str:
    text = text.replace("\r\n", "\n")
    i = text.find("*** START OF")
    if i >= 0:
        j = text.find("\n", i)
        text = text[j + 1:] if j >= 0 else text[i:]
    k = text.find("*** END OF")
    return text[:k] if k >= 0 else text


def _score(extract: str) -> int:
    low = extract.lower()
    s = sum(1 for w in BUSINESS_SIGNAL_WORDS if w in low)
    s += 3 * sum(1 for w in EVENT_WORDS if w in low)
    s += 2 * sum(1 for p in RESULT_PATTERNS if re.search(p, low))
    s += 3 if re.search(r"\b(1[5-9]|20)\d{2}\b", extract) else 0
    return s


def best_excerpt(text: str):
    """Best ~2-4k character window of a long book (paragraph-aligned) by business-story score, or None."""
    paras = [re.sub(r"\s*\n\s*", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    paras = [p for p in paras if len(p) > 80 and not p.isupper()]
    if len(paras) < 3:
        return None
    starts = list(range(0, len(paras), 2))
    if len(starts) > 400:                       # keep the scan fast on huge books
        starts = sorted(random.sample(starts, 400))
    best, best_score = None, -1
    for i in starts:
        buf, j = "", i
        while j < len(paras) and len(buf) < EXCERPT_MIN_CHARS:
            buf = (buf + " " + paras[j]).strip()
            j += 1
        if len(buf) < EXCERPT_MIN_CHARS:
            continue
        buf = _cap(buf)
        sc = _score(buf)
        if sc > best_score:
            best, best_score = buf, sc
    return (best, best_score) if best and best_score >= EXCERPT_MIN_SCORE else None


def fetch_gutenberg_story(book: dict, group: str) -> dict:
    raw = _http_get(book["url"], timeout=60).decode("utf-8", errors="replace")
    found = best_excerpt(strip_gutenberg_boilerplate(raw))
    if not found:
        raise RuntimeError(f"SKIP-NO-EXCERPT: '{book['title']}' has no strong business-story passage")
    excerpt, _ = found
    name = f"{book['title']} by {book['author']}"
    check_story_text(name, excerpt, check_biography=False)
    return _record(name, f"https://www.gutenberg.org/ebooks/{book['id']}", excerpt, group, "gutenberg")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--exclude", default="", help="comma-separated titles or slugs already used (from the Sheet)")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--group", default="", help="story type; must be a key of GROUPS (empty = random)")
    ap.add_argument("--sources", default=SOURCES_DEFAULT, help="comma-separated, in priority order")
    args = ap.parse_args()

    excluded = fe.parse_exclude(args.exclude)
    if args.group and args.group not in GROUPS:
        raise RuntimeError(f"HARD FAIL: unknown group '{args.group}'. Valid: {', '.join(GROUPS)}")
    sources = [s.strip().lower() for s in (args.sources or SOURCES_DEFAULT).split(",") if s.strip()]
    bad = [s for s in sources if s not in ("wikipedia", "wikisource", "gutenberg")]
    if bad:
        raise RuntimeError(f"HARD FAIL: unknown source(s) {bad}. Valid: wikipedia, wikisource, gutenberg")
    first = args.group or random.choice(list(GROUPS))
    order = [first] + [g for g in GROUPS if g != first]      # fall back to the other groups if one runs dry

    results = []

    def add(rec) -> bool:
        if rec["id"] in excluded or any(r["id"] == rec["id"] for r in results):
            return False
        results.append(rec)
        print(f"FOUND [{rec['source']}] {rec['name']}", file=sys.stderr)
        return True

    for source in sources:
        if len(results) >= args.limit:
            break
        try:
            if source in ("wikipedia", "wikisource"):
                for group in order:
                    if len(results) >= args.limit:
                        break
                    pool = discover_titles(group, GROUPS[group]) if source == "wikipedia" else wikisource_titles(group)
                    attempts = 0
                    for title in pool:
                        if len(results) >= args.limit or attempts >= MAX_FETCH_ATTEMPTS_PER_GROUP:
                            break
                        if fe.slugify(title) in excluded:
                            continue
                        attempts += 1
                        try:
                            add(fetch_story(title, group, source))
                        except RuntimeError as e:
                            print(f"SKIP: {e}", file=sys.stderr)
                    if len(results) < args.limit:
                        print(f"[{source}/{group}] gave {len(results)}/{args.limit}; trying the next group", file=sys.stderr)
            else:  # gutenberg
                attempts = 0
                for book in gutenberg_books():
                    if len(results) >= args.limit or attempts >= MAX_FETCH_ATTEMPTS_PER_GROUP:
                        break
                    if fe.slugify(f"{book['title']} by {book['author']}") in excluded:
                        continue
                    attempts += 1
                    try:
                        add(fetch_gutenberg_story(book, first))
                    except RuntimeError as e:
                        print(f"SKIP: {e}", file=sys.stderr)
        except Exception as e:   # a fallback source must never kill a run that another source can serve
            print(f"SOURCE FAILED ({source}): {e!r} - moving on", file=sys.stderr)
        if len(results) < args.limit:
            print(f"[{source}] total so far {len(results)}/{args.limit}; trying the next source", file=sys.stderr)

    if not results:
        raise RuntimeError(
            "HARD FAIL: no new stories found in any source - either the exclude list covers everything "
            "discoverable, every candidate failed the story filters, or the sources are failing."
        )
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(results)} new stories to {args.out} (first: {results[0]['name']} / {results[0]['source']} / {results[0]['group']})", file=sys.stderr)


if __name__ == "__main__":
    main()
