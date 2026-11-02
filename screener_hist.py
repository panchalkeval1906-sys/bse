# -*- coding: utf-8 -*-
"""
screener_hist.py -- Financial Results ke liye "pichle saal / trend / margin / P/E" ke naye columns.
LIVE BOT KO NAHI CHHUATA: alag file hai, results_analysis.py ya price_backfill.py mein kuch nahi badalta.
Sirf screener.in ke quarterly table padhta hai (ek company ka ek page = saare quarters), aur CSV likhta hai.

Input  : hist_input.csv   (columns: id, scrip, quarter_label, variant, market_cap)
Output : hist_output.csv  (har id ke naye columns)
Cache  : screener_cache.json (jo company ho chuki wo dobara nahi maangta; beech mein ruke toh wahin se aage)

Commands (server pe, /root/bse_bot mein):
    python3 screener_hist.py test      # pehli 5 companies, kuch save nahi hota, sirf print
    python3 screener_hist.py run       # poora (resume safe)
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

INP = Path("hist_input.csv")
OUT = Path("hist_output.csv")
CACHE = Path("screener_cache.json")
BASE = "https://www.screener.in/company"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}
SLEEP = 1.3
CAP = 200.0   # ek quarter ki growth ko +-200% pe cap (low-base blowup se average na bigde)

# Screener ke row-label -> hamara field
WANTED = {"sales": "revenue", "revenue": "revenue", "net profit": "net_profit", "eps in rs": "eps",
          "opm %": "opm", "other income": "other_income", "profit before tax": "pbt"}

OUT_COLS = ["id", "hist_status",
            "ly_yoy_rev", "ly_yoy_profit", "ly_qoq_rev", "ly_qoq_profit",
            "cur_yoy_rev", "cur_yoy_profit", "cur_qoq_rev", "cur_qoq_profit",
            "d_yoy_rev", "d_yoy_profit", "d_qoq_rev", "d_qoq_profit",
            "avg4_yoy_rev", "avg4_yoy_profit", "acc_rev", "acc_profit",
            "opm_cur", "opm_ly", "opm_chg", "pe_pre", "oi_pbt"]


# ------------------------------------------------------------------ helpers
def to_float(s):
    s = (s or "").replace(",", "").replace("%", "").replace("\u00a0", " ").strip()
    if not s or s in ("-", "\u2014"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def month_end_shift(d: date, months_back: int) -> date:
    import calendar
    y, m = d.year, d.month - months_back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, calendar.monthrange(y, m)[1])


def period_end_from_label(label: str):
    """'Q1 FY27' -> 2026-06-30 (Indian FY: Q1=Jun, Q2=Sep, Q3=Dec, Q4=Mar)."""
    m = re.match(r"^Q([1-4])\s*FY(\d\d)$", (label or "").strip())
    if not m:
        return None
    q, fy = int(m.group(1)), 2000 + int(m.group(2))
    month = {1: 6, 2: 9, 3: 12, 4: 3}[q]
    year = fy if q == 4 else fy - 1
    return month_end_shift(date(year, month, 1), 0)


def rev_g(cur, base):
    if cur is None or base is None or base <= 0 or cur < 0:
        return None
    return round((cur - base) / base * 100, 1)


def np_g(cur, base):
    """Number (base>0) ya text Turnaround / Loss narrowed / Loss widened (base loss), warna None."""
    if cur is None or base is None:
        return None
    if base > 0:
        return round((cur - base) / base * 100, 1)
    if base < 0:
        if cur >= 0:
            return "Turnaround"
        return "Loss narrowed" if cur > base else ("Loss widened" if cur < base else None)
    return None


def isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def cap(x):
    return max(-CAP, min(CAP, x))


# ------------------------------------------------------------------ Screener fetch/parse
def parse_screener_html(html: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    sec = soup.find(id="quarters")
    if sec is None:
        return None
    sub = sec.find("p", class_="sub")
    sub_text = sub.get_text(" ", strip=True).lower() if sub else ""
    variant = ("consolidated" if "consolidated figures" in sub_text
               else "standalone" if "standalone figures" in sub_text else "unmarked")
    table = sec.find("table")
    if table is None or table.find("thead") is None or table.find("tbody") is None:
        return None
    keys = [th.get("data-date-key") for th in table.find("thead").find_all("th")][1:]
    quarters = {k: {} for k in keys if k}
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).replace("+", "").replace("\u00a0", " ")
        label = re.sub(r"\s+", " ", label).strip().lower()
        field = WANTED.get(label)
        if not field:
            continue
        for key, td in zip(keys, cells[1:]):
            if key:
                v = to_float(td.get_text(" ", strip=True))
                if v is not None:
                    quarters[key].setdefault(field, v)
    quarters = {k: v for k, v in quarters.items() if v}
    return {"variant": variant, "quarters": quarters} if quarters else None


class Blocked(Exception):
    pass


def fetch_variant(scrip: str, want: str):
    """(quarters_dict | None, status_text). Transient error pe Exception (cache nahi hoga)."""
    urls = [f"{BASE}/{scrip}/consolidated/", f"{BASE}/{scrip}/"] if want == "consolidated" else [f"{BASE}/{scrip}/"]
    seen_page = False
    for url in urls:
        for attempt in range(3):
            time.sleep(SLEEP)
            try:
                resp = requests.get(url, headers=HEADERS, timeout=25)
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise RuntimeError("network: %s" % type(e).__name__)
                time.sleep(5)
                continue
            if resp.status_code == 429:
                time.sleep(60 * (attempt + 1))
                if attempt == 2:
                    raise Blocked("429")
                continue
            if resp.status_code in (403,):
                raise Blocked("403")
            if resp.status_code >= 500:
                time.sleep(10)
                if attempt == 2:
                    raise RuntimeError("5xx")
                continue
            break
        if resp.status_code == 404:
            continue
        if resp.status_code != 200:
            continue
        seen_page = True
        parsed = parse_screener_html(resp.text)
        if not parsed:
            continue
        v = parsed["variant"]
        if v == want or (v == "unmarked" and want == "standalone"):
            return parsed["quarters"], "ok"
    return None, ("variant match nahi (ya quarterly table nahi)" if seen_page else "Screener page nahi mila")


# ------------------------------------------------------------------ features
def features(quarters: dict, period_end: date, market_cap):
    def q(k):  # k quarters peeche
        return quarters.get(month_end_shift(period_end, 3 * k).isoformat()) or {}

    def val(k, f):
        return q(k).get(f)

    res = {c: "" for c in OUT_COLS if c not in ("id", "hist_status")}

    # pichle saal ka same quarter (t-4): uski YoY (vs t-8) aur QoQ (vs t-5)
    ly_yoy_rev = rev_g(val(4, "revenue"), val(8, "revenue"))
    ly_yoy_np = np_g(val(4, "net_profit"), val(8, "net_profit"))
    ly_qoq_rev = rev_g(val(4, "revenue"), val(5, "revenue"))
    ly_qoq_np = np_g(val(4, "net_profit"), val(5, "net_profit"))
    # abhi ka quarter (t0): Screener se (agar wahan aa chuka ho)
    have_cur = bool(q(0))
    cur_yoy_rev = rev_g(val(0, "revenue"), val(4, "revenue")) if have_cur else None
    cur_yoy_np = np_g(val(0, "net_profit"), val(4, "net_profit")) if have_cur else None
    cur_qoq_rev = rev_g(val(0, "revenue"), val(1, "revenue")) if have_cur else None
    cur_qoq_np = np_g(val(0, "net_profit"), val(1, "net_profit")) if have_cur else None

    def put(name, v):
        res[name] = "" if v is None else v

    for n, v in [("ly_yoy_rev", ly_yoy_rev), ("ly_yoy_profit", ly_yoy_np), ("ly_qoq_rev", ly_qoq_rev),
                 ("ly_qoq_profit", ly_qoq_np), ("cur_yoy_rev", cur_yoy_rev), ("cur_yoy_profit", cur_yoy_np),
                 ("cur_qoq_rev", cur_qoq_rev), ("cur_qoq_profit", cur_qoq_np)]:
        put(n, v)

    def diff(a, b):
        return round(a - b, 1) if isnum(a) and isnum(b) else None
    put("d_yoy_rev", diff(cur_yoy_rev, ly_yoy_rev))
    put("d_yoy_profit", diff(cur_yoy_np, ly_yoy_np))
    put("d_qoq_rev", diff(cur_qoq_rev, ly_qoq_rev))
    put("d_qoq_profit", diff(cur_qoq_np, ly_qoq_np))

    # pichle 4 quarters (t-1..t-4) ki YoY growth ka average
    def avg4(kind):
        vals = []
        for k in (1, 2, 3, 4):
            if kind == "rev":
                g = rev_g(val(k, "revenue"), val(k + 4, "revenue"))
            else:
                g = np_g(val(k, "net_profit"), val(k + 4, "net_profit"))
            if isnum(g):
                vals.append(cap(g))
        return round(sum(vals) / len(vals), 1) if len(vals) >= 3 else None
    a_rev, a_np = avg4("rev"), avg4("np")
    put("avg4_yoy_rev", a_rev)
    put("avg4_yoy_profit", a_np)
    put("acc_rev", round(cap(cur_yoy_rev) - a_rev, 1) if isnum(cur_yoy_rev) and a_rev is not None else None)
    put("acc_profit", round(cap(cur_yoy_np) - a_np, 1) if isnum(cur_yoy_np) and a_np is not None else None)

    # margin
    o0, o4 = val(0, "opm"), val(4, "opm")
    put("opm_cur", o0)
    put("opm_ly", o4)
    put("opm_chg", round(o0 - o4, 1) if o0 is not None and o4 is not None else None)

    # filing se pehle ka P/E = market cap (filing se pehle ka) / pichle 4 quarters ka net profit
    pe = None
    nps = [val(k, "net_profit") for k in (1, 2, 3, 4)]
    try:
        mc = float(market_cap) if market_cap not in ("", None) else None
    except ValueError:
        mc = None
    if mc and all(x is not None for x in nps) and sum(nps) > 0:
        pe = round(mc / sum(nps), 1)
    put("pe_pre", pe)

    # earnings quality: other income / PBT (%)
    oi, pbt = val(0, "other_income"), val(0, "pbt")
    put("oi_pbt", round(oi / pbt * 100, 1) if oi is not None and pbt is not None and pbt > 0 else None)

    if not have_cur:
        status = "abhi ka quarter Screener mein nahi (diff/acceleration khaali)"
    elif res["ly_yoy_rev"] == "" and res["ly_yoy_profit"] == "":
        status = "history chhoti (pichle saal ki growth nahi bani)"
    else:
        status = "ok"
    return status, res


# ------------------------------------------------------------------ main
def load_input():
    return list(csv.DictReader(open(INP, encoding="utf-8")))


def load_cache():
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(c):
    CACHE.write_text(json.dumps(c), encoding="utf-8")


def cmd_test(n=5):
    rows = load_input()
    seen, picked = set(), []
    for r in rows:
        k = (r["scrip"], r["variant"])
        if k in seen:
            continue
        seen.add(k)
        picked.append(r)
        if len(picked) >= n:
            break
    for r in picked:
        pe_date = period_end_from_label(r["quarter_label"])
        print("\n---", r["scrip"], r["quarter_label"], r["variant"], "mcap", r["market_cap"])
        try:
            qs, st = fetch_variant(r["scrip"], r["variant"])
        except Exception as e:  # noqa: BLE001
            print("  fetch error:", e)
            continue
        if not qs:
            print("  Screener:", st)
            continue
        print("  Screener quarters:", len(qs), "| naye se purane:", sorted(qs)[-1], "...", sorted(qs)[0])
        status, res = features(qs, pe_date, r["market_cap"])
        print("  status:", status)
        for k, v in res.items():
            print("   %-16s %s" % (k, v))


def cmd_run():
    rows = load_input()
    cache = load_cache()
    keys = sorted({(r["scrip"], r["variant"]) for r in rows})
    todo = [k for k in keys if "%s|%s" % k not in cache]
    print("Companies:", len(keys), "| cache mein pehle se:", len(keys) - len(todo), "| ab laani:", len(todo))
    t0 = time.time()
    try:
        for i, (sc, var) in enumerate(todo, start=1):
            try:
                qs, st = fetch_variant(sc, var)
            except Blocked as e:
                print("Screener ne rok diya (%s). Cache save kar diya; 1-2 ghante baad dobara chalao." % e)
                save_cache(cache)
                break
            except Exception as e:  # noqa: BLE001
                print("  %s: temporary error (%s), baad mein dobara try hoga" % (sc, e))
                continue
            cache["%s|%s" % (sc, var)] = {"q": qs, "st": st}
            if i % 25 == 0:
                save_cache(cache)
                el = time.time() - t0
                print("  %d/%d (%.0f min, baaki ~%.0f min)" % (i, len(todo), el / 60, el / i * (len(todo) - i) / 60))
        else:
            pass
    finally:
        save_cache(cache)

    out, counts = [], {}
    for r in rows:
        ce = cache.get("%s|%s" % (r["scrip"], r["variant"]))
        base = {c: "" for c in OUT_COLS}
        base["id"] = r["id"]
        pe_date = period_end_from_label(r["quarter_label"])
        if ce is None:
            base["hist_status"] = "abhi fetch nahi hua"
        elif not ce["q"]:
            base["hist_status"] = ce["st"]
        elif pe_date is None:
            base["hist_status"] = "quarter label samajh nahi aaya"
        else:
            status, res = features(ce["q"], pe_date, r["market_cap"])
            base.update(res)
            base["hist_status"] = status
        counts[base["hist_status"]] = counts.get(base["hist_status"], 0) + 1
        out.append(base)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(out)
    print("\nOutput:", OUT, "| rows:", len(out))
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print("  %5d  %s" % (v, k))


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else ""
    if c == "test":
        cmd_test(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    elif c == "run":
        cmd_run()
    else:
        print(__doc__)
