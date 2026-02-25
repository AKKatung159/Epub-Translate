import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
import requests
import json
import os
import time
import subprocess
import logging
import psutil
import argparse
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import XMLParsedAsHTMLWarning
import warnings

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# ================= CONFIG =================

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")

TRANSLATE_MODEL = "scb10x/typhoon-translate1.5-4b"
POLISH_MODEL = "scb10x/llama3.2-typhoon2-3b-instruct"

MAX_CHARS = 600
BASE_WORKERS = 2
MAX_RAM_PERCENT = 85

CHECKPOINT_FILE = "progress.json"
STATS_FILE = "stats.json"
LOG_FILE = "translate.log"

session = requests.Session()
shutdown_flag = False

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)

# ================= SIGNAL =================

def handle_shutdown(sig, frame):
    global shutdown_flag
    logging.warning("🛑 Graceful shutdown requested...")
    shutdown_flag = True

signal.signal(signal.SIGINT, handle_shutdown)

# ================= UTIL =================

def restart_ollama():
    logging.warning("🔄 Restarting Ollama...")
    subprocess.run(["podman", "restart", "ollama"], check=False)
    time.sleep(8)

def adaptive_workers():
    ram = psutil.virtual_memory().percent
    if ram > 80:
        return 1
    return BASE_WORKERS

def wait_for_ram():
    while psutil.virtual_memory().percent > MAX_RAM_PERCENT:
        logging.warning("⚠️ RAM high, waiting...")
        time.sleep(2)

def is_watermark(text: str) -> bool:
    t = text.strip().lower().replace(" ", "")

    BAD_PATTERNS = [
        "oceanofpdf",
        "www.oceanofpdf",
        "@oceanofpdf",
    ]

    if any(p in t for p in BAD_PATTERNS):
        return True

    if t.endswith(".com") and len(t) < 40:
        return True

    return False

def should_skip_translation(text: str) -> bool:
    t = text.strip()

    if len(t) <= 20:
        return True

    if "." in t and " " not in t:
        return True

    has_alpha = any(c.isalpha() for c in t)
    if not has_alpha:
        return True

    return False

# ================= CHECKPOINT =================

def load_progress():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def atomic_save(data):
    temp = CHECKPOINT_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(temp, CHECKPOINT_FILE)

# ================= QUALITY =================

def is_bad_output(inp, out):
    if not out or not out.strip():
        return True

    if len(inp.strip()) < 30:
        return False

    thai_chars = sum("\u0E00" <= c <= "\u0E7F" for c in out)

    if thai_chars == 0:
        return True

    return False

# ================= OLLAMA =================

def call_ollama(model, system_prompt, text):
    if should_skip_translation(text):
        return text.strip()
    payload = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "options": {
            "num_ctx": 3072,
            "num_thread": min(12, os.cpu_count() or 8),
            "num_batch": 128,
            "temperature": 0.2,
        },
    }

    for _ in range(10):
        try:
            wait_for_ram()
            response = session.post(
                OLLAMA_URL,
                json=payload,
                stream=True,
                timeout=600,
            )
            response.raise_for_status()

            output_parts = []

            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if data.get("done"):
                    break

                msg = data.get("message")
                if msg and "content" in msg:
                    output_parts.append(msg["content"])

            output = "".join(output_parts).strip()
            # Debug
            # logging.debug(f"RAW OUTPUT PREVIEW: {output[:120]!r}")
            # logging.warning(f"OUTPUT SAMPLE: {output[:200]!r}")
            # logging.warning(f"INPUT LEN={len(text)} OUTPUT LEN={len(output)}")
            if is_bad_output(text, output):
                logging.warning("⚠️ Bad output detected, retrying...")
                continue

            return output.strip()

        except Exception as e:
            logging.error(f"Ollama error: {e}")
            restart_ollama()

    raise RuntimeError("Ollama failed")

# ================= PROMPTS =================

TRANSLATE_SYSTEM = """
You are a professional English-to-Thai novel translator.

MISSION:
Translate the input into high-quality Thai novel prose while strictly preserving meaning and structure.

NON-NEGOTIABLE RULES:
- Preserve ALL paragraph breaks exactly.
- Preserve ALL character names exactly as written.
- DO NOT translate names, places, organizations, skills, or special terms that are proper nouns.
- DO NOT invent, infer, or hallucinate any content.
- DO NOT add explanations, notes, or extra words.
- DO NOT summarize or omit any part of the text.
- Maintain the original tone, mood, pacing, and narrative voice.

TERMINOLOGY RULE:
- Technical terms, game terms, system terms, ranks, or untranslatable proper nouns MAY remain in English when necessary.
- When keeping English terms, integrate them naturally into Thai prose.
- Do NOT overuse English if a natural Thai equivalent clearly exists.

STYLE REQUIREMENTS:
- Use smooth, natural, publication-quality Thai novel style.
- Avoid stiff or literal translation.
- Ensure dialogue sounds natural in Thai.
- Preserve emphasis and emotional weight of the original.

OUTPUT RULES (CRITICAL):
- Output Thai text ONLY.
- NO English explanations.
- NO comments.
- NO metadata.
- NO extra formatting beyond original paragraph breaks.

FAIL-SAFE:
If any part is ambiguous, translate conservatively and DO NOT guess new information.
"""

POLISH_SYSTEM = """
You are a professional Thai novel editor.

MISSION:
Improve the Thai text to be more fluent, natural, and publication-ready WITHOUT changing meaning.

NON-NEGOTIABLE RULES:
- Preserve ALL paragraph breaks exactly.
- Preserve ALL character names exactly.
- Preserve all English technical terms that already exist in the text.
- DO NOT add new information.
- DO NOT remove information.
- DO NOT rewrite the story.
- DO NOT change tone or intent.

EDITING SCOPE:
- Improve flow and readability.
- Smooth awkward phrasing.
- Fix grammar and spacing.
- Enhance natural Thai novel rhythm.

STRICT LIMITS:
- Meaning must remain EXACTLY the same.
- Sentence count and paragraph structure must remain aligned.
- No summarization.
- No expansion.

OUTPUT RULES (CRITICAL):
- Output improved Thai text ONLY.
- NO explanations.
- NO comments.
- NO extra text.
"""

# ================= CHUNK =================

def chunk_text(paragraphs):
    chunks = []
    current = ""

    for p in paragraphs:
        if len(current) + len(p) + 2 > MAX_CHARS:
            chunks.append(current)
            current = p
        else:
            current += "\n\n" + p
    if current:
        chunks.append(current)
    return chunks

# ================= PROCESS =================

def process_epub(input_path, output_path, mode):
    book = epub.read_epub(input_path)
    progress = load_progress()

    documents = [
        item for item in book.get_items()
        if item.get_type() == ebooklib.ITEM_DOCUMENT
    ]

    total_chars_estimate = 0

    for item in documents:
        html = item.get_content().decode("utf-8")
        soup_tmp = BeautifulSoup(html, "lxml-xml")
        for p in soup_tmp.find_all("p"):
            text = p.get_text().strip()
            if len(text) > 5 and not is_watermark(text):
                total_chars_estimate += len(text)

    start_time = time.time()
    total_processed = 0

    for doc_i, item in enumerate(documents):

        if shutdown_flag:
            break

        html = item.get_content().decode("utf-8")
        soup = BeautifulSoup(html, "lxml-xml")

        paragraphs = []
        p_tags = soup.find_all("p")

        for p in p_tags:
            text = p.get_text().strip()

            if len(text) <= 5:
                continue

            if is_watermark(text):
                logging.info(f"🧹 Removed watermark: {text[:60]!r}")
                p.decompose()
                continue

            paragraphs.append(text)

        if not paragraphs:
            continue

        chunks = chunk_text(paragraphs)
        results = [""] * len(chunks)
        context_tail = ""

        workers = adaptive_workers()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}

            for i, chunk in enumerate(chunks):

                key = f"{mode}_{doc_i}_{i}"

                if key in progress:
                    results[i] = progress[key]
                    continue

                input_text = context_tail + "\n\n" + chunk if context_tail else chunk

                model = TRANSLATE_MODEL if mode == "translate" else POLISH_MODEL
                system = TRANSLATE_SYSTEM if mode == "translate" else POLISH_SYSTEM

                fut = executor.submit(call_ollama, model, system, input_text)
                futures[fut] = (i, key, chunk)

            for fut in as_completed(futures):
                i, key, original_chunk = futures[fut]
                result = fut.result()

                results[i] = result
                progress[key] = result
                atomic_save(progress)

                context_tail = result[-300:]
                total_processed += len(original_chunk)

                elapsed = time.time() - start_time
                speed = total_processed / elapsed if elapsed else 0

                remaining = max(0, total_chars_estimate - total_processed)
                eta_seconds = int(remaining / speed) if speed > 0 else -1

                if eta_seconds > 0:
                    logging.info(f"⏳ ETA: {eta_seconds // 60}m {eta_seconds % 60}s")

                with open(STATS_FILE, "w") as f:
                    json.dump({
                        "processed_chars": total_processed,
                        "total_chars": total_chars_estimate,
                        "speed_chars_per_sec": speed,
                        "eta_seconds": eta_seconds,
                        "ram_percent": psutil.virtual_memory().percent
                    }, f)

        full_text = "\n\n".join(results)
        new_paragraphs = full_text.split("\n\n")

        p_tags = soup.find_all("p")
        idx = 0

        for p in p_tags:
            original = p.get_text().strip()

            if len(original) <= 5:
                continue

            if idx < len(new_paragraphs):
                p.string = new_paragraphs[idx]
                idx += 1

        item.set_content(str(soup).encode("utf-8"))

    epub.write_epub(output_path, book)

# ================= PIPELINE =================

def full_pipeline(input_file):
    base, _ = os.path.splitext(input_file)
    temp_file = base + "_tmp.epub"
    output_file = base + "_th.epub"

    logging.info("PASS 1: TRANSLATE")
    process_epub(input_file, temp_file, "translate")

    restart_ollama()

    logging.info("PASS 2: POLISH")
    process_epub(temp_file, output_file, "polish")

    logging.info("DONE")

# ================= MAIN =================

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Input EPUB file")
    args = parser.parse_args()
    full_pipeline(args.input)