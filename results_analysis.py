"""
results_analysis.py -- Financial Results ka Revenue / Net Profit analysis
=========================================================================

Kya karta hai:
    1) Results PDF ka text Gemini ko deta hai aur sirf numbers (JSON) wapas maangta hai:
       current quarter ka Revenue + Net Profit (aur PDF ke baaki columns bhi).
    2) Pichle numbers (YoY: same quarter pichle saal, QoQ: pichla quarter) Screener se
       leta hai (screener.in/company/<BSE scrip code>/), jahan saari quarters ek page par
       hoti hain. Screener fail ho toh PDF ke apne columns fallback ke taur par use hote hain.
    3) Growth % PYTHON nikalta hai (AI nahi) aur tumhare format mein Telegram message banata hai.

Rule: koi bhi number na mile / bharosemand na ho toh wo line "N/A" dikhegi -- galat
number kabhi nahi dikhayega.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import time
from datetime import date, datetime

import requests

logger = logging.getLogger("bse_bot")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

SCREENER_BASE = "https://www.screener.in/company"
SCREENER_TIMEOUT_SECONDS = 20
SCREENER_CACHE_TTL_SECONDS = 6 * 3600     # ek company ka page 6 ghante tak cache
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}
_screener_cache: dict = {}

# 1 unit = kitne Crore
UNIT_TO_CRORE = {
    "crore": 1.0, "crores": 1.0, "cr": 1.0,
    "lakh": 0.01, "lakhs": 0.01, "lac": 0.01, "lacs": 0.01,
    "million": 0.1, "millions": 0.1,
    "thousand": 0.0001, "thousands": 0.0001,
    "rupee": 1e-7, "rupees": 1e-7,
}


# --------------------------------------------------------------------------
# Screener: fetch + parse
# --------------------------------------------------------------------------

def _to_float(s: str) -> float | None:
    s = (s or "").replace(",", "").replace("%", "").replace("\u00a0", " ").strip()
    if not s or s in ("-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_screener_html(html: str) -> dict | None:
    """
    Screener page ke 'Quarterly Results' table se:
      {"variant": "consolidated"|"standalone"|"unmarked",
       "quarters": {"2026-03-31": {"revenue": 0.0, "net_profit": -0.05, "eps": -0.03}, ...}}
    Table na mile toh None. Values Rs. Crore mein hain (Screener ka apna unit).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    sec = soup.find(id="quarters")
    if sec is None:
        return None

    sub = sec.find("p", class_="sub")
    sub_text = sub.get_text(" ", strip=True).lower() if sub else ""
    if "consolidated figures" in sub_text:
        variant = "consolidated"
    elif "standalone figures" in sub_text:
        variant = "standalone"
    else:
        variant = "unmarked"   # sirf ek hi version hota hai toh Screener kuch likhta nahi

    table = sec.find("table")
    if table is None or table.find("thead") is None or table.find("tbody") is None:
        return None

    keys = [th.get("data-date-key") for th in table.find("thead").find_all("th")][1:]

    wanted = {"sales": "revenue", "revenue": "revenue", "net profit": "net_profit", "eps in rs": "eps"}
    quarters: dict = {k: {} for k in keys if k}

    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).replace("+", "").replace("\u00a0", " ")
        label = re.sub(r"\s+", " ", label).strip().lower()
        field = wanted.get(label)
        if not field:
            continue
        for key, td in zip(keys, cells[1:]):
            if key:
                val = _to_float(td.get_text(" ", strip=True))
                if val is not None:
                    quarters[key].setdefault(field, val)

    quarters = {k: v for k, v in quarters.items() if v}
    return {"variant": variant, "quarters": quarters} if quarters else None


def fetch_screener_quarters(scrip: str, want_variant: str) -> dict | None:
    """
    Screener se quarterly data laata hai. Sirf tab return karta hai jab page ka variant
    (consolidated/standalone) filing ke variant se match kare -- warna None, taaki
    standalone vs consolidated ka galat comparison kabhi na ho.
    """
    cache_key = (scrip, want_variant)
    hit = _screener_cache.get(cache_key)
    if hit and time.time() - hit[0] < SCREENER_CACHE_TTL_SECONDS:
        return hit[1]

    if want_variant == "consolidated":
        urls = [f"{SCREENER_BASE}/{scrip}/consolidated/", f"{SCREENER_BASE}/{scrip}/"]
    else:
        urls = [f"{SCREENER_BASE}/{scrip}/"]

    result = None
    for url in urls:
        try:
            time.sleep(1.0)  # Screener par polite raho
            resp = requests.get(url, headers=_HEADERS, timeout=SCREENER_TIMEOUT_SECONDS)
            if resp.status_code != 200:
                logger.warning(f"Screener {resp.status_code} for {url}")
                continue
            parsed = parse_screener_html(resp.text)
            if not parsed:
                logger.warning(f"Screener page parse nahi hua: {url}")
                continue
            v = parsed["variant"]
            if v == want_variant or (v == "unmarked" and want_variant == "standalone"):
                result = parsed["quarters"]
                break
            logger.info(f"Screener variant mismatch ({v} vs filing {want_variant}) for {url}")
        except Exception as e:  # noqa: BLE001 -- network/parse kuch bhi ho, crash nahi
            logger.warning(f"Screener fetch fail {url}: {e}")

    _screener_cache[cache_key] = (time.time(), result)
    return result


# --------------------------------------------------------------------------
# Date helpers
# --------------------------------------------------------------------------

def month_end_shift(d: date, months_back: int) -> date:
    y, m = d.year, d.month - months_back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, calendar.monthrange(y, m)[1])


def quarter_labels(period_end: date) -> tuple[str, str]:
    """(June 26, Q1 FY27) -- Indian financial year (Apr-Mar)."""
    short = period_end.strftime("%B %y")
    fy = period_end.year + 1 if period_end.month >= 4 else period_end.year
    q = ((period_end.month - 4) % 12) // 3 + 1
    return short, f"Q{q} FY{str(fy)[-2:]}"


def _parse_iso(s) -> date | None:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# LLM extraction (PDF text -> numbers)
# --------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are extracting numbers from an Indian listed company's quarterly financial \
results filing (BSE). Return ONLY a JSON object, no markdown, no explanation.

Rules:
- Use the "Statement of (Standalone|Consolidated) (Un)audited Financial Results" table. If the document has BOTH \
standalone and consolidated tables, use the CONSOLIDATED one.
- "revenue" = Revenue from operations (NOT Total income). Only if the company has no "Revenue from operations" \
line (banks/NBFCs/insurers), use Total income and set "revenue_basis" to "total income".
- "net_profit" = Net Profit/(Loss) for the period after tax (the "Total profit (loss) for period" line). \
Do NOT use Total Comprehensive Income, and do NOT use profit before tax.
- The table has several columns (current quarter, preceding quarter, same quarter previous year, year-to-date, \
previous year). Identify columns by the DATES in the headers, not by position. Give each block its own period_end.
- Numbers in brackets like (5.02) are NEGATIVE: output -5.02. Remove commas. Use null if a value is missing.
- "unit" is the unit stated in the table header: one of "lakhs", "crores", "millions", "thousands", "rupees".
- Only the 3-month QUARTER columns. Never use half-year, nine-month or full-year columns.

Schema:
{"variant": "consolidated" or "standalone",
 "unit": "lakhs",
 "revenue_basis": "revenue from operations",
 "current":      {"period_end": "YYYY-MM-DD", "revenue": number or null, "net_profit": number or null},
 "prev_quarter": {"period_end": "YYYY-MM-DD", "revenue": number or null, "net_profit": number or null},
 "year_ago":     {"period_end": "YYYY-MM-DD", "revenue": number or null, "net_profit": number or null}}

Filing text:
"""


def to_crore(value, unit: str) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    factor = UNIT_TO_CRORE.get(str(unit or "").strip().lower())
    return None if factor is None else v * factor


def parse_llm_json(raw: str) -> dict | None:
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# Comparison text
# --------------------------------------------------------------------------

def _amt(v: float) -> str:
    return f"-₹{abs(v):,.2f} Cr" if v < 0 else f"₹{v:,.2f} Cr"


def _change_phrase(cur: float, base: float) -> str:
    ch = (cur - base) / base * 100
    if abs(ch) < 0.05:
        return "was flat"
    return f"{'grew' if ch > 0 else 'declined'} by {abs(ch):.1f}%"


def revenue_text(cur, base) -> str:
    if cur is None or base is None:
        return "Revenue: N/A (data nahi mila)"
    pair = f"({_amt(cur)} vs {_amt(base)})"
    if base <= 0 or cur < 0:
        return f"Revenue: N/A (% banta nahi) {pair}"
    return f"Revenue {_change_phrase(cur, base)}  {pair}"


def profit_text(cur, base) -> str:
    if cur is None or base is None:
        return "Net profit: N/A (data nahi mila)"
    pair = f"({_amt(cur)} vs {_amt(base)})"
    if base > 0 and cur >= 0:
        return f"Net profit {_change_phrase(cur, base)}  {pair}"
    if base > 0 and cur < 0:
        return f"Net profit turned into a LOSS  {pair}"
    if base < 0 and cur >= 0:
        return f"Net profit turned POSITIVE (loss se profit)  {pair}"
    if base < 0 and cur < 0:
        word = "narrowed" if cur > base else ("widened" if cur < base else "unchanged")
        return f"Net loss {word}  {pair}"
    return f"Net profit: N/A (base zero hai) {pair}"


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.02, 0.03 * max(abs(a), abs(b)))


# --------------------------------------------------------------------------
# Unit sanity guard
# --------------------------------------------------------------------------

_POWERS_OF_TEN = (100.0, 0.01, 10.0, 0.1, 1000.0, 0.001)
UNIT_CHECK_MIN_CRORE = 0.5   # isse chhote Screener numbers 2-decimal rounding ki wajah se verify nahi ho sakte


def detect_unit_scale(data: dict, unit: str, screener: dict, qoq_date: date, yoy_date: date) -> float | None:
    """
    PDF ke pichle-quarter/saal-purane columns (jo LLM ke diye unit se Crore mein convert hue)
    ko Screener ke same quarter se match karta hai.
      - ~1x      -> unit sahi hai, return 1.0
      - ~10/100/1000x (ya 1/10, 1/100, 1/1000) -> LLM ne unit galat padha; return correction
        factor (current numbers is se multiply honge)
      - verify nahi ho paya / unclear -> None
    """
    ratios = []
    for blk_name, expected in (("prev_quarter", qoq_date), ("year_ago", yoy_date)):
        blk = data.get(blk_name) or {}
        if _parse_iso(blk.get("period_end")) != expected:
            continue
        s_row = screener.get(expected.isoformat(), {})
        for f in ("revenue", "net_profit"):
            pv, sv = to_crore(blk.get(f), unit), s_row.get(f)
            if pv is None or sv is None or abs(sv) < UNIT_CHECK_MIN_CRORE or pv == 0:
                continue
            if (pv > 0) != (sv > 0):
                continue
            ratios.append(abs(pv) / abs(sv))
    if not ratios:
        return None

    def near(r: float, target: float) -> bool:
        return abs(r / target - 1) <= 0.15

    if all(near(r, 1.0) for r in ratios):
        return 1.0
    for k in _POWERS_OF_TEN:
        if all(near(r, k) for r in ratios):
            return 1.0 / k        # PDF-based number = sahi * k  ->  sahi = number / k
    return None


# --------------------------------------------------------------------------
# Growth % (price tracker / Google Sheet ke liye number form mein)
# --------------------------------------------------------------------------

def revenue_growth_pct(cur, base):
    """Revenue growth %, 1 decimal. Base <= 0 ya data nahi -> None."""
    if cur is None or base is None or base <= 0 or cur < 0:
        return None
    return round((cur - base) / base * 100, 1)


def profit_growth_value(cur, base):
    """
    Net profit growth: base > 0 ho toh % number (profit se loss mein jaaye toh bhi number, jaise -150.0).
    Base loss/zero ho toh text: Turnaround / Loss narrowed / Loss widened. Data nahi -> None.
    """
    if cur is None or base is None:
        return None
    if base > 0:
        return round((cur - base) / base * 100, 1)
    if base < 0:
        if cur >= 0:
            return "Turnaround"
        return "Loss narrowed" if cur > base else ("Loss widened" if cur < base else None)
    return None


# --------------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------------

def _md(s: str) -> str:
    s = str(s)
    for ch in ("_", "*", "[", "]", "`"):
        s = s.replace(ch, f"\\{ch}")
    return s


def build_results_analysis(company: str, scrip: str, pdf_text: str, llm_call,
                           filing_dt: str = "", pdf_url: str = "",
                           footer_lines: tuple = (), metrics_out: dict | None = None) -> str | None:
    """
    Returns: Telegram (Markdown) message, ya None agar numbers bharosemand tareeke se
    nikal nahi paaye (caller tab purana normal message bhej dega).
    llm_call(prompt) -> str : Gemini ka raw text response.
    metrics_out (optional dict): agar diya toh usme growth numbers bhar deta hai (price tracker ke liye):
      label, yoy_rev, yoy_profit, qoq_rev, qoq_profit. Message ka format isse nahi badalta.
    """
    if not pdf_text:
        return None

    data = parse_llm_json(llm_call(EXTRACTION_PROMPT + pdf_text))
    if not data:
        logger.info("Results analysis: LLM se valid JSON nahi mila.")
        return None

    unit = data.get("unit")
    variant = str(data.get("variant") or "").lower()
    if variant not in ("consolidated", "standalone"):
        logger.info("Results analysis: variant clear nahi.")
        return None

    cur_blk = data.get("current") or {}
    period_end = _parse_iso(cur_blk.get("period_end"))
    cur_rev = to_crore(cur_blk.get("revenue"), unit)
    cur_np = to_crore(cur_blk.get("net_profit"), unit)
    if period_end is None or (cur_rev is None and cur_np is None):
        logger.info("Results analysis: current quarter ke numbers/period nahi mile.")
        return None

    yoy_date = month_end_shift(period_end, 12)
    qoq_date = month_end_shift(period_end, 3)

    screener = fetch_screener_quarters(scrip, variant) or {}

    unit_note = ""
    scale = detect_unit_scale(data, unit, screener, qoq_date, yoy_date)
    if scale is None:
        logger.info(f"{company}: unit verify nahi ho paya (Screener se overlap nahi / chhote numbers).")
    elif scale != 1.0:
        logger.warning(f"{company}: LLM ka unit '{unit}' galat lag raha hai, numbers {scale}x se correct kar raha hoon.")
        cur_rev = cur_rev * scale if cur_rev is not None else None
        cur_np = cur_np * scale if cur_np is not None else None
        unit_note = f"Note: PDF ka unit auto-correct hua ({scale:g}x) -- ek baar PDF se verify kar lena"

    def pdf_block(expected: date) -> dict:
        blk = data.get("prev_quarter" if expected == qoq_date else "year_ago") or {}
        if _parse_iso(blk.get("period_end")) != expected:
            return {}   # PDF ka column expected quarter se match nahi karta -> use mat karo
        r, n = to_crore(blk.get("revenue"), unit), to_crore(blk.get("net_profit"), unit)
        k = scale if scale is not None else 1.0
        return {"revenue": r * k if r is not None else None,
                "net_profit": n * k if n is not None else None}

    used_pdf_fallback = False

    def baseline(expected: date) -> dict:
        nonlocal used_pdf_fallback
        s = screener.get(expected.isoformat(), {})
        p = pdf_block(expected)
        out = {}
        for f in ("revenue", "net_profit"):
            sv, pv = s.get(f), p.get(f)
            if sv is not None and pv is not None and not _close(sv, pv):
                logger.warning(f"{company}: {expected} {f} Screener={sv} vs PDF={pv} mismatch -- Screener use kar raha hoon")
            if sv is not None:
                out[f] = sv
            elif pv is not None:
                out[f] = pv
                used_pdf_fallback = True
        return out

    yoy, qoq = baseline(yoy_date), baseline(qoq_date)

    short, fyq = quarter_labels(period_end)

    if metrics_out is not None:
        try:
            metrics_out.update({
                "label": f"{fyq} ({variant.capitalize()})",
                "yoy_rev": revenue_growth_pct(cur_rev, yoy.get("revenue")),
                "yoy_profit": profit_growth_value(cur_np, yoy.get("net_profit")),
                "qoq_rev": revenue_growth_pct(cur_rev, qoq.get("revenue")),
                "qoq_profit": profit_growth_value(cur_np, qoq.get("net_profit")),
            })
        except Exception as e:  # noqa: BLE001 -- tracker ke numbers fail hon toh message pe asar nahi
            logger.warning(f"{company}: tracker metrics nahi bane: {e}")
    y_short, _ = quarter_labels(yoy_date)
    q_short, _ = quarter_labels(qoq_date)

    lines = [f"📊 *{_md(company)}* — {fyq} ({variant.capitalize()})",
             f"Quarter : {short}", ""]
    lines += [f"*YoY ({short} vs {y_short})*",
              f"1. {revenue_text(cur_rev, yoy.get('revenue'))}",
              f"2. {profit_text(cur_np, yoy.get('net_profit'))}", "",
              f"*QoQ ({short} vs {q_short})*",
              f"1. {revenue_text(cur_rev, qoq.get('revenue'))}",
              f"2. {profit_text(cur_np, qoq.get('net_profit'))}", ""]

    # ---- Extra analysis (sirf real numbers se) ----
    extra = []
    if cur_rev and cur_rev > 0 and cur_np is not None:
        margin = cur_np / cur_rev * 100
        parts = []
        for tag, base in (("YoY", yoy), ("QoQ", qoq)):
            r, n = base.get("revenue"), base.get("net_profit")
            if r and r > 0 and n is not None:
                parts.append(f"{tag} {n / r * 100:.1f}%")
        extra.append(f"Net margin: {margin:.1f}%" + (f" ({', '.join(parts)})" if parts else ""))

    ttm_dates = [month_end_shift(period_end, k) for k in (3, 6, 9)]
    if cur_rev is not None and cur_np is not None and all(
            screener.get(d.isoformat(), {}).get("revenue") is not None
            and screener.get(d.isoformat(), {}).get("net_profit") is not None for d in ttm_dates):
        ttm_rev = cur_rev + sum(screener[d.isoformat()]["revenue"] for d in ttm_dates)
        ttm_np = cur_np + sum(screener[d.isoformat()]["net_profit"] for d in ttm_dates)
        extra.append(f"TTM (last 4 quarters): Revenue {_amt(ttm_rev)} · Net profit {_amt(ttm_np)}")

    if str(data.get("revenue_basis") or "").lower().startswith("total income"):
        extra.append("Note: revenue = Total income (company ke paas 'Revenue from operations' line nahi)")
    if unit_note:
        extra.append(unit_note)
    if used_pdf_fallback:
        extra.append("Note: kuch purane numbers Screener ke bajaye PDF se liye gaye")

    if extra:
        lines.append("*Extra Analysis*")
        lines += [f"- {e}" for e in extra]
        lines.append("")

    lines += [l for l in footer_lines if l]
    tail = f"🕒 {_md(filing_dt)}" if filing_dt else ""
    if pdf_url:
        tail += f"{' · ' if tail else ''}[Full Results PDF]({pdf_url})"
    if tail:
        lines.append(tail)

    return "\n".join(lines).rstrip()
