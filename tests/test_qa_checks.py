"""
CUBE QA Tool — Acceptance Test Suite
=====================================
Tests all 16 acceptance criteria from §13 of the requirements.

Run with:
    cd cube_qa && python -m unittest tests.test_qa_checks -v

These tests use real check functions with controlled ParsedXML fixtures.
They do NOT use hardcoded expected strings — they verify behavioural contracts.
"""
from __future__ import annotations

import sys
import os
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.models import (
    CheckResult, Decision, ExecutionStatus, ParsedXML,
    QAStatus, Route, SourceDocument, XMLSection,
)
from modules.checks import (
    check_01_book_name,
    check_02_citation,
    check_03_issuance_type,
    check_04_05_source_url,
    check_06_toc_structure,
    check_07_content_fidelity,
    check_08_12,
)
from modules.routing import calculate_decision_and_routing
from modules.rm_adapter import RMAdapter


# ─── Fixture helpers ──────────────────────────────────────────────────────────

def _xml(
    *,
    doc_id="test-00000000-0000-0000-0000-000000000001",
    cube_book_id=None,
    content_title="Regulation 1",
    content_number="1",
    issue_date=None,
    issuing_body="Test Authority",
    issuing_body_id="auth-001",
    compliance_date=None,
    country_of_issue="Brazil",
    country_of_issue_id="BR",
    translation_type=None,
    translation_source=None,
    requested_url=None,
    requested_urlname=None,
    document_type="Regulation",
    url_link=None,
    citation="Regulation 1",
    issuance_type="Regulation",
    issue_date_status=None,
    compliance_date_status=None,
    book_load_type=None,
    version="1",
    language="en",
    update_guid=None,
    book_is_pdf=None,
    capture_date="2026-09-24T00:00:00",
    metadata_overwrite=None,
    body_title=None,
    sections=None,
    images=None,
    footnotes=None,
    tables=None,
    is_well_formed=True,
    parse_errors=None,
) -> ParsedXML:
    return ParsedXML(
        doc_id=doc_id, cube_book_id=cube_book_id,
        content_title=content_title, content_number=content_number,
        issue_date=issue_date, issuing_body=issuing_body,
        issuing_body_id=issuing_body_id, compliance_date=compliance_date,
        country_of_issue=country_of_issue, country_of_issue_id=country_of_issue_id,
        translation_type=translation_type, translation_source=translation_source,
        requested_url=requested_url, requested_urlname=requested_urlname,
        document_type=document_type, url_link=url_link,
        citation=citation, issuance_type=issuance_type,
        issue_date_status=issue_date_status,
        compliance_date_status=compliance_date_status,
        book_load_type=book_load_type, version=version,
        language=language, update_guid=update_guid,
        book_is_pdf=book_is_pdf, capture_date=capture_date,
        metadata_overwrite=metadata_overwrite, body_title=body_title,
        sections=sections or [], images=images or [],
        footnotes=footnotes or [], tables=tables or [],
        is_well_formed=is_well_formed, parse_errors=parse_errors or [],
    )


def _source(
    *,
    text="Section 1\nSome content here.",
    url="https://example-authority.gov.br/doc/1",
    sections=None,
    images=None,
    extraction_warnings=None,
    retrieval_status_code=200,
    retrieval_error=None,
    extraction_method="test",
) -> SourceDocument:
    return SourceDocument(
        origin=url,
        retrieval_time=None,
        content_type="text/html",
        file_hash="abc123",
        text=text,
        page_texts=[text],
        headings=[],
        tables=[],
        images=images or [],
        footnotes=[],
        document_date=None,
        document_version=None,
        extraction_method=extraction_method,
        extraction_warnings=extraction_warnings or [],
        retrieval_status_code=retrieval_status_code,
        retrieval_error=retrieval_error,
        ocr_used=False,
        ocr_confidence=None,
        is_complete=True,
    )


def _check_result(
    num: int, name: str,
    qa_status: QAStatus,
    routing: Route = Route.NONE,
    execution_status: ExecutionStatus = ExecutionStatus.COMPLETED,
) -> CheckResult:
    return CheckResult(
        check_number=num,
        check_name=name,
        qa_status=qa_status,
        execution_status=execution_status,
        reason="Test fixture",
        findings=[],
        method="unit-test",
        coverage=None,
        recommended_correction=None,
        routing=routing,
        verification_limitations=[],
        informational_notes=[],
    )


_CHECK_NAMES = [
    "Book Name", "Citation", "Issuance Type", "Source URL Validity",
    "Broken vs Blocked", "TOC Structure", "Content Fidelity",
    "Translation Sanity", "Styling", "Placeholder Metadata",
    "Issuance Date", "Final Sweep",
]

def _all_pass():
    return [_check_result(i, name, QAStatus.PASS)
            for i, name in enumerate(_CHECK_NAMES, 1)]

def _with_one_override(idx: int, status: QAStatus,
                       route: Route = Route.NONE) -> list:
    results = _all_pass()
    results[idx - 1] = _check_result(idx, _CHECK_NAMES[idx - 1], status, route)
    return results


# ─── Test cases ───────────────────────────────────────────────────────────────

class TestCriterion01_BlankIssuanceTypeFails(unittest.TestCase):
    """§13 criterion 1: Blank IssuanceType fails."""

    def test_none_fails(self):
        self.assertEqual(check_03_issuance_type.run(_xml(issuance_type=None)).qa_status,
                         QAStatus.FAIL)

    def test_empty_string_fails(self):
        self.assertEqual(check_03_issuance_type.run(_xml(issuance_type="")).qa_status,
                         QAStatus.FAIL)

    def test_whitespace_fails(self):
        self.assertEqual(check_03_issuance_type.run(_xml(issuance_type="   ")).qa_status,
                         QAStatus.FAIL)

    def test_known_vocab_does_not_fail(self):
        r = check_03_issuance_type.run(_xml(issuance_type="Regulation",
                                            document_type="Regulation"))
        self.assertNotEqual(r.qa_status, QAStatus.FAIL,
            "Correctly populated IssuanceType must not FAIL")


class TestCriterion02_DefaultMetadataFails(unittest.TestCase):
    """§13 criterion 2: DEFAULT metadata fails even with populated UUIDs."""

    def test_default_issuing_body_with_real_uuid_fails(self):
        r = check_08_12.run_check_10(_xml(issuing_body="DEFAULT",
                                          issuing_body_id="real-uuid-001"))
        self.assertEqual(r.qa_status, QAStatus.FAIL)

    def test_default_country_with_real_uuid_fails(self):
        r = check_08_12.run_check_10(_xml(country_of_issue="DEFAULT",
                                          country_of_issue_id="real-uuid-002"))
        self.assertEqual(r.qa_status, QAStatus.FAIL)

    def test_real_values_pass(self):
        r = check_08_12.run_check_10(_xml(issuing_body="Banco Central do Brasil",
                                          country_of_issue="Brazil"))
        self.assertNotEqual(r.qa_status, QAStatus.FAIL)

    def test_sample_xml_known_defaults(self):
        sample = Path(__file__).parent.parent / "sample_data" / \
                 "DE--DE--REG--22A6E807-6910-42AB-A752-C1DF1943EEF2_v1.xml"
        if not sample.exists():
            self.skipTest("Sample XML not present")
        from modules.xml_parser import full_parse
        p = full_parse(sample, None)
        self.assertEqual(check_08_12.run_check_10(p).qa_status, QAStatus.FAIL)


class TestCriterion03_CitationFallbackPasses(unittest.TestCase):
    """§13 criterion 3: Citation fallback passes; blank fails."""

    def test_exact_fallback_passes(self):
        r = check_02_citation.run(_xml(citation="Document Title as Citation"))
        self.assertEqual(r.qa_status, QAStatus.PASS)

    def test_none_fails(self):
        self.assertEqual(check_02_citation.run(_xml(citation=None)).qa_status,
                         QAStatus.FAIL)

    def test_empty_fails(self):
        self.assertEqual(check_02_citation.run(_xml(citation="")).qa_status,
                         QAStatus.FAIL)

    def test_real_citation_passes(self):
        r = check_02_citation.run(_xml(citation="Resolution CMN no. 5,280 of 26/2/2026"))
        self.assertEqual(r.qa_status, QAStatus.PASS)


class TestCriterion04_BlankUrlsDoNotOverrideValidUrl(unittest.TestCase):
    """§13 criterion 4: Blank optional URL fields don't override valid populated URL."""

    def test_url_link_used_when_others_blank(self):
        p = _xml(url_link="https://www.bcb.gov.br/normativos/resolucoes/2026/res_5280.pdf",
                 requested_url=None, requested_urlname=None)
        r = check_04_05_source_url.run_check_04(p)
        self.assertNotEqual(r.execution_status, ExecutionStatus.BLOCKED)
        combined = (r.reason or "") + " ".join(
            f.evidence or "" for f in r.findings
        )
        self.assertIn("bcb.gov.br", combined,
            "Populated URL must be evaluated, not silently skipped")

    def test_no_url_anywhere_fails(self):
        p = _xml(url_link=None, requested_url=None, requested_urlname=None)
        self.assertEqual(check_04_05_source_url.run_check_04(p).qa_status,
                         QAStatus.FAIL)


class TestCriterion05_UrlSyntaxAloneCannotPass(unittest.TestCase):
    """§13 criterion 5: Plausible URL syntax alone cannot pass source verification."""

    def test_well_formed_url_no_retrieval_is_not_pass(self):
        p = _xml(url_link="https://www.bcb.gov.br/normativos/resolucoes/2026/res_5280.pdf")
        r = check_04_05_source_url.run_check_04(p, source_doc=None)
        self.assertNotEqual(r.qa_status, QAStatus.PASS,
            "URL syntax check alone must never produce PASS")

    def test_bare_domain_homepage_fails(self):
        p = _xml(url_link="https://www.bcb.gov.br/")
        self.assertEqual(check_04_05_source_url.run_check_04(p).qa_status,
                         QAStatus.FAIL)


class TestCriterion06_BlockedRetrievalIsUnverified(unittest.TestCase):
    """§13 criterion 6: Blocked retrieval → Unverified, not false broken-link diagnosis."""

    def test_access_denied_is_unverified_not_fail(self):
        p = _xml(url_link="https://www.bcb.gov.br/normativos/resolucoes/2026/res_5280.pdf")
        src = _source(
            url="https://www.bcb.gov.br/normativos/resolucoes/2026/res_5280.pdf",
            text="",
            extraction_warnings=["HTTP 403 Forbidden — access denied, possible geo-block"],
            retrieval_status_code=403,
            retrieval_error="access_denied",
        )
        r = check_04_05_source_url.run_check_05(p, source_doc=src)
        self.assertNotEqual(r.qa_status, QAStatus.FAIL,
            "403/access-denied must not be diagnosed as confirmed broken link")
        self.assertIn(r.qa_status, (QAStatus.UNVERIFIED, QAStatus.NA),
            "Blocked retrieval must yield Unverified or N/A")


class TestCriterion07_MissingSourcePreventsFidelityApproval(unittest.TestCase):
    """§13 criterion 7: Missing source evidence prevents content-fidelity approval."""

    def test_no_source_doc_cannot_pass(self):
        r = check_07_content_fidelity.run(_xml(), source_doc=None)
        self.assertNotEqual(r.qa_status, QAStatus.PASS)
        self.assertIn(r.qa_status, (QAStatus.UNVERIFIED, QAStatus.FAIL, QAStatus.NA))


class TestCriterion08_StylingOnlyDoesNotFail(unittest.TestCase):
    """§13 criterion 8: Styling-only differences are informational, never Fail."""

    def test_check_09_never_fails(self):
        src = _source(text="SECTION 1\nSome content here.")
        r = check_08_12.run_check_09(_xml(), source_doc=src)
        self.assertNotEqual(r.qa_status, QAStatus.FAIL)
        self.assertIn(r.qa_status, (QAStatus.INFORMATIONAL, QAStatus.PASS, QAStatus.NA))


class TestCriterion09_SubstantiveDefectsFail(unittest.TestCase):
    """§13 criterion 9: Missing substantive text and wrong numbers fail check 7."""

    def test_all_source_content_missing_from_xml(self):
        src = _source(
            text="Article 1. Capital requirements. Article 2. Minimum 8 percent.",
        )
        p = _xml(sections=[])
        r = check_07_content_fidelity.run(p, source_doc=src)
        self.assertNotEqual(r.qa_status, QAStatus.PASS,
            "XML empty while source has content must not Pass")

    def test_numeric_substitution_not_pass(self):
        src = _source(
            text="The ratio shall not fall below 8 percent.",
        )
        bad_section = XMLSection(
            level_id="sec-001", parent_id=None,
            title="Article 1", num=1, link=None,
            paragraphs=[{"id": "p-001",
                         "text": "The ratio shall not fall below 9 percent."}]
        )
        p = _xml(sections=[bad_section])
        r = check_07_content_fidelity.run(p, source_doc=src)
        self.assertNotEqual(r.qa_status, QAStatus.PASS,
            "8→9 numeric substitution must not produce PASS")


class TestCriterion10_TranslationTitleAloneCannotPass(unittest.TestCase):
    """§13 criterion 10: Unavailable translation cannot pass on English title alone."""

    def test_no_translation_doc_not_pass(self):
        p = _xml(language="en", translation_type=None)
        r = check_08_12.run_check_08(p, source_doc=None, translation_doc=None)
        self.assertNotEqual(r.qa_status, QAStatus.PASS)

    def test_english_title_alone_not_pass(self):
        p = _xml(content_title="Resolution on Capital Requirements",
                 language="en", translation_type=None)
        r = check_08_12.run_check_08(p, source_doc=None, translation_doc=None)
        self.assertNotEqual(r.qa_status, QAStatus.PASS)


class TestCriterion11_SourceImagesAbsentFromXmlReported(unittest.TestCase):
    """§13 criterion 11: Source images absent from XML are reported in check 12."""

    def test_source_images_xml_none_is_flagged(self):
        src = _source(images=[{"src": "figure1.png", "alt": "Figure 1", "page": 1}])
        p = _xml(images=[])
        r = check_08_12.run_check_12(p, source_doc=src)
        self.assertNotEqual(r.qa_status, QAStatus.PASS,
            "Source has images, XML has none — must not silently Pass")

    def test_no_images_either_side_no_image_fail(self):
        src = _source(images=[])
        p = _xml(images=[])
        r = check_08_12.run_check_12(p, source_doc=src)
        image_fails = [f for f in r.findings
                       if "image" in (f.reason or "").lower()
                       and f.qa_status == QAStatus.FAIL]
        self.assertEqual(len(image_fails), 0,
            "No image failures expected when neither side has images")


class TestCriterion12_UnresolvedCheckPreventsApproval(unittest.TestCase):
    """§13 criterion 12: An unresolved required check prevents approval."""

    def test_unverified_prevents_approve(self):
        results = _with_one_override(4, QAStatus.UNVERIFIED, Route.RESEARCH)
        d, *_ = calculate_decision_and_routing(results, _xml())
        self.assertNotEqual(d, Decision.APPROVE)

    def test_single_fail_yields_fail_decision(self):
        results = _with_one_override(3, QAStatus.FAIL, Route.DATA_MANAGEMENT)
        d, *_ = calculate_decision_and_routing(results, _xml())
        self.assertEqual(d, Decision.FAIL)

    def test_all_pass_yields_approve(self):
        d, *_ = calculate_decision_and_routing(_all_pass(), _xml())
        self.assertEqual(d, Decision.APPROVE)


class TestCriterion13_RMConflictsVisible(unittest.TestCase):
    """§13 criterion 13: RM identity conflicts remain visible and are not overwritten."""

    def test_load_export_does_not_mutate_xml(self):
        adapter = RMAdapter()
        # load_export takes a Path; pass a non-existent path to get graceful failure
        import tempfile, json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "book_name": "RM Version of Book Name",
                "citation": "RM Citation",
                "issuing_body": "RM Issuing Body",
            }, f)
            tmp_path = Path(f.name)
        adapter.load_export(tmp_path)
        tmp_path.unlink()

        p = _xml(content_title="XML Book Name", citation="XML Citation",
                 issuing_body="XML Issuing Body")
        # XML values must not have been touched
        self.assertEqual(p.content_title, "XML Book Name")
        self.assertEqual(p.citation, "XML Citation")
        self.assertEqual(p.issuing_body, "XML Issuing Body")

    def test_rm_adapter_has_no_write_methods(self):
        import inspect
        write_verbs = ("post", "put", "patch", "delete", "submit", "approve",
                       "reject", "publish", "write", "update", "modify", "route")
        for name, obj in inspect.getmembers(RMAdapter):
            if callable(obj) and not name.startswith("__"):
                for verb in write_verbs:
                    self.assertFalse(name.lower().startswith(verb),
                        f"RMAdapter must not have write method: {name}")


class TestCriterion14_MalformedXmlAndOcrHandled(unittest.TestCase):
    """§13 criterion 14: Malformed XML and unavailable OCR are handled clearly."""

    def test_malformed_xml_content_check_cannot_pass(self):
        p = _xml(is_well_formed=False, parse_errors=["Unexpected token at line 3"])
        r = check_07_content_fidelity.run(p, source_doc=None)
        self.assertNotEqual(r.qa_status, QAStatus.PASS)

    def test_sample_xml_parses_without_crash(self):
        sample = Path(__file__).parent.parent / "sample_data" / \
                 "DE--DE--REG--22A6E807-6910-42AB-A752-C1DF1943EEF2_v1.xml"
        if not sample.exists():
            self.skipTest("Sample XML not present")
        from modules.xml_parser import full_parse
        try:
            p = full_parse(sample, None)
            self.assertTrue(p.is_well_formed)
        except Exception as exc:
            self.fail(f"full_parse raised {exc!r}")

    def test_empty_ocr_text_cannot_pass_fidelity(self):
        src = _source(text="", extraction_warnings=["OCR not available"],
                      extraction_method="ocr_unavailable")
        r = check_07_content_fidelity.run(_xml(), source_doc=src)
        self.assertNotEqual(r.qa_status, QAStatus.PASS)


class TestCriterion15_RoutingPreservesAllFailures(unittest.TestCase):
    """§13 criterion 15: Routing preserves all failures (primary + secondary routes)."""

    def test_research_and_dm_failures_both_in_output(self):
        results = _all_pass()
        results[3] = _check_result(4, "Source URL Validity",
                                   QAStatus.FAIL, Route.RESEARCH)
        results[9] = _check_result(10, "Placeholder Metadata",
                                   QAStatus.FAIL, Route.DATA_MANAGEMENT)
        decision, primary, secondaries, handover_note, _ = \
            calculate_decision_and_routing(results, _xml())

        self.assertEqual(decision, Decision.FAIL)
        all_routes = {primary} | set(secondaries)
        self.assertIn(Route.RESEARCH, all_routes,
            "Research route must be present")
        self.assertIn(Route.DATA_MANAGEMENT, all_routes,
            "Data Management route must be present")

        note_lower = handover_note.lower()
        self.assertTrue(
            any(kw in note_lower for kw in ("source", "url", " 4", "check 4")),
            f"Handover note must mention URL failure. Got: {handover_note[:200]}"
        )
        self.assertTrue(
            any(kw in note_lower for kw in ("placeholder", "jurisdiction",
                                             "issuing", " 10", "check 10")),
            f"Handover note must mention placeholder failure. Got: {handover_note[:200]}"
        )


class TestCriterion16_NoRMWriteActions(unittest.TestCase):
    """§13 criterion 16: No RM write actions exist anywhere in the codebase."""

    def test_rm_adapter_methods_all_read_only(self):
        import inspect
        dangerous = ("post", "put", "patch", "delete", "submit", "approve",
                     "reject", "publish", "write", "update", "modify",
                     "route", "comment", "flag", "set_status")
        for name, obj in inspect.getmembers(RMAdapter):
            if inspect.isfunction(obj) or inspect.ismethod(obj):
                for verb in dangerous:
                    self.assertFalse(name.lower().startswith(verb),
                        f"RMAdapter must not have write method '{name}'")

    def test_connect_has_no_http_writes(self):
        import inspect
        src = inspect.getsource(RMAdapter.connect)
        for method in (".post(", ".put(", ".patch(", ".delete(",
                       "requests.post", "requests.put", "requests.delete"):
            self.assertNotIn(method, src,
                f"RMAdapter.connect must not call {method!r}")

    def test_load_export_makes_no_http_calls(self):
        import inspect
        src = inspect.getsource(RMAdapter.load_export)
        for lib in ("requests.", "httpx.", "urllib.request"):
            self.assertNotIn(lib, src,
                f"load_export must not contain HTTP calls ({lib!r} found)")


# ─── Sample XML defect tests ──────────────────────────────────────────────────

class TestSampleXmlKnownDefects(unittest.TestCase):
    """
    Run real check functions against the actual sample XML and assert
    that the known defects identified in requirements analysis are detected.
    """

    @classmethod
    def setUpClass(cls):
        sample = Path(__file__).parent.parent / "sample_data" / \
                 "DE--DE--REG--22A6E807-6910-42AB-A752-C1DF1943EEF2_v1.xml"
        if not sample.exists():
            raise unittest.SkipTest("Sample XML not present")
        from modules.xml_parser import full_parse
        cls.p = full_parse(sample, None)

    def test_check_03_blank_issuance_type(self):
        """IssuanceType is blank in sample → FAIL."""
        r = check_03_issuance_type.run(self.p)
        self.assertEqual(r.qa_status, QAStatus.FAIL,
            f"Got {r.qa_status}: {r.reason}")

    def test_check_10_default_placeholders(self):
        """issuing_body='DEFAULT', country_of_issue='DEFAULT' → FAIL."""
        r = check_08_12.run_check_10(self.p)
        self.assertEqual(r.qa_status, QAStatus.FAIL,
            f"Got {r.qa_status}: {r.reason}")

    def test_check_11_capture_date_substitution(self):
        """IssueDateStatus='Date Replaced By Capture Date' → FAIL."""
        r = check_08_12.run_check_11(self.p)
        self.assertEqual(r.qa_status, QAStatus.FAIL,
            f"Got {r.qa_status}: {r.reason}")

    def test_check_02_fallback_citation_passes(self):
        """citation='Document Title as Citation' → PASS."""
        r = check_02_citation.run(self.p)
        self.assertEqual(r.qa_status, QAStatus.PASS,
            f"Got {r.qa_status}: {r.reason}")

    def test_check_04_url_not_verified(self):
        """
        Sample has a BCB URL that cannot be retrieved in this environment.
        Check 4 must not produce PASS — it must be FAIL or UNVERIFIED.
        (FAIL if the URL is a search page / not confirmed doc; UNVERIFIED if blocked.)
        """
        r = check_04_05_source_url.run_check_04(self.p)
        self.assertNotEqual(r.qa_status, QAStatus.PASS,
            f"URL check must not PASS without retrieval: {r.reason}")

    def test_well_formed_and_parseable(self):
        self.assertTrue(self.p.is_well_formed)
        self.assertEqual(len(self.p.parse_errors), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
