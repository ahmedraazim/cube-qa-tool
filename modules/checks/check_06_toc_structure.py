"""
Check 6: TOC / structure — book structure vs. source structure.

For documents with ≤ 20 sections: inspect all.
For larger: inspect first N, middle N, last N (configurable).
Record XML IDs for all inspected sections.
"""
from __future__ import annotations

from typing import List, Optional

from .. import config_loader as cfg
from ..content_comparator import align_sections, normalise_for_comparison, similarity_ratio
from ..models import (
    CheckFinding, CheckResult, ExecutionStatus, ParsedXML,
    QAStatus, Route, SourceDocument, XMLSection,
)


def _select_sections(sections: List[XMLSection], threshold: int, n_sample: int) -> List[XMLSection]:
    """Select sections to inspect."""
    total = len(sections)
    if total == 0:
        return []
    if total <= threshold:
        return list(sections)

    first = sections[:n_sample]
    last = sections[-n_sample:]
    mid_start = max(n_sample, total // 2 - n_sample // 2)
    mid_end = min(total - n_sample, mid_start + n_sample)
    middle = sections[mid_start:mid_end]

    selected = []
    seen = set()
    for s in first + middle + last:
        if s.level_id not in seen:
            selected.append(s)
            seen.add(s.level_id)
    return selected


def run(
    parsed_xml: ParsedXML,
    source_doc: Optional[SourceDocument] = None,
    **_kwargs,
) -> CheckResult:
    threshold = int(cfg.get("checks.toc_structure.full_inspect_threshold", 20))
    n_first = int(cfg.get("checks.toc_structure.sample_first", 2))
    n_middle = int(cfg.get("checks.toc_structure.sample_middle", 2))
    n_last = int(cfg.get("checks.toc_structure.sample_last", 2))
    n_sample = max(n_first, n_middle, n_last)

    flat_sections = parsed_xml.flat_sections
    total_xml_sections = len(flat_sections)

    # ── No sections in XML ───────────────────────────────────────────────────
    if total_xml_sections == 0:
        return CheckResult(
            check_number=6,
            check_name="TOC / Structure",
            qa_status=QAStatus.FAIL,
            execution_status=ExecutionStatus.COMPLETED,
            reason="No sections (level elements) found in the XML body.",
            findings=[
                CheckFinding(
                    xml_id=None,
                    parent_section_id=None,
                    xpath="//body/level",
                    source_location=None,
                    observed="0 sections",
                    expected="At least one section",
                    severity="error",
                )
            ],
            method="xml-structure-inspection",
            coverage="all",
            routing=Route.DATA_MANAGEMENT,
        )

    xml_titles = [s.title for s in flat_sections]
    sampled = _select_sections(flat_sections, threshold, n_sample)
    sampled_ids = [s.level_id for s in sampled]

    coverage_desc = (
        f"All {total_xml_sections} section(s)"
        if total_xml_sections <= threshold
        else f"{len(sampled)} of {total_xml_sections} sections sampled (first {n_first}, "
             f"middle {n_middle}, last {n_last})"
    )

    limitations: list[str] = []
    findings: list[CheckFinding] = []
    informational: list[str] = []

    # ── No source document: can only check internal XML structure ─────────────
    if source_doc is None or not source_doc.text:
        if source_doc is None:
            limitations.append("No source document provided — cannot compare structure against source.")
        else:
            limitations.append(
                f"Source content could not be extracted "
                f"({source_doc.extraction_method}): {'; '.join(source_doc.extraction_warnings)}. "
                "Cannot compare structure against source."
            )

        # Check internal consistency
        blank_title_sections = [s for s in flat_sections if not s.title.strip()]
        if blank_title_sections:
            for s in blank_title_sections[:5]:
                findings.append(
                    CheckFinding(
                        xml_id=s.level_id,
                        parent_section_id=s.parent_id,
                        xpath=s.xpath,
                        source_location=None,
                        observed="Section title is blank",
                        expected="Non-blank section title",
                        severity="warning",
                    )
                )
            informational.append(
                f"{len(blank_title_sections)} section(s) have blank titles."
            )

        return CheckResult(
            check_number=6,
            check_name="TOC / Structure",
            qa_status=QAStatus.UNVERIFIED,
            execution_status=ExecutionStatus.COMPLETED,
            reason=(
                f"XML has {total_xml_sections} section(s). "
                "Cannot compare against source — source document not available."
            ),
            findings=findings,
            informational_notes=informational,
            method="xml-internal-inspection",
            coverage=coverage_desc,
            verification_limitations=limitations,
        )

    # ── Compare against source ───────────────────────────────────────────────
    source_headings = source_doc.headings or []

    if not source_headings:
        # Try to extract headings from text
        import re
        lines = (source_doc.text or "").split("\n")
        source_headings = [
            line.strip()
            for line in lines
            if line.strip() and len(line.strip()) < 150
            and not line.strip().startswith("http")
        ][:50]
        if source_headings:
            informational.append("Headings extracted from plain text lines (HTML headings not available).")

    if not source_headings:
        return CheckResult(
            check_number=6,
            check_name="TOC / Structure",
            qa_status=QAStatus.UNVERIFIED,
            execution_status=ExecutionStatus.COMPLETED,
            reason=(
                f"XML has {total_xml_sections} section(s), but no headings could be extracted "
                "from the source document to compare."
            ),
            method="source-heading-extraction-failed",
            coverage=coverage_desc,
            verification_limitations=[
                "Source headings could not be extracted. Manual comparison required."
            ],
        )

    # Perform alignment on sampled sections only
    sampled_titles = [s.title for s in sampled]
    alignments = align_sections(source_headings[:len(sampled) + 10], sampled_titles)

    unmatched_source = [(si, sh) for si, xi, sim in alignments if xi is None
                        for si2, sh in [(si, source_headings[si])] if si2 == si]
    unmatched_xml = [(xi, sampled_titles[xi]) for si, xi, sim in alignments
                     if si is None and xi is not None]
    low_similarity = [(si, xi, sim) for si, xi, sim in alignments
                      if si is not None and xi is not None and sim < 0.6]

    for si, source_heading in unmatched_source:
        findings.append(
            CheckFinding(
                xml_id=None,
                parent_section_id=None,
                xpath="//body",
                source_location=f"Source heading #{si+1}",
                observed=f"Source heading {source_heading!r} has no matching XML section",
                expected="Matching XML section",
                severity="error",
            )
        )

    for xi, xml_title in unmatched_xml:
        sec = sampled[xi]
        findings.append(
            CheckFinding(
                xml_id=sec.level_id,
                parent_section_id=sec.parent_id,
                xpath=sec.xpath,
                source_location=None,
                observed=f"XML section {xml_title!r} has no matching source heading",
                expected="Matching source heading",
                severity="warning",
            )
        )

    for si, xi, sim in low_similarity:
        sec = sampled[xi]
        findings.append(
            CheckFinding(
                xml_id=sec.level_id,
                parent_section_id=sec.parent_id,
                xpath=sec.xpath,
                source_location=f"Source heading #{si+1}",
                observed=f"Low similarity ({sim:.0%}): source={source_headings[si]!r} xml={sampled_titles[xi]!r}",
                expected="High-similarity match (>60%)",
                severity="warning",
            )
        )

    if findings:
        status = QAStatus.FAIL
        reason = (
            f"Structure discrepancies found between XML ({total_xml_sections} sections) "
            f"and source ({len(source_headings)} headings)."
        )
    else:
        matched_count = len([a for a in alignments if a[0] is not None and a[1] is not None])
        status = QAStatus.PASS if matched_count > 0 else QAStatus.UNVERIFIED
        reason = (
            f"Structure comparison: {matched_count} of {len(sampled)} sampled sections matched source. "
            f"XML total: {total_xml_sections} sections."
        )

    if total_xml_sections > threshold:
        limitations.append(
            f"Only {len(sampled)} of {total_xml_sections} sections were compared. "
            "Full automated comparison not performed on sections not in the sample."
        )

    return CheckResult(
        check_number=6,
        check_name="TOC / Structure",
        qa_status=status,
        execution_status=ExecutionStatus.COMPLETED,
        reason=reason,
        findings=findings,
        informational_notes=informational,
        method="section-alignment",
        coverage=coverage_desc,
        routing=Route.DATA_MANAGEMENT if findings else None,
        verification_limitations=limitations,
    )
