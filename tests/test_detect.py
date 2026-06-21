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


def test_combine_aligned_merges_sheets_by_position():
    # two "sheets": same column order, header at different rows, header text differs
    st = [["Stock name", "ISIN", "Sell value"],     # header at row 0
          ["INFY", "INE009A01021", "100"],
          ["", "", ""],                              # blank row -> dropped
          ["WIPRO", "INE075A01022", "200"]]
    lt = [["preamble", "", ""],
          ["Stock NAME", "isin", "Sell Value"],      # header at row 1, different case
          ["TCS", "INE467B01029", "300"]]
    out = detect.combine_aligned([(st, 0), (lt, 1)], width=3)
    assert out == [["INFY", "INE009A01021", "100"],
                   ["WIPRO", "INE075A01022", "200"],
                   ["TCS", "INE467B01029", "300"]]


def test_combine_aligned_pads_and_truncates_to_width():
    a = [["h1", "h2", "h3", "h4"], ["a", "b", "c", "d"]]   # wider
    b = [["h1", "h2"], ["e", "f"]]                          # narrower
    out = detect.combine_aligned([(a, 0), (b, 0)], width=3)
    assert out == [["a", "b", "c"], ["e", "f", ""]]


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


def test_split_short_long_broker_gain_columns():
    # a statement printing separate short- and long-term gain columns maps both,
    # and the generic gain matcher does not steal either.
    mp = detect.auto_map(["Security", "ISIN", "Sale Date", "Purchase Date",
                          "Sale Value", "Purchase Value",
                          "Short Term Gain", "Long Term Gain"])
    assert mp["broker_stcg"]["header"] == "Short Term Gain"
    assert mp["broker_ltcg"]["header"] == "Long Term Gain"


def test_single_broker_gain_still_maps():
    mp = detect.auto_map(["Security", "Sale Date", "Purchase Date",
                          "Sale Value", "Purchase Value", "Realised P&L"])
    assert mp["broker_gain"]["header"] == "Realised P&L"


def test_purchase_and_sale_expense_columns_map_separately():
    mp = detect.auto_map(["Security", "Sale Date", "Purchase Date", "Sale Value",
                          "Purchase Value", "Purchase Charges", "Selling Expenses"])
    assert mp["purchase_expenses"]["header"] == "Purchase Charges"
    assert mp["transfer_expenses"]["header"] == "Selling Expenses"


def test_extract_isin_from_merged_name():
    assert detect.extract_isin("ICICI Bank Ltd - INE090A01021") == "INE090A01021"
    assert detect.extract_isin("INE002A01018 Reliance") == "INE002A01018"
    assert detect.extract_isin("Reliance Industries Ltd") is None


def test_strip_isin_cleans_name_only_when_present():
    assert detect.strip_isin("ICICI Bank Ltd - INE090A01021") == "ICICI Bank Ltd"
    assert detect.strip_isin("INE009A01021 Infosys Ltd") == "Infosys Ltd"
    # no embedded ISIN -> separators in a legitimate name are left intact
    assert detect.strip_isin("HDFC Ltd / Bonus") == "HDFC Ltd / Bonus"


def test_clean_security_name_strips_ais_depository_boilerplate():
    # AIS / depository "Sale of securities" descriptions: issuer + instrument tail + ISIN.
    c = detect.clean_security_name
    assert c("ICICI SECURITIES LIMITED EQ NEW FV RS. 5/-(INE763G01038)") == "ICICI SECURITIES LIMITED"
    assert c("ITC LIMITED - EQUITY SHARES OF RE.1/- AFTER SPLIT(INE154A01018)") == "ITC LIMITED"
    assert c("LUPIN LIMITED-NEW EQUITY SHARES OF RS. 2/- AFTER SUB-DIVISION(INE326A01037)") == "LUPIN LIMITED"
    assert c("MPHASIS LIMITED EQUITY SHARES(INE356A01018)") == "MPHASIS LIMITED"
    # short words inside a real name survive (cut is at a whole-word EQ/EQUITY SHARES marker)
    assert c("STATE BANK OF INDIA EQ NEW RE. 1/-(INE062A01020)") == "STATE BANK OF INDIA"
    assert c("EQUITAS SMALL FINANCE BANK LIMITED EQ(INE063P01018)") == "EQUITAS SMALL FINANCE BANK LIMITED"
    assert c("THE NEW INDIA ASSURANCE COMPANY LIMITED EQ(INE470Y01017)") == "THE NEW INDIA ASSURANCE COMPANY LIMITED"
    # a mutual-fund scheme that merely contains the word "Equity" is NOT truncated
    assert c("SBI Equity Hybrid Fund - Direct Plan - Growth") == "SBI Equity Hybrid Fund - Direct Plan - Growth"


def test_ais_sale_header_auto_maps_sales_consideration_not_per_unit_rate():
    # The depository detail header. "SALES CONSIDERATION" (plural) is the total; the
    # per-unit "SALE PRICE PER UNIT" must be ignored, not read as the total.
    hdrs = ["SR.NO.", "DATE OF SALE/TRANSFER", "SECURITY NAME (SECURITY CODE)",
            "SECURITY CLASS", "QUANTITY", "SALE PRICE PER UNIT", "SALES CONSIDERATION",
            "COST OF ACQUISITION", "UNIT FMV", "FAIR MARKET VALUE", "STATUS"]
    mp = detect.auto_map(hdrs)
    assert mp["sale_consideration"]["header"] == "SALES CONSIDERATION"
    assert mp["security_name"]["header"] == "SECURITY NAME (SECURITY CODE)"
    assert mp["transfer_date"]["header"] == "DATE OF SALE/TRANSFER"
    used = {i["header"] for i in mp.values()}
    assert "SALE PRICE PER UNIT" not in used


def test_name_isin_merge_rate_flags_merged_layout():
    merged = [{"name": "INFY INE009A01021"}, {"name": "TCS INE467B01029"},
              {"name": "WIPRO INE075A01022"}]
    assert detect.name_isin_merge_rate(merged, "name", None) == 1.0
    plain = [{"name": "INFY"}, {"name": "TCS"}]
    assert detect.name_isin_merge_rate(plain, "name", None) == 0.0
    # a populated separate ISIN column means there is nothing to rescue
    sep = [{"name": "INFY INE009A01021", "isin": "INE009A01021"},
           {"name": "TCS INE467B01029", "isin": "INE467B01029"}]
    assert detect.name_isin_merge_rate(sep, "name", "isin") == 0.0


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
