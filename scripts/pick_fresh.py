import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from semdup import _embed
path, exclude, limit, thr = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
recs = json.load(open(path))
history = []
seen = set()
for h in exclude.split(","):
    h = h.strip()
    if h and h.lower() not in seen:
        seen.add(h.lower())
        history.append(h)
fresh, lines = [], []
if recs and history:
    texts = [(r.get("name", "") + ". " + r.get("question", "") + " " + r.get("summary", "")[:350]) for r in recs]
    vecs = _embed(texts + history)
    cand, hist = vecs[:len(recs)], vecs[len(recs):]
    sims = cand @ hist.T
    for i, r in enumerate(recs):
        j = int(sims[i].argmax())
        sc = float(sims[i][j])
        ok = sc < thr
        lines.append("%s %.3f %s ~ %s" % ("KEEP" if ok else "DROP", sc, r.get("name"), history[j]))
        if ok:
            fresh.append(r)
else:
    fresh = recs
    lines.append("no history or no candidates")
json.dump(fresh[:limit], open(path, "w"), indent=2)
os.makedirs("data", exist_ok=True)
open("data/semdup_report.txt", "w").write(chr(10).join(lines) + chr(10) + "kept %d of %d (limit %d, threshold %s)" % (len(fresh), len(recs), limit, thr) + chr(10))
print(chr(10).join(lines))
