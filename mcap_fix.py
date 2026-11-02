# mcap_fix.py -- READ-ONLY. Kuch bhi server ki file/Sheet mein nahi badalta; sirf naya CSV banata hai.
# Kaam: jin rows mein Market Cap khaali hai unka market cap dobara nikalna (illiquid SME stocks ke liye relaxed lookback).
# Purani wajah: market cap ke liye filing-din ka close EXACT us din chahiye tha aur aaj ka close pichle 3 din mein.
# Illiquid stock un dinon trade nahi hota, isliye khaali reh jaata tha. Ab pichle dinon tak peeche dekhta hai (Prev Close jaise).
# Chalao (server, /root/bse_bot mein):  python3 mcap_fix.py
import csv
import json
import sqlite3
import sys
import price_backfill as pb

INP = "mcap_fix_input.csv"
OUT = "mcap_fixed.csv"
REF_BACK = 150     # filing-din ke close ke liye itne trading din peeche tak dekho
REF_FWD = 150      # peeche na mile toh itne trading din AAGE tak (approx)
NEAR = 10          # itne din ke andar ka close "ok" maana jayega, usse door ka "approx"
NOW_BACK = 400     # aaj ke close ke liye pura calendar (suspended stock ka aakhri traded close bhi chalega)

con = sqlite3.connect("file:%s?mode=ro" % str(pb.BHAV_DB), uri=True)
calendar = sorted(d for (d,) in con.execute("SELECT day FROM days"))
mcap_now = pb._load_mcap()
print("calendar:", len(calendar), "din,", calendar[0], "se", calendar[-1], "| mcap_now scrips:", len(mcap_now))

rows = list(csv.DictReader(open(INP, encoding="utf-8")))
by_scrip = {}
for r in rows:
    by_scrip.setdefault(r["scrip"], []).append(r)

out = []
for sc, rs in by_scrip.items():
    got_all, prev_all = {}, {}
    for d, o, h, l, c, pv in con.execute("SELECT day,o,h,l,c,prev FROM bhav WHERE scrip=?", (sc,)):
        got_all[d] = {sc: [o, h, l, c]}
        if pv is not None:
            prev_all[d] = {sc: pv}
    now = mcap_now.get(sc)
    # aaj ka (sabse naya) close
    d_now = c_now = None
    for d in reversed(calendar[-NOW_BACK:]):
        v = (got_all.get(d, {}).get(sc) or [None] * 4)[3]
        if v:
            d_now, c_now = d, v
            break
    for r in rs:
        res = {"id": r["id"], "scrip": sc, "label": r["label"], "existing_mcap": r["existing_mcap"],
               "new_mcap": "", "order_mcap": "", "gap_days": "", "method": ""}
        try:
            ref = pb._ref_day(calendar, r["filing_date"], r["filing_time"])
            if not now:
                res["method"] = "BSE ne market cap nahi diya (fix nahi ho sakta)"
            elif not ref or ref not in calendar:
                res["method"] = "ref din calendar mein nahi"
            elif not c_now:
                res["method"] = "pichle %d din mein koi trade nahi" % NOW_BACK
            else:
                i = calendar.index(ref)
                d_ref = c_ref = None
                gap = 0
                for k, d in enumerate(reversed(calendar[max(0, i - REF_BACK): i + 1])):
                    v = (got_all.get(d, {}).get(sc) or [None] * 4)[3]
                    if v:
                        d_ref, c_ref, gap = d, v, -k
                        break
                if not c_ref:                     # peeche kuch nahi mila -> aage ka pehla close
                    for k, d in enumerate(calendar[i + 1: i + 1 + REF_FWD], start=1):
                        v = (got_all.get(d, {}).get(sc) or [None] * 4)[3]
                        if v:
                            d_ref, c_ref, gap = d, v, k
                            break
                if not c_ref:
                    res["method"] = "is scrip ka koi close nahi mila (na peeche na aage)"
                else:
                    f = pb._split_factor(sc, d_ref, d_now, calendar, got_all, prev_all)
                    m = round(now * c_ref * f / c_now, 2)
                    res["new_mcap"] = m
                    res["gap_days"] = gap
                    if abs(gap) <= NEAR:
                        res["method"] = "ok (factor %.4f)" % f
                    elif gap < 0:
                        res["method"] = "approx: %d trading din PEECHE ka close (factor %.4f)" % (-gap, f)
                    else:
                        res["method"] = "approx: %d trading din AAGE ka close (factor %.4f)" % (gap, f)
                    try:
                        amt = float(r["amount_cr"].replace(",", "")) if r["amount_cr"] else None
                    except ValueError:
                        amt = None
                    if r["label"] == "Order Win" and amt and m:
                        res["order_mcap"] = round(amt / m * 100, 2)
        except Exception as e:  # noqa: BLE001
            res["method"] = "error: %s" % type(e).__name__
        out.append(res)

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["id", "scrip", "label", "existing_mcap", "new_mcap", "order_mcap", "gap_days", "method"])
    w.writeheader()
    w.writerows(out)

blank = [x for x in out if not x["existing_mcap"]]
ok_blank = [x for x in blank if x["new_mcap"] != ""]
print("khaali rows:", len(blank), "| ab mil gaye:", len(ok_blank))
chk = [x for x in out if x["existing_mcap"] and x["new_mcap"] != ""]
if chk:
    diffs = sorted(abs(float(x["new_mcap"]) / float(x["existing_mcap"].replace(",", "")) - 1) * 100 for x in chk)
    print("VALIDATION (purani rows jinka market cap pehle se hai): %d rows, median farak %.2f%%, 90th pct %.2f%%, max %.2f%%"
          % (len(chk), diffs[len(diffs) // 2], diffs[int(len(diffs) * 0.9)], diffs[-1]))
from collections import Counter
cnt = Counter(("ok" if x["method"].startswith("ok") else "approx peeche" if "PEECHE" in x["method"] else "approx aage" if "AAGE" in x["method"] else x["method"]) for x in blank)
print("khaali rows ka hisaab:")
for k, v in cnt.most_common():
    print("  %5d  %s" % (v, k))
print("Output file:", OUT)
