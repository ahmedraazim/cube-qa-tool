"""
Check 3: Issuance Type.

- Must be populated.
- Should match the document type described in the title/content.
- Uses configurable approved vocabulary.
- Fallback when nothing fits: "Notice".
- Does NOT use document-type as a substitute.
"""
from __future__ import annotations

import re
from typing import Optional

from .. import config_loader as cfg
from ..auto_extract import extract_issuance_type as _extract_type
from ..models import CheckFinding, CheckResult, ExecutionStatus, ParsedXML, QAStatus, Route, SourceDocument


def _find_best_match(title: str, vocab: list[str]) -> Optional[str]:
    """Look for approved vocabulary terms inside the document title."""
    title_lower = title.lower()
    for term in vocab:
        if term.lower() in title_lower:
            return term
    return None


def run(parsed_xml: ParsedXML, source_doc=None, **_kwargs) -> CheckResult:
    issuance_type = (parsed_xml.issuance_type or "").strip()
    doc_type = (parsed_xml.document_type or "").strip()
    content_title = (parsed_xml.content_title or "").strip()
    body_title = (parsed_xml.body_title or "").strip()

    vocab: list[str] = cfg.get_list("checks.issuance_type.approved_vocabulary")
    if not vocab:
        vocab = ["Regulation", "Directive", "Guidance", "Notice", "Circular",
                 "Decision", "Order", "Rule", "Resolution", "Standard"]
    fallback = cfg.get("checks.issuance_type.fallback", "Notice")
    doc_type_mapping: dict = cfg.get("checks.issuance_type.document_type_mapping") or {}

    # ── Step 1: blank check ──────────────────────────────────────────────────
    if not issuance_type:
        # Try to suggest what it should be
        combined_title = f"{content_title} {body_title}"
        suggested = _find_best_match(combined_title, vocab)

        # Try source content extraction
        src_text = (source_doc.text if source_doc else None) or ""
        src_url  = (source_doc.origin if source_doc else None) or ""
        src_result = _extract_type(content_title, body_title, src_text, src_url)
        if not suggested and src_result:
            suggested = src_result[0]
        if not suggested and doc_type:
            suggested = doc_type_mapping.get(doc_type)
        if not suggested:
            suggested = fallback

        note = (
            f"Auto-suggested type: {suggested!r} "
            + (f"(extracted from source content)" if src_result else "(from title analysis)")
            + " — verify against approved vocabulary."
        )

        return CheckResult(
            check_number=3,
            check_name="Issuance Type",
            qa_status=QAStatus.FAIL,
            execution_status=ExecutionStatus.COMPLETED,
            reason="IssuanceType is blank.",
            findings=[
                CheckFinding(
                    xml_id=None,
                    parent_section_id=None,
                    xpath="//metadata/IssuanceType",
                    source_location=None,
                    observed="(blank)",
                    expected=f"An approved issuance type, e.g. {suggested!r}",
                    severity="error",
                )
            ],
            method="metadata-inspection + title-analysis",
            coverage="IssuanceType field + content-title",
            recommended_correction=(
                f"Set IssuanceType to an approved value. Suggested: {suggested!r}. "
                f"If no type fits, the accepted fallback is {fallback!r}."
            ),
            routing=Route.DATA_MANAGEMENT,
            informational_notes=[note],
            verification_limitations=[
                "Suggested mapping is based on title text analysis only. "
                "A human reviewer must confirm against the permitted vocabulary."
            ],
        )

    # ── Step 2: vocabulary check ─────────────────────────────────────────────
    vocab_lower = [v.lower() for v in vocab]
    is_in_vocab = issuance_type.lower() in vocab_lower

    # ── Step 3: title-match check ────────────────────────────────────────────
    combined_title = f"{content_title} {body_title}"
    title_suggested = _find_best_match(combined_title, vocab)
    doc_type_suggested = doc_type_mapping.get(doc_type) if doc_type else None

    findings: list[CheckFinding] = []
    informational: list[str] = []

    if not is_in_vocab:
        findings.append(
            CheckFinding(
                xml_id=None,
                parent_section_id=None,
                xpath="//metadata/IssuanceType",
                source_location=None,
                observed=f"IssuanceType: {issuance_type!r}",
                expected=f"One of the approved vocabulary: {vocab[:8]}...",
                severity="error",
            )
        )

    # Check for mismatch between assigned type and title evidence
    mismatch_note = None
    if title_suggested and title_suggested.lower() != issuance_type.lower():
        mismatch_note = (
            f"Title analysis suggests {title_suggested!r} but IssuanceType is {issuance_type!r}. "
            "Confirm which is correct."
        )
        informational.append(mismatch_note)
    elif doc_type_suggested and doc_type_suggested.lower() != issuance_type.lower():
        informational.append(
            f"document-type field ({doc_type!r}) maps to {doc_type_suggested!r} "
            f"but IssuanceType is {issuance_type!r}."
        )

    if findings:
        return CheckResult(
            check_number=3,
            check_name="Issuance Type",
            qa_status=QAStatus.FAIL,
            execution_status=ExecutionStatus.COMPLETED,
            reason=f"IssuanceType {issuance_type!r} is not in the approved vocabulary.",
            findings=findings,
            informational_notes=informational,
            method="vocabulary-check + title-analysis",
            coverage="IssuanceType field",
            recommended_correction=f"Replace with an approved type. If nothing fits, use {fallback!r}.",
            routing=Route.DATA_MANAGEMENT,
        )

    reason = f"IssuanceType {issuance_type!r} is present and in the approved vocabulary."
    if mismatch_note:
        reason += f" Note: {mismatch_note}"

    status = QAStatus.PASS if not mismatch_note else QAStatus.UNVERIFIED

    return CheckResult(
        check_number=3,
        check_name="Issuance Type",
        qa_status=status,
        execution_status=ExecutionStatus.COMPLETED,
        reason=reason,
        informational_notes=informational,
        method="vocabulary-check + title-analysis",
        coverage="IssuanceType field",
        verification_limitations=[
            "Title-based type suggestion is informational only. Confirmed match requires "
            "human review of the source document."
        ] if mismatch_note else [],
    )
