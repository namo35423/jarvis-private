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

from aiohttp import WSMsgType, web

from jarvis.config import UI_DIR
from jarvis.core import Assistant

log = logging.getLogger("jarvis.server")


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
        self.app = web.Application()
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
        a.router.add_get("/api/config", self.api_config)
        a.router.add_post("/api/config", self.api_set_config)
        a.router.add_get("/", self.index)
        a.router.add_get("/{filename:.+}", self.static_file)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        host = self.cfg.get("server.host", "127.0.0.1")
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

    def _apply_config(self, path: str, value) -> None:
        """Some settings need a live component update, not just a stored value."""
        a = self.assistant
        if path == "voice.volume":
            a.audio_out.volume = float(value)
        elif path == "listening.chimes":
            a.earcons.enabled = bool(value)
        elif path == "listening.enabled":
            a.listener.set_enabled(bool(value))
        elif path in ("assistant_name", "user_name", "address_as"):
            a.rebuild_system_prompt()

    async def _preview_voice(self, voice: str) -> None:
        if not voice:
            return
        sample = "Good evening. Systems are online and everything is running locally."
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

    async def api_config(self, request: web.Request) -> web.Response:
        return web.json_response(self.cfg.as_dict())

    async def api_set_config(self, request: web.Request) -> web.Response:
        data = await request.json()
        path, value = data.get("path"), data.get("value")
        if not path:
            return web.json_response({"error": "path is required"}, status=400)
        self.cfg.set(path, value)
        self._apply_config(path, value)
        return web.json_response({"ok": True, "config": self.cfg.as_dict()})

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
