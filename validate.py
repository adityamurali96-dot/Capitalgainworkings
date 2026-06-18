"""
validate.py — broker-vs-engine validation. PURE, ZERO I/O.

An extra, independent check: most broker / AMC statements already carry their OWN
short-term and long-term capital gain figures. This module cross-checks the
engine's computed numbers against what the statement already says, three ways:

  1. Per-lot      — the broker's own per-lot gain column (mapped as `broker_gain`)
                    is compared, lot by lot, against the engine's computed gain.
                    A delta beyond tolerance means a per-row difference to chase
                    (grandfathering, charges, rounding) or a classification clash.
  2. Roll-up      — short-term / long-term / total: engine vs the SUM of the
                    broker's per-lot gains over the same lots (bucketed by the
                    engine's LT/ST call), so the headline numbers tie out.
  3. Printed      — the broker's own printed summary figures ("Short Term profit",
                    "LongTermWithOutIndex", "Taxable Long Term", "Realised P&L", …)
                    are scanned out of the raw workbook and surfaced for the
                    preparer to eyeball against the roll-up.

No tax logic here. It only sums, compares and reports. The preparer judges every
delta — like reco.py, this surfaces differences, it does not resolve them.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from mapping import parse_amount   # reuse the rupee/locale-aware number parser

TOL_ABS = 1.0     # rupee tolerance (absorbs rounding)
TOL_PCT = 0.01    # or 1% of the larger magnitude


def _within(a: float, b: float, tol_abs: float = TOL_ABS, tol_pct: float = TOL_PCT) -> bool:
    return abs(a - b) <= max(tol_abs, tol_pct * max(abs(a), abs(b)))


# ---- per-lot + roll-up comparison -------------------------------------------

@dataclass
class LotCheck:
    """One lot: engine gain vs the broker's own stated gain."""
    security_name: str
    isin: str
    section: str
    is_ltcg: bool
    engine_gain: float
    broker_gain: float | None      # None when the statement carried no per-lot gain
    delta: float | None            # engine - broker (None when no broker gain)
    status: str                    # 'match' | 'mismatch' | 'no_broker'


@dataclass
class BucketCheck:
    """One summary bucket rolled up: engine total vs broker total over its lots."""
    key: str
    n: int
    n_broker: int
    engine_gain: float
    broker_gain: float
    delta: float | None
    status: str                    # 'match' | 'mismatch' | 'no_broker'


@dataclass
class PrintedFigure:
    """A capital-gain figure the broker already PRINTED in the statement."""
    sheet: str
    label: str
    kind: str                      # 'short' | 'long' | 'total'
    value: float


@dataclass
class ValidationResult:
    lots: list = field(default_factory=list)
    buckets: list = field(default_factory=list)
    rollup: dict = field(default_factory=dict)     # 'short'|'long'|'total' -> dict
    coverage: dict = field(default_factory=dict)   # {'n', 'n_broker'}
    printed: list = field(default_factory=list)    # PrintedFigure (scanned)
    tol_abs: float = TOL_ABS
    tol_pct: float = TOL_PCT

    def counts(self) -> dict:
        return {
            "lots": len(self.lots),
            "with_broker": self.coverage.get("n_broker", 0),
            "match": sum(1 for l in self.lots if l.status == "match"),
            "mismatch": sum(1 for l in self.lots if l.status == "mismatch"),
            "no_broker": sum(1 for l in self.lots if l.status == "no_broker"),
        }

    def has_broker_gain(self) -> bool:
        return self.coverage.get("n_broker", 0) > 0


def _status(n_broker: int, eng: float, brk: float, tol_abs: float, tol_pct: float) -> str:
    if n_broker == 0:
        return "no_broker"
    return "match" if _within(eng, brk, tol_abs, tol_pct) else "mismatch"


def build_validation(results, tol_abs: float = TOL_ABS, tol_pct: float = TOL_PCT) -> ValidationResult:
    """Compare each engine Result against the broker's own per-lot gain, then roll
    up to per-bucket and short/long/total.

    The roll-up and per-bucket totals are summed APPLES-TO-APPLES — both the engine
    and broker sides cover only the lots that actually carry a broker figure — so a
    delta is a genuine per-lot discrepancy, never an artefact of missing coverage.
    Coverage (lots with a broker figure / total lots) is reported alongside, and
    lots with no broker figure are still listed and marked `no_broker` so nothing
    is hidden. When the broker states a gain for every lot (the usual case for a
    statement that carries a gain column), the engine side equals the deliverable's
    own totals."""
    vres = ValidationResult(tol_abs=tol_abs, tol_pct=tol_pct)
    bucket_acc: dict[str, list] = {}            # key -> [n, n_broker, eng_sum, brk_sum]
    roll = {"short": [0, 0, 0.0, 0.0], "long": [0, 0, 0.0, 0.0], "total": [0, 0, 0.0, 0.0]}

    for r in results:
        bg = r.tx.broker_gain
        has = bg is not None
        eng = round(r.gain, 2)
        delta = round(eng - bg, 2) if has else None
        status = ("no_broker" if not has
                  else "match" if _within(eng, bg, tol_abs, tol_pct) else "mismatch")
        vres.lots.append(LotCheck(
            security_name=r.tx.security_name, isin=r.tx.isin or "", section=r.section,
            is_ltcg=r.is_ltcg, engine_gain=eng,
            broker_gain=(round(bg, 2) if has else None), delta=delta, status=status))

        b = bucket_acc.setdefault(r.bucket, [0, 0, 0.0, 0.0])
        b[0] += 1
        side = "long" if r.is_ltcg else "short"
        for k in (side, "total"):
            roll[k][0] += 1
        if has:    # only lots with a broker figure feed BOTH comparison sides
            b[1] += 1; b[2] += eng; b[3] += bg
            for k in (side, "total"):
                roll[k][1] += 1; roll[k][2] += eng; roll[k][3] += bg

    for key, (n, nb, es, bs) in bucket_acc.items():
        vres.buckets.append(BucketCheck(
            key=key, n=n, n_broker=nb, engine_gain=round(es, 2), broker_gain=round(bs, 2),
            delta=(round(es - bs, 2) if nb else None),
            status=_status(nb, es, bs, tol_abs, tol_pct)))
    vres.buckets.sort(key=lambda c: -abs(c.delta or 0))

    for key, (n, nb, es, bs) in roll.items():
        vres.rollup[key] = {
            "engine": round(es, 2), "broker": round(bs, 2), "n": n, "n_broker": nb,
            "delta": (round(es - bs, 2) if nb else None),
            "status": _status(nb, es, bs, tol_abs, tol_pct),
        }
    vres.coverage = {"n": len(results),
                     "n_broker": sum(1 for r in results if r.tx.broker_gain is not None)}
    return vres


# ---- scan the raw workbook for the broker's PRINTED summary figures ----------

_SHORT = re.compile(r"short[\s\-]*term", re.I)
_LONG = re.compile(r"long[\s\-]*term", re.I)
# a "this is the overall gain" line (Kotak "Realised P&L", "Net P&L", "Total P&L")
_TOTAL = re.compile(r"\b(realis|realiz|net|total|overall)\w*\b.*\b(p\s*&\s*l|pnl|profit|gain)\b", re.I)
# the label must read like a GAIN line, not a sale-value / cost line
_GAINY = re.compile(r"gain|profit|p\s*&\s*l|pnl", re.I)


def _row_values_right_of(row: list, label_col: int) -> list[float]:
    """Parseable numbers sitting to the right of the label cell, in column order."""
    out = []
    for c in row[label_col + 1:]:
        v = parse_amount(c)
        if v is not None:
            out.append(v)
    return out


def scan_broker_totals(sheets: dict) -> list[PrintedFigure]:
    """Sweep every sheet of the raw workbook for capital-gain figures the broker
    already printed. A cell whose text names short/long-term (or an overall P&L
    line) and that has a number to its right is recorded; the rightmost number is
    taken (broker summaries print period splits then a Total column). Lot-level
    section dividers carry no number and are skipped; lot rows never carry the
    literal 'short term' / 'long term' text, so they do not false-positive.

    `sheets` is {sheet_name: list[list[str]]} — the cleaned raw rows already held
    on the job from upload. Best-effort and for reference only; everything found
    is surfaced for the preparer rather than silently matched to a bucket."""
    found: list[PrintedFigure] = []
    seen: set[tuple] = set()
    for name, rows in (sheets or {}).items():
        for row in rows:
            cells = [str(c) for c in row]
            for ci, cell in enumerate(cells):
                text = cell.strip()
                if not text or parse_amount(text) is not None:
                    continue  # skip blanks and pure-number cells (those are values)
                is_short = bool(_SHORT.search(text))
                is_long = bool(_LONG.search(text))
                is_total = bool(_TOTAL.search(text))
                if is_short or is_long:
                    if not _GAINY.search(text):
                        # "Short Term" on its own is ambiguous (could head a sale
                        # block); require a gain/profit word OR a value to its right.
                        if not _row_values_right_of(row, ci):
                            continue
                    kind = "short" if is_short else "long"
                elif is_total:
                    kind = "total"
                else:
                    continue
                nums = _row_values_right_of(row, ci)
                if not nums:
                    continue
                value = round(nums[-1], 2)        # rightmost = the Total column
                key = (name, text[:60], value)
                if key in seen:
                    continue
                seen.add(key)
                found.append(PrintedFigure(sheet=name, label=text[:80], kind=kind, value=value))
    return found
