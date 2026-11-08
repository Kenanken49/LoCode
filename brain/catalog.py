"""Freedom — the model catalog: every brain this engine can actually talk to.

Two kinds of entries, and nothing else:

* discovered — whatever a running `llama-server` reports on /v1/models, for each
  base URL in FREEDOM_LLAMA_URLS (default 127.0.0.1:8080). llama.cpp speaks the
  OpenAI dialect and applies the GGUF's own chat template, so these are driven
  through the openai-compat adapter by their real id.
* declared — the cloud and local-server integrations, each with the env var that
  holds its key.

A declared entry is a real endpoint, not a stand-in: it only reaches the picker
once `server._model_rows` finds it available, and every row that reaches the
picker can answer a prompt. The list is the registry of what this build knows how
to drive (`/api/keys` offers the key names from it), not a promise that each one
is configured.
"""
from __future__ import annotations
import json
import os
import urllib.request
from .models import ModelInfo


def llama_urls() -> list[str]:
    """Every llama.cpp base URL to discover, in order (FREEDOM_LLAMA_URLS wins)."""
    raw = os.environ.get("FREEDOM_LLAMA_URLS", "").strip()
    if raw:
        return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
    return [os.environ.get("FREEDOM_LLAMA_URL", "http://127.0.0.1:8080").rstrip("/")]


def llama_url() -> str:
    return llama_urls()[0]


def served(url: str = "") -> list[ModelInfo]:
    """Models a llama.cpp server is serving right now (id, ctx, params).

    llama.cpp speaks the OpenAI dialect at /v1/chat/completions and applies the
    GGUF's own chat template, so these are driven through the openai-compat
    adapter instead of the old <|user|> prompt guesswork. An unreachable server
    returns [] — nothing is invented to fill the gap.
    """
    base = (url or llama_url()).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/v1/models", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:                                  # noqa: BLE001 - no server is a normal state
        return []
    rows: list[ModelInfo] = []
    for row in data.get("data") or data.get("models") or []:
        mid = str(row.get("id") or row.get("name") or "")
        if not mid:
            continue
        meta = row.get("meta") or {}
        params = float(meta.get("n_params") or 0) / 1e9
        rows.append(ModelInfo(
            id=mid,
            provider="openai-compat",
            label=os.path.basename(mid) + " (llama.cpp, this PC)",
            kind="local",
            context=int(meta.get("n_ctx") or 4096),
            params_b=round(params, 2),
            tools=False,
            base_url=base + "/v1",
            path="/chat/completions",
            note="served by llama-server at " + base,
        ))
    return rows


def catalog() -> list[ModelInfo]:
    e = os.environ.get
    rows: list[ModelInfo] = []
    seen: set[str] = set()
    for base in llama_urls():
        for m in served(base):
            if m.id not in seen:
                seen.add(m.id)
                rows.append(m)
    rows += [
        # A local OpenAI-dialect server: LM Studio, vLLM, LocalAI, Jan. Real when
        # something listens on the port, listed only then.
        ModelInfo(id=e("FREEDOM_LOCAL_MODEL", "local-model"), provider="openai-compat",
                  label="Local OpenAI-compatible (LM Studio / vLLM)", kind="local", context=8192,
                  params_b=7.0, quant_bits=4.0, tools=True,
                  base_url=e("FREEDOM_LOCAL_URL", "http://127.0.0.1:1234/v1"),
                  api_key_env="FREEDOM_LOCAL_KEY", note="LM Studio default http://127.0.0.1:1234/v1"),
        # Cloud — OpenAI and the services that speak its dialect.
        ModelInfo(id=e("FREEDOM_OPENAI_MODEL", "gpt-4o-mini"), provider="openai-compat",
                  label="OpenAI (GPT-4o mini)", kind="cloud", context=128000, tools=True,
                  base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY"),
        ModelInfo(id="deepseek-chat", provider="openai-compat", label="DeepSeek V3 (cloud)",
                  kind="cloud", context=64000, tools=True,
                  base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY"),
        ModelInfo(id=e("FREEDOM_GROQ_MODEL", "llama-3.3-70b-versatile"), provider="openai-compat",
                  label="Groq (Llama 3.3 70B, fast)", kind="cloud", context=32768, tools=True,
                  base_url="https://api.groq.com/openai/v1", api_key_env="GROQ_API_KEY"),
        ModelInfo(id="mixtral-8x7b", provider="openai-compat", label="Together / Mixtral (cloud)",
                  kind="cloud", context=32768, tools=True,
                  base_url="https://api.together.xyz/v1", api_key_env="TOGETHER_API_KEY"),
        ModelInfo(id=e("FREEDOM_OPENROUTER_MODEL", "mimo-2.5-pro"), provider="openai-compat",
                  label="OpenRouter (MiMo / GLM / Minimax)", kind="cloud", context=64000, tools=True,
                  base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY"),
        # Qwen's cloud models are served on Alibaba Model Studio through the
        # OpenAI dialect; Qwen3.8 Flash is the fast multimodal one, 1M context.
        ModelInfo(id=e("FREEDOM_QWEN_MODEL", "qwen3.8-flash"), provider="openai-compat",
                  label="Qwen3.8 Flash (cloud)", kind="cloud", context=1000000,
                  tools=True, vision=True,
                  base_url=e("FREEDOM_QWEN_URL",
                             "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
                  api_key_env="DASHSCOPE_API_KEY",
                  note="Alibaba Model Studio · 1M context · text + image"),
        # Cloud — Anthropic and Gemini, with their own protocols.
        ModelInfo(id=e("FREEDOM_ANTHROPIC_MODEL", "claude-haiku-4-5"), provider="anthropic",
                  label="Claude Haiku (cloud)", kind="cloud", context=200000, tools=True,
                  base_url="https://api.anthropic.com", api_key_env="ANTHROPIC_API_KEY"),
        ModelInfo(id=e("FREEDOM_GEMINI_MODEL", "gemini-2.5-flash-lite"), provider="gemini",
                  label="Gemini Flash-Lite (cloud)", kind="cloud", context=1000000, tools=True,
                  base_url="https://generativelanguage.googleapis.com", api_key_env="GEMINI_API_KEY"),
    ]
    return rows
