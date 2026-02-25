# 📚 EPUB AI Translator (Ollama + Thai Novel Pipeline)

High-quality English → Thai novel translation pipeline using local LLMs via Ollama.

## Designed for:
- 📖 Full EPUB book translation
- 🧠 Context-aware chunk processing
-	✨ Two-pass pipeline (Translate → Polish)
-	💾 Crash-safe checkpointing
-	📊 Live performance stats + ETA
-	🔄 Automatic Ollama recovery
-	🧹 Watermark removal

⸻

## 🏗 Architecture Overview
```
Pipeline flow:

EPUB
  ↓
Extract <p> paragraphs
  ↓
Chunk intelligently (context aware)
  ↓
PASS 1 → Translate Model
  ↓
PASS 2 → Polish Model
  ↓
Rebuild EPUB
```
Models used:
- Translation: scb10x/typhoon-translate1.5-4b
- Polish: scb10x/llama3.2-typhoon2-3b-instruct

Runs locally via:

```http://localhost:11434/api/chat```

-----------------------------------------------------------------------------------------------------------------------

## 🚀 Features

### ✅ Smart Chunking
- 600 char chunk limit
- Context tail memory (last 300 chars)
- Preserves paragraph structure

### ✅ Quality Control
- Detects bad outputs
- Retries automatically
- Prevents empty/non-Thai responses

### ✅ Resource Adaptive
- Dynamic worker adjustment
- RAM usage guard
- Waits when memory > 85%

###✅ Crash-Safe
- Atomic checkpoint saving (progress.json)
- Resume anytime
- Two-pass checkpoint support

### ✅ Live Monitoring
- translate.log
- stats.json
- Real-time ETA
- Characters/sec tracking

-----------------------------------------------------------------------------------------------------------------------

## 📦 Requirements

1. Python 3.9+

2. Ollama (Running in Podman container named ollama)

Install models:
```
ollama pull scb10x/typhoon-translate1.5-4b
ollama pull scb10x/llama3.2-typhoon2-3b-instruct
```

3. Python Dependencies

Install with:

```make deps```

or manually:

```pip install ebooklib beautifulsoup4 lxml requests psutil tqdm```


-----------------------------------------------------------------------------------------------------------------------

## ⚙️ Configuration

Inside process_book.py:
```
MAX_CHARS = 600
BASE_WORKERS = 2
MAX_RAM_PERCENT = 85
```
Environment override:
```
export OLLAMA_URL="http://localhost:11434/api/chat"
```

-----------------------------------------------------------------------------------------------------------------------

## 🧪 Usage

Basic

```make run INPUT="your_book.epub"```

or

`make r INPUT="your_book.epub"`

Output:

`your_book_th.epub`


⸻

🔍 Monitoring

Live Logs

`make logs`

Live Stats

`make stats`

Example stats.json:
```
{
  "processed_chars": 150000,
  "total_chars": 900000,
  "speed_chars_per_sec": 120.4,
  "eta_seconds": 6234,
  "ram_percent": 72.1
}
```

-----------------------------------------------------------------------------------------------------------------------

## 🛠 Utility Commands

Health Check

`make health`

Restart Ollama

`make restart`

Clean temp files

`make clean`

Full reset (⚠ destructive)

`make reset`


-----------------------------------------------------------------------------------------------------------------------

## 📁 Project Structure
```
process_book.py
Makefile
progress.json
stats.json
translate.log
*_tmp.epub
*_th.epub
```

-----------------------------------------------------------------------------------------------------------------------

## 🧨 Recovery & Resume

If process crashes:

Just rerun:

`make run INPUT="your_book.epub"`

It resumes from progress.json.

-----------------------------------------------------------------------------------------------------------------------

## ⚡ Performance Notes

Recommended machine:
	•	16GB RAM
	•	8+ threads
	•	SSD storage

For low-RAM systems:
```
BASE_WORKERS = 1
MAX_RAM_PERCENT = 75
```

-----------------------------------------------------------------------------------------------------------------------

## 🧩 Advanced Improvements (Future Ideas)
- Per-chapter parallelization
- Persistent context embedding memory
- Translation consistency glossary
- Named entity preservation cache
- GPU-aware worker scaling
- SQLite checkpoint backend
- 	Web dashboard

-----------------------------------------------------------------------------------------------------------------------

🔐 License

Personal research / educational use.
