"""
tests/test_validate.py — checks for the broker-vs-engine validation layer.

Two kinds of test:
  1. build_validation: per-lot match/mismatch/no-broker, the short/long/total
     roll-up, per-bucket roll-up, and tolerance behaviour.
  2. scan_broker_totals: pulls the broker's PRINTED short/long/total figures out
     of raw sheet rows and ignores lot-level dividers and sale/cost lines.

Plain asserts, no framework. Run: python tests/test_validate.py
"""
from __future__ import annotations
import os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import validate
from compute import Tx, compute_row, compute_all


def _tx(**kw):
    base = dict(
        security_name="X", acquisition_date=date(2020, 1, 1),
        transfer_date=date(2024, 8, 1), purchase_cost=100.0,
        sale_consideration=1000.0, asset_type="equity",
    )
    base.update(kw)
    return Tx(**base)


# ---- build_validation -------------------------------------------------------

def test_lot_match_within_tolerance():
    # engine gain = 1000 - 100 = 900; broker says 900.5 -> within ₹1 abs tol
    r = compute_row(_tx(broker_gain=900.5))
    v = validate.build_validation([r])
    assert v.lots[0].status == "match"
    assert v.coverage == {"n": 1, "n_broker": 1}


def test_lot_mismatch_beyond_tolerance():
    r = compute_row(_tx(broker_gain=500.0))   # engine 900 vs broker 500
    v = validate.build_validation([r])
    assert v.lots[0].status == "mismatch"
    assert v.lots[0].delta == 400.0           # engine - broker


def test_lot_no_broker_figure_is_tracked_not_dropped():
    r = compute_row(_tx())                     # broker_gain left None
    v = validate.build_validation([r])
    assert v.lots[0].status == "no_broker"
    assert v.lots[0].broker_gain is None
    assert v.coverage == {"n": 1, "n_broker": 0}
    assert v.has_broker_gain() is False


def test_rollup_splits_short_long_and_total():
    # one long-term equity lot (held >12m) and one short-term (held <12m)
    lt = compute_row(_tx(acquisition_date=date(2020, 1, 1),
                         transfer_date=date(2024, 8, 1), broker_gain=900.0))
    st = compute_row(_tx(acquisition_date=date(2024, 1, 1),
                         transfer_date=date(2024, 8, 1),
                         sale_consideration=500.0, broker_gain=400.0))
    assert lt.is_ltcg is True and st.is_ltcg is False
    v = validate.build_validation([lt, st])
    assert v.rollup["long"]["engine"] == 900.0
    assert v.rollup["short"]["engine"] == 400.0      # 500 - 100
    assert v.rollup["total"]["engine"] == 1300.0
    assert v.rollup["total"]["broker"] == 1300.0
    assert v.rollup["total"]["status"] == "match"


def test_rollup_status_no_broker_when_column_absent():
    v = validate.build_validation([compute_row(_tx())])
    assert v.rollup["total"]["status"] == "no_broker"
    assert v.rollup["total"]["delta"] is None


def test_bucket_rollup_groups_by_bucket():
    lt = compute_row(_tx(broker_gain=900.0))                      # LTCG 112A Equity
    st = compute_row(_tx(acquisition_date=date(2024, 1, 1), broker_gain=850.0))  # STCG 111A Equity
    v = validate.build_validation([lt, st])
    keys = {b.key for b in v.buckets}
    assert "LTCG 112A Equity" in keys and "STCG 111A Equity" in keys


def test_counts_summarise_statuses():
    rs = [compute_row(_tx(broker_gain=900.0)),     # match
          compute_row(_tx(broker_gain=100.0)),     # mismatch
          compute_row(_tx())]                       # no broker
    c = validate.build_validation(rs).counts()
    assert c == {"lots": 3, "with_broker": 2, "match": 1, "mismatch": 1, "no_broker": 1}


def test_split_broker_columns_feed_validation_per_lot():
    # a long-term lot validated against the broker's LONG column (short column ignored)
    lt = compute_row(_tx(acquisition_date=date(2020, 1, 1), transfer_date=date(2024, 8, 1),
                         broker_stcg=0.0, broker_ltcg=900.0))
    # a short-term lot validated against the broker's SHORT column
    st = compute_row(_tx(acquisition_date=date(2024, 1, 1), transfer_date=date(2024, 8, 1),
                         sale_consideration=500.0, broker_stcg=400.0, broker_ltcg=0.0))
    v = validate.build_validation([lt, st])
    assert v.lots[0].broker_gain == 900.0 and v.lots[0].status == "match"
    assert v.lots[1].broker_gain == 400.0 and v.lots[1].status == "match"
    assert v.rollup["long"]["broker"] == 900.0
    assert v.rollup["short"]["broker"] == 400.0
    assert v.coverage == {"n": 2, "n_broker": 2}


# ---- scan_broker_totals -----------------------------------------------------

def test_scan_picks_label_then_value():
    # Zerodha-style: label cell, value to the right
    sheets = {"Equity": [["Short Term profit", "770897.38"],
                         ["Long Term profit", "259691.47"]]}
    figs = validate.scan_broker_totals(sheets)
    by_kind = {f.kind: f.value for f in figs}
    assert by_kind["short"] == 770897.38
    assert by_kind["long"] == 259691.47


def test_scan_takes_rightmost_total_column():
    # CAMS-style: period columns then a Total column -> take the rightmost number
    sheets = {"S": [["LongTermWithOutIndex-Capital Gain", "100", "200", "0", "300", "0", "600"]]}
    figs = validate.scan_broker_totals(sheets)
    assert len(figs) == 1
    assert figs[0].kind == "long" and figs[0].value == 600.0


def test_scan_ignores_dividers_without_numbers():
    # MProfit-style section divider: "Short Term Capital Gain" with no value -> skip
    sheets = {"S": [["", "Short Term Capital Gain", "", "", ""]]}
    assert validate.scan_broker_totals(sheets) == []


def test_scan_total_pnl_line():
    sheets = {"G&L": [["Client", "RK", "", "Realised P&L", "", "6241987.51"]]}
    figs = validate.scan_broker_totals(sheets)
    assert figs and figs[0].kind == "total" and figs[0].value == 6241987.51


def test_scan_does_not_grab_sale_or_cost_lines():
    sheets = {"S": [["Full Value Consideration", "1", "2", "3"],
                    ["Cost of Acquisition", "4", "5", "6"]]}
    assert validate.scan_broker_totals(sheets) == []


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} passed")


if __name__ == "__main__":
    main()
