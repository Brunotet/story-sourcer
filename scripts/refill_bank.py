import base64, json, os, re, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from semdup import _embed
import numpy as np
UA = {"User-Agent": "story-sourcer-refill/1.0 (github.com/Brunotet)"}
API = "https://en.wikipedia.org/w/api.php"
cands = json.loads(base64.b64decode(os.environ["CANDS_B64"]).decode())
history = [h.strip() for h in os.environ.get("HISTORY", "").split(",") if h.strip()]
remove = set(r.strip().lower() for r in os.environ.get("REMOVE", "").split("|") if r.strip())
bank = [b for b in json.load(open("data/topics.json")) if b["title"].lower() not in remove]
THR = float(os.environ.get("THRESHOLD", "0.62"))
MAXADD = int(os.environ.get("MAXADD", "120"))
# The gate reads the FULL article, not just the lead. Some leads are one short sentence (Pepsi: ~250 chars)
# while the history further down is rich. The real story research happens later in the Brain (web dossier).
MIN_CHARS = 1500
MAX_TEXT = 8000
MIN_MARKERS = 3
MARKERS = ["advertis", "campaign", "marketing", "marketed", "slogan", "brand", "promot", "profit", "sold", "sales", "company", "invented", "popular", "tradition", "myth", "claim", "lobby", "consumer", "customers", "founded"]
report = []
if remove:
    report.append("removed from bank: " + ", ".join(sorted(remove)))
def get(url, tries=4):
    last = None
    for a in range(tries):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25))
        except Exception as e:
            last = e
            time.sleep(1.5 * (a + 1))
    raise last
def wiki(title):
    q = urllib.parse.urlencode({"action": "query", "prop": "extracts|pageprops", "ppprop": "disambiguation", "explaintext": "1", "redirects": "1", "format": "json", "titles": title})
    d = get(API + "?" + q)
    pages = (d.get("query") or {}).get("pages") or {}
    pg = next(iter(pages.values()), None)
    if not pg or "missing" in pg:
        return None
    if "disambiguation" in (pg.get("pageprops") or {}):
        return None
    ex = (pg.get("extract") or "").strip()
    if len(ex) < MIN_CHARS:
        return None
    return pg.get("title", title), ex[:MAX_TEXT]
def words(s):
    return set(re.findall(r"[a-z0-9]+", s.lower()))
def resolve(title):
    errs = []
    try:
        w = wiki(title)
        if w:
            return w, None
    except Exception as e:
        errs.append(str(e)[:60])
    try:
        data = get("https://en.wikipedia.org/w/rest.php/v1/search/title?q=" + urllib.parse.quote(title) + "&limit=3")
        for p in data.get("pages", []):
            cand = p.get("title", "")
            a, b = words(title), words(cand)
            if cand and a and len(a & b) / len(a | b) >= 0.5:
                try:
                    w = wiki(cand)
                except Exception as e:
                    errs.append(str(e)[:60])
                    continue
                if w:
                    return w, None
    except Exception as e:
        errs.append(str(e)[:60])
    return None, ("; ".join(errs) if errs else None)
existing = set(b["title"].lower() for b in bank)
checked = []
seen = set()
for c in cands:
    t = str(c.get("title", "")).strip()
    q = str(c.get("q", "")).strip()
    if not t or not q or len(q) > 140:
        continue
    if t.lower() in existing or t.lower() in seen:
        report.append("SKIP-DUP-TITLE " + t)
        continue
    seen.add(t.lower())
    time.sleep(0.3)
    w, err = resolve(t)
    if not w:
        report.append(("ERR-WIKI %s (%s)" % (t, err)) if err else ("NO-WIKI " + t))
        continue
    canon, ex = w
    if canon.lower() in existing:
        report.append("SKIP-DUP-CANON " + canon)
        continue
    low = ex.lower()
    hits = [m for m in MARKERS if m in low]
    if len(hits) < MIN_MARKERS:
        report.append("NO-STORY %s (%d markers)" % (canon, len(hits)))
        continue
    checked.append({"title": canon, "q": q, "text": canon + ". " + q + " " + ex[:300]})
base_texts = [b["title"] + ". " + b["q"] for b in bank] + history
accepted = []
if checked:
    vc = _embed([c["text"] for c in checked])
    vb = _embed(base_texts) if base_texts else None
    kept = []
    for i, c in enumerate(checked):
        sb = float((vb @ vc[i]).max()) if vb is not None else 0.0
        sk = float((np.array(kept) @ vc[i]).max()) if kept else 0.0
        sc = max(sb, sk)
        if sc >= THR:
            report.append("DROP %.3f %s (%s)" % (sc, c["title"], "history/bank" if sb >= sk else "new candidate"))
            continue
        accepted.append({"q": c["q"], "title": c["title"]})
        kept.append(vc[i])
        report.append("KEEP %.3f %s" % (sc, c["title"]))
        if len(accepted) >= MAXADD:
            break
bank2 = bank + accepted
json.dump(bank2, open("data/topics.json", "w"), indent=2, ensure_ascii=False)
open("data/refill_report.txt", "w").write(chr(10).join(report) + chr(10) + "candidates %d, resolved with a real story %d, added %d, bank size %d" % (len(cands), len(checked), len(accepted), len(bank2)) + chr(10))
print(chr(10).join(report))
