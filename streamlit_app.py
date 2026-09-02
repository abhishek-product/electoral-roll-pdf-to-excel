"""Electoral Roll PDF -> Excel.

Upload scanned SIR draft-roll PDFs; get a browsable table and Excel/CSV
downloads. Every part is cross-checked against the totals printed on its own
cover page, so the output carries its own QA.
"""
import io
import os
import tempfile
import traceback
import datetime

import pandas as pd
import streamlit as st

import engine

MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "60"))
SLOW_PAGE_WARN = 120          # pages above which we warn about runtime

PREVIEW_COLS = ["source_file", "part", "polling_station", "section", "serial", "seq",
                "epic", "name", "relation", "relative_name", "house", "age",
                "gender", "page", "flag"]

COVER_SHOW = ["ac", "pc", "polling_station", "ps_address", "sections", "ward",
              "district", "pin_code", "start_serial", "end_serial", "male",
              "female", "third_gender", "total", "publication_date"]

st.set_page_config(page_title="Electoral Roll PDF → Excel",
                   page_icon="🗳️", layout="wide")


def build_xlsx(electors: pd.DataFrame, parts: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        parts.to_excel(xl, sheet_name="Parts & Validation", index=False)
        electors.to_excel(xl, sheet_name="Electors", index=False)
        for sheet, frame in (("Parts & Validation", parts), ("Electors", electors)):
            ws = xl.sheets[sheet]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for i, col in enumerate(frame.columns, 1):
                longest = frame[col].astype(str).str.len().max() if len(frame) else 10
                width = max(10, min(46, int(longest or 10) + 2))
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    return buf.getvalue()


def extract_all(uploads):
    rows_all, part_recs, problems = [], [], []
    bar = st.progress(0.0, text="Starting…")
    n = len(uploads)

    for idx, up in enumerate(uploads):
        name = up.name
        data = up.getvalue()
        if len(data) / 1e6 > MAX_MB:
            problems.append(f"**{name}** skipped — {len(data)/1e6:.0f} MB exceeds the "
                            f"{MAX_MB} MB limit.")
            continue

        tmp = os.path.join(tempfile.gettempdir(), f"roll_{os.getpid()}_{idx}.pdf")
        with open(tmp, "wb") as fh:
            fh.write(data)
        try:
            def prog(frac, msg, idx=idx, name=name):
                bar.progress(min((idx + frac) / n, 1.0), text=f"{name} — {msg}")

            rows, cover, summ = engine.process_pdf(tmp, progress=prog)
            for r in rows:
                r["source_file"] = name
            rows_all.extend(rows)

            rec = {"file": name, "status": summ["status"],
                   "part": cover.get("part", ""), "rows": summ["rows"]}
            rec.update({k: cover.get(k, "") for k in COVER_SHOW})
            rec.update({
                "male_found": summ["male"], "female_found": summ["female"],
                "third_found": summ["third"],
                "serial_agreement": f"{summ['serial_agree']}/{summ['rows']}",
                "flagged_rows": summ["flagged"],
                "validation": summ["checks"],
            })
            part_recs.append(rec)
        except Exception as exc:
            problems.append(f"**{name}** failed — {exc}")
            if os.environ.get("ROLLAPP_DEBUG"):
                problems.append(f"```\n{traceback.format_exc()}\n```")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    bar.progress(1.0, text="Done")
    return rows_all, part_recs, problems


st.title("🗳️ Electoral Roll PDF → Excel")
st.markdown(
    "Turns scanned **SIR draft electoral roll** PDFs — the ECI image-only kind with "
    "no selectable text — into a spreadsheet. Each elector box is located, OCR'd, "
    "and then cross-checked against the totals printed on that PDF's own cover page."
)

with st.sidebar:
    st.header("How it works")
    st.markdown(
        "1. Detect every elector box on the page (OpenCV)\n"
        "2. OCR the serial, EPIC and body of each box separately (Tesseract)\n"
        "3. Parse the fields and reconcile against the cover-page totals\n"
    )
    st.header("Speed")
    st.markdown(
        "OCR is CPU-bound: about **4 seconds per page**, so a 30-page part takes "
        "roughly 2 minutes on a small free server. Upload a few files at a time."
    )
    st.header("Accuracy notes")
    st.markdown(
        "- Where `serial` and `seq` disagree, trust **`seq`** — it is the position in "
        "reading order and needs no OCR of the cramped serial box.\n"
        "- Output is OCR. Letters that resemble digits (A/4, I/1, O/0, S/5) can be "
        "misread, most often in house numbers.\n"
        "- Some entries with very long names have their `Age`/`Gender` line clipped "
        "by the source PDF itself; those come through blank and are flagged."
    )
    if engine.TESS:
        st.success("Tesseract found", icon="✅")
    else:
        st.error("Tesseract not installed — extraction will fail.", icon="⚠️")

uploads = st.file_uploader("Roll PDFs", type=["pdf"], accept_multiple_files=True)

if uploads:
    st.caption(f"{len(uploads)} file(s) selected, "
               f"{sum(len(u.getvalue()) for u in uploads)/1e6:.1f} MB total")
    if len(uploads) > 8:
        st.warning(
            f"{len(uploads)} files will take a while — roughly "
            f"{len(uploads)*2} minutes. Consider smaller batches.", icon="⏳")

go = st.button("Extract", type="primary", disabled=not uploads)

if go and uploads:
    rows, part_recs, problems = extract_all(uploads)
    if not rows:
        st.error("No rows extracted.")
        for p in problems:
            st.markdown("- " + p)
    else:
        electors = pd.DataFrame(rows)
        electors = electors[[c for c in PREVIEW_COLS if c in electors.columns]]
        parts = pd.DataFrame(part_recs)
        st.session_state["electors"] = electors
        st.session_state["parts"] = parts
        st.session_state["problems"] = problems

if "electors" in st.session_state:
    electors = st.session_state["electors"]
    parts = st.session_state["parts"]
    problems = st.session_state.get("problems", [])

    clean = int((parts["status"] != "CHECK").sum())
    agree = int((electors["serial"].astype(str) == electors["seq"].astype(str)).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Electors", f"{len(electors):,}")
    c2.metric("Parts reconciled", f"{clean} / {len(parts)}")
    c3.metric("Unique EPICs", f"{electors['epic'].nunique():,}")
    c4.metric("Flagged rows", f"{int((electors['flag'] != '').sum()):,}")

    if clean < len(parts):
        st.warning("Some parts did not reconcile with their cover page — see the "
                   "**Parts & validation** tab.", icon="⚠️")
    else:
        st.success(f"All {len(parts)} part(s) reconcile with their cover-page totals "
                   f"(count and male/female/third-gender split).", icon="✅")
    st.caption(f"Printed serial agrees with reading order on {agree:,} of "
               f"{len(electors):,} rows ({100.0*agree/max(len(electors),1):.1f}%). "
               f"Prefer `seq` where they differ.")

    for p in problems:
        st.error(p)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            "⬇️ Download Excel workbook", data=build_xlsx(electors, parts),
            file_name=f"electoral_roll_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True)
    with d2:
        st.download_button(
            "⬇️ Download CSV", data=electors.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"electoral_roll_{stamp}.csv", mime="text/csv",
            use_container_width=True)

    t1, t2 = st.tabs(["Electors", "Parts & validation"])
    with t1:
        q = st.text_input("Filter (matches name, EPIC, relative or house)",
                          placeholder="e.g. SHARMA or UBV0901298")
        view = electors
        if q:
            ql = q.strip().lower()
            mask = False
            for col in ("name", "epic", "relative_name", "house"):
                if col in view.columns:
                    mask = (view[col].astype(str).str.lower().str.contains(ql, na=False)
                            | mask)
            view = view[mask]
            st.caption(f"{len(view):,} matching row(s)")
        st.dataframe(view, use_container_width=True, height=560, hide_index=True)
    with t2:
        st.dataframe(parts, use_container_width=True, height=560, hide_index=True)

st.divider()
st.caption(
    "Source PDFs are published openly by the Election Commission of India. Uploads "
    "are processed in memory and deleted immediately; nothing is retained by this "
    "app. Output is OCR — verify any individual row against the source page before "
    "relying on it officially."
)
