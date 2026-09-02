# Electoral Roll PDF → Excel

Turns scanned **SIR draft electoral roll** PDFs published by the Election
Commission of India into a spreadsheet.

These PDFs have **no text layer** — every page is a scanned JPEG, so nothing can
be copied or searched. This app locates each elector box on the page, OCRs it,
parses the fields, and then **reconciles the result against the totals printed on
the PDF's own cover page** — so the output ships with its own quality check.

Upload one or more PDFs → browse the table → download `.xlsx` or `.csv`.

## How it works

1. **Box detection** (OpenCV) — morphological line detection finds each elector's
   ruled box, giving an exact entry count per page independent of OCR.
2. **Targeted OCR** (Tesseract) — the serial, the EPIC number, and the body of
   each box are cropped and OCR'd *separately*, with a digit whitelist for the
   serial and an alphanumeric one for the EPIC. Crops are stacked into one image
   per page so each page costs 4 OCR calls instead of ~90.
3. **Parsing** — fields are matched with separator-tolerant regexes
   (`:` is frequently misread as `=`, `?`, `2` or `+`), and wrapped lines are
   folded into the preceding field.
4. **Reconciliation** — extracted counts and the male/female/third-gender split
   are compared against the cover page's own figures. Mismatches are surfaced,
   never silently smoothed over.

## Accuracy

Validated on 81 parts of AC-45 Mehrauli (~50,000 electors):

- **81 / 81 parts** reconciled with their cover-page totals
- Sum of cover end-serials matched the extracted row count exactly
- **0 duplicate EPIC numbers**
- Blind spot-checks against source page images: 17 of 18 rows perfect on every
  field; the one miss was a house number (`111A/9` read as `1114/9`)

### Known limitations

- **Prefer `seq` over `serial`.** The printed serial sits in a cramped ruled box
  and mis-OCRs on roughly 1% of rows. `seq` is the position in reading order and
  needs no OCR, and box counts match every cover total exactly — so where the two
  disagree, `seq` is right.
- **Letters that look like digits** (A/4, I/1, O/0, S/5) are the main residual
  error, usually in house numbers.
- **Some rows genuinely have no age/gender.** Entries with very long names
  overflow the fixed-height box and the source PDF clips the
  `Age : … Gender : …` line off the page. Not recoverable; flagged as `no_age`.
- **EPIC formats vary.** Not all are `AAA9999999` — `NLNT040401`,
  `CKD093439` and `H2T1025733` are all genuine. Validation uses
  `[A-Z][A-Z0-9]{1,3}\d{6,7}`; don't tighten it to a fixed 3-letter prefix.

## Deploy to Streamlit Community Cloud (free)

1. Push this folder to a GitHub repo.
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**.
3. Pick the repo, branch, and set main file to `streamlit_app.py`.
4. Deploy. `packages.txt` installs Tesseract and `requirements.txt` the Python
   deps automatically.

Free-tier resources are modest (~1 GB RAM, ~2 vCPU). Page images are streamed and
OCR'd in batches so memory stays flat, but expect **~4 seconds per page** — a
30-page part takes about two minutes. Upload a few files at a time.

## Run locally

Local runs are much faster, since OCR parallelises across all your cores.

```bash
pip install -r requirements.txt
```

Install Tesseract:

- **Windows:** `winget install UB-Mannheim.TesseractOCR`
- **Debian/Ubuntu:** `sudo apt-get install tesseract-ocr tesseract-ocr-eng`
- **macOS:** `brew install tesseract`

Then:

```bash
streamlit run streamlit_app.py
```

If Tesseract isn't on `PATH`, set `TESSERACT_PATH` to the binary.

## Batch CLI

For bulk work, skip the UI — `engine_base.py` runs as a multiprocess batch job
and writes one CSV per PDF:

```bash
python engine_base.py <output_dir> - 12 <input_dir>
```

Arguments: output dir, filename filter (`-` for all), worker count, then one or
more input dirs (semicolon-separated). Set `OMP_THREAD_LIMIT=1` — Tesseract
multi-threads internally and will otherwise oversubscribe the CPU, costing about
4× throughput.

## Files

| File | Purpose |
|---|---|
| `streamlit_app.py` | Web UI |
| `engine.py` | Portable Tesseract discovery, batched page processing, validation |
| `engine_base.py` | Box detection, OCR, field parsing, cover parsing, batch CLI |
| `requirements.txt` | Python dependencies |
| `packages.txt` | apt packages for Streamlit Cloud (Tesseract) |

## Note on the data

The source PDFs are published openly by the ECI without authentication. Uploads
here are processed in memory and deleted immediately; nothing is retained. The
extracted data is still personal information about real people — please don't
republish it in bulk.
