import base64, json, os, re, sys, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from semdup import _embed
import numpy as np
UA = {"User-Agent": "story-sourcer-refill/1.0 (github.com/Brunotet)"}
cands = json.loads(base64.b64decode(os.environ["CANDS_B64"]).decode())
history = [h.strip() for h in os.environ.get("HISTORY", "").split(",") if h.strip()]
remove = set(r.strip().lower() for r in os.environ.get("REMOVE", "").split("|") if r.strip())
bank = [b for b in json.load(open("data/topics.json")) if b["title"].lower() not in remove]
THR = float(os.environ.get("THRESHOLD", "0.62"))
MAXADD = int(os.environ.get("MAXADD", "120"))
MARKERS = ["advertis", "campaign", "marketing", "marketed", "slogan", "brand", "promot", "profit", "sold", "sales", "company", "invented", "popular", "tradition", "myth", "claim", "lobby", "consumer", "customers", "founded"]
report = []
if remove:
    report.append("removed from bank: " + ", ".join(sorted(remove)))
def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20))
def wiki(title):
    try:
        d = get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"), safe=""))
    except Exception:
        return None
    if d.get("type") != "standard":
        return None
    ex = d.get("extract", "")
    if len(ex) < 350:
        return None
    return d.get("title", title), ex
def words(s):
    return set(re.findall(r"[a-z0-9]+", s.lower()))
def resolve(title):
    w = wiki(title)
    if w:
        return w
    try:
        data = get("https://en.wikipedia.org/w/rest.php/v1/search/title?q=" + urllib.parse.quote(title) + "&limit=3")
        for p in data.get("pages", []):
            cand = p.get("title", "")
            a, b = words(title), words(cand)
            if cand and a and len(a & b) / len(a | b) >= 0.5:
                w = wiki(cand)
                if w:
                    return w
    except Exception:
        pass
    return None
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
    w = resolve(t)
    if not w:
        report.append("NO-WIKI " + t)
        continue
    canon, ex = w
    if canon.lower() in existing:
        report.append("SKIP-DUP-CANON " + canon)
        continue
    low = ex.lower()
    hits = [m for m in MARKERS if m in low]
    if len(hits) < 2:
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
