# cg-engine

Two tools behind one menu (pick one at the start screen):

1. **Capital Gain Summary** — map once → classify → compute deterministically →
   three writers. One canonical table feeds the firm CG Summary (Output A), the
   Winman import file (Output B), and a broker-vs-engine validation sheet
   (Output C) that ties the engine back to the gains the statement already states.
2. **AIS Reconciliation** — upload a capital-gains/broker file **and** the AIS
   statement; columns are auto-detected and sale values are matched per security
   to surface matched / mismatched / only-in-CG / only-in-AIS. Standalone — it
   does not require running the summary first.

This is a tool. It surfaces the classification basis and the full gain logic for
every row so the preparer can verify them. Responsibility for the figures and the
filing rests with the preparer.

## Run (Mac)

Double-click `run.command`. First launch builds a venv and installs deps, then
opens http://127.0.0.1:5000.

## Run (Windows)

Double-click `run.bat`. Same behaviour: first launch builds `.venv`, installs
deps, then opens http://127.0.0.1:5000. Needs Python 3 on PATH (install from
python.org and tick "Add python.exe to PATH").

## Run (manual, any OS)

```
pip install -r requirements.txt
python app.py
```

Outputs are written to `~/Downloads/cg-engine-out/` (override with `CG_OUT`).
The Flask templates live in `templates/`; the ISIN classification DB
(`isin_master.db`) sits beside the code (override path with `ISIN_DB_PATH`).

## The flow

1. **Upload** one source file (xlsx / xls / csv). On read, all-blank columns (the
   `.xls` merged-cell spillover) are dropped, then every sheet is scanned to find
   the lot-level data and its header row.
2. **Pick** the sheet and the header row — the sheet that looks like lot-level
   data is **recommended and pre-selected**, with its detected header row
   highlighted (skips broker client-info junk rows). Override either if wrong.
3. **Map & declare** — the source columns are **auto-matched** to the canonical
   fields from their header names (green = confident, amber = low-confidence
   guess); review and fix, then declare the per-source facts the engine must not
   guess. One of those fields is optional and validation-only: `broker_gain` —
   the broker's OWN already-computed per-lot gain / P&L / capital-gain column.
   It is never used to compute; mapping it switches on the validation step below.
   - `cost_basis_meaning` — **raw** (engine grandfathers) vs **already-grandfathered**
     (engine suppresses FMV). This is the highest-risk silent error; it is a
     required choice.
   - `grandfathering_basis` — **by acquisition date** (default: FMV substituted
     only for lots acquired before 01-Feb-2018; acquisition date required) vs
     **FMV-based** (for statements that drop the purchase date on old holdings —
     a lot with no acquisition date but a 31-Jan-2018 FMV is treated as a
     pre-2018 grandfathered long-term lot; with no FMV either it is treated as
     **short-term** and the row is **flagged/flashed** so the preparer can map
     the buried acquisition-date column or key it in).
   - default asset type (if homogeneous), STT default, 50AA default, FMV basis.
   - **forward-fill name/ISIN** — for grouped layouts that print the scrip once
     and leave the lot rows beneath blank (IIFL); auto-suggested when detected.
   Section-divider, sub-total and repeated-header rows are dropped here so they
   never reach the engine as errors; the skipped count is shown next.
4. **Classify** — three-state gate per row:
   - `trusted` ISIN hit · `proposed` name match (confirm) · `manual` set it.
   - 50AA flag shows only for debt rows, pre-filled from acquisition date
     (on/after 01-Apr-2023 → proposed Yes). "Set 50AA = No for all debt" sweep
     reports the count it changed.
   - when several sheets were combined (ST/LT or per-account splits), the line
     items are **grouped under a "Sheet: …" divider** so each row's sheet of
     origin is visible while classifying.
   - under FMV-based grandfathering, rows missing an acquisition date carry a
     **GF** note: green (FMV present → grandfathered long-term) or red (no FMV →
     short-term); a summary warning is flashed for the red ones.
5. **Compute & download** — Output A, Output B and Output C, plus an on-screen
   logic snapshot and a broker-vs-engine validation panel.

## The three outputs

**Output A — `*_CG_Summary.xlsx`** (Arial 11)
- `CG Summary` — six buckets + totals, COI-feeding.
- `Workings` — every lot with the full audit snapshot: classification basis +
  confidence, holding days, threshold, LT/ST, pivot side, section, rate,
  cost-basis meaning, grandfathering applied + the nested-formula detail, net
  sale, cost used, gain, flags.
- `COI block` — paste-ready computation block, cross-referenced to CG Summary.

**Output B — `*_Winman.xlsx`** (Arial 11)
- Three data sheets in Winman column order: `Gains on STT paid shares`,
  `Units of MF except Equity fund`, `Virtual Digital Assets`.
- Only the **input** columns are filled; grey/computed columns
  (NETSALE / COSTOFACQUISITION / SHORTTERM / LTCG) are left blank for Winman's
  macro to finish. ACTUALCOST is raw; FMV is separate — one source of truth.
- `Not in Winman` — foreign / unlisted / non-STT equity, for the ITR schedules.
- Paste the data block into your live `securitiesshortGain.xlsm` (or import if
  your Winman build accepts xlsx). Lot-level with ISIN, sale date, sale
  consideration and quantity intact, so the AIS reco can consume it.

**Output C — `*_Validation.xlsx`** (Arial 11) — the extra check
- Most broker / AMC statements already carry their **own** short-term and
  long-term capital-gain figures. This output puts those side-by-side with what
  the engine computed, so the preparer can confirm the two agree before filing.
  The same two sheets are also folded into Output A (the CG Summary file is
  self-checking).
- `Validation` — the **short / long / total** roll-up (engine vs broker), a
  per-bucket breakdown, and the figures the broker **already printed** in the
  statement (scanned out of the raw workbook, for the eyeball check). The
  comparison is apples-to-apples: both sides are summed only over the lots that
  carry a broker figure, and coverage (broker lots / total) is shown — so a
  delta is a real per-lot difference, never a coverage artefact.
- `Lot Validation` — every lot, the engine's gain vs the broker's own stated
  gain, with the delta and a status; mismatches are sorted to the top as the
  chase list. Differences typically trace to grandfathering, charges/expenses,
  rounding, or a classification disagreement — all of which the preparer judges.
- Switched on when you map the broker's gain column (`broker_gain`) on the Map
  screen; it is auto-detected for the common formats (Zerodha `Profit`, Groww/
  Zerodha `Realised P&L`, ICICI `Profit/Loss`, MProfit `Capital Gain`, …). With
  no gain column mapped, the printed-figures scan still runs for reference.

## AIS Reconciliation (the second menu path)

Upload two files — the capital-gains/broker file and the AIS statement. Both are
auto-detected with the same `detect.py` engine (sheet, header row, columns), then
reduced to **sale value per security** and matched:

- **key**: valid ISIN first; a name-only side is rescued by normalised name.
- **tolerance**: matched if values agree within ₹1 or 1% (absorbs AIS rounding).
- **buckets**: matched · mismatched (chase the delta) · only-in-CG · only-in-AIS
  (a sale that may be missing from your file). On-screen plus a downloadable
  `*_AIS_Reco.xlsx` (Reco Summary + a sheet per bucket).

No tax logic — `reco.py` only sums and compares; the preparer judges every delta.

## Where the logic lives (and what to tweak)

- `compute.py` — the deterministic core, **zero I/O**. 23-Jul-2024 split,
  Section 55(2)(ac) nested grandfathering, holding-period month test, section
  routing, rate labels. Every rule is here, one place to tweak.
- `detect.py` — the auto-detection layer, **zero I/O**. The broker-header
  synonym table (`SYNONYMS`), sheet/header detection, greedy column auto-mapping,
  and the blank-column / forward-fill / divider-row handling. Add a new broker by
  dropping its header aliases into `SYNONYMS` — no other change. Every guess is
  shown with confidence on the map screen and is overridable; nothing routes
  silently.
- `reco.py` — the AIS reconciliation engine, **zero I/O**. Per-security
  aggregation, ISIN/name keying, tolerance match into the four buckets.
  `writer_reco.py` renders the workbook. Tune the match tolerance in
  `reco.reconcile` (`tol_abs`, `tol_pct`).
- `validate.py` — the broker-vs-engine validation, **zero I/O**. `build_validation`
  compares the engine's per-lot gain against the broker's own `broker_gain` column
  (per-lot, per-bucket, short/long/total roll-up, apples-to-apples over covered
  lots); `scan_broker_totals` sweeps the raw workbook for the short/long/total
  figures the broker already printed. `writer_validation.py` renders the workbook
  (and appends the same sheets into Output A). Tune tolerance via `TOL_ABS` /
  `TOL_PCT`; teach it a new broker's gain header by adding aliases to
  `detect.SYNONYMS["broker_gain"]`.
- `tests/test_compute.py` — hand-checked. Run `python tests/test_compute.py`.
  Includes the proof that FMV is suppressed when cost is already grandfathered.
- `tests/test_detect.py` — unit tests for the matcher, plus a corpus test that
  runs detection over `reference/*` when present. Run `python tests/test_detect.py`.
- `tests/test_validate.py` — per-lot match/mismatch/coverage, the roll-up, and the
  printed-figure scanner. Run `python tests/test_validate.py`.
- `isin_db.py` — set `ISIN_DB_PATH` (or drop `isin_master.db` beside the code).
  Schema is introspected, so differing column names are tolerated. No DB → every
  row degrades to manual; nothing is guessed.
- `writer_winman.py` — column ORDER mirrors the documented machine-key row.
  Eyeball the first paste against your Winman version and tweak `SHEET1/3/5` if a
  build differs. This is the one build-specific spot.
- Rate regime is parameterised by AY in `compute.py` (`_equity_rate`, thresholds).
  AY 2025-26 default; AY 2024-25 included.

## Known v1 boundaries

- One source file per run (multi-source consolidation is the next iteration).
- Statements that carry a 31-Jan-2018 FMV but drop the lot-level acquisition
  date for old holdings (CAMS & Karvy/KFIN transaction sheets) can be computed
  with **grandfathering basis = FMV-based**: FMV-bearing lots are treated as
  pre-2018 grandfathered long-term, and lots with neither acquisition date nor
  FMV fall to short-term with a flashed warning. By default these rows still
  flag the missing required field rather than being guessed. Purely aggregated /
  redemption-only summaries (Kotak's per-scrip Gain & Loss) carry no sale date
  either and still need the AMC/broker's lot-level statement.
- Foreign securities: pre-convert to INR (excluded from compute by design).
- `Is it LTCG?` is set explicitly per the Winman skill's dropdown rule, not left
  blank — change in `writer_winman._val` if your build prefers blank.
