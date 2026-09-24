"""
Decision and routing logic.

Rules:
- APPROVE: All required checks satisfactorily verified, no confirmed failures.
- FAIL: At least one confirmed failure.
- HOLD: No confirmed failure, but required verification incomplete.
- BLOCKED: Invalid/unreadable input.

Routing:
- Research: wrong/broken/unconfirmed source, missing native tab, translation issues, wrong date.
- Data Management: placeholder jurisdiction/issuing body, structural/TOC problems, issuance type.
- Select dominant or first-blocking issue as primary route.
- Do NOT hide secondary failures.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Tuple

from .models import (
    AuditReport, CheckResult, Decision, ExecutionStatus,
    ParsedXML, QAStatus, Route,
)

# Which checks route to Research vs Data Management (per checklist)
_RESEARCH_CHECKS = {4, 5, 7, 8, 11}       # Source URL, fidelity, translation, date
_DATA_MGMT_CHECKS = {1, 3, 6, 10}          # Book name, issuance type, structure, placeholder
# Check 2 (citation) → Data Management
_DATA_MGMT_CHECKS.add(2)
# Check 12 (final sweep) → depends on finding type, default Research
_RESEARCH_CHECKS.add(12)


def _check_routes_to(check: CheckResult) -> Route:
    """Return the route for a given failing check."""
    if check.routing:
        return check.routing
    if check.check_number in _DATA_MGMT_CHECKS:
        return Route.DATA_MANAGEMENT
    return Route.RESEARCH


def calculate_decision_and_routing(
    check_results: List[CheckResult],
    parsed_xml: ParsedXML,
) -> Tuple[Decision, Route, List[Route], str, str]:
    """
    Returns (decision, primary_route, secondary_routes, explanation, handover_note).
    """

    confirmed_failures = [r for r in check_results if r.is_confirmed_failure]
    unresolved = [
        r for r in check_results
        if r.qa_status == QAStatus.UNVERIFIED
        or r.execution_status in (ExecutionStatus.BLOCKED, ExecutionStatus.FAILED_TO_EXECUTE)
    ]
    all_blocked = all(
        r.execution_status == ExecutionStatus.BLOCKED for r in check_results
    )

    # ── BLOCKED ──────────────────────────────────────────────────────────────
    if all_blocked:
        return (
            Decision.BLOCKED,
            Route.NONE,
            [],
            "All checks were blocked due to an invalid or unreadable input.",
            "All checks blocked — invalid XML input. Correct the XML and re-run.",
        )

    # ── FAIL ─────────────────────────────────────────────────────────────────
    if confirmed_failures:
        routes_used = [_check_routes_to(f) for f in confirmed_failures]
        research_failures = [f for f in confirmed_failures if _check_routes_to(f) == Route.RESEARCH]
        dm_failures = [f for f in confirmed_failures if _check_routes_to(f) == Route.DATA_MANAGEMENT]

        # Primary route: first blocking check
        first_failure = confirmed_failures[0]
        primary = _check_routes_to(first_failure)

        secondary_set = set(routes_used) - {primary}
        secondary = [r for r in [Route.RESEARCH, Route.DATA_MANAGEMENT] if r in secondary_set]

        explanation_parts = [
            f"FAIL — {len(confirmed_failures)} confirmed failure(s)."
        ]
        for f in confirmed_failures:
            explanation_parts.append(
                f"• Check {f.check_number} ({f.check_name}): {f.reason[:120]}"
            )
        if unresolved:
            explanation_parts.append(f"Also {len(unresolved)} unresolved check(s):")
            for u in unresolved[:3]:
                explanation_parts.append(f"  ↳ Check {u.check_number}: {u.reason[:80]}")

        explanation = "\n".join(explanation_parts)

        # Handover note
        fail_summaries = "; ".join(
            f"Check {f.check_number} ({f.check_name}): {f.reason[:80]}"
            for f in confirmed_failures
        )
        handover = (
            f"FAILING — {len(confirmed_failures)} confirmed failure(s): {fail_summaries}. "
            f"Primary route: {primary.value}."
        )
        if unresolved:
            handover += (
                f" Additionally, {len(unresolved)} check(s) could not be fully verified "
                "and require attention before resubmission."
            )

        return Decision.FAIL, primary, secondary, explanation, handover

    # ── HOLD ─────────────────────────────────────────────────────────────────
    if unresolved:
        # Determine most significant unresolved
        research_unresolved = [r for r in unresolved if _check_routes_to(r) == Route.RESEARCH]
        dm_unresolved = [r for r in unresolved if _check_routes_to(r) == Route.DATA_MANAGEMENT]

        primary = Route.RESEARCH if research_unresolved else Route.DATA_MANAGEMENT
        secondary = [Route.DATA_MANAGEMENT] if research_unresolved and dm_unresolved else []

        explanation = (
            f"HOLD — no confirmed failures, but {len(unresolved)} check(s) unresolved:\n"
            + "\n".join(
                f"• Check {u.check_number} ({u.check_name}): {u.qa_status.value} — {u.reason[:100]}"
                for u in unresolved
            )
        )
        handover = (
            f"HOLD — verification incomplete on {len(unresolved)} check(s). "
            "Cannot approve until all required checks are resolved. "
            "Route to " + primary.value + " for resolution."
        )
        return Decision.HOLD, primary, secondary, explanation, handover

    # ── APPROVE ───────────────────────────────────────────────────────────────
    passing = [r for r in check_results if r.qa_status in (QAStatus.PASS, QAStatus.NA, QAStatus.INFORMATIONAL)]
    explanation = (
        f"APPROVE — all {len(passing)} applicable checks pass or are N/A. "
        "No confirmed failures and no unresolved required checks."
    )
    handover = (
        "READY TO PUBLISH — all QA checks pass. "
        f"Approved on run {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}."
    )

    return Decision.APPROVE, Route.NONE, [], explanation, handover


def _datetime_utcnow():
    from datetime import datetime
    return datetime.utcnow()


def _datetime_utcnow():
    from datetime import datetime
    return datetime.utcnow()
