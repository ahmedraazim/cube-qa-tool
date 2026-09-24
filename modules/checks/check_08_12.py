"""
Checks 8–12:
  8. Translation sanity
  9. Styling exclusion (informational)
 10. Placeholder metadata
 11. Issuance date
 12. Final sweep
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from .. import config_loader as cfg
from ..auto_extract import extract_issuance_date as _extract_date, extract_issuing_body as _extract_body
from ..content_comparator import normalise_for_comparison
from ..models import (
    CheckFinding, CheckResult, ExecutionStatus, ParsedXML,
    QAStatus, Route, SourceDocument,
)


# ─── Check 8: Translation sanity ─────────────────────────────────────────────

def _detect_source_language_ai(source_text: str, ai_client):
    """Detect language of source content using AI. Returns (lang_code, lang_name) or None."""
    try:
        import json
        snippet = source_text[:800]
        schema = '{"language_code": "<ISO 639-1>", "language_name": "<full name>", "confidence": "<high|medium|low>"}'
        prompt = (
            "Identify the primary language of this regulatory document excerpt. "
            "Respond ONLY with JSON, no markdown: " + schema + "\n\n"
            "TEXT:\n" + snippet
        )
        msg = ai_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        data = json.loads(raw)
        return data.get("language_code", ""), data.get("language_name", "")
    except Exception:
        return None
def run_check_08(
    parsed_xml: ParsedXML,
    source_doc: Optional[SourceDocument] = None,
    translation_doc: Optional[SourceDocument] = None,
    ai_client=None,
    **_kwargs,
) -> CheckResult:
    translation_type = (parsed_xml.translation_type or "").strip()
    language = (parsed_xml.language or "").strip().lower()
    body_title = (parsed_xml.body_title or "")
    findings: list[CheckFinding] = []
    informational: list[str] = []
    limitations: list[str] = []

    # ── AI language detection from source ────────────────────────────────────
    # When source is available, detect its actual language and compare to XML declared language
    if source_doc and source_doc.text and ai_client:
        ai_lang = _detect_source_language_ai(source_doc.text[:800], ai_client)
        if ai_lang:
            detected_code, detected_name = ai_lang
            detected_norm = detected_code.lower().split("-")[0] if detected_code else ""
            declared_norm = language.split("-")[0] if language else ""
            if detected_norm and declared_norm and detected_norm != declared_norm:
                findings.append(CheckFinding(
                    xml_id=None, parent_section_id=None,
                    xpath="//metadata/Language",
                    source_location=source_doc.origin,
                    observed=(
                        f"XML declares language='{language}' but source content "
                        f"detected as '{detected_name}' ({detected_code})"
                    ),
                    expected="XML language code should match the source document language",
                    severity="error",
                ))
                informational.append(
                    f"AI language detection: source is in {detected_name} ({detected_code}), "
                    f"XML declares {language!r}. "
                    "If this is an English-only book without a native-language tab, "
                    "the language code should reflect the source language."
                )
            else:
                informational.append(
                    f"AI verified: source language ({detected_name}, {detected_code}) "
                    f"matches XML declared language '{language}'."
                )
    elif source_doc and source_doc.text:
        # langdetect fallback
        try:
            from langdetect import detect
            detected_code = detect(source_doc.text[:500])
            detected_norm = detected_code.lower().split("-")[0] if detected_code else ""
            declared_norm = language.split("-")[0] if language else ""
            if detected_norm and declared_norm and detected_norm != declared_norm:
                informational.append(
                    f"langdetect: source appears to be '{detected_code}', XML declares '{language}'. "
                    "Verify that the language metadata field is correct."
                )
            else:
                informational.append(
                    f"langdetect: source language '{detected_code}' consistent with XML '{language}'."
                )
        except Exception:
            pass

    # Determine if translation is expected
    body_non_english = sum(1 for c in body_title if ord(c) > 127) / max(len(body_title), 1) > 0.05

    if translation_type.lower() == "english" and not body_non_english and language == "en":
        return CheckResult(
            check_number=8,
            check_name="Translation Sanity",
            qa_status=QAStatus.NA if not findings else QAStatus.FAIL,
            execution_status=ExecutionStatus.COMPLETED,
            reason="Source appears to be in English. No translation sanity check required." if not findings else "Language mismatch detected.",
            findings=findings,
            informational_notes=informational,
            method="language-detection",
            coverage="metadata/Language + source content",
        )

    # English metadata with non-English body — flag as possible labeling issue
    if language == "en" and body_non_english:
        findings.append(
            CheckFinding(
                xml_id=None,
                parent_section_id=None,
                xpath="//metadata/Language",
                source_location=None,
                observed=f"Language='en' but body title appears non-English: {body_title!r}",
                expected="Language field should match body content language, with a separate English translation tab",
                severity="warning",
            )
        )
        informational.append(
            "The language label 'en' is inconsistent with non-English body content. "
            "This may mean the English translation is stored as the body instead of in a separate tab, "
            "or the Language field is incorrect."
        )

    # No translation document provided
    if translation_doc is None:
        limitations.append(
            "No translation document provided. Cannot evaluate translation quality."
        )
        # Cannot assess from title alone
        return CheckResult(
            check_number=8,
            check_name="Translation Sanity",
            qa_status=QAStatus.UNVERIFIED,
            execution_status=ExecutionStatus.COMPLETED,
            reason="Translation document not provided. Cannot assess translation sanity.",
            findings=findings,
            informational_notes=informational,
            method="none",
            coverage="none",
            verification_limitations=limitations + [
                "A title alone is never sufficient to declare a translation passed."
            ],
        )

    if not translation_doc.text:
        limitations.append("Translation document could not be extracted.")
        return CheckResult(
            check_number=8,
            check_name="Translation Sanity",
            qa_status=QAStatus.UNVERIFIED,
            execution_status=ExecutionStatus.COMPLETED,
            reason="Translation document content could not be extracted.",
            findings=findings,
            method=translation_doc.extraction_method,
            coverage="none",
            verification_limitations=limitations,
        )

    trans_text = translation_doc.text

    # Basic coherence checks
    non_ascii_ratio = sum(1 for c in trans_text[:1000] if ord(c) > 127) / max(len(trans_text[:1000]), 1)
    if non_ascii_ratio > 0.3:
        findings.append(
            CheckFinding(
                xml_id=None,
                parent_section_id=None,
                xpath="//body",
                source_location=translation_doc.origin,
                observed=f"High non-ASCII ratio in translation: {non_ascii_ratio:.0%}",
                expected="Translation should be predominantly in English",
                severity="error",
            )
        )

    # Check for garbled/placeholder text patterns
    garble_patterns = [
        r"\?\?\?",
        r"XXXXXXXXX",
        r"\[TRANSLATE\]",
        r"\[TODO\]",
        r"Lorem ipsum",
        r"lorem ipsum",
    ]
    for pat in garble_patterns:
        if re.search(pat, trans_text):
            findings.append(
                CheckFinding(
                    xml_id=None,
                    parent_section_id=None,
                    xpath="//body",
                    source_location=translation_doc.origin,
                    observed=f"Garbled/placeholder pattern found: {pat!r}",
                    expected="Coherent English translation",
                    severity="error",
                )
            )

    # AI-assisted check (if available and enabled)
    ai_assessment = None
    import os
    if ai_client is not None and os.environ.get("AI_SOURCE_ANALYSIS", "1") == "1":
        try:
            sample = trans_text[:2000]
            prompt = (
                "You are a regulatory-content QA specialist. "
                "Evaluate whether the following text is a coherent English translation "
                "of regulatory content. Report: (1) Is it coherent English? (2) Any material omissions "
                "or garbled passages? (3) Any meaning-changing errors? "
                "Be specific. Do not manufacture a Pass.\n\n"
                f"TEXT:\n{sample}"
            )
            response = ai_client.messages.create(
                model=os.environ.get("CUBE_AI_MODEL", "claude-sonnet-4-6"),
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            ai_assessment = response.content[0].text
            informational.append(f"AI translation assessment: {ai_assessment[:400]}")
        except Exception as exc:
            limitations.append(f"AI assessment failed: {exc}")

    error_findings = [f for f in findings if f.severity == "error"]
    if error_findings:
        status = QAStatus.FAIL
        reason = f"{len(error_findings)} translation quality issue(s) found."
    elif findings:
        status = QAStatus.UNVERIFIED
        reason = "Translation sanity warnings found. Manual review recommended."
    else:
        status = QAStatus.PASS if ai_assessment is None else QAStatus.UNVERIFIED
        reason = "No automated translation issues found."
        if ai_assessment:
            reason += " AI assessment available — review informational notes."
            limitations.append("AI assessment is supplementary; not an independent source.")

    return CheckResult(
        check_number=8,
        check_name="Translation Sanity",
        qa_status=status,
        execution_status=ExecutionStatus.COMPLETED,
        reason=reason,
        findings=findings,
        informational_notes=informational,
        method="text-analysis" + (" + AI" if ai_assessment else ""),
        coverage="translation document text",
        routing=Route.RESEARCH if error_findings else None,
        verification_limitations=limitations,
    )


# ─── Check 9: Styling exclusion ──────────────────────────────────────────────

def run_check_09(
    parsed_xml: ParsedXML,
    source_doc: Optional[SourceDocument] = None,
    **_kwargs,
) -> CheckResult:
    """
    Check 9 always produces Informational, never Fail.
    Styling differences must not be reported as failures.
    """
    informational: list[str] = []

    if source_doc and source_doc.text:
        informational.append(
            "Styling comparison is excluded per QA policy: bold, italics, capitalization, "
            "bullet formatting, and layout differences are not fail conditions and are not "
            "reported to providers. Only substantive content differences are evaluated."
        )
    else:
        informational.append("No source provided. Styling exclusion policy applies by default.")

    return CheckResult(
        check_number=9,
        check_name="Styling Exclusion",
        qa_status=QAStatus.INFORMATIONAL,
        execution_status=ExecutionStatus.COMPLETED,
        reason=(
            "Styling differences (bold, italic, capitalization, bullets, layout) "
            "are excluded from QA by policy and do not constitute failures."
        ),
        informational_notes=informational,
        method="policy-application",
        coverage="all",
    )


# ─── Check 10: Placeholder metadata ──────────────────────────────────────────

def run_check_10(
    parsed_xml: ParsedXML,
    **_kwargs,
) -> CheckResult:
    placeholder_vals: list[str] = cfg.get_list("checks.placeholder_metadata.placeholder_values")
    if not placeholder_vals:
        placeholder_vals = ["DEFAULT", "default", "", "N/A", "TBC", "TBD", "Unknown", "UNKNOWN"]

    findings: list[CheckFinding] = []
    informational: list[str] = []

    def _is_placeholder(val: Optional[str]) -> bool:
        if val is None or val.strip() == "":
            return True
        return val.strip() in placeholder_vals

    # Issuing body
    ib = parsed_xml.issuing_body
    ib_id = parsed_xml.issuing_body_id
    if _is_placeholder(ib):
        extra = ""
        if ib_id:
            extra = f" (UUID {ib_id} is populated but does not validate the placeholder label)"
        findings.append(
            CheckFinding(
                xml_id=None,
                parent_section_id=None,
                xpath="//metadata/issuing-body",
                source_location=None,
                observed=f"issuing-body={ib!r}{extra}",
                expected="A real, specific issuing authority name (not a placeholder or DEFAULT)",
                severity="error",
            )
        )
        informational.append(
            "The issuing authority is the government body that published the regulation, "
            "NOT the website publisher or host."
        )

    # Jurisdiction / country-of-issue
    coi = parsed_xml.country_of_issue
    coi_id = parsed_xml.country_of_issue_id
    if _is_placeholder(coi):
        extra = ""
        if coi_id:
            extra = f" (UUID {coi_id} is populated but does not validate the placeholder label)"
        findings.append(
            CheckFinding(
                xml_id=None,
                parent_section_id=None,
                xpath="//metadata/country-of-issue",
                source_location=None,
                observed=f"country-of-issue={coi!r}{extra}",
                expected="A real, specific jurisdiction value (not a placeholder or DEFAULT)",
                severity="error",
            )
        )

    if not findings:
        return CheckResult(
            check_number=10,
            check_name="Placeholder Metadata",
            qa_status=QAStatus.PASS,
            execution_status=ExecutionStatus.COMPLETED,
            reason=(
                f"Jurisdiction ({coi!r}) and Issuing Body ({ib!r}) both appear "
                "to be real, specific values."
            ),
            method="metadata-inspection",
            coverage="country-of-issue, issuing-body",
        )

    return CheckResult(
        check_number=10,
        check_name="Placeholder Metadata",
        qa_status=QAStatus.FAIL,
        execution_status=ExecutionStatus.COMPLETED,
        reason=f"{len(findings)} placeholder metadata value(s) found.",
        findings=findings,
        informational_notes=informational,
        method="metadata-inspection",
        coverage="country-of-issue, issuing-body",
        recommended_correction=(
            "Replace DEFAULT/placeholder values with real taxonomy-mapped values. "
            "Do not invent replacement IDs — use the approved taxonomy lookup."
        ),
        routing=Route.DATA_MANAGEMENT,
    )


# ─── Check 11: Issuance Date ──────────────────────────────────────────────────

def run_check_11(
    parsed_xml: ParsedXML,
    source_doc: Optional[SourceDocument] = None,
    **_kwargs,
) -> CheckResult:
    issue_date = (parsed_xml.issue_date or "").strip()
    issue_date_status = (parsed_xml.issue_date_status or "").strip()
    capture_date = (parsed_xml.capture_date or "").strip()
    findings: list[CheckFinding] = []
    informational: list[str] = []
    limitations: list[str] = []

    warning_statuses: list[str] = cfg.get_list("checks.issue_date_status.warning_values")
    if not warning_statuses:
        warning_statuses = [
            "Date Replaced By Capture Date",
            "Capture Date Used",
            "No Date Found",
            "Date Estimated",
        ]

    # ── Blank date ─────────────────────────────────────────────────────────
    if not issue_date:
        return CheckResult(
            check_number=11,
            check_name="Issuance Date",
            qa_status=QAStatus.FAIL,
            execution_status=ExecutionStatus.COMPLETED,
            reason="issue-date is blank.",
            findings=[
                CheckFinding(
                    xml_id=None,
                    parent_section_id=None,
                    xpath="//metadata/issue-date",
                    source_location=None,
                    observed="(blank)",
                    expected="The actual issuance date from the source document",
                    severity="error",
                )
            ],
            method="metadata-inspection",
            coverage="issue-date, IssueDateStatus",
            routing=Route.RESEARCH,
        )

    # ── Capture date substitution warning ────────────────────────────────
    if issue_date_status in warning_statuses:
        findings.append(
            CheckFinding(
                xml_id=None,
                parent_section_id=None,
                xpath="//metadata/IssueDateStatus",
                source_location=None,
                observed=f"IssueDateStatus={issue_date_status!r}  issue-date={issue_date!r}",
                expected="Actual issuance date from the source, with IssueDateStatus reflecting a confirmed date",
                severity="error",
            )
        )
        informational.append(
            f"IssueDateStatus={issue_date_status!r} indicates the stored date may not be "
            "the actual issuance date. A capture-date substitution is a serious error — "
            "an incorrect date reaching a customer is a real problem."
        )
        if capture_date:
            informational.append(f"Capture date: {capture_date}")
            if issue_date and capture_date:
                # Check if they're the same (strong signal of substitution)
                try:
                    id_dt = datetime.fromisoformat(issue_date.replace("Z", "+00:00"))
                    cd_dt = datetime.fromisoformat(capture_date.replace("Z", "+00:00"))
                    if id_dt.date() == cd_dt.date():
                        informational.append(
                            f"issue-date ({issue_date[:10]}) matches capture-date ({capture_date[:10]}) — "
                            "confirms capture-date substitution."
                        )
                except Exception:
                    pass

    # ── Compare with source date ──────────────────────────────────────────
    if source_doc and source_doc.text:
        from ..content_comparator import extract_dates
        source_dates = extract_dates(source_doc.text[:3000])
        if source_dates:
            informational.append(f"Dates found in source: {source_dates[:8]}")
            # Try to find the issue date in the source
            issue_date_short = issue_date[:10] if len(issue_date) >= 10 else issue_date
            found_in_source = any(issue_date_short in d for d in source_dates)
            if not found_in_source and issue_date_status not in warning_statuses:
                informational.append(
                    f"Issue date {issue_date_short!r} not directly found in source dates. "
                    "Confirm against original document."
                )
        else:
            limitations.append("No dates extracted from source — cannot confirm issuance date.")
    else:
        limitations.append("No source document available for date verification.")

    # ── Try to auto-extract date from source ─────────────────────────────
    if source_doc and source_doc.text:
        doc_num = (parsed_xml.content_number or "").strip()
        extracted = _extract_date(source_doc.text, doc_num)
        if extracted:
            iso_date, evidence = extracted
            informational.append(
                f"🗓️ Auto-extracted date from source: {iso_date!r}  \n"
                f"Evidence: '{evidence[:100]}'  \n"
                f"If correct, replace issue-date with {iso_date!r} and set IssueDateStatus to 'Confirmed'."
            )
        else:
            limitations.append("Could not extract date from source text automatically.")

    if findings:
        status = QAStatus.FAIL
        reason = (
            f"Issuance date issue: IssueDateStatus={issue_date_status!r}. "
            f"Stored date: {issue_date[:10]}."
        )
        if capture_date:
            reason += f" Capture date: {capture_date[:10]}."
    else:
        status = QAStatus.PASS if not limitations else QAStatus.UNVERIFIED
        reason = f"issue-date={issue_date[:10]!r}, IssueDateStatus={issue_date_status!r}."
        if status == QAStatus.UNVERIFIED:
            reason += " Could not confirm against source — marked Unverified."

    return CheckResult(
        check_number=11,
        check_name="Issuance Date",
        qa_status=status,
        execution_status=ExecutionStatus.COMPLETED,
        reason=reason,
        findings=findings,
        informational_notes=informational,
        method="metadata-inspection" + (" + source-date-extraction" if source_doc else ""),
        coverage="issue-date, IssueDateStatus, capture-date",
        recommended_correction=(
            "Replace the date with the actual issuance date from the source document. "
            "Never automatically substitute the effective date or capture date."
        ) if findings else None,
        routing=Route.RESEARCH if findings else None,
        verification_limitations=limitations,
    )


# ─── Check 12: Final sweep ───────────────────────────────────────────────────

def run_check_12(
    parsed_xml: ParsedXML,
    source_doc: Optional[SourceDocument] = None,
    **_kwargs,
) -> CheckResult:
    findings: list[CheckFinding] = []
    informational: list[str] = []
    limitations: list[str] = []

    # ── Images ───────────────────────────────────────────────────────────────
    xml_images = parsed_xml.images
    source_images = source_doc.images if source_doc else []

    if xml_images:
        broken_imgs = [img for img in xml_images
                       if not img.get("src") or img.get("src", "").strip() in ("", "#", "data:")]
        if broken_imgs:
            for img in broken_imgs[:5]:
                findings.append(
                    CheckFinding(
                        xml_id=img.get("id"),
                        parent_section_id=img.get("level_id"),
                        xpath=f"//img[@src='{img.get('src', '')}']",
                        source_location=None,
                        observed=f"Image with blank/broken src: alt={img.get('alt', '')!r}",
                        expected="Valid image src attribute",
                        severity="warning",
                    )
                )
        informational.append(f"XML contains {len(xml_images)} image reference(s).")
    else:
        informational.append(
            "No images found in XML. This is NOT automatically acceptable — "
            "check whether the source document contains images that should have been captured."
        )
        if source_doc and source_doc.images:
            findings.append(
                CheckFinding(
                    xml_id=None,
                    parent_section_id=None,
                    xpath="//body",
                    source_location=source_doc.origin,
                    observed=f"0 images in XML, {len(source_doc.images)} images in source",
                    expected="Images from source should appear in XML",
                    severity="warning",
                )
            )
        elif source_doc is None:
            limitations.append(
                "Cannot check for missing images without a source document."
            )

    # ── Footnotes ────────────────────────────────────────────────────────────
    xml_footnotes = parsed_xml.footnotes
    if xml_footnotes:
        informational.append(f"XML contains {len(xml_footnotes)} footnote(s).")
        # Check for broken footnote references
        xml_text = "\n".join(
            p.get("text", "")
            for s in parsed_xml.flat_sections
            for p in s.paragraphs
        )
        for fn in xml_footnotes:
            fn_id = fn.get("id", "")
            if fn_id and fn_id not in xml_text:
                findings.append(
                    CheckFinding(
                        xml_id=fn_id,
                        parent_section_id=None,
                        xpath=f"//footnote[@id='{fn_id}']",
                        source_location=None,
                        observed=f"Footnote id={fn_id!r} not referenced in body text",
                        expected="Each footnote should be referenced from body text",
                        severity="warning",
                    )
                )
    else:
        informational.append(
            "No footnotes in XML. Confirm source has no footnotes (not assumed)."
        )
        if source_doc and source_doc.footnotes:
            findings.append(
                CheckFinding(
                    xml_id=None,
                    parent_section_id=None,
                    xpath="//body",
                    source_location=source_doc.origin,
                    observed=f"0 footnotes in XML, {len(source_doc.footnotes)} in source",
                    expected="Footnotes from source should appear in XML",
                    severity="warning",
                )
            )

    # ── Missing / duplicated content ─────────────────────────────────────────
    flat = parsed_xml.flat_sections
    para_texts = [p.get("text", "") for s in flat for p in s.paragraphs]
    para_texts = [t for t in para_texts if len(t.strip()) > 30]

    # Duplication (ignore expected repeated headers)
    seen: dict[str, int] = {}
    section_titles = {s.title for s in flat if s.title}
    for pt in para_texts:
        norm = normalise_for_comparison(pt)
        if norm in section_titles:
            continue  # Expected repeated header
        if norm in seen:
            findings.append(
                CheckFinding(
                    xml_id=None,
                    parent_section_id=None,
                    xpath="//body",
                    source_location=None,
                    observed=f"Duplicated paragraph: {pt[:120]!r}",
                    expected="No accidental duplication",
                    severity="warning",
                )
            )
        else:
            seen[norm] = 1

    # Tables
    xml_tables = parsed_xml.tables
    if xml_tables:
        informational.append(f"XML contains {len(xml_tables)} table(s).")
    if source_doc and source_doc.tables and not xml_tables:
        findings.append(
            CheckFinding(
                xml_id=None,
                parent_section_id=None,
                xpath="//body",
                source_location=source_doc.origin,
                observed=f"0 tables in XML, {len(source_doc.tables)} tables in source",
                expected="Tables from source should be captured in XML",
                severity="warning",
            )
        )

    # ── Status ───────────────────────────────────────────────────────────────
    error_findings = [f for f in findings if f.severity == "error"]
    warn_findings = [f for f in findings if f.severity == "warning"]

    if error_findings:
        status = QAStatus.FAIL
        reason = f"Final sweep: {len(error_findings)} error(s), {len(warn_findings)} warning(s)."
    elif warn_findings:
        status = QAStatus.UNVERIFIED
        reason = f"Final sweep: {len(warn_findings)} warning(s) require review."
    else:
        status = QAStatus.PASS if source_doc else QAStatus.UNVERIFIED
        reason = (
            "Final sweep complete — no errors or warnings found."
            if source_doc
            else "Final sweep limited — no source document to compare against."
        )

    return CheckResult(
        check_number=12,
        check_name="Final Sweep",
        qa_status=status,
        execution_status=ExecutionStatus.COMPLETED,
        reason=reason,
        findings=findings,
        informational_notes=informational,
        method="structural-inspection" + (" + source-comparison" if source_doc else ""),
        coverage="images, footnotes, tables, duplication",
        verification_limitations=limitations,
    )
