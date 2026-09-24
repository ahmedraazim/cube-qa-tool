"""
Source document retrieval and content extraction.

Handles:
- HTTP/HTTPS fetching with SSRF protection
- Selenium (Chrome) for JavaScript-rendered pages — primary JS renderer
- Playwright fallback (if installed)
- PDF link auto-discovery from rendered pages
- PDF text extraction (with optional OCR)
- HTML content extraction (BeautifulSoup)
- DOCX text extraction
- Plain text files
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import mimetypes
import os
import re
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin, unquote

import urllib3
import chardet
import requests
from bs4 import BeautifulSoup

# Suppress SSL warnings — corporate proxies often use self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from . import config_loader as cfg
from .models import SourceDocument

# ─── Optional imports ─────────────────────────────────────────────────────────

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
    _tcmd = os.environ.get("TESSERACT_CMD", "")
    if _tcmd:
        pytesseract.pytesseract.tesseract_cmd = _tcmd
except ImportError:
    OCR_AVAILABLE = False

# Selenium — installed via: pip install selenium webdriver-manager
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False


# ─── SSRF protection ──────────────────────────────────────────────────────────

def _is_private_address(host: str) -> bool:
    allowed = os.environ.get("ALLOWED_INTERNAL_HOSTS", "rm.gocube.global").split(",")
    if host.strip() in [h.strip() for h in allowed]:
        return False
    try:
        addr = socket.gethostbyname(host)
        ip = ipaddress.ip_address(addr)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except Exception:
        return False


def _validate_url(url: str) -> Tuple[bool, str]:
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"URL parse error: {exc}"
    if parsed.scheme not in ("http", "https"):
        return False, f"Unsupported scheme: {parsed.scheme!r}"
    if not parsed.netloc:
        return False, "URL has no host"
    if _is_private_address(parsed.hostname or ""):
        return False, f"URL resolves to private address: {parsed.hostname}"
    return True, ""


# ─── HTTP session ─────────────────────────────────────────────────────────────

_SESSION: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,ar;q=0.8,en-US;q=0.7,en;q=0.6",
            "Accept-Encoding": "gzip, deflate, br",
        })
    return _SESSION


# ─── Selenium fetch — tries Chrome, Edge, Firefox in order ──────────────────

_COMMON_ARGS = [
    "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
    "--disable-gpu", "--window-size=1920,1080",
    "--ignore-certificate-errors", "--ignore-ssl-errors",
    "--allow-running-insecure-content", "--disable-web-security",
    "--disable-blink-features=AutomationControlled",
]
_UA = (
    "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CONTENT_SELECTORS = [
    "article", "main", ".content", "#content",
    ".normativo-content", ".document-content",
    "[class*='legisl']", "[class*='normativ']",
    "p", "div.texto", ".texto-norma",
]


def _run_driver(driver, url: str, wait: int) -> str:
    """Navigate, wait for content, scroll, return page_source."""
    driver.set_page_load_timeout(35)
    driver.get(url)
    for sel in _CONTENT_SELECTORS:
        try:
            WebDriverWait(driver, wait).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            break
        except TimeoutException:
            continue
    time.sleep(2)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)
    return driver.page_source


def _try_chrome(url: str, wait: int) -> tuple:
    options = ChromeOptions()
    for a in _COMMON_ARGS:
        options.add_argument(a)
    options.add_argument(_UA)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = None
    try:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            svc = ChromeService(ChromeDriverManager().install())
        except Exception:
            svc = ChromeService()
        driver = webdriver.Chrome(service=svc, options=options)
        html = _run_driver(driver, url, wait)
        return html.encode("utf-8"), None
    except Exception as exc:
        return None, f"Chrome: {str(exc)[:120]}"
    finally:
        if driver:
            try: driver.quit()
            except: pass


def _try_edge(url: str, wait: int) -> tuple:
    """Microsoft Edge — pre-installed on every Windows 10/11 machine."""
    try:
        from selenium.webdriver.edge.options import Options as EdgeOptions
        from selenium.webdriver.edge.service import Service as EdgeService
    except ImportError:
        return None, "Edge: selenium edge module not available"
    options = EdgeOptions()
    for a in _COMMON_ARGS:
        options.add_argument(a)
    options.add_argument(_UA)
    driver = None
    try:
        try:
            from webdriver_manager.microsoft import EdgeChromiumDriverManager
            svc = EdgeService(EdgeChromiumDriverManager().install())
        except Exception:
            svc = EdgeService()
        driver = webdriver.Edge(service=svc, options=options)
        html = _run_driver(driver, url, wait)
        return html.encode("utf-8"), None
    except Exception as exc:
        return None, f"Edge: {str(exc)[:120]}"
    finally:
        if driver:
            try: driver.quit()
            except: pass


def _try_firefox(url: str, wait: int) -> tuple:
    """Firefox with geckodriver."""
    try:
        from selenium.webdriver.firefox.options import Options as FFOptions
        from selenium.webdriver.firefox.service import Service as FFService
    except ImportError:
        return None, "Firefox: selenium firefox module not available"
    options = FFOptions()
    options.add_argument("--headless")
    options.set_preference("acceptInsecureCerts", True)
    driver = None
    try:
        try:
            from webdriver_manager.firefox import GeckoDriverManager
            svc = FFService(GeckoDriverManager().install())
        except Exception:
            svc = FFService()
        driver = webdriver.Firefox(service=svc, options=options)
        html = _run_driver(driver, url, wait)
        return html.encode("utf-8"), None
    except Exception as exc:
        return None, f"Firefox: {str(exc)[:120]}"
    finally:
        if driver:
            try: driver.quit()
            except: pass


def _selenium_fetch(url: str, wait_seconds: int = 8) -> tuple:
    """
    Try Chrome → Edge → Firefox in order.
    Returns (html_bytes, error_message).
    Edge is pre-installed on all Windows 10/11 machines so it's a reliable fallback.
    """
    if not SELENIUM_AVAILABLE:
        return None, "Selenium not installed. Run: pip install selenium webdriver-manager"

    errors = []

    html, err = _try_chrome(url, wait_seconds)
    if html:
        return html, None
    errors.append(err)

    html, err = _try_edge(url, wait_seconds)
    if html:
        return html, None
    errors.append(err)

    html, err = _try_firefox(url, wait_seconds)
    if html:
        return html, None
    errors.append(err)

    return None, (
        "All browsers failed. Details: " + " | ".join(e for e in errors if e) + ". "
        "Chrome/Edge/Firefox must be installed. Run: pip install --upgrade selenium webdriver-manager"
    )


# ─── Playwright fetch (secondary fallback) ────────────────────────────────────

def _playwright_fetch(url: str) -> Optional[bytes]:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=25000, wait_until="networkidle")
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
            return html.encode("utf-8")
    except ImportError:
        return None
    except Exception:
        return None


# ─── PDF/document link discovery ─────────────────────────────────────────────

def _find_document_links(html_str: str, base_url: str) -> List[str]:
    """
    Scan rendered HTML for direct document links (PDF, DOCX, or regulation endpoints).
    Returns URLs ordered by confidence (most specific first).
    """
    soup = BeautifulSoup(html_str, "html.parser")
    high: List[str] = []
    medium: List[str] = []
    base_domain = urlparse(base_url).netloc

    doc_keywords_url = (
        "pdf", "download", "normativo", "legisl", "decreto",
        "resolu", "circular", "lei", "portaria", "instruc",
        "deliber", "acord", "regulament", "document",
    )
    doc_keywords_text = ("pdf", "download", "full text", "texto", "documento", "baixar")

    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript"):
            continue
        full = urljoin(base_url, href)
        if urlparse(full).netloc != base_domain:
            continue
        link_text = a.get_text(strip=True).lower()
        href_lower = href.lower()

        if href_lower.endswith(".pdf") or href_lower.endswith(".docx"):
            high.insert(0, full)
        elif "download" in href_lower:
            high.append(full)
        elif any(kw in href_lower for kw in doc_keywords_url):
            medium.append(full)
        elif any(kw in link_text for kw in doc_keywords_text):
            medium.append(full)

    # Deduplicate
    seen: set = set()
    result = []
    for u in high + medium:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result[:6]


# ─── Main fetch functions ─────────────────────────────────────────────────────

def fetch_url(url: str) -> SourceDocument:
    """Plain HTTP fetch — no JavaScript rendering."""
    ok, err = _validate_url(url)
    if not ok:
        return SourceDocument(
            origin=url, retrieval_time=datetime.utcnow(),
            content_type=None, file_hash=None, text=None,
            retrieval_error=f"URL rejected: {err}",
            retrieval_status_code=None,
            extraction_method="rejected", is_complete=False,
        )

    timeout = int(os.environ.get("SOURCE_FETCH_TIMEOUT",
                                  cfg.get("retrieval.timeout_seconds", 20)))
    max_bytes = int(os.environ.get("SOURCE_MAX_BYTES",
                                    cfg.get("retrieval.max_bytes", 52428800)))
    max_redirects = int(cfg.get("retrieval.max_redirects", 5))
    retry_attempts = int(cfg.get("retrieval.retry_attempts", 2))
    retry_delay = float(cfg.get("retrieval.retry_delay_seconds", 2))

    session = _get_session()
    session.max_redirects = max_redirects
    last_error = None
    last_status = None

    for attempt in range(retry_attempts):
        try:
            resp = session.get(url, timeout=timeout, stream=True, allow_redirects=True, verify=False)
            last_status = resp.status_code
            content_type = resp.headers.get("content-type", "")

            if resp.url != url:
                final_ok, final_err = _validate_url(resp.url)
                if not final_ok:
                    return SourceDocument(
                        origin=url, retrieval_time=datetime.utcnow(),
                        content_type=content_type, file_hash=None, text=None,
                        retrieval_error=f"Redirect blocked: {final_err}",
                        retrieval_status_code=last_status,
                        extraction_method="redirect-blocked", is_complete=False,
                    )

            raw_data = b""
            for chunk in resp.iter_content(chunk_size=65536):
                raw_data += chunk
                if len(raw_data) > max_bytes:
                    raw_data = raw_data[:max_bytes]
                    break

            return _extract_content(
                raw_data=raw_data,
                content_type=content_type,
                origin=resp.url,
                retrieval_time=datetime.utcnow(),
                file_hash=hashlib.sha256(raw_data).hexdigest(),
                status_code=last_status,
            )

        except requests.exceptions.Timeout:
            last_error = "Connection timed out"
        except requests.exceptions.ConnectionError as exc:
            last_error = f"Connection error: {exc}"
        except requests.exceptions.TooManyRedirects:
            last_error = "Too many redirects"
            break
        except Exception as exc:
            last_error = f"Retrieval error: {exc}"
            break

        if attempt < retry_attempts - 1:
            time.sleep(retry_delay)

    return SourceDocument(
        origin=url, retrieval_time=datetime.utcnow(),
        content_type=None, file_hash=None, text=None,
        retrieval_error=last_error or "Unknown retrieval failure",
        retrieval_status_code=last_status,
        extraction_method="failed",
        extraction_warnings=[
            "Retrieval failed after retries. Cannot determine whether the link "
            "is genuinely broken or blocked by geography/network."
        ],
        is_complete=False,
    )


def fetch_url_smart(url: str) -> SourceDocument:
    """
    Smart multi-strategy fetch — Selenium FIRST for reliable JS rendering:
      1. Selenium headless Chrome (primary — handles BCB, UAE, Angular/React pages)
      2. Playwright (secondary JS renderer if Selenium unavailable)
      3. Plain HTTP (fallback for simple static pages or when no renderer available)
      4. PDF/document link discovery from rendered page
    Returns the best source document found.
    """
    # ── Step 1: Selenium (primary) ────────────────────────────────────────────
    rendered_html: Optional[bytes] = None
    renderer_used = None
    renderer_errors: list = []

    sel_html, sel_err = _selenium_fetch(url)
    if sel_html:
        rendered_html = sel_html
        renderer_used = "selenium-chrome"
    else:
        renderer_errors.append(f"Selenium: {sel_err}")

    # ── Step 2: Playwright fallback ───────────────────────────────────────────
    if not rendered_html:
        pw_html = _playwright_fetch(url)
        if pw_html:
            rendered_html = pw_html
            renderer_used = "playwright-chromium"
        else:
            renderer_errors.append("Playwright: not available or failed")

    if rendered_html:
        rendered_doc = _extract_html(
            raw_data=rendered_html,
            origin=url,
            retrieval_time=datetime.utcnow(),
            file_hash=hashlib.sha256(rendered_html).hexdigest(),
            status_code=200,
        )
        rendered_text = (rendered_doc.text or "").strip()

        # Use rendered version if it has meaningful content
        if len(rendered_text) > 100:
            rendered_doc.extraction_warnings.insert(
                0, f"Content rendered via {renderer_used}. "
                   f"Extracted {len(rendered_text):,} characters."
            )

            # ── Step 4: look for embedded PDF/doc links in rendered page ─────
            html_str = rendered_html.decode("utf-8", errors="replace")
            doc_links = _find_document_links(html_str, url)
            if doc_links:
                rendered_doc.extraction_warnings.append(
                    f"Document links found in page: {doc_links[:3]}"
                )
                # Try to fetch the first direct PDF
                for link in doc_links:
                    ok, _ = _validate_url(link)
                    if not ok:
                        continue
                    pdf_doc = fetch_url(link)
                    if not pdf_doc.retrieval_error and pdf_doc.text and len(pdf_doc.text.strip()) > 300:
                        pdf_doc.extraction_warnings.insert(
                            0, f"Document fetched from discovered link: {link} "
                               f"(page rendered via {renderer_used})"
                        )
                        return pdf_doc

            return rendered_doc

    # ── Step 3: Plain HTTP fallback (when no renderer available) ────────────
    doc = fetch_url(url)
    plain_text = (doc.text or "").strip()
    http_ok = doc.retrieval_status_code == 200
    is_js_shell = any(
        "javascript" in w.lower() or "angular" in w.lower() or "react" in w.lower()
        for w in (doc.extraction_warnings or [])
    ) or (http_ok and len(plain_text) < 300)
    if not is_js_shell and len(plain_text) >= 300:
        return doc  # plain fetch got good content

    # ── Step 4: PDF link discovery from plain HTML ────────────────────────────
    if http_ok and doc.retrieval_status_code == 200:
        try:
            raw = requests.get(url, timeout=20, headers=_get_session().headers, verify=False).content
            html_str = raw.decode("utf-8", errors="replace")
            doc_links = _find_document_links(html_str, url)
            for link in doc_links:
                ok, _ = _validate_url(link)
                if not ok:
                    continue
                link_doc = fetch_url(link)
                if not link_doc.retrieval_error and link_doc.text and len(link_doc.text.strip()) > 200:
                    link_doc.extraction_warnings.insert(
                        0, f"Document fetched from page link: {link}"
                    )
                    return link_doc
        except Exception:
            pass

    # ── No renderer succeeded — report why and give instructions ────────────
    if not rendered_html:
        sel_msg = renderer_errors[0] if renderer_errors else "Selenium: not tried"
        renderer_note = (
            f"JavaScript rendering failed. {sel_msg}. "
            "FIX: Run these two commands in your terminal, then restart the app:\n"
            "  pip install selenium webdriver-manager\n"
            "  (Chrome browser must be installed on this computer)\n"
            "OR: open the URL in Chrome, wait for it to load, press Ctrl+S, "
            "save as Webpage Complete, then upload the .html file."
        )
        if doc.extraction_warnings:
            doc.extraction_warnings.insert(0, renderer_note)
        else:
            doc.extraction_warnings = [renderer_note]

    return doc


# ─── File loader ─────────────────────────────────────────────────────────────

def load_file(file_path: Path, filename: str = "") -> SourceDocument:
    if not file_path.exists():
        return SourceDocument(
            origin=filename or str(file_path),
            retrieval_time=datetime.utcnow(),
            content_type=None, file_hash=None, text=None,
            retrieval_error=f"File not found: {file_path}",
            is_complete=False,
        )
    raw_data = file_path.read_bytes()
    file_hash = hashlib.sha256(raw_data).hexdigest()
    name = filename or file_path.name
    content_type, _ = mimetypes.guess_type(name)
    if not content_type:
        content_type = _sniff_content_type(raw_data)
    return _extract_content(
        raw_data=raw_data,
        content_type=content_type or "application/octet-stream",
        origin=name,
        retrieval_time=datetime.utcnow(),
        file_hash=file_hash,
        status_code=None,
    )


# ─── Content extraction ───────────────────────────────────────────────────────

def _sniff_content_type(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return "application/zip"
    if data[:5] in (b"<?xml", b"<html", b"<!DOC"):
        return "text/html"
    return "text/plain"


def _extract_content(
    raw_data: bytes,
    content_type: str,
    origin: str,
    retrieval_time: datetime,
    file_hash: str,
    status_code: Optional[int],
) -> SourceDocument:
    ct = (content_type or "").lower()
    if "pdf" in ct or origin.lower().endswith(".pdf"):
        return _extract_pdf(raw_data, origin, retrieval_time, file_hash, status_code)
    if "docx" in ct or "wordprocessingml" in ct or origin.lower().endswith(".docx"):
        return _extract_docx(raw_data, origin, retrieval_time, file_hash, status_code)
    if "html" in ct or "xml" in ct:
        return _extract_html(raw_data, origin, retrieval_time, file_hash, status_code)
    return _extract_text(raw_data, origin, retrieval_time, file_hash, status_code)


def _extract_html(
    raw_data: bytes,
    origin: str,
    retrieval_time: datetime,
    file_hash: str,
    status_code: Optional[int],
) -> SourceDocument:
    warnings: List[str] = []
    enc = (chardet.detect(raw_data[:8192]).get("encoding") or "utf-8")
    try:
        html_str = raw_data.decode(enc, errors="replace")
    except Exception:
        html_str = raw_data.decode("utf-8", errors="replace")

    soup = BeautifulSoup(html_str, "html.parser")
    for tag in soup.find_all(["nav", "footer", "script", "style", "aside", "header"]):
        tag.decompose()

    headings = [h.get_text(strip=True) for h in soup.find_all(re.compile(r"^h[1-6]$"))]
    tables: List[List[List[str]]] = []
    for tbl in soup.find_all("table"):
        rows = []
        for tr in tbl.find_all("tr"):
            rows.append([td.get_text(strip=True) for td in tr.find_all(["td", "th"])])
        if rows:
            tables.append(rows)
    images = [{"src": img.get("src", ""), "alt": img.get("alt", "")}
              for img in soup.find_all("img")]
    footnotes = [fn.get_text(strip=True)
                 for fn in soup.find_all(attrs={"class": re.compile(r"footnote|fn-|endnote", re.I)})]

    main = (soup.find("main") or soup.find("article") or
            soup.find("div", id=re.compile(r"content|main|body", re.I)))
    text = (main or soup).get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # JS-shell detection
    is_js_shell = (
        len(text) < 200 or
        "<app-root" in html_str.lower() or
        "ng-version" in html_str.lower() or
        "data-reactroot" in html_str.lower() or
        ("<script" in html_str.lower() and len(text.strip()) < 100)
    )
    if is_js_shell:
        warnings.append(
            "Page is JavaScript-rendered — plain fetch captured only the shell. "
            "Selenium will attempt to render it automatically."
        )
    elif len(text) < 100:
        warnings.append("Extracted text is very short — page may require JavaScript rendering.")

    return SourceDocument(
        origin=origin, retrieval_time=retrieval_time,
        content_type="text/html", file_hash=file_hash,
        text=text, headings=headings, tables=tables,
        images=images, footnotes=footnotes,
        retrieval_status_code=status_code,
        extraction_method="html-beautifulsoup",
        extraction_warnings=warnings,
        is_complete=bool(text and len(text) > 50),
    )


def _extract_pdf(
    raw_data: bytes,
    origin: str,
    retrieval_time: datetime,
    file_hash: str,
    status_code: Optional[int],
) -> SourceDocument:
    warnings: List[str] = []
    text = ""
    page_texts: List[Dict[str, Any]] = []
    ocr_used = False
    ocr_confidence: Optional[float] = None

    if not PDF_AVAILABLE:
        return SourceDocument(
            origin=origin, retrieval_time=retrieval_time,
            content_type="application/pdf", file_hash=file_hash,
            text=None, retrieval_status_code=status_code,
            extraction_method="pdf-unavailable",
            extraction_warnings=["pdfminer.six not available."],
            is_complete=False,
        )
    try:
        text = (pdf_extract_text(io.BytesIO(raw_data)) or "").strip()
    except Exception as exc:
        warnings.append(f"pdfminer extraction failed: {exc}")
        text = ""

    if not text.strip() and OCR_AVAILABLE:
        warnings.append("No text layer in PDF — attempting OCR.")
        ocr_used = True
        try:
            from pdf2image import convert_from_bytes
            images = convert_from_bytes(raw_data, dpi=200)
            page_strings, confidences = [], []
            for i, img in enumerate(images):
                data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
                conf_vals = [float(c) for c in data["conf"]
                             if str(c).lstrip("-").isdigit() and int(c) >= 0]
                if conf_vals:
                    confidences.append(sum(conf_vals) / len(conf_vals))
                page_str = pytesseract.image_to_string(img)
                page_strings.append(page_str)
                page_texts.append({"page": i + 1, "text": page_str})
            text = "\n\n".join(page_strings)
            if confidences:
                ocr_confidence = sum(confidences) / len(confidences)
                if ocr_confidence < 70:
                    warnings.append(f"Low OCR confidence ({ocr_confidence:.1f}%) — verify manually.")
        except ImportError:
            warnings.append("pdf2image not available — OCR skipped.")
            text = None
        except Exception as exc:
            warnings.append(f"OCR failed: {exc}")
            text = None
    elif not text.strip():
        warnings.append("No text extracted from PDF and OCR not available.")
        text = None

    return SourceDocument(
        origin=origin, retrieval_time=retrieval_time,
        content_type="application/pdf", file_hash=file_hash,
        text=text, page_texts=page_texts,
        retrieval_status_code=status_code,
        extraction_method="pdf-ocr" if ocr_used else "pdf-text",
        extraction_warnings=warnings,
        ocr_used=ocr_used, ocr_confidence=ocr_confidence,
        is_complete=bool(text),
    )


def _extract_docx(
    raw_data: bytes,
    origin: str,
    retrieval_time: datetime,
    file_hash: str,
    status_code: Optional[int],
) -> SourceDocument:
    warnings: List[str] = []
    if not DOCX_AVAILABLE:
        return SourceDocument(
            origin=origin, retrieval_time=retrieval_time,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_hash=file_hash, text=None,
            retrieval_status_code=status_code,
            extraction_method="docx-unavailable",
            extraction_warnings=["python-docx not available"],
            is_complete=False,
        )
    try:
        document = docx.Document(io.BytesIO(raw_data))
    except Exception as exc:
        return SourceDocument(
            origin=origin, retrieval_time=retrieval_time,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_hash=file_hash, text=None,
            retrieval_status_code=status_code,
            extraction_method="docx-failed",
            extraction_warnings=[f"DOCX parse error: {exc}"],
            is_complete=False,
        )
    paras, headings = [], []
    for para in document.paragraphs:
        t = para.text.strip()
        if not t:
            continue
        if para.style and "heading" in para.style.name.lower():
            headings.append(t)
        paras.append(t)
    tables = [[
        [cell.text.strip() for cell in row.cells]
        for row in tbl.rows
    ] for tbl in document.tables]
    text = "\n".join(paras)
    return SourceDocument(
        origin=origin, retrieval_time=retrieval_time,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_hash=file_hash, text=text, headings=headings, tables=tables,
        retrieval_status_code=status_code,
        extraction_method="docx-python-docx",
        extraction_warnings=warnings, is_complete=True,
    )


def _extract_text(
    raw_data: bytes,
    origin: str,
    retrieval_time: datetime,
    file_hash: str,
    status_code: Optional[int],
) -> SourceDocument:
    enc = (chardet.detect(raw_data[:8192]).get("encoding") or "utf-8")
    text = raw_data.decode(enc, errors="replace").strip()
    return SourceDocument(
        origin=origin, retrieval_time=retrieval_time,
        content_type="text/plain", file_hash=file_hash,
        text=text, retrieval_status_code=status_code,
        extraction_method="text-plain", is_complete=True,
    )
