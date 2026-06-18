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
import os, re, sqlite3
from typing import Optional

DB_PATH = os.environ.get("ISIN_DB_PATH",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "isin_master.db"))

# map whatever the DB calls a category onto our asset_type vocabulary
_CATEGORY_MAP = {
    "equity": "equity", "equity_share": "equity", "stock": "equity", "share": "equity",
    "eof": "eof", "equity_oriented": "eof", "equity mutual fund": "eof", "equity_mf": "eof",
    "elss": "eof",
    "debt": "mf_debt", "mf_debt": "mf_debt", "debt_mf": "mf_debt", "liquid": "mf_debt",
    "hybrid": "mf_debt", "gold": "mf_debt", "international": "mf_debt", "fof": "mf_debt",
}

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
        cols = [c[1].lower() for c in cur.execute(f"PRAGMA table_info('{t}')").fetchall()]
        real = {c[1].lower(): c[1] for c in cur.execute(f"PRAGMA table_info('{t}')").fetchall()}
        isin_col = next((real[c] for c in cols if "isin" in c), None)
        name_col = next((real[c] for c in cols if c in ("name","security_name","scheme","scheme_name","symbol","description")), None)
        type_col = next((real[c] for c in cols if c in ("asset_type","category","type","class","orientation","asset_class")), None)
        if isin_col:
            return t, isin_col, name_col, type_col
    return None


def _norm_type(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _CATEGORY_MAP.get(str(raw).strip().lower())


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
                at = _norm_type(row[c_type]) if c_type else None
                if at:
                    # demote international equity funds that masquerade as equity
                    if at == "eof" and c_name and _INTL_HINT.search(str(row[c_name] or "")):
                        return {"asset_type": "mf_debt", "basis": f"ISIN hit, name suggests international -> debt-treatment",
                                "confidence": "proposed", "matched": isin, "reason": "international fund, verify orientation"}
                    return {"asset_type": at, "basis": f"ISIN exact match in {table}",
                            "confidence": "trusted", "matched": isin, "reason": ""}
                return _miss("ISIN found but category not mapped — set manually", matched=isin)

        # 2) name match -> proposed (heuristic, never trusted)
        if name and c_name:
            key = name.strip().upper()[:18]
            row = cur.execute(
                f"SELECT * FROM '{table}' WHERE UPPER({c_name}) LIKE ? LIMIT 1",
                (key + "%",)).fetchone()
            if row:
                at = _norm_type(row[c_type]) if c_type else None
                if at:
                    return {"asset_type": at, "basis": f"name prefix match ({row[c_name]})",
                            "confidence": "proposed", "matched": row[c_name],
                            "reason": "name-only match — confirm orientation"}

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
