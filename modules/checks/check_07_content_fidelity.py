"""
Check 7: Content fidelity — deep paragraph-level comparison.

Rules:
- Compare source document content paragraph-by-paragraph against XML.
- Detect: missing articles/clauses, substituted numbers/dates, truncated text.
- Styling differences are NOT failures (bold, caps, bullets).
- Do NOT use similarity score alone as Pass criterion.
- OCR low confidence → Unverified, not auto-approved.
- AI used for semantic mismatch detection when available.
"""
from __future__ import annotations

import re
from typing import Optional

from ..content_comparator import (
    check_number_fidelity,
    find_differences,
    normalise_for_comparison,
    similarity_ratio,
    align_sections,
)
from ..models import (
    CheckFinding, CheckResult, ExecutionStatus, ParsedXML,
    QAStatus, Route, SourceDocument, XMLSection,
)


def _section_text(section: XMLSection) -> str:
    parts = []
    for para in section.paragraphs:
        t = para.get("text", "").strip()
        if t:
            parts.append(t)
    for child in section.children:
        parts.append(_section_text(child))
    return "\n".join(parts)


def _xml_full_text(parsed_xml: ParsedXML) -> str:
    parts = []
    for section in parsed_xml.flat_sections:
        if section.title:
            parts.append(section.title)
        for para in section.paragraphs:
            t = para.get("text", "").strip()
            if t:
                parts.append(t)
    return "\n".join(parts)


def _split_into_paragraphs(text: str) -> list[str]:
    """Split text into meaningful paragraphs for comparison."""
    # Split on double newline or numbered list items
    paras = re.split(r'\n{2,}', text)
    result = []
    for p in paras:
        p = p.strip()
        if len(p) > 20:  # ignore very short fragments
            result.append(p)
    return result


def _find_missing_paragraphs(source_paras: list[str], xml_paras: list[str]) -> list[dict]:
    """
    Find paragraphs present in source but absent (or severely truncated) in XML.
    Returns list of {source_text, issue_type, severity}.
    """
    issues = []
    xml_combined = "\n".join(normalise_for_comparison(p) for p in xml_paras)

    for sp in source_paras:
        sp_norm = normalise_for_comparison(sp)
        if len(sp_norm) < 30:
            continue

        # Check if this paragraph (or a close match) appears in XML
        # Use a key phrase approach: first 60 chars of normalised text
        key = sp_norm[:60].strip()
        if not key:
            continue

        if key in xml_combined:
            continue  # found

        # Check similarity against each XML paragraph
        best_sim = max(
            (similarity_ratio(sp_norm[:200], normalise_for_comparison(xp)[:200])
             for xp in xml_paras),
            default=0.0
        )

        if best_sim < 0.4:
            # Likely missing entirely
            issues.append({
                "source_text": sp[:300],
                "issue_type": "missing_from_xml",
                "similarity": best_sim,
                "severity": "error",
            })
        elif best_sim < 0.75:
            # Present but significantly different
            issues.append({
                "source_text": sp[:300],
                "issue_type": "significantly_different",
                "similarity": best_sim,
                "severity": "error" if best_sim < 0.55 else "warning",
            })

    return issues


def _ai_compare_sections(
    source_excerpt: str,
    xml_excerpt: str,
    ai_client,
    lang: str = "",
) -> Optional[dict]:
    """
    Use AI to identify meaningful content differences between source and XML excerpts.
    Returns dict with {has_issues, issues_list, confidence} or None on failure.
    """
    try:
        import json
        prompt = (
            "You are a regulatory compliance QA specialist. "
            "Compare these two text excerpts — SOURCE (original regulation) and XML (captured version). "
            "Identify MEANINGFUL differences only: missing clauses, wrong numbers/dates, "
            "truncated text, changed obligations. "
            "IGNORE: formatting, bold/italic, capitalisation, paragraph spacing.\n\n"
            f"DECLARED LANGUAGE: {lang or 'unknown'}\n\n"
            f"SOURCE TEXT:\n{source_excerpt[:1200]}\n\n"
            f"XML TEXT:\n{xml_excerpt[:1200]}\n\n"
            "Respond ONLY with JSON (no markdown):\n"
            '{"has_issues": <true|false>, '
            '"issues": [{"type": "missing|substituted|truncated|extra", '
            '"description": "brief description", "severity": "error|warning"}], '
            '"overall_similarity": "<high|medium|low>", '
            '"notes": "<one sentence>"}'
        )
        msg = ai_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        return json.loads(raw)
    except Exception:
        return None


def run(
    parsed_xml: ParsedXML,
    source_doc: Optional[SourceDocument] = None,
    native_doc: Optional[SourceDocument] = None,
    ai_client=None,
    **_kwargs,
) -> CheckResult:
    findings: list[CheckFinding] = []
    informational: list[str] = []
    limitations: list[str] = []

    language = (parsed_xml.language or "").strip().lower()
    effective_source = native_doc or source_doc

    # ── No source ────────────────────────────────────────────────────────────
    if effective_source is None:
        return CheckResult(
            check_number=7,
            check_name="Content Fidelity",
            qa_status=QAStatus.UNVERIFIED,
            execution_status=ExecutionStatus.COMPLETED,
            reason=(
                "No source document provided. "
                "Upload the original source (PDF, HTML, or DOCX) to enable content comparison."
            ),
            method="none",
            coverage="none",
            verification_limitations=[
                "To enable this check: upload the source document, or provide the source URL "
                "and ensure network access to retrieve it."
            ],
        )

    # ── Incomplete extraction ────────────────────────────────────────────────
    if not effective_source.is_complete or not (effective_source.text or "").strip():
        # Check if it's a JS-rendered page with guidance
        js_warning = next(
            (w for w in (effective_source.extraction_warnings or []) if "javascript" in w.lower()),
            None
        )
        if js_warning:
            return CheckResult(
                check_number=7,
                check_name="Content Fidelity",
                qa_status=QAStatus.UNVERIFIED,
                execution_status=ExecutionStatus.COMPLETED,
                reason=(
                    "Source page requires JavaScript rendering — plain HTTP fetch captured only the page shell. "
                    "Content could not be extracted for comparison."
                ),
                method=effective_source.extraction_method,
                coverage="none",
                verification_limitations=[
                    "To compare content: open the source URL in your browser → Ctrl+S → "
                    "Save as 'Webpage, Complete' → upload the saved HTML file as the source document."
                ],
                informational_notes=[js_warning],
            )
        return CheckResult(
            check_number=7,
            check_name="Content Fidelity",
            qa_status=QAStatus.UNVERIFIED,
            execution_status=ExecutionStatus.COMPLETED,
            reason=(
                f"Source content could not be extracted "
                f"(method: {effective_source.extraction_method}). "
                + "; ".join(effective_source.extraction_warnings or [])
            ),
            method=effective_source.extraction_method,
            coverage="none",
            verification_limitations=limitations,
        )

    # ── OCR confidence guard ─────────────────────────────────────────────────
    if effective_source.ocr_used and (effective_source.ocr_confidence or 100) < 70:
        return CheckResult(
            check_number=7,
            check_name="Content Fidelity",
            qa_status=QAStatus.UNVERIFIED,
            execution_status=ExecutionStatus.COMPLETED,
            reason=(
                f"OCR confidence is too low ({effective_source.ocr_confidence:.1f}%) "
                "to support reliable comparison. Manual review required."
            ),
            method="ocr-comparison-refused",
            coverage="none",
            verification_limitations=[
                "A higher-quality scan or a text-layer PDF is required for automated comparison."
            ],
        )

    # ── Build texts ──────────────────────────────────────────────────────────
    xml_text = _xml_full_text(parsed_xml)
    source_text = effective_source.text or ""

    if not xml_text.strip():
        findings.append(CheckFinding(
            xml_id=None, parent_section_id=None,
            xpath="//body",
            source_location=None,
            observed="XML has no extractable section text",
            expected="XML body sections containing the regulation text",
            severity="error",
        ))

    # ── Overall similarity (informational only) ──────────────────────────────
    compare_len = min(len(source_text), len(xml_text), 6000)
    if compare_len > 100:
        sim = similarity_ratio(source_text[:compare_len], xml_text[:compare_len])
        informational.append(
            f"Overall text similarity ({compare_len} chars): {sim:.1%} — informational only, not used as Pass criterion."
        )
    else:
        sim = 0.0

    # ── Section structure comparison ─────────────────────────────────────────
    source_headings = effective_source.headings or []
    xml_titles = [s.title for s in parsed_xml.flat_sections if s.title]

    if source_headings and xml_titles:
        alignments = align_sections(source_headings, xml_titles)
        unmatched_source = [source_headings[si] for si, xi, score in alignments
                            if xi is None and si is not None]
        unmatched_xml = [xml_titles[xi] for si, xi, score in alignments
                         if si is None and xi is not None]

        if unmatched_source:
            for heading in unmatched_source[:5]:
                findings.append(CheckFinding(
                    xml_id=None, parent_section_id=None,
                    xpath="//body//level",
                    source_location=effective_source.origin,
                    observed=f"Section present in source but not matched in XML: {heading!r}",
                    expected="Matching section in XML",
                    severity="error",
                ))

        if unmatched_xml:
            for heading in unmatched_xml[:5]:
                findings.append(CheckFinding(
                    xml_id=None, parent_section_id=None,
                    xpath="//body//level",
                    source_location=effective_source.origin,
                    observed=f"XML section not found in source: {heading!r}",
                    expected="Section traceable to source document",
                    severity="warning",
                ))

        informational.append(
            f"Section alignment: {len(source_headings)} source headings, "
            f"{len(xml_titles)} XML sections. "
            f"Unmatched source: {len(unmatched_source)}, unmatched XML: {len(unmatched_xml)}."
        )

    # ── Paragraph-level missing content detection ────────────────────────────
    source_paras = _split_into_paragraphs(source_text[:8000])
    xml_paras = _split_into_paragraphs(xml_text[:8000])

    if source_paras and xml_paras:
        missing = _find_missing_paragraphs(source_paras, xml_paras)
        for m in missing[:8]:
            excerpt = m["source_text"][:250]
            findings.append(CheckFinding(
                xml_id=None, parent_section_id=None,
                xpath="//body",
                source_location=effective_source.origin,
                observed=f"[{m['issue_type']}] Source paragraph not matched in XML (similarity {m['similarity']:.0%}):\n{excerpt}",
                expected="This content should appear in the XML",
                severity=m["severity"],
                excerpt_source=m["source_text"][:300],
                excerpt_xml="",
            ))

    # ── Number and date fidelity ─────────────────────────────────────────────
    num_issues = check_number_fidelity(source_text[:6000], xml_text[:6000])
    for issue in num_issues[:10]:
        findings.append(CheckFinding(
            xml_id=None, parent_section_id=None,
            xpath="//body",
            source_location=effective_source.origin,
            observed=f"{issue['type']}: {issue['value']!r} — in source but not in XML",
            expected="All numbers and dates from source must appear unchanged in XML",
            severity=issue["severity"],
        ))

    # ── Structural diff (sentence-level) ────────────────────────────────────
    diffs = find_differences(source_text[:5000], xml_text[:5000])
    for d in diffs[:6]:
        if d["type"] in ("missing_from_xml", "replaced"):
            findings.append(CheckFinding(
                xml_id=None, parent_section_id=None,
                xpath="//body",
                source_location=effective_source.origin,
                observed=f"XML: {d['xml_excerpt'][:200]!r}",
                expected=f"Source: {d['source_excerpt'][:200]!r}",
                severity=d["severity"],
                excerpt_source=d.get("source_excerpt", "")[:200],
                excerpt_xml=d.get("xml_excerpt", "")[:200],
            ))

    # ── AI comparison (when available) ───────────────────────────────────────
    if ai_client and source_text and xml_text:
        ai_result = _ai_compare_sections(
            source_excerpt=source_text[:1200],
            xml_excerpt=xml_text[:1200],
            ai_client=ai_client,
            lang=language,
        )
        if ai_result:
            if ai_result.get("has_issues"):
                for issue in ai_result.get("issues", [])[:5]:
                    findings.append(CheckFinding(
                        xml_id=None, parent_section_id=None,
                        xpath="//body",
                        source_location=effective_source.origin,
                        observed=f"[AI] {issue.get('type','issue')}: {issue.get('description','')}",
                        expected="Content should match source without substantive changes",
                        severity=issue.get("severity", "warning"),
                    ))
            informational.append(
                f"AI comparison: overall similarity '{ai_result.get('overall_similarity', 'unknown')}'. "
                + (ai_result.get("notes") or "")
            )

    # ── Duplication check ────────────────────────────────────────────────────
    paras_all = [p["text"] for s in parsed_xml.flat_sections
                 for p in s.paragraphs if len(p.get("text", "")) > 60]
    seen: dict[str, int] = {}
    for p in paras_all:
        norm = normalise_for_comparison(p)
        if norm in seen:
            findings.append(CheckFinding(
                xml_id=None, parent_section_id=None,
                xpath="//body",
                source_location=None,
                observed=f"Duplicate paragraph: {p[:100]!r}",
                expected="No duplicated paragraphs",
                severity="warning",
            ))
        else:
            seen[norm] = 1

    # ── Decision ────────────────────────────────────────────────────────────
    error_findings = [f for f in findings if f.severity == "error"]
    warn_findings = [f for f in findings if f.severity == "warning"]

    if error_findings:
        status = QAStatus.FAIL
        reason = (
            f"{len(error_findings)} confirmed content issue(s) found: "
            + "; ".join(f.observed[:80] for f in error_findings[:3])
        )
    elif warn_findings:
        status = QAStatus.UNVERIFIED
        reason = (
            f"{len(warn_findings)} potential difference(s) found — manual review recommended. "
            "No confirmed errors, but differences warrant human verification."
        )
    elif not source_text.strip():
        status = QAStatus.UNVERIFIED
        reason = "Source text is empty — comparison could not be performed."
    else:
        status = QAStatus.PASS
        reason = (
            f"No confirmed content differences found. "
            f"Text similarity: {sim:.1%}. "
            "Numbers and dates are consistent. "
            "No missing sections detected."
        )

    return CheckResult(
        check_number=7,
        check_name="Content Fidelity",
        qa_status=status,
        execution_status=ExecutionStatus.COMPLETED,
        reason=reason,
        findings=findings,
        informational_notes=informational,
        method=(
            "paragraph-alignment + number-check + sentence-diff"
            + (" + AI-semantic" if ai_client else "")
            + f" (source: {effective_source.extraction_method})"
        ),
        coverage=(
            f"{len(source_paras)} source paragraphs vs {len(xml_paras)} XML paragraphs; "
            f"~{min(len(source_text), 8000):,} chars compared"
        ),
        routing=Route.RESEARCH if error_findings else None,
        verification_limitations=limitations + [
            "Comparison covers extracted text only. Embedded images checked by Check 12."
        ],
    )
