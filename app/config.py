import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.environ.get("DATA_DIR", "./data"))
    llm_base_url: str = os.environ.get("LLM_BASE_URL", "http://192.168.0.191:8080/v1")
    llm_api_key: str = os.environ.get("LLM_API_KEY", "")
    llm_timeout: float = float(os.environ.get("LLM_TIMEOUT", "900"))
    translate_model: str = os.environ.get("TRANSLATE_MODEL", "typhoon-translate1.5-4b")
    glossary_model: str = os.environ.get("GLOSSARY_MODEL", "typhoon2.1-4b")
    chunk_chars: int = int(os.environ.get("CHUNK_CHARS", "2400"))      # English characters per request
    translate_parallel: int = int(os.environ.get("TRANSLATE_PARALLEL", "4"))   # chunks in flight (= llama-server slots)
    max_upload_mb: int = int(os.environ.get("MAX_UPLOAD_MB", "100"))
    max_glossary_terms: int = int(os.environ.get("MAX_GLOSSARY_TERMS", "400"))
    max_pdf_mb: int = int(os.environ.get("MAX_PDF_MB", "300"))
    discord_webhook_url: str = os.environ.get("DISCORD_WEBHOOK_URL", "")       # empty = no notifications
    public_url: str = os.environ.get("PUBLIC_URL", "")                         # for links in notifications


settings = Settings()
