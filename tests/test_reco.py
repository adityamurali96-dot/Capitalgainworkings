"""
tests/test_reco.py — checks for the AIS reconciliation engine.

Plain asserts, no framework. Run:
    python tests/test_reco.py
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reco


def _rows(*triples):
    # (name, isin, value) -> dict rows with the columns aggregate() expects
    return [{"Name": n, "ISIN": i, "Value": v} for n, i, v in triples]


def agg(rows):
    return reco.aggregate(rows, "Name", "ISIN", "Value", None)


def test_normalise_name_drops_suffixes_and_punct():
    assert reco.normalise_name("Reliance Industries Ltd.") == reco.normalise_name("RELIANCE INDUSTRIES")
    assert reco.normalise_name("Infosys Limited") == "INFOSYS"


def test_reco_key_prefers_valid_isin():
    k, kind = reco.reco_key("INE002A01018", "Reliance")
    assert kind == "isin" and k == "INE002A01018"
    k, kind = reco.reco_key("not-an-isin", "Reliance Ltd")
    assert kind == "name" and k == "RELIANCE"


def test_aggregate_sums_per_security_and_skips_blank_value():
    a = agg(_rows(("INFY", "INE009A01021", "100"),
                  ("INFY", "INE009A01021", "50"),
                  ("Divider row", "", "")))   # no value -> skipped
    assert set(a) == {"INE009A01021"}
    assert a["INE009A01021"].value == 150 and a["INE009A01021"].n == 2


def test_reconcile_matched_mismatched_and_one_sided():
    cg = agg(_rows(("INFY", "INE009A01021", "1000"),
                   ("WIPRO", "INE075A01022", "500"),
                   ("TCS", "INE467B01029", "800")))
    ais = agg(_rows(("INFY", "INE009A01021", "1000"),     # matches
                    ("WIPRO", "INE075A01022", "560"),     # mismatch (delta 60)
                    ("HDFC", "INE001A01036", "300")))     # only in AIS
    r = reco.reconcile(cg, ais, tol_abs=1.0, tol_pct=0.01)
    c = r.counts()
    assert c == {"matched": 1, "mismatched": 1, "only_cg": 1, "only_ais": 1}
    assert r.matched[0].isin == "INE009A01021"
    assert r.mismatched[0].delta == -60.0
    assert r.only_cg[0].name == "TCS"
    assert r.only_ais[0].name == "HDFC"


def test_tolerance_absorbs_small_rounding():
    cg = agg(_rows(("INFY", "INE009A01021", "1000000")))
    ais = agg(_rows(("INFY", "INE009A01021", "1000050")))  # 0.005% off
    r = reco.reconcile(cg, ais)
    assert r.counts()["matched"] == 1 and r.counts()["mismatched"] == 0


def test_name_fallback_matches_isin_side_to_name_side():
    # CG has the ISIN; AIS gives only the name -> pass 2 should still match them.
    cg = agg(_rows(("Infosys Ltd", "INE009A01021", "1000")))
    ais = agg(_rows(("INFOSYS LIMITED", "", "1000")))
    r = reco.reconcile(cg, ais)
    assert r.counts() == {"matched": 1, "mismatched": 0, "only_cg": 0, "only_ais": 0}


def test_totals_balance():
    cg = agg(_rows(("A", "INE000A01010", "100"), ("B", "INE000B01018", "200")))
    ais = agg(_rows(("A", "INE000A01010", "100"), ("C", "INE000C01016", "50")))
    t = reco.reconcile(cg, ais).totals()
    assert t["cg"] == 300 and t["ais"] == 150 and t["delta"] == 150


def test_reco_key_extracts_isin_embedded_in_description():
    # AIS reports the ISIN inside a verbose, single free-text description column.
    k, kind = reco.reco_key("", "RELIANCE INDUSTRIES LIMITED-EQ INE002A01018")
    assert kind == "isin" and k == "INE002A01018"
    # ...and inside an otherwise-noisy ISIN cell.
    k, kind = reco.reco_key("INE002A01018-EQ", "Reliance")
    assert kind == "isin" and k == "INE002A01018"


def test_normalise_name_strips_isin_and_instrument_noise():
    # The broker's terse name and the AIS verbose description collapse to one core.
    broker = reco.normalise_name("Reliance Industries Ltd")
    ais = reco.normalise_name("RELIANCE INDUSTRIES LIMITED - EQUITY SHARES")
    assert broker == ais == "RELIANCEINDUSTRIES"


def test_match_when_only_ais_carries_isin_in_its_description():
    # The hard AIS case: the broker name differs AND has no ISIN column, while AIS
    # buries the ISIN in its description. The embedded-ISIN key rescues the match.
    cg = agg(_rows(("RELIANCE INDS", "INE002A01018", "5000")))
    ais = agg(_rows(("Reliance Industries Limited EQ INE002A01018", "", "5000")))
    r = reco.reconcile(cg, ais)
    assert r.counts() == {"matched": 1, "mismatched": 0, "only_cg": 0, "only_ais": 0}
    # the recovered ISIN is what surfaces on the matched row, with a clean name
    assert r.matched[0].isin == "INE002A01018"
    assert "INE002A01018" not in r.matched[0].name


def test_aggregate_keys_by_embedded_isin_and_cleans_name():
    a = agg(_rows(("Infosys Ltd INE009A01021", "", "100"),
                  ("INFOSYS LIMITED - INE009A01021", "", "50")))  # same ISIN, two descriptions
    assert set(a) == {"INE009A01021"}
    s = a["INE009A01021"]
    assert s.value == 150 and s.n == 2 and "INE009A01021" not in s.name


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t(); print(f"  ok  {t.__name__}"); passed += 1
    print(f"\n{passed}/{len(tests)} passed")


if __name__ == "__main__":
    main()
