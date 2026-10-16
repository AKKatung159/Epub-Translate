# EPUB Translate (English → Thai)

Web app that translates an English EPUB into Thai with local models, keeping the book's layout:
images, CSS, chapters, links, italics and the table of contents stay where they are; only the text changes.

Runs in the homelab (Proxmox VM `apps`, http://epub.lan) and uses `llama-server` on a ROG Ally as the model
server (OpenAI-compatible API). See `docs/17-epub-translate.md` in the homelab repo.

## What it does

1. **Upload** an `.epub` (drag and drop, upload progress bar).
2. **Cut into paragraphs.** Every leaf block (`<p>`, headings, list items, table cells, TOC labels, `<title>`)
   becomes one segment. Inline markup is turned into numbered markers (`[1]…[/1]`, `[2/]`) so the model can
   move it with the words; it is rebuilt into the original elements afterwards.
3. **Glossary.** Capitalised words that are not just sentence starts become candidates (names, places, titles,
   invented terms). Typhoon 2.1 sorts them (person/place/title/term, or "not a term"); Typhoon Translate gives
   one Thai form each (Royal Institute transliteration for invented names, meaning for ordinary words used as
   names). Multi-word names are rebuilt from their parts so they always agree. **You review and edit the table
   before translating.** For **book 2 of a series**, pick the earlier book on upload (or "นำเข้าจากเล่มก่อน" in the
   glossary tab): its names, Thai forms, character sheet and book profile carry over, entries are added only when
   this book mentions them, and anything typed in this book is never overwritten.
4. **Translate** in chunks of consecutive paragraphs (one chapter at a time), with the glossary entries that
   occur in the chunk passed as hard rules (`Translate "Maia" as "ไมอา"`). **Speakers:** a quote whose speaker is
   named next to a speech verb (`“…,” said Csevet`) is checked against that character's ending particle; a
   quote-only paragraph in a back-and-forth gets the speaker from two paragraphs back, told to the model.
   `he said` / `she said` are not guessed (tested on a novel: the point-of-view character is "he" for pages
   without being named, so the guess was often wrong).
5. **Check every paragraph**: really Thai, no English sentence left behind, sane length, no repetition loop, no
   leaked markers, glossary names enforced, formatting markers intact. Failures are retried one by one; if only
   italics/links break, the words are kept (noted); wrong content is flagged for review.
6. **Review** flagged paragraphs (edit or re-translate), preview chapters side by side.
7. **Download** the Thai EPUB (also mid-way: the job page shows up to which chapter it reads without gaps), or a bilingual edition (English paragraph
   in grey above each Thai one). Thai fonts and line height are added; `dc:language` becomes `th`.

Jobs survive restarts and wait (with a message) when the model server is asleep; pause / resume / cancel any time.
With `DISCORD_WEBHOOK_URL` set, Discord gets: glossary ready to review, 25 / 50 / 75 % (with ETA and readable
chapters), done, no model server for 5 minutes (and back), errors.

## PDF → Xteink X4 (menu "PDF → Xteink")

Converts a PDF into an `.xtch` (4 greys) or `.xtc` (black/white) file for the Xteink X4 with the CrossPoint
firmware, which cannot render PDF itself (ESP32-C3, ~380 KB RAM) but shows pre-rendered 480×800 pages instantly.
No model involved; it runs on its own queue, so it never waits behind a translation.

- **Landscape strips** (default): each page is rendered, running header and page number dropped, white margins
  cropped, scaled to 800 px wide and cut into 480-px strips in the white gap between two lines (never through a
  line when a gap exists). Strips flow across PDF pages so none is half empty; a chapter starts on a fresh strip.
  Read with the device turned sideways: text ~1.67× larger than a whole page upright.
- **At most 3 / 4 strips per page**: smaller text on big pages, fewer page turns. **Whole page**: pictures, forms.
- PDF bookmarks become the chapter menu (top two levels, at most 64); without bookmarks, evenly spaced entries.
- **Black/white (.xtc) by default.** The firmware mallocs one whole page each turn: 48 KB for XTC, 96 KB for
  4-grey XTCH. A 70 MB XTCH book gave "Memory error" on the X4 (no 96 KB block left in a fragmented heap); XTC
  halves both the request and the file. Strokes are thresholded at 170/255 so small text stays whole.
- Preview any device page in the browser and convert again with other options from the same upload.
- Format written from the firmware's own parser (`lib/Xtc`): header 56 bytes, title/author, 96-byte chapter
  records, page table, XTG/XTH pages (XTH: two bit planes, column-major from the right edge; 0 white, 1 dark grey,
  2 light grey, 3 black). `tests/test_pdfxtc.py` decodes pages the way the firmware does.

Speed: ~25 PDF pages per second (measured on the ROG Ally; the apps VM may be slower); an A5 novel page → ~2.4 device pages (~48 KB each in black/white, ~95 KB in 4 greys).

## Models

| Use | Model | Default name |
| --- | --- | --- |
| Translation, glossary Thai forms | [Typhoon Translate 1.5 4B](https://huggingface.co/typhoon-ai/typhoon-translate1.5-4b-gguf) Q4_K_M | `typhoon-translate1.5-4b` |
| Glossary classification | Typhoon 2.1 Gemma3 4B Q4_K_M | `typhoon2.1-4b` |

Both are served by one `llama-server` in router mode (`--models-preset models.ini`), which loads them on demand.
On the Ally (Radeon 780M, Vulkan) the app keeps 4 chunks in flight (`TRANSLATE_PARALLEL`, = llama-server `parallel`):
4 sequences batch to ~64–82 tok/s total vs ~25 for one, so ~45 English characters per second end to end:
a 160k-character book takes ~1 h, a 800k-character novel ~5 h.

## Configuration (environment)

| Variable | Default | |
| --- | --- | --- |
| `LLM_BASE_URL` | `http://192.168.0.191:8080/v1` | OpenAI-compatible endpoint |
| `LLM_API_KEY` | (empty) | llama-server `--api-key` |
| `TRANSLATE_MODEL` / `GLOSSARY_MODEL` | see above | model names in the router |
| `CHUNK_CHARS` | `2400` | English characters per request |
| `DATA_DIR` | `/data` (image) | SQLite DB + uploaded books |
| `MAX_UPLOAD_MB` | `100` | |
| `TRANSLATE_PARALLEL` | `4` | chunks in flight (= llama-server `parallel`) |
| `DISCORD_WEBHOOK_URL` | (empty = off) | notifications |
| `MAX_PDF_MB` | `300` | PDF upload limit (streamed to disk) |
| `PUBLIC_URL` | (empty) | e.g. `http://epub.lan`, makes the Discord titles links to the job |

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest -q tests
LLM_BASE_URL=http://127.0.0.1:8080/v1 LLM_API_KEY=... .venv/bin/uvicorn app.main:app --port 8765
```

Image: `ghcr.io/akkatung159/epub-translate` (built by `.github/workflows/image.yml` on every push to main).

Only translate books you have the right to translate; the output is for personal use.
