# cg-engine

Map once → classify → compute deterministically → two writers. One canonical
table feeds both the firm CG Summary (Output A) and the Winman import file
(Output B). The AIS reconciliation is a separate downstream step that consumes
either output.

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
3. **Map & declare** — the source columns are **auto-matched** to the 8 canonical
   fields from their header names (green = confident, amber = low-confidence
   guess); review and fix, then declare the per-source facts the engine must not
   guess:
   - `cost_basis_meaning` — **raw** (engine grandfathers) vs **already-grandfathered**
     (engine suppresses FMV). This is the highest-risk silent error; it is a
     required choice.
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
5. **Compute & download** — Output A and Output B, plus an on-screen logic snapshot.

## The two outputs

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
- `tests/test_compute.py` — hand-checked. Run `python tests/test_compute.py`.
  Includes the proof that FMV is suppressed when cost is already grandfathered.
- `tests/test_detect.py` — unit tests for the matcher, plus a corpus test that
  runs detection over `reference/*` when present. Run `python tests/test_detect.py`.
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
- Aggregated / redemption-only statements that carry no lot-level acquisition
  date (CAMS & Karvy/KFIN transaction sheets, Kotak's per-scrip Gain & Loss
  summary) can't be computed lot-by-lot — detection flags the missing required
  field rather than guessing. Use the AMC/broker's lot-level statement instead.
- Foreign securities: pre-convert to INR (excluded from compute by design).
- `Is it LTCG?` is set explicitly per the Winman skill's dropdown rule, not left
  blank — change in `writer_winman._val` if your build prefers blank.
