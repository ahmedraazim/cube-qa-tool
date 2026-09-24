"""
High-end content comparison engine.

Strategy:
1. Smart source parsing — detect articles/sections by text pattern, not HTML tags
2. Multi-strategy matching — exact, number-anchor, key-phrase, semantic
3. Word-level diff — highlight exactly which words differ
4. Coverage report — % of source captured in XML
"""
from __future__ import annotations

import html as _html
import re
import unicodedata
from difflib import SequenceMatcher, ndiff
from typing import Any, Dict, List, Optional, Tuple

from .models import ParsedXML


# ─── Text utilities ───────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    text = _html.unescape(text or "")
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\u200c", "")
    text = re.sub(r" {2,}", " ", text).strip()
    return text


def _norm(text: str) -> str:
    """Normalise for comparison: NFC, lower, collapse whitespace."""
    text = unicodedata.normalize("NFC", _clean(text)).lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _norm(a)[:400], _norm(b)[:400]).ratio()


def _extract_numbers(text: str) -> set:
    return set(re.findall(r"\b\d[\d.,]*\b", text))


# ─── Article/section pattern detection ───────────────────────────────────────
# Covers Portuguese, English, Arabic, French article markers

_ARTICLE_PATTERNS = [
    # Portuguese
    r"^(Art\.?\s*\d+[°ºª]?\.?\s)",
    r"^(Artigo\s+\d+[°ºª]?\.?\s)",
    r"^(§\s*\d+[°ºª]?\.?\s)",
    # English
    r"^(Article\s+\d+\.?\s)",
    r"^(Section\s+\d+\.?\s)",
    r"^(Chapter\s+\d+\.?\s)",
    r"^(Part\s+[IVX\d]+\.?\s)",
    r"^(Clause\s+\d+\.?\s)",
    # Arabic
    r"^(المادة\s+\d+)",
    r"^(الفصل\s+\d+)",
    # French
    r"^(Article\s+\d+\s)",
    r"^(Chapitre\s+\d+\s)",
    # Numbered lists
    r"^(\d+\.\s+[A-ZÀ-Ú])",
    r"^([IVXLCDM]+\.\s+[A-ZÀ-Ú])",
]

_HEADING_PATTERNS = [
    r"^R\s*E\s*S\s*O\s*L\s*V\s*[EEU]",  # RESOLVEU / RESOLVE
    r"^CONSIDERANDO",
    r"^WHEREAS",
    r"^PREAMBLE",
    r"^DEFINITIONS?",
    r"^DISPOSIÇÕES?\s+(GERAIS|FINAIS|TRANSITÓRIAS)",
    r"^GENERAL\s+PROVISIONS?",
]


def _is_article_marker(line: str) -> Optional[str]:
    """Return the article marker text if this line starts a new article, else None."""
    line = line.strip()
    for pat in _ARTICLE_PATTERNS + _HEADING_PATTERNS:
        m = re.match(pat, line, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None


def _article_number(marker: str) -> Optional[str]:
    """Extract numeric/roman portion from an article marker."""
    m = re.search(r"\d+", marker)
    return m.group(0) if m else None


# ─── Smart source parser ──────────────────────────────────────────────────────

class SourceBlock:
    """One logical block from the source: a heading + its paragraphs."""
    def __init__(self, marker: str, number: Optional[str], text: str, index: int):
        self.marker = marker          # e.g. "Art. 1º"
        self.number = number          # e.g. "1"
        self.text = text              # full text of the block
        self.index = index            # position in document
        self.matched_xml_id: Optional[str] = None
        self.match_score: float = 0.0
        self.match_strategy: str = ""
        self.diff_html: str = ""

    def __repr__(self):
        return f"SourceBlock({self.marker!r}, sim={self.match_score:.0%})"


def parse_source_blocks(source_text: str) -> List[SourceBlock]:
    """
    Split source text into logical blocks using article/section markers.
    Each block = one article or section and all its paragraph text.
    Handles flat text where no HTML headings are present.
    """
    if not source_text:
        return []

    lines = [_clean(l) for l in source_text.splitlines()]
    lines = [l for l in lines if l]

    blocks: List[SourceBlock] = []
    current_marker = "PREAMBLE"
    current_number = None
    current_lines: List[str] = []
    idx = 0

    for line in lines:
        marker = _is_article_marker(line)
        if marker:
            # Save current block
            if current_lines:
                full_text = " ".join(current_lines)
                if len(full_text) > 15:
                    blocks.append(SourceBlock(
                        marker=current_marker,
                        number=current_number,
                        text=full_text,
                        index=idx,
                    ))
                    idx += 1
            current_marker = line
            current_number = _article_number(marker)
            current_lines = [line]
        else:
            if len(line) > 5:
                current_lines.append(line)

    # Final block
    if current_lines:
        full_text = " ".join(current_lines)
        if len(full_text) > 15:
            blocks.append(SourceBlock(
                marker=current_marker,
                number=current_number,
                text=full_text,
                index=idx,
            ))

    # If no article markers found, treat each paragraph as its own block
    if len(blocks) <= 1 and source_text.strip():
        paras = [p.strip() for p in re.split(r'\n{2,}', source_text) if len(p.strip()) > 20]
        if len(paras) > len(blocks):
            blocks = [
                SourceBlock(marker=f"Para {i+1}", number=str(i+1), text=_clean(p), index=i)
                for i, p in enumerate(paras)
            ]

    return blocks


# ─── XML block extractor ──────────────────────────────────────────────────────

class XMLBlock:
    def __init__(self, section_id: str, title: str, depth: int,
                 text: str, xpath: str, number: Optional[str] = None):
        self.section_id = section_id
        self.title = _clean(title)
        self.depth = depth
        self.text = _clean(text)
        self.xpath = xpath
        self.number = number or _article_number(title)

    def __repr__(self):
        return f"XMLBlock({self.title!r})"


def extract_xml_blocks(parsed_xml: ParsedXML) -> List[XMLBlock]:
    """Extract XML sections as flat blocks with their full text."""
    blocks = []
    for sec in parsed_xml.flat_sections:
        para_texts = [
            _clean(p.get("text", ""))
            for p in sec.paragraphs if p.get("text", "").strip()
        ]
        full_text = _clean(sec.title or "") + " " + " ".join(para_texts)
        if full_text.strip():
            blocks.append(XMLBlock(
                section_id=sec.level_id,
                title=sec.title or "",
                depth=sec.depth,
                text=full_text.strip(),
                xpath=f"//level[@id='{sec.level_id}']",
            ))
    return blocks


# ─── Word-level diff HTML ─────────────────────────────────────────────────────

def _word_diff_html(source_text: str, xml_text: str) -> Tuple[str, str]:
    """
    Generate word-level diff HTML for side-by-side display.
    Returns (source_html, xml_html).
    Red = in source but not XML. Green = in XML but not source.
    """
    src_words = _norm(source_text).split()
    xml_words = _norm(xml_text).split()

    src_html_parts = []
    xml_html_parts = []

    matcher = SequenceMatcher(None, src_words, xml_words, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        src_chunk = " ".join(src_words[i1:i2])
        xml_chunk = " ".join(xml_words[j1:j2])
        if op == "equal":
            src_html_parts.append(src_chunk)
            xml_html_parts.append(xml_chunk)
        elif op == "replace":
            if src_chunk:
                src_html_parts.append(
                    f'<span style="background:#ffd7d7;border-radius:2px;padding:1px 2px">{src_chunk}</span>'
                )
            if xml_chunk:
                xml_html_parts.append(
                    f'<span style="background:#d4f4d4;border-radius:2px;padding:1px 2px">{xml_chunk}</span>'
                )
        elif op == "delete":
            if src_chunk:
                src_html_parts.append(
                    f'<span style="background:#ffd7d7;border-radius:2px;padding:1px 2px;text-decoration:line-through">{src_chunk}</span>'
                )
        elif op == "insert":
            if xml_chunk:
                xml_html_parts.append(
                    f'<span style="background:#d4f4d4;border-radius:2px;padding:1px 2px">{xml_chunk}</span>'
                )

    return " ".join(src_html_parts), " ".join(xml_html_parts)


# ─── Multi-strategy matching ──────────────────────────────────────────────────

def _match_number(src: SourceBlock, xml: XMLBlock) -> float:
    """Boost if article numbers match."""
    if src.number and xml.number and src.number == xml.number:
        return 0.5
    return 0.0


def _match_key_phrase(src: SourceBlock, xml: XMLBlock) -> float:
    """Match on first 80 chars of content."""
    a = _norm(src.text)[:80]
    b = _norm(xml.text)[:80]
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _match_numbers_in_text(src: SourceBlock, xml: XMLBlock) -> float:
    """Penalty if source has key numbers not in XML."""
    src_nums = _extract_numbers(src.text)
    xml_nums = _extract_numbers(xml.text)
    if not src_nums:
        return 0.0
    overlap = src_nums & xml_nums
    return len(overlap) / len(src_nums)


def match_blocks(
    src_blocks: List[SourceBlock],
    xml_blocks: List[XMLBlock],
) -> List[Dict[str, Any]]:
    """
    Match each XML block to the best source block using multiple strategies.
    Returns comparison rows for display.
    """
    used_src: set = set()
    results = []

    for xb in xml_blocks:
        best_score = 0.0
        best_src = None
        best_strategy = ""

        for i, sb in enumerate(src_blocks):
            # Strategy 1: full text similarity
            sim = _sim(sb.text, xb.text)

            # Strategy 2: number anchor boost
            num_boost = _match_number(sb, xb)

            # Strategy 3: key-phrase match
            kp = _match_key_phrase(sb, xb)

            # Strategy 4: number-in-text overlap
            num_overlap = _match_numbers_in_text(sb, xb)

            # Combined score
            combined = (
                sim * 0.5 +
                num_boost * 0.2 +
                kp * 0.2 +
                num_overlap * 0.1
            )

            if combined > best_score:
                best_score = combined
                best_src = (i, sb)
                if num_boost > 0:
                    best_strategy = f"article-number + similarity ({sim:.0%})"
                elif kp > 0.6:
                    best_strategy = f"key-phrase ({kp:.0%})"
                else:
                    best_strategy = f"text-similarity ({sim:.0%})"

        # Decide status
        if best_score >= 0.65:
            status = "✅ Match"
        elif best_score >= 0.35:
            status = "⚠️ Partial"
        else:
            status = "❌ Missing / Changed"

        src_text = best_src[1].text if best_src else ""
        src_marker = best_src[1].marker if best_src else "—"

        # Word diff
        src_diff_html = xml_diff_html = ""
        if best_src and best_score >= 0.20:
            src_diff_html, xml_diff_html = _word_diff_html(src_text, xb.text)

        # Number mismatches (only if matched)
        num_issues = []
        if best_src and best_score >= 0.35:
            src_nums = _extract_numbers(src_text)
            xml_nums = _extract_numbers(xb.text)
            missing_nums = src_nums - xml_nums
            extra_nums = xml_nums - src_nums
            if missing_nums:
                num_issues.append(f"Numbers in source missing from XML: {sorted(missing_nums)[:5]}")
            if extra_nums:
                num_issues.append(f"Extra numbers in XML not in source: {sorted(extra_nums)[:5]}")

        results.append({
            "xml_section": xb.title or xb.section_id,
            "xml_text": xb.text[:300],
            "xml_xpath": xb.xpath,
            "xml_depth": xb.depth,
            "xml_number": xb.number or "",
            "src_marker": src_marker,
            "src_text": src_text[:300],
            "score": best_score,
            "status": status,
            "strategy": best_strategy,
            "src_diff_html": src_diff_html,
            "xml_diff_html": xml_diff_html,
            "num_issues": num_issues,
        })

        if best_src:
            used_src.add(best_src[0])

    # Source blocks with no XML match
    unmatched_src = [sb for i, sb in enumerate(src_blocks) if i not in used_src]

    return results, unmatched_src


# ─── Coverage report ──────────────────────────────────────────────────────────

def coverage_report(
    match_results: List[Dict],
    src_blocks: List[SourceBlock],
    xml_blocks: List[XMLBlock],
) -> Dict[str, Any]:
    """Compute coverage statistics."""
    total_xml = len(xml_blocks)
    matched = sum(1 for r in match_results if "✅" in r["status"])
    partial = sum(1 for r in match_results if "⚠️" in r["status"])
    missing = sum(1 for r in match_results if "❌" in r["status"])

    total_src = len(src_blocks)
    src_xml_texts = [xb.text for xb in xml_blocks]
    src_covered = sum(
        1 for sb in src_blocks
        if max((_sim(sb.text, x) for x in src_xml_texts), default=0) >= 0.35
    )

    all_src_nums = set()
    all_xml_nums = set()
    for sb in src_blocks:
        all_src_nums |= _extract_numbers(sb.text)
    for xb in xml_blocks:
        all_xml_nums |= _extract_numbers(xb.text)

    missing_nums = all_src_nums - all_xml_nums

    return {
        "total_xml_nodes": total_xml,
        "xml_matched": matched,
        "xml_partial": partial,
        "xml_missing": missing,
        "xml_match_pct": int(matched / total_xml * 100) if total_xml else 0,
        "total_src_blocks": total_src,
        "src_covered": src_covered,
        "src_coverage_pct": int(src_covered / total_src * 100) if total_src else 0,
        "numbers_in_source_missing_from_xml": sorted(missing_nums)[:20],
    }
