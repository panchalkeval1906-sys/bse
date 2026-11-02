"""
price_tracker.py -- Filing aane ke baad stock ka price movement Google Sheet mein track karta hai.

Ye bse_telegram_bot.py se ALAG file hai. Bot ki taraf se sirf 3 chhote hook judte hain
(import, filing bhejne ke baad track_filing(), aur main loop mein maybe_run_daily_update()).
Is file mein kuch bhi fail ho -- bot ka normal kaam (alerts) kabhi nahi rukta.

Kaise kaam karta hai:
1. Jab koi filing (Order Win / Buyback / Dividend declared / Rights Issue / Bonus Issue)
   Telegram pe jaati hai, uski ek row Google Sheet mein ban jaati hai
   (Company, Filing Date, Filing Time, Filing Type, Summary, Scrip Code).
2. Roz shaam (market band hone ke baad) BSE ki apni daily "bhavcopy" file se
   har tracked stock ka Open/High/Low/Close nikalke sheet ki wahi row update hoti hai.
   Trading days ka calendar Reliance ke price-history se banta hai (holiday/weekend apne aap skip).
3. Day 0 = filing wala din (SIRF tab jab filing market chalu hone ke time aayi ho).
   Day 1..Day 7 = uske baad ke 7 asli trading days.

Test commands (server pe):
    python3 price_tracker.py test 532380                    -> BSE se pichle kuch din ka price
    python3 price_tracker.py test 532380 2026-10-08 11:30   -> us filing ke liye Day0..Day7 mapping
    python3 price_tracker.py sheettest                      -> Google Sheet se connection check
    python3 price_tracker.py runnow                         -> abhi daily update chalao (shaam ka wait na karo)
"""

import contextlib
import csv
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger("bse_bot")  # bot ka hi logger -- logs wahi bot.log mein dikhenge

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

SHEET_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
SHEET_SECRET = os.getenv("GOOGLE_SHEET_SECRET", "").strip()

# Kaun si filings track hongi (classify_filing_label ke naam). Naya category
# jodna ho toh bas yahan naam add kar do (jaise "Stock Split", "Merger/Acquisition").
TRACK_LABELS = {"Order Win", "Buyback", "Dividend", "Rights Issue", "Bonus Issue"}
# Financial Results alag rakha hai (TRACK_LABELS mein nahi) taaki purana backfill script na badle.
# Bot sirf unhi results ko bhejta hai jinka YoY/QoQ analysis ban gaya ho (board-meeting intimation nahi).
RESULTS_LABEL = "Financial Results"
# Sheet ke Key Details / Market Cap / Growth columns ke liye extra fields
DETAIL_FIELDS = ("amount_cr", "per_share", "shares_ratio", "record_date", "market_cap",
                 "yoy_rev", "yoy_profit", "qoq_rev", "qoq_profit")

# "Dividend" label sirf tab track hoga jab dividend DECLARE hua ho (board meeting
# wali intimation nahi) -- bot ki existing is_dividend_declared_filing() se decide hota hai.

TRACK_DAYS = 7                       # Day 1 se Day 7 tak (Day 0 alag, sirf market-hours filing ke liye)
# Daily update ke time slots (IST). Pehla slot BSE ki bhavcopy aane ke baad; agar tab tak file
# publish nahi hui thi, toh doosra slot dobara koshish karta hai.
UPDATE_SLOTS_IST = [(18, 30), (21, 30)]
MARKET_OPEN_MIN = 9 * 60 + 15        # 9:15
MARKET_CLOSE_MIN = 15 * 60 + 30      # 3:30 PM
GIVE_UP_AFTER_DAYS = 30              # itne din baad bhi 7 din poore na hon toh tracking band
KEEP_DONE_ROWS_DAYS = 45             # poori ho chuki tracking local file mein itne din rakhni

IST = timezone(timedelta(hours=5, minutes=30))

DATA_DIR = Path(__file__).resolve().parent / "bse_bot_data"
DATA_DIR.mkdir(exist_ok=True)
TRACK_FILE = DATA_DIR / "price_tracking.json"
LAST_RUN_FILE = DATA_DIR / "price_tracking_last_run.json"
CACHE_FILE = DATA_DIR / "price_cache.json"          # kaun se din ka price pehle hi nikal chuke hain
TRACKER_BSE_DIR = DATA_DIR / "tracker_bse"          # tracker ke BSE client ki apni folder (bot ke session se alag)
BHAV_TMP_DIR = DATA_DIR / "tracker_bse" / "bhav"    # bhavcopy CSV yahan aati hai, parse ke baad delete ho jaati hai
CACHE_KEEP_DAYS = 45

# Ye teen stocks har trading din trade hote hain -- inki price-history ke dates se
# "market kab khula tha" ka calendar banta hai (Reliance, TCS, HDFC Bank).
CALENDAR_SCRIPS = ["500325", "532540", "500180"]

_lock = threading.Lock()        # TRACK_FILE ko padhne/likhne ke liye (chhota kaam)
_sync_lock = threading.Lock()   # sheet/BSE wala lamba kaam ek waqt pe ek hi thread kare
_daily_thread: threading.Thread | None = None

def enabled() -> bool:
    return bool(SHEET_URL and SHEET_SECRET)


# --------------------------------------------------------------------------
# Local tracking file
# --------------------------------------------------------------------------

def _load() -> list:
    with _lock:
        if TRACK_FILE.exists():
            try:
                with open(TRACK_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("price_tracking.json padh nahi paya -- khaali list se shuru kar raha hoon.")
        return []


def _save(rows: list) -> None:
    with _lock:
        tmp = TRACK_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        tmp.replace(TRACK_FILE)


def _append_if_new(new_row: dict, cutoff: str) -> bool:
    """Naya row add karta hai (agar same id pehle se nahi hai). Poora load-modify-save ek hi lock ke andar."""
    with _lock:
        rows = []
        if TRACK_FILE.exists():
            try:
                with open(TRACK_FILE, "r", encoding="utf-8") as f:
                    rows = json.load(f)
            except (json.JSONDecodeError, OSError):
                rows = []
        if any(r.get("id") == new_row["id"] for r in rows):
            return False
        rows = [r for r in rows if not (r.get("done") and r.get("filing_date", "9999") < cutoff)]
        rows.append(new_row)
        tmp = TRACK_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        tmp.replace(TRACK_FILE)
        return True


def _patch(row_id: str, **fields) -> None:
    """Sirf ek row ke fields badalta hai (naya file load karke) -- taaki dusre thread ka add kiya hua row na kho jaye."""
    with _lock:
        rows = []
        if TRACK_FILE.exists():
            try:
                with open(TRACK_FILE, "r", encoding="utf-8") as f:
                    rows = json.load(f)
            except (json.JSONDecodeError, OSError):
                rows = []
        for r in rows:
            if r.get("id") == row_id:
                r.update(fields)
        tmp = TRACK_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        tmp.replace(TRACK_FILE)


# --------------------------------------------------------------------------
# Filing date/time
# --------------------------------------------------------------------------

def _parse_filing_dt(dt_raw: str) -> tuple[str, str | None]:
    """
    BSE ka NEWS_DT aisa aata hai: "2026-10-10T16:22:16.817". Isme se (YYYY-MM-DD, "HH:MM") nikalta hai.
    Samajh na aaye toh abhi ka IST time use karta hai (bot filing aate hi process karta hai, toh lagbhag sahi rehta hai).
    """
    m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})", dt_raw or "")
    if m:
        return m.group(1), f"{m.group(2)}:{m.group(3)}"
    now = datetime.now(IST)
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M")


# --------------------------------------------------------------------------
# Price source: BSE ki apni data (bse library) -- calendar + daily bhavcopy
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _open_client():
    from bse import BSE  # lazy import
    TRACKER_BSE_DIR.mkdir(exist_ok=True)
    BHAV_TMP_DIR.mkdir(exist_ok=True)
    with BSE(str(TRACKER_BSE_DIR)) as client:
        yield client


def _get_calendar(client) -> list | None:
    """
    Trading days ki sorted list ["YYYY-MM-DD", ...] -- Reliance/TCS/HDFC Bank ki 12-mahine ki
    price-history ke dates se (ye har market din trade hote hain, toh weekend/holiday khud hi bahar).
    """
    for code in CALENDAR_SCRIPS:
        try:
            data = client.equityPriceVolumeT12M(code)
            dates = []
            for row in data["Data"]["data"]:
                try:
                    dates.append(datetime.strptime(row[0], "%a %b %d %Y %H:%M:%S").strftime("%Y-%m-%d"))
                except (ValueError, TypeError, IndexError):
                    continue
            if dates:
                return sorted(set(dates))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Price tracker: trading calendar ({code}) nahi mila: {type(e).__name__}: {e}")
    return None


def _download_day(client, date_str: str, scrips: set) -> dict | None:
    """
    Us din ki BSE bhavcopy download karke sirf maange hue scrips ka [Open, High, Low, Close] nikalta hai.
    Return: {scrip: [o,h,l,c]} (jo scrip us din trade nahi hua wo isme nahi hoga) -- ya None agar file hi na mili
    (holiday, ya abhi publish nahi hui, ya network issue).
    """
    path = None
    try:
        path = client.bhavcopyReport(datetime.strptime(date_str, "%Y-%m-%d"), str(BHAV_TMP_DIR))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Price tracker: {date_str} ki bhavcopy nahi mili: {type(e).__name__}: {e}")
        return None
    try:
        out = {}
        with open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            for row in csv.DictReader(f):
                code = (row.get("FinInstrmId") or "").strip()
                if code in scrips:
                    try:
                        out[code] = [round(float(row["OpnPric"]), 2), round(float(row["HghPric"]), 2),
                                     round(float(row["LwPric"]), 2), round(float(row["ClsPric"]), 2)]
                    except (KeyError, ValueError, TypeError):
                        continue
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Price tracker: {date_str} ki bhavcopy padh nahi paya: {type(e).__name__}: {e}")
        return None
    finally:
        try:
            Path(path).unlink()
        except Exception:  # noqa: BLE001
            pass


def plan_days(calendar: list, fdate: str, ftime: str | None) -> list:
    """
    Return: 8 items [Day0, Day1, ..., Day7] -- har item ek trading date ("YYYY-MM-DD") ya None.
    - Day 0: filing wala din, SIRF agar filing market chalu hone ke time (9:15-3:30) aayi ho
      aur us din market khula tha.
    - Filing market khulne se pehle (pre-market) aayi ho: wahi din poora filing ke baad ka
      pehla session hai, isliye wo Day 1 banta hai.
    - Filing market band hone ke baad / weekend / holiday: Day 1 = agla trading day.
    """
    plan = [None] * (TRACK_DAYS + 1)
    after = [d for d in calendar if d > fdate]
    mins = None
    if ftime:
        try:
            hh, mm = ftime.split(":")
            mins = int(hh) * 60 + int(mm)
        except ValueError:
            mins = None
    if fdate in calendar and mins is not None:
        if MARKET_OPEN_MIN <= mins <= MARKET_CLOSE_MIN:
            plan[0] = fdate
        elif mins < MARKET_OPEN_MIN:
            after.insert(0, fdate)
    for i, d in enumerate(after[:TRACK_DAYS], start=1):
        plan[i] = d
    return plan


def _flatten(values: list) -> list:
    """8 items (har ek [o,h,l,c] ya None) -> sheet ke 32 cells."""
    flat = []
    for v in values:
        flat.extend(list(v) if v else ["", "", "", ""])
    return flat


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"days": {}}


def _save_cache(cache: dict) -> None:
    cutoff = (datetime.now(IST) - timedelta(days=CACHE_KEEP_DAYS)).strftime("%Y-%m-%d")
    cache["days"] = {d: v for d, v in cache.get("days", {}).items() if d >= cutoff}
    tmp = CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    tmp.replace(CACHE_FILE)


# --------------------------------------------------------------------------
# Google Sheet (Apps Script web app)
# --------------------------------------------------------------------------

_last_post_error = ""


def _post(payload: dict) -> bool:
    global _last_post_error
    _last_post_error = ""
    try:
        payload = dict(payload, secret=SHEET_SECRET)
        resp = requests.post(
            SHEET_URL, data=json.dumps(payload),
            headers={"Content-Type": "text/plain;charset=utf-8"}, timeout=40,
        )
        try:
            body = resp.json()
        except ValueError:
            logger.warning(f"Price tracker: Google Sheet se ajeeb jawab aaya (status {resp.status_code}) -- URL/deploy check karo.")
            return False
        if body.get("ok"):
            return True
        _last_post_error = str(body.get("error") or "")
        logger.warning(f"Price tracker: Google Sheet ne mana kiya: {_last_post_error}")
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Price tracker: Google Sheet tak pahunch nahi paya: {type(e).__name__}: {e}")
        return False


def _sync_pending() -> None:
    """Jo rows abhi tak sheet mein add nahi hui (internet/Sheet issue se), unhe add karne ki koshish."""
    for r in _load():
        if r.get("synced"):
            continue
        ok = _post({
            "action": "add", "id": r["id"], "company": r["company"],
            "filing_date": r["filing_date"], "filing_time": r.get("filing_time") or "",
            "label": r["label"], "summary": r["summary"], "scrip": r["scrip"],
            **{k: r.get(k) for k in DETAIL_FIELDS},
        })
        if ok:
            _patch(r["id"], synced=True)
            logger.info(f"Price tracker: sheet mein row add hui: {r['company']} ({r['label']})")


def _sync_in_background() -> None:
    def _work():
        try:
            with _sync_lock:
                _sync_pending()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Price tracker: background sync fail: {type(e).__name__}: {e}")
    threading.Thread(target=_work, daemon=True).start()


# --------------------------------------------------------------------------
# Public hooks (bot inhe call karta hai)
# --------------------------------------------------------------------------

def should_track(label: str, dividend_declared: bool) -> bool:
    if label == RESULTS_LABEL:
        return True
    if label not in TRACK_LABELS:
        return False
    if label == "Dividend" and not dividend_declared:
        return False
    return True


def track_filing(company: str, scrip: str, label: str, dt_raw: str, summary: str,
                 dividend_declared: bool = False, extras: dict | None = None) -> bool:
    """Telegram pe bheji ja chuki filing ko tracking mein daalta hai. Kabhi exception nahi uthata.
    extras: amount_cr, per_share, shares_ratio, record_date, market_cap, yoy_rev, yoy_profit, qoq_rev, qoq_profit."""
    try:
        if not enabled() or not should_track(label, dividend_declared):
            return False
        if not scrip or scrip == "-":
            return False
        fdate, ftime = _parse_filing_dt(dt_raw)
        tid = hashlib.sha1(f"{scrip}|{label}|{fdate}|{ftime}|{summary[:80]}".encode("utf-8")).hexdigest()[:12]

        cutoff = (datetime.now(IST) - timedelta(days=KEEP_DONE_ROWS_DAYS)).strftime("%Y-%m-%d")
        new_row = {
            "id": tid, "scrip": str(scrip), "company": company, "label": label,
            "filing_date": fdate, "filing_time": ftime, "summary": summary,
            "synced": False, "done": False, "pushed": None,
        }
        for k in DETAIL_FIELDS:
            v = (extras or {}).get(k)
            if v is not None:
                new_row[k] = v
        if not _append_if_new(new_row, cutoff):
            return False
        logger.info(f"Price tracker: tracking shuru: {company} ({label}), filing {fdate} {ftime}")
        _sync_in_background()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Price tracker: track_filing fail ({company}): {type(e).__name__}: {e}")
        return False


def run_daily_update() -> None:
    """Saari active trackings ke liye BSE bhavcopy se OHLC laake sheet mein update karta hai."""
    with _sync_lock:
        _sync_pending()
        active = [r for r in _load() if not r.get("done") and r.get("synced")]
        if not active:
            logger.info("Price tracker: daily update -- koi active tracking nahi.")
            return
        today = datetime.now(IST).strftime("%Y-%m-%d")
        updated = 0
        with _open_client() as client:
            calendar = _get_calendar(client)
            if not calendar:
                logger.warning("Price tracker: trading calendar nahi mila, aaj ka update skip (agle slot/din dobara).")
                return
            plans = {r["id"]: plan_days(calendar, r["filing_date"], r.get("filing_time")) for r in active}

            # Kaun si date ke liye kaun se scrips chahiye
            needed: dict = {}
            for r in active:
                for d in plans[r["id"]]:
                    if d:
                        needed.setdefault(d, set()).add(r["scrip"])

            cache = _load_cache()
            for d in sorted(needed):
                entry = cache["days"].get(d)
                if entry and needed[d].issubset(set(entry.get("scrips", []))):
                    continue  # pehle hi nikal chuke hain
                time.sleep(1)  # BSE pe halka raho
                got = _download_day(client, d, needed[d])
                if got is None:
                    continue  # file nahi mili -- agle slot/din koshish hogi
                merged_scrips = set(entry.get("scrips", [])) | needed[d] if entry else set(needed[d])
                merged_data = dict(entry.get("data", {})) if entry else {}
                merged_data.update(got)
                cache["days"][d] = {"scrips": sorted(merged_scrips), "data": merged_data}
            _save_cache(cache)

            for r in active:
                plan = plans[r["id"]]
                values, all_fetched = [], True
                for d in plan:
                    if d is None:
                        values.append(None)
                        continue
                    entry = cache["days"].get(d)
                    if not entry or r["scrip"] not in entry.get("scrips", []):
                        all_fetched = False
                        values.append(None)
                    else:
                        values.append(entry["data"].get(r["scrip"]))
                flat = _flatten(values)
                pushed_ok = True
                if flat != r.get("pushed"):
                    pushed_ok = _post({"action": "update", "id": r["id"], "values": flat})
                    if pushed_ok:
                        _patch(r["id"], pushed=flat)
                        updated += 1
                    elif "row nahi mili" in _last_post_error:
                        # Sheet se row delete kar di gayi -- matlab tracking band karni hai
                        _patch(r["id"], done=True)
                        logger.info(f"Price tracker: sheet se row hata di gayi, tracking band: {r['company']}")
                        continue
                age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(r["filing_date"], "%Y-%m-%d")).days
                complete = plan[TRACK_DAYS] is not None and all_fetched
                if pushed_ok and (complete or age > GIVE_UP_AFTER_DAYS):
                    _patch(r["id"], done=True)
        logger.info(f"Price tracker: daily update poora, {updated} rows sheet mein update hui.")


def _safe_daily_run() -> None:
    try:
        run_daily_update()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Price tracker: daily update fail: {type(e).__name__}: {e}")


def maybe_run_daily_update() -> None:
    """Main loop se har cycle call hota hai. Shaam ke slots (18:30, 21:30 IST) pe, alag thread mein chalta hai."""
    global _daily_thread
    try:
        if not enabled():
            return
        now = datetime.now(IST)
        now_min = now.hour * 60 + now.minute
        slot_mins = [h * 60 + m for h, m in UPDATE_SLOTS_IST]
        reached = [i for i, sm in enumerate(slot_mins) if now_min >= sm]
        if not reached:
            return
        today = now.strftime("%Y-%m-%d")
        last_date, last_slot = None, -1
        if LAST_RUN_FILE.exists():
            try:
                with open(LAST_RUN_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                last_date, last_slot = saved.get("date"), int(saved.get("slot", -1))
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        if last_date != today:
            last_slot = -1
        if max(reached) <= last_slot:
            return  # is slot ka kaam ho chuka
        if _daily_thread is not None and _daily_thread.is_alive():
            return
        with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today, "slot": max(reached)}, f)
        _daily_thread = threading.Thread(target=_safe_daily_run, daemon=True)
        _daily_thread.start()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Price tracker: maybe_run_daily_update fail: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# Test commands
# --------------------------------------------------------------------------

def _cli() -> None:
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "test":
        if len(sys.argv) < 3:
            print("Use: python3 price_tracker.py test <scrip_code> [YYYY-MM-DD] [HH:MM]")
            return
        scrip = sys.argv[2]
        with _open_client() as client:
            calendar = _get_calendar(client)
            if not calendar:
                print("Trading calendar nahi mila -- upar ka warning dekho.")
                return
            print(f"\nCalendar mila: {len(calendar)} trading days, aakhri din {calendar[-1]}")
            if len(sys.argv) > 3:
                fdate = sys.argv[3]
                ftime = sys.argv[4] if len(sys.argv) > 4 else None
                plan = plan_days(calendar, fdate, ftime)
                print(f"\n{scrip}: filing {fdate} {ftime or '(time nahi diya)'} maanke Day mapping:")
            else:
                plan = ([None] + calendar[-6:] + [None])[:TRACK_DAYS + 1]
                print(f"\n{scrip}: pichle trading days ka price:")
            for i, d in enumerate(plan):
                if not d:
                    print(f"  Day {i}: -")
                    continue
                time.sleep(1)
                got = _download_day(client, d, {scrip})
                if got is None:
                    print(f"  Day {i}: {d}  (is din ki bhavcopy nahi mili)")
                elif scrip in got:
                    o, h, l, c = got[scrip]
                    print(f"  Day {i}: {d}  O {o}  H {h}  L {l}  C {c}")
                else:
                    print(f"  Day {i}: {d}  (is din ye stock trade nahi hua)")

    elif cmd == "sheettest":
        if not enabled():
            print("GOOGLE_SHEET_WEBHOOK_URL / GOOGLE_SHEET_SECRET .env mein set nahi hain.")
            return
        tid = "TEST" + datetime.now(IST).strftime("%H%M%S")
        ok1 = _post({"action": "add", "id": tid, "company": "TEST COMPANY (delete kar dena)",
                     "filing_date": datetime.now(IST).strftime("%Y-%m-%d"), "filing_time": "10:30",
                     "label": "Order Win", "summary": "Ye sirf connection test hai.", "scrip": "000000"})
        ok2 = _post({"action": "update", "id": tid, "values": _flatten([[1, 2, 0.5, 1.5]] + [None] * TRACK_DAYS)})
        print("Row add:", "OK" if ok1 else "FAIL", "| Row update:", "OK" if ok2 else "FAIL")
        print("Sheet kholke dekho -- ek TEST row dikhni chahiye. Dekhne ke baad usse delete kar dena.")

    elif cmd == "runnow":
        run_daily_update()
        print("Daily update chal gaya. Sheet check karo.")

    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
