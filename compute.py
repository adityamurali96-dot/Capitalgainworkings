"""
compute.py — the deterministic capital-gains core.

ZERO I/O. No file reads, no prompts, no network. Pure functions from a
canonical transaction to an enriched one. This is the only place a bug is a
wrong filing, so it is the only place that gets a hand-checked unit-test suite
(see tests/test_compute.py).

Everything here is mechanical and must be built (never asked):
  - holding-period month test
  - 23-Jul-2024 pivot split
  - Section 55(2)(ac) grandfathering nested substitution
  - section assignment (111A / 112A / 50AA / 112 / 115BBH / foreign / unlisted)
  - bucket + rate label
  - gain = net sale consideration - cost used

Everything the engine must NOT decide silently arrives already resolved on the
canonical row: asset_type, stt_paid, is_50aa, cost_basis_meaning, fmv. Those are
captured at mapping/classification time, upstream of this module.
"""

from __future__ import annotations
import calendar
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

PIVOT = date(2024, 7, 23)          # FY24-25 rate-split pivot
GF_CUTOFF = date(2018, 2, 1)       # grandfathering applies to lots acquired BEFORE this

# asset_type vocabulary
ASSET_TYPES = {"equity", "eof", "business_trust", "mf_debt", "vda", "foreign", "unlisted"}

# LTCG month thresholds
TH_EQUITY = 12        # equity / EOF / business trust
TH_OTHER_POST = 24    # non-equity financial assets, transfer on/after 23-Jul-2024
TH_OTHER_PRE = 36     # non-equity, transfer before 23-Jul-2024
TH_FOREIGN = 24
TH_UNLISTED = 24


def add_months(d: date, n: int) -> date:
    """d + n calendar months, clamping the day to the target month length."""
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _is_long_term(acq: date, xfer: date, threshold_months: int) -> bool:
    """Held for MORE THAN threshold months => transfer strictly after acq+threshold."""
    return xfer > add_months(acq, threshold_months)


@dataclass
class Tx:
    """Canonical transaction. Dates are datetime.date. Amounts are floats (no commas)."""
    security_name: str
    acquisition_date: date
    transfer_date: date
    purchase_cost: float                 # meaning governed by cost_basis_meaning
    sale_consideration: float            # gross
    asset_type: str                      # one of ASSET_TYPES
    quantity: Optional[float] = None
    isin: Optional[str] = None
    transfer_expenses: float = 0.0
    fmv_31jan2018: Optional[float] = None   # per-unit by default (Winman col J convention)
    fmv_basis: str = "per_unit"             # "per_unit" | "total"
    stt_paid: bool = True
    is_50aa: bool = False
    cost_basis_meaning: str = "raw"         # "raw" | "grandfathered"
    source_label: str = ""
    classification_basis: str = ""          # how asset_type was decided (trace)
    classification_confidence: str = ""     # trusted | proposed | manual

    def __post_init__(self):
        if self.asset_type not in ASSET_TYPES:
            raise ValueError(f"unknown asset_type {self.asset_type!r}")
        if self.transfer_date < self.acquisition_date:
            raise ValueError(
                f"transfer_date {self.transfer_date} precedes acquisition_date "
                f"{self.acquisition_date} for {self.security_name!r}"
            )


@dataclass
class Result:
    tx: Tx
    holding_days: int = 0
    threshold_months: int = 0
    is_ltcg: bool = False
    pivot_side: str = ""                  # "pre" | "post"
    section: str = ""                     # 111A | 112A | 50AA | 112 | 115BBH | foreign | unlisted
    bucket: str = ""                      # summary bucket key
    rate_label: str = ""
    net_sale_consideration: float = 0.0
    cost_used: float = 0.0
    grandfathering_applied: bool = False
    grandfathering_detail: str = ""
    gain: float = 0.0
    flags: list = field(default_factory=list)

    def trace(self) -> dict:
        """The audit snapshot that rides into Output A, one row."""
        return {
            "security_name": self.tx.security_name,
            "isin": self.tx.isin,
            "source": self.tx.source_label,
            "asset_type": self.tx.asset_type,
            "classification_basis": self.tx.classification_basis,
            "classification_confidence": self.tx.classification_confidence,
            "holding_days": self.holding_days,
            "threshold_months": self.threshold_months,
            "is_ltcg": self.is_ltcg,
            "pivot_side": self.pivot_side,
            "section": self.section,
            "rate_label": self.rate_label,
            "cost_basis_meaning": self.tx.cost_basis_meaning,
            "grandfathering_applied": self.grandfathering_applied,
            "grandfathering_detail": self.grandfathering_detail,
            "net_sale_consideration": round(self.net_sale_consideration, 2),
            "cost_used": round(self.cost_used, 2),
            "gain": round(self.gain, 2),
            "flags": "; ".join(self.flags),
        }


# ---- rate regime (parameterised by AY) ----------------------------------

def _equity_rate(is_ltcg: bool, post: bool, ay: str) -> str:
    if ay == "2025-26":
        if is_ltcg:
            return "112A @12.5%" if post else "112A @10%"
        return "111A @20%" if post else "111A @15%"
    if ay == "2024-25":
        return "112A @10%" if is_ltcg else "111A @15%"
    raise ValueError(f"rate regime for AY {ay} not configured — confirm before routing")


# ---- holding period + LT/ST ---------------------------------------------

def _threshold_for(tx: Tx, post: bool) -> int:
    a = tx.asset_type
    if a in ("equity", "eof", "business_trust"):
        return TH_EQUITY
    if a == "mf_debt":
        return TH_OTHER_POST if post else TH_OTHER_PRE
    if a == "foreign":
        return TH_FOREIGN
    if a == "unlisted":
        return TH_UNLISTED
    return 0  # vda has no holding distinction


# ---- grandfathering 55(2)(ac) -------------------------------------------

def _fmv_total(tx: Tx) -> Optional[float]:
    if tx.fmv_31jan2018 is None:
        return None
    if tx.fmv_basis == "total":
        return tx.fmv_31jan2018
    # per-unit -> total needs quantity
    if tx.quantity is None:
        return None
    return tx.fmv_31jan2018 * tx.quantity


def _apply_grandfathering(tx: Tx, net_sale: float, res: Result) -> float:
    """
    Returns the cost to use. Substitution per Sec 55(2)(ac):
        cost = max(actual_cost, min(FMV_31Jan2018_total, net_sale_consideration))
    Applied ONLY when: asset is equity/EOF, acquired before 01-Feb-2018, AND the
    source cost is RAW. If the source already grandfathered, FMV is suppressed.
    """
    eligible = (
        tx.asset_type in ("equity", "eof")
        and tx.acquisition_date < GF_CUTOFF
    )
    if not eligible:
        return tx.purchase_cost

    if tx.cost_basis_meaning == "grandfathered":
        res.grandfathering_detail = (
            "pre-2018 lot, but source cost declared already-grandfathered "
            "-> FMV suppressed, cost used as supplied (no double-count)"
        )
        if tx.fmv_31jan2018 is not None:
            res.flags.append("FMV present but ignored (cost already grandfathered)")
        return tx.purchase_cost

    # cost is raw -> engine substitutes
    fmv_t = _fmv_total(tx)
    if fmv_t is None:
        res.flags.append("pre-2018 lot, raw cost, FMV missing -> grandfathering NOT applied; key-in FMV")
        res.grandfathering_detail = "pre-2018 raw lot with no FMV — actual cost retained, confirm FMV"
        return tx.purchase_cost

    lower = min(fmv_t, net_sale)
    cost = max(tx.purchase_cost, lower)
    res.grandfathering_applied = True
    won = "FMV" if cost == lower and lower >= tx.purchase_cost and lower == fmv_t else (
        "net-sale-cap" if cost == lower else "actual cost")
    res.grandfathering_detail = (
        f"max(actual {tx.purchase_cost:,.0f}, "
        f"min(FMV {fmv_t:,.0f}, net sale {net_sale:,.0f})) "
        f"= {cost:,.0f} [{won} retained]"
    )
    return cost


# ---- section + bucket routing -------------------------------------------

def _route(tx: Tx, is_ltcg: bool, post: bool, ay: str, res: Result):
    a = tx.asset_type

    if a == "vda":
        res.section = "115BBH"
        res.bucket = "VDA 115BBH"
        res.rate_label = "115BBH @30%"
        res.is_ltcg = False
        return

    if a in ("equity", "eof", "business_trust"):
        if not tx.stt_paid:
            # off-market equity: not 111A/112A. Route to a non-STT block, flag.
            res.section = "112 (non-STT equity)"
            res.bucket = "Equity non-STT (LTCG)" if is_ltcg else "Equity non-STT (STCG)"
            res.rate_label = "112 @12.5%" if is_ltcg else "slab"
            res.flags.append("equity sold without STT — outside 111A/112A; verify routing")
            return
        res.section = "112A" if is_ltcg else "111A"
        res.bucket = ("LTCG 112A Equity" if is_ltcg else "STCG 111A Equity")
        res.rate_label = _equity_rate(is_ltcg, post, ay)
        return

    if a == "mf_debt":
        if tx.is_50aa:
            res.section = "50AA"
            res.bucket = "STCG Debt (50AA/slab)"
            res.rate_label = "slab"
            res.is_ltcg = False
            return
        if is_ltcg:
            res.section = "112"
            res.bucket = "LTCG Debt"
            res.rate_label = "112 @12.5%" if post else "112 @20% (indexed)"
        else:
            res.section = "slab"
            res.bucket = "STCG Debt (50AA/slab)"
            res.rate_label = "slab"
        return

    if a == "foreign":
        res.section = "foreign"
        res.bucket = "Foreign (LTCG)" if is_ltcg else "Foreign (STCG)"
        res.rate_label = "112 @12.5%" if is_ltcg else "slab"
        res.flags.append("foreign — own COI block; FTC handled outside; not in Winman file")
        return

    if a == "unlisted":
        res.section = "unlisted"
        res.bucket = "Unlisted (LTCG)" if is_ltcg else "Unlisted (STCG)"
        res.rate_label = "112 @12.5%" if is_ltcg else "slab"
        res.flags.append("unlisted — own COI block; not in Winman file")
        return


# ---- the one public function --------------------------------------------

def compute_row(tx: Tx, ay: str = "2025-26") -> Result:
    res = Result(tx=tx)
    res.holding_days = (tx.transfer_date - tx.acquisition_date).days
    post = tx.transfer_date >= PIVOT
    res.pivot_side = "post" if post else "pre"

    # net sale consideration: VDA allows no expense deduction (115BBH)
    if tx.asset_type == "vda":
        res.net_sale_consideration = tx.sale_consideration
    else:
        res.net_sale_consideration = tx.sale_consideration - (tx.transfer_expenses or 0.0)

    # LT/ST
    if tx.asset_type == "vda":
        res.is_ltcg = False
        res.threshold_months = 0
    elif tx.asset_type == "mf_debt" and tx.is_50aa:
        res.is_ltcg = False                      # 50AA: always STCG, holding ignored
        res.threshold_months = 0
    else:
        th = _threshold_for(tx, post)
        res.threshold_months = th
        res.is_ltcg = _is_long_term(tx.acquisition_date, tx.transfer_date, th)

    # cost (grandfathering substitution if eligible)
    res.cost_used = _apply_grandfathering(tx, res.net_sale_consideration, res)

    # routing (may force is_ltcg False for vda/50aa already handled)
    _route(tx, res.is_ltcg, post, ay, res)

    # gain
    res.gain = res.net_sale_consideration - res.cost_used
    return res


def compute_all(txns: list[Tx], ay: str = "2025-26") -> list[Result]:
    return [compute_row(t, ay) for t in txns]
