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
   before translating.**
4. **Translate** in chunks of consecutive paragraphs (one chapter at a time), with the glossary entries that
   occur in the chunk passed as hard rules (`Translate "Maia" as "ไมอา"`).
5. **Check every paragraph**: really Thai, no English sentence left behind, sane length, no repetition loop, no
   leaked markers, glossary names enforced, formatting markers intact. Failures are retried one by one; if only
   italics/links break, the words are kept (noted); wrong content is flagged for review.
6. **Review** flagged paragraphs (edit or re-translate), preview chapters side by side.
7. **Download** the Thai EPUB (also mid-way, partially translated), or a bilingual edition (English paragraph
   in grey above each Thai one). Thai fonts and line height are added; `dc:language` becomes `th`.

Jobs survive restarts and wait (with a message) when the model server is asleep; pause / resume / cancel any time.

## Models

| Use | Model | Default name |
| --- | --- | --- |
| Translation, glossary Thai forms | [Typhoon Translate 1.5 4B](https://huggingface.co/typhoon-ai/typhoon-translate1.5-4b-gguf) Q4_K_M | `typhoon-translate1.5-4b` |
| Glossary classification | Typhoon 2.1 Gemma3 4B Q4_K_M | `typhoon2.1-4b` |

Both are served by one `llama-server` in router mode (`--models-preset models.ini`), which loads them on demand.
On the Ally (Radeon 780M, Vulkan) translation runs at ~24 tok/s, about 28 English characters per second:
a 160k-character book takes ~1.5 h, a 800k-character novel ~8 h.

## Configuration (environment)

| Variable | Default | |
| --- | --- | --- |
| `LLM_BASE_URL` | `http://192.168.0.191:8080/v1` | OpenAI-compatible endpoint |
| `LLM_API_KEY` | (empty) | llama-server `--api-key` |
| `TRANSLATE_MODEL` / `GLOSSARY_MODEL` | see above | model names in the router |
| `CHUNK_CHARS` | `2400` | English characters per request |
| `DATA_DIR` | `/data` (image) | SQLite DB + uploaded books |
| `MAX_UPLOAD_MB` | `100` | |

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest -q tests
LLM_BASE_URL=http://127.0.0.1:8080/v1 LLM_API_KEY=... .venv/bin/uvicorn app.main:app --port 8765
```

Image: `ghcr.io/akkatung159/epub-translate` (built by `.github/workflows/image.yml` on every push to main).

Only translate books you have the right to translate; the output is for personal use.
