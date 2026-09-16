"""Ollama client: streaming chat with tool calls, plus vision and warm-up.

Latency notes that shape this file:
  * Ollama caches the KV state of the previous request's prompt prefix. The
    system prompt and tool schemas are therefore kept byte-identical between
    turns - anything time-varying goes in the user message, never the system
    prompt, or every turn pays a full re-prefill (seconds, not milliseconds).
  * The model is preloaded the moment the wake word fires, so weights are
    resident by the time speech-to-text hands over the text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("jarvis.llm")

# Some small models narrate a tool call as text instead of emitting it
# structurally. Qwen's template uses this exact wrapper, so recover it.
_TOOL_TEXT = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass
class ChatChunk:
    """One event from the model stream."""

    kind: str  # "text" | "thinking" | "tool_calls" | "done" | "error"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


class LLMError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None
        self._warmed: set[str] = set()
        self._supports_think: dict[str, bool] = {}

    @property
    def url(self) -> str:
        return self.cfg.get("llm.ollama_url", "http://127.0.0.1:11434").rstrip("/")

    @property
    def model(self) -> str:
        return self.cfg.get("llm.model", "qwen3.5:4b")

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.cfg.get("llm.request_timeout_s", 180), connect=5.0)
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------- lifecycle
    async def is_up(self) -> bool:
        try:
            c = await self.client()
            r = await c.get(f"{self.url}/api/version", timeout=3.0)
            return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    async def ensure_up(self, timeout: float = 40.0) -> bool:
        """Start Ollama.app if the server is not answering."""
        if await self.is_up():
            return True
        log.info("ollama not responding - launching Ollama.app")
        try:
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/open", "-g", "-a", "Ollama",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except OSError as exc:
            log.warning("could not launch Ollama.app: %s", exc)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.is_up():
                return True
            await asyncio.sleep(1.0)
        return False

    async def list_models(self) -> list[str]:
        try:
            c = await self.client()
            r = await c.get(f"{self.url}/api/tags", timeout=10.0)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except (httpx.HTTPError, KeyError, ValueError):
            return []

    async def warm(self, model: str | None = None) -> None:
        """Load weights into memory without generating anything."""
        model = model or self.model
        try:
            c = await self.client()
            await c.post(
                f"{self.url}/api/chat",
                json={"model": model, "messages": [], "keep_alive": self.cfg.get("llm.keep_alive", "2h")},
                timeout=120.0,
            )
            self._warmed.add(model)
        except httpx.HTTPError as exc:
            log.debug("warm-up failed for %s: %s", model, exc)

    # ------------------------------------------------------------------ chat
    def _options(self) -> dict[str, Any]:
        return {
            "num_ctx": self.cfg.get("llm.num_ctx", 8192),
            "temperature": self.cfg.get("llm.temperature", 0.7),
            "top_p": self.cfg.get("llm.top_p", 0.8),
            "num_predict": self.cfg.get("llm.num_predict", 220),
        }

    def _payload(self, messages, tools, model, stream) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self.cfg.get("llm.keep_alive", "2h"),
            "options": self._options(),
        }
        if tools:
            body["tools"] = tools
        if self._supports_think.get(model, True):
            body["think"] = bool(self.cfg.get("llm.think", False))
        return body

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """Stream one assistant turn. Yields text as it arrives, then tool calls."""
        model = model or self.model
        c = await self.client()
        body = self._payload(messages, tools, model, stream=True)
        buffered_text: list[str] = []
        collected: list[ToolCall] = []

        try:
            async with c.stream("POST", f"{self.url}/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                    # Older/non-thinking models reject the think parameter outright.
                    if "think" in detail.lower() and self._supports_think.get(model, True):
                        self._supports_think[model] = False
                        log.info("model %s does not take think=; retrying without it", model)
                        async for chunk in self.chat_stream(messages, tools, model):
                            yield chunk
                        return
                    yield ChatChunk("error", text=f"Ollama returned {resp.status_code}: {detail}")
                    return

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("error"):
                        yield ChatChunk("error", text=str(data["error"]))
                        return
                    msg = data.get("message") or {}
                    if msg.get("thinking"):
                        yield ChatChunk("thinking", text=msg["thinking"])
                    piece = msg.get("content") or ""
                    if piece:
                        buffered_text.append(piece)
                        yield ChatChunk("text", text=piece)
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        collected.append(ToolCall(fn.get("name", ""), args or {}, tc.get("id", "")))
                    if data.get("done"):
                        text = "".join(buffered_text)
                        if not collected:
                            collected = _recover_text_tool_calls(text)
                        if collected:
                            yield ChatChunk("tool_calls", tool_calls=collected)
                        yield ChatChunk(
                            "done",
                            text=text,
                            stats={
                                "eval_count": data.get("eval_count", 0),
                                "eval_duration": data.get("eval_duration", 0),
                                "prompt_eval_count": data.get("prompt_eval_count", 0),
                                "prompt_eval_duration": data.get("prompt_eval_duration", 0),
                                "load_duration": data.get("load_duration", 0),
                                "total_duration": data.get("total_duration", 0),
                            },
                        )
                        return
        except httpx.HTTPError as exc:
            yield ChatChunk("error", text=f"Could not reach the local model: {exc}")
        except asyncio.CancelledError:
            raise

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Non-streaming convenience call (used for classification and priming)."""
        model = model or self.model
        c = await self.client()
        body = self._payload(messages, tools, model, stream=False)
        if options:
            body["options"] = {**body["options"], **options}
        r = await c.post(f"{self.url}/api/chat", json=body)
        if r.status_code != 200:
            raise LLMError(f"Ollama returned {r.status_code}: {r.text[:200]}")
        return r.json()

    async def vision(self, prompt: str, image_b64: str, model: str | None = None) -> str:
        """Ask a vision-capable model about an image (used for 'what's on my screen')."""
        model = model or self.cfg.get("llm.vision_model") or self.model
        c = await self.client()
        r = await c.post(
            f"{self.url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
                "stream": False,
                "think": False,
                "keep_alive": "2m",  # a second model only borrows memory briefly
                "options": {"temperature": 0.3},
            },
            timeout=120.0,
        )
        if r.status_code != 200:
            raise LLMError(f"vision request failed ({r.status_code}): {r.text[:200]}")
        return (r.json().get("message", {}).get("content") or "").strip()


def _recover_text_tool_calls(text: str) -> list[ToolCall]:
    """Parse tool calls a model wrote as plain text instead of emitting properly."""
    out: list[ToolCall] = []
    for match in _TOOL_TEXT.finditer(text or ""):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        name = data.get("name") or data.get("tool")
        args = data.get("arguments") or data.get("parameters") or {}
        if name and isinstance(args, dict):
            out.append(ToolCall(name, args))
    return out


def strip_tool_call_text(text: str) -> str:
    """Remove any tool-call markup so it is never spoken aloud."""
    return _TOOL_TEXT.sub("", text or "").strip()
