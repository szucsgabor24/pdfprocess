# PDF → Atom feed

Drop a PDF into `inbox/`; a GitHub Action turns it into an article (text + figures)
on GitHub Pages and adds it to an Atom feed you can follow in Inoreader.

## One-time setup

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
2. **Settings → Actions → General → Workflow permissions:** make sure *Read and write
   permissions* is allowed (the workflow commits processed files back; it also declares
   `contents: write` itself, so this only matters if your org restricts it).
3. Feed URL — subscribe to it in Inoreader (*Add subscription* → paste URL):

   ```
   https://szucsgabor24.github.io/pdfprocess/feed.xml
   ```

   Article index: `https://szucsgabor24.github.io/pdfprocess/`

To change the feed title or author,
set `FEED_TITLE` / `FEED_AUTHOR` as `env:` on the Build step in
`.github/workflows/build.yml`.

## Routine: adding a PDF

1. Open the repo on github.com → `inbox/` → **Add file → Upload files** → drop the
   PDF(s) → **Commit changes** (to the default branch).
2. The **Build feed** workflow runs (Actions tab, ~1–3 min; longer if OCR is needed).
3. The PDF is moved to `processed/`, the article appears under
   `/articles/<slug>/`, and Inoreader picks it up on its next poll.

If a PDF fails, it stays in `inbox/`, the run shows a warning, and it is retried on
the next run (or run **Actions → Build feed → Run workflow** manually).

## What the build does

- Processes only PDFs whose SHA-256 is not yet in `data/articles.json`; reruns and
  re-uploads of the same file never create duplicates.
- **Title:** PDF metadata title → largest-font text on page 1 → file name.
- **Date:** git commit date of the uploaded PDF.
- **Slug:** ASCII from the title (`Őszi jelentés` → `oszi-jelentes`); a short hash is
  appended on collision.
- **Text:** headings and paragraphs rebuilt from PyMuPDF text blocks (larger font →
  heading); page numbers and repeated headers/footers dropped; simple two-column
  reading order.
- **Figures:** embedded raster images ≥ 100×100 px are saved as `img-N.png`, placed in
  reading order; each image appears once (repeated logos are not duplicated).
  Vector-drawn charts are not extracted — they remain in the original PDF.
- **OCR:** if a page has images but no text, `ocrmypdf -l hun+eng --skip-text` runs and
  the article notes "OCR applied". Headings are not detected on OCR pages.
- `original.pdf` is copied next to the article and linked at its end.
- **Feed:** `site/feed.xml`, Atom, newest 100, full HTML content with absolute URLs,
  plus an `enclosure` link to the PDF.

## Layout

```
inbox/               upload here
processed/           PDFs after processing
data/articles.json   metadata (slug, title, date, source, sha256, ocr)
data/content/        article HTML fragments used to (re)build the feed
site/                published site (committed so later runs only add to it)
templates/           Jinja2 templates for article and index pages
build.py             the converter
```

## Local run

```bash
sudo apt install tesseract-ocr tesseract-ocr-hun ghostscript   # for OCR
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
SITE_URL=http://localhost:8000 python build.py
python -m http.server -d site 8000
```
