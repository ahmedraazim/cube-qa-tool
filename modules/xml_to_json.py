"""
XML → JSON conversion and hierarchical node comparison for CUBE QA.
"""
from __future__ import annotations
import re
import html as _html
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from .models import ParsedXML, XMLSection


# ─── XML → JSON ──────────────────────────────────────────────────────────────

def section_to_dict(sec: XMLSection) -> Dict[str, Any]:
    return {
        "id": sec.level_id,
        "title": sec.title,
        "depth": sec.depth,
        "paragraphs": [
            {"id": p.get("id", ""), "text": p.get("text", "").strip()}
            for p in sec.paragraphs if p.get("text", "").strip()
        ],
        "children": [section_to_dict(c) for c in sec.children],
    }


def parsed_xml_to_json(p: ParsedXML) -> Dict[str, Any]:
    return {
        "metadata": {
            "doc_id": p.doc_id,
            "cube_book_id": p.cube_book_id,
            "content_title": p.content_title,
            "content_number": p.content_number,
            "language": p.language,
            "issuance_type": p.issuance_type,
            "issue_date": p.issue_date,
            "issue_date_status": p.issue_date_status,
            "capture_date": p.capture_date,
            "issuing_body": p.issuing_body,
            "issuing_body_id": p.issuing_body_id,
            "country_of_issue": p.country_of_issue,
            "country_of_issue_id": p.country_of_issue_id,
            "citation": p.citation,
            "url_link": p.url_link,
            "requested_url": p.requested_url,
            "requested_urlname": p.requested_urlname,
            "document_type": p.document_type,
            "translation_type": p.translation_type,
            "book_load_type": p.book_load_type,
            "book_is_pdf": p.book_is_pdf,
            "update_guid": p.update_guid,
        },
        "body": {
            "title": p.body_title,
            "sections": [section_to_dict(s) for s in p.sections],
            "total_sections": len(p.flat_sections),
            "images": p.images,
            "tables": len(p.tables),
            "footnotes": len(p.footnotes),
        },
        "validation": {
            "is_well_formed": p.is_well_formed,
            "xsd_valid": p.xsd_valid,
            "parse_errors": p.parse_errors,
            "xsd_errors": p.xsd_errors,
        },
    }


# ─── XML node tree (hierarchical) ────────────────────────────────────────────

def xml_node_tree(p: ParsedXML) -> List[Dict[str, Any]]:
    """
    Return XML body as a hierarchical list of nodes:
    [{level, title, depth, paragraphs:[text], children:[...]}]
    Root title (body_title) is inserted as depth=0 if present.
    """
    def _node(sec: XMLSection) -> Dict[str, Any]:
        return {
            "id": sec.level_id,
            "title": sec.title or "",
            "depth": sec.depth,
            "paragraphs": [
                _clean(p.get("text", ""))
                for p in sec.paragraphs if p.get("text", "").strip()
            ],
            "children": [_node(c) for c in sec.children],
            "xpath": f"//level[@id='{sec.level_id}']",
        }

    tree = [_node(s) for s in p.sections]

    # Insert body_title as a virtual root node
    if p.body_title:
        tree = [{
            "id": "body-title",
            "title": p.body_title,
            "depth": -1,
            "paragraphs": [],
            "children": tree,
            "xpath": "//body/title",
        }]

    return tree


def flat_xml_nodes(p: ParsedXML) -> List[Dict[str, Any]]:
    """Flat list of every paragraph node for the comparison table."""
    rows = []
    for sec in p.flat_sections:
        for para in sec.paragraphs:
            text = _clean(para.get("text", ""))
            if not text:
                continue
            rows.append({
                "section_id": sec.level_id,
                "section_title": sec.title or "(no title)",
                "depth": sec.depth,
                "para_id": para.get("id", ""),
                "text": text,
                "xpath": f"//level[@id='{sec.level_id}']",
            })
    return rows


def _clean(text: str) -> str:
    """Unescape HTML entities, strip non-breaking spaces."""
    if not text:
        return ""
    text = _html.unescape(text)
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\u200c", "")
    text = re.sub(r" {2,}", " ", text).strip()
    return text


# ─── Source content → hierarchical nodes ─────────────────────────────────────

def source_to_hierarchy(source_text: str, headings: List[str]) -> List[Dict[str, Any]]:
    """
    Parse source text into a hierarchy: title → sections → paragraphs.
    Returns [{title, depth, paragraphs:[str], children:[...]}]
    """
    if not source_text:
        return []

    lines = [l.strip() for l in source_text.splitlines() if l.strip()]

    # Build heading set for fast lookup
    heading_set = set(h.lower() for h in headings)

    def _is_heading(line: str) -> bool:
        if line.lower() in heading_set:
            return True
        # Short lines that look like titles/articles
        if len(line) < 100 and re.match(
            r'^(art\.|article|chapter|section|part|annex|título|capítulo|seção|\d+[\.\):])',
            line, re.IGNORECASE
        ):
            return True
        return False

    # Group lines into (heading, [paragraphs]) pairs
    sections: List[Tuple[str, List[str]]] = []
    current_heading = lines[0] if lines else "Document"
    current_paras: List[str] = []
    para_buf: List[str] = []

    for line in lines[1:]:
        if _is_heading(line):
            # flush current paragraph buffer
            if para_buf:
                current_paras.append(" ".join(para_buf))
                para_buf = []
            sections.append((current_heading, current_paras))
            current_heading = line
            current_paras = []
        elif len(line) < 20:
            # likely a short label, skip
            continue
        else:
            para_buf.append(line)
            # flush paragraph at sentence boundaries or long lines
            if line.endswith(".") or line.endswith("。") or len(" ".join(para_buf)) > 400:
                current_paras.append(" ".join(para_buf))
                para_buf = []

    if para_buf:
        current_paras.append(" ".join(para_buf))
    sections.append((current_heading, current_paras))

    # Build tree: first section is root
    if not sections:
        return []

    root_title, root_paras = sections[0]
    children = [
        {
            "title": t,
            "depth": 1,
            "paragraphs": ps,
            "children": [],
        }
        for t, ps in sections[1:]
        if t or ps
    ]

    return [{
        "title": root_title,
        "depth": 0,
        "paragraphs": root_paras,
        "children": children,
    }]


def source_to_nodes(source_text: str, headings: List[str]) -> List[Dict[str, Any]]:
    """Flat list of source paragraphs with section attribution."""
    rows = []
    hierarchy = source_to_hierarchy(source_text, headings)

    def _walk(nodes, depth=0):
        for node in nodes:
            section = node["title"]
            for para in node["paragraphs"]:
                if para.strip():
                    rows.append({
                        "section": section,
                        "depth": depth,
                        "para_num": len(rows) + 1,
                        "text": para.strip(),
                    })
            _walk(node.get("children", []), depth + 1)

    _walk(hierarchy)
    return rows


# ─── Hierarchical comparison ──────────────────────────────────────────────────

def build_comparison_tree(
    parsed_xml: ParsedXML,
    source_text: str,
    source_headings: List[str],
) -> List[Dict[str, Any]]:
    """
    Build a hierarchical comparison: for each XML node, find the best
    matching source content and report Match / Partial / Missing.

    Returns list of rows ready for display.
    """
    xml_nodes = flat_xml_nodes(parsed_xml)
    src_nodes = source_to_nodes(source_text, source_headings)

    src_texts = [n["text"] for n in src_nodes]

    def _sim(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        a_n = a.lower()[:300]
        b_n = b.lower()[:300]
        return SequenceMatcher(None, a_n, b_n).ratio()

    rows = []
    for xn in xml_nodes:
        xtext = xn["text"]
        best_sim = 0.0
        best_src = None
        for sn in src_nodes:
            s = _sim(xtext, sn["text"])
            if s > best_sim:
                best_sim = s
                best_src = sn

        if best_sim >= 0.75:
            status = "✅ Match"
        elif best_sim >= 0.40:
            status = "⚠️ Partial"
        else:
            status = "❌ Missing / Changed"

        rows.append({
            "Depth": xn["depth"],
            "XML Section": xn["section_title"],
            "XML Text": xtext[:180],
            "Source Section": best_src["section"][:60] if best_src else "—",
            "Source Text": best_src["text"][:180] if best_src else "—",
            "Similarity": f"{best_sim:.0%}",
            "Status": status,
        })

    # Unmatched source nodes
    unmatched = []
    xml_texts = [n["text"] for n in xml_nodes]
    for sn in src_nodes:
        best = max((_sim(sn["text"], x) for x in xml_texts), default=0.0)
        if best < 0.35:
            unmatched.append(sn)

    return rows, unmatched
