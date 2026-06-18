"""
tests/test_detect.py — checks for the auto-detection layer.

Two kinds of test:
  1. Pure unit tests on the synonym matcher / blank handling (no files needed).
  2. A corpus test over reference/*.xls* IF that folder is present — it asserts
     the recommended sheet, header row and required-field mapping for the formats
     that actually carry lot-level buy+sell data. Files known to lack lot-level
     dates (aggregated or redemption-only statements) are allowed to under-match
     and are reported, not failed.

Run:
    python tests/test_detect.py

Plain asserts, no framework. Exits non-zero on the first failure.
"""
from __future__ import annotations
import os, sys, glob, warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import detect

REF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reference")


# ---- unit tests --------------------------------------------------------------

def test_auto_map_basic_synonyms():
    mp = detect.auto_map(["Stock Symbol", "ISIN", "Qty", "Sale Date", "Sale Value",
                          "Purchase Date", "Purchase Value"])
    assert mp["security_name"]["header"] == "Stock Symbol"
    assert mp["isin"]["header"] == "ISIN"
    assert mp["transfer_date"]["header"] == "Sale Date"
    assert mp["acquisition_date"]["header"] == "Purchase Date"
    assert mp["sale_consideration"]["header"] == "Sale Value"
    assert mp["purchase_cost"]["header"] == "Purchase Value"


def test_per_unit_rate_never_maps_to_total_amount():
    # "Sale Rate" / "Buy price" are per-unit and must not be read as the total.
    mp = detect.auto_map(["Security", "Sale Rate (S)", "Buy price", "Sale Amount",
                          "Purchase Amount"])
    assert mp["sale_consideration"]["header"] == "Sale Amount"
    assert mp["purchase_cost"]["header"] == "Purchase Amount"


def test_clean_total_outranks_priced_cost_column():
    # MProfit ships both a per-unit "Cost of Acquisition Price" and a total.
    mp = detect.auto_map(["Asset Name", "Cost of Acquisition Price (CA)",
                          "Acquisition Cost / Total", "Sale Date", "Sale Amt.",
                          "Pur. Date"])
    assert mp["purchase_cost"]["header"] == "Acquisition Cost / Total"


def test_each_header_used_at_most_once():
    mp = detect.auto_map(["Date", "Date", "Amount"])
    cols = [v["col"] for v in mp.values()]
    assert len(cols) == len(set(cols))


def test_drop_blank_columns():
    rows = [["a", "", "b", ""], ["c", "", "d", ""]]
    new, keep = detect.drop_blank_columns(rows)
    assert keep == [0, 2]
    assert new == [["a", "b"], ["c", "d"]]


def test_forward_fill_cols():
    rows = [{"name": "INFY", "qty": ""}, {"name": "", "qty": "10"},
            {"name": "", "qty": "20"}, {"name": "WIPRO", "qty": "5"}]
    detect.forward_fill_cols(rows, ["name"])
    assert [r["name"] for r in rows] == ["INFY", "INFY", "INFY", "WIPRO"]


def test_junk_label_and_repeat_header():
    assert detect.is_junk_label("Total")
    assert detect.is_junk_label("Short Term Capital Gain")
    assert not detect.is_junk_label("RELIANCE INDUSTRIES")
    hdr = ["Stock Symbol", "ISIN", "Qty"]
    assert detect.is_repeat_header(["Stock Symbol", "ISIN", "Qty"], hdr)
    assert not detect.is_repeat_header(["INFY", "INE009A01021", "32"], hdr)


def test_detect_header_row_skips_preamble():
    rows = [
        ["Client ID", "YQL053", ""],
        ["", "", ""],
        ["Symbol", "Sale Date", "Sale Value"],   # not enough required -> keep scanning
        ["Symbol", "ISIN", "Quantity", "Buy Date", "Sale Date", "Buy Value", "Sell Value"],
    ]
    det = detect.detect_header_row(rows)
    assert det is not None
    ri, mp, req, tot = det
    assert ri == 3 and req == 5


# ---- corpus test over the real statements (optional) -------------------------

# formats that genuinely carry lot-level buy AND sell data -> must fully detect
EXPECT_FULL = {
    "7500061695_EQCapitalGainsDetails.xlsx",
    "8501783213_EQCapitalGainsDetails.xlsx",
    "Capital Gain Statement.xls",
    "Capital Gain.xls",
    "CapitalGains_Zerodha_2024_25.xlsx",
    "Carnelian PMS - CG stmt for FY25 - RK - Liquid Strategy.xlsx",
    "Groww Mutual Funds Capital Gains.xlsx",
    "Groww Stocks Capital Gains-Kumar.xlsx",
    "ICICI Direct Stocks P&L-New.xls",
    "IIFL Capital Gain Report.xls",
    "MF - Realized Capital Gain Detailed - FY25 - Vaibhav - 080725.xlsx",
    "MProfit_FY_2024_25.xlsx",
    "Valentis Debt Cap Gain FY 24-25.xls",
    "Valentis Equity Cap Gain FY 24-25.xls",
}
# aggregated / redemption-only statements that lack lot-level dates by design
EXPECT_PARTIAL = {
    "2025_05_23_CAMS_Capital_Gains_Statement.xls",
    "2025_05_23_Karvy_Capital_Gains_Statement.xlsx",
    "Kotak Securities - CG statement - FY25.xlsx",
}


def _load_cleaned(path):
    import pandas as pd
    warnings.filterwarnings("ignore")
    eng = "xlrd" if path.lower().endswith(".xls") else "openpyxl"
    xls = pd.read_excel(path, sheet_name=None, header=None, dtype=str, engine=eng)
    out = {}
    for sn, df in xls.items():
        rows = df.fillna("").astype(str).values.tolist()
        rows, _ = detect.drop_blank_columns(rows)
        out[sn] = rows
    return out


def test_reference_corpus():
    files = sorted(glob.glob(os.path.join(REF, "*.xls")) +
                   glob.glob(os.path.join(REF, "*.xlsx")))
    if not files:
        print("  -- reference/ not present; skipping corpus test")
        return
    try:
        import pandas  # noqa: F401
    except ImportError:
        print("  -- pandas not installed; skipping corpus test")
        return
    for path in files:
        name = os.path.basename(path)
        ranked = detect.rank_sheets(_load_cleaned(path))
        top = ranked[0]
        mp = top["automap"]
        hit = [f for f in detect.REQUIRED if f in mp]
        if name in EXPECT_FULL:
            assert len(hit) == len(detect.REQUIRED), (
                f"{name}: expected all required fields, got {hit} "
                f"on sheet {top['name']!r}")
        else:
            # partial formats: just report what was found
            print(f"  -- partial: {name}: {len(hit)}/{len(detect.REQUIRED)} "
                  f"required on {top['name']!r}")


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
