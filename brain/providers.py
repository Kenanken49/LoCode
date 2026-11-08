"""Freedom - provider implementations.

Concrete adapters behind one tiny contract: generate(messages, tools) -> Generation
(or stream(messages, tools) for the OpenAI dialect, which yields deltas as the
model produces them).
Covers llama.cpp, Ollama, every OpenAI-compatible server/cloud, Anthropic and
Google Gemini. Every one of them calls a real endpoint.
"""

from __future__ import annotations

import re
import json
import time
import urllib.error
from typing import Any, Dict, List

from .models import (Generation, ModelInfo, Provider, _get, _jload, _key, _post,
                     _post_stream)


# --------------------------------------------------------------------------- #
# OpenAI-compatible - covers OpenAI + most of the industry
# --------------------------------------------------------------------------- #
class OpenAICompat(Provider):
    name = "openai-compat"

    def generate(self, messages, tools=None, temperature=0.2, max_tokens=1024) -> Generation:
        url = self.model.base_url.rstrip("/") + (self.model.path or "/chat/completions")
        payload: Dict[str, Any] = {
            "model": self.model.id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools and self.model.tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        t0 = time.time()
        try:
            data = _post(url, payload, self.headers(), self.timeout)
        except Exception as exc:                      # noqa: BLE001 - surface every transport failure
            msg = str(exc)
            reader = getattr(exc, "read", None)
            if callable(reader):
                try:
                    msg += " :: " + reader().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
            return Generation(error="%s: %s" % (self.model.provider, msg), model=self.model.id,
                              finish="error", latency_ms=int((time.time() - t0) * 1000))

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        calls: List[Dict[str, Any]] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = _jload(fn.get("arguments") or "{}")
            calls.append({"name": fn.get("name", ""), "args": args if isinstance(args, dict) else {},
                          "id": tc.get("id", "")})
        usage = data.get("usage") or {}
        return Generation(
            text=text,
            tool_calls=calls,
            finish=choice.get("finish_reason") or ("tool_calls" if calls else "stop"),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or len(text) // 4),
            latency_ms=int((time.time() - t0) * 1000),
            model=data.get("model") or self.model.id,
        )


    def stream(self, messages, tools=None, temperature=0.2, max_tokens=1024):
        """Yield ("text", delta) per token and ("calls", [...]) once at the end.

        Same request as generate() with stream=True: the first token reaches the
        UI after one model step instead of after the whole answer, which is the
        difference between a reply that appears and one that types itself out.
        Native tool calls arrive as index-keyed deltas that are assembled here.
        """
        url = self.model.base_url.rstrip("/") + (self.model.path or "/chat/completions")
        payload: Dict[str, Any] = {
            "model": self.model.id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools and self.model.tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        pending: Dict[int, Dict[str, str]] = {}
        try:
            for chunk in _post_stream(url, payload, self.headers(), self.timeout):
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield ("text", text)
                    for tc in delta.get("tool_calls") or []:
                        slot = pending.setdefault(
                            int(tc.get("index") or 0), {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = str(tc["id"])
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = str(fn["name"])
                        if fn.get("arguments"):
                            slot["arguments"] += str(fn["arguments"])
        except Exception as exc:                      # noqa: BLE001 - the caller reports it
            raise RuntimeError("%s: %s" % (self.model.provider, exc)) from exc
        if pending:
            calls: List[Dict[str, Any]] = []
            for idx in sorted(pending):
                slot = pending[idx]
                args = _jload(slot["arguments"] or "{}")
                calls.append({"name": slot["name"], "args": args if isinstance(args, dict) else {},
                              "id": slot["id"]})
            yield ("calls", calls)


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #
class Anthropic(Provider):
    name = "anthropic"

    def headers(self):
        return {"x-api-key": _key(self.model.api_key_env),
                "anthropic-version": "2023-06-01", "Content-Type": "application/json"}

    def generate(self, messages, tools=None, temperature=0.2, max_tokens=1024) -> Generation:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        payload: Dict[str, Any] = {
            "model": self.model.id,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "assistant" if m["role"] == "assistant" else "user",
                          "content": m["content"]} for m in turns],
        }
        if system:
            payload["system"] = system
        if tools and self.model.tools:
            payload["tools"] = [{
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
            } for t in tools]
        t0 = time.time()
        try:
            data = _post(self.model.base_url.rstrip("/") + "/v1/messages", payload, self.headers(), self.timeout)
        except Exception as exc:                      # noqa: BLE001
            return Generation(error="anthropic: %s" % exc, model=self.model.id, finish="error")
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [{"name": b.get("name", ""), "args": b.get("input") or {}, "id": b.get("id", "")}
                 for b in blocks if b.get("type") == "tool_use"]
        usage = data.get("usage") or {}
        return Generation(text=text, tool_calls=calls, finish=data.get("stop_reason") or "stop",
                          prompt_tokens=int(usage.get("input_tokens") or 0),
                          completion_tokens=int(usage.get("output_tokens") or 0),
                          latency_ms=int((time.time() - t0) * 1000),
                          model=data.get("model") or self.model.id)

    def stream(self, messages, tools=None, temperature=0.2, max_tokens=1024):
        """Yield ("text", delta) per token and ("calls", [...]) at the end.

        Anthropic's SSE announces a tool_use block with content_block_start and
        then carries that call's arguments as input_json_delta fragments, so the
        text streams as it is written while the arguments are assembled here.
        """
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        payload: Dict[str, Any] = {
            "model": self.model.id,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "assistant" if m["role"] == "assistant" else "user",
                          "content": m["content"]} for m in turns],
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools and self.model.tools:
            payload["tools"] = [{
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
            } for t in tools]
        blocks: Dict[int, Dict[str, str]] = {}
        try:
            for frame in _post_stream(self.model.base_url.rstrip("/") + "/v1/messages",
                                      payload, self.headers(), self.timeout):
                kind = frame.get("type")
                idx = int(frame.get("index") or 0)
                if kind == "content_block_start":
                    block = frame.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        blocks[idx] = {"name": str(block.get("name") or ""),
                                       "id": str(block.get("id") or ""), "json": ""}
                elif kind == "content_block_delta":
                    delta = frame.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield ("text", delta["text"])
                    elif delta.get("type") == "input_json_delta":
                        slot = blocks.setdefault(idx, {"name": "", "id": "", "json": ""})
                        slot["json"] += str(delta.get("partial_json") or "")
                elif kind == "error":
                    raise RuntimeError((frame.get("error") or {}).get("message") or "anthropic error")
        except Exception as exc:                      # noqa: BLE001 - the caller reports it
            raise RuntimeError("anthropic: %s" % exc) from exc
        if blocks:
            calls: List[Dict[str, Any]] = []
            for idx in sorted(blocks):
                slot = blocks[idx]
                args = _jload(slot["json"] or "{}")
                calls.append({"name": slot["name"], "args": args if isinstance(args, dict) else {},
                              "id": slot["id"]})
            yield ("calls", calls)


# --------------------------------------------------------------------------- #
# Google Gemini
# --------------------------------------------------------------------------- #
class Gemini(Provider):
    name = "gemini"

    def generate(self, messages, tools=None, temperature=0.2, max_tokens=1024) -> Generation:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m["content"]}]} for m in messages if m["role"] != "system"]
        payload: Dict[str, Any] = {"contents": contents,
                                   "generationConfig": {"temperature": temperature,
                                                        "maxOutputTokens": max_tokens}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools and self.model.tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": t["function"]["name"],
                 "description": t["function"].get("description", ""),
                 "parameters": t["function"].get("parameters", {})} for t in tools]}]
        url = "%s/v1beta/models/%s:generateContent?key=%s" % (
            self.model.base_url.rstrip("/"), self.model.id, _key(self.model.api_key_env))
        t0 = time.time()
        try:
            data = _post(url, payload, {"Accept": "application/json"}, self.timeout)
        except Exception as exc:                      # noqa: BLE001
            return Generation(error="gemini: %s" % exc, model=self.model.id, finish="error")
        text, calls = "", []
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if "text" in part:
                    text += part["text"]
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    calls.append({"name": fc.get("name", ""), "args": fc.get("args") or {},
                                  "id": fc.get("name", "")})
        usage = data.get("usageMetadata") or {}
        return Generation(text=text, tool_calls=calls, finish="stop",
                          prompt_tokens=int(usage.get("promptTokenCount") or 0),
                          completion_tokens=int(usage.get("candidatesTokenCount") or 0),
                          latency_ms=int((time.time() - t0) * 1000), model=self.model.id)

    def stream(self, messages, tools=None, temperature=0.2, max_tokens=1024):
        """Yield text as Gemini's :streamGenerateContent sends it.

        Gemini frames whole parts rather than tokens, and delivers functionCall
        parts inside the same stream; those are collected and emitted once at the
        end, so the caller sees the same contract as the OpenAI dialect.
        """
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m["content"]}]} for m in messages if m["role"] != "system"]
        payload: Dict[str, Any] = {"contents": contents,
                                   "generationConfig": {"temperature": temperature,
                                                        "maxOutputTokens": max_tokens}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools and self.model.tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": t["function"]["name"],
                 "description": t["function"].get("description", ""),
                 "parameters": t["function"].get("parameters", {})} for t in tools]}]
        url = "%s/v1beta/models/%s:streamGenerateContent?alt=sse&key=%s" % (
            self.model.base_url.rstrip("/"), self.model.id, _key(self.model.api_key_env))
        calls: List[Dict[str, Any]] = []
        try:
            for frame in _post_stream(url, payload, {"Accept": "text/event-stream"}, self.timeout):
                if frame.get("error"):
                    raise RuntimeError((frame["error"] or {}).get("message") or "gemini error")
                for cand in frame.get("candidates") or []:
                    for part in (cand.get("content") or {}).get("parts") or []:
                        if part.get("text"):
                            yield ("text", part["text"])
                        elif "functionCall" in part:
                            fc = part["functionCall"] or {}
                            calls.append({"name": str(fc.get("name") or ""),
                                          "args": fc.get("args") or {},
                                          "id": str(fc.get("name") or "")})
        except Exception as exc:                      # noqa: BLE001 - the caller reports it
            raise RuntimeError("gemini: %s" % exc) from exc
        if calls:
            yield ("calls", calls)


# --------------------------------------------------------------------------- #
# Ollama native
# --------------------------------------------------------------------------- #
class Ollama(Provider):
    name = "ollama"

    def available(self):
        """A listening Ollama is not an installed model.

        The port probe says the server answers; /api/tags says whether this tag
        is actually pulled. Without the second check the picker offered
        qwen2.5-coder:7b as "ready" on a box whose Ollama has no models at all.
        """
        ok, why = super().available()
        if not ok:
            return ok, why
        tags = self.list_local()
        if self.model.id in tags:
            return True, "pulled in Ollama"
        if not tags:
            return False, "Ollama is running but has no models (ollama pull %s)" % self.model.id
        return False, "not pulled in Ollama (ollama pull %s)" % self.model.id

    def generate(self, messages, tools=None, temperature=0.2, max_tokens=1024) -> Generation:
        payload = {"model": self.model.id, "messages": messages, "stream": False,
                   "options": {"temperature": temperature, "num_ctx": self.model.context,
                               "num_predict": max_tokens}}
        if tools and self.model.tools:
            payload["tools"] = tools
        t0 = time.time()
        try:
            data = _post(self.model.base_url.rstrip("/") + "/api/chat", payload, {}, self.timeout)
        except Exception as exc:                      # noqa: BLE001
            return Generation(error="ollama: %s" % exc, model=self.model.id, finish="error")
        msg = data.get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = (c or {}).get("function") or {}
            if fn:
                calls.append({"name": fn.get("name", ""), "args": fn.get("arguments") or {}, "id": ""})
        return Generation(text=msg.get("content") or "", tool_calls=calls,
                          finish=data.get("done_reason") or "stop",
                          prompt_tokens=int(data.get("prompt_eval_count") or 0),
                          completion_tokens=int(data.get("eval_count") or 0),
                          latency_ms=int((time.time() - t0) * 1000), model=self.model.id)

    def list_local(self) -> List[str]:
        """Discover whatever the user already pulled into ~/.ollama."""
        try:
            data = _get(self.model.base_url.rstrip("/") + "/api/tags", {}, 10) or {}
            return [m["name"] for m in data.get("models") or [] if m.get("name")]
        except Exception:
            return []


# --------------------------------------------------------------------------- #
# llama.cpp HTTP server native
# --------------------------------------------------------------------------- #
class LlamaCpp(Provider):
    name = "llama.cpp"

    @staticmethod
    def render(messages) -> str:
        parts = []
        for m in messages:
            role = m.get("role", "user")
            tag = "tool" if role == "tool" else role
            parts.append("<|" + tag + "|>\n%s<|end|>\n" % m.get("content", ""))
        parts.append("<|assistant|>\n")
        return "".join(parts)

    def generate(self, messages, tools=None, temperature=0.2, max_tokens=1024) -> Generation:
        payload = {"prompt": self.render(messages), "n_predict": max_tokens,
                   "temperature": temperature, "antiprompt": ["<|end|>", "<|user|>"]}
        t0 = time.time()
        try:
            data = _post(self.model.base_url.rstrip("/") + "/completion", payload, self.headers(), self.timeout)
        except Exception as exc:                      # noqa: BLE001
            return Generation(error="llama.cpp: %s" % exc, model=self.model.id, finish="error")
        text = data.get("content") or (data.get("choices") or [{}])[0].get("text", "")
        calls = parse_tool_blocks(text)
        usage = data.get("usage") or {}
        return Generation(text=strip_tool_blocks(text), tool_calls=calls,
                          finish="tool_calls" if calls else "stop",
                          prompt_tokens=int(usage.get("prompt_tokens") or data.get("tokens_predicted") or 0),
                          completion_tokens=int(usage.get("completion_tokens") or data.get("tokens_n_predicted") or 0),
                          latency_ms=int((time.time() - t0) * 1000), model=self.model.id)


TOOL_BLOCK_RE = re.compile(r"(?:```(?:json|tool)?\s*)?\"?\{\"\s*call\"\s*:", re.S)


def parse_tool_blocks(text: str) -> List[Dict[str, Any]]:
    """Recover tool intent from models that emit JSON instead of native tool calls.

    Small local models routinely answer with a fenced JSON blob; we normalise
    that into real tool calls so the agent loop works with *any* model.
    """
    out: List[Dict[str, Any]] = []
    if not text:
        return out
    for match in re.finditer(r"\{[^{}]*\"(?:call|tool|name)\"[^{}]*\}", text, re.S):
        blob = _jload(match.group(0))
        if not isinstance(blob, dict):
            continue
        name = blob.get("call") or blob.get("tool") or blob.get("name") or ""
        args = blob.get("args") or blob.get("arguments") or blob.get("input") or blob.get("parameters") or {}
        if isinstance(args, str):
            args = _jload(args) or {"target": args}
        if name:
            out.append({"name": str(name), "args": args if isinstance(args, dict) else {}, "id": ""})
    for fenced in re.finditer(r"```(?:json|tool)\s*(.*?)```", text, re.S):
        blob = _jload(fenced.group(1).strip())
        if isinstance(blob, dict):
            name = blob.get("call") or blob.get("tool") or blob.get("name") or ""
            args = blob.get("args") or blob.get("arguments") or blob.get("input") or {}
            if isinstance(args, str):
                args = _jload(args) or {}
            if name:
                out.append({"name": str(name), "args": args if isinstance(args, dict) else {}, "id": ""})
    return out


def strip_tool_blocks(text: str) -> str:
    return re.sub(r"```(?:json|tool)\s*\{.*?\}\s*```", "", text, flags=re.S).strip()
