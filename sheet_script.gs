// BSE Filing Tracker -- Google Apps Script (v6)
//
// DO TABS, DO ALAG LAYOUT:
//  "Tracker"  (live bot)  : v4 jaisa hi -- A..O fixed, phir Day 0..7 x (Open High Low Close), last ID.   [KUCH NAHI BADLA]
//  "Backtest" (backfill)  : v5 -- A..O fixed (v4 jaisa) + P..V naye analysis columns,
//                           phir har Day ke 8 columns: Open | High | High vs Open % | Low | Low vs Open % |
//                           Close | Close vs Open % | Volume (x avg)  -- % columns sheet ke formulas hain. Last: ID.
//  Header 2 rows, data row 3 se.
//
// Backtest ke naye columns (P..V):
//   P Dividend Yield (%)            = per share / pichla close x 100
//   Q Order Value / Mkt Cap (%)     (Order Win)
//   R Buyback Value / Mkt Cap (%)   (Buyback)
//   S Buyback Price vs Market (%)   = buyback price / pichla close - 1  (premium)
//   T Buyback Mode                  Tender Offer / Open Market
//   U Avg Volume (20d)              filing se pehle ke 20 trading din ka average volume
//   V Prev Close (Rs)               filing se pehle ka aakhri close (reference price)
//   W Pre 1W Return (%)             filing se pehle ke 7 calendar din ka return (Prev Close vs 7 din pehle ka close)
//   X Pre 15D Return (%)            filing se pehle ke 15 calendar din ka return
//   Y Pre 1M Return (%)             filing se pehle ke 30 calendar din ka return
//   Z Pre 5D Volume (x avg)         filing se pehle ke 5 trading din ka avg volume / 20-din avg (move pehle hi aa chuka?)

const SECRET = "TrkgjjVJo0waPlyqeIGo3sNr";   // server .env ke GOOGLE_SHEET_SECRET se match hona chahiye
const TAB = "Tracker";
const ALLOWED_TABS = ["Tracker", "Backtest"];
const DAYS = 8;                                   // Day 0 .. Day 7
const HEADER_ROWS = 2;
const FIRST_DATA_ROW = HEADER_ROWS + 1;           // 3

const V4_FIXED = ["Company", "Filing Date", "Filing Time", "Filing Type", "Summary", "Scrip Code",
                  "Amount (Rs Cr)", "Per Share (Rs)", "Shares / Ratio", "Record Date",
                  "Market Cap (Rs Cr)",
                  "YoY Revenue Growth (%)", "YoY Profit Growth (%)",
                  "QoQ Revenue Growth (%)", "QoQ Profit Growth (%)"];
const BT_EXTRA = ["Dividend Yield (%)", "Order Value / Mkt Cap (%)", "Buyback Value / Mkt Cap (%)",
                  "Buyback Price vs Market (%)", "Buyback Mode", "Avg Volume (20d)", "Prev Close (Rs)",
                  "Pre 1W Return (%)", "Pre 15D Return (%)", "Pre 1M Return (%)", "Pre 5D Volume (x avg)"];

function mkCfg_(fixed, dayLabels, isBt) {
  const nFixed = fixed.length;
  const per = dayLabels.length;
  return { fixed: fixed, dayLabels: dayLabels, per: per, nFixed: nFixed, isBt: isBt,
           firstDay: nFixed + 1, idCol: nFixed + DAYS * per + 1 };
}
const CFG_TR = mkCfg_(V4_FIXED, ["Open", "High", "Low", "Close"], false);
const CFG_BT = mkCfg_(V4_FIXED.concat(BT_EXTRA),
  ["Open", "High", "High vs Open (%)", "Low", "Low vs Open (%)", "Close", "Close vs Open (%)", "Volume (x avg)"], true);

function cfg_(tab) { return tab === "Backtest" ? CFG_BT : CFG_TR; }

function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet() {
  return out_({ ok: true, note: "BSE Filing Tracker script chal rahi hai (v6)" });
}

function colA1_(n) {
  let s = "";
  while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); }
  return s;
}

// ---------- Sheet / header ----------

function getSheet(name) {
  name = name || TAB;
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  ensureLayout_(sh, cfg_(name));
  return sh;
}

function ensureLayout_(sh, c) {
  const c1 = sh.getRange(1, 1).getValue();

  if (c.isBt) {
    // Backtest: v5 pehle se hai?
    if (c1 === "Company" && sh.getRange(1, c.nFixed).getValue() === c.fixed[c.nFixed - 1]
        && sh.getRange(1, c.idCol).getValue() === "ID") return;
    // Purana (v4 ya khaali) Backtest: poora saaf karke naya layout. Backtest ka data backfill 'run' se dobara ban jaata hai.
    sh.setFrozenRows(0);
    sh.setFrozenColumns(0);
    sh.getRange(1, 1, sh.getMaxRows(), sh.getMaxColumns()).breakApart();
    sh.clear();
    buildHeader_(sh, c);
    return;
  }

  // Tracker (v4 layout) -- purana migrate logic jaisa tha waisa hi
  if (c1 === "Company" && sh.getRange(1, 11).getValue() === V4_FIXED[10]) return;
  if (c1 === "Company" && sh.getRange(1, 7).getValue() === V4_FIXED[6]
      && sh.getRange(1, 11).getValue() === "Day 0") {
    sh.insertColumnsAfter(10, 5);
  } else if (sh.getRange(1, 7).getValue() === "Day 0" && sh.getRange(2, 7).getValue() === "Open") {
    sh.insertColumnsAfter(6, 9);
  } else if (sh.getRange(1, 7).getValue() === "Day 0 Open") {
    sh.insertRowAfter(1);
    sh.insertColumnsAfter(6, 9);
  }
  buildHeader_(sh, c);
}

function tryMerge_(range) {
  try { range.merge(); } catch (e) { /* merge fail ho toh baaki kaam rukna nahi chahiye */ }
}

function buildHeader_(sh, c) {
  sh.setFrozenRows(0);
  sh.setFrozenColumns(0);

  if (sh.getMaxColumns() < c.idCol) {
    sh.insertColumnsAfter(sh.getMaxColumns(), c.idCol - sh.getMaxColumns());
  }
  const hdr = sh.getRange(1, 1, HEADER_ROWS, c.idCol);
  hdr.breakApart();

  const r1 = new Array(c.idCol).fill("");
  const r2 = new Array(c.idCol).fill("");
  c.fixed.forEach(function (t, i) { r1[i] = t; });
  r1[c.idCol - 1] = "ID";
  for (let d = 0; d < DAYS; d++) {
    const col = c.firstDay - 1 + d * c.per;
    r1[col] = "Day " + d;
    for (let k = 0; k < c.per; k++) r2[col + k] = c.dayLabels[k];
  }
  hdr.setValues([r1, r2]);

  for (let i = 1; i <= c.nFixed; i++) tryMerge_(sh.getRange(1, i, 2, 1));
  tryMerge_(sh.getRange(1, c.idCol, 2, 1));
  for (let d = 0; d < DAYS; d++) tryMerge_(sh.getRange(1, c.firstDay + d * c.per, 1, c.per));

  hdr.setFontWeight("bold").setHorizontalAlignment("center").setVerticalAlignment("middle").setWrap(true);
  sh.getRange(1, 1, 2, 6).setBackground("#1f3a5f").setFontColor("#ffffff");
  sh.getRange(1, 7, 2, 5).setBackground("#2e6f6f").setFontColor("#ffffff");    // Key Details + Market Cap
  sh.getRange(1, 12, 2, 4).setBackground("#8a5a2b").setFontColor("#ffffff");   // Results growth
  if (c.isBt) sh.getRange(1, 16, 2, 11).setBackground("#5b3f8c").setFontColor("#ffffff");  // Analysis columns (P..Z)
  sh.getRange(1, c.idCol, 2, 1).setBackground("#dddddd").setFontColor("#666666");
  for (let d = 0; d < DAYS; d++) {
    const color = (d % 2 === 0) ? "#d9e8fb" : "#e3f3e6";
    sh.getRange(1, c.firstDay + d * c.per, 2, c.per).setBackground(color).setFontColor("#000000");
  }
  hdr.setBorder(true, true, true, true, true, true, "#999999", SpreadsheetApp.BorderStyle.SOLID);
  sh.setFrozenRows(HEADER_ROWS);
  sh.setFrozenColumns(1);

  sh.setColumnWidth(1, 190);
  sh.setColumnWidth(5, 300);
  for (let k = 7; k <= c.nFixed; k++) sh.setColumnWidth(k, 95);
  for (let k = c.firstDay; k < c.idCol; k++) sh.setColumnWidth(k, 62);

  const n = Math.max(sh.getMaxRows() - HEADER_ROWS, 1);
  formatRows_(sh, FIRST_DATA_ROW, n, c);
}

function formatRows_(sh, startRow, n, c) {
  sh.getRange(startRow, 2, n, 1).setNumberFormat("yyyy-mm-dd").setHorizontalAlignment("center");
  sh.getRange(startRow, 3, n, 1).setNumberFormat("hh:mm").setHorizontalAlignment("center");
  sh.getRange(startRow, 4, n, 1).setHorizontalAlignment("center");
  sh.getRange(startRow, 6, n, 1).setHorizontalAlignment("center");
  sh.getRange(startRow, 7, n, 2).setNumberFormat("#,##0.00").setHorizontalAlignment("center");
  sh.getRange(startRow, 9, n, 1).setNumberFormat("#,##0").setHorizontalAlignment("center");
  sh.getRange(startRow, 10, n, 1).setNumberFormat("yyyy-mm-dd").setHorizontalAlignment("center");
  sh.getRange(startRow, 11, n, 1).setNumberFormat("#,##0.00").setHorizontalAlignment("center");
  sh.getRange(startRow, 12, n, 4).setNumberFormat("0.0").setHorizontalAlignment("center");
  if (c.isBt) {
    sh.getRange(startRow, 16, n, 4).setNumberFormat("0.00").setHorizontalAlignment("center");   // P..S
    sh.getRange(startRow, 20, n, 1).setHorizontalAlignment("center");                            // T mode
    sh.getRange(startRow, 21, n, 1).setNumberFormat("#,##0").setHorizontalAlignment("center");   // U avg vol
    sh.getRange(startRow, 22, n, 1).setNumberFormat("0.00").setHorizontalAlignment("center");    // V prev close
    sh.getRange(startRow, 23, n, 4).setNumberFormat("0.00").setHorizontalAlignment("center");    // W..Z pre-filing returns + volume
  }
  sh.getRange(startRow, c.firstDay, n, DAYS * c.per).setNumberFormat("0.00").setHorizontalAlignment("center");
}

function setupTracker() {
  const sh = getSheet("Tracker");
  Logger.log("Tracker layout taiyar. Rows: " + sh.getLastRow());
}

// Editor mein ▶ Run: Backtest tab ko naye v5 layout mein banata hai (purana Backtest data saaf ho jaata hai)
function setupBacktest() {
  const sh = getSheet("Backtest");
  Logger.log("Backtest v6 tab taiyar. Columns: " + CFG_BT.idCol + ", rows: " + sh.getLastRow());
}

function fixTrackerHeader() {
  const sh = getSheet("Tracker");
  buildHeader_(sh, CFG_TR);
  Logger.log("Tracker header theek kar diya.");
}

// ---------- Rows ----------

function findRow_(sh, id, c) {
  const last = sh.getLastRow();
  if (last < FIRST_DATA_ROW) return -1;
  const ids = sh.getRange(FIRST_DATA_ROW, c.idCol, last - HEADER_ROWS, 1).getValues();
  for (let i = 0; i < ids.length; i++) {
    if (String(ids[i][0]) === String(id)) return FIRST_DATA_ROW + i;
  }
  return -1;
}

function nz_(v) { return (v === null || v === undefined) ? "" : v; }

function safeText_(s) {
  s = (s === null || s === undefined) ? "" : String(s);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function ratioText_(v) {
  if (v === null || v === undefined || v === "") return "";
  if (typeof v === "number") return v;
  const s = String(v).trim();
  return /^\d+\s*[:\/]\s*\d+$/.test(s) ? "'" + s : s;
}

// rowNum: sheet mein is row ki asli row-number (Backtest ke formulas ke liye zaroori)
function makeRow_(p, vals, c, rowNum) {
  const row = new Array(c.idCol).fill("");
  row[0] = safeText_(p.company);
  row[1] = p.filing_date || "";
  row[2] = p.filing_time || "";
  row[3] = safeText_(p.label);
  row[4] = safeText_(p.summary);
  row[5] = p.scrip || "";
  row[6] = nz_(p.amount_cr);
  row[7] = nz_(p.per_share);
  row[8] = ratioText_(p.shares_ratio);
  row[9] = nz_(p.record_date);
  row[10] = nz_(p.market_cap);
  row[11] = nz_(p.yoy_rev);
  row[12] = nz_(p.yoy_profit);
  row[13] = nz_(p.qoq_rev);
  row[14] = nz_(p.qoq_profit);
  vals = vals || [];

  if (!c.isBt) {
    for (let i = 0; i < vals.length && i < DAYS * 4; i++) row[c.firstDay - 1 + i] = vals[i];
  } else {
    row[15] = nz_(p.div_yield);
    row[16] = nz_(p.order_mcap);
    row[17] = nz_(p.bb_mcap);
    row[18] = nz_(p.bb_premium);
    row[19] = safeText_(p.buyback_mode);
    row[20] = nz_(p.avg_vol);
    row[21] = nz_(p.prev_close);
    row[22] = nz_(p.pre_1w);
    row[23] = nz_(p.pre_15d);
    row[24] = nz_(p.pre_1m);
    row[25] = nz_(p.pre_vol5);
    const vr = p.vol_ratios || [];
    for (let d = 0; d < DAYS; d++) {
      const base = c.firstDay - 1 + d * c.per;          // 0-based index of this day's Open
      // Order: Open | High | High vs Open | Low | Low vs Open | Close | Close vs Open | Volume
      row[base]     = nz_(vals[d * 4]);        // Open
      row[base + 1] = nz_(vals[d * 4 + 1]);    // High
      row[base + 3] = nz_(vals[d * 4 + 2]);    // Low
      row[base + 5] = nz_(vals[d * 4 + 3]);    // Close
      const o = colA1_(base + 1) + rowNum, h = colA1_(base + 2) + rowNum,
            l = colA1_(base + 4) + rowNum, cl = colA1_(base + 6) + rowNum;
      row[base + 2] = '=IF(AND(ISNUMBER(' + o + '),ISNUMBER(' + h + '),' + o + '<>0),(' + h + '/' + o + '-1)*100,"")';
      row[base + 4] = '=IF(AND(ISNUMBER(' + o + '),ISNUMBER(' + l + '),' + o + '<>0),(' + l + '/' + o + '-1)*100,"")';
      row[base + 6] = '=IF(AND(ISNUMBER(' + o + '),ISNUMBER(' + cl + '),' + o + '<>0),(' + cl + '/' + o + '-1)*100,"")';
      row[base + 7] = nz_(vr[d]);
    }
  }
  row[c.idCol - 1] = p.id;
  return row;
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  try {
    lock.waitLock(30000);
    const p = JSON.parse(e.postData.contents);
    if (p.secret !== SECRET) return out_({ ok: false, error: "galat secret" });

    const tab = p.tab || TAB;
    if (ALLOWED_TABS.indexOf(tab) < 0) return out_({ ok: false, error: "tab allowed nahi: " + tab });
    const c = cfg_(tab);
    const sh = getSheet(tab);

    if (p.action === "add") {
      if (findRow_(sh, p.id, c) > 0) return out_({ ok: true, note: "pehle se hai" });
      const rowNum = Math.max(sh.getLastRow(), HEADER_ROWS) + 1;
      sh.appendRow(makeRow_(p, null, c, rowNum));
      formatRows_(sh, sh.getLastRow(), 1, c);
      return out_({ ok: true });
    }

    if (p.action === "update") {
      if (c.isBt) return out_({ ok: false, error: "Backtest mein update action nahi hai" });
      const row = findRow_(sh, p.id, c);
      if (row < 0) return out_({ ok: false, error: "row nahi mili" });
      const vals = (p.values || []).slice(0, DAYS * 4);
      while (vals.length < DAYS * 4) vals.push("");
      sh.getRange(row, c.firstDay, 1, DAYS * 4).setValues([vals]);
      return out_({ ok: true });
    }

    if (p.action === "bulk") {
      const rows = p.rows || [];
      const existing = {};
      const last = sh.getLastRow();
      if (last >= FIRST_DATA_ROW) {
        sh.getRange(FIRST_DATA_ROW, c.idCol, last - HEADER_ROWS, 1).getValues()
          .forEach(function (r) { existing[String(r[0])] = true; });
      }
      const fresh = [];
      rows.forEach(function (r) {
        const key = String(r.id);
        if (existing[key]) return;
        existing[key] = true;
        fresh.push(r);
      });
      if (fresh.length) {
        const start = Math.max(sh.getLastRow(), HEADER_ROWS) + 1;
        const outRows = fresh.map(function (r, i) { return makeRow_(r, r.values || [], c, start + i); });
        const needRows = start + outRows.length - 1;
        if (sh.getMaxRows() < needRows) sh.insertRowsAfter(sh.getMaxRows(), needRows - sh.getMaxRows());
        sh.getRange(start, 1, outRows.length, c.idCol).setValues(outRows);
        formatRows_(sh, start, outRows.length, c);
      }
      return out_({ ok: true, added: fresh.length, skipped: rows.length - fresh.length });
    }

    return out_({ ok: false, error: "unknown action" });
  } catch (err) {
    return out_({ ok: false, error: String(err) });
  } finally {
    try { lock.releaseLock(); } catch (e2) { }
  }
}
