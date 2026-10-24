"""Server-side configuration; no credentials are returned to the frontend."""
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

from .errors import IntelligenceError

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def _env_number(name: str, default: str, number_type: type = float):
    try:
        return number_type(os.getenv(name, default))
    except (TypeError, ValueError):
        raise IntelligenceError(
            "PROVIDER_CONFIGURATION", f"{name} must be a valid number.", status_code=503
        ) from None


def _env_think() -> bool | None:
    value = os.getenv("OLLAMA_THINK", "default").strip().lower()
    if value in {"", "default"}:
        return None
    if value in {"true", "false"}:
        return value == "true"
    raise IntelligenceError(
        "PROVIDER_CONFIGURATION", "OLLAMA_THINK must be default, true or false.", status_code=503
    )


@dataclass(frozen=True)
class Settings:
    app_mode: str = "demo"
    llm_provider: str = "openai_compatible"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = ""
    llm_response_format: str = "json_schema"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_num_ctx: int = 4096
    ollama_num_predict: int = 2048
    ollama_temperature: float = 0.2
    ollama_keep_alive: str = "5m"
    ollama_think: bool | None = None
    ollama_context_bytes_per_token: float = 3.0
    output_llm_model: str = ""
    output_num_predict: int = 3072
    output_temperature: float = 0.5
    output_timeout: float = 240.0
    autopresent_base_url: str = "http://127.0.0.1:8091/v1"
    autopresent_model: str = "startupforge-autopresent"
    autopresent_timeout: float = 300.0
    search_provider: str = "wikipedia"
    tavily_api_key: str = ""
    web_discovery_provider: str = "ddgs"
    web_search_engines: str = "yahoo,duckduckgo"
    web_search_region: str = "in-en"
    web_request_timeout: float = 15.0
    retrieval_provider: str = "keyword"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    provider_timeout: float = 90.0
    database_path: str = "data/intelligence.sqlite3"
    demo_delay: float = 0.15

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(BACKEND_ROOT / ".env", override=False)
        return cls(
            app_mode=os.getenv("APP_MODE", "demo"),
            llm_provider=os.getenv("LLM_PROVIDER", "openai_compatible"),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_model=os.getenv("LLM_MODEL", ""),
            llm_response_format=os.getenv("LLM_RESPONSE_FORMAT", "json_schema"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            ollama_num_ctx=_env_number("OLLAMA_NUM_CTX", "4096", int),
            ollama_num_predict=_env_number("OLLAMA_NUM_PREDICT", "2048", int),
            ollama_temperature=_env_number("OLLAMA_TEMPERATURE", "0.2"),
            ollama_keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", "5m"),
            ollama_think=_env_think(),
            ollama_context_bytes_per_token=_env_number("OLLAMA_CONTEXT_BYTES_PER_TOKEN", "3"),
            output_llm_model=os.getenv("OUTPUT_LLM_MODEL", "").strip(),
            output_num_predict=_env_number("OUTPUT_NUM_PREDICT", "3072", int),
            output_temperature=_env_number("OUTPUT_TEMPERATURE", "0.5"),
            output_timeout=_env_number("OUTPUT_TIMEOUT", "240"),
            autopresent_base_url=os.getenv("AUTOPRESENT_BASE_URL", "http://127.0.0.1:8091/v1").rstrip("/"),
            autopresent_model=os.getenv("AUTOPRESENT_MODEL", "startupforge-autopresent").strip(),
            autopresent_timeout=_env_number("AUTOPRESENT_TIMEOUT", "300"),
            search_provider=os.getenv("SEARCH_PROVIDER", "wikipedia"),
            tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
            web_discovery_provider=os.getenv("WEB_DISCOVERY_PROVIDER", "ddgs"),
            web_search_engines=os.getenv("WEB_SEARCH_ENGINES", "yahoo,duckduckgo"),
            web_search_region=os.getenv("WEB_SEARCH_REGION", "in-en"),
            web_request_timeout=_env_number("WEB_REQUEST_TIMEOUT", "15"),
            retrieval_provider=os.getenv("RETRIEVAL_PROVIDER", "keyword"),
            embedding_model=os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
            provider_timeout=_env_number("PROVIDER_TIMEOUT", "90"),
            database_path=os.getenv("DATABASE_PATH", "data/intelligence.sqlite3"),
            demo_delay=_env_number("DEMO_DELAY", "0.15"),
        )
