# story-sourcer

Sourcing-only repo. Pulls new, never-before-used psychology experiments
from Wikipedia. Dedup is not tracked here — it comes from your existing
Google Sheet history, compiled by the Guard node, and passed in as an
`--exclude` list each run. This repo has no memory of its own between
runs; the Sheet is the single source of truth for what has already
been used.

Scripting (the Gemini call) happens in your existing n8n Script node,
not here — see `N8N_SCRIPT_NODE.md` for the prompt and validation code.

## The picture end to end

1. **Google Sheet** — permanent record of every story already used.
2. **Guard node (n8n)** — compiles the current history from the Sheet
   into an exclude list, triggers this repo's `workflow_dispatch` with
   that list as input.
3. **story-sourcer (this repo)** — discovers candidate titles from
   Wikipedia's experiment list, filters out anything in the exclude
   list, fetches summaries for the first N new ones, writes
   `data/latest.json`, commits.
4. **n8n** reads `data/latest.json` back via the GitHub API, sends each
   record through the Script node (Gemini + validation).
5. Once a script clears validation, **Guard node appends it to the
   Sheet history** so it can never be selected again.

This repo never marks anything "used" itself — that would create a
second, competing source of truth. The Sheet stays authoritative.

## Structure

- `scripts/fetch_experiments.py` — dynamic Wikipedia discovery, filters
  against `--exclude`, hard-fails only if zero new results are found.
- `schemas/experiment.schema.json`
- `data/latest.json` — output of the most recent run only, not a
  running log (the Sheet is the log).
- `N8N_SCRIPT_NODE.md` — Gemini prompt + validation for the Script node.

## Local run

```bash
python scripts/fetch_experiments.py \
  --out data/latest.json \
  --exclude "asch-conformity-experiments,milgram-experiment" \
  --limit 3
```

## n8n integration

Guard node compiles the Sheet history into a comma-separated string and
triggers `.github/workflows/dispatch.yml` with it as the `exclude`
input — same `workflow_dispatch` pattern as `studio-wake`. No secrets
required for this repo (no Gemini call happens here).

## Extending the source list

Titles come dynamically from the linked articles on Wikipedia's
"List of psychology experiments" page — edit that page upstream to add
new source material, no code change needed here.
