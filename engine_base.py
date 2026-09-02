"""OCR Delhi SIR-2026 draft electoral roll PDFs (image-only) into structured rows."""
import os, re, sys, csv, glob, subprocess, tempfile, traceback
import cv2, numpy as np, pypdf

TESS = "tesseract"   # overridden by engine.find_tesseract() at import time
GAP = 60           # white separator between stacked crops
HDR_H = 48         # height of the serial/EPIC strip inside each box
PHOTO_CUT = 0.77   # keep left 77% of box width (drops the "Photo Available" placeholder)

OUT = ""


def page_images(path):
    r = pypdf.PdfReader(path)
    for i, p in enumerate(r.pages):
        res = p.get("/Resources") or {}
        xo = res.get("/XObject")
        img = None
        if xo:
            xo = xo.get_object()
            for k in xo:
                o = xo[k].get_object()
                if o.get("/Subtype") == "/Image":
                    buf = np.frombuffer(o.get_data(), dtype=np.uint8)
                    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
                    break
        yield i, img


def find_boxes(gray):
    bw = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)[1]
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (60, 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 60))
    lines = cv2.dilate(cv2.erode(bw, hk), hk) | cv2.dilate(cv2.erode(bw, vk), vk)
    cnts, _ = cv2.findContours(lines, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    raw = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if 560 < w < 700 and 210 < h < 300:
            raw.append((x, y, w, h))
    kept = []                                  # dedupe outer/inner contour pairs
    for x, y, w, h in sorted(raw, key=lambda r: -r[2] * r[3]):
        if not any(abs(x - a) < 12 and abs(y - b) < 12 for a, b, _, _ in kept):
            kept.append((x, y, w, h))
    kept.sort(key=lambda r: (round(r[1] / 60), r[0]))   # reading order: row, then column
    return kept


def derule(crop, hlen=40, vlen=22):
    """Whiten long horizontal/vertical rules so box borders don't confuse OCR."""
    bw = cv2.threshold(crop, 200, 255, cv2.THRESH_BINARY_INV)[1]
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (hlen, 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vlen))
    rules = cv2.dilate(cv2.erode(bw, hk), hk) | cv2.dilate(cv2.erode(bw, vk), vk)
    rules = cv2.dilate(rules, np.ones((3, 3), np.uint8))
    out = crop.copy()
    out[rules > 0] = 255
    return out


def stack(crops):
    """Pad crops to a common size and stack them vertically with white gaps."""
    h = max(c.shape[0] for c in crops)
    w = max(c.shape[1] for c in crops)
    cell = h + GAP
    out = np.full((cell * len(crops), w), 255, np.uint8)
    for i, c in enumerate(crops):
        out[i * cell:i * cell + c.shape[0], :c.shape[1]] = c
    return out, cell


def tess_lines(img, tmpdir, tag, psm, whitelist=None):
    """Run tesseract on img; return [(y_centre, text)] per recognised text line."""
    p = os.path.join(tmpdir, tag + ".png")
    cv2.imwrite(p, img)
    cmd = [TESS, p, os.path.join(tmpdir, tag), "--psm", str(psm), "-l", "eng", "tsv"]
    if whitelist:
        cmd += ["-c", "tessedit_char_whitelist=" + whitelist]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    tsv = os.path.join(tmpdir, tag + ".tsv")
    if not os.path.exists(tsv):
        return []
    groups = {}
    with open(tsv, encoding="utf-8", errors="replace") as f:
        rd = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        next(rd, None)
        for row in rd:
            if len(row) < 12 or row[11].strip() == "":
                continue
            # TSV cols: level page block par line word left top width height conf text
            key = (row[1], row[2], row[3], row[4])
            left, top, ht = int(row[6]), int(row[7]), int(row[9])
            groups.setdefault(key, []).append((left, row[11], top + ht / 2.0))
    lines = []
    for words in groups.values():
        words.sort()
        lines.append((sum(w[2] for w in words) / len(words),
                      " ".join(w[1] for w in words).strip()))
    return sorted(lines)


APOS = "['\u2019\u02bc`]"
JUNK = r"^[^A-Za-z]{0,4}"                 # OCR speckle before the field label
SEP = r"\s*[:=;?.,\-\u2014\u2022]?\s*"    # ':' is sometimes read as = ? . etc.
REL = re.compile(JUNK + r"(Father|Husband|Mother|Other)s?\s*" + APOS +
                 r"?\s*s?\s*Name" + SEP + r"(.*)$", re.I)
NAME = re.compile(JUNK + r"Name" + SEP + r"(.*)$", re.I)
# the roll also uses a bare "Others: <name>" label, with no "Name" word
REL_BARE = re.compile(JUNK + r"(Others?)\s*[:=.;]\s*(.*)$", re.I)
HOUSE = re.compile(JUNK + r"Hous?e?\s*(?:Number|No\.?)" + SEP + r"(.*)$", re.I)
AGE = re.compile(JUNK + r"Age" + SEP + r"(\d{1,3})\s*Gender" + SEP + r"(\w+)", re.I)


def parse_body(lines):
    d = {"name": "", "relation": "", "relative_name": "", "house": "",
         "age": "", "gender": "", "_extra": []}
    last = None
    for _, t in lines:
        t = t.strip()
        if not t or t.lower() in ("photo", "available", "photo available"):
            continue
        m = REL.match(t) or REL_BARE.match(t)
        if m:
            d["relation"] = m.group(1).title()
            d["relative_name"] = m.group(2).strip()
            last = "relative_name"
            continue
        m = NAME.match(t)
        if m:
            d["name"] = m.group(1).strip()
            last = "name"
            continue
        m = HOUSE.match(t)
        if m:
            d["house"] = m.group(1).strip()
            last = "house"
            continue
        m = AGE.match(t)
        if m:
            d["age"], d["gender"] = m.group(1), m.group(2).title()
            last = "age"
            continue
        if last in ("house", "name", "relative_name"):      # wrapped continuation line
            d[last] = (d[last] + " " + t).strip()
        else:
            d["_extra"].append(t)
    return d


# EPICs here are not all AAA9999999: NLNT040401, CKD093439 and H2T1025733 are genuine
EPIC_OK = re.compile(r"[A-Z][A-Z0-9]{1,3}\d{6,7}")
SER_OK = re.compile(r"\d{1,4}")
LET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DIG_FOR_LET = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"}
LET_FOR_DIG = {"O": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "Q": "0"}


def norm_epic(s):
    """Fix digit/letter confusions using the known AAA9999999 shape."""
    s = re.sub(r"[^A-Z0-9]", "", s.upper())
    if len(s) != 10:
        return ""
    head = "".join(DIG_FOR_LET.get(c, c) if c.isdigit() else c for c in s[:3])
    tail = "".join(LET_FOR_DIG.get(c, c) if c.isalpha() else c for c in s[3:])
    out = head + tail
    return out if EPIC_OK.fullmatch(out) else ""


def retry_epic(crop, tmpdir, tag):
    """Re-OCR a single EPIC crop with alternative settings."""
    for psm, wl in ((7, LET + "0123456789"), (8, LET + "0123456789"),
                    (7, None), (13, LET + "0123456789")):
        txt = "".join(t for _, t in tess_lines(crop, tmpdir, tag + str(psm), psm, wl))
        cand = norm_epic(txt)
        if cand:
            return cand
    return ""


def retry_serial(crop, tmpdir, tag):
    for psm in (7, 8, 13):
        txt = "".join(t for _, t in tess_lines(crop, tmpdir, tag + "s" + str(psm),
                                               psm, "0123456789"))
        txt = re.sub(r"\D", "", txt)
        if SER_OK.fullmatch(txt):
            return txt
    return ""


def count_cells(gray):
    """Locate the 6 data cells of the 'NUMBER OF ELECTORS' table on a cover page."""
    bw = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)[1]
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 25))
    L = cv2.dilate(cv2.erode(bw, hk), hk) | cv2.dilate(cv2.erode(bw, vk), vk)
    cnts, _ = cv2.findContours(L, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rs = [cv2.boundingRect(c) for c in cnts]
    rs = [r for r in rs if r[1] > 1900 and 60 < r[2] < 500 and 30 < r[3] < 130]
    rows = {}
    for r in rs:
        rows.setdefault(round(r[1] / 25), []).append(r)
    cand = [v for v in rows.values() if len(v) >= 6]
    if not cand:
        return []
    best = max(cand, key=lambda v: v[0][1])          # lowest such row = data row
    best.sort(key=lambda r: r[0])
    return best[:6]


COVER_COLS = ["file", "part", "ac", "pc", "year", "qualifying_date", "revision_type",
              "publication_date", "sections", "main_town", "ward", "post_office",
              "police_station", "tahsil", "district", "pin_code", "polling_station",
              "ps_address", "ps_type", "aux_stations", "start_serial", "end_serial",
              "male", "female", "third_gender", "total"]


def _grab(gray, tmpdir, tag, y0, y1, x0, x1, psm=6, wl=None, rule=False):
    crop = gray[y0:y1, x0:x1]
    if rule:
        crop = derule(crop)
    return [t for _, t in tess_lines(crop, tmpdir, tag, psm, wl)]


def parse_cover(gray, tmpdir):
    c = {k: "" for k in COVER_COLS}
    full = " \n".join(_grab(gray, tmpdir, "cvfull", 0, 2806, 0, 1983, 6))

    def pick(pat, s=None, grp=1):
        m = re.search(pat, s if s is not None else full, re.I)
        return m.group(grp).strip() if m else ""

    c["ac"] = pick(r"Assembly Constituency" + SEP + r"([^|\n]*?)\s*(?:Part No|$)")
    c["part"] = pick(r"Part\s*No\.?" + SEP + r"(\d+)")
    c["pc"] = pick(r"Assembly Constituency is located" + SEP + r"([^\n]*(?:\n[^\n]*)?)")
    c["pc"] = " ".join(c["pc"].split())
    c["year"] = pick(r"Year of Revision" + SEP + r"(\d{4})")
    c["qualifying_date"] = pick(r"Qualifying Date" + SEP + r"([\d\-/.]{8,10})")
    c["publication_date"] = pick(r"Date of Publication" + SEP + r"([\d\-/.]{8,10})")
    c["revision_type"] = pick(r"Type of Revision" + SEP + r"([^\n]*)")
    c["ps_type"] = " ".join(_grab(gray, tmpdir, "cvpt", 1640, 1745, 1560, 1940, 7)) or \
        pick(r"Type of Polling Station\s*([A-Za-z]+)")

    secs = [s.strip() for s in _grab(gray, tmpdir, "cvsec", 1000, 1580, 40, 838, 6)
            if re.match(r"^\s*\d+\s*[-–]", s.strip())]
    c["sections"] = " | ".join(secs)

    # Labels and values sit in separate columns; OCR them apart so the ':' between
    # them (often misread as 2 / + / i) never contaminates the value.
    labs = tess_lines(gray[1020:1420, 840:1355], tmpdir, "cvrl", 6)
    vals = tess_lines(gray[1020:1420, 1372:1940], tmpdir, "cvrv", 6)
    for label, key in [("Main Town or Village", "main_town"), ("Ward", "ward"),
                       ("Post Office", "post_office"), ("Police Station", "police_station"),
                       ("Tahsil", "tahsil"), ("District", "district"), ("Pin ?code", "pin_code")]:
        ly = next((y for y, t in labs if re.match(r"^\s*" + label, t, re.I)), None)
        if ly is None:
            continue
        near = [(abs(y - ly), t) for y, t in vals if abs(y - ly) < 28]
        if near:
            c[key] = min(near)[1].strip(" :=+.;-")
    c["pin_code"] = re.sub(r"\D", "", c["pin_code"])

    ps = _grab(gray, tmpdir, "cvps", 1650, 2010, 40, 838, 6)
    name, addr, mode = [], [], None
    for line in ps:
        if re.search(r"Name of Polling Station", line, re.I):
            mode = "n"; continue
        if re.search(r"Address of Polling Station", line, re.I):
            mode = "a"; continue
        if not line.strip():
            continue
        (name if mode == "n" else addr).append(line.strip())
    c["polling_station"] = " ".join(name)
    c["ps_address"] = " ".join(addr)

    aux = _grab(gray, tmpdir, "cvax", 1752, 1955, 1572, 1938, 7, "0123456789")
    c["aux_stations"] = re.sub(r"\D", "", "".join(aux))

    # Inset well inside each cell's rules -- derule() would eat the stroke of a '1'.
    cells = count_cells(gray)
    keys = ["start_serial", "end_serial", "male", "female", "third_gender", "total"]
    for k, (x, y, w, h) in zip(keys, cells):
        v = _grab(gray, tmpdir, "cvc" + k, y + 10, y + h - 10, x + 10, x + w - 10,
                  7, "0123456789")
        c[k] = re.sub(r"\D", "", "".join(v))
    return c


def page_header(gray, tmpdir, pg):
    ac = part = section = ""
    for _, t in tess_lines(gray[0:95, 0:1983], tmpdir, "ph%d" % pg, 6):
        if re.search(r"Section\s*No", t, re.I):
            section = re.sub(r"^.*?Section\s*No\s*and\s*Name\s*[:.]?\s*", "", t, flags=re.I).strip()
        if re.search(r"Assembly\s*Constituency", t, re.I):
            ac = re.sub(r"^.*?Name\s*[:.]?\s*", "", t.split("Part")[0], flags=re.I).strip()
        m = re.search(r"Part\s*No\.?\s*[:.]?\s*(\d+)", t, re.I)
        if m:
            part = m.group(1)
    return ac, part, section


def do_page(gray, tmpdir, pg):
    boxes = find_boxes(gray)
    ac, part, section = page_header(gray, tmpdir, pg)
    if not boxes:
        return [], {"page": pg + 1, "boxes": 0}

    sers, epics, bodies = [], [], []
    for (x, y, w, h) in boxes:
        cut = int(w * PHOTO_CUT)
        # serial sits in a bordered rectangle at rel (10,10)-(204,47); take a
        # generous window and erase the rectangle's rules so glyphs aren't clipped
        sers.append(derule(gray[y + 4:y + 54, x + 6:x + 210]))
        epics.append(gray[y + 2:y + HDR_H, x + int(w * .68):x + w - 4])
        bodies.append(gray[y + HDR_H:y + h - 3, x + 3:x + cut])

    s_img, s_cell = stack(sers)
    e_img, e_cell = stack(epics)
    b_img, b_cell = stack(bodies)
    s_lines = tess_lines(s_img, tmpdir, "s%d" % pg, 6, "0123456789")
    e_lines = tess_lines(e_img, tmpdir, "e%d" % pg, 6, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/")
    b_lines = tess_lines(b_img, tmpdir, "b%d" % pg, 6)

    def bucket(lines, cell, n):
        out = [[] for _ in range(n)]
        for yc, t in lines:
            i = int(yc // cell)
            if 0 <= i < n:
                out[i].append((yc, t))
        return out

    n = len(boxes)
    sb = bucket(s_lines, s_cell, n)
    eb = bucket(e_lines, e_cell, n)
    bb = bucket(b_lines, b_cell, n)
    rows = []
    for i in range(n):
        d = parse_body(bb[i])
        d["serial"] = "".join(t for _, t in sb[i]).replace(" ", "")
        d["epic"] = "".join(t for _, t in eb[i]).replace(" ", "")
        if not EPIC_OK.fullmatch(d["epic"]):
            d["epic"] = retry_epic(epics[i], tmpdir, "re%d_%d" % (pg, i)) or d["epic"]
        if not SER_OK.fullmatch(d["serial"]):
            d["serial"] = retry_serial(sers[i], tmpdir, "rs%d_%d" % (pg, i)) or d["serial"]
        d["extra"] = "; ".join(d.pop("_extra"))
        d.update(page=pg + 1, section=section, part=part, ac=ac)
        rows.append(d)
    return rows, {"page": pg + 1, "boxes": n}


COLS = ["file", "part", "ac", "page", "section", "serial", "seq", "epic", "name",
        "relation", "relative_name", "house", "age", "gender", "extra", "flag"]


def do_pdf(args):
    # One tesseract thread per worker: we get parallelism from the process pool,
    # and letting each tesseract also fan out oversubscribes the CPU badly.
    os.environ["OMP_THREAD_LIMIT"] = "1"
    path, out = args
    base = os.path.basename(path)
    out_csv = os.path.join(out, base.replace(".pdf", ".csv"))
    if os.path.exists(out_csv) and os.path.getsize(out_csv) > 100:
        return base, "skip", [], -1
    log = []
    try:
        allrows = []
        cover = {k: "" for k in COVER_COLS}
        with tempfile.TemporaryDirectory() as td:
            for pg, gray in page_images(path):
                if gray is None:
                    continue
                if pg == 0:
                    cover = parse_cover(gray, td)
                    cover["file"] = base
                    log.append({"page": 1, "boxes": 0})
                    continue
                rows, info = do_page(gray, td, pg)
                allrows.extend(rows)
                log.append(info)
        for i, r in enumerate(allrows, 1):     # reading-order sequence cross-check
            r["file"] = base
            r["seq"] = i
            f = []
            if r["serial"] != str(i):
                f.append("serial!=seq")
            if not EPIC_OK.fullmatch(r["epic"] or ""):
                f.append("epic_bad")
            if not r["name"]:
                f.append("no_name")
            if not r["age"]:
                f.append("no_age")
            r["flag"] = ",".join(f)
        if not cover["part"] and allrows:
            cover["part"] = allrows[0]["part"]
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
            wr.writeheader()
            wr.writerows(allrows)
        with open(out_csv.replace(".csv", ".cover.csv"), "w", newline="",
                  encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=COVER_COLS, extrasaction="ignore")
            wr.writeheader()
            wr.writerow(cover)
    except Exception:
        return base, "ERROR " + traceback.format_exc(), log, 0
    return base, "ok", log, len(allrows)


def main():
    import multiprocessing as mp
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    # 4th arg: one or more source dirs (semicolon-separated); default = cwd
    srcs = sys.argv[4].split(";") if len(sys.argv) > 4 else ["."]
    files = sorted(f for s in srcs for f in glob.glob(os.path.join(s, "*.pdf")))
    if len(sys.argv) > 2 and sys.argv[2] != "-":
        pats = sys.argv[2].split(",")
        files = [f for f in files if any(t in f for t in pats)]
    nw = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    tasks = [(f, out) for f in files]
    print("files:", len(files), "workers:", nw, flush=True)
    done = 0
    with mp.Pool(nw) as pool:
        for base, st, log, n in pool.imap_unordered(do_pdf, tasks):
            done += 1
            print("[%d/%d] %s %s pages=%d electors=%d"
                  % (done, len(files), base, st, len(log), n), flush=True)
            if st != "ok":
                print(st, flush=True)


if __name__ == "__main__":
    main()
