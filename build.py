#!/usr/bin/env python3
"""Convert new PDFs in inbox/ into HTML articles and an Atom feed under site/.

Idempotent: a PDF whose SHA-256 is already in data/articles.json is never
processed twice. Site URL comes from $SITE_URL (set by the workflow), else it
is derived from $GITHUB_REPOSITORY.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import timedelta
from math import ceil
from urllib.parse import urljoin
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pymupdf as fitz  # PyMuPDF
from feedgen.feed import FeedGenerator
from lxml import etree
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "inbox"
PROCESSED = ROOT / "processed"
DATA = ROOT / "data"
CONTENT = DATA / "content"          # stored article body fragments (for the feed)
ARTICLES_JSON = DATA / "articles.json"
SITE = ROOT / "site"
TEMPLATES = ROOT / "templates"

MIN_IMG_PX = 100
SPLIT_MIN_PAGES = 21      # PDFs with more than 20 pages become several feed items
CHUNK_PAGES = 10          # fallback part size when there are no chapters
FEED_LIMIT = 100
OCR_LANGS = "hun+eng"


# --------------------------------------------------------------------------- config

def site_url() -> str:
    url = os.environ.get("SITE_URL", "").strip()
    if not url:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" in repo:
            owner, name = repo.split("/", 1)
            url = f"https://{owner.lower()}.github.io/{name}"
        else:
            url = "http://localhost:8000"
    return url.rstrip("/")


SITE_TITLE = os.environ.get("FEED_TITLE", "PDF articles")
FEED_AUTHOR = os.environ.get(
    "FEED_AUTHOR", os.environ.get("GITHUB_REPOSITORY_OWNER", "pdfprocess"))


# --------------------------------------------------------------------------- helpers

_TRANSLIT = str.maketrans({
    "ß": "ss", "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
    "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "þ": "th", "ð": "d", "ı": "i",
})


def slugify(text: str, max_len: int = 60) -> str:
    text = text.translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0] or text[:max_len]
    return text or "article"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit_date(path: Path) -> datetime:
    """Date of the last commit touching `path`; now (UTC) if not committed."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", str(path.relative_to(ROOT))],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if out:
            return datetime.fromisoformat(out)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        pass
    return datetime.now(timezone.utc).replace(microsecond=0)


def load_articles() -> list[dict]:
    if ARTICLES_JSON.exists():
        return json.loads(ARTICLES_JSON.read_text(encoding="utf-8"))
    return []


def save_articles(articles: list[dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    articles.sort(key=lambda a: a["date"], reverse=True)
    ARTICLES_JSON.write_text(
        json.dumps(articles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- OCR

def page_needs_ocr(page: fitz.Page) -> bool:
    return not page.get_text("text").strip() and bool(page.get_images())


def ocr_if_needed(src: Path, workdir: Path) -> tuple[Path, set[int]]:
    """Return (pdf to extract from, set of page indices that were OCRed)."""
    with fitz.open(src) as doc:
        pages = {i for i, p in enumerate(doc) if page_needs_ocr(p)}
    if not pages:
        return src, set()
    out = workdir / "ocr.pdf"
    cmd = [sys.executable, "-m", "ocrmypdf", "-l", OCR_LANGS, "--skip-text",
           "--rotate-pages", "--output-type", "pdf", "-q", str(src), str(out)]
    print(f"  OCR on {len(pages)} page(s): {' '.join(cmd[2:])}")
    subprocess.run(cmd, check=True)
    return out, pages


# --------------------------------------------------------------------------- extraction

_LIGATURES = str.maketrans({"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
                            "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"})


def clean(t: str) -> str:
    return t.translate(_LIGATURES)


PARA_BREAK = "\x00"
GLUE = "\x01"  # trailing drop cap: join the next block without a space


def block_text(block: dict) -> str:
    """Block text; PARA_BREAK marks a paragraph start inside the block (drop cap)."""
    lines = []
    for line in block["lines"]:
        t = "".join(s["text"] for s in line["spans"]).strip()
        if t:
            size = max(s["size"] for s in line["spans"] if s["text"].strip())
            lines.append((t, size))
    out, dropcap = "", ""
    for i, (t, size) in enumerate(lines):
        nxt = lines[i + 1][1] if i + 1 < len(lines) else (lines[i - 1][1] if i else 0)
        if re.fullmatch(r"[A-ZÀ-Ý]", t) and nxt and size > 2 * nxt:
            dropcap = t   # glue to the next line, which starts a new paragraph
            continue
        if dropcap:
            out += (PARA_BREAK if out else "") + dropcap + t
            dropcap = ""
        elif not out:
            out = t
        elif re.search(r"\w-$", out) and t[:1].islower():   # de-hyphenate
            out = out[:-1] + t
        else:
            out += " " + t
    if dropcap:  # drop cap is the block's last line: it starts the next block's text
        out += (PARA_BREAK if out else "") + dropcap + GLUE
    return PARA_BREAK.join(re.sub(r"\s+", " ", clean(p)).strip() for p in out.split(PARA_BREAK))


def block_size(block: dict) -> float:
    sizes = [s["size"] for l in block["lines"] for s in l["spans"] if s["text"].strip()]
    return max(sizes) if sizes else 0.0


def block_bold(block: dict) -> bool:
    spans = [s for l in block["lines"] for s in l["spans"] if s["text"].strip()]
    return bool(spans) and all(s["flags"] & 16 or "bold" in s["font"].lower() for s in spans)


def norm_repeat(t: str) -> str:
    return re.sub(r"\d+", "#", t.lower()).strip()


def detect_title(doc: fitz.Document, fallback: str, ocr_first_page: bool = False) -> str:
    meta = (doc.metadata or {}).get("title", "") or ""
    meta = meta.strip()
    meta = clean(meta)
    junk = re.compile(r"^(untitled|microsoft (word|powerpoint)|document\d*$|.*\.[a-z0-9]{1,5}$)", re.I)
    filename_like = " " not in meta and ("_" in meta or re.search(r"\d{3,}", meta))
    if len(meta) >= 3 and not junk.match(meta) and not filename_like:
        return meta
    if len(doc) and not ocr_first_page:  # OCR font sizes are not meaningful
        blocks = [b for b in doc[0].get_text("dict")["blocks"] if b["type"] == 0]
        by_size: dict[float, list[str]] = {}
        for b in blocks:
            for l in b["lines"]:
                for s in l["spans"]:
                    t = s["text"].strip()
                    if t:
                        by_size.setdefault(round(s["size"]), []).append(t)
        # Largest font whose text looks like a title (skips drop caps, symbols).
        for size in sorted(by_size, reverse=True):
            title = re.sub(r"\s+", " ", clean(" ".join(by_size[size]))).strip()
            if len(title) >= 8 and len(re.findall(r"\w", title)) >= 5:
                return title[:200]
    return fallback


_NOT_NAME = re.compile(
    r"\b(universit\w*|institut\w*|college|school|department|dept|faculty|centre|center|bank|"
    r"press|journal|review|paper|papers|report|volume|vol|series|working|abstract|summary|"
    r"introduction|chapter|copyright|egyetem\w*|intézet\w*|tanszék\w*|kar|kiadó|"
    r"összefoglaló|bevezetés|fejezet|the|of|for|in|on|to|with|from|"
    r"microsoft|office|user|admin\w*|owner|windows|adobe|acrobat|unknown|anonymous|"
    r"kingdom|states|republic|reserve|federal|national|union|ministry|government|agency|"
    r"association|society|foundation|committee|council|group|company|corporation|inc|ltd|"
    r"llc|zrt|kft|nyrt|minisztérium|hivatal)\b", re.I)
_MARKS = re.compile(r"[*†‡§¹²³⁴⁵⁶⁷⁸⁹⁰\d]+(?=,|\s|$)")  # footnote marks after names
_NAME_WORD = r"(?:[A-ZÀ-ÖØ-Ý][\w'’\-]*\.?|[A-ZÀ-ÖØ-Ý]\.)"
_CONNECT = r"(?:,\s*(?:and\s+|&\s+|és\s+)?|\s+(?:and|&|és)\s+)"
_ONE_NAME = rf"{_NAME_WORD}(?:\s+(?:(?:van|von|de|der|den|da|di|du|la|le|ten|ter)\s+)*{_NAME_WORD}){{1,4}}"
_NAMES = re.compile(rf"^(?:by\s+)?{_ONE_NAME}(?:{_CONNECT}{_ONE_NAME}){{0,7}}$")


def _name_case(t: str) -> str:
    """NIALL FERGUSON -> Niall Ferguson; mixed case is left alone."""
    if t.upper() != t:
        return t
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), t)


def looks_like_names(t: str) -> bool:
    t = _MARKS.sub("", t).strip()
    if t.upper() != t and re.search(r"\b[A-Z]{2,}\b", t):  # acronym in mixed case: GDP, IMF
        return False
    return (3 <= len(t) <= 120 and not _NOT_NAME.search(t) and "@" not in t
            and not re.search(r"\d", t) and bool(_NAMES.match(_name_case(t))))


def detect_author(doc: fitz.Document, title: str, ocr_first_page: bool = False) -> str:
    """A name line on page 1 near the title, else the PDF metadata author, else ''.
    The byline wins because metadata often names the typesetter or uploader."""
    byline = "" if ocr_first_page or not len(doc) else _byline(doc[0], title)
    if byline:
        return byline
    meta = clean((doc.metadata or {}).get("author", "") or "").strip()
    meta = re.sub(r"\s*;\s*", ", ", meta)
    if looks_like_names(meta):
        return _name_case(_MARKS.sub("", meta).strip())
    return ""


def _byline(page: fitz.Page, title: str) -> str:
    norm = lambda t: re.sub(r"\W+", " ", t).strip().lower()
    for b in sorted((b for b in page.get_text("dict")["blocks"] if b["type"] == 0),
                    key=lambda b: b["bbox"][1]):
        if b["bbox"][1] > 0.5 * page.rect.height:
            break
        for line in b["lines"]:
            t = re.sub(r"\s+", " ", clean("".join(s["text"] for s in line["spans"]))).strip()
            if t and norm(t) not in norm(title) and looks_like_names(t):
                t = re.sub(r"^by\s+", "", t, flags=re.I)
                t = _MARKS.sub("", t).strip()
                return _name_case(t)
    return ""


def reading_order(items: list[dict], page_width: float) -> list[dict]:
    """Order items: full-width items split the page into bands; inside a band,
    left column top-to-bottom then right column top-to-bottom."""
    mid = page_width / 2
    items = sorted(items, key=lambda it: (round(it["bbox"][1]), it["bbox"][0]))
    out, band = [], []

    def flush():
        band.sort(key=lambda it: (0 if (it["bbox"][0] + it["bbox"][2]) / 2 < mid else 1,
                                  it["bbox"][1]))
        out.extend(band)
        band.clear()

    for it in items:
        x0, _, x1, _ = it["bbox"]
        if x1 - x0 > 0.55 * page_width or (x0 < mid - 20 and x1 > mid + 20):
            flush()
            out.append(it)
        else:
            band.append(it)
    flush()
    return out


CAPTION = re.compile(r"^(fig(ure)?|table|chart|exhibit|source|sources|note|notes|"
                     r"ábra|táblázat|forrás|megjegyzés)\b", re.I)
FIG_DPI = 200


def _merge(rects: list) -> list:
    rects = [fitz.Rect(r) for r in rects]
    merged = True
    while merged:
        merged = False
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                if rects[i].intersects(rects[j]):
                    rects[i] |= rects.pop(j)
                    merged = True
                    break
            if merged:
                break
    return rects


def _inside(bbox, region: fitz.Rect, share: float = 0.5) -> bool:
    r = fitz.Rect(bbox)
    return not r.is_empty and (r & region).get_area() >= share * r.get_area()


def figure_regions(page: fitz.Page, texts: list[dict], is_ocr: bool) -> list[fitz.Rect]:
    """Page areas to render as figures: raster images >= MIN_IMG_PX and clusters
    of vector drawings (charts), grown to include their short labels but not
    their captions ("Figure 1 ...", "Source: ...")."""
    pr, area = page.rect, page.rect.get_area()
    rects = []
    for img in page.get_images(full=True):
        if img[2] < MIN_IMG_PX or img[3] < MIN_IMG_PX:
            continue
        for r in page.get_image_rects(img[0]):
            r &= pr
            if r.is_empty or r.width < 20 or r.height < 20:
                continue
            if is_ocr and r.get_area() > 0.6 * area:  # the scan itself, not a figure
                continue
            rects.append(r)
    if not is_ocr:
        try:
            clusters = page.cluster_drawings()
        except Exception:  # noqa: BLE001
            clusters = []
        for r in clusters:
            r &= pr
            if r.width >= 100 and r.height >= 60 and r.get_area() < 0.85 * area:
                rects.append(r)
    regions = _merge(rects)

    out = []
    for reg in regions:
        # grow by nearby labels (axis ticks, legends, panel titles)
        grown = True
        while grown:
            grown = False
            for t in texts:
                tb = fitz.Rect(t["bbox"])
                if (len(t["text"]) <= 80 and not CAPTION.match(t["text"])
                        and tb.intersects(reg + (-10, -10, 10, 10)) and not reg.contains(tb)):
                    reg |= tb
                    grown = True
        # keep captions out of the picture
        for t in texts:
            tb = fitz.Rect(t["bbox"])
            if CAPTION.match(t["text"]) and _inside(tb, reg):
                if tb.y0 + tb.y1 > reg.y0 + reg.y1:
                    reg.y1 = min(reg.y1, tb.y0 - 4)  # 4 > render padding
                else:
                    reg.y0 = max(reg.y0, tb.y1 + 4)
        if reg.height < 30 or reg.width < 30:
            continue
        # a box that holds running prose is a text box/table, not a figure
        if any(len(t["text"]) > 200 and _inside(t["bbox"], reg) for t in texts):
            continue
        out.append(reg)
    return _merge(out)


def render_region(page: fitz.Page, rect: fitz.Rect, dest: Path) -> bytes | None:
    pix = page.get_pixmap(clip=(rect + (-3, -3, 3, 3)) & page.rect, dpi=FIG_DPI, alpha=False)
    if pix.width < MIN_IMG_PX or pix.height < MIN_IMG_PX:
        return None
    data = pix.tobytes("png")
    dest.write_bytes(data)
    return data


def extract(pdf: Path, outdir: Path, ocr_pages: set[int]) -> list[dict]:
    """Return a list of content elements: {'kind': 'h2'|'h3'|'h4'|'p'|'img', ...}."""
    doc = fitz.open(pdf)
    pages = []
    size_counter: Counter = Counter()
    edge_counter: Counter = Counter()

    for pno, page in enumerate(doc):
        h = page.rect.height
        blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
        texts = []
        for b in blocks:
            t = block_text(b)
            if not t:
                continue
            edge = (b["bbox"][1] < 0.08 * h or b["bbox"][3] > 0.92 * h) and len(t) <= 150
            if edge:
                edge_counter[norm_repeat(t)] += 1
            ocr = pno in ocr_pages  # OCR text has no reliable size/weight
            texts.append({"type": "text", "bbox": b["bbox"], "text": t,
                          "size": 0.0 if ocr else block_size(b),
                          "bold": False if ocr else block_bold(b), "edge": edge,
                          "spans": [(round(s["size"], 1), len(s["text"].strip()))
                                    for l in b["lines"] for s in l["spans"]]})
        figs = figure_regions(page, texts, pno in ocr_pages)
        texts = [t for t in texts if not any(_inside(t["bbox"], f) for f in figs)]
        if pno not in ocr_pages:
            for t in texts:
                for size, n in t["spans"]:
                    size_counter[size] += n
        pages.append((page, texts, figs))

    body = size_counter.most_common(1)[0][0] if size_counter else 10.0
    n_pages = len(doc)
    repeated = {k for k, v in edge_counter.items()
                if n_pages >= 3 and v >= max(3, n_pages // 2)}

    heading_sizes = sorted({round(t["size"]) for _, ts, _ in pages for t in ts
                            if t["size"] >= body * 1.15 and len(t["text"]) < 200},
                           reverse=True)
    level = {s: f"h{min(2 + i, 4)}" for i, s in enumerate(heading_sizes)}

    elements: list[dict] = []
    seen_imgs: set[str] = set()
    img_n = 0
    dropcap = ""
    for pno, (page, texts, figs) in enumerate(pages):
        items = [t for t in texts
                 if not (t["edge"] and (norm_repeat(t["text"]) in repeated
                                        or re.fullmatch(r"[\d\s/.\-–|]+|page \d+.*", t["text"], re.I)))]
        items += [{"type": "img", "bbox": tuple(f)} for f in figs]
        for it in reading_order(items, page.rect.width):
            if it["type"] == "img":
                name = f"img-{img_n + 1}.png"
                data = render_region(page, fitz.Rect(it["bbox"]), outdir / name)
                if data is None:
                    continue
                digest = hashlib.md5(data).hexdigest()
                if digest in seen_imgs:  # same logo/figure repeated on many pages
                    (outdir / name).unlink()
                    continue
                seen_imgs.add(digest)
                img_n += 1
                elements.append({"kind": "img", "src": name, "page": pno, "y": it["bbox"][1]})
                continue
            t = it["text"]
            if dropcap:
                t, dropcap = dropcap + t, ""
            elif re.fullmatch(r"[A-ZÀ-Ý]", t) and it["size"] > 2 * body:
                dropcap = t
                continue
            r = round(it["size"])
            pos = {"page": pno, "y": it["bbox"][1]}
            if r in level and len(t) < 200:
                elements.append({"kind": level[r], "text": t, "sized": True, **pos})
            elif it["bold"] and len(t) < 120 and not t.endswith((".", ",", ";")):
                elements.append({"kind": "h4", "text": t, **pos})
            elif elements and elements[-1]["kind"] == "p" and elements[-1]["text"].endswith(GLUE):
                elements[-1]["text"] = elements[-1]["text"][:-1] + t
            elif elements and elements[-1]["kind"] == "p" and _continues(elements[-1]["text"], t):
                # paragraph split across a column / page break
                prev = elements[-1]["text"]
                elements[-1]["text"] = (prev[:-1] + t) if prev.endswith("-") else f"{prev} {t}"
            else:
                elements.append({"kind": "p", "text": t, **pos})
    doc.close()
    return elements


# --------------------------------------------------------------------------- splitting

def _split_at(elements: list[dict], starts: list[tuple[int, float, str]]) -> list[tuple[str, list[dict]]]:
    """Cut the element stream at (page, y) positions. Content before the first cut
    (front matter) joins the first part; empty parts are dropped."""
    parts: list[tuple[str, list[dict]]] = []
    starts = sorted(starts, key=lambda s: (s[0], s[1]))
    i = 0
    for k, (page, y, name) in enumerate(starts):
        nxt = starts[k + 1] if k + 1 < len(starts) else None
        chunk = []
        while i < len(elements):
            el = elements[i]
            if nxt and (el["page"], el["y"]) >= (nxt[0], nxt[1] - 2):
                break
            chunk.append(el)
            i += 1
        if chunk:
            parts.append((name, chunk))
    return parts


def split_by_bookmarks(doc: fitz.Document, elements: list[dict]) -> list:
    toc = doc.get_toc(simple=False)
    if not toc:
        return []
    top = min(e[0] for e in toc)
    entries = [e for e in toc if e[0] == top]
    if len(entries) == 1:  # a single root bookmark (the book itself): use its children
        entries = [e for e in toc if e[0] == top + 1]
    starts = []
    for lvl, name, page, dest in entries:
        if page < 1:
            continue
        to = dest.get("to") if isinstance(dest, dict) else None
        y = float(to.y) if to is not None and 0 <= to.y <= doc[page - 1].rect.height else 0.0
        starts.append((page - 1, y, clean(name).strip()))
    if len(starts) < 2:
        return []
    starts[0] = (0, 0.0, starts[0][2])  # front matter belongs to the first part
    return _split_at(elements, starts)


def split_by_headings(elements: list[dict], n_pages: int) -> list:
    """Split at the largest font-size heading level that marks at least two
    chapters; adjacent heading blocks (a heading broken over lines) count once."""
    for kind in ("h2", "h3", "h4"):
        starts, prev_heading = [], False
        for el in elements:
            is_h = el.get("sized") and el["kind"] == kind
            if is_h and prev_heading:
                starts[-1] = (starts[-1][0], starts[-1][1], f"{starts[-1][2]} {el['text']}")
            elif is_h:
                starts.append((el["page"], el["y"], el["text"]))
            prev_heading = is_h
        # sections on (almost) every page are not chapters
        if 2 <= len(starts) <= max(2, n_pages // 2):
            starts[0] = (0, 0.0, starts[0][2])
            parts = _split_at(elements, starts)
            if len(parts) >= 2:
                return parts
        if len(starts) > 1:
            return []  # too frequent to be chapters; smaller levels would be worse
        # 0 or 1 heading at this level (e.g. only the document title): try the next
    return []


def split_by_pages(elements: list[dict], n_pages: int) -> list:
    n = ceil(n_pages / CHUNK_PAGES)
    size = ceil(n_pages / n)
    return _split_at(elements, [(k * size, 0.0, "") for k in range(n)])


def split_parts(doc: fitz.Document, elements: list[dict]) -> tuple[list, str]:
    for method, parts in (("bookmarks", lambda: split_by_bookmarks(doc, elements)),
                          ("headings", lambda: split_by_headings(elements, len(doc))),
                          ("pages", lambda: split_by_pages(elements, len(doc)))):
        found = parts()
        if len(found) >= 2:
            return found, method
    return [("", elements)], "none"


def _continues(prev: str, nxt: str) -> bool:
    return bool(prev) and not re.search(r"[.!?:;\"”)]$", prev) and nxt[:1].islower()


# --------------------------------------------------------------------------- rendering

env = Environment(loader=FileSystemLoader(TEMPLATES),
                  autoescape=select_autoescape(["html", "xml"]))


def render_body(elements: list[dict], title: str, ocr: bool,
                nav: list[tuple[str, str]] | None = None, pdf_href: str = "original.pdf") -> str:
    """Article body fragment with relative URLs (img-N.png, original.pdf).
    `nav` is a list of (label, href) links shown at the top and bottom (parts)."""
    out = []
    nav_html = ""
    if nav:
        nav_html = '<p class="nav">' + " · ".join(
            f'<a href="{html.escape(h)}">{html.escape(l)}</a>' if h else html.escape(l)
            for l, h in nav) + "</p>"
        out.append(nav_html)
    if ocr:
        out.append('<p><em>OCR applied: some pages were scanned images; their text '
                   'was recognised automatically and may contain errors.</em></p>')
    merged: list[dict] = []
    for el in elements:
        prev = merged[-1] if merged else None
        if (prev and el["kind"] != "img" and el["kind"].startswith("h") and prev["kind"] == el["kind"]
                and (el["text"][:1].islower() or not re.search(r"[.!?:]$", prev["text"]))):
            prev["text"] = f'{prev["text"]} {el["text"]}'
        else:
            merged.append(dict(el))
    norm = lambda t: re.sub(r"\W+", " ", t).strip().lower()
    title_done = False
    for el in merged:
        if el["kind"] == "img":
            out.append(f'<figure><img src="{el["src"]}" alt="" loading="lazy"></figure>')
        elif not title_done and el["kind"].startswith("h") and norm(el["text"]) == norm(title):
            title_done = True  # don't repeat the title
        else:
            for part in el["text"].replace(GLUE, "").split(PARA_BREAK):
                out.append(f'<{el["kind"]}>{html.escape(part)}</{el["kind"]}>')
    out.append("<hr>")
    if nav_html:
        out.append(nav_html)
    out.append(f'<p><a href="{pdf_href}">Original PDF</a></p>')
    return "\n".join(out) + "\n"


def absolutize(fragment: str, base: str) -> str:
    return re.sub(r'(src|href)="(?![a-z][a-z0-9+.-]*:|/|#)([^"]+)"',
                  lambda m: f'{m.group(1)}="{urljoin(base, m.group(2))}"', fragment)


def write_article_page(a: dict, body: str, part: dict | None = None) -> None:
    d = SITE / "articles" / a["slug"]
    if part:
        d /= part["path"]
    page = env.get_template("article.html").render(
        title=part["title"] if part else a["title"], author=a.get("author", ""),
        date=part["date"] if part else a["date"], body=body, site_title=SITE_TITLE,
        root="../../../" if part else "../../")
    (d / "index.html").write_text(page, encoding="utf-8")


def write_parts_index(a: dict) -> None:
    """articles/<slug>/ for a split PDF: list of its parts."""
    page = env.get_template("parts.html").render(a=a, site_title=SITE_TITLE)
    (SITE / "articles" / a["slug"] / "index.html").write_text(page, encoding="utf-8")


def render_all_pages(articles: list[dict]) -> None:
    """Re-render every page from stored fragments so template changes apply to all."""
    for a in articles:
        if not (SITE / "articles" / a["slug"]).is_dir():
            continue
        if a.get("parts"):
            for part in a["parts"]:
                frag = CONTENT / a["slug"] / f"{part['path']}.html"
                if frag.exists() and (SITE / "articles" / a["slug"] / part["path"]).is_dir():
                    write_article_page(a, frag.read_text(encoding="utf-8"), part)
            write_parts_index(a)
        else:
            frag = CONTENT / f"{a['slug']}.html"
            if frag.exists():
                write_article_page(a, frag.read_text(encoding="utf-8"))


def feed_items(articles: list[dict]) -> list[dict]:
    """One feed item per article, or per part for split PDFs; newest first."""
    items = []
    for a in articles:
        if a.get("parts"):
            for part in a["parts"]:
                items.append({"path": f"{a['slug']}/{part['path']}/", "title": part["title"],
                              "author": a.get("author", ""), "date": part["date"],
                              "frag": CONTENT / a["slug"] / f"{part['path']}.html",
                              "pdf": SITE / "articles" / a["slug"] / "original.pdf",
                              "pdf_href": "../original.pdf"})
        else:
            items.append({"path": f"{a['slug']}/", "title": a["title"],
                          "author": a.get("author", ""), "date": a["date"],
                          "frag": CONTENT / f"{a['slug']}.html",
                          "pdf": SITE / "articles" / a["slug"] / "original.pdf",
                          "pdf_href": "original.pdf"})
    items.sort(key=lambda i: i["date"], reverse=True)
    return items


def repo_and_branch() -> tuple[str, str]:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "")
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                  text=True, check=True).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return ""
    if not repo:
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", git("remote", "get-url", "origin"))
        repo = m.group(1) if m else ""
    return repo, branch or git("rev-parse", "--abbrev-ref", "HEAD") or "main"


def write_index(articles: list[dict], base: str) -> None:
    page = env.get_template("index.html").render(articles=articles, site_title=SITE_TITLE)
    (SITE / "index.html").write_text(page, encoding="utf-8")
    # public list used by the upload page to show progress (same origin, no rate limit)
    public = [{k: a.get(k, "") for k in ("slug", "title", "author", "date")} for a in articles]
    (SITE / "articles.json").write_text(json.dumps(public, ensure_ascii=False), encoding="utf-8")
    repo, branch = repo_and_branch()
    if repo:
        (SITE / "upload").mkdir(exist_ok=True)
        page = env.get_template("upload.html").render(
            repo=repo, branch=branch, feed_url=f"{base}/feed.xml", site_title=SITE_TITLE)
        (SITE / "upload" / "index.html").write_text(page, encoding="utf-8")


def write_feed(articles: list[dict], base: str) -> None:
    fg = FeedGenerator()
    fg.id(f"{base}/")
    fg.title(SITE_TITLE)
    fg.author({"name": FEED_AUTHOR})
    fg.link(href=f"{base}/", rel="alternate")
    fg.link(href=f"{base}/feed.xml", rel="self")
    fg.language("hu")
    newest = feed_items(articles)[:FEED_LIMIT]
    entry_links = []
    fg.updated(datetime.fromisoformat(newest[0]["date"]) if newest
               else datetime(2000, 1, 1, tzinfo=timezone.utc))
    for a in newest:
        url = f"{base}/articles/{a['path']}"
        frag = a["frag"].read_text(encoding="utf-8") if a["frag"].exists() else ""
        date = datetime.fromisoformat(a["date"])
        fe = fg.add_entry(order="append")
        fe.id(url)
        fe.title(a["title"])
        if a.get("author"):
            fe.author({"name": a["author"]})
        pdf = a["pdf"]
        links = [{"href": url, "rel": "alternate", "type": "text/html"},
                 {"href": urljoin(url, a["pdf_href"]), "rel": "enclosure", "type": "application/pdf",
                  "title": "Original PDF",
                  "length": str(pdf.stat().st_size) if pdf.exists() else "0"}]
        fe.link(links)
        entry_links.append(links)
        fe.published(date)
        fe.updated(date)
        fe.content(absolutize(frag, url), type="html")
    # feedgen 1.0 loses rel/type/length/title on <entry><link>; restore them.
    root = etree.fromstring(fg.atom_str(pretty=False))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry, links in zip(root.findall("a:entry", ns), entry_links):
        for el, attrs in zip(entry.findall("a:link", ns), links):
            for k, v in attrs.items():
                el.set(k, v)
    etree.indent(root, space="  ")
    (SITE / "feed.xml").write_bytes(
        etree.tostring(root, xml_declaration=True, encoding="UTF-8") + b"\n")


# --------------------------------------------------------------------------- main

def unique_slug(title: str, digest: str, taken: set[str]) -> str:
    slug = slugify(title)
    if slug in taken or (SITE / "articles" / slug).exists():
        slug = f"{slug}-{digest[:6]}"
    return slug


def move_to_processed(pdf: Path, digest: str) -> str:
    PROCESSED.mkdir(exist_ok=True)
    dest = PROCESSED / pdf.name
    if dest.exists():
        if sha256(dest) == digest:
            pdf.unlink()
            return dest.name
        dest = PROCESSED / f"{pdf.stem}-{digest[:6]}{pdf.suffix}"
    shutil.move(str(pdf), dest)
    return dest.name


def process(pdf: Path, articles: list[dict]) -> dict:
    digest = sha256(pdf)
    date = git_commit_date(pdf)
    with tempfile.TemporaryDirectory() as tmp:
        src, ocr_pages = ocr_if_needed(pdf, Path(tmp))
        with fitz.open(src) as doc:
            title = detect_title(doc, pdf.stem.replace("_", " ").strip(), 0 in ocr_pages)
            author = detect_author(doc, title, 0 in ocr_pages)
        slug = unique_slug(title, digest, {a["slug"] for a in articles})
        outdir = SITE / "articles" / slug
        work = Path(tmp) / "img"
        work.mkdir()
        outdir.mkdir(parents=True, exist_ok=True)
        try:
            elements = extract(src, work, ocr_pages)
            article = {"slug": slug, "title": title, "author": author, "date": date.isoformat(),
                       "source": pdf.name, "sha256": digest, "ocr": bool(ocr_pages)}
            with fitz.open(src) as doc:
                n_pages = len(doc)
                parts, method = (split_parts(doc, elements) if n_pages >= SPLIT_MIN_PAGES
                                 else ([("", elements)], "none"))
            if len(parts) > 1:
                print(f"  {n_pages} pages -> {len(parts)} parts (split by {method})")
                write_parts(article, parts, work, date)
            else:
                for f in work.iterdir():
                    shutil.move(str(f), outdir / f.name)
                body = render_body(elements, title, bool(ocr_pages))
                CONTENT.mkdir(parents=True, exist_ok=True)
                (CONTENT / f"{slug}.html").write_text(body, encoding="utf-8")
                write_article_page(article, body)
        except Exception:
            shutil.rmtree(outdir, ignore_errors=True)  # retried on the next run
            shutil.rmtree(CONTENT / slug, ignore_errors=True)
            raise
    shutil.copy2(pdf, outdir / "original.pdf")
    article["source"] = move_to_processed(pdf, digest)
    return article


def write_parts(article: dict, parts: list, work: Path, date: datetime) -> None:
    """Write site/articles/<slug>/part-NN/ for each part, with its own figures,
    previous/next links and timestamps one minute apart (part 1 oldest)."""
    slug, title, m = article["slug"], article["title"], len(parts)
    norm = lambda t: re.sub(r"\W+", " ", t).strip().lower()
    meta = []
    for n, (chapter, _) in enumerate(parts, 1):
        chapter = re.sub(r"\s+", " ", chapter).strip()
        if norm(chapter) == norm(title):
            chapter = ""  # the first chapter is often the paper's own title
        full = f"{title} — Part {n}/{m}" + (f": {chapter}" if chapter else "")
        meta.append({"n": n, "path": f"part-{n:02d}", "title": full, "chapter": chapter,
                     "date": (date + timedelta(minutes=n - 1)).isoformat()})
    (CONTENT / slug).mkdir(parents=True, exist_ok=True)
    for i, (part, (_, els)) in enumerate(zip(meta, parts)):
        d = SITE / "articles" / slug / part["path"]
        d.mkdir(parents=True, exist_ok=True)
        els = [dict(e) for e in els]
        k = 0
        for e in els:  # each part gets its own img-1.png, img-2.png, ...
            if e["kind"] == "img":
                k += 1
                shutil.copy2(work / e["src"], d / f"img-{k}.png")
                e["src"] = f"img-{k}.png"
        label = lambda p: f"Part {p['n']}" + (f": {p['chapter']}" if p["chapter"] else "")
        nav = [(f"Part {part['n']} of {m}", "../")]
        if i > 0:
            nav.append((f"← {label(meta[i - 1])}", f"../{meta[i - 1]['path']}/"))
        if i + 1 < m:
            nav.append((f"{label(meta[i + 1])} →", f"../{meta[i + 1]['path']}/"))
        skip = part["chapter"] or title
        body = render_body(els, skip, article["ocr"] and i == 0, nav, "../original.pdf")
        (CONTENT / slug / f"{part['path']}.html").write_text(body, encoding="utf-8")
        write_article_page(article, body, part)
    article["parts"] = meta
    write_parts_index(article)


def backfill_authors(articles: list[dict]) -> None:
    """Articles built before author detection existed have no 'author' key."""
    for a in articles:
        if "author" in a:
            continue
        pdf = PROCESSED / a["source"]
        a["author"] = ""
        if pdf.exists():
            with fitz.open(pdf) as doc:
                a["author"] = detect_author(doc, a["title"], a.get("ocr", False))
            print(f"author for {a['slug']}: {a['author'] or '(none found)'}")


def main() -> int:
    base = site_url()
    articles = load_articles()
    known = {a.get("sha256") for a in articles}
    failures = 0

    for pdf in sorted(p for p in INBOX.glob("*") if p.suffix.lower() == ".pdf"):
        digest = sha256(pdf)
        if digest in known:
            print(f"skip (already processed): {pdf.name}")
            move_to_processed(pdf, digest)
            continue
        print(f"processing: {pdf.name}")
        try:
            a = process(pdf, articles)
        except Exception as e:  # noqa: BLE001 - keep going, report at the end
            failures += 1
            print(f"::error file=inbox/{pdf.name}::{type(e).__name__}: {e}")
            continue
        articles.append(a)
        known.add(digest)
        save_articles(articles)
        print(f"  -> articles/{a['slug']}/  ({a['title']})"
              + (f", {len(a['parts'])} parts" if a.get("parts") else ""))

    backfill_authors(articles)
    save_articles(articles)
    SITE.mkdir(exist_ok=True)
    render_all_pages(articles)
    write_index(articles, base)
    write_feed(articles, base)
    print(f"feed: {base}/feed.xml ({min(len(feed_items(articles)), FEED_LIMIT)} entries)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
