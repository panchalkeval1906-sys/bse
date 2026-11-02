"""
BSE Corporate Announcements -> Telegram Notifier
==================================================

Kya karta hai:
    - Har X minute mein BSE India se naye corporate announcements/filings check karta hai
      (bse Python library use karke -- Playwright/scraping nahi, ye maintained library
      BSE ke session/cookies/headers khud handle karti hai).
    - Jo announcements pehle nahi bheje gaye (naye hain), unhe Telegram par bhej deta hai.
    - Crash/restart ke baad bhi duplicate messages nahi aayenge (seen IDs disk par save hote hain).
    - Network errors, BSE downtime, Telegram rate-limits sab handle kiye gaye hain -- script
      band nahi hogi, bas retry karke aage badhegi.

Setup:
    1) pip install bse python-dotenv requests
    2) Isi folder mein ".env" file banao (bse_telegram_bot.py ke saath) is content ke saath:

           TELEGRAM_BOT_TOKEN=your_new_token_here
           TELEGRAM_CHAT_ID=your_chat_id_here

       (Token BotFather se /revoke karke naya lena -- purana wala is chat mein already
        expose ho chuka hai, usse turant revoke kar dena.)

    3) Run: python bse_telegram_bot.py

    4) Jab tak ye process chalta rahega (laptop on, terminal open / background service),
       tab tak naye BSE filings Telegram par aate rahenge. Neeche "Continuous run" section
       mein laptop band na karna pade uske liye options diye hain.
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    import results_analysis  # Financial Results ka Revenue/Net Profit analysis (alag file)
except ImportError:          # file/bs4 na ho toh bot purane tareeke se chalta rahe
    results_analysis = None

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

load_dotenv()  # .env file se TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID load karega

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Multiple Gemini API keys (alag-alag Google accounts se) -- agar ek key ka
# free-tier rate-limit lag jaye, agli key try hogi. GEMINI_API_KEYS mein
# comma se separate karke saari keys daalo (.env mein). Agar ye set nahi hai,
# toh purana wala single GEMINI_API_KEY hi use hoga (backward-compatible).
_raw_keys = os.getenv("GEMINI_API_KEYS", "").strip()
if _raw_keys:
    GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
elif GEMINI_API_KEY:
    GEMINI_API_KEYS = [GEMINI_API_KEY]
else:
    GEMINI_API_KEYS = []

# --- Dividend Tracker (naya, alag Telegram bot) -----------------------------
# Ye purane bot se bilkul alag hai -- alag token, alag chat -- lekin isi
# script/server mein chalta hai taaki filing-detection dobara likhna na pade.
DIVIDEND_BOT_TOKEN = os.getenv("DIVIDEND_BOT_TOKEN", "").strip()
DIVIDEND_CHAT_ID = os.getenv("DIVIDEND_CHAT_ID", "").strip()

DIVIDEND_DIGEST_HOUR_IST = 9     # roz IST 9:15 pe daily leaderboard bhejna hai
DIVIDEND_DIGEST_MINUTE_IST = 15
IST_OFFSET = timedelta(hours=5, minutes=30)

# Agar yield isse zyada nikle, list mein warning ke saath dikhega -- itna high
# yield aksar galat price/amount fetch ka sign hota hai (ya genuinely bahut
# kam price wala stock hai), dono case mein user ko double-check karna chahiye.
DIVIDEND_YIELD_SANITY_CAP = 20.0

# Agar record date kabhi extract hi nahi ho paayi (PDF na mila, ya wording
# match nahi hui), toh entry hamesha list mein atki reh jaati -- kyunki
# expiry check sirf tab chalta hai jab date pata ho. Isliye: agar entry itne
# din se purani ho gayi (declaration/added_on se), maan lo uski record date
# bhi nikal chuki hogi (India mein usually 30-45 din ke andar hoti hai), aur
# use hata do.
DIVIDEND_UNKNOWN_DATE_EXPIRY_DAYS = 30

# Amount extract karne ka regex -- "Rs. 5 per share", "₹5/- per share",
# "Rs 5.50 each" jaise patterns pakadta hai. Trial-error se aage tune hoga.
DIVIDEND_AMOUNT_REGEX = re.compile(
    r"(?:rs\.?|inr|₹)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(?:/-)?\s*(?:per\s*share|per\s*equity\s*share|each|/share)",
    re.IGNORECASE,
)

# Record date extract karne ke patterns -- "Record Date ... 20/09/2026",
# "Record Date is fixed as Wednesday, September 20, 2026" jaise cases.
RECORD_DATE_NUMERIC_REGEX = re.compile(
    r"record\s*date[^0-9]{0,350}?(\d{1,2})[/\-\s](\d{1,2}|[A-Za-z]+)[/\-\s](\d{2,4})",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_NAMES_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)

# "Record Date ... 20th September, 2026" / "20 September 2026" -- din pehle,
# fir month ka naam (word mein), fir saal. RECORD_DATE_NUMERIC_REGEX ye nahi
# pakadta kyunki "20th" mein digit ke turant baad separator (/,-,space) nahi
# hai, "th" letters hain.
RECORD_DATE_DAY_MONTHNAME_REGEX = re.compile(
    r"record\s*date.{0,350}?(\d{1,2})\s*(?:st|nd|rd|th)?[,\s]+("
    + _MONTH_NAMES_PATTERN + r")[,\s]+(\d{4})",
    re.IGNORECASE | re.DOTALL,
)

# "Record Date is fixed as Wednesday, September 20, 2026" / "Record Date:
# September 20, 2026" -- month ka naam pehle, fir din, fir saal (bahut common
# format Indian filings mein). Purana regex ye bilkul nahi pakadta tha --
# isi wajah se har entry mein "Record Date: ?" aa raha tha.
#
# re.DOTALL zaroori hai -- PDF se nikala hua text mein "Record Date" ke baad
# aksar line-break aa jaata hai (PDF ki apni line-wrapping ki wajah se), aur
# normal "." kabhi line-break cross nahi karta. DOTALL flag isse cross karne
# deta hai -- isi wajah se pehle SAB entries mein "Record Date: ?" aa raha tha.
RECORD_DATE_MONTHNAME_DAY_REGEX = re.compile(
    r"record\s*date.{0,350}?(?:[A-Za-z]+day,?\s+)?("
    + _MONTH_NAMES_PATTERN + r")\s+(\d{1,2})\s*(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.IGNORECASE | re.DOTALL,
)

# AI summary settings -- agar GEMINI_API_KEY nahi di hai toh bot purane style
# (BSE ke apne text ko trim karke) ka summary use karega, koi error nahi aayega.
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_TIMEOUT_SECONDS = 25
PDF_MAX_CHARS_FOR_AI = 8000    # PDF se itne characters tak hi AI ko bhejo (cost/speed ke liye)
PDF_DOWNLOAD_TIMEOUT_SECONDS = 20

# Financial Results analysis (YoY / QoQ Revenue + Net Profit) -- results_analysis.py chahiye
RESULTS_ANALYSIS_ENABLED = True
RESULTS_PDF_MAX_CHARS = 20000    # results table kabhi kabhi cover-letter ke baad aati hai, isliye zyada text
RESULTS_PDF_MAX_PAGES = 12

POLL_INTERVAL_SECONDS = 180          # har 3 minute mein check karega
MAX_RETRY_ON_ERROR = 3               # ek check cycle ke andar retries
RETRY_BACKOFF_SECONDS = 15           # retries ke beech wait
MAX_SEEN_IDS = 20000                 # seen-ids file zyada bada na ho, isliye trim karte rahenge

# Jab bhi bot start ho, us waqt tak jo bhi announcements already aa chuke hain
# unhe silently "seen" mark kar do (Telegram par MAT bhejo). Sirf ab ke baad
# jo NAYE announcements aayenge, wahi bhejo. Ye True rakhna default behaviour
# hai jo tumne manga tha -- "bot chalu karu tabhi se, purani wali nahi chahiye".
ONLY_NEW_SINCE_STARTUP = True

# ----------------------------------------------------------------------
# IMPORTANT FILINGS FILTER
# ----------------------------------------------------------------------
# Sirf wahi announcements Telegram par bhejo jinke headline/subject/category
# mein neeche diye keywords mein se koi ek bhi milta ho (case-insensitive).
#
# Naya keyword add karna ho toh bas neeche list mein comma laga ke
# quotes ke andar add kar do -- jaise "dividend", "stock split".
# Keyword hata na ho toh us line ko delete ya # laga ke comment kar do.
IMPORTANT_KEYWORDS = [
    # --- Corporate Actions ---
    "buyback", "buy back", "buy-back",
    "bonus issue", "bonus shares", "bonus share",
    "stock split", "sub-division of shares", "subdivision of shares", "split of shares",
    "rights issue", "rights entitlement",
    "special dividend", "interim dividend", "final dividend", "dividend declared",
    "delisting", "open offer", "capital reduction", "reduction of share capital",
    # --- Mergers, Acquisitions & Restructuring ---
    "merger", "amalgamation", "acquisition", "acquire", "acquires", "acquired",
    "scheme of arrangement", "demerger", "slump sale",
    "joint venture", "stake sale", "stake purchase", "divestment", "divestiture",
    "investment in subsidiary", "investment in wholly owned subsidiary",
    "investment in step down subsidiary", "capital infusion", "infusion of funds",
    "additional investment", "incorporation of subsidiary", "incorporation of wholly owned subsidiary",
    # --- Financial results ---
    "financial results", "quarterly results", "unaudited results",
    "audited results", "board meeting outcome", "board meeting intimation",
    "annual results",
    # --- Orders / business wins ---
    "order win", "receives order", "bags order", "new order",
    "order book", "contract win", "wins order", "work order",
    "purchase order", "lois", "letter of award", "letter of intent",
    "mou signed", "memorandum of understanding",
    # --- Credit rating ---
    "credit rating", "rating upgrade", "rating downgrade", "rating reaffirmed",
    "rating withdrawn", "crisil", "icra", "care ratings", "india ratings",
    # --- Insider trading / promoter activity ---
    "insider trading", "promoter shareholding", "pledge of shares",
    "pledged shares", "release of pledge", "encumbrance",
    "acquisition of shares by promoter", "disposal of shares by promoter",
    # --- Legal / regulatory / governance red flags ---
    "sebi order", "sebi penalty", "sebi investigation", "sebi probe",
    "show cause notice", "regulatory action against", "penalty imposed",
    "search and seizure", "income tax raid", "cbi raid", "ed raid",
    "enforcement directorate", "litigation update",
    "auditor resignation", "resignation of auditor", "resignation of statutory auditor",
    "resignation of cfo", "resignation of ceo", "resignation of md",
    "resignation of managing director", "resignation of director",
    # --- Financial distress / insolvency (bade red flags) ---
    "loan default", "default in payment", "default in repayment",
    "npa classification", "one time settlement", "corporate debt restructuring",
    "insolvency", "corporate insolvency resolution", "nclt", "winding up",
    "arbitration award",
    # --- Regulatory approvals (pharma/healthcare -- bade price movers) ---
    "usfda", "us fda", "warning letter", "import alert", "anda approval",
    "drug approval", "product recall",
    # --- Key management changes ---
    "appointment of ceo", "appointment of cfo", "appointment of managing director",
    "change in key managerial personnel", "appointment of director",
    # --- Capital raising ---
    "qualified institutions placement", "qip", "preferential allotment",
    "preferential issue", "fpo", "follow-on public offer",
    "conversion of warrants", "issue of warrants",
    "non-convertible debentures", "ncd issue", "fund raising",
    # --- Investor communication ---
    "investor presentation", "analyst meet", "investor meet", "earnings call",
    "analyst / investor", "investor / analyst", "analyst and investor",
]

# Ye phrases jinke milte hi announcement SKIP ho jayega -- chahe upar wale
# IMPORTANT_KEYWORDS mein se koi bhi match kyun na ho. Ye routine/boilerplate
# notices hain jo baar baar aate hain aur koi real action nahi batate
# (jaise "results aane wale hain isliye trading window band" -- har company
# mein har quarter aata hai).
EXCLUDE_KEYWORDS = [
    "trading window",
    "closure of trading window",
    # Insider trading / SAST (Substantial Acquisition of Shares & Takeovers)
    # disclosures -- ye generic "acquisition" keyword se match ho jaate hain
    # (kyunki regulation ka naam hi "Substantial Acquisition of Shares" hai),
    # isliye yahan explicitly exclude kiya hai taaki ye kabhi na aayen.
    "insider trading",
    "substantial acquisition of shares",
    "substantial acquisition of shares & takeovers",
    "sast regulations",
    "regulation 29(1)", "regulation 29(2)", "regulation 29 (1)", "regulation 29 (2)",
    "regulation 7(2)", "regulation 7 (2)",
    "promoter shareholding", "pledge of shares", "pledged shares",
    "release of pledge", "encumbrance",
    "acquisition of shares by promoter", "disposal of shares by promoter",
]
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# FILING LABEL (har filing ke top pe naam -- "Dividend", "Bonus", "Buyback" etc.)
# ----------------------------------------------------------------------
# Order maayne rakhta hai -- upar wale zyada specific hain, pehle check hote hain.
# Match karne ke liye headline/subject/category text (case-insensitive) use hota hai.
FILING_LABEL_RULES = [
    ("Dividend", ["dividend"]),
    ("Bonus Issue", ["bonus issue", "bonus shares", "bonus share"]),
    ("Buyback", ["buyback", "buy back", "buy-back"]),
    ("Stock Split", ["stock split", "sub-division of shares", "subdivision of shares", "split of shares"]),
    ("Rights Issue", ["rights issue", "rights entitlement"]),
    ("Delisting", ["delisting"]),
    ("Open Offer", ["open offer"]),
    ("Insider/Promoter Activity", ["insider trading", "promoter shareholding",
                                    "pledge of shares", "pledged shares",
                                    "release of pledge", "encumbrance",
                                    "acquisition of shares by promoter",
                                    "disposal of shares by promoter",
                                    "substantial acquisition of shares"]),
    ("Investment in Subsidiary", ["investment in subsidiary", "investment in wholly owned subsidiary",
                                    "investment in step down subsidiary", "capital infusion",
                                    "infusion of funds", "additional investment",
                                    "incorporation of subsidiary", "incorporation of wholly owned subsidiary"]),
    ("Merger/Acquisition", ["merger", "amalgamation", "acquisition", "acquire",
                            "acquires", "acquired", "scheme of arrangement",
                            "demerger", "slump sale"]),
    ("Stake Sale/JV", ["joint venture", "stake sale", "stake purchase",
                        "divestment", "divestiture"]),
    ("Financial Results", ["financial results", "quarterly results", "unaudited results",
                            "audited results", "annual results", "board meeting outcome"]),
    ("Order Win", ["order win", "receives order", "bags order", "new order",
                   "order book", "contract win", "wins order", "work order",
                   "purchase order", "lois", "letter of award", "letter of intent"]),
    ("MoU", ["mou signed", "memorandum of understanding"]),
    ("Credit Rating", ["credit rating", "rating upgrade", "rating downgrade",
                        "rating reaffirmed", "rating withdrawn", "crisil",
                        "icra", "care ratings", "india ratings"]),
    ("Regulatory/Legal", ["sebi order", "sebi penalty", "sebi investigation", "sebi probe",
                          "show cause notice", "regulatory action against", "penalty imposed",
                          "search and seizure", "income tax raid", "cbi raid", "ed raid",
                          "enforcement directorate", "litigation update"]),
    ("Resignation", ["auditor resignation", "resignation of auditor",
                      "resignation of statutory auditor", "resignation of cfo",
                      "resignation of ceo", "resignation of md",
                      "resignation of managing director", "resignation of director"]),
    ("Financial Distress", ["loan default", "default in payment", "default in repayment",
                            "npa classification", "one time settlement",
                            "corporate debt restructuring", "insolvency",
                            "corporate insolvency resolution", "nclt", "winding up",
                            "arbitration award"]),
    ("Regulatory Approval", ["usfda", "us fda", "warning letter", "import alert",
                              "anda approval", "drug approval", "product recall"]),
    ("Management Change", ["appointment of ceo", "appointment of cfo",
                            "appointment of managing director",
                            "change in key managerial personnel", "appointment of director"]),
    ("Warrants/Preferential Issue", ["conversion of warrants", "issue of warrants",
                                      "issued warrants", "allotment of warrants",
                                      "warrants", "preferential allotment", "preferential issue",
                                      "qualified institutions placement", "qip",
                                      "fpo", "follow-on public offer"]),
    ("Debt Issue", ["non-convertible debentures", "ncd issue", "fund raising"]),
    ("Investor Communication", ["investor presentation", "analyst meet", "investor meet",
                                "earnings call", "analyst / investor", "investor / analyst",
                                "analyst and investor"]),
    ("Postal Ballot", ["postal ballot"]),
    ("Capital Reduction", ["capital reduction", "reduction of share capital"]),
]


def classify_filing_label(item: dict) -> str | None:
    """
    Filing ke headline/subject/category padh ke ek chota naam deta hai
    (jaise "Dividend", "Bonus Issue", "Order Win") jo message ke top pe
    dikhega. Kisi bhi group se match na ho toh None -- caller BSE ki apni
    category (CATEGORYNAME) use kar lega fallback ke roop mein.
    """
    fields = [
        item.get("HEADLINE"), item.get("NEWSSUB"), item.get("MORE"),
        item.get("CATEGORYNAME"), item.get("Category"),
    ]
    combined = " ".join(str(f) for f in fields if f).lower()
    for label, keywords in FILING_LABEL_RULES:
        if any(kw in combined for kw in keywords):
            return label
    return None


def _result_variant(combined_lower: str) -> str:
    """
    Financial Results filing ke text mein "Standalone" ya "Consolidated"
    dhoondta hai. Kabhi kabhi dono words ek hi filing mein hote hain (company
    ne ek hi PDF mein dono de diye) -- us case mein "consolidated" priority
    leta hai, jaisa user ne explicitly bola: "Conso mile toh Conso lena,
    nahi toh Standalone".
    """
    if "consolidated" in combined_lower:
        return "consolidated"
    if "standalone" in combined_lower:
        return "standalone"
    return "unknown"


def should_send_financial_result(scrip: str, combined_text: str, fr_tracking: list) -> tuple[bool, list]:
    """
    Standalone vs Consolidated priority handle karta hai, jab same company
    (same scrip, same din) ke liye Financial Results do alag announcements
    mein aate hain (jo BSE pe bahut common hai):

    - Pehli baar is scrip ki koi Financial Results filing aaj aayi -> bhejo.
    - Pehle Standalone bhej chuke the, ab Consolidated aaya -> bhejo
      (behtar version hai, user isi ko priority deta hai).
    - Pehle Consolidated bhej chuke the, ab Standalone aaya -> SKIP karo
      (ab ye duplicate/lower-priority info hai, user ko nahi chahiye).
    - Pehle Consolidated bhej chuke, ab PHIR Consolidated aaya (revised
      filing) -> bhejo, kyunki ye genuinely corrected numbers ho sakte hain.

    Return: (is filing ko Telegram par bhejna hai, updated tracking list)
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    variant = _result_variant(combined_text.lower())

    existing = None
    existing_idx = None
    for i, e in enumerate(fr_tracking):
        if e.get("scrip") == scrip and e.get("date") == today_str:
            existing = e
            existing_idx = i
            break

    if existing is None:
        fr_tracking.append({"scrip": scrip, "date": today_str, "variant": variant})
        return True, fr_tracking

    prev_variant = existing.get("variant")

    if prev_variant == "consolidated" and variant == "standalone":
        logger.info(f"Financial Results: scrip {scrip} ke liye Consolidated already bhej chuke, Standalone skip kar raha hoon.")
        return False, fr_tracking

    if prev_variant == "standalone" and variant == "consolidated":
        fr_tracking[existing_idx]["variant"] = "consolidated"
        return True, fr_tracking

    return True, fr_tracking


DATA_DIR = Path(__file__).resolve().parent / "bse_bot_data"
DATA_DIR.mkdir(exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_announcements.json"
LOG_FILE = DATA_DIR / "bot.log"

# Dividend tracker ki apni files -- purani seen_announcements.json se alag,
# taaki dono features ek dusre se independent rahein.
DIVIDEND_STAGE_A_FILE = DATA_DIR / "dividend_stage_a.json"   # board meeting scheduled, amount pata nahi
DIVIDEND_STAGE_B_FILE = DATA_DIR / "dividend_stage_b.json"   # amount + record date confirm
DIVIDEND_LAST_DIGEST_FILE = DATA_DIR / "dividend_last_digest.json"  # aaj digest bheja ya nahi, dedup ke liye
DIVIDEND_BACKFILL_MARKER_FILE = DATA_DIR / "dividend_backfill_done.json"  # 30-din backfill ek hi baar chale

# Financial Results ke liye Standalone vs Consolidated priority track karne
# wali file -- same scrip, same din mein dono aa sakte hain (do alag
# announcements), user ko sirf behtar wala (Consolidated) chahiye.
FINANCIAL_RESULTS_TRACK_FILE = DATA_DIR / "financial_results_sent.json"

# --------------------------------------------------------------------------
# Logging (console + file dono mein)
# --------------------------------------------------------------------------

logger = logging.getLogger("bse_bot")
logger.setLevel(logging.INFO)

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))
logger.addHandler(_console)

_file = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(_file)


# --------------------------------------------------------------------------
# Seen-announcements store (crash-safe de-duplication)
# --------------------------------------------------------------------------

def load_seen_ids() -> set:
    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"seen_announcements.json corrupt tha, fresh start kar raha hoon: {e}")
    return set()


def save_seen_ids(seen_ids: set) -> None:
    try:
        ids_list = list(seen_ids)
        if len(ids_list) > MAX_SEEN_IDS:
            ids_list = ids_list[-MAX_SEEN_IDS:]  # purane trim kar do
        tmp_file = SEEN_FILE.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(ids_list, f)
        tmp_file.replace(SEEN_FILE)  # atomic write -- beech mein crash se file corrupt nahi hogi
    except OSError as e:
        logger.error(f"seen ids save nahi ho paaye: {e}")


def make_announcement_id(item: dict) -> str:
    """
    BSE ke response mein exact unique-id field version/API ke hisaab se badal sakta hai,
    isliye hum khud ek stable fingerprint bana rahe hain scrip code + headline + date/time se.
    Isse duplicate detection reliable rehta hai chahe field names thoda alag ho.
    """
    scrip = str(item.get("SCRIP_CD") or item.get("scripcode") or "")
    headline = str(item.get("HEADLINE") or item.get("NEWSSUB") or item.get("Headline") or "")
    subject = str(item.get("NEWSSUB") or item.get("MORE") or "")
    dt = str(item.get("NEWS_DT") or item.get("DT_TM") or item.get("News_submission_dt") or "")
    news_id = str(item.get("NEWSID") or item.get("NSURL") or "")

    raw = f"{scrip}|{headline}|{subject}|{dt}|{news_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Dividend Tracker -- naya, alag bot (DIVIDEND_BOT_TOKEN / DIVIDEND_CHAT_ID)
# --------------------------------------------------------------------------

def _load_json_list(path: Path) -> list:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"{path.name} corrupt tha, fresh start kar raha hoon: {e}")
    return []


def _save_json_list(path: Path, data: list) -> None:
    try:
        tmp_file = path.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_file.replace(path)
    except OSError as e:
        logger.error(f"{path.name} save nahi ho paayi: {e}")


def _combined_dividend_text(item: dict) -> str:
    fields = [
        item.get("HEADLINE"), item.get("NEWSSUB"), item.get("MORE"),
        item.get("CATEGORYNAME"), item.get("Category"),
    ]
    return " ".join(str(f) for f in fields if f).lower()


DIVIDEND_DECLARED_KEYWORDS = ["dividend declared", "interim dividend", "final dividend", "special dividend"]


def is_dividend_declared_filing(item: dict) -> bool:
    combined = _combined_dividend_text(item)
    return any(kw in combined for kw in DIVIDEND_DECLARED_KEYWORDS)


def is_board_meeting_dividend_filing(item: dict) -> bool:
    combined = _combined_dividend_text(item)
    return "board meeting" in combined and "dividend" in combined and not is_dividend_declared_filing(item)


def extract_dividend_amount(text: str) -> float | None:
    """
    "Rs 5 per share" jaisa pattern dhoondta hai -- lekin "equity shares of
    face value of Rs. 10/- each" jaisi line ko dividend amount samajh lene
    ki galti se bachta hai (face value dividend nahi hai). Jis match ke
    aas-paas "dividend" shabd mile, use priority; "face value" ke paas
    wale match ko poori tarah skip karta hai.
    """
    if not text:
        return None
    fallback = None
    for m in DIVIDEND_AMOUNT_REGEX.finditer(text):
        start = max(0, m.start() - 200)
        end = min(len(text), m.end() + 80)
        context = text[start:end].lower()
        if "face value" in context:
            continue  # ye share ka face value hai, dividend amount nahi
        if "dividend" in context:
            return float(m.group(1))
        if fallback is None:
            fallback = float(m.group(1))
    return fallback


# Rupee amounts extract karne ke liye -- "Rs. 100 Crore", "₹50 lakh",
# "Rs. 25,00,00,000" (bina crore/lakh likhe) -- teeno tarah ke patterns.
AMOUNT_CRORE_UNIT_REGEX = re.compile(
    r"(?:rs\.?|inr|₹)\s*[:\-]?\s*([\d,]+(?:\.\d+)?)\s*(crore|cr\.?|lakh|lac)s?\b",
    re.IGNORECASE,
)
AMOUNT_RAW_NUMBER_REGEX = re.compile(
    r"(?:rs\.?|inr|₹)\s*[:\-]?\s*([\d,]{8,})\b",
    re.IGNORECASE,
)


def extract_amount_crore(text: str) -> float | None:
    """
    Filing text se rupee amount nikaalta hai aur ₹ Crore mein convert karke deta hai.
    Order value, funds raised, debt amount -- sabke liye reuse hota hai.
    Nahi mila toh None (caller "PDF check karo" dikhayega, galat number nahi).
    """
    match = AMOUNT_CRORE_UNIT_REGEX.search(text)
    if match:
        try:
            num = float(match.group(1).replace(",", ""))
        except ValueError:
            return None
        unit = match.group(2).lower()
        if unit.startswith("lak") or unit.startswith("lac"):
            num = num / 100.0
        return round(num, 2)

    match2 = AMOUNT_RAW_NUMBER_REGEX.search(text)
    if match2:
        try:
            num = float(match2.group(1).replace(",", ""))
        except ValueError:
            return None
        return round(num / 1e7, 2)  # raw rupees -> crore

    return None


def _build_date_str(day_s: str, mon_s: str, year_s: str) -> str | None:
    try:
        day = int(day_s)
        year = int(year_s)
        if year < 100:
            year += 2000
        if mon_s.isdigit():
            month = int(mon_s)
        else:
            month = _MONTHS.get(mon_s.lower()[:4]) or _MONTHS.get(mon_s.lower()[:3])
        if not month:
            return None
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def extract_record_date(text: str) -> str | None:
    """
    Best-effort record date nikalta hai -- teen formats try karta hai kyunki
    BSE filings mein date likhne ka style company-se-company alag hota hai:
      1) "20/09/2026" ya "20-09-2026" (numeric)
      2) "20th September, 2026" (din pehle, month naam baad mein)
      3) "September 20, 2026" ya "Wednesday, September 20, 2026" (month naam
         pehle, din baad mein -- ye format bahut common hai, pehle isse
         bilkul handle nahi kiya jaa raha tha, isi wajah se har entry mein
         "Record Date: ?" aa raha tha)
    Nahi mile toh None -- caller "verify manually" dikhayega, galat date
    dikhane se better hai.
    """
    match = RECORD_DATE_NUMERIC_REGEX.search(text)
    if match:
        result = _build_date_str(*match.groups())
        if result:
            return result

    match = RECORD_DATE_DAY_MONTHNAME_REGEX.search(text)
    if match:
        result = _build_date_str(*match.groups())
        if result:
            return result

    match = RECORD_DATE_MONTHNAME_DAY_REGEX.search(text)
    if match:
        mon_s, day_s, year_s = match.groups()
        result = _build_date_str(day_s, mon_s, year_s)
        if result:
            return result

    return None


def get_opening_price(scrip_code: str) -> float | None:
    """
    Usi din ki opening price BSE se leta hai (bse library ka .quote() method
    reuse kar rahe hain -- koi naya API/dependency nahi chahiye).
    """
    try:
        from bse import BSE
        with BSE(str(DATA_DIR)) as bse_client:
            data = bse_client.quote(scrip_code)
        open_price = data.get("Open") or data.get("open")
        return float(open_price) if open_price else None
    except Exception as e:  # noqa: BLE001 -- price fetch fail ho toh crash nahi, bas None
        logger.warning(f"Price fetch fail hui scrip {scrip_code} ke liye: {e}")
        return None


def get_market_cap(scrip_code: str) -> float | None:
    """
    Market cap (₹ Crore mein) BSE se leta hai -- bse library ka .listSecurities()
    method reuse kar rahe hain, isme 'Mktcap' field seedha mil jaata hai, koi
    alag paid API nahi chahiye. Real-time nahi hai (BSE jab bhi last update kiya
    ho), lekin ye kaafi hai -- exact-second market cap ki zaroorat nahi hai.
    """
    try:
        from bse import BSE
        with BSE(str(DATA_DIR)) as bse_client:
            # group="" zaroori hai -- agar diya na jaaye toh library default
            # 'A' group use karti hai, aur zyaadatar chhoti/mid companies
            # Group A mein nahi hoti (isliye pehle khaali data aa raha tha).
            # Khaali group -> BSE ki saari groups (A/B/T/X etc.) mein search hota hai.
            data = bse_client.listSecurities(scripcode=str(scrip_code), group="")
        if not data:
            logger.info(f"Market cap: listSecurities() ne scrip {scrip_code} ke liye khaali data diya.")
            return None
        entry = data[0] if isinstance(data, list) else data
        mktcap = (
            entry.get("Mktcap") or entry.get("MKTCAP") or entry.get("mktcap")
            or entry.get("MktCap") or entry.get("Mkt_Cap") or entry.get("MARKETCAP")
        )
        if not mktcap:
            logger.info(
                f"Market cap: scrip {scrip_code} ke response mein 'Mktcap' field nahi mila. "
                f"Response ke keys: {list(entry.keys()) if isinstance(entry, dict) else 'N/A'}"
            )
            return None
        return float(mktcap)
    except Exception as e:  # noqa: BLE001 -- market cap fetch fail ho toh crash nahi, bas None
        logger.warning(f"Market cap fetch fail hui scrip {scrip_code} ke liye: {e}")
        return None


def get_last_revenue_crore(scrip_code: str) -> float | None:
    """
    Company ka revenue (₹ Crore mein) nikaalne ki koshish karta hai -- TTM
    (trailing twelve months, matlab last 4 quarters jodke) banane ki koshish
    karta hai, taaki sirf ek quarter ka number na dikhe. Ye BEST-EFFORT hai --
    field naming aur data-shape BSE ke response mein consistent nahi hai,
    isliye kai case mein None hi aayega, ya sirf latest single-quarter figure
    (agar 4 quarters ka data na mile). Caller ko None mile toh revenue line
    simply skip kar dega, galat number kabhi nahi dikhayega.
    """
    revenue_keys = ("Revenue", "REVENUE", "TotalIncome", "Total_Income",
                     "NetSales", "Net_Sales", "Revenue_From_Operations",
                     "IncmFrmOpr")

    def _extract_revenue(entry: dict) -> float | None:
        for key in revenue_keys:
            if entry.get(key):
                try:
                    return float(str(entry[key]).replace(",", ""))
                except (ValueError, TypeError):
                    continue
        return None

    try:
        from bse import BSE
        with BSE(str(DATA_DIR)) as bse_client:
            data = bse_client.resultsSnapshot(str(scrip_code))
        if not data:
            return None

        candidates = data if isinstance(data, list) else [data]
        entries = [e for e in candidates if isinstance(e, dict)]

        # TTM banane ki koshish: agar 4 ya zyada quarterly entries hain,
        # sabse recent 4 ka revenue jod do (assume list sorted hai, jaisa
        # BSE ka historical results data usually latest-first ya
        # latest-last hota hai -- dono direction try karte hain).
        if len(entries) >= 4:
            revs = [_extract_revenue(e) for e in entries[:4]]
            revs = [r for r in revs if r is not None]
            if len(revs) == 4:
                return round(sum(revs), 2)

        # Fallback: sirf jo bhi single entry mile usi se ek quarter ka number
        # (TTM nahi bana paye) -- best-effort, isse better kuch nahi mila.
        for entry in entries:
            rev = _extract_revenue(entry)
            if rev is not None:
                return round(rev, 2)

        return None
    except Exception as e:  # noqa: BLE001 -- ye best-effort hai, fail hone par bas None
        logger.warning(f"Revenue fetch fail hui scrip {scrip_code} ke liye (best-effort tha): {e}")
        return None


def _parse_filing_date(dt_str: str) -> str | None:
    """
    BSE ke response mein date ka format thoda alag ho sakta hai (jaise
    "2026-09-13T07:00:00" ya "13-Sep-2026" ya "13/09/2026"). Jo bhi format ho,
    usme se "YYYY-MM-DD" nikalne ki koshish karta hai. Fail ho toh None --
    caller phir aaj ki date use karega fallback ke roop mein.
    """
    if not dt_str:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_str)
    if m:
        return m.group(1)
    m = re.match(r"(\d{1,2})[-/](" + _MONTH_NAMES_PATTERN + r")[-/](\d{4})", dt_str, re.IGNORECASE)
    if m:
        day, mon_name, year = m.groups()
        mon = _MONTHS.get(mon_name.lower())
        if mon:
            return f"{year}-{mon:02d}-{int(day):02d}"
    m = re.match(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", dt_str)
    if m:
        day, mon, year = m.groups()
        try:
            return f"{year}-{int(mon):02d}-{int(day):02d}"
        except ValueError:
            return None
    return None


def _dividend_company_name(item: dict) -> str:
    return str(
        item.get("SLONGNAME") or item.get("Slongname") or item.get("CompanyName")
        or item.get("scripname") or "Unknown Company"
    )


def _dividend_scrip_code(item: dict) -> str:
    return str(item.get("SCRIP_CD") or item.get("scripcode") or "-")


def process_stage_a(item: dict, stage_a: list) -> list:
    scrip = _dividend_scrip_code(item)
    company = _dividend_company_name(item)
    dt = str(item.get("NEWS_DT") or item.get("DT_TM") or "")

    stage_a = [e for e in stage_a if e.get("scrip") != scrip]  # purana entry hatao agar hai
    stage_a.append({"scrip": scrip, "company": company, "meeting_filed_on": dt})
    logger.info(f"Dividend Stage A mein add hua: {company} ({scrip})")
    return stage_a


def process_stage_b(item: dict, stage_a: list, stage_b: list) -> tuple:
    scrip = _dividend_scrip_code(item)
    company = _dividend_company_name(item)
    headline = str(item.get("HEADLINE") or "")
    subject = str(item.get("NEWSSUB") or item.get("MORE") or "")
    text_for_extraction = f"{headline} {subject}"

    amount = extract_dividend_amount(text_for_extraction)
    record_date = extract_record_date(text_for_extraction)

    # Headline/subject mein na mile toh PDF text try karo
    attachment = item.get("ATTACHMENTNAME") or item.get("Attachment") or ""
    if (amount is None or record_date is None) and attachment:
        pdf_text = download_pdf_text_by_attachment(attachment)
        if pdf_text:
            if amount is None:
                amount = extract_dividend_amount(pdf_text)
            if record_date is None:
                record_date = extract_record_date(pdf_text)

    yield_pct = None
    open_price = None
    if amount is not None:
        open_price = get_opening_price(scrip)
        if open_price:
            yield_pct = round((amount / open_price) * 100, 2)

    dt_raw = str(item.get("NEWS_DT") or item.get("DT_TM") or item.get("News_submission_dt") or "")
    filing_date = _parse_filing_date(dt_raw) or datetime.now().strftime("%Y-%m-%d")

    stage_a = [e for e in stage_a if e.get("scrip") != scrip]  # Stage A se hatao (agar tha)
    stage_b = [e for e in stage_b if e.get("scrip") != scrip]  # purana Stage B entry (agar tha) replace karo

    stage_b.append({
        "scrip": scrip,
        "company": company,
        "amount": amount,               # None ho sakta hai -- verify manually case
        "record_date": record_date,     # None ho sakta hai -- verify manually case
        "open_price": open_price,
        "yield_pct": yield_pct,
        "added_on": filing_date,        # BSE ki asli filing date -- processing time nahi,
                                         # taaki restart/backfill se expiry clock reset na ho
    })
    logger.info(
        f"Dividend Stage B mein add hua: {company} ({scrip}) -- "
        f"amount={amount}, record_date={record_date}, yield={yield_pct}"
    )
    return stage_a, stage_b


def remove_expired_stage_b(stage_b: list) -> list:
    today = datetime.now().date()
    kept = []
    for entry in stage_b:
        rd = entry.get("record_date")
        if rd:
            try:
                if datetime.strptime(rd, "%Y-%m-%d").date() < today:
                    logger.info(f"Record date nikal gayi, hata raha hoon: {entry.get('company')}")
                    continue
            except ValueError:
                pass
        else:
            # Record date kabhi nahi mili -- agar entry bahut purani ho gayi
            # hai, maan lo uski record date bhi nikal chuki hogi, warna ye
            # hamesha ke liye list mein atki rahegi (jaisa M Lakhamsi ke saath
            # ho raha tha).
            added_on = entry.get("added_on")
            if added_on:
                try:
                    added_date = datetime.strptime(added_on, "%Y-%m-%d").date()
                    if (today - added_date).days > DIVIDEND_UNKNOWN_DATE_EXPIRY_DAYS:
                        logger.info(
                            f"Record date {DIVIDEND_UNKNOWN_DATE_EXPIRY_DAYS}+ din se nahi mili, "
                            f"purani maan ke hata raha hoon: {entry.get('company')}"
                        )
                        continue
                except ValueError:
                    pass
        kept.append(entry)
    return kept


def send_dividend_message(text: str) -> bool:
    if not DIVIDEND_BOT_TOKEN or not DIVIDEND_CHAT_ID:
        logger.error("DIVIDEND_BOT_TOKEN / DIVIDEND_CHAT_ID .env mein set nahi hai!")
        return False
    url = f"https://api.telegram.org/bot{DIVIDEND_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": DIVIDEND_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(retry_after + 1)
                continue
            logger.error(f"Dividend Telegram error {resp.status_code}: {resp.text[:200]}")
        except requests.RequestException as e:
            logger.warning(f"Dividend Telegram request fail (attempt {attempt}/3): {e}")
            time.sleep(3)
    return False


def build_dividend_digest(stage_a: list, stage_b: list) -> str:
    lines = ["📊 *Dividend Yield Leaderboard*\n"]

    # Ranked list mein SIRF wahi entries jinki record date CONFIRM ho chuki hai --
    # warna date-less entries top pe "Record Date: ?" ke saath dikhti thi, aur
    # kabhi expire bhi nahi hoti thi. Ab wo saari "Verify manually" mein jayengi.
    ranked = sorted(
        [e for e in stage_b if e.get("yield_pct") is not None and e.get("record_date")],
        key=lambda e: e["yield_pct"], reverse=True,
    )
    unverified = [e for e in stage_b if not (e.get("yield_pct") is not None and e.get("record_date"))]

    if ranked:
        for i, e in enumerate(ranked, 1):
            rd = e.get("record_date")
            flag = " ⚠️_(yield asaamanya zyada, verify karo)_" if e["yield_pct"] > DIVIDEND_YIELD_SANITY_CAP else ""
            lines.append(f"{i}) {e['company']} — ₹{e['amount']}/share — {e['yield_pct']}% — Record Date: {rd}{flag}")
    else:
        lines.append("_Koi confirmed dividend entry nahi hai abhi._")

    if unverified:
        lines.append("\n⚠️ *Verify manually* (record date ya amount/yield auto-detect nahi ho paaya):")
        for e in unverified:
            extra = []
            if e.get("amount") is not None:
                extra.append(f"₹{e['amount']}/share")
            if e.get("yield_pct") is not None:
                extra.append(f"{e['yield_pct']}% yield")
            detail = f" — {', '.join(extra)}" if extra else ""
            lines.append(f"- {e['company']} (scrip {e['scrip']}){detail}")

    if stage_a:
        lines.append("\n📋 *Upcoming (Board Meeting Scheduled):*")
        for e in stage_a:
            lines.append(f"- {e['company']}")

    return "\n".join(lines)


def maybe_send_daily_digest(stage_a: list, stage_b: list) -> None:
    now_ist = datetime.now(timezone.utc).replace(tzinfo=None) + IST_OFFSET
    today_str = now_ist.strftime("%Y-%m-%d")

    last = _load_json_list(DIVIDEND_LAST_DIGEST_FILE)
    last_sent = last[0] if last else None

    target_minutes = DIVIDEND_DIGEST_HOUR_IST * 60 + DIVIDEND_DIGEST_MINUTE_IST
    now_minutes = now_ist.hour * 60 + now_ist.minute

    if now_minutes >= target_minutes and last_sent != today_str:
        text = build_dividend_digest(stage_a, stage_b)
        if send_dividend_message(text):
            logger.info("Dividend daily digest bhej diya.")
            _save_json_list(DIVIDEND_LAST_DIGEST_FILE, [today_str])
        else:
            logger.error("Dividend daily digest bhejne mein fail hua, agla cycle mein retry hoga.")


def fetch_announcements_for_range(from_date: datetime, to_date: datetime,
                                   category: str = "-1", subcategory: str = "-1") -> list:
    """
    fetch_todays_announcements() jaisa hi hai, bas date range aur category/subcategory
    filter bhi le sakta hai -- isse dividend backfill seedha BSE se sirf "Dividend"
    ya "Board Meeting" wali filings maangta hai (saara data scan karke client-side
    filter karne se zyada reliable aur halka, kam data process hota hai).
    Purana fetch_todays_announcements() function bilkul waisa hi hai jaisa tha, isko
    touch nahi kiya -- normal alert pipeline isi purane function ko use karta rahega.
    """
    from bse import BSE

    all_items = []
    with BSE(str(DATA_DIR)) as bse_client:
        page_no = 1
        total_count = None
        while True:
            res = bse_client.announcements(
                page_no=page_no, from_date=from_date, to_date=to_date,
                category=category, subcategory=subcategory,
            )
            if total_count is None:
                try:
                    total_count = int(res["Table1"][0]["ROWCNT"])
                except (KeyError, IndexError, ValueError, TypeError):
                    total_count = 0
            page_items = res.get("Table", []) or []
            all_items.extend(page_items)
            if total_count == 0 or len(all_items) >= total_count or not page_items:
                break
            page_no += 1
            time.sleep(0.5)
    return all_items


def run_dividend_backfill_once(stage_a: list, stage_b: list) -> tuple:
    """
    Ek hi baar chalta hai (marker file check karta hai) -- bot ke pehli baar
    start hone par pichle 30 din ke dividend-related filings dekh ke Stage A/B
    list turant bana deta hai, taaki Day-1 se hi list khaali na ho.

    BSE ke category/subcategory filter directly use kar rahe hain (saara data
    scan karke client-side filter karne ke bajaye) -- zyada reliable aur halka.
    """
    if DIVIDEND_BACKFILL_MARKER_FILE.exists():
        return stage_a, stage_b

    logger.info("Dividend Tracker: pichle 30 din ka backfill shuru kar raha hoon (ek hi baar hoga)...")
    from_date = datetime.now() - timedelta(days=30)
    to_date = datetime.now()

    count_a = count_b = 0

    # --- Stage B: seedha "Dividend" subcategory se already-declared dividends ---
    try:
        declared_items = fetch_announcements_for_range(
            from_date, to_date, category="Corp. Action", subcategory="Dividend"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Dividend backfill (declared) fail hui: {e}")
        declared_items = []

    for item in declared_items:
        stage_a, stage_b = process_stage_b(item, stage_a, stage_b)
        count_b += 1

    # --- Stage A: "Board Meeting" category, sirf wo jinme dividend mention hai ---
    try:
        board_items = fetch_announcements_for_range(
            from_date, to_date, category="Board Meeting"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Dividend backfill (board meeting) fail hui: {e}")
        board_items = []

    stage_b_scrips = {e.get("scrip") for e in stage_b}
    for item in board_items:
        if is_board_meeting_dividend_filing(item) and _dividend_scrip_code(item) not in stage_b_scrips:
            stage_a = process_stage_a(item, stage_a)
            count_a += 1

    stage_b = remove_expired_stage_b(stage_b)
    _save_json_list(DIVIDEND_STAGE_A_FILE, stage_a)
    _save_json_list(DIVIDEND_STAGE_B_FILE, stage_b)
    _save_json_list(DIVIDEND_BACKFILL_MARKER_FILE, [datetime.now().strftime("%Y-%m-%d")])
    logger.info(f"Dividend backfill complete: Stage A mein {count_a}, Stage B mein {count_b} entries mile.")
    return stage_a, stage_b


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID .env mein set nahi hai!")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                # Telegram rate-limit: retry_after seconds batata hai
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                logger.warning(f"Telegram rate-limited, {retry_after}s wait kar raha hoon")
                time.sleep(retry_after + 1)
                continue
            logger.error(f"Telegram error {resp.status_code}: {resp.text[:200]}")
        except requests.RequestException as e:
            logger.warning(f"Telegram request fail (attempt {attempt}/3): {e}")
            time.sleep(3)
    return False


def is_important(item: dict) -> bool:
    """
    True agar is announcement ka headline/subject/category IMPORTANT_KEYWORDS
    mein se kisi se match karta hai (case-insensitive substring match) --
    LEKIN EXCLUDE_KEYWORDS mein se koi match ho jaye toh turant False
    (chahe kitna bhi important keyword match kyun na ho).
    """
    fields = [
        item.get("HEADLINE"),
        item.get("NEWSSUB"),
        item.get("MORE"),
        item.get("CATEGORYNAME"),
        item.get("Category"),
    ]
    combined = " ".join(str(f) for f in fields if f).lower()

    if any(keyword.lower() in combined for keyword in EXCLUDE_KEYWORDS):
        return False

    return any(keyword.lower() in combined for keyword in IMPORTANT_KEYWORDS)


def short_summary(item: dict, max_len: int = 220) -> str:
    """
    Headline + subject ko mila kar ek chota (1-2 line) summary banata hai.
    Ye AI-summary nahi hai -- BSE ke apne text ko hi trim/combine karta hai,
    isliye koi extra API key nahi chahiye.
    """
    headline = str(item.get("HEADLINE") or item.get("Headline") or "").strip()
    subject = str(item.get("NEWSSUB") or item.get("MORE") or "").strip()

    if headline and subject and headline.lower() != subject.lower():
        text = f"{headline} — {subject}"
    else:
        text = headline or subject or "New filing"

    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0].rstrip(",.-—") + "…"
    return text


def download_pdf_text(pdf_url: str, max_chars: int | None = None, max_pages: int = 6) -> str:
    """
    PDF download karke usse text nikalta hai (pehle kuch pages tak, itna kaafi hai
    zyada tar BSE filings ke liye). Error hone par khaali string return karta hai.
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bseindia.com/corporates/ann.html",
            "Accept": "application/pdf,*/*",
        }
        resp = requests.get(pdf_url, headers=headers, timeout=PDF_DOWNLOAD_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logger.warning(f"PDF download fail hua (status {resp.status_code}): {pdf_url}")
            return ""
        import pdfplumber
        from io import BytesIO

        limit = max_chars or PDF_MAX_CHARS_FOR_AI

        text_parts = []
        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            for page in pdf.pages[:max_pages]:  # default 6 pages, kaafi hai summary ke liye
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
                if sum(len(t) for t in text_parts) > limit:
                    break
        full_text = "\n".join(text_parts).strip()
        return full_text[:limit]
    except Exception as e:  # noqa: BLE001 -- PDF corrupt/scanned/timeout, koi bhi issue ho sakta hai
        logger.warning(f"PDF text nikalne mein dikkat: {e}")
        return ""


# BSE purane (roughly 30 din se pehle ke) filings ke PDF "AttachLive" folder
# se 404 de dete hain -- wo "AttachHis" (historical) folder mein chale jaate
# hain. Isliye pehle Live try karte hain (aaj/recent filings ke liye kaam
# karta hai), fail ho toh His try karte hain (purani filings ke liye) --
# isi wajah se dividend backfill (30 din purana data) mein record date/amount
# kabhi PDF se nikal hi nahi paa raha tha, hamesha "Record Date: ?" aa raha tha.
BSE_PDF_URL_TEMPLATES = [
    "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{}",
    "https://www.bseindia.com/xml-data/corpfiling/AttachHis/{}",
]


def download_pdf_text_by_attachment(attachment: str, max_chars: int | None = None, max_pages: int = 6) -> str:
    """
    download_pdf_text() jaisa hi hai, bas seedha attachment filename leta hai
    aur dono possible BSE folders try karta hai (pehle Live, fail ho toh His) --
    jo pehle mil jaye wahi use karta hai.
    """
    for template in BSE_PDF_URL_TEMPLATES:
        pdf_url = template.format(attachment)
        text = download_pdf_text(pdf_url, max_chars=max_chars, max_pages=max_pages)
        if text:
            return text
    return ""


def gemini_summarize(headline: str, subject: str, pdf_text: str) -> str:
    """
    Gemini API ko BSE filing ka text bhej ke 1-2 line ka Hinglish summary mangwata hai.
    Agar koi key nahi hai, ya sab keys fail ho jayein, toh khaali string return
    karta hai (caller fallback plain-text summary use karega).

    Multiple keys (GEMINI_API_KEYS) available hon toh -- ek key rate-limit (429)
    ya auth-error (401/403) de, toh turant agli key try karta hai. Sirf tab
    khaali string aayegi jab SAARI keys fail ho chuki hon.
    """
    if not GEMINI_API_KEYS:
        return ""

    context = f"Headline: {headline}\nSubject: {subject}\n\nDocument text:\n{pdf_text}"
    prompt = (
        "Neeche ek BSE (Bombay Stock Exchange) corporate filing ka text hai. "
        "Isko SIRF 1-2 lines mein Hinglish (Hindi-English mix, jaise ek trader/CA "
        "aapas mein baat karte hain) mein summarize karo. Sirf sabse important fact "
        "batao (jaise kis par kya action hua, kitna amount, kya faisla hua). "
        "Example style: 'Symbiotec mein promoter par insider trading case mila, "
        "SEBI ne 2 lakh ka penalty laga diya.' "
        "Koi extra explanation, disclaimer, ya heading mat do -- seedha 1-2 line ka jawab do.\n\n"
        f"{context}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    for idx, key in enumerate(GEMINI_API_KEYS):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={key}"
        )
        try:
            resp = requests.post(url, json=payload, timeout=GEMINI_TIMEOUT_SECONDS)

            if resp.status_code == 429:
                logger.warning(
                    f"Gemini key #{idx + 1} ka free-tier rate limit lag gaya, "
                    f"agli key try kar raha hoon."
                )
                continue

            if resp.status_code in (401, 403):
                logger.warning(
                    f"Gemini key #{idx + 1} invalid/unauthorized (status {resp.status_code}), "
                    f"agli key try kar raha hoon."
                )
                continue

            if resp.status_code != 200:
                logger.warning(f"Gemini API error {resp.status_code} (key #{idx + 1}): {resp.text[:200]}")
                continue

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                continue
            parts = candidates[0].get("content", {}).get("parts", [])
            text = " ".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
            # khaali response aaya -> agli key try karo

        except requests.RequestException as e:
            logger.warning(f"Gemini request fail hui (key #{idx + 1}): {e}")
            continue
        except (KeyError, ValueError) as e:
            logger.warning(f"Gemini response parse nahi hui (key #{idx + 1}): {e}")
            continue

    logger.warning(f"Saari {len(GEMINI_API_KEYS)} Gemini keys fail ho gayin, plain-text summary use karunga.")
    return ""


def gemini_generate(prompt: str) -> str:
    """Generic Gemini call (temperature 0, JSON-friendly). Keys rotate hoti hain, sab fail ho toh ''."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    for idx, key in enumerate(GEMINI_API_KEYS):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{GEMINI_MODEL}:generateContent?key={key}")
        try:
            resp = requests.post(url, json=payload, timeout=GEMINI_TIMEOUT_SECONDS * 2)
            if resp.status_code != 200:
                logger.warning(f"Gemini key #{idx + 1} status {resp.status_code}, agli key try kar raha hoon.")
                continue
            parts = (resp.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            text = " ".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
        except (requests.RequestException, ValueError) as e:
            # NOTE: e ko log mat karo seedha -- requests ke error mein URL (aur API key) aa sakti hai
            logger.warning(f"Gemini request fail (key #{idx + 1}): {type(e).__name__}")
    return ""


def try_results_analysis(item: dict, company: str, scrip: str, attachment: str, dt: str) -> str | None:
    """Financial Results filing ke liye YoY/QoQ analysis message. Kuch bhi fail ho toh None."""
    if not (RESULTS_ANALYSIS_ENABLED and results_analysis and GEMINI_API_KEYS and attachment):
        return None
    try:
        pdf_text = download_pdf_text_by_attachment(
            attachment, max_chars=RESULTS_PDF_MAX_CHARS, max_pages=RESULTS_PDF_MAX_PAGES)
        if not pdf_text:
            return None
        mktcap = get_market_cap(scrip)
        footer = (f"🏢 Market Cap: ₹{mktcap} Cr",) if mktcap else ()
        return results_analysis.build_results_analysis(
            company=company, scrip=scrip, pdf_text=pdf_text, llm_call=gemini_generate,
            filing_dt=str(dt or ""),
            pdf_url=f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}",
            footer_lines=footer,
        )
    except Exception as e:  # noqa: BLE001 -- analysis fail hone par normal message jaana chahiye
        logger.warning(f"Results analysis fail ({company}): {type(e).__name__}: {e}")
        return None


def build_summary(item: dict) -> str:
    """
    Announcement ke liye summary banata hai:
    - Agar GEMINI_API_KEY hai AUR PDF attachment hai -> PDF padh ke AI summary.
    - Warna -> purana plain-text (headline+subject trim karke) summary.
    """
    attachment = item.get("ATTACHMENTNAME") or item.get("Attachment") or ""
    headline = str(item.get("HEADLINE") or item.get("Headline") or "").strip()
    subject = str(item.get("NEWSSUB") or item.get("MORE") or "").strip()

    if GEMINI_API_KEYS and attachment:
        pdf_text = download_pdf_text_by_attachment(attachment)
        if pdf_text:
            ai_summary = gemini_summarize(headline, subject, pdf_text)
            if ai_summary:
                return ai_summary
            logger.info("Gemini se summary nahi mili, plain-text summary use kar raha hoon.")
        else:
            logger.info("PDF se text nahi nikal paya, plain-text summary use kar raha hoon.")
    elif not GEMINI_API_KEYS:
        logger.info("Koi Gemini API key set nahi hai, plain-text summary use ho raha hai.")
    elif not attachment:
        logger.info("Is announcement mein PDF attachment nahi hai, plain-text summary use ho raha hai.")

    return short_summary(item)


FILING_LABEL_KEYWORDS_MAP = dict(FILING_LABEL_RULES)


def _text_matches(combined_lower: str, label: str) -> bool:
    keywords = FILING_LABEL_KEYWORDS_MAP.get(label, [])
    return any(kw in combined_lower for kw in keywords)


def build_extra_details_line(item: dict, label: str | None, scrip: str, combined_text: str, mktcap: float | None) -> str:
    """
    Filing ke ACTUAL TEXT mein jo bhi topics mention hain (dividend, preferential
    issue, debt, order-win) unke hisaab se extra data-line(s) banata hai --
    header pe dikhne wale SINGLE label se independent hai. Isliye agar ek filing
    mein multiple cheezein combined hon (jaise "Preferential Issue + Resignation"
    ek hi announcement mein), header chahe "Resignation" dikhaye, ye function
    phir bhi preferential-issue wala amount dikha dega -- header label sirf
    DISPLAY ke liye hai, detail-detection alag se hoti hai.

    Market cap ab HAR message mein ek alag line (🏢) mein dikhta hai (caller
    fetch karke pass karta hai), isliye yahan dobara fetch nahi karte -- isse
    same scrip ke liye do baar BSE ko call nahi jaata.

    - Dividend -> amount/share + yield %
    - Warrants/Preferential Issue, Debt Issue, Investment in Subsidiary -> amount infused
    - Order Win -> order amount + (best-effort) TTM revenue (last 4 quarters jodke)
    Kisi bhi field ke liye data na mile toh us field ko chupchaap skip karta hai
    (galat/andaza wala number kabhi nahi dikhata).
    """
    combined_lower = combined_text.lower()
    parts: list[str] = []

    if _text_matches(combined_lower, "Dividend") and is_dividend_declared_filing(item):
        amount = extract_dividend_amount(combined_text)
        if amount:
            piece = f"₹{amount}/share"
            price = get_opening_price(scrip)
            if price:
                yield_pct = round((amount / price) * 100, 2)
                piece += f" · Yield: {yield_pct}%"
            parts.append(f"💰 {piece}")

    if (_text_matches(combined_lower, "Warrants/Preferential Issue")
            or _text_matches(combined_lower, "Debt Issue")
            or _text_matches(combined_lower, "Investment in Subsidiary")):
        amount_cr = extract_amount_crore(combined_text)
        if amount_cr:
            parts.append(f"💵 Amount infused: ₹{amount_cr} Cr")

    if _text_matches(combined_lower, "Order Win"):
        amount_cr = extract_amount_crore(combined_text)
        if amount_cr:
            parts.append(f"📦 Order value: ₹{amount_cr} Cr")

    return "\n".join(parts)


def format_announcement(item: dict) -> str:
    company = (
        item.get("SLONGNAME")
        or item.get("Slongname")
        or item.get("CompanyName")
        or item.get("scripname")
        or "Unknown Company"
    )
    scrip = str(item.get("SCRIP_CD") or item.get("scripcode") or "-")
    category = item.get("CATEGORYNAME") or item.get("Category") or ""
    dt = item.get("NEWS_DT") or item.get("DT_TM") or item.get("News_submission_dt") or ""
    attachment = item.get("ATTACHMENTNAME") or item.get("Attachment") or ""

    # Financial Results -> YoY/QoQ analysis message (fail ho toh neeche normal message)
    _res_text = (f"{item.get('HEADLINE') or ''} {item.get('NEWSSUB') or ''} {item.get('MORE') or ''}").lower()
    if classify_filing_label(item) == "Financial Results" and "result" in _res_text:
        analysis_msg = try_results_analysis(item, str(company), str(item.get("SCRIP_CD") or item.get("scripcode") or ""), attachment, dt)
        if analysis_msg:
            return analysis_msg

    summary = build_summary(item)

    headline = str(item.get("HEADLINE") or item.get("Headline") or "").strip()
    subject = str(item.get("NEWSSUB") or item.get("MORE") or "").strip()
    combined_text = f"{headline} {subject}"

    label = classify_filing_label(item)
    mktcap = get_market_cap(scrip)
    extra_line = build_extra_details_line(item, label, scrip, combined_text, mktcap)

    # Markdown special chars ko escape kar rahe hain taaki Telegram parse error na de
    def esc(s: str) -> str:
        s = str(s)
        for ch in ("_", "*", "[", "]", "`"):
            s = s.replace(ch, f"\\{ch}")
        return s

    # Label pehle priority mein dikhega (naya, reading-based naam). Agar
    # classify na kar paye, toh BSE ki apni category (jaisa pehle tha) dikhega.
    display_label = label or category

    # Financial Results ke liye Standalone/Consolidated tag header mein hi
    # dikha do, taaki user ko bina PDF khole pata chale kaunsa version hai.
    pdf_link_text = "PDF"
    if label == "Financial Results":
        variant = _result_variant(combined_text.lower())
        if variant == "consolidated":
            display_label += " (Consolidated)"
        elif variant == "standalone":
            display_label += " (Standalone)"
        pdf_link_text = "Full Results PDF"
        if not attachment:
            logger.info(
                f"Financial Results filing mila bina PDF attachment ke -- "
                f"scrip {scrip}, company {company}"
            )

    header = f"📢 *{esc(company)}*"
    if display_label:
        header += f" — {esc(display_label)}"

    msg = f"{header}\n{esc(summary)}"
    if extra_line:
        msg += f"\n{esc(extra_line)}"
    if mktcap:
        msg += f"\n🏢 Market Cap: ₹{mktcap} Cr"
    if dt:
        msg += f"\n🕒 {esc(dt)}"
    if attachment:
        pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}"
        msg += f" · [{pdf_link_text}]({pdf_url})"

    return msg


# --------------------------------------------------------------------------
# BSE fetch
# --------------------------------------------------------------------------

def fetch_todays_announcements() -> list:
    """
    Aaj ke saare announcements fetch karta hai (saari pages, paginated).
    bse library khud connection/session handle karti hai.
    """
    from bse import BSE  # lazy import taaki module-level import error clear dikhe

    today = datetime.now()
    all_items = []

    with BSE(str(DATA_DIR)) as bse_client:
        page_no = 1
        total_count = None

        while True:
            res = bse_client.announcements(
                page_no=page_no,
                from_date=today,
                to_date=today,
            )

            if total_count is None:
                try:
                    total_count = int(res["Table1"][0]["ROWCNT"])
                except (KeyError, IndexError, ValueError, TypeError):
                    total_count = 0

            page_items = res.get("Table", []) or []
            all_items.extend(page_items)

            if total_count == 0 or len(all_items) >= total_count or not page_items:
                break

            page_no += 1
            time.sleep(0.5)  # BSE par thoda polite raho, ek saath 50 requests mat maaro

    return all_items


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def run_check_cycle(seen_ids: set) -> set:
    last_err = None
    for attempt in range(1, MAX_RETRY_ON_ERROR + 1):
        try:
            items = fetch_todays_announcements()
            break
        except Exception as e:  # noqa: BLE001 -- BSE se koi bhi error aa sakta hai (Timeout/Connection/etc.)
            last_err = e
            logger.warning(f"Fetch fail (attempt {attempt}/{MAX_RETRY_ON_ERROR}): {e}")
            time.sleep(RETRY_BACKOFF_SECONDS)
    else:
        logger.error(f"Is cycle mein BSE se data nahi mil paya: {last_err}")
        return seen_ids

    stage_a = _load_json_list(DIVIDEND_STAGE_A_FILE)
    stage_b = _load_json_list(DIVIDEND_STAGE_B_FILE)
    dividend_changed = False

    fr_tracking = _load_json_list(FINANCIAL_RESULTS_TRACK_FILE)
    fr_changed = False

    new_items = []
    skipped_unimportant = 0
    for item in items:
        aid = make_announcement_id(item)
        if aid in seen_ids:
            continue
        seen_ids.add(aid)  # ek baar dekh liya, dobara nahi dekhenge (important ho ya na ho)

        # Dividend tracker: normal alert filter se independent hai, isliye
        # har naye item ko yahan alag se check karte hain.
        if is_dividend_declared_filing(item):
            stage_a, stage_b = process_stage_b(item, stage_a, stage_b)
            dividend_changed = True
        elif is_board_meeting_dividend_filing(item):
            stage_a = process_stage_a(item, stage_a)
            dividend_changed = True

        if not is_important(item):
            skipped_unimportant += 1
            continue

        # Financial Results: Standalone/Consolidated priority -- agar
        # Consolidated already bhej chuke is scrip ke liye aaj, toh Standalone
        # ab skip karo (user ko sirf behtar wala chahiye).
        if classify_filing_label(item) == "Financial Results":
            scrip = str(item.get("SCRIP_CD") or item.get("scripcode") or "")
            headline = str(item.get("HEADLINE") or item.get("Headline") or "")
            subject = str(item.get("NEWSSUB") or item.get("MORE") or "")
            should_send, fr_tracking = should_send_financial_result(scrip, f"{headline} {subject}", fr_tracking)
            fr_changed = True
            if not should_send:
                continue

        new_items.append(item)

    if dividend_changed:
        stage_b = remove_expired_stage_b(stage_b)
        _save_json_list(DIVIDEND_STAGE_A_FILE, stage_a)
        _save_json_list(DIVIDEND_STAGE_B_FILE, stage_b)

    if fr_changed:
        _save_json_list(FINANCIAL_RESULTS_TRACK_FILE, fr_tracking)

    if not new_items:
        logger.info(
            f"Koi naya IMPORTANT announcement nahi "
            f"(total aaj: {len(items)}, is cycle mein skip kiye: {skipped_unimportant})"
        )
        if skipped_unimportant:
            save_seen_ids(seen_ids)
        return seen_ids

    logger.info(f"{len(new_items)} naye IMPORTANT announcements mile, Telegram par bhej raha hoon...")

    for item in new_items:
        text = format_announcement(item)
        ok = send_telegram_message(text)
        if ok:
            logger.info(f"Sent: {item.get('SLONGNAME') or item.get('scripname') or '?'}")
        else:
            logger.error(f"Send fail: {item.get('SLONGNAME') or '?'} -- agle cycle me retry nahi hoga (already seen marked)")
        time.sleep(4)  # Telegram flood-limit + Gemini free-tier RPM dono ke liye safe gap

    save_seen_ids(seen_ids)
    return seen_ids


def prime_seen_ids_on_startup(seen_ids: set) -> set:
    """
    Bot start hote hi, aaj tak ke saare announcements ko 'seen' mark kar do
    (Telegram par bheje bina). Isse bot sirf ab ke baad ki NAYI filings
    hi bhejega -- purani (jo start se pehle hi aa chuki thi) skip ho jayengi.
    """
    logger.info("Startup: aaj tak ke maujooda announcements check kar raha hoon (bhejunga nahi, bas mark karunga)...")
    try:
        items = fetch_todays_announcements()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Startup priming fail hui (BSE se connect nahi ho paya): {e}. "
                        f"Chinta mat karo, agle check cycle mein normal filter chalega.")
        return seen_ids

    added = 0
    for item in items:
        aid = make_announcement_id(item)
        if aid not in seen_ids:
            seen_ids.add(aid)
            added += 1

    save_seen_ids(seen_ids)
    logger.info(f"{added} pehle se maujood announcements skip kar diye gaye (Telegram par nahi bheje).")
    return seen_ids


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error(
            "TELEGRAM_BOT_TOKEN ya TELEGRAM_CHAT_ID missing hai. "
            "Isi folder mein .env file banao (README dekho)."
        )
        sys.exit(1)

    logger.info("=" * 55)
    logger.info("  BSE Announcements -> Telegram Bot Started")
    logger.info(f"  Poll interval: {POLL_INTERVAL_SECONDS}s")
    logger.info("=" * 55)

    seen_ids = load_seen_ids()
    logger.info(f"Pehle se {len(seen_ids)} announcements 'seen' hai (dedup list)")

    if ONLY_NEW_SINCE_STARTUP:
        seen_ids = prime_seen_ids_on_startup(seen_ids)

    if DIVIDEND_BOT_TOKEN and DIVIDEND_CHAT_ID:
        stage_a = _load_json_list(DIVIDEND_STAGE_A_FILE)
        stage_b = _load_json_list(DIVIDEND_STAGE_B_FILE)
        stage_a, stage_b = run_dividend_backfill_once(stage_a, stage_b)

    send_telegram_message("✅ BSE announcements bot start ho gaya hai. Ab yahan se aage ki NAYI important filings hi milengi.")

    while True:
        cycle_start = time.time()
        try:
            seen_ids = run_check_cycle(seen_ids)
            if DIVIDEND_BOT_TOKEN and DIVIDEND_CHAT_ID:
                stage_a = _load_json_list(DIVIDEND_STAGE_A_FILE)
                stage_b = _load_json_list(DIVIDEND_STAGE_B_FILE)
                maybe_send_daily_digest(stage_a, stage_b)
        except KeyboardInterrupt:
            logger.info("Ctrl+C mila, bot band kar raha hoon.")
            break
        except Exception as e:  # noqa: BLE001 -- kabhi bhi crash na ho, bas log karke aage badho
            logger.exception(f"Unexpected error, agle cycle mein try karunga: {e}")

        elapsed = time.time() - cycle_start
        wait = max(5, POLL_INTERVAL_SECONDS - elapsed)
        time.sleep(wait)


if __name__ == "__main__":
    main()
