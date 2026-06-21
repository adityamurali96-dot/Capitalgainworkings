"""
reco.py — AIS reconciliation engine. PURE, ZERO I/O.

Reconciles the sale side of a capital-gains / broker file against the AIS
("Sale of securities and units of mutual fund") figures. Both sides are reduced
to per-security totals keyed by ISIN (falling back to a normalised name), then
compared with a tolerance. The output is four buckets the preparer acts on:

  matched      — same security, sale value agrees within tolerance
  mismatched   — same security, sale value differs (chase the delta)
  only_in_cg   — in the broker/CG file, absent from AIS
  only_in_ais  — in AIS, absent from the broker/CG file (a sale you missed?)

No tax logic here; it only sums and compares. The preparer judges every delta.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from mapping import parse_amount  # reuse the rupee/locale-aware number parser
from detect import extract_isin, clean_security_name  # ISIN-in-free-text + AIS name cleanup

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
# Corporate suffixes + the residual instrument noise that survives clean_security_name
# (which has already cut the depository "EQ …"/"EQUITY SHARES …" tail and the ISIN).
_DROP_WORDS = re.compile(
    r"\b(LTD|LIMITED|LLP|PVT|PRIVATE|THE|INDIA|CO|COMPANY|"
    r"EQUITY|EQ|SHARES?|UNITS?|MUTUAL|FUND|SCHEME|SERIES|NSE|BSE)\b"
)


def normalise_name(s) -> str:
    """Collapse a security name to a comparable token: first reduce a verbose AIS /
    depository description to its issuer name (cutting the 'EQ …'/'EQUITY SHARES …'
    tail and the embedded ISIN — see detect.clean_security_name), then drop corporate
    suffixes and reduce to bare alphanumerics. The broker's terse name and the AIS
    description land on the same core ('Reliance Industries Ltd' and
    'RELIANCE INDUSTRIES LIMITED EQ ...(INE002A01018)' both → 'RELIANCEINDUSTRIES')."""
    s = clean_security_name(s).upper()
    s = _DROP_WORDS.sub(" ", s)
    return re.sub(r"[^A-Z0-9]", "", s)


def reco_key(isin, name) -> tuple[str, str]:
    """(key, kind): a valid ISIN if present, else the normalised name.

    The ISIN is the only field AIS and the broker reliably share, so it is tried hard:
    a clean ISIN cell first, then an ISIN *embedded* in the ISIN cell (e.g.
    'INE002A01018-EQ'), then one embedded in the security description (the common AIS
    layout, where the depository reports 'NAME ... ISIN' in one free-text column).
    Only when no ISIN can be recovered does it fall back to the normalised name."""
    iv = str(isin or "").strip().upper()
    if _ISIN.match(iv):
        return iv, "isin"
    emb = extract_isin(iv) or extract_isin(name)
    if emb:
        return emb, "isin"
    return normalise_name(name), "name"


@dataclass
class Side:
    """One security's aggregated sale figures on a single side (CG or AIS)."""
    key: str
    kind: str           # 'isin' | 'name'
    name: str           # display name (first non-blank seen)
    isin: str
    value: float = 0.0  # total sale consideration
    qty: float = 0.0
    n: int = 0          # source rows aggregated


def aggregate(rows: list[dict], name_col, isin_col, value_col, qty_col) -> dict[str, Side]:
    """Sum sale value (and qty) per security. Rows without a parseable value or a
    usable key are skipped — that quietly drops dividers, totals and blanks."""
    out: dict[str, Side] = {}
    for r in rows:
        nm = str(r.get(name_col, "") if name_col else "").strip()
        isin = str(r.get(isin_col, "") if isin_col else "").strip()
        val = parse_amount(r.get(value_col, "")) if value_col else None
        if val is None:
            continue
        qty = (parse_amount(r.get(qty_col, "")) if qty_col else None) or 0.0
        key, kind = reco_key(isin, nm)
        if not key:
            continue
        disp = clean_security_name(nm) or nm  # show the issuer, not the ISIN-laden blob
        s = out.get(key)
        if s is None:
            out[key] = Side(key=key, kind=kind, name=disp,
                            isin=key if kind == "isin" else "",
                            value=val, qty=qty, n=1)
        else:
            s.value += val
            s.qty += qty
            s.n += 1
            if not s.name and disp:
                s.name = disp
    return out


@dataclass
class Pair:
    """A security present on both sides."""
    key: str
    name: str
    isin: str
    cg_value: float
    ais_value: float
    cg_n: int
    ais_n: int

    @property
    def delta(self) -> float:
        return round(self.cg_value - self.ais_value, 2)


@dataclass
class RecoResult:
    matched: list = field(default_factory=list)
    mismatched: list = field(default_factory=list)
    only_cg: list = field(default_factory=list)
    only_ais: list = field(default_factory=list)
    tol_abs: float = 1.0
    tol_pct: float = 0.01

    def counts(self) -> dict:
        return {"matched": len(self.matched), "mismatched": len(self.mismatched),
                "only_cg": len(self.only_cg), "only_ais": len(self.only_ais)}

    def totals(self) -> dict:
        cg = sum(p.cg_value for p in self.matched + self.mismatched) + \
            sum(s.value for s in self.only_cg)
        ais = sum(p.ais_value for p in self.matched + self.mismatched) + \
            sum(s.value for s in self.only_ais)
        return {"cg": round(cg, 2), "ais": round(ais, 2), "delta": round(cg - ais, 2)}


def _within(a: float, b: float, tol_abs: float, tol_pct: float) -> bool:
    return abs(a - b) <= max(tol_abs, tol_pct * max(abs(a), abs(b)))


def reconcile(cg: dict[str, Side], ais: dict[str, Side],
              tol_abs: float = 1.0, tol_pct: float = 0.01) -> RecoResult:
    """Match CG vs AIS per security. Pass 1 matches on the shared key (ISIN or
    name); pass 2 rescues leftovers where one side keyed by ISIN and the other by
    name. Remaining unmatched fall into only_cg / only_ais."""
    res = RecoResult(tol_abs=tol_abs, tol_pct=tol_pct)
    cg, ais = dict(cg), dict(ais)

    def settle(c: Side, a: Side):
        pair = Pair(c.key, c.name or a.name, c.isin or a.isin,
                    round(c.value, 2), round(a.value, 2), c.n, a.n)
        (res.matched if _within(c.value, a.value, tol_abs, tol_pct)
         else res.mismatched).append(pair)

    # pass 1: identical key
    for key in list(cg):
        if key in ais:
            settle(cg.pop(key), ais.pop(key))

    # pass 2: name fallback for what's left
    ais_by_name = {}
    for k, a in ais.items():
        ais_by_name.setdefault(normalise_name(a.name), k)
    for key in list(cg):
        nk = normalise_name(cg[key].name)
        ak = ais_by_name.get(nk)
        if nk and ak in ais:
            ais_by_name.pop(nk, None)
            settle(cg.pop(key), ais.pop(ak))

    res.only_cg = sorted(cg.values(), key=lambda s: -abs(s.value))
    res.only_ais = sorted(ais.values(), key=lambda s: -abs(s.value))
    res.matched.sort(key=lambda p: -abs(p.cg_value))
    res.mismatched.sort(key=lambda p: -abs(p.delta))
    return res
