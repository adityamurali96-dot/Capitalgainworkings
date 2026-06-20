"""
isin_db.py — orientation classifier with a three-state confidence gate.

Resolves asset ORIENTATION only (equity / eof / mf_debt / ...). It does NOT
decide STT, 50AA, grandfathering or cost meaning — those are not lookups.

Three states, by design:
  - "trusted"  : exact ISIN hit in master data            -> accept, no prompt
  - "proposed" : name-only match (heuristic)               -> goes to review queue
  - "manual"   : no ISIN, ISIN not in DB, or no name match -> user must set

If isin_master.db is absent or its schema is unrecognised, EVERY row degrades to
"manual" with a clear reason. Nothing is guessed.

Point DB_PATH at your real isin_master.db (the 26,802-ISIN NSE/AMFI build). The
reader introspects the schema, so it tolerates differing column names.
"""
from __future__ import annotations
import os, re, sqlite3, difflib
from typing import Optional

DB_PATH = os.environ.get("ISIN_DB_PATH",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "isin_master.db"))

# map whatever the DB calls a category onto our asset_type vocabulary.
# Covers both the generic vocabulary AND the values the bundled isin_master.db
# actually carries in `suggested_tax_class` (equity_listed / equity_etf /
# equity_mf / debt_mf / debt_security / review). "review" is deliberately left
# unmapped -> handled as a confirm-me state in lookup(), never silently trusted.
_CATEGORY_MAP = {
    "equity": "equity", "equity_share": "equity", "stock": "equity", "share": "equity",
    "equity_listed": "equity",
    "eof": "eof", "equity_oriented": "eof", "equity mutual fund": "eof", "equity_mf": "eof",
    "equity_etf": "eof", "elss": "eof",
    "debt": "mf_debt", "mf_debt": "mf_debt", "debt_mf": "mf_debt", "debt_security": "mf_debt",
    "liquid": "mf_debt", "hybrid": "mf_debt", "gold": "mf_debt", "international": "mf_debt",
    "fof": "mf_debt",
}

# Column-name candidates for the orientation/category column, in priority order.
# The bundled DB names it `suggested_tax_class`; generic builds may use any of the
# rest. Priority matters: prefer the tax-class column over the raw instrument_type
# (which carries values like MF/ETF that don't encode equity-vs-debt orientation).
_TYPE_COL_PRIORITY = (
    "suggested_tax_class", "asset_type", "tax_class", "category", "scheme_category",
    "type", "class", "orientation", "asset_class", "instrument_type",
)

_INTL_HINT = re.compile(r"\b(us|nasdaq|global|international|overseas|s&p|world|foreign)\b", re.I)


def _connect():
    if not os.path.exists(DB_PATH):
        return None
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        return con
    except Exception:
        return None


def _detect_columns(con):
    """Return (table, isin_col, name_col, type_col) by introspection, or None."""
    cur = con.cursor()
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    # prefer a table that actually has an isin-like column
    for t in tables:
        real = {c[1].lower(): c[1] for c in cur.execute(f"PRAGMA table_info('{t}')").fetchall()}
        cols = list(real)
        isin_col = next((real[c] for c in cols if "isin" in c), None)
        name_col = next((real[c] for c in cols if c in ("name","security_name","scheme","scheme_name","symbol","description")), None)
        # priority-ordered so the tax-class column wins over raw instrument_type
        type_col = next((real[c] for c in _TYPE_COL_PRIORITY if c in real), None)
        if isin_col:
            return t, isin_col, name_col, type_col
    return None


def _norm_type(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _CATEGORY_MAP.get(str(raw).strip().lower())


# words that carry no identity signal in a security/scheme name — dropped before the
# fuzzy compare so "Reliance Industries Ltd" and "RELIANCE INDUSTRIES" score as equal.
_NAME_NOISE = re.compile(
    r"\b(ltd|limited|pvt|private|the|co|company|corp|corporation|inc|plc|"
    r"fund|scheme|growth|regular|direct|plan|option|dividend|reinvestment|"
    r"idcw|payout|equity|series|nse|bse|mutual|amc)\b", re.I)

# floor below which a name match is too weak to even propose -> falls to manual.
_NAME_FLOOR = 0.45
# at/above this the proposal is treated as confident; below it is flagged low-confidence.
_NAME_CONFIDENT = 0.80


def _norm_name(s) -> str:
    s = str(s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = _NAME_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _ratio(a: str, b: str) -> float:
    """Blend of character-sequence similarity and token (word-set) overlap, so the
    score is robust to word reordering and trailing scheme/series cruft."""
    if not a or not b:
        return 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    tok = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return max(seq, tok)


def _best_name_match(cur, table, c_name, c_type, name):
    """Pull plausible candidates by a token LIKE, fuzzy-rank them, and return
    (asset_type, matched_name, ratio) for the best routable hit, or None. Only the
    routable rows (a category we can map) are considered, so the proposal always
    carries an asset_type."""
    qn = _norm_name(name)
    if not qn:
        return None
    tokens = [t for t in qn.split() if len(t) >= 3] or [qn[:4]]
    seen: set[str] = set()
    cands = []
    for tok in tokens[:2]:
        for row in cur.execute(
                f"SELECT * FROM '{table}' WHERE UPPER({c_name}) LIKE ? LIMIT 400",
                (f"%{tok}%",)).fetchall():
            nm = str(row[c_name] or "")
            if nm in seen:
                continue
            seen.add(nm)
            cands.append(row)
        if len(cands) >= 400:
            break
    best = None
    for row in cands:
        at = _norm_type(row[c_type]) if c_type else None
        if not at:
            continue
        r = _ratio(qn, _norm_name(row[c_name]))
        if best is None or r > best[2]:
            best = (at, str(row[c_name]), r)
    return best if (best and best[2] >= _NAME_FLOOR) else None


def lookup(isin: Optional[str], name: Optional[str]) -> dict:
    """
    -> {asset_type, basis, confidence, matched, reason}
    confidence in {trusted, proposed, manual}; asset_type may be None when manual.
    """
    con = _connect()
    if con is None:
        return _miss("no classification DB at {} — set asset type manually".format(os.path.basename(DB_PATH)))

    try:
        meta = _detect_columns(con)
        if not meta:
            return _miss("DB schema unrecognised — set asset type manually")
        table, c_isin, c_name, c_type = meta
        cur = con.cursor()

        # 1) exact ISIN -> trusted
        if isin:
            row = cur.execute(
                f"SELECT * FROM '{table}' WHERE UPPER({c_isin}) = ?",
                (isin.strip().upper(),)).fetchone()
            if row:
                raw = row[c_type] if c_type else None
                at = _norm_type(raw)
                if at:
                    # demote international equity funds that masquerade as equity
                    if at == "eof" and c_name and _INTL_HINT.search(str(row[c_name] or "")):
                        return {"asset_type": "mf_debt", "basis": f"ISIN hit, name suggests international -> debt-treatment",
                                "confidence": "proposed", "matched": isin, "reason": "international fund, verify orientation"}
                    return {"asset_type": at, "basis": f"ISIN exact match in {table}",
                            "confidence": "trusted", "matched": isin, "reason": ""}
                # ISIN is in the DB but the class can't be auto-routed.
                if str(raw or "").strip().lower() == "review":
                    return _miss("DB flags this as review (hybrid / international / gold / "
                                 "index / FoF) — confirm orientation", matched=isin)
                return _miss(f"ISIN found but class {raw!r} not mapped — set manually", matched=isin)

        # 2) name match -> proposed (heuristic, never trusted). Fuzzy-ranked so a
        #    near-miss still yields a PROPOSED asset class rather than dumping the
        #    preparer into manual; below 80% it is flagged low-confidence, below the
        #    floor it falls through to manual.
        if name and c_name:
            best = _best_name_match(cur, table, c_name, c_type, name)
            if best:
                at, matched_name, ratio = best
                pct = round(ratio * 100)
                if ratio >= _NAME_CONFIDENT:
                    reason = f"name match {pct}% ({matched_name}) — confirm orientation"
                else:
                    reason = (f"low-confidence name match {pct}% ({matched_name}) — "
                              f"proposed asset class only, verify before filing")
                return {"asset_type": at, "basis": f"name match {pct}%",
                        "confidence": "proposed", "matched": matched_name,
                        "reason": reason, "score": round(ratio, 3)}

        return _miss("no ISIN/name match — set asset type manually")
    finally:
        con.close()


def _miss(reason: str, matched=None) -> dict:
    return {"asset_type": None, "basis": "", "confidence": "manual",
            "matched": matched, "reason": reason}


def db_status() -> str:
    con = _connect()
    if con is None:
        return f"NOT FOUND ({DB_PATH}) — all rows will need manual asset-type"
    try:
        meta = _detect_columns(con)
        if not meta:
            return "FOUND but schema unrecognised — manual fallback active"
        t, ci, cn, ct = meta
        n = con.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
        return f"FOUND: {n} rows in '{t}' (isin={ci}, name={cn}, type={ct})"
    finally:
        con.close()
