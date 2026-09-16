"""Music control for Spotify and Apple Music."""

from __future__ import annotations

from urllib.parse import quote

from jarvis.sysutil import ToolError, applescript_quote, osascript, run
from jarvis.tools import ToolContext, tool

APPS = {"spotify": "Spotify", "music": "Music", "apple music": "Music", "itunes": "Music"}


async def _is_running(app: str) -> bool:
    # pgrep, not System Events: asking System Events costs an Automation grant
    # Jarvis may not have, and blocks for the full timeout when it does not.
    proc = await run(["/usr/bin/pgrep", "-x", app], timeout=5)
    return bool(proc.stdout.strip())


async def _state(app: str, timeout: float = 5.0) -> str:
    try:
        out = await osascript(f'tell application "{app}" to return player state as text', timeout=timeout)
        return out.strip().lower()
    except ToolError as exc:
        # "Not allowed to control Spotify" is not the same as "nothing is
        # playing", and silently conflating them hides the real fix.
        if "permission" in str(exc).lower() or "System Settings" in str(exc):
            raise
        return "stopped"


async def _pick_app(preferred: str = "") -> str:
    if preferred:
        name = APPS.get(preferred.strip().lower())
        if not name:
            raise ToolError(f"I only control Spotify and Music, not {preferred}")
        return name
    import asyncio

    checks = await asyncio.gather(*(_is_running(a) for a in ("Spotify", "Music")))
    running = [a for a, up in zip(("Spotify", "Music"), checks) if up]
    if not running:
        return "Spotify"  # nothing running: launching Spotify is the common case
    if len(running) == 1:
        return running[0]
    states = await asyncio.gather(*(_state(a) for a in running), return_exceptions=True)
    for app, state in zip(running, states):
        if state == "playing":
            return app
    return running[0]


async def _now_playing(app: str) -> str:
    state = await _state(app)
    if state not in ("playing", "paused"):
        return f"{app} isn't playing anything."
    try:
        track = await osascript(
            f'tell application "{app}" to return (name of current track) & " by " & (artist of current track)'
        )
    except ToolError:
        return f"{app} is {state}."
    verb = "Playing" if state == "playing" else "Paused on"
    return f"{verb} {track.strip()}."


@tool(
    name="media_control",
    description=(
        "Control music playback in Spotify or Apple Music: play, pause, skip, go back, say what is "
        "playing, change the app's volume, or start something by name. Use for 'play', 'pause', "
        "'skip this', 'what song is this', 'play some jazz'."
    ),
    params={
        "action": {"type": "string", "enum": ["play", "pause", "toggle", "next", "previous", "now_playing", "play_query", "shuffle_on", "shuffle_off", "volume_set"]},
        "query": {"type": "string", "description": "What to play, for play_query, e.g. 'Miles Davis'"},
        "app": {"type": "string", "enum": ["spotify", "music"], "description": "Which app; defaults to whichever is running"},
        "value": {"type": "number", "description": "0-100 for volume_set"},
    },
    required=["action"],
    category="media",
)
async def media_control(ctx: ToolContext, action: str, query: str = "", app: str = "", value: float = 50) -> str:
    action = action.lower()
    target = await _pick_app(app)

    if action == "now_playing":
        return await _now_playing(target)

    if action in ("play", "pause", "toggle"):
        if not await _is_running(target) and action != "pause":
            await run(["/usr/bin/open", "-a", target], timeout=15)
        command = {"play": "play", "pause": "pause", "toggle": "playpause"}[action]
        await osascript(f'tell application "{target}" to {command}')
        if action == "pause":
            return "Paused."
        return await _now_playing(target)

    if action in ("next", "previous"):
        verb = "next track" if action == "next" else "previous track"
        await osascript(f'tell application "{target}" to {verb}')
        return await _now_playing(target)

    if action in ("shuffle_on", "shuffle_off"):
        want = "true" if action == "shuffle_on" else "false"
        prop = "shuffling" if target == "Spotify" else "shuffle enabled"
        await osascript(f'tell application "{target}" to set {prop} to {want}')
        return f"Shuffle {'on' if want == 'true' else 'off'}."

    if action == "volume_set":
        level = max(0, min(100, int(value)))
        prop = "sound volume" if target == "Spotify" else "sound volume"
        await osascript(f'tell application "{target}" to set {prop} to {level}')
        return f"{target} volume at {level} percent."

    if action == "play_query":
        if not query:
            raise ToolError("say what to play")
        if target == "Music":
            try:
                await osascript(
                    f'tell application "Music"\n'
                    f'  set matches to (every track of library playlist 1 whose name contains {applescript_quote(query)} '
                    f'or artist contains {applescript_quote(query)})\n'
                    f'  if (count of matches) is 0 then error "nothing in the library matches"\n'
                    f'  play (item 1 of matches)\n'
                    f'end tell',
                    timeout=30,
                )
                return await _now_playing("Music")
            except ToolError as exc:
                raise ToolError(f"nothing in your Music library matches '{query}' ({exc})") from exc
        # Spotify's AppleScript dictionary has no search, so this opens the
        # search results rather than pretending to have started playback.
        await run(["/usr/bin/open", f"spotify:search:{quote(query)}"], timeout=15)
        return f"I've opened a Spotify search for {query} - Spotify won't let me press play on a search result, so pick one."

    raise ToolError(f"unknown action {action!r}")
