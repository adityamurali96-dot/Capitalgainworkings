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

import compute, mapping, isin_db
from writer_summary import write_summary
from writer_winman import write_winman

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


@app.route("/")
def home():
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
    j["sheets"] = {k: v.fillna("").astype(str).values.tolist() for k, v in sheets.items()}
    j["filename"] = f.filename
    return redirect(url_for("pick"))


@app.route("/pick", methods=["GET", "POST"])
def pick():
    j = job()
    if "sheets" not in j:
        return redirect(url_for("home"))
    sheet_names = list(j["sheets"].keys())
    if request.method == "POST":
        sheet = request.form["sheet"]
        header_row = int(request.form["header_row"])
        rows = j["sheets"][sheet]
        headers = [str(h).strip() for h in rows[header_row]]
        # de-dupe blank/dup headers
        seen = {}
        clean = []
        for i, h in enumerate(headers):
            h = h or f"col{i+1}"
            if h in seen:
                seen[h] += 1; h = f"{h}.{seen[h]}"
            else:
                seen[h] = 0
            clean.append(h)
        data = [dict(zip(clean, r)) for r in rows[header_row + 1:] if any(str(c).strip() for c in r)]
        j["headers"] = clean
        j["data"] = data
        return redirect(url_for("mapping_step"))
    # preview first sheet
    preview = {sn: j["sheets"][sn][:15] for sn in sheet_names}
    return render_template("pick.html", sheets=sheet_names, preview=preview,
                           filename=j.get("filename", ""))


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
        }
        j["mapping"] = m; j["decl"] = decl
        return redirect(url_for("classify_step"))
    return render_template("map.html", headers=j["headers"],
                           fields=mapping.CANONICAL_FIELDS, required=mapping.REQUIRED,
                           asset_types=sorted(compute.ASSET_TYPES))


@app.route("/classify", methods=["GET", "POST"])
def classify_step():
    j = job()
    if "mapping" not in j:
        return redirect(url_for("home"))
    m, decl, data = j["mapping"], j["decl"], j["data"]

    if request.method == "POST":
        # collect per-row resolutions
        resolved = []
        for i, row in enumerate(data):
            at = request.form.get(f"asset_{i}") or decl.get("default_asset_type")
            is50 = request.form.get(f"f50_{i}") == "yes"
            stt = request.form.get(f"stt_{i}") == "yes"
            cls = {"asset_type": at, "is_50aa": is50, "stt_paid": stt,
                   "basis": request.form.get(f"basis_{i}", "manual/confirmed"),
                   "confidence": request.form.get(f"conf_{i}", "manual")}
            tx, err = mapping.build_tx(row, m, decl, cls)
            resolved.append((tx, err))
        j["resolved"] = resolved
        return redirect(url_for("result"))

    # GET: pre-fill via DB, build the review table
    rows = []
    for i, row in enumerate(data):
        # peek name/isin using the mapping
        name = row.get(m.get("security_name") or "", "")
        isin_col = m.get("isin")
        isin = row.get(isin_col, "") if isin_col else ""
        look = isin_db.lookup(isin or None, name or None)
        at = look["asset_type"] or decl.get("default_asset_type")
        debt = (at == "mf_debt")
        # pre-fill 50AA proposal for debt rows from acquisition date
        acq = mapping.parse_date(row.get(m.get("acquisition_date") or ""))
        prop50 = bool(debt and acq and acq.toordinal() >= mapping.parse_date("2023-04-01").toordinal())
        stt_default = decl.get("default_stt_paid", at in ("equity", "eof", "business_trust"))
        rows.append({
            "i": i, "name": name[:60], "isin": isin,
            "asset_type": at or "", "confidence": look["confidence"],
            "basis": look["basis"] or decl.get("cost_basis_meaning", ""),
            "reason": look["reason"], "is_debt": debt,
            "prop50": prop50, "stt": stt_default,
        })
    j["prefill"] = rows
    return render_template("classify.html", rows=rows,
                           asset_types=sorted(compute.ASSET_TYPES),
                           decl=decl)


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
    write_summary(results, sfile, client=client, ay=j["decl"].get("ay", "2025-26"))
    _, counts = write_winman(results, wfile)
    j["results"] = results
    return render_template("result.html", ok=True, errors=errors, results=results,
                           counts=counts, summary_file=os.path.basename(sfile),
                           winman_file=os.path.basename(wfile),
                           total_gain=round(sum(r.gain for r in results), 2),
                           out_dir=OUT_DIR)


@app.route("/download/<which>")
def download(which):
    j = job()
    client = j.get("decl", {}).get("source_label", "client")
    base = "".join(c for c in client if c.isalnum() or c in " _-").strip() or "client"
    fn = f"{base}_CG_Summary.xlsx" if which == "summary" else f"{base}_Winman.xlsx"
    path = os.path.join(OUT_DIR, fn)
    if not os.path.exists(path):
        flash("File not found — re-run."); return redirect(url_for("home"))
    return send_file(path, as_attachment=True, download_name=fn)


@app.errorhandler(500)
def err500(e):
    return f"<pre>{traceback.format_exc()}</pre>", 500


if __name__ == "__main__":
    print("\ncg-engine running at  http://127.0.0.1:5000")
    print("ISIN DB:", isin_db.db_status())
    print("Outputs ->", OUT_DIR, "\n")
    app.run(debug=True, port=5000)
