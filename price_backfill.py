"""
price_backfill.py -- purani filings ka price data + key numbers backtest ke liye Google Sheet ke
"Backtest" tab mein bharta hai.

Ye LIVE BOT SE ALAG script hai. Bot ki files ko touch nahi karti, sirf unke filter/label/PDF/Gemini
functions import karke use karti hai. Apna alag BSE session folder use karti hai
(bse_bot_data/backfill/), live bot ke session se takrati nahi.

Kya kya bharta hai: Order Win, Dividend, Buyback, Rights Issue, Bonus Issue aur Financial Results
(sirf asli results, board-meeting intimation nahi) -- Day 0..7 prices, key numbers (amount, per-share,
ratio, record date), Market Cap (filing se pehle ke aakhri close pe) aur results ke YoY/QoQ growth.

Teen step (server pe, cd /root/bse_bot):

  1) SCAN: pichle N din ki BSE filings laao, bot ke filter se guzaaro, list save karo
        nohup python3 price_backfill.py scan 30 > bf_scan.log 2>&1 &

  2) ENRICH: har filing ki PDF padho, Gemini se Hinglish summary + key numbers (amount, per-share,
     ratio, record date) nikaalo. Dheere chalta hai (~1 ghanta+), beech mein ruk jaye toh dobara
     chalao, wahin se aage badhega.
        nohup python3 price_backfill.py enrich > bf_enrich.log 2>&1 &

  3) RUN: BSE bhavcopy se Day 0..Day 7 prices + Market Cap nikalo aur "Backtest" tab mein sab likho
        nohup python3 price_backfill.py run > bf_run.log 2>&1 &
     Default mein sirf wahi rows jaati hain jinke Day 7 tak ke prices aa chuke hain. Baaki rows
     (haal ki filings) Day 7 ke baad dobara 'run' chalane pe judengi. Abhi sab kuch chahiye ho toh:
        python3 price_backfill.py run all

Dobara chalane se duplicate rows nahi banti (har row ki apni ID hai, sheet same ID skip karta hai).
"""

import base64
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import bse_telegram_bot as bot   # sirf filter/label/PDF/Gemini functions ke liye (main() nahi chalta)
import results_analysis as ra    # Financial Results ka YoY/QoQ growth
import price_tracker as pt       # calendar, bhavcopy, plan_days, sheet _post
import filing_details as fd      # key numbers + summary extractor

BF_DIR = bot.DATA_DIR / "backfill"
BF_DIR.mkdir(exist_ok=True)
(BF_DIR / "ann").mkdir(exist_ok=True)

# Live tracker ke folders se alag, taaki 6:30 PM ke live update se file clash na ho
pt.TRACKER_BSE_DIR = BF_DIR / "bse"
pt.BHAV_TMP_DIR = BF_DIR / "bse" / "bhav"

FILINGS_FILE = BF_DIR / "backfill_filings.json"
BHAV_DB = BF_DIR / "bhav_cache.sqlite"            # bhavcopy ka disk cache (RAM bachane ke liye)
INFLIGHT_FILE = BF_DIR / "inflight.txt"     # jo filing abhi process ho rahi hai (process mare toh pata chale kaun si)
SHEET_TAB = "Backtest"
CHUNK = 100                      # ek baar mein sheet ko itni rows bhejna
ENRICH_KEYS = ["summary_ai", "amount_cr", "per_share", "shares_ratio", "record_date", "enriched",
               "skip", "pdf_tries", "buyback_mode", "poison", "skip_reason", "retry_vision", "vision_done", "via_vision", "res_label", "yoy_rev", "yoy_profit", "qoq_rev", "qoq_profit"]
MCAP_FILE = BF_DIR / "mcap_now.json"   # {scrip: aaj ka market cap (Rs Cr)} -- run step mein bharta hai
# Results PDF mein ye words hone chahiye, warna wo intimation/cover-letter hai (Gemini mein bhejna bekaar)
RESULT_TEXT_HINTS = ("revenue from operations", "total income", "profit", "net sales")
INTIMATION_WORDS = ("intimation of board meeting", "prior intimation", "date of board meeting",
                    "notice of board meeting", "to consider and approve", "trading window",
                    "intimation of meeting", "meeting of the board of directors will be held")
# Board-meeting ki intimation / schedule jaisi cheezein -- ye asli results nahi (jab tak "outcome" na ho)
INTIMATION_WORDS2 = ("intimation", "board meeting", "to consider", "will be held", "scheduled", "schedule of",
                     "trading window", "newspaper", "publication", "prior intimation", "notice of")
RESULT_MARKERS = ("quarter ended", "year ended", "half year", "half-year", "nine months", "audited",
                  "financial results", "financial result", "results for")
GEMINI_GAP_SECONDS = 3           # har filing ke baad ruko (live bot ki Gemini quota ka khayal)
MAX_CONSECUTIVE_FAILS = 5        # itni baar lagataar Gemini fail ho toh ruk jao (quota khatam)


def p(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Gemini keys ki smart rotation (sirf enrich ke liye; live bot ka code nahi chhua)
#  - jis key ko 429 aaye wo side mein: din ki limit => 2 ghante, per-minute limit => ~70 sec
#  - baaki bachi keys mein se baari-baari se kaam (har baar #1 se shuru nahi)
#  - "kaun si key kab tak side mein" ek file mein save (key ka sirf chhota hash), service restart ho tab bhi yaad rahe
# --------------------------------------------------------------------------
KEY_COOLDOWN_FILE = BF_DIR / "key_cooldown.json"
COOLDOWN_DAILY = 7200      # din ki limit lagi => itni der baad dobara test karo
COOLDOWN_MINUTE = 70       # per-minute limit
COOLDOWN_AUTH = 21600      # 401/403
_rr = {"next": 0}


def _kid(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _load_cool() -> dict:
    try:
        with open(KEY_COOLDOWN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cool(cool: dict) -> None:
    try:
        with open(KEY_COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(cool, f)
    except OSError:
        pass


def _smart_gemini_generate(prompt: str, pdf_b64: str = None, want_json: bool = True) -> str:
    keys = bot.GEMINI_API_KEYS
    if not keys:
        return ""
    n = len(keys)
    parts = [{"text": prompt}]
    if pdf_b64:      # PDF seedha Gemini ko (scanned / image wali tables ke liye)
        parts = [{"inlineData": {"mimeType": "application/pdf", "data": pdf_b64}}, {"text": prompt}]
    gen_cfg = {"temperature": 0}
    if want_json:
        gen_cfg["responseMimeType"] = "application/json"
    payload = {"contents": [{"parts": parts}], "generationConfig": gen_cfg}
    cool = _load_cool()

    def available():
        t = time.time()
        return [i for i in range(n) if cool.get(_kid(keys[i]), 0) <= t]

    avail = available()
    if not avail:                       # saari keys side mein: sabse jaldi wali ka thoda intezaar, phir ek baar dekho
        wait = min(cool.get(_kid(k), 0) for k in keys) - time.time()
        time.sleep(max(1.0, min(wait, 45.0)))
        avail = available()
        if not avail:
            return ""
    order = sorted(avail, key=lambda i: (i - _rr["next"]) % n)

    for i in order:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{bot.GEMINI_MODEL}:generateContent?key={keys[i]}")
        try:
            resp = requests.post(url, json=payload, timeout=bot.GEMINI_TIMEOUT_SECONDS * (4 if pdf_b64 else 2))
        except requests.RequestException as e:      # e ko log nahi karte (URL mein key hoti hai)
            bot.logger.warning(f"Gemini request fail (key #{i + 1}): {type(e).__name__}")
            continue
        code = resp.status_code
        if code == 200:
            try:
                parts = (resp.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                text = " ".join(pp.get("text", "") for pp in parts).strip()
            except ValueError:
                text = ""
            if text:
                _rr["next"] = (i + 1) % n
                return text
            continue
        if code == 429:
            daily = "perday" in resp.text.lower().replace(" ", "").replace("_", "")
            secs = COOLDOWN_DAILY if daily else COOLDOWN_MINUTE
            cool[_kid(keys[i])] = time.time() + secs
            _save_cool(cool)
            bot.logger.warning(f"Gemini key #{i + 1}: {'din ki' if daily else 'per-minute'} limit (429), "
                               f"{int(secs / 60)} min side mein.")
        elif code in (401, 403):
            cool[_kid(keys[i])] = time.time() + COOLDOWN_AUTH
            _save_cool(cool)
            bot.logger.warning(f"Gemini key #{i + 1} invalid/unauthorized ({code}), side mein.")
        else:
            bot.logger.warning(f"Gemini key #{i + 1} status {code}, agli key try kar raha hoon.")
    return ""


def _load_rows() -> list:
    if not FILINGS_FILE.exists():
        return []
    with open(FILINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_rows(rows: list) -> None:
    tmp = FILINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    tmp.replace(FILINGS_FILE)


# --------------------------------------------------------------------------
# Step 1: SCAN
# --------------------------------------------------------------------------

def _fetch_day(client, day: datetime) -> list:
    """Ek din ki saari announcements (saari pages). Page fail ho toh 3 baar retry."""
    items, page, total = [], 1, None
    while True:
        res = None
        for attempt in range(1, 4):
            try:
                res = client.announcements(page_no=page, from_date=day, to_date=day)
                break
            except Exception as e:  # noqa: BLE001
                p(f"    retry {attempt}/3 (page {page}): {type(e).__name__}: {e}")
                time.sleep(8)
        if res is None:
            raise RuntimeError(f"page {page} 3 baar fail hua")
        if total is None:
            try:
                total = int(res["Table1"][0]["ROWCNT"])
            except (KeyError, IndexError, ValueError, TypeError):
                total = 0
        page_items = res.get("Table", []) or []
        items.extend(page_items)
        if total == 0 or len(items) >= total or not page_items:
            break
        page += 1
        time.sleep(0.5)
    return items

def _is_real_results_text(text: str) -> bool:
    """True sirf agar text asli results filing jaisa lage (intimation/schedule nahi)."""
    t = (text or "").lower()
    if "result" not in t:
        return False
    if "outcome" in t:
        return True
    if any(w in t for w in INTIMATION_WORDS) or any(w in t for w in INTIMATION_WORDS2):
        return False
    return any(w in t for w in RESULT_MARKERS)


def cmd_prune(results_days=None) -> None:
    """
    Rescan ke bina list chhoti karta hai:
      1) Financial Results ki intimation-type filings hataata hai
      2) Ek company ki 20 din ke andar ki results filings (Standalone + Consolidated) mein se ek rakhta hai
         (Consolidated ko tarjeeh)
      3) results_days diya ho toh sirf itne pichle dinon ki results rakhta hai (baaki labels ko nahi chhedta)
    """
    from collections import Counter, defaultdict
    rows = _load_rows()
    backup = FILINGS_FILE.with_name("backfill_filings_FULL_backup.json")
    if not backup.exists():          # pehli prune se pehle poori list ka backup (dobara scan na karna pade)
        backup.write_text(FILINGS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        p(f"Backup bana liya: {backup}")
    cutoff = None
    if results_days:
        cutoff = (datetime.now() - timedelta(days=int(results_days))).strftime("%Y-%m-%d")

    def txt(r):
        return f"{r.get('headline') or ''} {r.get('subject') or ''} {r.get('summary') or ''}"

    others, res = [], []
    d_intim = d_old = 0
    for r in rows:
        if r.get("label") != pt.RESULTS_LABEL:
            others.append(r)
        elif not _is_real_results_text(txt(r)):
            d_intim += 1
        elif cutoff and r.get("filing_date", "") < cutoff:
            d_old += 1
        else:
            res.append(r)

    by_scrip = defaultdict(list)
    for r in res:
        by_scrip[r["scrip"]].append(r)
    kept_res, d_dup = [], 0
    for scrip, lst in by_scrip.items():
        lst.sort(key=lambda r: (r["filing_date"], r["filing_time"]))
        cluster = []
        def flush():
            nonlocal d_dup
            if cluster:
                best = next((x for x in cluster if "consolidated" in txt(x).lower()), cluster[0])
                kept_res.append(best)
                d_dup += len(cluster) - 1
        for r in lst:
            if cluster and (datetime.strptime(r["filing_date"], "%Y-%m-%d")
                            - datetime.strptime(cluster[0]["filing_date"], "%Y-%m-%d")).days > 20:
                flush()
                cluster = []
            cluster.append(r)
        flush()

    keep = sorted(others + kept_res, key=lambda r: (r["filing_date"], r["filing_time"]))
    _save_rows(keep)
    c = Counter(r.get("label") for r in keep)
    p(f"PRUNE: intimation {d_intim} + purani results {d_old} + duplicate (standalone/consolidated) {d_dup} hata di.")
    p(f"Ab total {len(keep)}")
    for k, v in c.most_common():
        p(f"  {k}: {v}")


def _pick(item: dict) -> dict | None:
    """Bot ke live flow jaisa hi filter: is_important -> label -> TRACK_LABELS (+ Dividend sirf declared)."""
    if not bot.is_important(item):
        return None
    label = bot.classify_filing_label(item) or ""
    if label == pt.RESULTS_LABEL:
        text = f"{item.get('HEADLINE') or ''} {item.get('NEWSSUB') or ''} {item.get('MORE') or ''}"
        if not _is_real_results_text(text):
            return None    # board meeting ki intimation / schedule -- asli results nahi
    elif label not in pt.TRACK_LABELS:
        return None
    if label == "Dividend" and not bot.is_dividend_declared_filing(item):
        return None
    scrip = bot._dividend_scrip_code(item)
    if not scrip or scrip == "-":
        return None
    dt_raw = str(item.get("NEWS_DT") or item.get("DT_TM") or item.get("News_submission_dt") or "")
    m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})", dt_raw)
    if not m:
        return None
    return {
        "id": "bf" + bot.make_announcement_id(item)[:10],
        "company": bot._dividend_company_name(item),
        "scrip": str(scrip),
        "label": label,
        "filing_date": m.group(1),
        "filing_time": f"{m.group(2)}:{m.group(3)}",
        "summary": bot.short_summary(item),
        "headline": str(item.get("HEADLINE") or item.get("Headline") or "").strip(),
        "subject": str(item.get("NEWSSUB") or item.get("MORE") or "").strip(),
        "attachment": str(item.get("ATTACHMENTNAME") or item.get("Attachment") or "").strip(),
    }


def cmd_scan(days: int) -> None:
    from bse import BSE

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days)
    p(f"SCAN shuru: {start:%Y-%m-%d} se {today:%Y-%m-%d} tak ({days} din)")

    old = {r["id"]: r for r in _load_rows()}      # pehle ka enrich kiya hua data bacha ke rakho
    found: dict = {}
    failed_days = []
    with BSE(str(BF_DIR / "ann")) as client:
        d = start
        while d <= today:
            try:
                items = _fetch_day(client, d)
            except Exception as e:  # noqa: BLE001
                p(f"  {d:%Y-%m-%d}: FAIL ({e}) -- skip")
                failed_days.append(f"{d:%Y-%m-%d}")
                d += timedelta(days=1)
                continue
            n_before = len(found)
            for it in items:
                row = _pick(it)
                if row:
                    prev = old.get(row["id"], {})
                    for k in ENRICH_KEYS:
                        if k in prev:
                            row[k] = prev[k]
                    found[row["id"]] = row
            p(f"  {d:%Y-%m-%d}: BSE se {len(items)} filings, in mein se tracking layak {len(found) - n_before}")
            _save_rows(sorted(found.values(), key=lambda r: (r["filing_date"], r["filing_time"])))
            d += timedelta(days=1)
            time.sleep(1)

    p("\n===== SCAN POORA =====")
    by_label: dict = {}
    for r in found.values():
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
    p(f"Total tracking-layak filings: {len(found)}")
    for k, v in sorted(by_label.items(), key=lambda kv: -kv[1]):
        p(f"  {k}: {v}")
    if failed_days:
        p(f"Ye din fail hue (dobara scan chalane pe try honge): {', '.join(failed_days)}")
    p(f"List save hui: {FILINGS_FILE}")
    p("Ab agla step: python3 price_backfill.py enrich")


# --------------------------------------------------------------------------
# Step 2: ENRICH (PDF + Gemini)
# --------------------------------------------------------------------------

VISION_PROMPT = (
    "This PDF is a BSE filing of an Indian listed company. Find the financial results statement (Statement of "
    "Profit and Loss / Unaudited or Audited Financial Results). Transcribe ONLY that table as plain text, one line per "
    "row, keeping the column headings exactly (for example 'Quarter ended 30.06.2026 | Quarter ended 31.03.2026 | "
    "Quarter ended 30.06.2025 | Year ended 31.03.2026') and the numbers of each row: Revenue from operations, Other "
    "income, Total income, Total expenses, Profit/(Loss) before tax, Net Profit/(Loss) for the period, Earnings per share. "
    "If both Standalone and Consolidated tables exist, transcribe BOTH and label them STANDALONE and CONSOLIDATED. "
    "Write the unit (Rs lakhs / Rs crore / Rs million) at the top. Do not calculate or guess anything. "
    "If the PDF has no financial results table, reply exactly: NO_RESULTS_TABLE"
)
VISION_MAX_BYTES = 9_000_000


def _download_pdf_bytes(attachment: str):
    """PDF ke raw bytes (pehle AttachLive, phir AttachHis). None agar na mile ya bahut bada ho."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0.0.0 Safari/537.36",
               "Referer": "https://www.bseindia.com/corporates/ann.html", "Accept": "application/pdf,*/*"}
    for template in bot.BSE_PDF_URL_TEMPLATES:
        try:
            resp = requests.get(template.format(attachment), headers=headers, timeout=60)
        except requests.RequestException:
            continue
        if resp.status_code == 200 and resp.content[:4] == b"%PDF":
            return resp.content if len(resp.content) <= VISION_MAX_BYTES else None
    return None


def _analyze_results_text(r: dict, text: str):
    """(done, ok): done=False => Gemini ne jawab nahi diya (baad mein dobara); ok=True => numbers mil gaye."""
    gemini_empty = {"v": False}

    def llm(prompt: str) -> str:
        out = bot.gemini_generate(prompt)
        if not out:
            gemini_empty["v"] = True
        return out

    metrics: dict = {}
    msg = ra.build_results_analysis(company=r["company"], scrip=r["scrip"], pdf_text=text,
                                    llm_call=llm, metrics_out=metrics)
    if gemini_empty["v"]:
        return False, False
    if msg and metrics.get("label"):
        r["res_label"] = metrics["label"]
        for k in ("yoy_rev", "yoy_profit", "qoq_rev", "qoq_profit"):
            r[k] = metrics.get(k)
        r["summary_ai"] = bot._results_tracker_summary(metrics)
        return True, True
    return True, False


def _finish_skip(r: dict, reason: str) -> bool:
    r["skip"], r["enriched"], r["skip_reason"] = True, True, reason
    return True


def _enrich_results(r: dict) -> bool:
    """
    Financial Results: PDF text -> Gemini -> YoY/QoQ growth. Text se na ho toh PDF seedha Gemini ko dikhate hain
    (scanned / image wali tables). True = ho gaya (ya asli results nahi nikle, skip mark), False = Gemini se jawab
    nahi aaya (baad mein dobara).
    """
    if not r.get("attachment"):
        return _finish_skip(r, "no_attachment")

    # 1) pdfplumber ka text (sasta). retry_vision wali filings seedha vision pe jaati hain.
    if not r.get("retry_vision") and not r.get("vision_done"):
        text = ""
        try:
            text = bot.download_pdf_text_by_attachment(
                r["attachment"], max_chars=bot.RESULTS_PDF_MAX_CHARS, max_pages=bot.RESULTS_PDF_MAX_PAGES) or ""
        except Exception as e:  # noqa: BLE001
            p(f"    PDF error: {type(e).__name__}")
        if text and any(k in text.lower() for k in RESULT_TEXT_HINTS):
            done, ok = _analyze_results_text(r, text)
            if not done:
                return False
            if ok:
                r["enriched"] = True
                return True
        # text nahi mila / results ke words nahi / numbers nahi nikle -> vision try karenge

    # 2) vision: PDF seedha Gemini ko
    data = _download_pdf_bytes(r["attachment"])
    if not data:
        return _finish_skip(r, "pdf_nahi_mili_ya_bahut_badi")
    out = bot.gemini_generate(VISION_PROMPT, pdf_b64=base64.b64encode(data).decode("ascii"), want_json=False)
    if not out:
        return False                      # quota / error: baad mein dobara
    r["vision_done"] = True
    if "NO_RESULTS_TABLE" in out[:60] or len(out) < 80:
        return _finish_skip(r, "pdf_mein_results_table_nahi")
    done, ok = _analyze_results_text(r, out)
    if not done:
        return False
    if ok:
        r["enriched"] = True
        r["via_vision"] = True
        return True
    return _finish_skip(r, "numbers_nahi_nikle_vision_ke_baad")


def _cmd_enrich_inner() -> None:
    if not bot.GEMINI_API_KEYS:
        p("Gemini key .env mein nahi mili -- ruk gaya.")
        return
    rows = _load_rows()
    if not rows:
        p("Pehle scan chalao: python3 price_backfill.py scan 30")
        return
    todo = [r for r in rows if not r.get("enriched")]
    p(f"ENRICH shuru: {len(todo)} filings baaki hain (total {len(rows)})")
    if not todo:
        p("Sab filings enrich ho chuki hain. Ab agla step: python3 price_backfill.py run")
        sys.exit(3)

    # Pichli baar process kisi filing pe mara tha (aksar bahut bada PDF -> memory khatam)? Use PDF ke bina karenge.
    try:
        last_id = INFLIGHT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        last_id = ""
    if last_id:
        for r in rows:
            if r["id"] == last_id and not r.get("enriched"):
                r["poison"] = True
                p(f"  !! Pichli baar is filing pe process mara tha: {r['company'][:30]} ({r['label']}). Ise PDF ke bina karenge.")
                _save_rows(rows)
                break

    fails_in_row = 0
    done = 0
    for i, r in enumerate(todo, start=1):
        try:
            INFLIGHT_FILE.write_text(r["id"], encoding="utf-8")
        except OSError:
            pass
        if r.get("poison"):
            # PDF ke bina: results ko skip, baaki ko sirf headline/subject se
            if r["label"] == pt.RESULTS_LABEL:
                r["skip"] = True
                r["enriched"] = True
                p(f"  [{i}/{len(todo)}] Financial Results | {r['company'][:28]}: SKIP (PDF se process mar jata hai)")
                if i % 10 == 0:
                    _save_rows(rows)
                continue
        if r["label"] == pt.RESULTS_LABEL:
            ok = False
            for attempt in range(1, 4):
                ok = _enrich_results(r)
                if ok:
                    break
                time.sleep(20)
            if ok:
                fails_in_row = 0
                done += 1
                state = "SKIP (asli results nahi)" if r.get("skip") else (
                    f"{r.get('res_label')}: YoY rev={r.get('yoy_rev')} profit={r.get('yoy_profit')} "
                    f"| QoQ rev={r.get('qoq_rev')} profit={r.get('qoq_profit')}")
                p(f"  [{i}/{len(todo)}] Financial Results | {r['company'][:28]}: {state}")
            else:
                fails_in_row += 1
                p(f"  [{i}/{len(todo)}] Financial Results | {r['company'][:28]}: Gemini se jawab nahi aaya (baad mein dobara)")
                if fails_in_row >= MAX_CONSECUTIVE_FAILS:
                    p(f"\n{MAX_CONSECUTIVE_FAILS} baar lagataar fail -- Gemini ki free limit lagi lagti hai. "
                      "Ruk raha hoon. Kuch ghante baad (ya kal) 'enrich' dobara chalao, wahin se aage badhega.")
                    _save_rows(rows)
                    return
            if i % 10 == 0:
                _save_rows(rows)
            time.sleep(GEMINI_GAP_SECONDS)
            continue

        text = ""
        if r.get("attachment") and not r.get("poison"):
            try:
                text = bot.download_pdf_text_by_attachment(r["attachment"]) or ""
            except Exception as e:  # noqa: BLE001
                p(f"    PDF error: {type(e).__name__}")
        res = None
        for attempt in range(1, 4):
            res = fd.extract(r["label"], r.get("headline", ""), r.get("subject", ""), text,
                             llm_call=bot.gemini_generate, want_summary=True, fallback=bot)
            if res["ok"]:
                break
            time.sleep(20)
        if res and res["ok"]:
            fails_in_row = 0
            r["summary_ai"] = res["summary"]
            r["amount_cr"] = res["amount_cr"]
            r["per_share"] = res["per_share"]
            r["shares_ratio"] = res["shares_ratio"]
            r["record_date"] = res["record_date"]
            r["buyback_mode"] = res.get("buyback_mode")
            r["enriched"] = True
            done += 1
            p(f"  [{i}/{len(todo)}] {r['label']} | {r['company'][:28]}: amt={res['amount_cr']} "
              f"per_share={res['per_share']} ratio/shares={res['shares_ratio']} rec={res['record_date']} mode={res.get('buyback_mode')}")
        else:
            fails_in_row += 1
            p(f"  [{i}/{len(todo)}] {r['label']} | {r['company'][:28]}: Gemini se jawab nahi aaya (baad mein dobara)")
            if fails_in_row >= MAX_CONSECUTIVE_FAILS:
                p(f"\n{MAX_CONSECUTIVE_FAILS} baar lagataar fail -- Gemini ki free limit lagi lagti hai. "
                  "Ruk raha hoon. Kuch ghante baad (ya kal) 'enrich' dobara chalao, wahin se aage badhega.")
                _save_rows(rows)
                return
        if i % 10 == 0:
            _save_rows(rows)
        time.sleep(GEMINI_GAP_SECONDS)

    _save_rows(rows)
    try:
        INFLIGHT_FILE.write_text("", encoding="utf-8")
    except OSError:
        pass
    left = sum(1 for r in rows if not r.get("enriched"))
    p("\n===== ENRICH POORA =====")
    p(f"Is baar {done} filings ho gayi. Abhi bhi baaki: {left}")
    if left:
        p("Baaki ke liye 'enrich' dobara chalao.")
    else:
        p("Ab agla step: python3 price_backfill.py run")
        sys.exit(3)


def cmd_enrich() -> None:
    """Enrich chalata hai. Normal band hone par (poora / Gemini limit / 2-ghante restart) inflight saaf ho jata hai;
    sirf tab bachta hai jab process zabardasti (memory se) maara jaye -- tab agli baar wo filing PDF ke bina hoti hai."""
    import signal

    def _term(signum, frame):  # systemd ka restart / stop
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _term)
    bot.gemini_generate = _smart_gemini_generate     # sirf is process mein; live bot ka code waisa hi
    try:
        _cmd_enrich_inner()
    finally:
        try:
            INFLIGHT_FILE.write_text("", encoding="utf-8")
        except OSError:
            pass


# --------------------------------------------------------------------------
# Step 3: RUN
# --------------------------------------------------------------------------

def _load_mcap() -> dict:
    if MCAP_FILE.exists():
        try:
            with open(MCAP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def _fetch_mcap_now(client, scrip: str):
    """BSE listSecurities se aaj ka market cap (Rs Cr). Return: (float ya None, pakka_jawab_mila: bool)."""
    try:
        data = client.listSecurities(scripcode=str(scrip), group="")
    except Exception as e:  # noqa: BLE001
        p(f"    market cap fetch error ({scrip}): {type(e).__name__}")
        return None, False
    if not data:
        return None, True
    entry = data[0] if isinstance(data, list) else data
    if not isinstance(data, (list, dict)) or not isinstance(entry, dict):
        return None, True
    val = (entry.get("Mktcap") or entry.get("MKTCAP") or entry.get("mktcap")
           or entry.get("MktCap") or entry.get("Mkt_Cap") or entry.get("MARKETCAP"))
    try:
        return (float(str(val).replace(",", "")) if val else None), True
    except ValueError:
        return None, True


def _ref_day(calendar: list, fdate: str, ftime: str):
    """Filing se PEHLE ka aakhri trading close: market band hone ke baad aayi toh usi din ka, warna pichle trading din ka."""
    mins = None
    try:
        hh, mm = str(ftime).split(":")
        mins = int(hh) * 60 + int(mm)
    except ValueError:
        pass
    if fdate in calendar and mins is not None and mins > pt.MARKET_CLOSE_MIN:
        return fdate
    prev = [d for d in calendar if d < fdate]
    return prev[-1] if prev else None


def _market_cap_at(scrip: str, ref: str, mcap_now: dict, got_all: dict, latest_days: list,
                   calendar=None, prev_all=None):
    """
    Us din ka market cap = aaj ka market cap x (us din ka close x split_factor / aaj (latest) ka close).
    split_factor: ref ke baad bonus/split hua ho toh us din ka share count kam tha, uska adjustment
    (BSE ke PrvsClsgPric se pakda jata hai). Prev close na mile toh factor 1 (purana tareeka).
    """
    now = mcap_now.get(scrip)
    if not now or not ref:
        return None
    c_ref = (got_all.get(ref, {}).get(scrip) or [None] * 4)[3]
    c_now, d_now = None, None
    for d in reversed(latest_days):
        v = (got_all.get(d, {}).get(scrip) or [None] * 4)[3]
        if v:
            c_now, d_now = v, d
            break
    if not c_ref or not c_now:
        return None
    factor = 1.0
    if calendar and prev_all:
        factor = _split_factor(scrip, ref, d_now, calendar, got_all, prev_all)
    return round(now * c_ref * factor / c_now, 2)


def _download_day_x(client, date_str: str, scrips: set):
    """
    pt._download_day jaisa hi, par saath mein PrvsClsgPric (pichla close, BSE ise bonus/split pe adjust karta hai)
    aur TtlTradgVol (volume) bhi nikalta hai. Return: (ohlc, prev, vol) ya None agar file na mili.
    Live tracker ka pt._download_day waisa hi rehta hai, usko chheda nahi.
    """
    path = None
    try:
        path = client.bhavcopyReport(datetime.strptime(date_str, "%Y-%m-%d"), str(pt.BHAV_TMP_DIR))
    except Exception as e:  # noqa: BLE001
        p(f"    {date_str} bhavcopy error: {type(e).__name__}")
        return None
    try:
        ohlc, prev, vol = {}, {}, {}
        with open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            for row in csv.DictReader(f):
                code = (row.get("FinInstrmId") or "").strip()
                if code not in scrips:
                    continue
                try:
                    ohlc[code] = [round(float(row["OpnPric"]), 2), round(float(row["HghPric"]), 2),
                                  round(float(row["LwPric"]), 2), round(float(row["ClsPric"]), 2)]
                except (KeyError, ValueError, TypeError):
                    continue
                try:
                    prev[code] = float(row["PrvsClsgPric"])
                except (KeyError, ValueError, TypeError):
                    pass
                try:
                    vol[code] = float(row["TtlTradgVol"])
                except (KeyError, ValueError, TypeError):
                    pass
        return ohlc, prev, vol
    except Exception as e:  # noqa: BLE001
        p(f"    {date_str} bhavcopy padh nahi paya: {type(e).__name__}")
        return None
    finally:
        try:
            Path(path).unlink()
        except Exception:  # noqa: BLE001
            pass


SPLIT_TOLERANCE = 0.02   # pichla close aur kal ka close 2% se zyada alag ho toh corporate action (bonus/split) maano


def _split_factor(scrip: str, ref: str, until: str, calendar: list, got_all: dict, prev_all: dict) -> float:
    """
    ref din ke baad se `until` din tak ke beech bonus/split/rights jaise events ka combined factor.
    BSE har din ka PrvsClsgPric corporate action pe adjust karta hai. Toh agar kisi din ka PrvsClsgPric
    pichle asli close se alag ho, toh ratio = prev / pichla close. Sab ratios ka product = factor.
    Misaal: 1:1 bonus -> prev = close/2 -> factor 0.5 -> us din ka share count aaj ka aadha tha.
    """
    last_close = (got_all.get(ref, {}).get(scrip) or [None] * 4)[3]
    if not last_close:
        return 1.0
    factor = 1.0
    for d in calendar:
        if d <= ref or d > until:
            continue
        pv = prev_all.get(d, {}).get(scrip)
        if pv and last_close and abs(pv / last_close - 1) > SPLIT_TOLERANCE:
            factor *= pv / last_close
        c = (got_all.get(d, {}).get(scrip) or [None] * 4)[3]
        if c:
            last_close = c
    return factor


VOL_WINDOW = 20          # filing se pehle ke itne trading din ka average volume
VOL_MIN_DAYS = 15        # itne din ki bhavcopy mili ho tabhi average maano
PRE_DAYS_BACK = 45       # filing se pehle itne trading din ki bhavcopy chahiye (30 calendar din ke returns ke liye)


def _prev_close(scrip: str, ref: str, calendar: list, got_all: dict):
    """Filing se pehle ka aakhri close. Ref din pe trade na hua ho toh usse pehle ke 10 din tak peeche dekhta hai."""
    if not ref or ref not in calendar:
        return None
    i = calendar.index(ref)
    for d in reversed(calendar[max(0, i - 10): i + 1]):
        v = (got_all.get(d, {}).get(scrip) or [None] * 4)[3]
        if v:
            return v
    return None


def _avg_volume(scrip: str, ref: str, calendar: list, got_all: dict, vol_all: dict):
    """Ref din tak ke 20 trading din ka average volume (jis din trade nahi hua = 0). None agar data kam."""
    if not ref or ref not in calendar:
        return None
    i = calendar.index(ref)
    days = [d for d in calendar[max(0, i - VOL_WINDOW + 1): i + 1] if d in got_all]
    if len(days) < VOL_MIN_DAYS:
        return None
    total = sum(vol_all.get(d, {}).get(scrip, 0) for d in days)
    return round(total / len(days), 0) if total > 0 else None


def _close_on_or_before(sc: str, day: str, calendar: list, got_all: dict, max_back: int = 6):
    """day par ya usse pehle ke aakhri trading din ka close (stock trade na hua ho toh max_back din tak peeche)."""
    import bisect
    i = bisect.bisect_right(calendar, day) - 1
    for j in range(i, max(-1, i - max_back), -1):
        v = (got_all.get(calendar[j], {}).get(sc) or [None] * 4)[3]
        if v:
            return v
    return None


def _pre_return(sc: str, ref: str, ref_close, calendar_days: int, calendar: list, got_all: dict):
    """Filing se pehle ka return: Prev Close vs (ref din - calendar_days) ke baad ka aakhri close."""
    if not ref or not ref_close or not calendar:
        return None
    target = (datetime.strptime(ref, "%Y-%m-%d") - timedelta(days=calendar_days)).strftime("%Y-%m-%d")
    if target < calendar[0]:
        return None
    old = _close_on_or_before(sc, target, calendar, got_all)
    return round((ref_close / old - 1) * 100, 2) if old else None


def _pre_volume_ratio(sc: str, ref: str, avg20, calendar: list, got_all: dict, vol_all: dict):
    """Filing se pehle ke 5 trading din ka average volume / 20-din ka average volume."""
    if not ref or not avg20 or ref not in calendar:
        return None
    i = calendar.index(ref)
    days = [d for d in calendar[max(0, i - 4): i + 1] if d in got_all]
    if len(days) < 3:
        return None
    avg5 = sum(vol_all.get(d, {}).get(sc, 0) for d in days) / len(days)
    return round(avg5 / avg20, 2)


def _derived(r: dict, ref, mcap, plan: list, calendar: list, got_all: dict, vol_all: dict) -> dict:
    """Naye analysis columns: dividend yield, order/buyback vs market cap, buyback premium, volume ratios."""
    sc, label = r["scrip"], r["label"]
    pc = _prev_close(sc, ref, calendar, got_all)
    avg = _avg_volume(sc, ref, calendar, got_all, vol_all)
    amt, ps = r.get("amount_cr"), r.get("per_share")
    out = {"prev_close": pc, "avg_vol": avg, "div_yield": None, "order_mcap": None,
           "bb_mcap": None, "bb_premium": None,
           "pre_1w": _pre_return(sc, ref, pc, 7, calendar, got_all),
           "pre_15d": _pre_return(sc, ref, pc, 15, calendar, got_all),
           "pre_1m": _pre_return(sc, ref, pc, 30, calendar, got_all),
           "pre_vol5": _pre_volume_ratio(sc, ref, avg, calendar, got_all, vol_all)}
    if label == "Dividend" and ps and pc:
        out["div_yield"] = round(ps / pc * 100, 2)
    if label == "Order Win" and amt and mcap:
        out["order_mcap"] = round(amt / mcap * 100, 2)
    if label == "Buyback":
        if amt and mcap:
            out["bb_mcap"] = round(amt / mcap * 100, 2)
        if ps and pc:
            out["bb_premium"] = round((ps / pc - 1) * 100, 2)
    ratios = []
    for d in plan:
        v = vol_all.get(d, {}).get(sc) if d else None
        ratios.append(round(v / avg, 2) if (v is not None and avg) else None)
    out["vol_ratios"] = ratios
    return out


def _res_rank(r: dict) -> tuple:
    return (2 if "Consolidated" in str(r.get("res_label")) else 1, r["filing_date"], r["filing_time"])


def _usable_rows(rows: list) -> list:
    """Sheet mein jaane layak rows: skip hataao, results sirf wahi jinka analysis bana, ek quarter ka ek hi (Consolidated pehle)."""
    out, best = [], {}
    for r in rows:
        if r.get("skip"):
            continue
        if r["label"] == pt.RESULTS_LABEL:
            if not r.get("res_label"):
                continue
            key = (r["scrip"], str(r["res_label"]).split(" (")[0])
            if key not in best or _res_rank(r) > _res_rank(best[key]):
                best[key] = r
            continue
        out.append(r)
    out.extend(best.values())
    return sorted(out, key=lambda r: (r["filing_date"], r["filing_time"]))


def _open_bhav_db(fresh: bool = False):
    """Har din ka (open, high, low, close, prev close, volume) disk pe -- 512 MB RAM mein sab memory mein rakhna mumkin nahi."""
    if fresh and BHAV_DB.exists():
        BHAV_DB.unlink()
    con = sqlite3.connect(str(BHAV_DB))
    con.execute("CREATE TABLE IF NOT EXISTS bhav(day TEXT, scrip TEXT, o REAL, h REAL, l REAL, c REAL, "
                "prev REAL, vol REAL, PRIMARY KEY(day, scrip))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bhav_scrip ON bhav(scrip)")
    con.execute("CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    con.commit()
    return con


def _store_day(con, day: str, ohlc: dict, prev: dict, vol: dict) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO bhav VALUES (?,?,?,?,?,?,?,?)",
        [(day, sc, v[0], v[1], v[2], v[3], prev.get(sc), vol.get(sc)) for sc, v in ohlc.items()])
    con.execute("INSERT OR REPLACE INTO days VALUES (?)", (day,))
    con.commit()


def _build_payload_rows(rows, plans, refs, con, calendar, mcap_now, latest_days, include_all):
    """
    Har scrip ke liye uska poora price-series disk se nikaal ke (chhote dicts mein) purane functions ko deta hai.
    Isse RAM kam lagti hai (pehle saare scrips x saare din memory mein the).
    Return: (payload_rows, incomplete_count)
    """
    from collections import defaultdict
    days_set = {d for (d,) in con.execute("SELECT day FROM days")}
    got_all = {d: {} for d in days_set}
    prev_all = {d: {} for d in days_set}
    vol_all = {d: {} for d in days_set}
    by_scrip = defaultdict(list)
    for r in rows:
        by_scrip[r["scrip"]].append(r)

    payload_rows, incomplete = [], 0
    for sc in sorted(by_scrip):
        series = con.execute("SELECT day,o,h,l,c,prev,vol FROM bhav WHERE scrip=?", (sc,)).fetchall()
        for d, o, h, l, c, pv, vo in series:
            if d in got_all:
                got_all[d][sc] = [o, h, l, c]
                if pv is not None:
                    prev_all[d][sc] = pv
                if vo is not None:
                    vol_all[d][sc] = vo
        for r in by_scrip[sc]:
            plan = plans[r["id"]]
            complete = plan[pt.TRACK_DAYS] is not None and all(d in got_all for d in plan if d)
            if not complete and not include_all:
                incomplete += 1
                continue
            values = [got_all.get(d, {}).get(sc) if d else None for d in plan]
            mcap = _market_cap_at(sc, refs[r["id"]], mcap_now, got_all, latest_days, calendar, prev_all)
            row = {
                "id": r["id"], "company": r["company"], "filing_date": r["filing_date"],
                "filing_time": r["filing_time"], "label": r["label"],
                "summary": r.get("summary_ai") or r["summary"],
                "scrip": sc,
                "amount_cr": r.get("amount_cr"), "per_share": r.get("per_share"),
                "shares_ratio": r.get("shares_ratio"), "record_date": r.get("record_date"),
                "buyback_mode": r.get("buyback_mode"),
                "market_cap": mcap,
                "yoy_rev": r.get("yoy_rev"), "yoy_profit": r.get("yoy_profit"),
                "qoq_rev": r.get("qoq_rev"), "qoq_profit": r.get("qoq_profit"),
                "values": pt._flatten(values),
            }
            row.update(_derived(r, refs[r["id"]], mcap, plan, calendar, got_all, vol_all))
            payload_rows.append(row)
        for d, *_ in series:
            if d in got_all:
                got_all[d].pop(sc, None)
                prev_all[d].pop(sc, None)
                vol_all[d].pop(sc, None)
    payload_rows.sort(key=lambda x: (x["filing_date"], x["filing_time"]))
    return payload_rows, incomplete


def cmd_run(include_all: bool = False, fresh: bool = False) -> None:
    if not pt.enabled():
        p("GOOGLE_SHEET_WEBHOOK_URL / GOOGLE_SHEET_SECRET .env mein nahi mile -- ruk gaya.")
        return
    # Pehle check: Google Apps Script v5 (naya layout) deploy hai ya nahi -- warna ghanton baad pata chalta
    try:
        info = requests.get(os.getenv("GOOGLE_SHEET_WEBHOOK_URL", ""), timeout=60).text
    except Exception as e:  # noqa: BLE001
        info = ""
        p(f"Sheet ki script tak pahunch nahi paya ({type(e).__name__}).")
    if "v6" not in info:
        p("STOP: Google Apps Script abhi naye (v6) layout pe deploy nahi hai.")
        p("Sheet -> Extensions -> Apps Script -> Deploy -> Manage deployments -> pencil -> New version -> Deploy.")
        p(f"(Script ka jawab: {info[:120]})")
        return
    p("Apps Script v6 theek hai.")
    all_rows = _load_rows()
    if not all_rows:
        p("Pehle scan chalao: python3 price_backfill.py scan 30")
        return
    rows = _usable_rows(all_rows)
    n_pending = sum(1 for r in rows if not r.get("enriched") and r["label"] != pt.RESULTS_LABEL)
    p(f"RUN shuru: {len(rows)} filings sheet layak (scan mein total {len(all_rows)}). "
      f"Enrich baaki (numbers khaali rahenge): {n_pending}")

    with pt._open_client() as client:
        calendar = pt._get_calendar(client)
        if not calendar:
            p("Trading calendar nahi mila -- baad mein dobara try karo.")
            return
        p(f"Trading calendar: {len(calendar)} din, aakhri din {calendar[-1]}")

        plans = {r["id"]: pt.plan_days(calendar, r["filing_date"], r["filing_time"]) for r in rows}
        refs = {r["id"]: _ref_day(calendar, r["filing_date"], r["filing_time"]) for r in rows}
        latest_days = calendar[-3:]

        # Market cap (aaj ka) -- ek baar, file mein save, dobara nahi maangega
        mcap_now = _load_mcap()
        todo = sorted({r["scrip"] for r in rows if r["scrip"] not in mcap_now})
        if todo:
            p(f"{len(todo)} scrips ka aaj ka market cap laa raha hoon...")
        for i, sc in enumerate(todo, start=1):
            val, sure = _fetch_mcap_now(client, sc)
            if sure:
                mcap_now[sc] = val
            time.sleep(1)
            if i % 25 == 0:
                MCAP_FILE.write_text(json.dumps(mcap_now), encoding="utf-8")
                p(f"  market cap: {i}/{len(todo)}")
        MCAP_FILE.write_text(json.dumps(mcap_now), encoding="utf-8")

        needed: dict = {}
        for r in rows:
            for d in plans[r["id"]] + [refs[r["id"]]]:
                if d:
                    needed.setdefault(d, set()).add(r["scrip"])
        for d in latest_days:
            needed.setdefault(d, set()).update(r["scrip"] for r in rows)
        # Bonus/split pakadne ke liye ref din se aaj tak ka har trading din chahiye
        first_ref = min((x for x in refs.values() if x), default=None)
        if first_ref:
            start_i = max(0, calendar.index(first_ref) - PRE_DAYS_BACK) if first_ref in calendar else 0
            for d in calendar[start_i:]:
                needed.setdefault(d, set()).update(r["scrip"] for r in rows)
        p(f"{len(needed)} alag trading dinon ki bhavcopy download karni hai")

        con = _open_bhav_db(fresh)
        want = sorted({r["scrip"] for r in rows})
        try:
            cached = set(json.loads((con.execute("SELECT v FROM meta WHERE k='scrips'").fetchone() or ["[]"])[0]))
        except ValueError:
            cached = set()
        if not set(want) <= cached:      # naye scrips aaye ho toh cache naya banta hai
            con.execute("DELETE FROM bhav")
            con.execute("DELETE FROM days")
            con.execute("INSERT OR REPLACE INTO meta VALUES ('scrips', ?)", (json.dumps(want),))
            con.commit()
            p("Bhavcopy cache naya banega.")
        days_done = {d for (d,) in con.execute("SELECT day FROM days")}
        if days_done:
            p(f"Cache mein {len(days_done)} din pehle se hain, wo dobara download nahi honge.")
        missing = []
        for i, d in enumerate(sorted(needed), start=1):
            if d in days_done:
                continue
            res = None
            for attempt in range(1, 3):
                time.sleep(1)
                res = _download_day_x(client, d, needed[d])
                if res is not None:
                    break
                time.sleep(5)
            if res is None:
                missing.append(d)
                p(f"  [{i}/{len(needed)}] {d}: bhavcopy NAHI mili")
            else:
                _store_day(con, d, res[0], res[1], res[2])
                p(f"  [{i}/{len(needed)}] {d}: ok ({len(res[0])} stocks ka data)")

    payload_rows, incomplete = _build_payload_rows(rows, plans, refs, con, calendar, mcap_now, latest_days, include_all)
    con.close()

    if incomplete:
        p(f"\n{incomplete} rows abhi nahi bheji (Day 7 tak ke prices abhi aaye nahi). "
          "Din poore hone ke baad 'run' dobara chalana, ye tab judengi. Abhi bhejni ho toh: run all")
    p(f"\nSheet ke '{SHEET_TAB}' tab mein likh raha hoon ({len(payload_rows)} rows, {CHUNK} ke chunk mein)...")
    done = 0
    for i in range(0, len(payload_rows), CHUNK):
        chunk = payload_rows[i:i + CHUNK]
        ok = pt._post({"action": "bulk", "tab": SHEET_TAB, "rows": chunk})
        if not ok:
            p(f"  chunk {i // CHUNK + 1} FAIL: {pt._last_post_error or 'sheet tak pahunch nahi paya'}")
            p("  Ruk gaya. Problem theek karke 'run' dobara chalao -- jo rows ja chuki hain wo dobara nahi judengi.")
            return
        done += len(chunk)
        p(f"  {done}/{len(payload_rows)} rows likh di")
        time.sleep(2)

    p("\n===== RUN POORA =====")
    if missing:
        p(f"Ye dinon ki bhavcopy nahi mili (inke cells khaali hain): {', '.join(missing)}")
    p(f"Google Sheet ka '{SHEET_TAB}' tab dekho.")


def cmd_diag_skip(n: int = 12) -> None:
    """Kuch skip hui Financial Results ko dobara chala ke batata hai ki skip ki asli wajah kya thi (kuch save nahi karta)."""
    import random
    rows = [r for r in _load_rows() if r["label"] == pt.RESULTS_LABEL and r.get("skip")]
    p(f"DIAG: {len(rows)} skip hui results mein se {n} random check kar raha hoon...")
    random.seed(5)
    bot.gemini_generate = _smart_gemini_generate
    for r in random.sample(rows, min(n, len(rows))):
        p(f"\n--- {r['company'][:30]} | {r['filing_date']} | {(r.get('headline') or r.get('subject') or '')[:70]}")
        try:
            text = bot.download_pdf_text_by_attachment(
                r["attachment"], max_chars=bot.RESULTS_PDF_MAX_CHARS, max_pages=bot.RESULTS_PDF_MAX_PAGES) or ""
        except Exception as e:  # noqa: BLE001
            p(f"  PDF error: {type(e).__name__}")
            continue
        if not text:
            p("  WAJAH: PDF text khaali (download fail / scanned image)")
            continue
        hint = any(k in text.lower() for k in RESULT_TEXT_HINTS)
        p(f"  PDF text {len(text)} chars, results-words mile: {hint}")
        if not hint:
            p("  WAJAH: PDF mein results ke words nahi. Shuru ka text: " + text[:160].replace("\n", " "))
            continue
        metrics: dict = {}
        msg = ra.build_results_analysis(company=r["company"], scrip=r["scrip"], pdf_text=text,
                                        llm_call=bot.gemini_generate, metrics_out=metrics)
        p(f"  analysis: message={'haan' if msg else 'NAHI'} | label={metrics.get('label')} | "
          f"yoy_rev={metrics.get('yoy_rev')} yoy_profit={metrics.get('yoy_profit')} "
          f"qoq_rev={metrics.get('qoq_rev')} qoq_profit={metrics.get('qoq_profit')}")
        if not (msg and metrics.get("label")):
            p("  WAJAH: PDF asli results jaisi hai par numbers nikal nahi paye (upar ki INFO/WARNING line dekho)")


def cmd_reenrich() -> None:
    """Skip hui Financial Results ko dobara enrich ke liye khol deta hai (PDF seedha Gemini ko dikhake)."""
    rows = _load_rows()
    n = 0
    for r in rows:
        if r["label"] == pt.RESULTS_LABEL and r.get("skip") and not r.get("via_vision"):
            r["enriched"], r["skip"] = False, False
            r["retry_vision"], r["vision_done"], r["pdf_tries"] = True, False, 0
            n += 1
    _save_rows(rows)
    p(f"{n} skip hui Financial Results dobara enrich ke liye khol di. Ab enrich chalao.")


def cmd_vision_test(n: int = 8) -> None:
    """N skip hui results pe vision tareeka test karta hai (kuch save nahi hota)."""
    import random
    rows = [r for r in _load_rows() if r["label"] == pt.RESULTS_LABEL and r.get("skip")]
    random.seed(11)
    bot.gemini_generate = _smart_gemini_generate
    good = 0
    for r in random.sample(rows, min(n, len(rows))):
        t = dict(r)
        t["retry_vision"], t["vision_done"], t["enriched"], t["skip"] = True, False, False, False
        t0 = time.time()
        res = _enrich_results(t)
        status = ("OK numbers mile" if t.get("enriched") and not t.get("skip") else
                  f"skip: {t.get('skip_reason')}" if t.get("skip") else "Gemini jawab nahi (baad mein)")
        if t.get("enriched") and not t.get("skip"):
            good += 1
        p(f"{r['company'][:28]:28} | {r['filing_date']} | {status} | yoy_rev={t.get('yoy_rev')} "
          f"yoy_profit={t.get('yoy_profit')} qoq_rev={t.get('qoq_rev')} qoq_profit={t.get('qoq_profit')} | {int(time.time() - t0)}s")
    p(f"\nTEST: {good}/{n} mein numbers mil gaye.")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "scan":
        cmd_scan(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    elif cmd == "reenrich":
        cmd_reenrich()
    elif cmd == "visiontest":
        cmd_vision_test(int(sys.argv[2]) if len(sys.argv) > 2 else 8)
    elif cmd == "diagskip":
        cmd_diag_skip(int(sys.argv[2]) if len(sys.argv) > 2 else 12)
    elif cmd == "prune":
        cmd_prune(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif cmd == "enrich":
        cmd_enrich()
    elif cmd == "run":
        cmd_run(include_all=("all" in sys.argv[2:]), fresh=("fresh" in sys.argv[2:]))
    else:
        p("Use:\n  python3 price_backfill.py scan 30\n  python3 price_backfill.py enrich\n  python3 price_backfill.py run\n  python3 price_backfill.py run all")


if __name__ == "__main__":
    main()
