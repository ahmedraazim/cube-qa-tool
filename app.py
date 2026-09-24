"""
CUBE New-Book QA Tool — Streamlit Application v11
Tabbed layout · All checks collapsed · Interactive filters · Pipeline log in reports
"""
from __future__ import annotations

import json as _json
import os, sys, re, tempfile, time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional, List

import streamlit as st
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="CUBE QA Tool", page_icon="📋",
    layout="wide", initial_sidebar_state="expanded",
)

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from modules.models import AuditReport, Decision, ExecutionStatus, QAStatus, Route
from modules.qa_engine import QARunner, PipelineEvent
from modules.report_generator import generate_html, generate_json, generate_csv
from modules.rm_adapter import RMAdapter, extract_book_id_from_url, compare_xml_rm
from modules.xml_to_json import (
    parsed_xml_to_json, flat_xml_nodes, source_to_nodes,
    build_comparison_tree, xml_node_tree,
)
from modules.comparison_engine import (
    parse_source_blocks, extract_xml_blocks, match_blocks,
    coverage_report, _word_diff_html,
)
from modules.auto_extract import (
    build_auto_corrections, detect_language_from_text,
    extract_issuance_type, extract_issuance_date, extract_issuing_body,
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.decision-approve{background:#dcffe4;border:2px solid #22863a;padding:14px 20px;
  border-radius:8px;font-size:1.3em;font-weight:700;color:#22863a;display:inline-block}
.decision-fail{background:#ffeef0;border:2px solid #cb2431;padding:14px 20px;
  border-radius:8px;font-size:1.3em;font-weight:700;color:#cb2431;display:inline-block}
.decision-hold{background:#fff5b1;border:2px solid #e36209;padding:14px 20px;
  border-radius:8px;font-size:1.3em;font-weight:700;color:#b45309;display:inline-block}
.decision-blocked{background:#f5f0ff;border:2px solid #6f42c1;padding:14px 20px;
  border-radius:8px;font-size:1.3em;font-weight:700;color:#6f42c1;display:inline-block}
.finding-box{background:#fff8dc;border-left:3px solid #e36209;
  padding:6px 10px;margin:4px 0;font-size:.88em;border-radius:2px}
.correction-box{background:#e8f4fd;border-left:3px solid #0366d6;
  padding:6px 10px;margin:4px 0;font-size:.88em;border-radius:2px}
.limit-note{color:#666;font-size:.83em;font-style:italic}
div[data-testid="stExpander"] summary{cursor:pointer}
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
for k, v in [("report", None), ("pipeline_log", []), ("running", False)]:
    if k not in st.session_state:
        st.session_state[k] = v

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📋 CUBE QA Tool")
    st.caption("New-Book QA Automation")
    st.markdown("---")

    st.header("1. Book XML")
    xml_file = st.file_uploader("Upload book XML *", type=["xml"], key="xml_upload")

    st.header("2. XSD Schema")
    xsd_file = st.file_uploader("Optional XSD / .txt XSD",
                                 type=["xsd","txt","xml"], key="xsd_upload")

    st.header("3. Source Document")
    st.caption("Auto-fetched from XML URL via Selenium. Upload only if login required.")
    source_file = st.file_uploader("Upload source (PDF, HTML, DOCX, TXT)",
                                    type=["pdf","html","htm","docx","txt"], key="source_upload")
    source_url_input = st.text_input("Override source URL",
                                      placeholder="https://  — leave blank to use URL from XML",
                                      key="source_url")
    with st.expander("JS-rendered pages (BCB, UAE, etc.)"):
        st.markdown(
            "Selenium auto-renders JavaScript pages (Chrome or Edge).  \n"
            "Fallback: Ctrl+S in your browser → save as Webpage, Complete → upload above."
        )

    st.header("4. Native & Translation")
    native_file = st.file_uploader("Native-language capture (optional)",
                                    type=["pdf","html","htm","docx","txt"], key="native_upload")
    translation_file = st.file_uploader("English translation (optional)",
                                         type=["pdf","html","htm","docx","txt"],
                                         key="translation_upload")

    st.markdown("---")
    st.header("5. RM Connection")
    rm_url_input = st.text_input("RM Review URL",
                                  placeholder="https://rm.gocube.global/rdi/book/review/…",
                                  key="rm_url")
    rm_export_file = st.file_uploader("RM metadata export (JSON fallback)",
                                       type=["json"], key="rm_export")

    st.markdown("---")
    st.header("6. AI Integration")
    ai_key = st.text_input("Anthropic API Key (optional)", type="password", key="ai_key")
    ai_enabled = bool(ai_key and ai_key.strip())
    st.caption("✅ AI enabled" if ai_enabled else "AI checks disabled (no key)")

    st.markdown("---")
    run_btn = st.button("▶ Run QA", type="primary",
                         disabled=(xml_file is None), use_container_width=True)

# ── Welcome ───────────────────────────────────────────────────────────────────
if xml_file is None:
    st.title("CUBE New-Book QA")
    st.info("Upload a book XML file in the sidebar to begin.")
    with st.expander("How this tool works"):
        st.markdown("""
**Step 1 — XML → JSON:** Full parse with metadata table, body sections, schema validation.

**Step 2 — Selenium fetch:** Headless Chrome/Edge opens the source URL, renders JavaScript, extracts all content.

**Step 3 — Node comparison:** Source parsed into articles by text pattern → matched to XML nodes with article-number anchoring + text similarity + word-level diff.

**Step 4 — Auto-corrections:** Issuance type, date, and issuing body extracted from source content automatically.

**Step 5 — 12 QA Checks:** Full CUBE New-Book checklist → FAIL / HOLD / APPROVE decision with routing.

**Step 6 — RDM scan:** Selenium opens the RM review URL and reads every visible metadata field.
        """)
    st.stop()

# ── Run ───────────────────────────────────────────────────────────────────────
if run_btn:
    st.session_state.report = None
    st.session_state.pipeline_log = []
    st.session_state.running = True

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        xml_path = tmp / xml_file.name
        xml_path.write_bytes(xml_file.getvalue())

        xsd_path = None
        if xsd_file:
            xsd_path = tmp / xsd_file.name
            xsd_path.write_bytes(xsd_file.getvalue())

        source_path = None
        if source_file:
            source_path = tmp / source_file.name
            source_path.write_bytes(source_file.getvalue())

        native_path = None
        if native_file:
            native_path = tmp / native_file.name
            native_path.write_bytes(native_file.getvalue())

        translation_path = None
        if translation_file:
            translation_path = tmp / translation_file.name
            translation_path.write_bytes(translation_file.getvalue())

        rm_export_path = None
        if rm_export_file:
            rm_export_path = tmp / rm_export_file.name
            rm_export_path.write_bytes(rm_export_file.getvalue())

        ai_client = None
        if ai_enabled:
            try:
                import anthropic
                ai_client = anthropic.Anthropic(api_key=ai_key)
            except Exception as exc:
                st.warning(f"AI client: {exc}")

        runner = QARunner(
            xml_path=xml_path, xsd_path=xsd_path,
            source_path=source_path,
            source_url=source_url_input.strip() if source_url_input else None,
            native_path=native_path, translation_path=translation_path,
            rm_review_url=rm_url_input.strip() if rm_url_input else None,
            rm_export_path=rm_export_path, ai_client=ai_client,
        )

        with st.container():
            st.subheader("⏳ Running pipeline…")
            pbar = st.progress(0)
            ph = st.empty()
            total_stages = 24
            count = 0
            log: list = []
            report: Optional[AuditReport] = None

            for event in runner.run():
                count += 1
                pbar.progress(min(count / total_stages, 0.99))
                icon = {"running":"⏳","completed":"✅","failed":"❌","blocked":"🚫"}.get(event.status,"•")
                log.append(f"{icon} **{event.stage}** — {event.message[:120]}")
                ph.markdown("\n\n".join(log[-10:]))
                if event.stage == "COMPLETE" and isinstance(event.detail, AuditReport):
                    report = event.detail
                    pbar.progress(1.0)

        if report:
            st.session_state.report = report
            st.session_state.pipeline_log = log

    st.session_state.running = False
    st.rerun()

# ── Results ───────────────────────────────────────────────────────────────────
report: Optional[AuditReport] = st.session_state.report
pipeline_log: list = st.session_state.pipeline_log

if report is None:
    st.stop()

p = report.parsed_xml
src_doc = report.source_document

# ── Decision banner ───────────────────────────────────────────────────────────
dec = report.overall_decision
dec_class = {
    Decision.APPROVE:"decision-approve", Decision.FAIL:"decision-fail",
    Decision.HOLD:"decision-hold", Decision.BLOCKED:"decision-blocked",
}.get(dec, "decision-hold")
st.markdown(f'<div class="{dec_class}">🏁 {dec.value}</div>', unsafe_allow_html=True)
st.markdown("")

c1, c2, c3 = st.columns(3)
c1.metric("Confirmed Failures", report.confirmed_failures)
c2.metric("Unresolved Checks",  report.unresolved_checks)
c3.metric("Primary Route",      report.primary_route.value)

# ── Quick-filter pills ────────────────────────────────────────────────────────
all_checks = report.check_results
status_counts = {}
for cr in all_checks:
    status_counts[cr.qa_status.value] = status_counts.get(cr.qa_status.value, 0) + 1

st.markdown("---")
pill_col = st.columns(len(status_counts) + 1)
filter_status = pill_col[0].selectbox(
    "Filter checks", ["All"] + list(status_counts.keys()),
    label_visibility="collapsed", key="check_filter",
)

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_xml, tab_source, tab_cmp, tab_corrections, tab_checks, tab_rdm, tab_report = st.tabs([
    "📂 XML & JSON",
    "🌐 Source",
    "🔍 Comparison",
    "🔧 Corrections",
    "✅ QA Checks",
    "🔗 RDM",
    "📄 Report & Log",
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — XML & JSON
# ══════════════════════════════════════════════════════════════════════════════
with tab_xml:
    st.subheader("XML Parsed to JSON")
    xml_json = parsed_xml_to_json(p)
    t_meta, t_body, t_raw = st.tabs(["Metadata", "Body Sections", "Full JSON"])

    with t_meta:
        meta = xml_json["metadata"]
        rows_meta = []
        for k, v in meta.items():
            val_str = str(v) if v is not None else ""
            flag = "❌" if not val_str else ("⚠️" if "DEFAULT" in val_str else "✅")
            rows_meta.append({"Flag": flag, "Field": k, "Value": val_str or "(blank)"})
        df_meta = pd.DataFrame(rows_meta)
        def _style_meta(row):
            if row["Flag"] == "❌": return ["","","background:#ffeef0;color:#cb2431"]
            if row["Flag"] == "⚠️": return ["","","background:#fff5b1;color:#b45309"]
            return ["","",""]
        st.dataframe(df_meta.style.apply(_style_meta, axis=1),
                     use_container_width=True, hide_index=True)

    with t_body:
        body = xml_json["body"]
        st.caption(f"Sections: {body['total_sections']} | Images: {len(body['images'])} | "
                   f"Tables: {body['tables']} | Footnotes: {body['footnotes']}")
        xml_nodes = flat_xml_nodes(p)
        if xml_nodes:
            st.dataframe(pd.DataFrame([{
                "Depth": n["depth"], "Section": n["section_title"][:60],
                "Para ID": n["para_id"], "Text": n["text"][:200], "XPath": n["xpath"],
            } for n in xml_nodes]), use_container_width=True, hide_index=True)
        else:
            st.info("No paragraph nodes in XML body.")

    with t_raw:
        st.json(xml_json)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — SOURCE
# ══════════════════════════════════════════════════════════════════════════════
with tab_source:
    st.subheader("Source URL — Fetched via Selenium")
    from modules.source_retriever import SELENIUM_AVAILABLE
    if SELENIUM_AVAILABLE:
        st.success("✅ Selenium is installed")
    else:
        st.error("❌ Selenium not installed — run: pip install selenium webdriver-manager")

    if src_doc is None:
        st.warning("No source fetched.")
    else:
        src_text = (src_doc.text or "").strip()
        src_warnings = src_doc.extraction_warnings or []
        js_ok = any("selenium" in w.lower() or "playwright" in w.lower() for w in src_warnings)
        js_fail = any("failed" in w.lower() and "javascript" in w.lower() for w in src_warnings)

        st.markdown(
            f"**Origin:** `{src_doc.origin}`  \n"
            f"**Method:** `{src_doc.extraction_method}` | "
            f"**HTTP:** `{src_doc.retrieval_status_code or 'n/a'}` | "
            f"**Chars:** `{len(src_text):,}`"
        )

        if js_ok and len(src_text) > 200:
            st.success(f"✅ JavaScript rendered — {len(src_text):,} chars extracted")
        elif src_doc.retrieval_error and not src_text:
            st.error(f"❌ {src_doc.retrieval_error}")
        elif src_text:
            st.info(f"📄 {len(src_text):,} chars extracted")
        else:
            st.error("❌ No content extracted")

        for w in src_warnings[:3]:
            if w:
                st.warning(w)

        if not src_text or len(src_text) < 100:
            st.info(
                "**To get full content:** Open the URL in Chrome → wait for it to load → "
                "Ctrl+S → save as **Webpage, Complete** → upload in sidebar."
            )

        if src_text:
            with st.expander(f"📄 Source text ({len(src_text):,} chars)", expanded=False):
                st.text_area("", src_text[:6000], height=300, key="src_raw",
                             label_visibility="collapsed")
            if src_doc.headings:
                with st.expander(f"Headings ({len(src_doc.headings)})", expanded=False):
                    for h in src_doc.headings[:30]:
                        st.markdown(f"- {h}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — COMPARISON
# ══════════════════════════════════════════════════════════════════════════════
with tab_cmp:
    st.subheader("Node-by-Node Comparison: Source vs XML")
    src_text_cmp = (src_doc.text or "").strip() if src_doc else ""
    xml_blocks = extract_xml_blocks(p)

    if not src_text_cmp or len(src_text_cmp) < 80:
        st.warning(
            "Source not available. Upload source document or ensure the URL is reachable.  \n"
            "**Quick fix:** Ctrl+S in browser → Webpage, Complete → upload in sidebar."
        )
        if xml_blocks:
            st.markdown("**XML document structure:**")
            for xb in xml_blocks:
                indent = "　" * xb.depth
                st.markdown(f"{indent}**{xb.title}** — {xb.text[:100]}")
    else:
        src_blocks = parse_source_blocks(src_text_cmp)
        match_results, unmatched_src = match_blocks(src_blocks, xml_blocks)
        cov = coverage_report(match_results, src_blocks, xml_blocks)

        cm1, cm2, cm3, cm4, cm5 = st.columns(5)
        cm1.metric("XML nodes", cov["total_xml_nodes"])
        cm2.metric("✅ Matched", cov["xml_matched"])
        cm3.metric("⚠️ Partial", cov["xml_partial"])
        cm4.metric("❌ Missing", cov["xml_missing"])
        cm5.metric("Match rate", f"{cov['xml_match_pct']}%")

        pct = cov["xml_match_pct"]
        (st.success if pct >= 80 else st.warning if pct >= 50 else st.error)(
            f"{'✅' if pct >= 80 else '⚠️' if pct >= 50 else '❌'} {pct}% of XML content confirmed in source."
        )

        if cov["numbers_in_source_missing_from_xml"]:
            st.error(f"Numbers in source missing from XML: {', '.join(cov['numbers_in_source_missing_from_xml'][:8])}")

        fsel = st.radio("Filter:", ["All","✅ Match","⚠️ Partial","❌ Missing / Changed"],
                        horizontal=True, key="cmp_filter")
        show_diff = st.checkbox("Show word-level diff (🟥 missing from XML | 🟩 extra in XML)", value=True)

        for row in match_results:
            if fsel != "All" and not row["status"].startswith(fsel[:2]):
                continue
            with st.expander(
                f"{row['status']} | **{row['xml_section'][:55]}** | {row['score']:.0%} | {row['strategy']}",
                expanded=False,
            ):
                col_s, col_m, col_x = st.columns([5, 1, 5])
                with col_s:
                    st.markdown(f"**📄 Source** — `{row['src_marker'][:50]}`")
                    html_content = row.get("src_diff_html","") if show_diff else ""
                    st.markdown(
                        f'<div style="background:#fafafa;border:1px solid #ddd;border-radius:4px;'
                        f'padding:8px;font-size:.88em;line-height:1.5">'
                        f'{html_content or row["src_text"][:400]}</div>',
                        unsafe_allow_html=True,
                    )
                with col_m:
                    st.markdown(
                        f'<div style="text-align:center;font-size:1.5em;margin-top:28px">'
                        f'{"✅" if "✅" in row["status"] else "⚠️" if "⚠️" in row["status"] else "❌"}'
                        f'<br><small style="font-size:.5em">{row["score"]:.0%}</small></div>',
                        unsafe_allow_html=True,
                    )
                with col_x:
                    st.markdown(f"**🗂️ XML** — `{row['xml_section'][:50]}`")
                    html_content = row.get("xml_diff_html","") if show_diff else ""
                    st.markdown(
                        f'<div style="background:#fafafa;border:1px solid #ddd;border-radius:4px;'
                        f'padding:8px;font-size:.88em;line-height:1.5">'
                        f'{html_content or row["xml_text"][:400]}</div>',
                        unsafe_allow_html=True,
                    )
                if row["num_issues"]:
                    for ni in row["num_issues"]:
                        st.warning(f"🔢 {ni}")

        if unmatched_src:
            with st.expander(f"📋 {len(unmatched_src)} source block(s) not in XML", expanded=True):
                for sb in unmatched_src[:15]:
                    st.markdown(
                        f'<div style="background:#fff5f5;border-left:3px solid #cb2431;'
                        f'padding:8px;margin:4px 0;font-size:.88em">'
                        f'<b>[{sb.marker[:60]}]</b><br>{sb.text[:300]}</div>',
                        unsafe_allow_html=True,
                    )

        st.caption("🟥 Red = in source, missing/changed in XML &nbsp;|&nbsp; 🟩 Green = in XML, not in source")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — AUTO-CORRECTIONS
# ══════════════════════════════════════════════════════════════════════════════
with tab_corrections:
    st.subheader("🔧 Auto-Suggested Corrections")
    corrections = build_auto_corrections(p, src_doc)

    if corrections:
        st.info(
            f"**{len(corrections)} field(s)** can be auto-corrected from source content. "
            "Review each suggestion before applying."
        )
        for i, corr in enumerate(corrections):
            conf_color = "#22863a" if "High" in corr["confidence"] else "#e36209"
            with st.expander(
                f"📝 **{corr['field']}** — suggested: `{corr['suggested']}`",
                expanded=True,
            ):
                ca, cb = st.columns(2)
                with ca:
                    st.markdown(f"**Current:** `{corr['current']}`")
                    st.markdown(f"**Suggested:** `{corr['suggested']}`")
                    st.caption(f"XPath: `{corr['xpath']}`")
                with cb:
                    st.markdown(f"**Evidence:** {corr['evidence']}")
                    st.markdown(
                        f'<span style="color:{conf_color};font-weight:600">'
                        f"Confidence: {corr['confidence']}</span>",
                        unsafe_allow_html=True,
                    )
                st.code(corr["suggested"], language=None)

        corrections_json = _json.dumps(corrections, indent=2, ensure_ascii=False)
        st.download_button(
            "⬇️ Export Corrections JSON",
            data=corrections_json.encode("utf-8"),
            file_name=f"corrections_{report.run_id[:8]}.json",
            mime="application/json",
        )
    elif src_doc and src_doc.text:
        st.success("✅ No auto-corrections needed — all extractable fields look good.")
    else:
        st.info("Upload the source document to enable auto-extraction of corrections.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — QA CHECKS
# ══════════════════════════════════════════════════════════════════════════════
with tab_checks:
    st.subheader("QA Checks — All 12")

    STATUS_ICONS = {
        QAStatus.PASS:"✅ Pass", QAStatus.FAIL:"❌ Fail",
        QAStatus.UNVERIFIED:"⚠️ Unverified", QAStatus.NA:"➖ N/A",
        QAStatus.INFORMATIONAL:"ℹ️ Info",
    }

    # Summary bar
    stat_cols = st.columns(5)
    for col, (label, color, val) in zip(stat_cols, [
        ("✅ Pass",       "#22863a", sum(1 for c in all_checks if c.qa_status==QAStatus.PASS)),
        ("❌ Fail",       "#cb2431", sum(1 for c in all_checks if c.qa_status==QAStatus.FAIL)),
        ("⚠️ Unverified","#e36209", sum(1 for c in all_checks if c.qa_status.value=="Unverified")),
        ("ℹ️ Info",       "#0366d6", sum(1 for c in all_checks if c.qa_status.value=="Informational")),
        ("➖ N/A",        "#586069", sum(1 for c in all_checks if c.qa_status.value=="N/A")),
    ]):
        col.markdown(
            f'<div style="text-align:center;background:#f6f8fa;border-radius:8px;'
            f'padding:10px;border:1px solid #e1e4e8">'
            f'<div style="font-size:1.5em;font-weight:700;color:{color}">{val}</div>'
            f'<div style="font-size:.82em;color:#666">{label}</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("")

    # All checks — collapsed by default, click arrow to expand
    shown = [cr for cr in all_checks
             if filter_status == "All" or cr.qa_status.value == filter_status]

    for cr in shown:
        icon = STATUS_ICONS.get(cr.qa_status, str(cr.qa_status.value))
        # ALWAYS collapsed — user must click to expand
        with st.expander(f"Check {cr.check_number}: {cr.check_name} — {icon}", expanded=False):
            col_a, col_b = st.columns([3, 1])
            with col_a:
                st.markdown(f"**Reason:** {cr.reason}")
                if cr.recommended_correction:
                    st.markdown(
                        f'<div class="correction-box">🔧 <b>Correction:</b> {cr.recommended_correction}</div>',
                        unsafe_allow_html=True,
                    )
            with col_b:
                st.markdown(f"**Status:** `{cr.qa_status.value}`")
                st.markdown(f"**Execution:** `{cr.execution_status.value}`")
                if cr.method:    st.markdown(f"**Method:** {cr.method}")
                if cr.coverage:  st.markdown(f"**Coverage:** {cr.coverage}")
                if cr.routing and cr.routing != Route.NONE:
                    st.markdown(f"**Route:** {cr.routing.value}")

            if cr.findings:
                st.markdown("**Findings:**")
                for f in cr.findings:
                    parts = []
                    if f.observed: parts.append(f"**Observed:** {f.observed[:300]}")
                    if f.expected: parts.append(f"**Expected:** {f.expected[:200]}")
                    if f.xpath:    parts.append(f"`{f.xpath}`")
                    if f.excerpt_source: parts.append(f"*Source:* {f.excerpt_source[:180]}")
                    if f.excerpt_xml:    parts.append(f"*XML:* {f.excerpt_xml[:180]}")
                    st.markdown(
                        '<div class="finding-box">' + "<br>".join(parts) + "</div>",
                        unsafe_allow_html=True,
                    )

            for note in (cr.informational_notes or []):
                st.caption(f"ℹ️ {note}")
            for lim in (cr.verification_limitations or []):
                st.markdown(f'<p class="limit-note">⚠️ {lim}</p>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — RDM
# ══════════════════════════════════════════════════════════════════════════════
with tab_rdm:
    st.subheader("RDM Metadata Scanner")

    if not rm_url_input or not rm_url_input.strip().startswith("http"):
        st.info(
            "Enter the **RM Review URL** in the sidebar (section 5).  \n"
            "Selenium will open that page, read every visible metadata field, "
            "and compare it against the XML automatically."
        )
        st.markdown("""
**What it scans:**
- Book Name / Content Title
- Issuance Type, Issue Date, Effective Date
- Issuing Body, Jurisdiction / Country
- Language, Source URL, Citation

**If the page requires login:** log in manually in your browser, export the metadata as JSON from RDM, and upload under "RM metadata export" in the sidebar.
        """)
    elif report.rm_metadata:
        rm_meta = report.rm_metadata
        rm_raw = getattr(rm_meta, "raw_data", {}) or {}

        fields_found = len(rm_raw)
        if rm_meta.book_name or rm_meta.issuance_type or rm_meta.issuing_body:
            st.success(f"✅ RDM page scanned — {fields_found} field(s) found on page.")
        else:
            st.warning("Page opened but no structured metadata fields recognised.")

        rows = compare_xml_rm(p, rm_meta)
        df_rm = pd.DataFrame(rows)

        def _style_rm(row):
            f = row.get("finding","")
            if f == "CONFLICT":
                return ["","background:#ffeef0;color:#cb2431;font-weight:600",
                        "background:#ffeef0;color:#cb2431;font-weight:600",""]
            if f == "Match":
                return ["","background:#f0fff4","background:#f0fff4",""]
            if "Missing" in f:
                return ["","background:#fff5f5","",""]
            return ["","","",""]

        st.dataframe(df_rm.style.apply(_style_rm, axis=1),
                     use_container_width=True, hide_index=True)

        conflicts = [r for r in rows if r.get("finding") == "CONFLICT"]
        missing   = [r for r in rows if "Missing" in r.get("finding","")]

        if conflicts:
            st.error(f"❌ {len(conflicts)} conflict(s) — XML and RDM disagree:")
            for c in conflicts:
                st.markdown(f"- **{c['field']}**: XML=`{c['xml_value']}` vs RDM=`{c['rm_value']}`")
        if missing:
            st.warning(f"⚠️ {len(missing)} field(s) present in RDM but missing in XML.")
        if not conflicts and not missing:
            st.success("✅ XML and RDM metadata are fully consistent.")

        if rm_raw:
            with st.expander(f"All {len(rm_raw)} raw fields from RDM page", expanded=False):
                for label, value in rm_raw.items():
                    if not label.startswith("_"):
                        st.markdown(f"**{label}:** {value}")
    else:
        st.error(
            "Could not scan RDM page.  \n"
            "Ensure Chrome or Edge is installed and the URL is reachable from this machine."
        )
        st.info(
            "**Workaround:** Log into RDM in your browser → export metadata as JSON → "
            "upload under 'RM metadata export' in the sidebar."
        )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 7 — REPORT & LOG
# ══════════════════════════════════════════════════════════════════════════════
with tab_report:
    st.subheader("Download Reports")

    dc1, dc2, dc3 = st.columns(3)
    with dc1:
        st.download_button(
            "⬇️ HTML Report (full)",
            data=generate_html(report, pipeline_log).encode("utf-8"),
            file_name=f"cube_qa_{report.run_id[:8]}.html",
            mime="text/html", use_container_width=True,
        )
    with dc2:
        st.download_button(
            "⬇️ JSON Report (full)",
            data=generate_json(report, pipeline_log).encode("utf-8"),
            file_name=f"cube_qa_{report.run_id[:8]}.json",
            mime="application/json", use_container_width=True,
        )
    with dc3:
        st.download_button(
            "⬇️ CSV Summary",
            data=generate_csv(report).encode("utf-8"),
            file_name=f"cube_qa_{report.run_id[:8]}.csv",
            mime="text/csv", use_container_width=True,
        )

    st.markdown("---")
    st.subheader("📋 Handover Note")
    st.text_area("", report.handover_note, height=110, key="handover",
                 label_visibility="collapsed")
    st.caption("Copy this note and paste it into the RM handover field.")

    st.markdown("---")
    st.subheader("🔧 Pipeline Execution Log")
    if pipeline_log:
        with st.container():
            for line in pipeline_log:
                st.markdown(line)
    else:
        st.info("No log available.")
