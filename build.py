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

def block_text(block: dict) -> str:
    lines = []
    for line in block["lines"]:
        t = "".join(s["text"] for s in line["spans"]).strip()
        if t:
            lines.append(t)
    out = ""
    for t in lines:
        if not out:
            out = t
        elif re.search(r"\w-$", out) and t[:1].islower():   # de-hyphenate
            out = out[:-1] + t
        else:
            out += " " + t
    return re.sub(r"\s+", " ", out).strip()


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
    junk = re.compile(r"^(untitled|microsoft (word|powerpoint)|document\d*$|.*\.(docx?|pptx?|pdf|tex|indd)$)", re.I)
    if len(meta) >= 3 and not junk.match(meta):
        return meta
    if len(doc) and not ocr_first_page:  # OCR font sizes are not meaningful
        blocks = [b for b in doc[0].get_text("dict")["blocks"] if b["type"] == 0]
        best_size, parts = 0.0, []
        for b in blocks:
            for l in b["lines"]:
                for s in l["spans"]:
                    t = s["text"].strip()
                    if not t:
                        continue
                    size = round(s["size"], 1)
                    if size > best_size + 0.05:
                        best_size, parts = size, [t]
                    elif abs(size - best_size) <= 0.05:
                        parts.append(t)
        title = re.sub(r"\s+", " ", " ".join(parts)).strip()
        if len(title) >= 3 and re.search(r"\w", title):
            return title[:200]
    return fallback


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


def save_image(doc: fitz.Document, xref: int, dest: Path) -> bool:
    try:
        pix = fitz.Pixmap(doc, xref)
        smask = doc.xref_get_key(xref, "SMask")
        if smask[0] == "xref":
            try:
                pix = fitz.Pixmap(pix, fitz.Pixmap(doc, int(smask[1].split()[0])))
            except Exception:
                pass
        if pix.colorspace and pix.colorspace.n not in (1, 3):
            pix = fitz.Pixmap(fitz.csRGB, pix)
        elif pix.colorspace is None and not pix.alpha:  # stencil mask etc.
            return False
        pix.save(dest)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    image xref {xref} skipped: {e}")
        return False


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
            if pno not in ocr_pages:
                for l in b["lines"]:
                    for s in l["spans"]:
                        size_counter[round(s["size"], 1)] += len(s["text"].strip())
            edge = b["bbox"][1] < 0.08 * h or b["bbox"][3] > 0.92 * h
            if edge:
                edge_counter[norm_repeat(t)] += 1
            ocr = pno in ocr_pages  # OCR text has no reliable size/weight
            texts.append({"type": "text", "bbox": b["bbox"], "text": t,
                          "size": 0.0 if ocr else block_size(b),
                          "bold": False if ocr else block_bold(b), "edge": edge})
        pages.append((page, texts))

    body = size_counter.most_common(1)[0][0] if size_counter else 10.0
    n_pages = len(doc)
    repeated = {k for k, v in edge_counter.items()
                if n_pages >= 3 and v >= max(3, n_pages // 2)}

    heading_sizes = sorted({round(t["size"]) for _, ts in pages for t in ts
                            if t["size"] >= body * 1.15 and len(t["text"]) < 200},
                           reverse=True)
    level = {s: f"h{min(2 + i, 4)}" for i, s in enumerate(heading_sizes)}

    elements: list[dict] = []
    seen_xrefs: set[int] = set()
    img_n = 0
    for pno, (page, texts) in enumerate(pages):
        items = [t for t in texts
                 if not (t["edge"] and (norm_repeat(t["text"]) in repeated
                                        or re.fullmatch(r"[\d\s/.\-–|]+|page \d+.*", t["text"], re.I)))]
        page_area = page.rect.width * page.rect.height
        for img in page.get_images(full=True):
            xref, w, hgt = img[0], img[2], img[3]
            if xref in seen_xrefs or w < MIN_IMG_PX or hgt < MIN_IMG_PX:
                continue
            rects = page.get_image_rects(xref)
            rect = rects[0] if rects else fitz.Rect(0, page.rect.height, page.rect.width, page.rect.height)
            if rect.width < 2 and rect.height < 2:
                continue
            # On OCRed pages the full-page scan is the text itself, not a figure.
            if pno in ocr_pages and rect.get_area() > 0.6 * page_area:
                continue
            seen_xrefs.add(xref)
            items.append({"type": "img", "bbox": tuple(rect), "xref": xref})
        for it in reading_order(items, page.rect.width):
            if it["type"] == "img":
                name = f"img-{img_n + 1}.png"
                if save_image(doc, it["xref"], outdir / name):
                    img_n += 1
                    elements.append({"kind": "img", "src": name})
                continue
            t = it["text"]
            r = round(it["size"])
            if r in level and len(t) < 200:
                elements.append({"kind": level[r], "text": t})
            elif it["bold"] and len(t) < 120 and not t.endswith((".", ",", ";")):
                elements.append({"kind": "h4", "text": t})
            elif elements and elements[-1]["kind"] == "p" and _continues(elements[-1]["text"], t):
                # paragraph split across a column / page break
                prev = elements[-1]["text"]
                elements[-1]["text"] = (prev[:-1] + t) if prev.endswith("-") else f"{prev} {t}"
            else:
                elements.append({"kind": "p", "text": t})
    doc.close()
    return elements


def _continues(prev: str, nxt: str) -> bool:
    return bool(prev) and not re.search(r"[.!?:;\"”)]$", prev) and nxt[:1].islower()


# --------------------------------------------------------------------------- rendering

env = Environment(loader=FileSystemLoader(TEMPLATES),
                  autoescape=select_autoescape(["html", "xml"]))


def render_body(elements: list[dict], title: str, ocr: bool) -> str:
    """Article body fragment with relative URLs (img-N.png, original.pdf)."""
    out = []
    if ocr:
        out.append('<p><em>OCR applied: some pages were scanned images; their text '
                   'was recognised automatically and may contain errors.</em></p>')
    for i, el in enumerate(elements):
        if el["kind"] == "img":
            out.append(f'<figure><img src="{el["src"]}" alt="" loading="lazy"></figure>')
        elif i == 0 and el["kind"].startswith("h") and el["text"].strip().lower() == title.strip().lower():
            continue  # don't repeat the title
        else:
            out.append(f'<{el["kind"]}>{html.escape(el["text"])}</{el["kind"]}>')
    out.append('<hr><p><a href="original.pdf">Original PDF</a></p>')
    return "\n".join(out) + "\n"


def absolutize(fragment: str, base: str) -> str:
    return re.sub(r'(src|href)="(?![a-z][a-z0-9+.-]*:|/|#)([^"]+)"',
                  lambda m: f'{m.group(1)}="{base}{m.group(2)}"', fragment)


def write_article_page(a: dict, body: str) -> None:
    page = env.get_template("article.html").render(
        title=a["title"], date=a["date"], body=body, site_title=SITE_TITLE)
    (SITE / "articles" / a["slug"] / "index.html").write_text(page, encoding="utf-8")


def write_index(articles: list[dict]) -> None:
    page = env.get_template("index.html").render(articles=articles, site_title=SITE_TITLE)
    (SITE / "index.html").write_text(page, encoding="utf-8")


def write_feed(articles: list[dict], base: str) -> None:
    fg = FeedGenerator()
    fg.id(f"{base}/")
    fg.title(SITE_TITLE)
    fg.author({"name": FEED_AUTHOR})
    fg.link(href=f"{base}/", rel="alternate")
    fg.link(href=f"{base}/feed.xml", rel="self")
    fg.language("hu")
    newest = articles[:FEED_LIMIT]
    entry_links = []
    fg.updated(datetime.fromisoformat(newest[0]["date"]) if newest
               else datetime(2000, 1, 1, tzinfo=timezone.utc))
    for a in newest:
        url = f"{base}/articles/{a['slug']}/"
        frag_path = CONTENT / f"{a['slug']}.html"
        frag = frag_path.read_text(encoding="utf-8") if frag_path.exists() else ""
        date = datetime.fromisoformat(a["date"])
        fe = fg.add_entry(order="append")
        fe.id(url)
        fe.title(a["title"])
        pdf = SITE / "articles" / a["slug"] / "original.pdf"
        links = [{"href": url, "rel": "alternate", "type": "text/html"},
                 {"href": f"{url}original.pdf", "rel": "enclosure", "type": "application/pdf",
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
        slug = unique_slug(title, digest, {a["slug"] for a in articles})
        outdir = SITE / "articles" / slug
        outdir.mkdir(parents=True, exist_ok=True)
        try:
            elements = extract(src, outdir, ocr_pages)
        except Exception:
            shutil.rmtree(outdir, ignore_errors=True)  # retried on the next run
            raise
    shutil.copy2(pdf, outdir / "original.pdf")
    body = render_body(elements, title, bool(ocr_pages))
    CONTENT.mkdir(parents=True, exist_ok=True)
    (CONTENT / f"{slug}.html").write_text(body, encoding="utf-8")
    article = {"slug": slug, "title": title, "date": date.isoformat(),
               "source": pdf.name, "sha256": digest, "ocr": bool(ocr_pages)}
    write_article_page(article, body)
    article["source"] = move_to_processed(pdf, digest)
    return article


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
        print(f"  -> articles/{a['slug']}/  ({a['title']})")

    save_articles(articles)
    SITE.mkdir(exist_ok=True)
    write_index(articles)
    write_feed(articles, base)
    print(f"feed: {base}/feed.xml ({min(len(articles), FEED_LIMIT)} entries)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
