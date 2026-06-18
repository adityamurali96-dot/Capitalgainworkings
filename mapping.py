"""
mapping.py — turn user-mapped raw rows + per-source declarations into canonical Tx.

The user maps source columns to canonical fields and declares the per-source
facts the engine must not guess (cost meaning, default asset type/STT/50AA,
FMV presence + basis). This module only normalises and assembles — no tax logic.
"""
from __future__ import annotations
import re
from datetime import date, datetime, timedelta
from compute import Tx

CANONICAL_FIELDS = [
    "security_name", "acquisition_date", "purchase_cost", "transfer_date",
    "sale_consideration", "quantity", "isin", "transfer_expenses", "fmv_31jan2018",
]
REQUIRED = ["security_name", "acquisition_date", "purchase_cost", "transfer_date", "sale_consideration"]

_EXCEL_EPOCH = date(1899, 12, 30)  # Excel serial origin (accounts for 1900 leap bug)


def parse_amount(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("\u20b9", "").replace("Rs.", "").replace("Rs", "")
    s = s.replace("(", "-").replace(")", "")
    if s in ("", "-", "NA", "N.A", "N.A.", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):
        # Excel serial
        try:
            return _EXCEL_EPOCH + timedelta(days=int(v))
        except Exception:
            return None
    s = str(v).strip()
    if s in ("", "NA", "N.A", "None", "nan"):
        return None
    s = s.split(" ")[0]  # drop time component
    fmts = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y",
            "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%m/%d/%Y", "%Y/%m/%d")
    for f in fmts:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def build_tx(row: dict, mapping: dict, decl: dict, classification: dict | None = None):
    """
    row: one source record (header->value)
    mapping: {canonical_field: source_header}   (only mapped fields present)
    decl: per-source declarations:
        cost_basis_meaning, default_asset_type, default_stt_paid (bool),
        default_is_50aa (bool), fmv_basis ('per_unit'|'total'), source_label
    classification: optional per-row override {asset_type, basis, confidence, is_50aa, stt_paid}
    Returns (Tx, error|None).
    """
    def g(field):
        col = mapping.get(field)
        if not col:
            return None
        return row.get(col)

    name = g("security_name")
    name = str(name).strip() if name is not None else None
    acq = parse_date(g("acquisition_date"))
    xfer = parse_date(g("transfer_date"))
    cost = parse_amount(g("purchase_cost"))
    sale = parse_amount(g("sale_consideration"))
    qty = parse_amount(g("quantity"))
    fmv = parse_amount(g("fmv_31jan2018"))

    # Grandfathering basis. Under "fmv" the acquisition date may be absent (many
    # broker statements drop it for grandfathered pre-2018 holdings and give only
    # the 31-Jan-2018 FMV). When it is, the FMV decides:
    #   FMV present -> pre-2018 grandfathered lot, long-term  (infer acq 31-Jan-2018)
    #   no FMV       -> holding period unknown, treated as SHORT TERM (warned)
    acq_inferred, acq_note = False, ""
    if acq is None and decl.get("grandfathering_basis", "by_date") == "fmv" and xfer is not None:
        if fmv is not None and fmv != 0:
            acq = date(2018, 1, 31)          # < 01-Feb-2018 cutoff -> grandfathering eligible
            acq_inferred = True
            acq_note = "acq date absent; 31-Jan-2018 FMV present -> treated as pre-2018 grandfathered long-term"
        else:
            acq = xfer                       # zero holding -> short term
            acq_inferred = True
            acq_note = "acq date and 31-Jan-2018 FMV both absent -> treated as SHORT TERM (holding period unknown)"
    isin = g("isin")
    isin = str(isin).strip().upper() if isin else None
    if isin and not re.fullmatch(r"[A-Z0-9]{12}", isin):
        # ISIN sometimes embedded in name e.g. "ICICI Bank Ltd-INE090A01021"
        m = re.search(r"([A-Z]{2}[A-Z0-9]{9}\d)", isin)
        isin = m.group(1) if m else (isin if len(isin) == 12 else None)
    if not isin and name:
        m = re.search(r"([A-Z]{2}[A-Z0-9]{9}\d)", name.upper())
        if m:
            isin = m.group(1)
    exp = parse_amount(g("transfer_expenses")) or 0.0

    cls = classification or {}
    asset_type = cls.get("asset_type") or decl.get("default_asset_type")
    stt = cls.get("stt_paid", decl.get("default_stt_paid", asset_type in ("equity", "eof", "business_trust")))
    is_50aa = cls.get("is_50aa", decl.get("default_is_50aa", False))

    missing = []
    if not name: missing.append("security_name")
    if not acq: missing.append("acquisition_date")
    if not xfer: missing.append("transfer_date")
    if cost is None: missing.append("purchase_cost")
    if sale is None: missing.append("sale_consideration")
    if not asset_type: missing.append("asset_type (classify)")
    if missing:
        return None, f"{name or '?'}: missing/unparsed -> {', '.join(missing)}"

    try:
        tx = Tx(
            security_name=name, acquisition_date=acq, transfer_date=xfer,
            purchase_cost=cost, sale_consideration=sale, asset_type=asset_type,
            quantity=qty, isin=isin, transfer_expenses=exp,
            fmv_31jan2018=fmv, fmv_basis=decl.get("fmv_basis", "per_unit"),
            stt_paid=bool(stt), is_50aa=bool(is_50aa),
            cost_basis_meaning=decl.get("cost_basis_meaning", "raw"),
            source_label=decl.get("source_label", ""),
            classification_basis=cls.get("basis", "declared (per-source default)"),
            classification_confidence=cls.get("confidence", "declared"),
            acq_inferred=acq_inferred, acq_note=acq_note,
        )
        return tx, None
    except ValueError as e:
        return None, f"{name}: {e}"
