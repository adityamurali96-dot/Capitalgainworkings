import csv, re, sqlite3, datetime, os
from collections import Counter

SRC = "/tmp/isin-database-main/csv-data"
OUT = "/tmp/out/isin_master.db"

# NO word boundary: AMFI jams growth+reinvestment ISINs together
# (e.g. INF209K01157INF209K01CE5). findall is left-to-right non-overlapping,
# so a 24-char run yields both 12-char ISINs.
ISIN_RE = re.compile(r'IN[0-9A-Z]{10}')
def isins(s): return ISIN_RE.findall((s or "").upper())

rows = {}
def put(isin, **kw):
    r = rows.setdefault(isin, {"isin":isin,"name":"","instrument_type":"",
        "suggested_tax_class":"","scheme_category":"","series":"","sources":set()})
    src = kw.pop("source", None)
    if src: r["sources"].add(src)
    for k,v in kw.items():
        if v and not r.get(k):          # first substantive writer wins; sources accumulate
            r[k]=v

def mf_class(cat):
    c = (cat or "").lower()
    if "elss" in c or c.startswith("equity") or ("equity" in c and "scheme" in c):
        return "equity_mf"
    if (c == "income" or "debt scheme" in c or
        any(w in c for w in ["debt","income","liquid","gilt","money market","floating",
            "overnight","duration","bond","credit risk","corporate","assured"])):
        return "debt_mf"
    return "review"   # hybrid / balanced / arbitrage / other scheme / growth / solution / index / fof

# International / overseas equity is NOT equity-oriented for Indian tax -> review.
INTL_RE = re.compile(r'\b(global|international|overseas|world|wide|u\.?s\.?|nasdaq|s\s*&\s*p\s*500|'
    r'japan|china|hang\s*seng|emerging|foreign|europe|asia|asean|brazil|taiwan|korea|'
    r'developed market|nyse|fang)\b', re.I)
# Commodity / debt / money-market ETFs are not equity-oriented -> review.
NONEQ_ETF_RE = re.compile(r'(gold|silver|liquid|debt|gilt|g\s*-?\s*sec|sdl|bond|commodit|'
    r'overnight|money market|t\s*-?bill|target maturit|\bpsu\b|bharat bond|nifty\s*1d|'
    r'\brate\b|\bgol\b)', re.I)   # \bgol\b catches the "UTI Gol[d]" truncation in etf.csv

def noneq_signal(*names):
    s = " ".join(n for n in names if n)
    return bool(NONEQ_ETF_RE.search(s) or INTL_RE.search(s))

# 1) active.csv : Sr.No., Company Name, ISIN, Instrument Type --------------------
with open(f"{SRC}/active.csv", encoding="utf-8", errors="replace") as f:
    for r in csv.reader(f):
        if not r or r[0].strip().lower().startswith("sr"): continue
        if len(r) < 4: continue
        name, isin, typ = r[1].strip(), r[2].strip().upper(), r[3].strip().upper()
        if not ISIN_RE.fullmatch(isin):
            m = isins(",".join(r)); isin = m[0] if m else ""
        if not isin: continue
        if typ == "EQUITY":
            put(isin, name=name, instrument_type="EQUITY",
                suggested_tax_class="equity_listed", source="nse_active")
        elif typ == "DEBT":
            put(isin, name=name, instrument_type="DEBT_SECURITY",
                suggested_tax_class="debt_security", source="nse_active")
        else:
            put(isin, name=name, source="nse_active")

# 2) equity.csv : NAME(1), SERIES(2), ISIN NUMBER(6) ---------------------------
with open(f"{SRC}/equity.csv", encoding="utf-8", errors="replace") as f:
    for r in csv.reader(f):
        if not r or r[0].strip().upper()=="SYMBOL": continue
        if len(r) < 7: continue
        name, series, isin = r[1].strip(), r[2].strip(), r[6].strip().upper()
        if not ISIN_RE.fullmatch(isin): continue
        put(isin, name=name, instrument_type="EQUITY",
            suggested_tax_class="equity_listed", series=series, source="nse_equity")

# 3) mf.csv : Scheme Name(2), Scheme Category(4), ISIN field = last cell --------
# (processed BEFORE etf so ETF rows carry the full untruncated AMFI name for the
#  equity-orientation decision below)
amfi_name = {}   # isin -> full AMFI scheme name (untruncated)
with open(f"{SRC}/mf.csv", encoding="utf-8", errors="replace") as f:
    for r in csv.reader(f):
        if not r or r[0].strip().upper()=="AMC": continue
        if len(r) < 10: continue
        name, cat = r[2].strip(), r[4].strip()
        ii = isins(r[-1])                          # splits concatenated growth+reinvestment ISINs
        if not ii: continue
        tc = mf_class(cat)
        if tc == "equity_mf" and (INTL_RE.search(name)):
            tc = "review"                          # international/overseas equity demotion
        for isin in ii:
            amfi_name[isin] = name
            put(isin, name=name, instrument_type="MF",
                suggested_tax_class=tc, scheme_category=cat, source="amfi_mf")

# 4) etf.csv : Security Name(2), ISIN Number(5) --------------------------------
# The ETF source is authoritative for the ETF flag and OVERRIDES the active.csv /
# mf.csv hit. Equity-index ETFs -> equity_etf; gold/silver/debt/commodity/
# international ETFs -> review (not equity-oriented).
with open(f"{SRC}/etf.csv", encoding="utf-8", errors="replace") as f:
    for r in csv.reader(f):
        if not r or r[0].strip().lower()=="symbol": continue
        if len(r) < 6: continue
        etf_name, isin = r[2].strip(), r[5].strip().upper()
        if not ISIN_RE.fullmatch(isin): continue
        tc = "review" if noneq_signal(etf_name, amfi_name.get(isin,"")) else "equity_etf"
        rec = rows.setdefault(isin, {"isin":isin,"name":"","instrument_type":"",
            "suggested_tax_class":"","scheme_category":"","series":"","sources":set()})
        rec["name"] = rec["name"] or etf_name
        rec["instrument_type"]    = "ETF"
        rec["suggested_tax_class"]= tc
        rec["sources"].add("nse_etf")

# 5) debt.csv : ISIN positional last; never downgrade an existing equity hit -----
with open(f"{SRC}/debt.csv", encoding="utf-8", errors="replace") as f:
    for r in csv.reader(f):
        if not r or r[0].strip().upper()=="SYMBOL": continue
        ii = isins(r[-1]) or isins(",".join(r))
        if not ii: continue
        name = r[1].strip() if len(r) > 1 else ""
        isin = ii[-1]
        if isin not in rows or not rows[isin]["instrument_type"]:
            put(isin, name=name, instrument_type="DEBT_SECURITY",
                suggested_tax_class="debt_security", source="nse_debt")
        else:
            put(isin, source="nse_debt")

# country / issuer-type from ISIN structure ------------------------------------
for isin, r in rows.items():
    r["country"]     = "IN" if isin.startswith("IN") else isin[:2]
    r["issuer_char"] = isin[2] if len(isin) > 2 else ""

# ---- write sqlite ------------------------------------------------------------
if os.path.exists(OUT): os.remove(OUT)
con = sqlite3.connect(OUT); cur = con.cursor()
cur.execute("""CREATE TABLE instruments(
  isin TEXT PRIMARY KEY, name TEXT, country TEXT, issuer_char TEXT,
  instrument_type TEXT, suggested_tax_class TEXT, scheme_category TEXT,
  series TEXT, sources TEXT)""")
cur.execute("""CREATE TABLE manual_overrides(
  isin TEXT PRIMARY KEY, name TEXT, instrument_type TEXT, suggested_tax_class TEXT,
  entered_on TEXT, note TEXT)""")
cur.execute("""CREATE TABLE _meta(key TEXT, value TEXT)""")
for isin, r in rows.items():
    cur.execute("INSERT INTO instruments VALUES(?,?,?,?,?,?,?,?,?)",
        (isin, r["name"], r.get("country",""), r.get("issuer_char",""),
         r["instrument_type"], r["suggested_tax_class"], r["scheme_category"],
         r["series"], ",".join(sorted(r["sources"]))))
cur.execute("CREATE INDEX idx_name ON instruments(name)")
cur.execute("CREATE INDEX idx_type ON instruments(instrument_type)")
meta = {
  "built_utc": datetime.datetime.now(datetime.UTC).isoformat(),
  "provenance": "Community mirror github.com/bhavansh/isin-database "
                "(NSE active/equity/etf/debt + AMFI scheme master). NOT a direct exchange pull. "
                "Refresh from source via build_isin_db.py on a networked machine.",
  "purpose": "Instrument classification only (equity vs debt vs MF vs ETF). "
             "suggested_tax_class is advisory; 'review' = pause and confirm. "
             "Does NOT decide grandfathering or final tax bucket.",
  "row_count": str(len(rows)),
}
for k,v in meta.items(): cur.execute("INSERT INTO _meta VALUES(?,?)", (k,v))
con.commit(); con.close()

# ---- report ------------------------------------------------------------------
print(f"rows: {len(rows)}")
print("instrument_type:", dict(Counter(r['instrument_type'] for r in rows.values())))
print("suggested_tax_class:", dict(Counter(r['suggested_tax_class'] for r in rows.values())))
print("ETF tax split:", dict(Counter(r['suggested_tax_class'] for r in rows.values() if r['instrument_type']=='ETF')))
