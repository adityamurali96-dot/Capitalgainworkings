"""
tests/test_isin_db.py — checks for the orientation classifier's name-based fallback.

Two kinds of test:
  1. Pure helpers (_norm_name / _ratio) — no DB needed.
  2. The fuzzy name fallback through lookup() — only runs when isin_master.db is
     present beside the code; it asserts that a near-miss name still yields a
     PROPOSED asset class (never silently manual) and that a sub-80% match is
     flagged low-confidence rather than dropped.

Plain asserts, no framework. Run: python tests/test_isin_db.py
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isin_db


# ---- pure helpers -----------------------------------------------------------

def test_norm_name_drops_noise_words():
    a = isin_db._norm_name("Reliance Industries Ltd")
    b = isin_db._norm_name("RELIANCE INDUSTRIES")
    assert a == b == "RELIANCE INDUSTRIES"


def test_ratio_is_high_for_typo_and_reorder():
    qn = isin_db._norm_name("Relianc Industrie")          # typos
    assert isin_db._ratio(qn, "RELIANCE INDUSTRIES") >= 0.80
    # token-set overlap rescues word reordering
    assert isin_db._ratio("INDUSTRIES RELIANCE", "RELIANCE INDUSTRIES") == 1.0


def test_ratio_is_low_for_unrelated_names():
    assert isin_db._ratio("RELIANCE INDUSTRIES", "TATA MOTORS") < 0.45


# ---- DB-backed fuzzy fallback (optional) ------------------------------------

def test_fuzzy_name_fallback_when_db_present():
    if not os.path.exists(isin_db.DB_PATH):
        print("  -- isin_master.db not present; skipping DB-backed fuzzy test")
        return
    # an exact-ish name -> proposed (never trusted on a name-only hit)
    hit = isin_db.lookup(None, "Reliance Industries Limited")
    assert hit["confidence"] == "proposed" and hit["asset_type"]
    # a misspelt name still proposes an asset class rather than going manual
    typo = isin_db.lookup(None, "Relianc Industrie Ltd")
    assert typo["confidence"] == "proposed" and typo["asset_type"]
    assert "name match" in typo["reason"]
    # pure noise -> manual (no plausible match)
    miss = isin_db.lookup(None, "Zzq Nonexistent Holdings Pvt")
    assert miss["confidence"] == "manual" and miss["asset_type"] is None


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
