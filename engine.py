"""Extraction engine for the web app.

Reuses the validated pipeline in engine_base.py unchanged; adds portable
Tesseract discovery, page-level parallelism, and per-part validation.
"""
import os, re, shutil, tempfile, collections
from concurrent.futures import ThreadPoolExecutor

import engine_base as eb

# one tesseract thread per worker -- parallelism comes from the thread pool
os.environ.setdefault("OMP_THREAD_LIMIT", "1")


def find_tesseract():
    """Locate the tesseract binary across Windows/Linux/container layouts."""
    cand = [os.environ.get("TESSERACT_PATH"), shutil.which("tesseract")]
    cand += [
        os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract", "/usr/local/bin/tesseract",
    ]
    for c in cand:
        if c and os.path.exists(c):
            return c
    return None


TESS = find_tesseract()
if TESS:
    eb.TESS = TESS

OTHERS = re.compile(r"^(.*?)\s+(Others?)\s*[:=.;]?\s*(.*)$", re.I)
EPIC_OK = re.compile(r"[A-Z][A-Z0-9]{1,3}\d{6,7}")

COLS = ["part", "polling_station", "section", "serial", "seq", "epic", "name",
        "relation", "relative_name", "house", "age", "gender", "page", "flag"]


def _repair(r):
    """Split the roll's bare 'Others: <name>' label back out of the name field."""
    if not r["relation"] and r["name"]:
        m = OTHERS.match(r["name"])
        if m:
            r["name"] = m.group(1).strip()
            r["relation"] = "Others"
            r["relative_name"] = m.group(3).strip()
    return r


def page_count(path):
    """Page count without decoding any images."""
    try:
        import pypdf
        return len(pypdf.PdfReader(path).pages)
    except Exception:
        return 0


def _run_batch(batch, td, workers):
    """OCR a batch of (page_index, image) in parallel, then release the images."""
    def work(item):
        pg, gray = item
        rows, _ = eb.do_page(gray, td, pg)
        return pg, rows
    with ThreadPoolExecutor(max_workers=workers) as ex:
        out = list(ex.map(work, batch))
    batch.clear()
    return out


def process_pdf(path, workers=None, progress=None):
    """OCR one roll PDF. Returns (rows, cover, summary)."""
    if not TESS:
        raise RuntimeError(
            "Tesseract OCR is not installed or not on PATH. Install it "
            "(Windows: winget install UB-Mannheim.TesseractOCR; "
            "Debian/Ubuntu: apt-get install tesseract-ocr) or set TESSERACT_PATH."
        )
    workers = workers or max(1, min(12, (os.cpu_count() or 2)))
    total = page_count(path) or 1

    cover = {k: "" for k in eb.COVER_COLS}
    results = []
    done = 0

    # Pages are streamed and processed in batches of `workers` so peak memory stays
    # at ~workers x 5.6 MB, not the whole document (a 36-page PDF is ~200 MB decoded).
    with tempfile.TemporaryDirectory() as td:
        batch = []
        for pg, gray in eb.page_images(path):
            if gray is None:
                continue
            if pg == 0:
                cover = eb.parse_cover(gray, td)
                done += 1
                if progress:
                    progress(done / total, "cover page")
                del gray
                continue
            batch.append((pg, gray))
            if len(batch) < workers:
                continue
            results.extend(_run_batch(batch, td, workers))
            done += len(batch)
            if progress:
                progress(min(done / total, 1.0), "page %d/%d" % (done, total))
            batch = []
        if batch:
            results.extend(_run_batch(batch, td, workers))
            done += len(batch)
            if progress:
                progress(min(done / total, 1.0), "page %d/%d" % (done, total))

    if not results and not cover.get("part"):
        raise RuntimeError("No elector entries found - is this a scanned roll PDF?")

    results.sort(key=lambda x: x[0])
    rows = [r for _, rs in results for r in rs]

    part = cover.get("part") or (rows[0]["part"] if rows else "")
    ps = cover.get("polling_station", "")
    for i, r in enumerate(rows, 1):
        _repair(r)
        r["seq"] = i
        r["part"] = part
        r["polling_station"] = ps
        fl = []
        if r["serial"] != str(i):
            fl.append("serial!=seq")
        if not EPIC_OK.fullmatch(r["epic"] or ""):
            fl.append("epic_bad")
        if not r["name"]:
            fl.append("no_name")
        if not r["age"]:
            fl.append("no_age")
        r["flag"] = ",".join(fl)

    return rows, cover, _validate(rows, cover)


def _validate(rows, cov):
    g = collections.Counter(r["gender"] for r in rows)
    m, f = g.get("Male", 0), g.get("Female", 0)
    third = sum(v for k, v in g.items() if k and k not in ("Male", "Female"))
    blank = g.get("", 0)

    def num(k):
        v = (cov.get(k) or "").strip()
        return int(v) if v.isdigit() else None

    ctot, cend, cm, cf, ct = (num("total"), num("end_serial"),
                             num("male"), num("female"), num("third_gender"))
    checks, hard = [], 0
    n = len(rows)
    if ctot is not None:
        if ctot == n:
            checks.append("total OK")
        elif cend == n or (cm or 0) + (cf or 0) + (ct or 0) == n:
            checks.append("cover total cell misread (%s); end-serial+split confirm %d"
                          % (ctot, n))
        else:
            checks.append("TOTAL %s vs %d" % (ctot, n)); hard += 1
    if cend is not None:
        if cend == n:
            checks.append("end-serial OK")
        else:
            checks.append("END-SERIAL %s vs %d" % (cend, n)); hard += 1
    for cv, ex, lab in ((cm, m, "male"), (cf, f, "female"), (ct, third, "third")):
        if cv is None:
            continue
        if cv == ex:
            checks.append(lab + " OK")
        elif ex < cv <= ex + blank:
            checks.append("%s %s vs %d (within %d blank)" % (lab, cv, ex, blank))
        else:
            checks.append("%s %s vs %d" % (lab.upper(), cv, ex)); hard += 1

    dup = n - len(set(r["epic"] for r in rows))
    if dup:
        checks.append("%d DUPLICATE EPIC" % dup); hard += 1

    return {
        "rows": n, "male": m, "female": f, "third": third,
        "blank_gender": blank,
        "serial_agree": sum(1 for r in rows if r["serial"] == str(r["seq"])),
        "flagged": sum(1 for r in rows if r["flag"]),
        "dup_epic": dup,
        "status": "CHECK" if hard else ("OK (clipped field in PDF)" if blank else "OK"),
        "checks": "; ".join(checks) or "no cover data",
    }
