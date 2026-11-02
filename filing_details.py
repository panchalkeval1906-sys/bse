"""
filing_details.py -- BSE filing se key numbers nikaalta hai (amount, per-share, ratio, record date)
aur chahe toh 1-2 line ka Hinglish summary bhi, EK hi Gemini call mein.

Ye module akela hai: koi side-effect nahi, koi import nahi (bot ya tracker ka). Gemini ka function
bahar se milta hai (llm_call), isliye bot ki key-rotation wahi use hoti hai.

Kis filing se kya nikalta hai:
  Order Win     -> amount_cr (order value, Rs Crore)
  Dividend      -> per_share (Rs), record_date, amount_cr (agar likha ho)
  Buyback       -> amount_cr (total size), per_share (buyback price), shares_ratio (kitne shares),
                   record_date, buyback_mode ("Tender Offer" ya "Open Market")
  Rights Issue  -> shares_ratio (jaise "1:4"), per_share (issue price), amount_cr (issue size), record_date
  Bonus Issue   -> shares_ratio (jaise "1:1"), record_date

Rule: sirf wahi value jo document mein SAAF likhi ho. Pakka na ho toh None (khaali) -- galat number
se khaali cell behtar hai.
"""

import json
import re
from datetime import datetime

MAX_TEXT_CHARS = 12000

# Kis label mein kaun si field matter karti hai. Baaki hamesha None rahegi.
APPLICABLE = {
    "Order Win":    {"amount_cr"},
    "Dividend":     {"per_share", "amount_cr", "record_date"},
    "Buyback":      {"amount_cr", "per_share", "shares_ratio", "record_date", "buyback_mode"},
    "Rights Issue": {"amount_cr", "per_share", "shares_ratio", "record_date"},
    "Bonus Issue":  {"shares_ratio", "record_date"},
}

RULES = {
    "Order Win": (
        "amount_cr = order/contract ki total value Rs CRORE mein. Conversion: Rs 50 lakh = 0.5, "
        "Rs 25,00,00,000 = 25, Rs 120 million = 12. Value foreign currency (USD etc.) mein ho ya likhi "
        "hi na ho toh null. Sirf is order ki value lo, company ka turnover ya purane orders nahi."
    ),
    "Dividend": (
        "per_share = dividend per equity share Rs mein (jaise 'Rs 2.50 per share' = 2.5). "
        "Face value dividend NAHI hota ('face value Rs 10 each' ko mat uthao). Agar dividend % mein "
        "likha hai AUR face value bhi diya hai toh per_share = face value x %. "
        "amount_cr = total dividend payout Rs crore mein, sirf agar likha ho. "
        "record_date = record date (YYYY-MM-DD)."
    ),
    "Buyback": (
        "amount_cr = buyback ka total size (maximum amount) Rs crore mein. "
        "per_share = buyback price per share Rs mein. "
        "shares_ratio = kitne shares buyback honge (poora number, integer, bina comma). "
        "record_date = record date agar diya ho (YYYY-MM-DD). "
        "buyback_mode = exactly 'Tender Offer' (shareholders se record date pe tender route se) ya "
        "'Open Market' (stock exchange ke through). Document mein saaf na likha ho toh null. "
        "Agar filing sirf daily/weekly progress report hai (kitne shares kharide ho chuke) toh yeh "
        "saari numeric fields null rakho, sirf summary likho."
    ),
    "Rights Issue": (
        "shares_ratio = rights entitlement ratio, format 'a:b' (jaise '1:4' = har 4 shares pe 1 naya). "
        "per_share = rights issue price Rs mein. amount_cr = total issue size Rs crore mein. "
        "record_date = record date (YYYY-MM-DD). Agar filing sirf newspaper advertisement ya "
        "intimation hai aur numbers nahi diye, toh null."
    ),
    "Bonus Issue": (
        "shares_ratio = bonus ratio, format 'a:b' (jaise '1:1' = har 1 share pe 1 bonus share). "
        "record_date = record date (YYYY-MM-DD)."
    ),
}


def _prompt(label: str, headline: str, subject: str, text: str, want_summary: bool) -> str:
    keys = ["amount_cr", "per_share", "shares_ratio", "record_date"]
    if label == "Buyback":
        keys.append("buyback_mode")
    if want_summary:
        keys = ["summary"] + keys
    example = "{" + ", ".join(f'"{k}": null' for k in keys) + "}"
    summary_rule = (
        "summary = SIRF 1-2 line ka Hinglish (Hindi-English mix, jaise ek trader/CA baat karta hai) "
        "summary: sabse important fact aur amount/ratio. Koi heading ya disclaimer nahi.\n"
        if want_summary else ""
    )
    return (
        "Neeche ek BSE corporate filing hai (type: " + label + "). Isse key numbers nikaalo aur "
        "SIRF ek JSON object do, bilkul isi format mein (koi extra text nahi):\n"
        + example + "\n\n"
        + summary_rule
        + RULES.get(label, "") + "\n"
        "General rules: sirf wahi value do jo document mein SAAF likhi ho. Guess ya andaaza mat lagao; "
        "pakka na ho toh null. Numbers plain (comma, Rs ya % symbol nahi). Dates YYYY-MM-DD.\n\n"
        f"Headline: {headline}\nSubject: {subject}\n\nDocument text:\n{text}"
    )


def _num(v, lo: float, hi: float):
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(str(v).replace(",", "").replace("Rs", "").replace("\u20b9", "").strip())
    except ValueError:
        return None
    return round(x, 4) if lo < x < hi else None


def _ratio(v):
    m = re.search(r"(\d+)\s*[:/]\s*(\d+)", str(v or ""))
    if not m or int(m.group(1)) <= 0 or int(m.group(2)) <= 0:
        return None
    return f"{int(m.group(1))}:{int(m.group(2))}"


def _shares(v):
    x = _num(v, 0, 1e12)
    return int(x) if x else None


def _date(v):
    s = str(v or "").strip()[:10]
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def _mode(v):
    s = str(v or "").lower()
    if "tender" in s:
        return "Tender Offer"
    if "open" in s:
        return "Open Market"
    return None


def _parse_json(raw: str):
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.MULTILINE).strip()
    try:
        data = json.loads(s)
    except ValueError:
        m = re.search(r"\{.*\}", s, flags=re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
    if isinstance(data, list) and data:
        data = data[0]
    return data if isinstance(data, dict) else None


def extract(label: str, headline: str, subject: str, pdf_text: str, llm_call,
            want_summary: bool = True, fallback=None) -> dict:
    """
    Returns: {"summary", "amount_cr", "per_share", "shares_ratio", "record_date", "buyback_mode", "ok"}
    ok=False matlab Gemini se jawab nahi aaya / parse nahi hua (caller retry kar sakta hai).
    fallback: bot module (sirf tab use hota hai jab Gemini bilkul fail ho) -- sirf dividend per-share
    aur record date ke liye, kyunki unke regex careful bane hain.
    """
    out = {"summary": "", "amount_cr": None, "per_share": None, "shares_ratio": None,
           "record_date": None, "buyback_mode": None, "ok": False}
    allowed = APPLICABLE.get(label)
    if not allowed:
        return out

    text = (pdf_text or "")[:MAX_TEXT_CHARS]
    try:
        raw = llm_call(_prompt(label, headline or "", subject or "", text, want_summary))
    except Exception:  # noqa: BLE001
        raw = ""
    data = _parse_json(raw)

    if data is not None:
        out["ok"] = True
        out["summary"] = str(data.get("summary") or "").strip()[:300] if want_summary else ""
        if "amount_cr" in allowed:
            out["amount_cr"] = _num(data.get("amount_cr"), 0, 1e6)
        if "per_share" in allowed:
            out["per_share"] = _num(data.get("per_share"), 0, 1e5)
        if "shares_ratio" in allowed:
            out["shares_ratio"] = _shares(data.get("shares_ratio")) if label == "Buyback" \
                else _ratio(data.get("shares_ratio"))
        if "record_date" in allowed:
            out["record_date"] = _date(data.get("record_date"))
        if "buyback_mode" in allowed:
            out["buyback_mode"] = _mode(data.get("buyback_mode"))
    elif fallback is not None and text:
        try:
            if label == "Dividend":
                out["per_share"] = _num(fallback.extract_dividend_amount(text), 0, 1e5)
            if "record_date" in allowed:
                out["record_date"] = _date(fallback.extract_record_date(text))
        except Exception:  # noqa: BLE001
            pass
    return out
