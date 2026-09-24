"""
Report generation: HTML, JSON, CSV audit reports.
Includes pipeline log in all formats.
"""
from __future__ import annotations

import csv
import json
import io
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import AuditReport, CheckResult, QAStatus


# ─── JSON ─────────────────────────────────────────────────────────────────────

def generate_json(report: AuditReport, pipeline_log: Optional[List[str]] = None) -> str:
    def _finding(f) -> dict:
        return {
            "xml_id": f.xml_id,
            "parent_section_id": f.parent_section_id,
            "xpath": f.xpath,
            "source_location": f.source_location,
            "observed": f.observed,
            "expected": f.expected,
            "excerpt_xml": f.excerpt_xml,
            "excerpt_source": f.excerpt_source,
            "severity": f.severity,
        }

    def _check(c: CheckResult) -> dict:
        return {
            "check_number": c.check_number,
            "check_name": c.check_name,
            "qa_status": c.qa_status.value,
            "execution_status": c.execution_status.value,
            "reason": c.reason,
            "findings": [_finding(f) for f in c.findings],
            "method": c.method,
            "coverage": c.coverage,
            "recommended_correction": c.recommended_correction,
            "routing": c.routing.value if c.routing else None,
            "verification_limitations": c.verification_limitations,
            "informational_notes": c.informational_notes,
        }

    data = {
        "run_id": report.run_id,
        "run_timestamp": report.run_timestamp.isoformat(),
        "xml_filename": report.xml_filename,
        "xml_hash": report.xml_hash,
        "source_identity": report.source_identity,
        "source_hash": report.source_hash,
        "overall_decision": report.overall_decision.value,
        "primary_route": report.primary_route.value,
        "secondary_routes": [r.value for r in report.secondary_routes],
        "routing_explanation": report.routing_explanation,
        "handover_note": report.handover_note,
        "confirmed_failures": report.confirmed_failures,
        "unresolved_checks": report.unresolved_checks,
        "schema_validation_passed": report.schema_validation_passed,
        "schema_errors": report.schema_errors,
        "checks": [_check(c) for c in report.check_results],
        "pipeline_log": pipeline_log or [],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


# ─── CSV ──────────────────────────────────────────────────────────────────────

def generate_csv(report: AuditReport) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Check #", "Check Name", "QA Status", "Execution Status",
        "Reason", "Recommended Correction", "Routing",
        "Finding Count", "Method", "Coverage",
    ])
    for c in report.check_results:
        writer.writerow([
            c.check_number, c.check_name, c.qa_status.value,
            c.execution_status.value, c.reason[:300],
            c.recommended_correction or "",
            c.routing.value if c.routing else "",
            len(c.findings), c.method, c.coverage,
        ])
    return buf.getvalue()


# ─── HTML ─────────────────────────────────────────────────────────────────────

_STATUS_ICON = {
    "Pass": "✅", "Fail": "❌", "Unverified": "⚠️",
    "N/A": "➖", "Informational": "ℹ️", "Blocked": "🚫",
}
_STATUS_COLOR = {
    "Pass": "#22863a", "Fail": "#cb2431", "Unverified": "#e36209",
    "N/A": "#586069", "Informational": "#0366d6", "Blocked": "#6f42c1",
}
_DECISION_COLOR = {
    "APPROVE FOR PUBLISH": "#22863a", "FAIL": "#cb2431",
    "HOLD — VERIFICATION INCOMPLETE": "#e36209",
    "BLOCKED — INVALID INPUT": "#6f42c1",
}


def _badge(text: str, color: str) -> str:
    return (
        f'<span style="background:{color};color:white;padding:2px 8px;'
        f'border-radius:3px;font-size:0.85em;font-weight:600">{text}</span>'
    )


def _esc(s) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


def generate_html(
    report: AuditReport,
    pipeline_log: Optional[List[str]] = None,
) -> str:
    ts = report.run_timestamp.strftime("%Y-%m-%d %H:%M UTC")
    dec_color = _DECISION_COLOR.get(report.overall_decision.value, "#586069")
    p = report.parsed_xml

    # ── Summary stats ────────────────────────────────────────────────────────
    total = len(report.check_results)
    passes = sum(1 for c in report.check_results if c.qa_status == QAStatus.PASS)
    fails  = sum(1 for c in report.check_results if c.qa_status == QAStatus.FAIL)
    unverf = sum(1 for c in report.check_results if c.qa_status.value == "Unverified")
    info   = sum(1 for c in report.check_results if c.qa_status.value == "Informational")
    na     = sum(1 for c in report.check_results if c.qa_status.value == "N/A")

    # ── Check rows ───────────────────────────────────────────────────────────
    check_rows = ""
    for c in report.check_results:
        qa_color = _STATUS_COLOR.get(c.qa_status.value, "#586069")
        icon = _STATUS_ICON.get(c.qa_status.value, "")
        bg = "#ffeef0" if c.qa_status.value == "Fail" else (
             "#fff5b1" if c.qa_status.value == "Unverified" else "#fff")

        findings_html = ""
        for f in c.findings[:8]:
            findings_html += (
                f'<div class="finding">'
                f'<b>Observed:</b> {_esc(f.observed[:250])}<br>'
                f'<b>Expected:</b> {_esc(f.expected[:200])}'
                + (f'<br><code>{_esc(f.xpath)}</code>' if f.xpath else "")
                + (f'<br><em>XML excerpt:</em> {_esc((f.excerpt_xml or "")[:150])}' if f.excerpt_xml else "")
                + (f'<br><em>Source excerpt:</em> {_esc((f.excerpt_source or "")[:150])}' if f.excerpt_source else "")
                + '</div>'
            )

        info_notes = ""
        if c.informational_notes:
            info_notes = '<div class="info-note">' + "<br>".join(
                f"ℹ️ {_esc(n[:200])}" for n in c.informational_notes[:3]
            ) + "</div>"

        lim_html = ""
        if c.verification_limitations:
            lim_html = '<div class="limitation">⚠️ ' + _esc(
                "; ".join(c.verification_limitations[:2])[:300]
            ) + "</div>"

        correction_html = ""
        if c.recommended_correction:
            correction_html = f'<div class="correction">🔧 <b>Correction:</b> {_esc(c.recommended_correction[:300])}</div>'

        check_rows += f"""
        <tr style="background:{bg}">
          <td class="num">{c.check_number}</td>
          <td><b>{_esc(c.check_name)}</b></td>
          <td>{_badge(f"{icon} {c.qa_status.value}", qa_color)}</td>
          <td class="reason">{_esc(c.reason[:350])}{correction_html}{findings_html}{info_notes}{lim_html}</td>
          <td class="routing">{_esc(c.routing.value if c.routing else "")}</td>
        </tr>"""

    # ── Metadata table ───────────────────────────────────────────────────────
    meta_fields = [
        ("Document ID",       p.doc_id),
        ("CubeBookId",        p.cube_book_id),
        ("Content Title",     p.content_title),
        ("Body Title",        p.body_title),
        ("Language",          p.language),
        ("Issuance Type",     p.issuance_type),
        ("Issue Date",        p.issue_date),
        ("IssueDateStatus",   p.issue_date_status),
        ("Issuing Body",      p.issuing_body),
        ("Country of Issue",  p.country_of_issue),
        ("Citation",          p.citation),
        ("Translation Type",  p.translation_type),
        ("Source URL",        p.url_link),
        ("Capture Date",      p.capture_date),
    ]

    def _meta_bg(val):
        if not val or val == "(blank)":
            return "background:#ffeef0"
        if str(val).upper() == "DEFAULT":
            return "background:#fff5b1"
        return ""

    meta_html = "\n".join(
        f'<tr><td><b>{k}</b></td>'
        f'<td style="{_meta_bg(v)}"><code>{_esc(v or "(blank)")}</code></td></tr>'
        for k, v in meta_fields
    )

    # ── Pipeline log ─────────────────────────────────────────────────────────
    log_html = ""
    if pipeline_log:
        clean_log = []
        for line in pipeline_log:
            escaped = _esc(line)
            escaped = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', escaped)
            clean_log.append(escaped)
        log_html = (
            '<h2>🔧 Pipeline Execution Log</h2>'
            '<div style="font-family:monospace;font-size:.85em;background:#f6f8fa;'
            'border:1px solid #e1e4e8;border-radius:4px;padding:12px;max-height:300px;'
            'overflow-y:auto;line-height:1.8">'
            + "<br>".join(clean_log)
            + "</div>"
        )

    # ── Full HTML ────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CUBE QA Report — {_esc(report.xml_filename)}</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:20px;background:#f6f8fa;color:#24292e}}
  .wrap{{max-width:1100px;margin:0 auto;background:#fff;border-radius:8px;padding:28px;box-shadow:0 1px 4px rgba(0,0,0,.12)}}
  h1{{font-size:1.5em;border-bottom:1px solid #e1e4e8;padding-bottom:10px;margin-top:0}}
  h2{{font-size:1.1em;margin-top:28px;color:#444;border-left:3px solid #0366d6;padding-left:8px}}
  .decision{{font-size:1.4em;font-weight:700;padding:14px 20px;border-radius:8px;
    background:{dec_color};color:white;display:inline-block;margin:16px 0}}
  .stats{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
  .stat{{background:#f6f8fa;border:1px solid #e1e4e8;padding:10px 18px;border-radius:8px;text-align:center;min-width:90px}}
  .stat .val{{font-size:1.6em;font-weight:700}}
  .stat .lbl{{font-size:.8em;color:#666;margin-top:2px}}
  table{{width:100%;border-collapse:collapse;font-size:.9em;margin-top:8px}}
  th{{background:#f6f8fa;border:1px solid #e1e4e8;padding:9px 10px;text-align:left}}
  td{{border:1px solid #e1e4e8;padding:8px 10px;vertical-align:top}}
  td.num{{width:32px;text-align:center;font-weight:700;font-size:1.05em}}
  td.reason{{max-width:420px;word-break:break-word}}
  td.routing{{white-space:nowrap;font-size:.88em}}
  .finding{{background:#fff8dc;border-left:3px solid #e36209;padding:5px 9px;margin:5px 0;font-size:.84em;border-radius:2px}}
  .correction{{background:#e8f4fd;border-left:3px solid #0366d6;padding:5px 9px;margin:5px 0;font-size:.84em;border-radius:2px}}
  .info-note{{color:#586069;font-size:.83em;margin-top:4px;font-style:italic}}
  .limitation{{color:#e36209;font-size:.82em;margin-top:4px}}
  .handover{{background:#f6f8fa;border:1px solid #e1e4e8;padding:14px;border-radius:4px;
    white-space:pre-wrap;font-family:monospace;font-size:.9em}}
  code{{background:#f6f8fa;padding:1px 5px;border-radius:3px;font-size:.85em}}
  @media print{{.wrap{{box-shadow:none}}}}
</style>
</head>
<body>
<div class="wrap">
  <h1>📋 CUBE New-Book QA Audit Report</h1>
  <p>
    <b>File:</b> {_esc(report.xml_filename)} &nbsp;|&nbsp;
    <b>Run:</b> {ts} &nbsp;|&nbsp;
    <b>Run ID:</b> <code>{report.run_id}</code>
  </p>
  <p><b>SHA-256:</b> <code>{report.xml_hash}</code></p>
  {f'<p><b>Source:</b> {_esc(report.source_identity)}</p>' if report.source_identity else ""}

  <div class="decision">🏁 {_esc(report.overall_decision.value)}</div>
  <p><b>Primary Route:</b> {_esc(report.primary_route.value)}
  {(' &nbsp;|&nbsp; <b>Secondary:</b> ' + _esc(", ".join(r.value for r in report.secondary_routes))) if report.secondary_routes else ""}
  </p>

  <div class="stats">
    <div class="stat"><div class="val" style="color:#22863a">{passes}</div><div class="lbl">Pass</div></div>
    <div class="stat"><div class="val" style="color:#cb2431">{fails}</div><div class="lbl">Fail</div></div>
    <div class="stat"><div class="val" style="color:#e36209">{unverf}</div><div class="lbl">Unverified</div></div>
    <div class="stat"><div class="val" style="color:#0366d6">{info}</div><div class="lbl">Info</div></div>
    <div class="stat"><div class="val" style="color:#586069">{na}</div><div class="lbl">N/A</div></div>
    <div class="stat"><div class="val">{total}</div><div class="lbl">Total</div></div>
  </div>

  <h2>Handover Note</h2>
  <div class="handover">{_esc(report.handover_note)}</div>

  <h2>Routing Explanation</h2>
  <pre style="white-space:pre-wrap;background:#f6f8fa;padding:10px;border-radius:4px;font-size:.88em;border:1px solid #e1e4e8">{_esc(report.routing_explanation)}</pre>

  <h2>Check Results (All {total})</h2>
  <table>
    <thead><tr>
      <th>#</th><th>Check</th><th>Status</th>
      <th>Reason, Evidence &amp; Correction</th><th>Route</th>
    </tr></thead>
    <tbody>{check_rows}</tbody>
  </table>

  <h2>XML Metadata</h2>
  <table>{meta_html}</table>

  {('<h2>Schema Validation</h2><p>XSD Valid: ' + str(report.schema_validation_passed) + '</p>' + ('<ul>' + ''.join(f'<li>{_esc(e)}</li>' for e in report.schema_errors[:5]) + '</ul>' if report.schema_errors else '')) if report.schema_validation_passed is not None else ''}

  {log_html}

  <hr style="margin-top:32px">
  <p style="color:#666;font-size:.82em">
    Generated by CUBE QA Tool &nbsp;|&nbsp; {ts} &nbsp;|&nbsp;
    Read-only evaluation — no RM metadata modified, no approval actions taken.
  </p>
</div>
</body>
</html>"""

    return html
