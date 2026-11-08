"""Local HTTP + WebSocket server: the UI, the hotkey, and the CLI all talk here.

Bound to 127.0.0.1 only. Everything Jarvis can do is reachable from this API,
which is what lets Claude Code (or a shell script) drive it:

    curl -s -XPOST localhost:8765/api/command -d '{"text":"what time is it"}'
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from jarvis.config import UI_DIR
from jarvis.core import Assistant

log = logging.getLogger("jarvis.server")


@web.middleware
async def local_only(request, handler):
    # A remote web page must not be able to drive this unauthenticated local API.
    hosts = {"127.0.0.1", "localhost", "::1"}
    if urlparse("http://" + request.host).hostname not in hosts:
        raise web.HTTPForbidden(text="Local host required")
    origin = request.headers.get("Origin")
    if origin and (urlparse(origin).hostname not in hosts or
                   urlparse(origin).netloc != request.host):
        raise web.HTTPForbidden(text="Local same-origin requests only")
    return await handler(request)


class UIClient:
    """One connected UI. Events are queued so a slow socket never blocks the core."""

    def __init__(self, ws: web.WebSocketResponse, loop: asyncio.AbstractEventLoop):
        self.ws = ws
        self.loop = loop
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)

    def send_event(self, event: dict) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest frame rather than stalling; levels are disposable.
            with_suppress(self.queue.get_nowait)
            with_suppress(self.queue.put_nowait, event)

    async def writer(self) -> None:
        while True:
            event = await self.queue.get()
            if self.ws.closed:
                return
            try:
                await self.ws.send_json(event)
            except (ConnectionResetError, RuntimeError):
                return


def with_suppress(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


class Server:
    def __init__(self, assistant: Assistant):
        self.assistant = assistant
        self.cfg = assistant.cfg
        self.app = web.Application(middlewares=[local_only])
        self._runner: web.AppRunner | None = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        a = self.app
        a.router.add_get("/ws", self.ws_handler)
        a.router.add_get("/api/status", self.api_status)
        a.router.add_post("/api/activate", self.api_activate)
        a.router.add_post("/api/stop", self.api_stop)
        a.router.add_post("/api/command", self.api_command)
        a.router.add_post("/api/say", self.api_say)
        a.router.add_post("/api/listening", self.api_listening)
        a.router.add_post("/api/grant", self.api_grant)
        a.router.add_get("/api/models", self.api_models)
        a.router.add_get("/api/config", self.api_config)
        a.router.add_post("/api/config", self.api_set_config)
        # Command Center: live readings and direct controls (additive; nothing above changed).
        a.router.add_get("/api/live", self.api_live)
        a.router.add_get("/api/weather", self.api_weather)
        a.router.add_get("/api/music", self.api_music)
        a.router.add_post("/api/music", self.api_music_action)
        a.router.add_get("/api/volume", self.api_volume)
        a.router.add_post("/api/volume", self.api_set_volume)
        a.router.add_post("/api/action", self.api_action)
        a.router.add_get("/api/history", self.api_history)
        a.router.add_get("/api/devices", self.api_devices)
        a.router.add_get("/api/timers", self.api_timers)
        a.router.add_get("/api/uistats", self.api_uistats)
        a.router.add_get("/", self.index)
        a.router.add_get("/{filename:.+}", self.static_file)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        host = self.cfg.get("server.host", "127.0.0.1")
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Jarvis control API must bind to loopback")
        port = int(self.cfg.get("server.port", 8765))
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        log.info("listening on http://%s:%d", host, port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # -------------------------------------------------------------- websocket
    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        client = UIClient(ws, asyncio.get_running_loop())
        self.assistant.clients.add(client)
        writer = asyncio.create_task(client.writer())
        log.info("ui connected (%d total)", len(self.assistant.clients))

        client.send_event({
            "type": "hello",
            "state": self.assistant.state,
            "config": self.cfg.as_dict(),
            "status": self.assistant.status(),
            "voices": self.assistant.speaker.voices(),
            "history": [t for t in self.assistant.memory.recent_turns(24) if t["role"] in ("user", "assistant")],
        })
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await self._handle_ui_message(data, client)
        finally:
            writer.cancel()
            self.assistant.clients.discard(client)
            log.info("ui disconnected (%d left)", len(self.assistant.clients))
        return ws

    async def _handle_ui_message(self, data: dict, client: UIClient) -> None:
        kind = data.get("type")
        a = self.assistant
        if kind == "activate":
            a.activate()
        elif kind == "stop":
            a.interrupt("ui")
        elif kind == "text":
            asyncio.create_task(a.handle_input(data.get("text", ""), source="ui", speak=data.get("speak", True)))
        elif kind == "confirm_response":
            a.resolve_confirmation_from_ui(data.get("id", ""), bool(data.get("approved")))
        elif kind == "set_config":
            path, value = data.get("path"), data.get("value")
            if path:
                self.cfg.set(path, value)
                self._apply_config(path, value)
                a.broadcast({"type": "config", "config": self.cfg.as_dict()})
        elif kind == "preview_voice":
            asyncio.create_task(self._preview_voice(data.get("voice", "")))
        elif kind == "set_listening":
            a.set_listening(bool(data.get("enabled", True)))
        elif kind == "new_conversation":
            a.new_conversation()
            a.broadcast({"type": "notice", "level": "info", "text": "New conversation."})
        elif kind == "cancel_job":
            asyncio.create_task(a.jobs.cancel(data.get("id", "")))
        elif kind == "ping":
            client.send_event({"type": "pong"})
        elif kind == "ui_stats":
            for waiter in list(getattr(self, "_stats_waiters", [])):
                waiter.append(data)

    def _apply_config(self, path: str, value) -> None:
        """Some settings need a live component update, not just a stored value."""
        a = self.assistant
        if path == "voice.volume":
            a.audio_out.volume = float(value)
        elif path == "listening.chimes":
            a.earcons.enabled = bool(value)
        elif path == "listening.enabled":
            a.listener.set_enabled(bool(value))
        elif path in ("assistant_name", "user_name", "address_as", "language"):
            a.rebuild_system_prompt()

    async def _preview_voice(self, voice: str) -> None:
        if not voice:
            return
        sample = "สวัสดีครับ ระบบพร้อมใช้งาน ทุกอย่างประมวลผลในเครื่องครับ" if "Kanya" in voice else "Good evening. Everything is running locally."
        try:
            samples, sr = await asyncio.get_running_loop().run_in_executor(
                None, self.assistant.speaker.synth, sample, voice
            )
            if sr != self.assistant.audio_out.sample_rate:
                from jarvis.tts import _resample

                samples = _resample(samples, sr, self.assistant.audio_out.sample_rate)
            self.assistant.audio_out.play(samples)
        except Exception as exc:
            log.warning("voice preview failed: %s", exc)

    # ------------------------------------------------------------------- API
    async def api_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.assistant.status())

    async def api_activate(self, request: web.Request) -> web.Response:
        self.assistant.activate()
        return web.json_response({"ok": True, "state": self.assistant.state})

    async def api_stop(self, request: web.Request) -> web.Response:
        self.assistant.interrupt("api")
        return web.json_response({"ok": True})

    async def api_command(self, request: web.Request) -> web.Response:
        """Run one instruction and return the reply. Used by the CLI and by Claude."""
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            data = {}
        text = (data.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "text is required"}, status=400)
        speak = bool(data.get("speak", True))
        reply = await self.assistant.handle_input(text, source="api", speak=speak)
        return web.json_response({"reply": reply, "state": self.assistant.state})

    async def api_say(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            data = {}
        text = (data.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "text is required"}, status=400)
        await self.assistant.say(text)
        return web.json_response({"ok": True})

    async def api_listening(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            data = {}
        if "enabled" in data:
            self.assistant.set_listening(bool(data["enabled"]))
        else:
            self.assistant.set_listening(not self.assistant.listener.enabled)
        return web.json_response({"listening": self.assistant.listener.enabled})

    async def api_grant(self, request: web.Request) -> web.Response:
        """Walk the Automation prompts while the user is at the keyboard."""
        from jarvis.permissions import probe_automation, summarise

        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            data = {}
        results = await probe_automation(force=bool(data.get("force")))
        text = summarise(results)
        self.assistant.broadcast({"type": "notice", "level": "info", "text": text})
        return web.json_response({"results": results, "summary": text})

    async def api_models(self, request: web.Request) -> web.Response:
        models = await self.assistant.llm.list_models()
        return web.json_response({"models": sorted(models), "current": self.assistant.llm.model})

    async def api_config(self, request: web.Request) -> web.Response:
        return web.json_response(self.cfg.as_dict())

    async def api_set_config(self, request: web.Request) -> web.Response:
        data = await request.json()
        path, value = data.get("path"), data.get("value")
        if not path:
            return web.json_response({"error": "path is required"}, status=400)
        if path == "llm.model":
            if value not in await self.assistant.llm.list_models():
                return web.json_response({"error": "Model is not installed in Ollama"}, status=400)
            self.assistant._ollama_ready = True
        self.cfg.set(path, value)
        self._apply_config(path, value)
        # Every open window follows a change, whichever client made it.
        self.assistant.broadcast({"type": "config", "config": self.cfg.as_dict()})
        if path == "llm.model":
            self.assistant.broadcast(self.assistant.status())
        return web.json_response({"ok": True, "config": self.cfg.as_dict()})

    # -------------------------------------------------------- command center
    async def api_live(self, request: web.Request) -> web.Response:
        from jarvis import live

        data = await live.snapshot(self.cfg)
        data["ready"] = self.assistant._ready
        data["mic"] = self.assistant._mic_status
        return web.json_response(data)

    async def api_weather(self, request: web.Request) -> web.Response:
        """Cached for 15 minutes per location: the panel must not hammer the weather service."""
        import time

        from jarvis.tools.web import weather_card
        from jarvis.sysutil import ToolError

        key = (self.cfg.get("location.city"), self.cfg.get("language"))
        cached = getattr(self, "_weather", None)
        if cached and cached[0] == key and time.monotonic() - cached[1] < 900 and not request.query.get("refresh"):
            return web.json_response(cached[2])
        try:
            data = {"ok": True, **await weather_card(self.cfg)}
        except (ToolError, Exception) as exc:  # noqa: BLE001 - the panel shows the reason
            return web.json_response({"ok": False, "error": str(exc)[:200]})
        self._weather = (key, time.monotonic(), data)
        return web.json_response(data)

    async def api_music(self, request: web.Request) -> web.Response:
        import time

        from jarvis.tools.youtube_music import player_snapshot

        cached = getattr(self, "_music", None)
        if cached and time.monotonic() - cached[0] < 1.5:
            return web.json_response(cached[1])
        data = await player_snapshot()
        self._music = (time.monotonic(), data)
        return web.json_response(data)

    async def api_music_action(self, request: web.Request) -> web.Response:
        """Transport buttons. The same verified youtube_music tool the voice commands use."""
        from jarvis.tools.youtube_music import player_snapshot

        data = await _json(request)
        action = str(data.get("action", ""))
        if action == "toggle":
            now = await player_snapshot()
            action = "pause" if now.get("available") and not now.get("paused") else "play"
        if action not in ("play", "pause", "next", "previous", "open", "liked"):
            return web.json_response({"error": "unknown action"}, status=400)
        args = {"action": action}
        if data.get("query"):
            import re

            query = str(data["query"]).strip()[:200]
            by = re.match(r"^(.+?)\s+by\s+(.+)$", query, re.I)  # "Shape of You by Ed Sheeran"
            if by and "ของ" not in query:
                args["query"], args["artist"] = by[1].strip(), by[2].strip()
            else:
                args["query"] = query
        result = await self.assistant.run_ui_tool("youtube_music", args)
        self._music = None
        return web.json_response({"result": result, "ok": not result.startswith(("Error:", "ทำรายการไม่สำเร็จ")),
                                  "state": await player_snapshot()})

    async def api_volume(self, request: web.Request) -> web.Response:
        from jarvis.sysutil import ToolError
        from jarvis.tools.system import _volume

        try:
            level, muted = await _volume()
        except ToolError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        return web.json_response({"ok": True, "level": level, "muted": muted})

    async def api_set_volume(self, request: web.Request) -> web.Response:
        from jarvis.sysutil import ToolError
        from jarvis.tools.system import _set_volume

        data = await _json(request)
        try:
            level = await _set_volume(int(data.get("level", 0)))
        except (ToolError, ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "level": level})

    # Command Center buttons map to fixed tool calls; the page never names a tool itself.
    UI_ACTIONS = {
        "open_ytm": [("youtube_music", {"action": "open"})],
        "open_chrome": [("open_app", {"name": "Google Chrome"})],
        "open_vscode": [("open_app", {"name": "Visual Studio Code"})],
        "study_mode": [("set_timer", {"duration": "25 minutes", "label": "โหมดอ่านหนังสือ"}),
                       ("youtube_music", {"action": "play", "query": "เพลงอ่านหนังสือ"})],
        "reminders_today": [("reminders", {"action": "list_today"})],
        "reminders_all": [("reminders", {"action": "list_all"})],
        "calendar_today": [("calendar", {"action": "list", "day": "today"})],
        "calendar_tomorrow": [("calendar", {"action": "list", "day": "tomorrow"})],
        "timers_list": [("timers", {"action": "list"})],
        "timers_cancel_all": [("timers", {"action": "cancel_all"})],
    }
    # Forms that create something: the tool's own confirmation still applies.
    UI_FORMS = {
        "reminder_create": ("reminders", {"action": "create"}, ("text", "when")),
        "event_create": ("calendar", {"action": "create"}, ("title", "start", "duration_minutes")),
        "timer_create": ("set_timer", {}, ("duration", "label")),
    }

    async def api_action(self, request: web.Request) -> web.Response:
        data = await _json(request)
        name = str(data.get("id", ""))
        steps = self.UI_ACTIONS.get(name)
        if steps is None and name in self.UI_FORMS:
            tool, base, fields = self.UI_FORMS[name]
            args = dict(base)
            for f in fields:
                if data.get(f) not in (None, ""):
                    args[f] = data[f] if f == "duration_minutes" else str(data[f])[:300]
            steps = [(tool, args)]
        if steps is None:
            return web.json_response({"error": "unknown action"}, status=400)
        results = []
        for tool, args in steps:
            result = await self.assistant.run_ui_tool(tool, dict(args))
            results.append(result)
            if result.startswith(("Error:", "ทำรายการไม่สำเร็จ", "ยกเลิก", "Cancelled")):
                break
        ok = not results[-1].startswith(("Error:", "ทำรายการไม่สำเร็จ", "ยกเลิก", "Cancelled"))
        return web.json_response({"ok": ok, "results": results, "text": " ".join(results)})

    async def api_history(self, request: web.Request) -> web.Response:
        try:
            limit = max(1, min(500, int(request.query.get("limit", 120))))
        except ValueError:
            limit = 120
        a = self.assistant
        return web.json_response({
            "turns": [t for t in a.memory.recent_turns(limit) if t["role"] in ("user", "assistant", "reset")],
            **a.learning.recent_episodes(min(limit, 100)),
        })

    async def api_devices(self, request: web.Request) -> web.Response:
        def query():
            import sounddevice as sd

            default_in = sd.default.device[0]
            return [{"index": i, "name": d["name"], "channels": d["max_input_channels"], "default": i == default_in}
                    for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]

        try:
            inputs = await asyncio.get_running_loop().run_in_executor(None, query)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"inputs": [], "error": str(exc)})
        return web.json_response({"inputs": inputs, "selected": self.cfg.get("listening.input_device"),
                                  "active": self.assistant.audio_in.device})

    async def api_uistats(self, request: web.Request) -> web.Response:
        """Ask every open Command Center for its measured frame rate (on demand only)."""
        replies: list = []
        self._stats_waiters = getattr(self, "_stats_waiters", [])
        self._stats_waiters.append(replies)
        try:
            self.assistant.broadcast({"type": "stats_request"})
            await asyncio.sleep(1.2)
        finally:
            self._stats_waiters.remove(replies)
        return web.json_response({"windows": replies})

    async def api_timers(self, request: web.Request) -> web.Response:
        items = self.assistant.scheduler.pending()
        return web.json_response({"items": [
            {"id": i.id, "kind": i.kind, "text": i.text, "label": i.label, "fire_at": i.fire_at} for i in items]})

    # ---------------------------------------------------------------- static
    async def index(self, request: web.Request) -> web.StreamResponse:
        return await self._serve(Path(UI_DIR) / "index.html")

    async def static_file(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["filename"]
        if ".." in name or name.startswith("/"):
            raise web.HTTPForbidden()
        return await self._serve(Path(UI_DIR) / name)

    async def _serve(self, path: Path) -> web.StreamResponse:
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def _json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
