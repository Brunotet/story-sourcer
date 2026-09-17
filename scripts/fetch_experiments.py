"""
Fetches new (never-before-used) psychology experiment / concept summaries
from Wikipedia.

Dedup source of truth is external: the compiled history of already-used
stories lives in Google Sheets, compiled by your existing Guard node.
This script does not track its own used-state — it takes that history
in as --exclude and filters against it, matching the studio convention
of the Guard node owning dedup, not individual pipeline stages.

Hard-fail by design: any network error, missing field, or empty summary
for a title being actively fetched raises immediately. A single bad
article is skipped (logged), not fatal — but zero usable results overall
is fatal, since that means the pipeline has nothing new to send forward.

Title discovery walks a broad set of Wikipedia categories (psychology
experiments, effects, biases, and related concepts) plus their
subcategories, recursed a few levels deep. This is a large, self-
expanding pool: as Wikipedia adds articles to any of these categories
or their subcategories, they become fetchable automatically, with no
code change needed here. Extend the pool by editing SEED_CATEGORIES,
not by hardcoding article titles. A short FALLBACK_TITLES list still
exists as a last resort if literally every seed category and its
subcategories come back empty (e.g. total API outage), but under
normal conditions the category walk should never be exhausted.

Usage:
    python fetch_experiments.py --out new_experiments.json --exclude asch-conformity-experiments,milgram-experiment --limit 3

--exclude accepts comma-separated slugs (matching this script's own
slugify()) OR comma-separated raw titles — both are normalized before
comparison, so the Guard node can pass through whatever it already
stores in the Sheet without needing to pre-slugify it.
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

WIKI_API = "https://en.wikipedia.org/w/api.php"

# A descriptive User-Agent per Wikipedia's API etiquette (WP:UA) reduces
# throttling versus an unidentified/generic one.
WIKI_USER_AGENT = "story-sourcer/1.0 (github.com/Brunotet/story-sourcer; automated content pipeline)"

# Small delay between every Wikipedia API call. The category walk can
# make well over a hundred calls in one run; pacing them avoids
# tripping the API's rate limit in the first place.
REQUEST_DELAY_SECONDS = 0.4

# Retry behavior for transient failures (429 Too Many Requests, 5xx,
# connection errors). Honors a Retry-After header when Wikipedia sends
# one; otherwise backs off with increasing delay.
MAX_RETRIES = 5
RETRY_BACKOFF_BASE_SECONDS = 2

# Broad seed set. Each of these is walked plus its subcategories (see
# MAX_SUBCATEGORY_DEPTH), so the effective pool is much larger than
# this list alone -- these are just entry points into the category
# tree. Add more seeds here any time you want to widen the topic net;
# no other code needs to change.
SEED_CATEGORIES = [
    "Category:Psychological experiments",
    "Category:Cognitive biases",
    "Category:Psychological effects",
    "Category:Memory biases",
    "Category:Decision-making",
    "Category:Conditioning",
    "Category:Behavioral concepts",
    "Category:Social psychology",
    "Category:Experimental psychology",
    "Category:Psychological theories",
    "Category:Heuristics",
    "Category:Cognitive science",
    "Category:Attribution (psychology)",
    "Category:Group processes",
    "Category:Human behavior",
]

# How many levels of subcategories to recurse into from each seed.
# 2 is generous without letting the walk explode indefinitely.
MAX_SUBCATEGORY_DEPTH = 2

# Safety cap on total distinct categories visited across the whole
# walk (seeds + subcats). Each category costs 2 API calls (pages +
# subcats), so this bounds the run to roughly 2x this many requests
# (plus retries) at REQUEST_DELAY_SECONDS apart -- kept modest so a
# single run finishes in a reasonable time even with the added
# pacing/retry logic below.
MAX_CATEGORIES_VISITED = 60

# Fallback list, used only if the ENTIRE category walk (every seed
# category and every subcategory found) returns zero articles --
# e.g. a total Wikipedia API outage. Not the primary path, and under
# normal conditions should never be reached.
FALLBACK_TITLES = [
    "Asch conformity experiments",
    "Milgram experiment",
    "Stanford prison experiment",
    "Bystander effect",
    "False memory",
    "Cognitive dissonance",
    "Halo effect",
    "Anchoring (cognitive bias)",
    "Loss aversion",
    "Confirmation bias",
    "Learned helplessness",
    "Marshmallow experiment",
]


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _wiki_get(params: dict) -> dict:
    """GET against the Wikipedia API with retry/backoff on transient
    failures. urllib raises HTTPError automatically for any non-2xx
    status (it never reaches a manual status check), so those must be
    caught explicitly rather than inspected on the response object."""
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": WIKI_USER_AGENT})

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = RETRY_BACKOFF_BASE_SECONDS * attempt
                else:
                    wait = RETRY_BACKOFF_BASE_SECONDS * attempt
                print(
                    f"WARNING: Wikipedia API returned {e.code}, retrying in "
                    f"{wait:.1f}s (attempt {attempt}/{MAX_RETRIES})",
                    file=sys.stderr,
                )
                last_error = e
                time.sleep(wait)
                continue
            # Non-retryable HTTP error (404, 400, etc.) — surface it as
            # the RuntimeError callers already know how to handle.
            raise RuntimeError(f"HARD FAIL: Wikipedia API returned status {e.code}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            wait = RETRY_BACKOFF_BASE_SECONDS * attempt
            print(
                f"WARNING: Wikipedia API request failed ({e}), retrying in "
                f"{wait:.1f}s (attempt {attempt}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            last_error = e
            time.sleep(wait)
            continue
        finally:
            time.sleep(REQUEST_DELAY_SECONDS)

    raise RuntimeError(
        f"HARD FAIL: Wikipedia API request failed after {MAX_RETRIES} retries: {last_error}"
    )


def fetch_category_members(category_title: str, member_type: str) -> list:
    """member_type is 'page' (articles in the category) or 'subcat'
    (subcategories of the category). Paginates via cmcontinue until
    exhausted. Returns a list of titles."""
    titles = []
    cmcontinue = None

    while True:
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": category_title,
            "cmtype": member_type,
            "cmlimit": "max",
            "format": "json",
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue

        data = _wiki_get(params)
        members = data.get("query", {}).get("categorymembers", [])
        for member in members:
            title = member.get("title", "")
            if title:
                titles.append(title)

        cmcontinue = data.get("continue", {}).get("cmcontinue")
        if not cmcontinue:
            break

    return titles


def discover_titles() -> list:
    """Walks SEED_CATEGORIES and their subcategories (to
    MAX_SUBCATEGORY_DEPTH) and merges every article title found into
    one deduped pool. Self-expanding: grows automatically as
    Wikipedia's category tree grows, no fixed list to exhaust."""
    seen_categories = set()
    all_titles = set()

    def walk(category_title: str, depth: int) -> None:
        if category_title in seen_categories:
            return
        if len(seen_categories) >= MAX_CATEGORIES_VISITED:
            return
        seen_categories.add(category_title)

        try:
            pages = fetch_category_members(category_title, "page")
        except RuntimeError as e:
            print(f"SKIP CATEGORY (pages): {e} [{category_title}]", file=sys.stderr)
            pages = []
        all_titles.update(pages)

        if depth >= MAX_SUBCATEGORY_DEPTH:
            return

        try:
            subcats = fetch_category_members(category_title, "subcat")
        except RuntimeError as e:
            print(f"SKIP CATEGORY (subcats): {e} [{category_title}]", file=sys.stderr)
            subcats = []

        for subcat in subcats:
            walk(subcat, depth + 1)

    for seed in SEED_CATEGORIES:
        walk(seed, depth=1)

    titles = [t for t in all_titles if not t.lower().startswith("list of")]

    if not titles:
        print(
            "WARNING: category walk returned nothing across all seed categories "
            f"and subcategories ({len(seen_categories)} categories checked), "
            "using fallback list",
            file=sys.stderr,
        )
        return FALLBACK_TITLES

    random.shuffle(titles)
    return titles


def fetch_summary(title: str) -> dict:
    # NOTE: exintro is deliberately omitted. The lead paragraph alone is
    # too thin for scripting — it gives the abstract framing (who, when,
    # broad topic) but not the concrete, filmable details a story needs
    # (what the setup physically looked like, what people actually did,
    # what the real result was). Pulling the full plaintext extract and
    # truncating gives the Script node's LLM actual material to draw
    # specific sentences from instead of vague paraphrase.
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": "1",
        "format": "json",
        "titles": title,
        "redirects": "1",
    }
    data = _wiki_get(params)

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        raise RuntimeError(f"HARD FAIL: no pages returned for '{title}'")

    page = next(iter(pages.values()))
    extract = page.get("extract", "").strip()

    # Cap length: enough for real, concrete detail (setup, what people
    # did, the actual result) without shipping an entire Wikipedia
    # article into the prompt. Cut cleanly at a sentence boundary
    # rather than mid-sentence.
    MAX_CHARS = 4500
    if len(extract) > MAX_CHARS:
        truncated = extract[:MAX_CHARS]
        last_period = truncated.rfind(". ")
        extract = truncated[:last_period + 1] if last_period > 0 else truncated

    if not extract:
        raise RuntimeError(f"HARD FAIL: empty summary for '{title}' — refusing to write blank record")

    canonical_title = page.get("title", title)
    return {
        "id": slugify(canonical_title),
        "name": canonical_title,
        "source_url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(canonical_title.replace(' ', '_'))}",
        "summary": extract,
        "fetched_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def parse_exclude(raw: str) -> set:
    if not raw:
        return set()
    return {slugify(item.strip()) for item in raw.split(",") if item.strip()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="path to write the new batch of experiments")
    parser.add_argument("--exclude", default="", help="comma-separated slugs or titles already used, from the Sheet history")
    parser.add_argument("--titles", nargs="*", default=None, help="override the discovered title list")
    parser.add_argument("--limit", type=int, default=3, help="max new experiments to fetch this run")
    args = parser.parse_args()

    excluded = parse_exclude(args.exclude)
    titles = args.titles if args.titles else discover_titles()

    results = []
    for title in titles:
        if len(results) >= args.limit:
            break
        if slugify(title) in excluded:
            continue
        try:
            record = fetch_summary(title)
        except RuntimeError as e:
            # A single article with no usable extract (disambig page,
            # stub, etc.) should not kill the whole run — skip and log.
            print(f"SKIP: {e}", file=sys.stderr)
            continue
        if record["id"] in excluded:
            # canonical title after redirect resolution might match
            # an excluded slug even if the raw title didn't
            continue
        results.append(record)

    if not results:
        raise RuntimeError(
            "HARD FAIL: no new experiments found — either the exclude list covers "
            "everything discoverable, or Wikipedia fetches are failing. Not writing "
            "empty output for the pipeline to mistake for 'nothing new exists'."
        )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(results)} new experiments to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
