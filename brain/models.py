"""Freedom - universal model layer.

One interface for every brain Freedom can plug into:

* local llama.cpp HTTP server   (http://127.0.0.1:8080/completion)
* Ollama                        (/api/chat, /api/tags)
* LM Studio / vLLM / LocalAI / text-generation-webui / Jan (OpenAI-compatible)
* OpenAI + any OpenAI-compatible cloud (Groq, Together, Fireworks, Cerebras,
  DeepInfra, OpenRouter, GitHub Models, Azure-style base URLs...)
* Anthropic Claude
* Google Gemini
*
* Every adapter here talks to a real endpoint: a model the picker offers can
  answer a prompt, and one that cannot is not offered.

Every provider exposes capabilities() including required_ram_gb, which the
memory engine uses to decide what actually fits on this machine.
"""

from __future__ import annotations

import os
import re
import json
import time
import socket
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ModelInfo:
    id: str
    provider: str
    label: str
    kind: str = "cloud"                 # local | cloud
    context: int = 8192
    params_b: float = 0.0               # billions of parameters
    quant_bits: float = 4.0             # effective weight precision
    tools: bool = True
    vision: bool = False
    base_url: str = ""
    api_key_env: str = ""
    path: str = ""                      # completion/chat path override
    note: str = ""

    @property
    def required_ram_gb(self) -> float:
        """Rough resident-RAM need: weights + KV cache + runtime overhead."""
        if self.kind != "local":
            return 0.0
        weights = self.params_b * 1e9 * self.quant_bits / 8
        kv = max(0.35e9, self.context / 1024 * 0.06e9)
        overhead = 0.7e9
        return round((weights + kv + overhead) / 1e9, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "provider": self.provider, "label": self.label,
            "kind": self.kind, "context": self.context, "params_b": self.params_b,
            "quant_bits": self.quant_bits, "tools": self.tools, "vision": self.vision,
            "required_ram_gb": self.required_ram_gb, "base_url": self.base_url,
            "api_key_env": self.api_key_env, "note": self.note,
        }


@dataclass
class Generation:
    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    finish: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _jload(raw):
    if raw in (None, ""):
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _post(url, payload, headers, timeout):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return _jload(raw) or {"raw": raw}


def _get(url, headers, timeout):
    req = urllib.request.Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _jload(resp.read().decode("utf-8", "replace"))


def _post_stream(url, payload, headers, timeout):
    """Yield each parsed JSON frame of an SSE response (stream=True).

    Same request as _post(), but the caller sees the first token instead of
    waiting for the whole answer to be generated.
    """
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            frame = _jload(data)
            if isinstance(frame, dict):
                yield frame


# Keys pasted into Integrations are kept on this engine, not in the environment:
# a restart keeps them, nothing is exported into the shell, and the file is mode
# 600 inside .locode/ (already git-ignored).
KEYS_FILE = Path(os.environ.get(
    "LOCODE_KEYS", str(Path(__file__).resolve().parent.parent / ".locode" / "keys.json")))


def _stored_keys() -> Dict[str, str]:
    try:
        data = json.loads(KEYS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _key(env):
    """The environment wins, then a key saved from the UI."""
    value = os.environ.get(env, "").strip()
    if value:
        return value
    return str(_stored_keys().get(env) or "").strip()


LOCAL_HOST_RE = re.compile(r"(localhost|127\.0\.0\.1|0\.0\.0\.0|::1)")
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_PROBE_CACHE: Dict[str, Any] = {}


def _port_open(url: str, timeout: float = 0.6) -> bool:
    """True when something accepts TCP on the host:port of url (cached ~5s)."""
    m = re.match(r"^https?://([^/]+)", url or "")
    if not m:
        return False
    host, _, port = m.group(1).partition(":")
    if not port:
        port = "443" if url.lower().startswith("https") else "80"
    key = host + ":" + port
    now = time.time()
    hit = _PROBE_CACHE.get(key)
    if hit and now - hit[0] < 5:
        return hit[1]
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            ok = True
    except (OSError, ValueError):
        ok = False
    _PROBE_CACHE[key] = (now, ok)
    return ok


class Provider:
    """Contract every model adapter implements."""

    name = "base"

    def __init__(self, model: ModelInfo, timeout: int = 120):
        self.model = model
        self.timeout = timeout

    def headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.model.api_key_env:
            secret = _key(self.model.api_key_env)
            if secret:
                h["Authorization"] = "Bearer " + secret
        return h

    def available(self):
        """(bool, reason) - can this brain be used right now?"""
        if self.model.kind == "local":
            host = re.sub(r"^https?://", "", self.model.base_url).split("/")[0]
            if not (LOCAL_HOST_RE.search(host) or IP_RE.match(host)):
                return True, "remote inference host"
            # A loopback URL is only a promise that a server exists; check it,
            # otherwise the UI shows a green model that cannot answer.
            if not _port_open(self.model.base_url):
                return False, "nothing listening on %s - start it (ollama serve / llama-server / LM Studio)" % host
        if self.model.api_key_env and not _key(self.model.api_key_env):
            return False, "missing API key (set $" + self.model.api_key_env + ")"
        return True, "ready"

    def generate(self, messages, tools=None, temperature=0.2, max_tokens=1024) -> Generation:
        raise NotImplementedError

    def capabilities(self) -> Dict[str, Any]:
        ok, why = self.available()
        caps = self.model.to_dict()
        caps.update({"available": ok, "why": why, "provider_name": self.name})
        return caps
