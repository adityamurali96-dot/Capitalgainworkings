"""
tests/test_compute.py — hand-checked unit tests for the deterministic core.

compute.py is the only place a bug is a wrong filing, so it is the only place
with hand-checked expectations. Run:

    python tests/test_compute.py

No test framework dependency — plain asserts, prints a summary, exits non-zero
on the first failure.
"""
from __future__ import annotations
import os, sys
from datetime import date

# allow running both as `python tests/test_compute.py` and from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import compute
from compute import Tx, compute_row, compute_all, add_months


def _tx(**kw):
    base = dict(
        security_name="X", acquisition_date=date(2020, 1, 1),
        transfer_date=date(2024, 8, 1), purchase_cost=100.0,
        sale_consideration=1000.0, asset_type="equity",
    )
    base.update(kw)
    return Tx(**base)


def test_add_months_clamps_to_month_length():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)   # leap year
    assert add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
    assert add_months(date(2023, 12, 15), 1) == date(2024, 1, 15)  # rolls the year


def test_equity_holding_boundary_is_strictly_after():
    # equity threshold = 12 months; LTCG requires transfer strictly after acq+12m
    acq = date(2023, 8, 1)
    on = compute_row(_tx(acquisition_date=acq, transfer_date=date(2024, 8, 1)))
    after = compute_row(_tx(acquisition_date=acq, transfer_date=date(2024, 8, 2)))
    assert on.is_ltcg is False, "exactly 12 months is still short-term"
    assert after.is_ltcg is True, "one day past 12 months is long-term"


def test_grandfathering_applied_on_raw_pre2018_lot():
    # acq before 01-Feb-2018, raw cost -> substitute max(cost, min(fmv_total, net_sale))
    # cost=100, fmv per-unit 50 * qty 10 = 500 (total), net sale 1000 -> cost_used 500
    r = compute_row(_tx(acquisition_date=date(2015, 1, 1), purchase_cost=100.0,
                        sale_consideration=1000.0, quantity=10, fmv_31jan2018=50.0,
                        fmv_basis="per_unit", cost_basis_meaning="raw"))
    assert r.grandfathering_applied is True
    assert r.cost_used == 500.0
    assert r.gain == 500.0
    assert r.section == "112A" and r.is_ltcg is True


def test_grandfathering_suppressed_when_cost_already_grandfathered():
    # the headline silent-error guard: source already grandfathered -> FMV ignored
    r = compute_row(_tx(acquisition_date=date(2015, 1, 1), purchase_cost=100.0,
                        sale_consideration=1000.0, quantity=10, fmv_31jan2018=50.0,
                        fmv_basis="per_unit", cost_basis_meaning="grandfathered"))
    assert r.grandfathering_applied is False
    assert r.cost_used == 100.0          # raw cost used, FMV not substituted
    assert r.gain == 900.0
    assert any("FMV present but ignored" in f for f in r.flags)


def test_50aa_debt_is_always_short_term():
    # 50AA: STCG regardless of holding period (held > 4 years here)
    r = compute_row(_tx(asset_type="mf_debt", is_50aa=True, stt_paid=False,
                        acquisition_date=date(2020, 1, 1), transfer_date=date(2024, 8, 1)))
    assert r.is_ltcg is False
    assert r.section == "50AA"
    assert r.rate_label == "slab"
    assert r.bucket == "STCG Debt (50AA/slab)"


def test_vda_no_expense_deduction_and_115bbh():
    r = compute_row(_tx(asset_type="vda", sale_consideration=1000.0,
                        transfer_expenses=50.0, purchase_cost=400.0))
    assert r.net_sale_consideration == 1000.0   # expenses NOT deducted for VDA
    assert r.gain == 600.0
    assert r.section == "115BBH"
    assert r.rate_label == "115BBH @30%"
    assert r.is_ltcg is False


def test_pivot_split_changes_equity_ltcg_rate():
    # post-GF-cutoff acq so grandfathering does not interfere; both are LTCG
    pre = compute_row(_tx(acquisition_date=date(2019, 1, 1), transfer_date=date(2024, 7, 22)))
    post = compute_row(_tx(acquisition_date=date(2019, 1, 1), transfer_date=date(2024, 7, 23)))
    assert pre.pivot_side == "pre" and pre.rate_label == "112A @10%"
    assert post.pivot_side == "post" and post.rate_label == "112A @12.5%"


def test_non_stt_equity_routed_outside_111a_112a():
    r = compute_row(_tx(acquisition_date=date(2019, 1, 1), transfer_date=date(2024, 8, 1),
                        stt_paid=False))
    assert r.section == "112 (non-STT equity)"
    assert any("without STT" in f for f in r.flags)


def test_debt_pivot_threshold_24_vs_36_months():
    # mf_debt (not 50AA): post-pivot threshold 24m, pre-pivot 36m
    # acq 2021-09-01; sold 2024-08-01 (post) -> ~35m, >24m -> LTCG
    post = compute_row(_tx(asset_type="mf_debt", stt_paid=False,
                           acquisition_date=date(2021, 9, 1), transfer_date=date(2024, 8, 1)))
    assert post.threshold_months == 24 and post.is_ltcg is True and post.section == "112"
    # sold 2024-07-01 (pre) -> ~34m, <36m -> STCG
    pre = compute_row(_tx(asset_type="mf_debt", stt_paid=False,
                          acquisition_date=date(2021, 9, 1), transfer_date=date(2024, 7, 1)))
    assert pre.threshold_months == 36 and pre.is_ltcg is False and pre.section == "slab"


def test_ay_2024_25_rate_regime():
    r = compute_row(_tx(acquisition_date=date(2019, 1, 1), transfer_date=date(2024, 8, 1)),
                    ay="2024-25")
    assert r.rate_label == "112A @10%"   # flat 10% LTCG under AY 2024-25


def test_transfer_before_acquisition_rejected():
    try:
        _tx(acquisition_date=date(2024, 8, 1), transfer_date=date(2024, 1, 1))
    except ValueError:
        return
    raise AssertionError("expected ValueError for transfer before acquisition")


def test_compute_all_preserves_order_and_count():
    txns = [_tx(security_name="A"), _tx(security_name="B", asset_type="vda")]
    out = compute_all(txns)
    assert len(out) == 2
    assert [r.tx.security_name for r in out] == ["A", "B"]


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} passed")


if __name__ == "__main__":
    main()
