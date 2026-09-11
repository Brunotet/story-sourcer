"""
Fetches new (never-before-used) psychology experiment summaries from
Wikipedia.

Dedup source of truth is external: the compiled history of already-used
stories lives in Google Sheets, compiled by your existing Guard node.
This script does not track its own used-state — it takes that history
in as --exclude and filters against it, matching the studio convention
of the Guard node owning dedup, not individual pipeline stages.

Hard-fail by design: any network error, missing field, or empty summary
for a title being actively fetched raises immediately. A single bad
article is skipped (logged), not fatal — but zero usable results overall
is fatal, since that means the pipeline has nothing new to send forward.

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
import re
import sys
import urllib.parse
import urllib.request

WIKI_API = "https://en.wikipedia.org/w/api.php"
LIST_PAGE = "List of psychology experiments"

# Fallback list, used only if the list-page scrape returns nothing
# (e.g. page gets renamed/restructured upstream). Not the primary path.
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


def discover_titles() -> list:
    """Pulls every linked article title from the Wikipedia
    'List of psychology experiments' page. This is the dynamic
    source list — extend by editing that Wikipedia page's links,
    not this file."""
    params = {
        "action": "query",
        "titles": LIST_PAGE,
        "prop": "links",
        "pllimit": "max",
        "plnamespace": "0",
        "format": "json",
    }
    titles = []
    plcontinue = None

    while True:
        query = dict(params)
        if plcontinue:
            query["plcontinue"] = plcontinue
        url = f"{WIKI_API}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "story-sourcer/1.0"})

        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HARD FAIL: Wikipedia list page fetch returned {resp.status}")
            data = json.loads(resp.read().decode("utf-8"))

        pages = data.get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        for link in page.get("links", []):
            title = link.get("title", "")
            if title and not title.lower().startswith("list of"):
                titles.append(title)

        plcontinue = data.get("continue", {}).get("plcontinue")
        if not plcontinue:
            break

    if not titles:
        print("WARNING: list-page scrape returned nothing, using fallback list", file=sys.stderr)
        return FALLBACK_TITLES

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
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "story-sourcer/1.0"})

    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HARD FAIL: Wikipedia returned status {resp.status} for '{title}'")
        data = json.loads(resp.read().decode("utf-8"))

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        raise RuntimeError(f"HARD FAIL: no pages returned for '{title}'")

    page = next(iter(pages.values()))
    extract = page.get("extract", "").strip()

    # Cap length: enough for real, concrete detail (setup, what people
    # did, the actual result) without shipping an entire Wikipedia
    # article into the prompt. Cut cleanly at a sentence boundary
    # rather than mid-sentence.
    MAX_CHARS = 3000
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
