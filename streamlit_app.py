"""Electoral roll PDF -> Excel.

Single-page tool: upload scanned SIR draft-roll PDFs, get the elector table and
download it. No developer chrome, no extra tabs.
"""
import io
import os
import tempfile
import datetime

import pandas as pd
import streamlit as st

import engine

MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "60"))

COLS = ["part", "polling_station", "section", "serial", "seq", "epic", "name",
        "relation", "relative_name", "house", "age", "gender", "page", "flag"]

st.set_page_config(page_title="Electoral Roll PDF → Excel", page_icon="🗳️",
                   layout="wide", menu_items={})

# Strip Streamlit's developer chrome: top bar, Deploy button, hamburger menu
# (Rerun / Auto rerun / Clear cache / Print / Record screen), footer badge.
st.markdown("""
<style>
  header[data-testid="stHeader"] {display: none;}
  [data-testid="stToolbar"], [data-testid="stDecoration"],
  [data-testid="stStatusWidget"], [data-testid="stAppDeployButton"],
  .stAppDeployButton, #MainMenu, footer {display: none !important;}
  [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {display: none;}
  .block-container {padding-top: 2.5rem; padding-bottom: 2rem; max-width: 1500px;}
</style>
""", unsafe_allow_html=True)


def build_xlsx(electors: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        electors.to_excel(xl, sheet_name="Electors", index=False)
        ws = xl.sheets["Electors"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, col in enumerate(electors.columns, 1):
            longest = electors[col].astype(str).str.len().max() if len(electors) else 10
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = \
                max(10, min(46, int(longest or 10) + 2))
    return buf.getvalue()


def extract(uploads):
    rows, reconciled, failures = [], 0, []
    bar = st.progress(0.0, text="Starting…")
    n = len(uploads)

    for idx, up in enumerate(uploads):
        data = up.getvalue()
        if len(data) / 1e6 > MAX_MB:
            failures.append(f"{up.name} — {len(data)/1e6:.0f} MB exceeds the "
                            f"{MAX_MB} MB limit")
            continue
        tmp = os.path.join(tempfile.gettempdir(), f"roll_{os.getpid()}_{idx}.pdf")
        with open(tmp, "wb") as fh:
            fh.write(data)
        try:
            def prog(frac, msg, idx=idx, name=up.name):
                bar.progress(min((idx + frac) / n, 1.0), text=f"{name} — {msg}")

            got, _cover, summ = engine.process_pdf(tmp, progress=prog)
            rows.extend(got)
            if summ["status"] != "CHECK":
                reconciled += 1
        except Exception as exc:
            failures.append(f"{up.name} — {exc}")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    bar.empty()
    return rows, reconciled, failures


st.title("🗳️ Electoral Roll PDF → Excel")
st.caption(
    "Upload scanned SIR draft electoral roll PDFs — the image-only kind with no "
    "selectable text — and download the electors as a spreadsheet. "
    "Roughly 4 seconds per page, so a 30-page part takes about two minutes."
)

uploads = st.file_uploader("Roll PDFs", type=["pdf"], accept_multiple_files=True,
                           label_visibility="collapsed")

if st.button("Extract", type="primary", disabled=not uploads) and uploads:
    if not engine.TESS:
        st.error("Tesseract OCR is not available on this server, so extraction "
                 "cannot run.")
    else:
        rows, reconciled, failures = extract(uploads)
        st.session_state["df"] = None
        if rows:
            df = pd.DataFrame(rows)
            st.session_state["df"] = df[[c for c in COLS if c in df.columns]]
            st.session_state["reconciled"] = (reconciled, len(uploads))
        st.session_state["failures"] = failures

for msg in st.session_state.get("failures", []):
    st.error(msg)

df = st.session_state.get("df")
if df is not None and len(df):
    done, total = st.session_state.get("reconciled", (0, 0))
    if done == total:
        st.success(f"**{len(df):,} electors** extracted — every file reconciles with "
                   f"the totals printed on its own cover page.")
    else:
        st.warning(f"**{len(df):,} electors** extracted, but {total - done} of {total} "
                   f"file(s) did not match their cover-page totals. Check rows where "
                   f"the `flag` column is set.")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    c1, c2, _ = st.columns([1, 1, 3])
    c1.download_button(
        "⬇️  Excel", data=build_xlsx(df), file_name=f"electors_{stamp}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True)
    c2.download_button(
        "⬇️  CSV", data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"electors_{stamp}.csv", mime="text/csv",
        use_container_width=True)

    q = st.text_input("Search", placeholder="Name, EPIC number, relative or house",
                      label_visibility="collapsed")
    view = df
    if q:
        ql = q.strip().lower()
        mask = False
        for col in ("name", "epic", "relative_name", "house"):
            if col in view.columns:
                mask = view[col].astype(str).str.lower().str.contains(ql, na=False) | mask
        view = view[mask]
        st.caption(f"{len(view):,} of {len(df):,} rows match")
    st.dataframe(view, use_container_width=True, height=600, hide_index=True)
