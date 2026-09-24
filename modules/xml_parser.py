"""
XML parsing and XSD validation for CUBE book XML files.

Security:
- External entity resolution is disabled (no XXE).
- Network access during parsing is disabled.
- File size is bounded before parsing begins.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lxml import etree

from .models import ParsedXML, XMLSection

MAX_XML_BYTES = 100 * 1024 * 1024  # 100 MB sanity guard


def _make_safe_parser() -> etree.XMLParser:
    """Return an lxml parser with external entity resolution disabled."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        recover=False,
    )


def parse_xml(xml_source: str | bytes | Path) -> Tuple[Optional[etree._Element], List[str]]:
    """
    Parse XML from a string, bytes, or file path.
    Returns (root_element, error_list).
    If parsing fails, root_element is None.
    """
    errors: List[str] = []
    parser = _make_safe_parser()

    try:
        if isinstance(xml_source, Path):
            size = xml_source.stat().st_size
            if size > MAX_XML_BYTES:
                return None, [f"XML file too large: {size} bytes (limit {MAX_XML_BYTES})"]
            with open(xml_source, "rb") as fh:
                data = fh.read()
        elif isinstance(xml_source, str):
            data = xml_source.encode("utf-8")
        else:
            data = xml_source

        if len(data) > MAX_XML_BYTES:
            return None, [f"XML data too large: {len(data)} bytes"]

        root = etree.fromstring(data, parser=parser)
        return root, []
    except etree.XMLSyntaxError as exc:
        errors.append(f"XML syntax error: {exc}")
        # Try recovery parse for partial extraction
        try:
            recovery_parser = etree.XMLParser(
                resolve_entities=False,
                no_network=True,
                recover=True,
            )
            root = etree.fromstring(data, parser=recovery_parser)
            errors.append("Parsed with recovery mode — content may be incomplete.")
            return root, errors
        except Exception:
            return None, errors
    except Exception as exc:
        return None, [f"XML parse error: {exc}"]


def validate_xsd(root: etree._Element, xsd_source: str | bytes | Path) -> Tuple[bool, List[str]]:
    """
    Validate an already-parsed XML element against an XSD schema.
    Returns (is_valid, error_messages).
    """
    errors: List[str] = []
    try:
        if isinstance(xsd_source, Path):
            with open(xsd_source, "rb") as fh:
                xsd_data = fh.read()
        elif isinstance(xsd_source, str):
            xsd_data = xsd_source.encode("utf-8")
        else:
            xsd_data = xsd_source

        xsd_doc = etree.fromstring(xsd_data, parser=_make_safe_parser())
        schema = etree.XMLSchema(xsd_doc)
        is_valid = schema.validate(root)
        if not is_valid:
            for err in schema.error_log:
                errors.append(f"Line {err.line}: {err.message}")
        return is_valid, errors
    except etree.XMLSchemaParseError as exc:
        return False, [f"XSD schema error: {exc}"]
    except Exception as exc:
        return False, [f"XSD validation error: {exc}"]


def _text(el: Optional[etree._Element], default: str = "") -> str:
    if el is None:
        return default
    return (el.text or "").strip()


def _extract_inline_text(el: etree._Element) -> str:
    """Extract all text content from an element including nested tags. Decode HTML entities."""
    import html as _html
    text = "".join(el.itertext()).strip()
    # Decode HTML entities that appear as literal text (e.g. &nbsp; -> space)
    text = _html.unescape(text)
    # Replace non-breaking spaces and zero-width chars with regular space
    text = text.replace(" ", " ").replace("​", "").replace("‌", "")
    # Collapse multiple spaces
    import re as _re
    text = _re.sub(r" {2,}", " ", text).strip()
    return text


def _parse_sections(
    level_elements: List[etree._Element],
    parent_id: Optional[str],
    depth: int,
) -> List[XMLSection]:
    sections = []
    for level_el in level_elements:
        level_id = level_el.get("id", "")
        num_str = level_el.get("num", "0")
        try:
            num = int(num_str)
        except (ValueError, TypeError):
            num = 0
        link = level_el.get("link")

        # Title
        title_el = level_el.find("title")
        title_text = _extract_inline_text(title_el).strip() if title_el is not None else ""

        # Paragraphs from html children
        paragraphs: List[Dict[str, Any]] = []
        for html_el in level_el.findall("html"):
            for child in html_el:
                para_id = child.get("id", "")
                text = _extract_inline_text(child).strip()
                tag = child.tag
                if text or para_id:
                    paragraphs.append({"id": para_id, "tag": tag, "text": text})

        # Tables
        tables: List[Dict[str, Any]] = []
        for html_el in level_el.findall("html"):
            for tbl_el in html_el.findall(".//table"):
                rows = []
                for tr in tbl_el.findall(".//tr"):
                    cells = [_extract_inline_text(td) for td in tr.findall(".//td") + tr.findall(".//th")]
                    rows.append(cells)
                tables.append({"level_id": level_id, "rows": rows})

        # Images within this level
        images: List[Dict[str, Any]] = []
        for html_el in level_el.findall("html"):
            for img_el in html_el.findall(".//img"):
                images.append({
                    "level_id": level_id,
                    "src": img_el.get("src", ""),
                    "alt": img_el.get("alt", ""),
                    "id": img_el.get("id", ""),
                })

        # Footnotes
        footnotes: List[Dict[str, Any]] = []
        for fn_el in level_el.findall(".//footnote"):
            footnotes.append({
                "id": fn_el.get("id", ""),
                "text": _extract_inline_text(fn_el),
            })

        # Recurse into child levels
        child_levels = level_el.findall("level")
        children = _parse_sections(child_levels, level_id, depth + 1)

        section = XMLSection(
            level_id=level_id,
            parent_id=parent_id,
            title=title_text,
            num=num,
            link=link,
            paragraphs=paragraphs,
            children=children,
            depth=depth,
        )
        sections.append(section)
    return sections


def extract_parsed_xml(root: etree._Element, raw_xml: str = "") -> ParsedXML:
    """Build a ParsedXML from a parsed lxml root element."""
    meta = root.find("metadata")
    body = root.find("body")

    def mt(tag: str) -> Optional[str]:
        if meta is None:
            return None
        el = meta.find(tag)
        if el is None:
            return None
        return (el.text or "").strip() or None

    def mt_attr(tag: str, attr: str) -> Optional[str]:
        if meta is None:
            return None
        el = meta.find(tag)
        if el is None:
            return None
        return el.get(attr)

    # Extract all images from the body
    all_images: List[Dict[str, Any]] = []
    all_footnotes: List[Dict[str, Any]] = []
    all_tables: List[Dict[str, Any]] = []

    if body is not None:
        for img_el in body.findall(".//img"):
            all_images.append({
                "src": img_el.get("src", ""),
                "alt": img_el.get("alt", ""),
                "id": img_el.get("id", ""),
                "level_id": None,
            })
        for fn_el in body.findall(".//footnote"):
            all_footnotes.append({
                "id": fn_el.get("id", ""),
                "text": _extract_inline_text(fn_el),
            })
        for tbl_el in body.findall(".//table"):
            rows = []
            for tr in tbl_el.findall(".//tr"):
                cells = [_extract_inline_text(td) for td in tr.findall(".//td") + tr.findall(".//th")]
                rows.append(cells)
            if rows:
                all_tables.append({"rows": rows})

    body_title = ""
    if body is not None:
        body_title_el = body.find("title")
        if body_title_el is not None:
            body_title = _extract_inline_text(body_title_el).strip()

    sections: List[XMLSection] = []
    if body is not None:
        top_levels = body.findall("level")
        sections = _parse_sections(top_levels, None, 0)

    cube_book_id = mt("CubeBookId")
    doc_id = root.get("id", "")

    return ParsedXML(
        doc_id=doc_id,
        cube_book_id=cube_book_id,
        content_title=mt("content-title"),
        content_number=mt("content-number"),
        issue_date=mt("issue-date"),
        issuing_body=mt("issuing-body"),
        issuing_body_id=mt_attr("issuing-body", "id"),
        compliance_date=mt("compliance-date"),
        country_of_issue=mt("country-of-issue"),
        country_of_issue_id=mt_attr("country-of-issue", "id"),
        translation_type=mt("translation-type"),
        translation_source=mt("translation-source"),
        requested_url=mt("requested-url"),
        requested_urlname=mt("requested-urlname"),
        document_type=mt("document-type"),
        url_link=mt("url-link"),
        citation=mt("Citation"),
        issuance_type=mt("IssuanceType"),
        issue_date_status=mt("IssueDateStatus"),
        compliance_date_status=mt("ComplianceDateStatus"),
        book_load_type=mt("BookLoadType"),
        version=None,  # handled below
        language=mt("Language"),
        update_guid=mt("UpdateGUID"),
        book_is_pdf=mt("BookIsPdf"),
        capture_date=mt("capture-date"),
        metadata_overwrite=mt("MetadataOverwrite"),
        body_title=body_title,
        sections=sections,
        images=all_images,
        footnotes=all_footnotes,
        tables=all_tables,
        raw_xml=raw_xml,
        is_well_formed=True,
    )


def full_parse(
    xml_source: str | bytes | Path,
    xsd_source: Optional[str | bytes | Path] = None,
) -> ParsedXML:
    """
    Parse XML and optionally validate against XSD.
    Always returns a ParsedXML; parse errors are recorded inside it.
    """
    raw_xml = ""
    if isinstance(xml_source, Path):
        try:
            raw_xml = xml_source.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw_xml = ""
    elif isinstance(xml_source, str):
        raw_xml = xml_source
    elif isinstance(xml_source, bytes):
        raw_xml = xml_source.decode("utf-8", errors="replace")

    root, parse_errors = parse_xml(xml_source)
    is_well_formed = len([e for e in parse_errors if "syntax error" in e.lower() or "parse error" in e.lower()]) == 0

    if root is None:
        # Return stub so downstream can report Blocked
        return ParsedXML(
            doc_id="",
            cube_book_id=None,
            content_title=None,
            content_number=None,
            issue_date=None,
            issuing_body=None,
            issuing_body_id=None,
            compliance_date=None,
            country_of_issue=None,
            country_of_issue_id=None,
            translation_type=None,
            translation_source=None,
            requested_url=None,
            requested_urlname=None,
            document_type=None,
            url_link=None,
            citation=None,
            issuance_type=None,
            issue_date_status=None,
            compliance_date_status=None,
            book_load_type=None,
            version=None,
            language=None,
            update_guid=None,
            book_is_pdf=None,
            capture_date=None,
            metadata_overwrite=None,
            body_title=None,
            raw_xml=raw_xml,
            is_well_formed=False,
            parse_errors=parse_errors,
        )

    parsed = extract_parsed_xml(root, raw_xml)
    parsed.is_well_formed = is_well_formed
    parsed.parse_errors = parse_errors

    # Version element
    if root.find("metadata") is not None:
        ver_el = root.find("metadata/version")
        if ver_el is not None:
            parsed.version = (ver_el.text or "").strip()

    # XSD validation
    if xsd_source is not None:
        xsd_valid, xsd_errors = validate_xsd(root, xsd_source)
        parsed.xsd_valid = xsd_valid
        parsed.xsd_errors = xsd_errors

    return parsed
