import base64, json, os, sys, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from semdup import _embed
import numpy as np
cands = json.loads(base64.b64decode(os.environ["CANDS_B64"]).decode())
history = [h.strip() for h in os.environ.get("HISTORY", "").split(",") if h.strip()]
bank = json.load(open("data/topics.json"))
THR = float(os.environ.get("THRESHOLD", "0.62"))
MAXADD = int(os.environ.get("MAXADD", "120"))
report = []
def wiki(title):
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"), safe="")
    req = urllib.request.Request(url, headers={"User-Agent": "story-sourcer-refill/1.0 (github.com/Brunotet)"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception:
        return None
    if d.get("type") != "standard":
        return None
    ex = d.get("extract", "")
    if len(ex) < 350:
        return None
    return d.get("title", title), ex
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
    w = wiki(t)
    if not w:
        report.append("NO-WIKI " + t)
        continue
    canon, ex = w
    if canon.lower() in existing:
        report.append("SKIP-DUP-CANON " + canon)
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
open("data/refill_report.txt", "w").write(chr(10).join(report) + chr(10) + "candidates %d, verified on Wikipedia %d, added %d, bank size %d" % (len(cands), len(checked), len(accepted), len(bank2)) + chr(10))
print(chr(10).join(report))
