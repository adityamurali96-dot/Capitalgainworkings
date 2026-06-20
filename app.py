"""
app.py — the cg-engine hub (Flask).

Flow:  upload -> pick header row -> map columns + declare -> classify (3-state
gate, 50AA confirm) -> compute -> download Output A + Output B.

State lives in an in-memory JOBS dict keyed by a job id (a cookie). Single-user,
local tool — no DB, no auth. The preparer is responsible for the figures; the
engine surfaces every classification and computation basis so they can verify.
"""
from __future__ import annotations
import io, os, uuid, traceback
from flask import (Flask, request, render_template, redirect, url_for,
                   session, send_file, flash)
import pandas as pd

import compute, mapping, isin_db, detect, reco, validate
from writer_summary import write_summary
from writer_winman import write_winman
from writer_reco import write_reco
from writer_validation import write_validation

app = Flask(__name__)
app.secret_key = os.environ.get("CG_SECRET", "cg-engine-local-dev")
JOBS: dict[str, dict] = {}

OUT_DIR = os.environ.get("CG_OUT", os.path.join(os.path.expanduser("~"), "Downloads", "cg-engine-out"))
os.makedirs(OUT_DIR, exist_ok=True)


def job():
    jid = session.get("jid")
    if not jid or jid not in JOBS:
        jid = uuid.uuid4().hex
        session["jid"] = jid
        JOBS[jid] = {}
    return JOBS[jid]


def _read_any(file_storage, header_row=None):
    name = file_storage.filename.lower()
    data = file_storage.read()
    bio = io.BytesIO(data)
    if name.endswith(".csv") or name.endswith(".tsv"):
        sep = "\t" if name.endswith(".tsv") else ","
        return {"(csv)": pd.read_csv(io.BytesIO(data), sep=sep, header=header_row, dtype=str)}
    engine = "xlrd" if name.endswith(".xls") else "openpyxl"
    xls = pd.read_excel(bio, sheet_name=None, header=header_row, dtype=str, engine=engine)
    return xls


def _suggest_forward_fill(j: dict, automap: dict) -> bool:
    """Propose carrying name/ISIN down when they are far sparser than the sale
    columns — the signature of a grouped layout (e.g. IIFL lists the scrip once)."""
    rows = j.get("rows_below", [])
    if not rows:
        return False
    def fill_rate(field):
        col = automap.get(field, {}).get("col")
        if col is None:
            return None
        n = sum(1 for r in rows if col < len(r) and str(r[col]).strip())
        return n / len(rows)
    name_r = fill_rate("security_name")
    date_r = fill_rate("transfer_date") or fill_rate("sale_consideration")
    return bool(name_r is not None and date_r and name_r < 0.6 * date_r)


def _suggest_isin_split(j: dict, automap: dict) -> bool:
    """Propose auto-splitting a merged "name + ISIN" security column when a large
    share of name cells carry an embedded ISIN and no separate ISIN column is
    already populated (the common broker layout)."""
    headers = j.get("headers", [])
    rows = j.get("rows_below", [])
    name_h = automap.get("security_name", {}).get("header")
    isin_h = automap.get("isin", {}).get("header")
    if not name_h or not rows:
        return False
    dict_rows = [dict(zip(headers, r)) for r in rows[:200]]
    return detect.name_isin_merge_rate(dict_rows, name_h, isin_h) >= 0.4


def _finalise_rows(j: dict, m: dict, forward_fill: bool) -> list[dict]:
    """Build the clean per-lot rows the engine will consume: dict-ify, optionally
    forward-fill grouped name/ISIN, then drop section-divider, total/footnote and
    repeated-header rows. The skipped count is stashed for the next screen."""
    headers = j["headers"]
    raw_rows = j.get("rows_below", [])
    raw_sheets = j.get("row_sheets", [None] * len(raw_rows))
    rows = [dict(zip(headers, r)) for r in raw_rows]
    if forward_fill:
        ff_cols = [m[f] for f in ("security_name", "isin") if m.get(f)]
        if ff_cols:
            detect.forward_fill_cols(rows, ff_cols)
    name_c = m.get("security_name")
    date_c = m.get("transfer_date")
    sale_c = m.get("sale_consideration")
    kept, kept_sheets, skipped = [], [], 0
    for r, sn in zip(rows, raw_sheets):
        if detect.is_repeat_header(list(r.values()), headers):
            skipped += 1; continue
        if name_c and detect.is_junk_label(r.get(name_c, "")):
            skipped += 1; continue
        # a real lot has at least a sale date or a sale amount
        has_sale = (date_c and str(r.get(date_c, "")).strip()) or \
                   (sale_c and str(r.get(sale_c, "")).strip())
        if not has_sale:
            skipped += 1; continue
        kept.append(r); kept_sheets.append(sn)
    j["skipped"] = skipped
    j["data_sheets"] = kept_sheets    # sheet of origin per kept row (multi-sheet runs)
    return kept


def _dedupe_headers(cells):
    """Header cells -> unique non-blank names (blanks become col1, col2, …)."""
    seen, clean = {}, []
    for i, h in enumerate(str(x).strip() for x in cells):
        h = h or f"col{i+1}"
        if h in seen:
            seen[h] += 1; h = f"{h}.{seen[h]}"
        else:
            seen[h] = 0
        clean.append(h)
    return clean


def _extract_records(file_storage):
    """Read a file for reconciliation: auto-pick the data sheet + header row,
    auto-map columns, and return (clean dict rows, automap, info). Reuses the
    same detection + blank/divider handling as the CG flow, plus a forward-fill
    for grouped layouts. The auto-map is a starting point — the AIS file's
    columns are shown for confirmation/override on the /ais/map screen; the
    broker/CG file is consumed automatically. `info["headers"]` carries the
    detected header names so the confirm screen can render a column preview."""
    sheets = _read_any(file_storage, header_row=None)
    cleaned = {}
    for k, v in sheets.items():
        rows = v.fillna("").astype(str).values.tolist()
        rows, _ = detect.drop_blank_columns(rows)
        cleaned[k] = rows
    ranked = detect.rank_sheets(cleaned)
    top = ranked[0] if ranked else None
    info = {"sheet": None, "header_row": None, "n": 0, "headers": []}
    if not top or top["header_row"] is None:
        return [], {}, info
    sheet, hr = top["name"], top["header_row"]
    rows = cleaned[sheet]
    clean = _dedupe_headers(rows[hr])
    automap = detect.auto_map(clean)
    below = [dict(zip(clean, r)) for r in rows[hr + 1:] if any(str(c).strip() for c in r)]
    name_c = automap.get("security_name", {}).get("header")
    isin_c = automap.get("isin", {}).get("header")
    val_c = automap.get("sale_consideration", {}).get("header")
    if name_c and val_c:  # grouped layout? carry name/ISIN down onto the lot rows
        nfill = sum(1 for r in below if str(r.get(name_c, "")).strip())
        vfill = sum(1 for r in below if str(r.get(val_c, "")).strip())
        if vfill and nfill < 0.6 * vfill:
            detect.forward_fill_cols(below, [c for c in (name_c, isin_c) if c])
    out = []
    for r in below:
        if detect.is_repeat_header(list(r.values()), clean):
            continue
        if name_c and detect.is_junk_label(r.get(name_c, "")):
            continue
        out.append(r)
    info = {"sheet": sheet, "header_row": hr, "n": len(out),
            "automap": automap, "headers": clean}
    return out, automap, info


# the four fields the reconciliation needs from each side
RECO_FIELDS = ["security_name", "isin", "sale_consideration", "quantity"]


def _reco_cols(automap):
    g = lambda f: automap.get(f, {}).get("header")
    return g("security_name"), g("isin"), g("sale_consideration"), g("quantity")


@app.route("/")
def home():
    session.pop("jid", None)
    return render_template("menu.html", db_status=isin_db.db_status())


@app.route("/cg")
def cg_home():
    """Entry to the Capital Gain Summary flow (upload -> pick -> map -> compute)."""
    session.pop("jid", None)
    return render_template("upload.html", db_status=isin_db.db_status())


@app.route("/upload", methods=["POST"])
def upload():
    j = job()
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a file."); return redirect(url_for("home"))
    try:
        sheets = _read_any(f, header_row=None)  # raw, no header yet
    except Exception as e:
        flash(f"Could not read file: {e}"); return redirect(url_for("home"))
    # drop all-blank columns (the .xls merged-cell spillover) so every later
    # step — preview, header detect, mapping — sees only real columns.
    cleaned = {}
    for k, v in sheets.items():
        rows = v.fillna("").astype(str).values.tolist()
        rows, _ = detect.drop_blank_columns(rows)
        cleaned[k] = rows
    j["sheets"] = cleaned
    j["filename"] = f.filename
    # auto-detect: which sheet is the data, which row is the header, column wiring
    j["ranked"] = detect.rank_sheets(cleaned)
    return redirect(url_for("pick"))


@app.route("/pick", methods=["GET", "POST"])
def pick():
    j = job()
    if "sheets" not in j:
        return redirect(url_for("home"))
    sheet_names = list(j["sheets"].keys())
    ranked = {r["name"]: r for r in j.get("ranked", [])}
    order = [r["name"] for r in j.get("ranked", [])]  # best-data-first

    if request.method == "POST":
        selected = request.form.getlist("sheets")
        if not selected:
            flash("Tick at least one sheet."); return redirect(url_for("pick"))
        # per-sheet header row (defaults to the detected one)
        hdr_of = {}
        for sn in selected:
            raw = request.form.get(f"header_row_{sn}")
            det = ranked.get(sn, {}).get("header_row")
            hdr_of[sn] = int(raw) if (raw not in (None, "")) else (det if det is not None else 0)
        # primary = the selected sheet ranked highest (best detected structure);
        # its header row defines the unified columns the others align onto.
        primary = min(selected, key=lambda s: order.index(s) if s in order else 999)
        unified = _dedupe_headers(j["sheets"][primary][hdr_of[primary]])
        width = len(unified)
        # combine: every selected sheet's rows, position-aligned to the unified
        # width (same column order across ST/LT or per-account splits). Different
        # header text/case across sheets is irrelevant — mapping is on `unified`.
        per_sheet_rows = {sn: detect.combine_aligned([(j["sheets"][sn], hdr_of[sn])], width)
                          for sn in selected}
        combined = [r for sn in selected for r in per_sheet_rows[sn]]
        # parallel list: which sheet each combined row came from, so the classify
        # screen can group line items under their sheet (e.g. "Sheet 1 — Short term").
        row_sheets = [sn for sn in selected for _ in per_sheet_rows[sn]]
        sheet_counts = {sn: len(rows) for sn, rows in per_sheet_rows.items()}
        j["headers"] = unified
        j["rows_below"] = combined
        j["row_sheets"] = row_sheets
        j["selected_sheets"] = selected
        j["sheet_counts"] = sheet_counts
        j["hdr_of"] = hdr_of          # remembered so "← Back" can restore the picks
        return redirect(url_for("mapping_step"))

    # GET: preview deep enough to reach each detected header; flag the sheets that
    # share the recommended sheet's structure so ST/LT splits pre-tick together.
    rec = j.get("ranked", [{}])[0] if j.get("ranked") else {}
    rec_reqset = frozenset(f for f in detect.REQUIRED if f in rec.get("automap", {}))
    # if the preparer is stepping BACK to this screen, restore their earlier picks.
    prev_selected = set(j.get("selected_sheets") or [])
    prev_hdr = j.get("hdr_of") or {}
    autocheck, sheet_hdr = {}, {}
    for idx, r in enumerate(j.get("ranked", [])):
        rset = frozenset(f for f in detect.REQUIRED if f in r.get("automap", {}))
        sheet_hdr[r["name"]] = prev_hdr.get(r["name"], r["header_row"])
        if prev_selected:
            autocheck[r["name"]] = r["name"] in prev_selected
        else:
            autocheck[r["name"]] = (
                (idx == 0 and r["header_row"] is not None) or
                (r["header_row"] is not None and len(rset) >= 4 and rset == rec_reqset
                 and len(rec_reqset) >= 4)
            )
    deepest = max([h for h in sheet_hdr.values() if h is not None] + [0])
    plen = min(40, max(15, deepest + 4))
    preview = {sn: j["sheets"][sn][:plen] for sn in sheet_names}
    return render_template("pick.html", sheets=sheet_names, preview=preview,
                           filename=j.get("filename", ""), ranked=ranked,
                           recommend=rec.get("name"), autocheck=autocheck,
                           sheet_hdr=sheet_hdr)


@app.route("/map", methods=["GET", "POST"])
def mapping_step():
    j = job()
    if "headers" not in j:
        return redirect(url_for("home"))
    if request.method == "POST":
        m = {fld: request.form.get(f"map_{fld}") or None for fld in mapping.CANONICAL_FIELDS}
        decl = {
            "source_label": request.form.get("source_label") or "Source",
            "cost_basis_meaning": request.form.get("cost_basis_meaning", "raw"),
            "default_asset_type": request.form.get("default_asset_type") or None,
            "default_stt_paid": request.form.get("default_stt_paid") == "yes",
            "default_is_50aa": request.form.get("default_is_50aa") == "yes",
            "fmv_basis": request.form.get("fmv_basis", "per_unit"),
            "grandfathering_basis": request.form.get("grandfathering_basis", "by_date"),
        }
        forward_fill = request.form.get("forward_fill") == "yes"
        j["mapping"] = m; j["decl"] = decl; j["forward_fill"] = forward_fill
        j["data"] = _finalise_rows(j, m, forward_fill)
        # a fresh mapping invalidates any classify choices remembered for back-nav
        j.pop("cls_choices", None)
        return redirect(url_for("classify_step"))
    automap = detect.auto_map(j["headers"])
    counts = j.get("sheet_counts", {})
    combined = [(sn, counts.get(sn, 0)) for sn in j.get("selected_sheets", [])]
    saved_map = j.get("mapping")          # present when stepping BACK to this screen
    ff_default = j.get("forward_fill") if "forward_fill" in j else _suggest_forward_fill(j, automap)
    return render_template("map.html", headers=j["headers"],
                           fields=mapping.CANONICAL_FIELDS, required=mapping.REQUIRED,
                           labels=mapping.FIELD_LABELS,
                           asset_types=sorted(compute.ASSET_TYPES),
                           automap=automap, ff_suggest=ff_default,
                           saved_map=saved_map, decl=j.get("decl") or {},
                           isin_merged=_suggest_isin_split(j, automap),
                           combined=combined)


@app.route("/classify", methods=["GET", "POST"])
def classify_step():
    j = job()
    if "mapping" not in j:
        return redirect(url_for("home"))
    m, decl, data = j["mapping"], j["decl"], j["data"]

    if request.method == "POST":
        # collect per-row resolutions (and remember the raw choices for "← Back")
        resolved, choices = [], []
        for i, row in enumerate(data):
            sel_asset = request.form.get(f"asset_{i}") or ""
            at = sel_asset or decl.get("default_asset_type")
            is50 = request.form.get(f"f50_{i}") == "yes"
            stt = request.form.get(f"stt_{i}") == "yes"
            choices.append({"asset": sel_asset, "stt": stt, "f50": is50})
            cls = {"asset_type": at, "is_50aa": is50, "stt_paid": stt,
                   "basis": request.form.get(f"basis_{i}", "manual/confirmed"),
                   "confidence": request.form.get(f"conf_{i}", "manual")}
            tx, err = mapping.build_tx(row, m, decl, cls)
            resolved.append((tx, err))
        j["cls_choices"] = choices
        j["resolved"] = resolved
        return redirect(url_for("result"))

    # GET: pre-fill via DB, build the review table
    gf_basis = decl.get("grandfathering_basis", "by_date")
    fmv_col = m.get("fmv_31jan2018")
    data_sheets = j.get("data_sheets", [])
    multi_sheet = len(j.get("selected_sheets", [])) > 1
    cls_choices = j.get("cls_choices") or []     # present when stepping BACK to this screen
    rows, gf_warn_n = [], 0
    for i, row in enumerate(data):
        # peek name/isin using the mapping, auto-splitting a merged "name + ISIN" cell
        name, isin = mapping.split_name_isin(row.get(m.get("security_name") or ""),
                                             row.get(m.get("isin") or "") if m.get("isin") else "")
        name, isin = name or "", isin or ""
        look = isin_db.lookup(isin or None, name or None)
        at = look["asset_type"] or decl.get("default_asset_type")
        # restore an earlier per-row choice when the preparer stepped back here
        choice = cls_choices[i] if i < len(cls_choices) else None
        if choice and choice.get("asset"):
            at = choice["asset"]
        debt = (at == "mf_debt")
        # pre-fill 50AA proposal for debt rows from acquisition date
        acq = mapping.parse_date(row.get(m.get("acquisition_date") or ""))
        prop50 = bool(debt and acq and acq.toordinal() >= mapping.parse_date("2023-04-01").toordinal())
        stt_default = decl.get("default_stt_paid", at in ("equity", "eof", "business_trust"))
        if choice is not None:
            prop50 = choice.get("f50", prop50)
            stt_default = choice.get("stt", stt_default)
        # grandfathering note when the acquisition date is absent (FMV-basis mode)
        gf_note, gf_warn = "", False
        if acq is None and gf_basis == "fmv":
            fmv_val = mapping.parse_amount(row.get(fmv_col, "")) if fmv_col else None
            if fmv_val:
                gf_note = "no acq date · 31-Jan-2018 FMV present → grandfathered long-term"
            else:
                gf_note, gf_warn = "no acq date & no FMV → treated as SHORT TERM", True
                gf_warn_n += 1
        rows.append({
            "i": i, "name": name[:60], "isin": isin,
            "asset_type": at or "", "confidence": look["confidence"],
            "basis": look["basis"] or decl.get("cost_basis_meaning", ""),
            "reason": look["reason"], "is_debt": debt,
            "prop50": prop50, "stt": stt_default,
            "sheet": data_sheets[i] if i < len(data_sheets) else None,
            "gf_note": gf_note, "gf_warn": gf_warn,
        })
    if gf_warn_n:
        flash(f"{gf_warn_n} row(s) have no acquisition date and no 31-Jan-2018 FMV — "
              f"they will be treated as SHORT TERM. Map the FMV column or key in the "
              f"acquisition date if that is wrong.")
    j["prefill"] = rows
    return render_template("classify.html", rows=rows,
                           asset_types=sorted(compute.ASSET_TYPES),
                           decl=decl, skipped=j.get("skipped", 0),
                           multi_sheet=multi_sheet)


@app.route("/result")
def result():
    j = job()
    if "resolved" not in j:
        return redirect(url_for("home"))
    txns = [tx for tx, err in j["resolved"] if tx]
    errors = [err for tx, err in j["resolved"] if err]
    if not txns:
        return render_template("result.html", errors=errors, ok=False,
                               results=[], counts={}, summary_file=None, winman_file=None)
    results = compute.compute_all(txns, ay=j["decl"].get("ay", "2025-26"))
    client = j["decl"].get("source_label", "Client")
    base = "".join(c for c in client if c.isalnum() or c in " _-").strip() or "client"
    sfile = os.path.join(OUT_DIR, f"{base}_CG_Summary.xlsx")
    wfile = os.path.join(OUT_DIR, f"{base}_Winman.xlsx")
    vfile = os.path.join(OUT_DIR, f"{base}_Validation.xlsx")
    # validation: compare the engine against the broker's own already-stated figures
    vres = validate.build_validation(results)
    vres.printed = validate.scan_broker_totals(j.get("sheets", {}))
    write_summary(results, sfile, client=client, ay=j["decl"].get("ay", "2025-26"),
                  validation=vres)
    _, counts = write_winman(results, wfile)
    write_validation(vres, vfile, client=client)
    j["results"] = results
    return render_template("result.html", ok=True, errors=errors, results=results,
                           counts=counts, summary_file=os.path.basename(sfile),
                           winman_file=os.path.basename(wfile),
                           validation_file=os.path.basename(vfile),
                           validation=vres, v_counts=vres.counts(),
                           total_gain=round(sum(r.gain for r in results), 2),
                           out_dir=OUT_DIR)


@app.route("/download/<which>")
def download(which):
    j = job()
    client = j.get("decl", {}).get("source_label", "client")
    base = "".join(c for c in client if c.isalnum() or c in " _-").strip() or "client"
    names = {"summary": f"{base}_CG_Summary.xlsx", "winman": f"{base}_Winman.xlsx",
             "validation": f"{base}_Validation.xlsx"}
    fn = names.get(which, names["summary"])
    path = os.path.join(OUT_DIR, fn)
    if not os.path.exists(path):
        flash("File not found — re-run."); return redirect(url_for("home"))
    return send_file(path, as_attachment=True, download_name=fn)


# ---- AIS reconciliation (the second main-menu path) ----------------------

@app.route("/ais", methods=["GET", "POST"])
def ais():
    if request.method == "GET":
        session.pop("jid", None)
        return render_template("ais_upload.html")
    j = job()
    cg_f = request.files.get("cg_file")
    ais_f = request.files.get("ais_file")
    if not cg_f or not cg_f.filename or not ais_f or not ais_f.filename:
        flash("Upload both files — the capital-gains/broker file and the AIS file.")
        return redirect(url_for("ais"))
    try:
        cg_rows, cg_map, cg_info = _extract_records(cg_f)
        ais_rows, ais_map, ais_info = _extract_records(ais_f)
    except Exception as e:
        flash(f"Could not read a file: {e}"); return redirect(url_for("ais"))

    # the broker/CG file is consumed automatically — it must yield a sale-value
    # column and at least one key (ISIN/name) on its own.
    problems = []
    if "sale_consideration" not in cg_map:
        problems.append("no sale-value column found in the capital-gains/broker file")
    if "isin" not in cg_map and "security_name" not in cg_map:
        problems.append("no ISIN or security-name column found in the capital-gains/broker file")
    if not ais_info.get("headers"):
        problems.append("could not locate the AIS data header automatically — check the file")
    if problems:
        for p in problems:
            flash(p)
        return redirect(url_for("ais"))

    # aggregate the CG side now; the AIS side waits for the confirm screen so the
    # preparer can see and override its column wiring before the match.
    cg_agg = reco.aggregate(cg_rows, *_reco_cols(cg_map))
    j["reco"] = {
        "cg_agg": cg_agg,
        "cg_label": os.path.splitext(cg_f.filename)[0][:24],
        "ais_label": os.path.splitext(ais_f.filename)[0][:24],
        "cg_info": cg_info,
        "ais_rows": ais_rows,
        "ais_headers": ais_info.get("headers", []),
        "ais_info": ais_info,
        "ais_automap": ais_map,
    }
    return redirect(url_for("ais_map"))


def _render_ais_map(r, col_field):
    """Render the AIS column-confirm screen: a selector sits on top of each
    column, with the real data shown beneath so the picked column is visible."""
    headers = r["ais_headers"]
    rows = r["ais_rows"]
    preview = [[str(row.get(h, "")) for h in headers] for row in rows[:12]]
    return render_template("ais_map.html", headers=headers, fields=RECO_FIELDS,
                           col_field=col_field, preview=preview,
                           ais_label=r["ais_label"], cg_label=r["cg_label"],
                           n=r["ais_info"].get("n", len(rows)),
                           sheet=r["ais_info"].get("sheet"))


@app.route("/ais/map", methods=["GET", "POST"])
def ais_map():
    j = job()
    r = j.get("reco")
    if not r:
        return redirect(url_for("ais"))
    headers = r["ais_headers"]

    if request.method == "POST":
        # column-first wiring: each column picks one canonical field (or none).
        # First column to claim a field wins (the screen enforces uniqueness).
        m, col_field = {}, {}
        for ci, h in enumerate(headers):
            f = request.form.get(f"col_{ci}") or ""
            if f in RECO_FIELDS:
                col_field[ci] = f
                if f not in m:
                    m[f] = h
        problems = []
        if "sale_consideration" not in m:
            problems.append("Pick the sale-value column in the AIS file.")
        if "isin" not in m and "security_name" not in m:
            problems.append("Pick an ISIN or a security-name column in the AIS file.")
        if problems:
            for p in problems:
                flash(p)
            return _render_ais_map(r, col_field)

        ais_agg = reco.aggregate(r["ais_rows"], m.get("security_name"),
                                 m.get("isin"), m.get("sale_consideration"),
                                 m.get("quantity"))
        result = reco.reconcile(r["cg_agg"], ais_agg)
        cg_label, ais_label = r["cg_label"], r["ais_label"]
        base = "".join(c for c in cg_label if c.isalnum() or c in " _-").strip() or "reco"
        rfile = os.path.join(OUT_DIR, f"{base}_AIS_Reco.xlsx")
        write_reco(result, rfile, cg_label=cg_label, ais_label=ais_label)
        j["reco_file"] = os.path.basename(rfile)
        return render_template("ais_result.html", result=result,
                               counts=result.counts(), totals=result.totals(),
                               cg_info=r["cg_info"], ais_info=r["ais_info"],
                               cg_label=cg_label, ais_label=ais_label,
                               reco_file=os.path.basename(rfile), out_dir=OUT_DIR)

    # GET: pre-select each column from the auto-map (col index -> canonical field)
    col_field = {}
    for f, info in r["ais_automap"].items():
        if f in RECO_FIELDS:
            ci = info.get("col")
            if ci is not None and 0 <= ci < len(headers):
                col_field[ci] = f
    return _render_ais_map(r, col_field)


@app.route("/ais/download")
def ais_download():
    j = job()
    fn = j.get("reco_file")
    path = os.path.join(OUT_DIR, fn) if fn else None
    if not path or not os.path.exists(path):
        flash("File not found — re-run the reconciliation."); return redirect(url_for("ais"))
    return send_file(path, as_attachment=True, download_name=fn)


@app.errorhandler(500)
def err500(e):
    return f"<pre>{traceback.format_exc()}</pre>", 500


if __name__ == "__main__":
    print("\ncg-engine running at  http://127.0.0.1:5000")
    print("ISIN DB:", isin_db.db_status())
    print("Outputs ->", OUT_DIR, "\n")
    app.run(debug=True, port=5000)
