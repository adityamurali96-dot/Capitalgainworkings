"""
detect.py — auto-detection layer that sits in front of mapping.

PURE, ZERO I/O. Given the raw rows of a sheet (list of list of str), it answers
the three questions that used to be tedious manual work, one per broker format:

  1. which sheet holds the lot-level data        -> rank_sheets()
  2. which row is the real column header         -> detect_header_row()
  3. which source column feeds each canonical    -> auto_map()
     field

Plus the blank-noise cleanup the formats force on us:

  - drop_blank_columns()  collapses the all-empty columns that .xls merged-cell
    spillover leaves behind (Carnelian 21/43, Capital Gain Stmt 11/33, IIFL 10/39).
  - forward_fill_cols()   carries a grouped name/ISIN down onto its lot rows
    (IIFL lists the scrip + ISIN once, then the lots beneath with those blank).
  - looks_like_data_row() / is_repeat_header() filter section-divider and
    repeated-header rows so they never reach the engine as errors.

Nothing here decides tax facts. It only proposes column wiring the preparer then
confirms on the map screen — every guess is overridable, and the confidence is
shown so a weak guess is obvious.
"""
from __future__ import annotations
import re

# canonical fields (kept in sync with mapping.CANONICAL_FIELDS)
CANONICAL_FIELDS = [
    "security_name", "acquisition_date", "purchase_cost", "transfer_date",
    "sale_consideration", "quantity", "isin", "transfer_expenses", "fmv_31jan2018",
]
REQUIRED = ["security_name", "acquisition_date", "purchase_cost", "transfer_date", "sale_consideration"]

# Header aliases seen across CAMS, Karvy/KFIN, Zerodha, Groww, ICICI Direct,
# IIFL, Kotak, MProfit, Valentis, Carnelian and the NSDL/CDSL broker dumps.
# Order matters only for readability; matching is by best score, not first hit.
SYNONYMS: dict[str, list[str]] = {
    "security_name": [
        "security name", "scrip name", "script name", "stock name", "stock symbol",
        "asset name", "scheme name", "fund name", "company name", "particulars",
        "name of security", "name of the security", "name of the asset",
        "scheme", "security", "symbol", "name of the company", "instrument",
    ],
    "isin": ["isin no", "isin number", "isin code", "isin"],
    "quantity": [
        "sale quantity", "qty sold", "qty. sold", "quantity", "units", "no of units",
        "no. of units", "current units", "no of shares", "qty",
    ],
    "acquisition_date": [
        "purchase date", "buy date", "pur date", "pur. date", "acquisition date",
        "date of purchase", "date of acquisition", "entry date", "buy dt",
    ],
    "transfer_date": [
        "sale date", "sell date", "date of sale", "date of transfer", "transfer date",
        "exit date", "redemption date", "sell dt", "date",
    ],
    "purchase_cost": [
        "purchase value", "buy value", "cost of acquisition", "purchase amount",
        "original cost amount", "acquisition cost", "purchase amt", "buy amt",
        "total cost", "cost amount", "effective cost", "purchase value", "cost",
    ],
    "sale_consideration": [
        "sale value", "sell value", "sale amount", "sale consideration", "sale amt",
        "sell amt", "full value of consideration", "full value", "redemption amount",
        "sale proceeds", "net value", "transaction value", "reported value",
        "total sale value", "amount",
    ],
    "transfer_expenses": [
        "sale expenses", "sell charges", "transfer expenses", "sale charges",
        "brokerage", "expenses",
    ],
    "fmv_31jan2018": [
        "price as on 31st jan", "price on 31-jan-18", "fmv price on 31",
        "rate as on 31 jan 18", "fmv as on 31", "fair market value", "grandfathered nav",
        "31st jan 2018", "31-jan-2018", "31/01/2018", "31 jan 18", "as on 31",
    ],
}

# Tokens that, when present, disqualify a header from the *total-amount* fields:
# a per-unit "Sale Rate (S)" / "Buy price" / "NAV" must never be read as the
# total consideration or cost. (The FMV field is exempt — it is a per-unit price.)
_RATE_TOKENS = {"rate", "price", "nav"}
_AMOUNT_OK = {"value", "amount", "cost", "consideration", "proceeds"}
_REJECT_RATE_FOR = {"purchase_cost", "sale_consideration"}


def _norm(s) -> str:
    """Lowercase, strip Excel header cruft: punctuation, (S)/(P) tags, '*', newlines."""
    s = str(s or "").lower().replace("\n", " ")
    s = re.sub(r"[()\[\]*:#/]", " ", s)
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokens(s: str) -> set[str]:
    return set(s.split())


def _score(field: str, header_norm: str) -> int:
    """0 (no match) .. 3 (exact). 2 = solid phrase/word match, 1 = weak token hit."""
    if not header_norm:
        return 0
    htoks = _tokens(header_norm)
    demote = 0
    if field in _REJECT_RATE_FOR and htoks & _RATE_TOKENS:
        # a per-unit "Sale Rate"/"Buy price"/"NAV" is never the *total* amount.
        if not (htoks & _AMOUNT_OK):
            return 0
        # has both (e.g. "Cost of Acquisition Price") -> allow, but a clean
        # "amount/value/cost" header must outrank it for the total field.
        demote = 1
    best = 0
    for alias in SYNONYMS[field]:
        if header_norm == alias:
            best = 3
            break
        if " " in alias:
            if alias in header_norm:
                best = max(best, 2)
        elif alias in htoks:
            best = max(best, 2)
    if not best:
        # weak fallback: a distinctive single-word alias appearing anywhere
        for alias in SYNONYMS[field]:
            if " " not in alias and len(alias) >= 4 and alias in header_norm:
                best = max(best, 1)
    return max(0, best - demote) if best else 0


def auto_map(headers: list[str]) -> dict[str, dict]:
    """
    Greedy one-to-one assignment of source headers -> canonical fields.

    Returns {canonical_field: {"header": <source header>, "col": <index>,
                               "score": int, "confidence": "high"|"low"}}.
    Only fields that found a column are present. Each header is used at most once.
    """
    norm = [_norm(h) for h in headers]
    # candidate (score, field, col) triples
    cands = []
    for field in CANONICAL_FIELDS:
        for col, hn in enumerate(norm):
            sc = _score(field, hn)
            if sc > 0:
                cands.append((sc, field, col))
    # prefer higher score, then required fields, then field declaration order
    req_rank = {f: i for i, f in enumerate(REQUIRED)}
    field_rank = {f: i for i, f in enumerate(CANONICAL_FIELDS)}
    cands.sort(key=lambda t: (-t[0], req_rank.get(t[1], 99), field_rank[t[1]]))
    out: dict[str, dict] = {}
    used_cols: set[int] = set()
    for sc, field, col in cands:
        if field in out or col in used_cols:
            continue
        out[field] = {"header": headers[col], "col": col, "score": sc,
                      "confidence": "high" if sc >= 2 else "low"}
        used_cols.add(col)
    return out


def detect_header_row(rows: list[list[str]], max_scan: int = 30):
    """
    Scan the top rows; return (row_index, automap, req_hits, total_hits) for the
    row that reads most like a header, or None if nothing convincing is found.
    """
    best = None
    for ri, row in enumerate(rows[:max_scan]):
        mp = auto_map([str(c) for c in row])
        req = sum(1 for f in REQUIRED if f in mp)
        tot = len(mp)
        # weight required matches heavily; a stray 'Date' column shouldn't win
        key = (req, tot)
        if best is None or key > best[0]:
            best = (key, ri, mp, req, tot)
    if best is None:
        return None
    (req, tot), ri, mp, _, _ = best
    if req >= 2 and tot >= 3:
        return ri, mp, req, tot
    return None


def rank_sheets(sheets: dict[str, list[list[str]]]):
    """
    Order sheets best-data-first. Each entry:
      {"name", "header_row", "automap", "req", "tot", "reason"}.
    Sheets with no detectable lot-level header sink to the bottom (header_row None).
    """
    scored = []
    for name, rows in sheets.items():
        det = detect_header_row(rows)
        if det:
            ri, mp, req, tot = det
            matched = ", ".join(
                mp[f]["header"] for f in REQUIRED if f in mp
            ) or "—"
            reason = f"row {ri+1}: matched {req}/{len(REQUIRED)} required ({matched})"
            scored.append({"name": name, "header_row": ri, "automap": mp,
                           "req": req, "tot": tot, "reason": reason})
        else:
            scored.append({"name": name, "header_row": None, "automap": {},
                           "req": 0, "tot": 0, "reason": "no lot-level header found"})
    scored.sort(key=lambda d: (d["req"], d["tot"]), reverse=True)
    return scored


# ---- blank / noise handling --------------------------------------------------

def drop_blank_columns(rows: list[list[str]]):
    """
    Remove columns that are empty in every row (the .xls merged-cell spillover).
    Returns (new_rows, kept_indices). Width is normalised to the widest row.
    """
    if not rows:
        return rows, []
    ncol = max((len(r) for r in rows), default=0)
    keep = [ci for ci in range(ncol)
            if any(ci < len(r) and str(r[ci]).strip() for r in rows)]
    new = [[(r[ci] if ci < len(r) else "") for ci in keep] for r in rows]
    return new, keep


def forward_fill_cols(rows: list[dict], cols: list[str]) -> None:
    """
    Carry the last non-blank value of each named column down onto following rows
    (in place). For grouped layouts (IIFL) that print the scrip/ISIN once on a
    header line and leave it blank on the lot lines beneath.
    """
    last = {c: "" for c in cols}
    for r in rows:
        for c in cols:
            v = str(r.get(c, "") or "").strip()
            if v:
                last[c] = v
            elif last[c]:
                r[c] = last[c]


_JUNK_LABEL = re.compile(
    r"^(grand\s+total|sub\s*total|total|net\b|opening|closing|summary|disclaimer|"
    r"this is|note\b|regd|report|statement of|short term|long term|intraday|"
    r"mutual funds?|equity\b|debt\b|listed shares|unlisted shares)",
    re.I,
)


def is_junk_label(name: str) -> bool:
    """A security cell that is really a section divider / total / footnote."""
    s = str(name or "").strip()
    return bool(s) and bool(_JUNK_LABEL.match(s))


def is_repeat_header(row: list[str], header: list[str]) -> bool:
    """A re-printed header line mid-table (ICICI Direct repeats it per block)."""
    rn = [_norm(c) for c in row]
    hn = [_norm(c) for c in header]
    hits = sum(1 for a, b in zip(rn, hn) if a and a == b)
    return hits >= max(3, len([h for h in hn if h]) // 2)
