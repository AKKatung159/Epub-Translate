# ===============================
# CONFIG
# ===============================

PYTHON := python
SCRIPT := process_book.py
INPUT ?= book.epub

# ===============================
# MAIN COMMANDS
# ===============================

.PHONY: run
run:
	@echo "🚀 Running translation for $(INPUT)"
	$(PYTHON) $(SCRIPT) "$(INPUT)"

# ===============================
# QUICK SHORTCUT
# ===============================

.PHONY: r
r: run

# ===============================
# OLLAMA HEALTH CHECK
# ===============================

.PHONY: health
health:
	@echo "🔍 Checking Ollama..."
	curl -s http://localhost:11434/api/tags | jq '.' || echo "❌ Ollama not reachable"

# ===============================
# RESTART OLLAMA (podman)
# ===============================

.PHONY: restart
restart:
	@echo "🔄 Restarting Ollama container..."
	podman restart ollama

# ===============================
# LIVE LOG VIEW
# ===============================

.PHONY: logs
logs:
	tail -f translate.log

# ===============================
# LIVE STATS VIEW
# ===============================

.PHONY: stats
stats:
	watch -n 2 cat stats.json

# ===============================
# CLEAN TEMP FILES
# ===============================

.PHONY: clean
clean:
	rm -f *_tmp.epub
	rm -f progress.json.tmp

# ===============================
# FULL RESET (DANGEROUS)
# ===============================

.PHONY: reset
reset:
	rm -f progress.json
	rm -f translate.log
	rm -f stats.json
	rm -f *_tmp.epub
	@echo "🔥 Reset complete"

# ===============================
# INSTALL DEPENDENCIES
# ===============================

.PHONY: deps
deps:
	pip install ebooklib beautifulsoup4 lxml tqdm requests psutil