# Electoral Roll PDF → Excel

Turns scanned Indian electoral roll PDFs (ECI SIR draft rolls — image-only, no
text layer) into a spreadsheet of electors.

Upload one or more PDFs, get a searchable table, download Excel or CSV.

## Why OCR is needed

These PDFs have **no text layer**. Every page is a single scanned JPEG
(typically 1983×2806), so `pdftotext` and copy-paste return nothing. The data
has to be recovered visually.

## How it works

1. **Locate entries** — morphological line detection (OpenCV) finds every
   elector box on the page. The layout is a regular 3-column grid of ~622×253 px
   boxes.
2. **OCR each region separately** — the serial number, the EPIC number and the
   body text are cropped and recognised independently, with character whitelists
   where the expected shape is known. Box rules are erased first so borders
   aren't read as digits.
3. **Parse fields** — `Name`, `Father's/Husband's/Mother's/Others` relation,
   `House Number`, `Age`, `Gender`, handling wrapped lines.
4. **Reconcile** — the extracted count and male/female/third-gender split are
   checked against the totals printed on the PDF's own cover page. Any part that
   doesn't match is reported rather than silently accepted.

## Output columns

`part, polling_station, section, serial, seq, epic, name, relation,
relative_name, house, age, gender, page, flag`

- `serial` — the serial number as printed, read by OCR
- `seq` — position in reading order within the part, independent of OCR
- `flag` — non-empty means the row deserves a look

**Where `serial` and `seq` disagree, trust `seq`.** The printed serial sits in a
cramped ruled box and mis-OCRs on roughly 1% of rows; `seq` is derived from
geometry. Box counts reconcile exactly with cover-page totals and serials run
contiguously, so position is the more reliable signal.

## Accuracy

Validated against 81 parts of Delhi AC-45 Mehrauli (SIR 2026 draft, ~50,000
electors):

- **81 / 81 parts reconciled** with their cover-page totals
- 0 duplicate EPIC numbers; 0 blank names; 0 malformed EPIC numbers
- Blind page spot-checks: 17 of 18 rows exact across every field

Known limits:

- Output is OCR. Letters that resemble digits (A/4, I/1, O/0, S/5) can be
  misread — most often in house numbers (`111A/9` read as `1114/9`).
- A few entries with very long names overflow the fixed-height box, so the
  **source PDF itself** clips the `Age`/`Gender` line. Those come through blank
  and flagged; they are not recoverable from the document.
- EPIC numbers are **not** all `AAA9999999`. Genuine values include `NLNT040401`
  (4 letters), `CKD093439` (6 digits) and `H2T1025733` (digit in position 2).
  Validation uses `[A-Z][A-Z0-9]{1,3}\d{6,7}` — don't tighten it.

## Run locally

Needs Python 3.10+ and Tesseract on `PATH`.

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Install Tesseract:

```bash
winget install UB-Mannheim.TesseractOCR
```

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-eng
```

Set `TESSERACT_PATH` if it isn't discoverable automatically.

## Deploy

Built for [Streamlit Community Cloud](https://share.streamlit.io): point it at
this repo with `streamlit_app.py` as the entrypoint. `packages.txt` installs
Tesseract on the container; `requirements.txt` covers Python deps.

Expect ~4 seconds per page on the free tier (2 vCPU). A 30-page part takes about
two minutes, so it suits a few files at a time rather than bulk runs.

## Batch use

For bulk extraction, skip the UI and use the engine directly — it parallelises
across PDFs:

```bash
python engine_base.py <output_dir> - 12 <source_dir>
```

## Files

| File | Purpose |
|---|---|
| `streamlit_app.py` | Single-page UI |
| `engine.py` | Per-PDF orchestration, parallelism, validation |
| `engine_base.py` | Box detection, OCR, field parsing, batch CLI |
| `packages.txt` | apt packages for the deploy container |

## Note on the source data

The source PDFs are published openly by the Election Commission of India.
Uploads are processed in memory and deleted immediately; nothing is retained.
Electoral rolls are personal data about real people — don't republish extracted
data in bulk.
