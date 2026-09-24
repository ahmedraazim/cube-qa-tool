"""
Check 2: Citation — must be filled in.
Fallback value "Document Title as Citation" is accepted.
"""
from __future__ import annotations

from ..models import CheckFinding, CheckResult, ExecutionStatus, ParsedXML, QAStatus, Route

ACCEPTED_FALLBACK = "Document Title as Citation"


def run(parsed_xml: ParsedXML, **_kwargs) -> CheckResult:
    citation = (parsed_xml.citation or "").strip()

    if not citation:
        return CheckResult(
            check_number=2,
            check_name="Citation",
            qa_status=QAStatus.FAIL,
            execution_status=ExecutionStatus.COMPLETED,
            reason="Citation field is blank. Must be populated or set to the accepted fallback.",
            findings=[
                CheckFinding(
                    xml_id=None,
                    parent_section_id=None,
                    xpath="//metadata/Citation",
                    source_location=None,
                    observed="(blank)",
                    expected=f'A citation string or the accepted fallback: "{ACCEPTED_FALLBACK}"',
                    severity="error",
                )
            ],
            method="metadata-inspection",
            coverage="Citation field",
            recommended_correction=f'Populate Citation field, or set it to "{ACCEPTED_FALLBACK}" if no specific citation is available.',
            routing=Route.DATA_MANAGEMENT,
        )

    if citation == ACCEPTED_FALLBACK:
        return CheckResult(
            check_number=2,
            check_name="Citation",
            qa_status=QAStatus.PASS,
            execution_status=ExecutionStatus.COMPLETED,
            reason=f'Citation contains the accepted fallback value: "{ACCEPTED_FALLBACK}".',
            method="metadata-inspection",
            coverage="Citation field",
        )

    return CheckResult(
        check_number=2,
        check_name="Citation",
        qa_status=QAStatus.PASS,
        execution_status=ExecutionStatus.COMPLETED,
        reason=f'Citation is populated: "{citation[:120]}".',
        method="metadata-inspection",
        coverage="Citation field",
    )
