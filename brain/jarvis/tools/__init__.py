"""Tool registry.

A tool is an async function the model can call. Register one with @tool:

    from jarvis.tools import tool, ToolContext
    from jarvis.sysutil import ToolError, osascript

    @tool(
        name="open_app",
        description="Open or focus an application by name.",
        params={"name": {"type": "string", "description": "App name, e.g. Spotify"}},
        required=["name"],
        ack="Opening {name}.",
    )
    async def open_app(ctx: ToolContext, name: str) -> str:
        ...
        return "Spotify is open."

Rules for tool authors:
  * Handlers are async and take ctx first, then keyword arguments matching params.
  * Return a short string (or a JSON-serialisable dict) describing the RESULT.
    That text is what the model sees, so make it factual and compact - it will
    be read aloud or summarised. Never return raw multi-page output.
  * Raise ToolError("...") for expected failures. The message goes back to the
    model so it can recover; it is not a crash.
  * Anything destructive, outward-facing or expensive declares `risk=` so the
    user is asked first. Prefer safe primitives (Trash, not rm).
  * Keep params flat: strings, numbers, booleans and enums. Small local models
    handle nested objects badly.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from jarvis.sysutil import ToolError, truncate

log = logging.getLogger("jarvis.tools")

RiskFn = Callable[[dict], str | None]

REGISTRY: dict[str, "ToolSpec"] = {}

MAX_RESULT_CHARS = 4000

# Tools whose result is already a spoken answer. When one of these is the only
# thing a request needs, its text is spoken directly and the turn ends there -
# no second trip through the model just to reword "It's 61 degrees" into
# "It's 61 degrees". That round costs ~2.5s, which is most of the wait.
SPEAKABLE_RESULT = {
    "get_weather", "open_app", "quit_app", "system_control", "media_control",
    "set_timer", "timers", "remember", "recall", "notify", "notes",
    "reminders", "calendar", "daily_briefing", "claude_jobs", "ask_claude",
    "organize_files", "chrome_tabs", "open_url", "messages_send", "file_action",
}


CONFIRM_ONLY = {"messages_send", "file_action", "run_shell"}


@dataclass
class ToolSpec:
    name: str
    description: str
    params: dict[str, Any]
    required: list[str]
    handler: Callable[..., Awaitable[Any]]
    risk: str | RiskFn | None = None
    ack: str | None = None
    category: str = "general"
    available: Callable[[], bool] | None = None

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.params,
                    "required": self.required,
                },
            },
        }

    def confirmation_for(self, args: dict) -> str | None:
        # Only things that cannot be taken back ask first: sending a message,
        # trashing a file, a destructive shell command. Everything else just runs.
        if self.risk is None or self.name not in CONFIRM_ONLY:
            return None
        if self.name == "file_action" and (args.get("action") or "").lower() != "trash":
            return None
        if callable(self.risk):
            try:
                return self.risk(args)
            except Exception:  # a broken classifier must fail closed
                log.exception("risk classifier for %s raised", self.name)
                return f"Run {self.name}?"
        return self.risk.format(**{k: _fmt(v) for k, v in args.items()}) if "{" in self.risk else self.risk


def _fmt(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def tool(
    *,
    name: str,
    description: str,
    params: dict[str, Any] | None = None,
    required: Iterable[str] | None = None,
    risk: str | RiskFn | None = None,
    ack: str | None = None,
    category: str = "general",
    available: Callable[[], bool] | None = None,
):
    def deco(fn: Callable[..., Awaitable[Any]]):
        spec = ToolSpec(
            name=name,
            description=description,
            params=params or {},
            required=list(required or []),
            handler=fn,
            risk=risk,
            ack=ack,
            category=category,
            available=available,
        )
        if name in REGISTRY:
            raise RuntimeError(f"duplicate tool name: {name}")
        REGISTRY[name] = spec
        return fn

    return deco


class ToolContext:
    """What a tool is allowed to reach. Injected by the orchestrator."""

    def __init__(self, assistant):
        self.assistant = assistant
        self.cfg = assistant.cfg
        self.memory = assistant.memory
        self.scheduler = assistant.scheduler
        self.jobs = assistant.jobs
        self.log = logging.getLogger("jarvis.tool")

    def say(self, text: str) -> None:
        """Speak a progress line immediately, without going through the model."""
        self.assistant.speak_async(text)

    def emit(self, event: dict) -> None:
        self.assistant.broadcast(event)

    async def confirm(self, prompt: str, detail: str = "") -> bool:
        return await self.assistant.request_confirmation(prompt, detail)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()


def available_tools() -> list[ToolSpec]:
    out = []
    for spec in REGISTRY.values():
        if spec.available is not None:
            try:
                if not spec.available():
                    continue
            except Exception:
                continue
        out.append(spec)
    return out


_SCHEMA_CACHE: list[dict] | None = None


def _clip(text: str, limit: int) -> str:
    """Shorten to the last full sentence (or word) that fits."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return cut[: end + 1] if end > limit * 0.5 else cut[: cut.rfind(" ")].rstrip(",;:") + "."


def schemas() -> list[dict]:
    """Tool schemas, compacted and cached.

    These ride along with every request, so their size is paid in prefill time
    whenever the prompt cache is cold. Clipping descriptions roughly halves them.
    Cached so the bytes are identical call to call, which keeps the cache warm.
    """
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        import copy

        out = []
        for spec in available_tools():
            sc = copy.deepcopy(spec.schema())
            fn = sc["function"]
            fn["description"] = _clip(fn.get("description", ""), 180)
            for prop in fn.get("parameters", {}).get("properties", {}).values():
                if "description" in prop:
                    prop["description"] = _clip(prop["description"], 70)
            out.append(sc)
        _SCHEMA_CACHE = out
    return _SCHEMA_CACHE


def load_all() -> None:
    """Import every module in this package so decorators run."""
    import jarvis.tools as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"jarvis.tools.{mod.name}")
    log.info("loaded %d tools: %s", len(REGISTRY), ", ".join(sorted(REGISTRY)))


def _coerce(spec: ToolSpec, args: dict) -> dict:
    """Small models send "5" for numbers and "true" for booleans. Fix that up."""
    out = {}
    for key, value in (args or {}).items():
        p = spec.params.get(key)
        if not p:
            continue  # drop hallucinated parameters rather than raising TypeError
        t = p.get("type")
        try:
            if t == "number" and isinstance(value, str):
                value = float(value)
            elif t == "integer" and isinstance(value, str):
                value = int(float(value))
            elif t == "boolean" and isinstance(value, str):
                value = value.strip().lower() in ("true", "yes", "1", "on")
            elif t == "string" and not isinstance(value, str) and value is not None:
                value = str(value)
            elif t == "array" and isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    value = parsed if isinstance(parsed, list) else [value]
                except json.JSONDecodeError:
                    value = [v.strip() for v in value.split(",") if v.strip()]
        except (TypeError, ValueError):
            pass
        out[key] = value
    return out


async def execute(ctx: ToolContext, name: str, args: dict) -> str:
    """Run a tool call and return the text result for the model."""
    spec = REGISTRY.get(name)
    if spec is None:
        close = ", ".join(sorted(REGISTRY)[:12])
        return f"Error: no tool named {name!r}. Available tools include: {close}"

    args = _coerce(spec, args or {})
    missing = [r for r in spec.required if r not in args or args[r] in (None, "")]
    if missing:
        return f"Error: missing required argument(s): {', '.join(missing)}"

    if ctx.cfg.get("safety.confirm_risky", True):
        prompt = spec.confirmation_for(args)
        if prompt:
            approved = await ctx.confirm(prompt)
            if not approved:
                return "Cancelled by the user. Do not retry this action."

    try:
        result = spec.handler(ctx, **args)
        if inspect.isawaitable(result):
            result = await result
    except ToolError as exc:
        return f"Error: {exc}"
    except asyncio.CancelledError:
        raise
    except TypeError as exc:
        log.exception("bad call to %s", name)
        return f"Error: invalid arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001 - a tool must never kill the turn
        log.exception("tool %s failed", name)
        return f"Error: {type(exc).__name__}: {exc}"

    if result is None:
        return "Done."
    if not isinstance(result, str):
        try:
            result = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            result = str(result)
    return truncate(result, MAX_RESULT_CHARS)


__all__ = [
    "tool",
    "ToolContext",
    "ToolSpec",
    "ToolError",
    "REGISTRY",
    "execute",
    "schemas",
    "load_all",
    "available_tools",
]
