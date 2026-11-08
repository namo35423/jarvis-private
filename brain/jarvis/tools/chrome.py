"""Driving the user's real Chrome, through AppleScript.

Chrome 136+ refuses --remote-debugging-port on the default profile, so the
DevTools protocol is not an option for the browser the user is actually signed
into. AppleScript can always see tabs and URLs; reading and clicking inside a
page additionally needs View > Developer > Allow JavaScript from Apple Events,
which is off by default - so every JS path degrades to fetching the URL instead
and says which route it took.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import quote, urlparse

import httpx

from jarvis.sysutil import ToolError, applescript_quote, osascript, run, truncate
from jarvis.tools import ToolContext, tool

APP = "Google Chrome"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"

JS_DISABLED = re.compile(r"javascript.*(turned off|not allowed|disabled)|applescript", re.I)
RISKY_WORDS = re.compile(
    r"\b(buy|purchase|order|checkout|pay|payment|subscribe|delete|remove|send|post|publish|"
    r"confirm|transfer|withdraw|book|reserve|sign out|log out|cancel subscription)\b", re.I
)

_js_state = {"ok": None, "checked": 0.0}


def _confirm_click(args: dict) -> str | None:
    if (args.get("action") or "") not in ("click", "submit"):
        return None
    target = args.get("target") or ""
    if RISKY_WORDS.search(target) or re.search("ซื้อ|จ่าย|โอน|ชำระ|ส่ง|ลบ|ยืนยัน|จอง|เผยแพร่", target) or (args.get("action") == "submit") :
        return f"Click '{target.strip()}' in the browser?"
    return None


async def _tell(script: str, timeout: float = 6.0) -> str:
    return await osascript(f'tell application "{APP}"\n{script}\nend tell', timeout=timeout)


async def _running() -> bool:
    # pgrep needs no Automation permission, so this probe can never hang on TCC.
    proc = await run(["/usr/bin/pgrep", "-x", "Google Chrome"], timeout=5)
    return bool(proc.stdout.strip())


async def _js(code: str, timeout: float = 25.0) -> str:
    """Run JS in the active tab. Raises a helpful ToolError when not permitted."""
    if _js_state["ok"] is False and time.time() - _js_state["checked"] < 60:
        raise ToolError(
            "Chrome will not let me run JavaScript yet. In Chrome's menu bar choose "
            "View > Developer > Allow JavaScript from Apple Events, then ask me again."
        )
    script = f"execute front window's active tab javascript {applescript_quote(code)}"
    try:
        out = await _tell(script, timeout=timeout)
    except ToolError as exc:
        if JS_DISABLED.search(str(exc)):
            _js_state.update(ok=False, checked=time.time())
            raise ToolError(
                "Chrome will not let me run JavaScript yet. In Chrome's menu bar choose "
                "View > Developer > Allow JavaScript from Apple Events, then ask me again."
            ) from exc
        raise
    _js_state.update(ok=True, checked=time.time())
    return out


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _looks_like_url(text: str) -> bool:
    t = (text or "").strip()
    if t.startswith(("http://", "https://", "file://")):
        return True
    return bool(re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", t))


@tool(
    name="open_url",
    description=(
        "Open a web page in Chrome. Accepts a full URL, a bare domain like 'nytimes.com', or a search "
        "phrase (which opens a search). Use for 'open X', 'go to X', 'pull up X', 'search the web for X'."
    ),
    params={"url": {"type": "string", "description": "URL, domain, or what to search for"}},
    required=["url"],
    ack="Opening it now.",
    category="browser",
)
async def open_url(ctx: ToolContext, url: str) -> str:
    target = (url or "").strip()
    if not target:
        raise ToolError("no address given")
    if _looks_like_url(target):
        if not target.startswith(("http://", "https://", "file://")):
            target = f"https://{target}"
        label = _domain(target) or target
    else:
        target = f"https://duckduckgo.com/?q={quote(target)}"
        label = f"a search for {url.strip()}"
    proc = await run(["/usr/bin/open", "-a", APP, target], timeout=15)
    proc.check("open Chrome")
    return f"Opened {label}."


@tool(
    name="chrome_tabs",
    description=(
        "See and manage Chrome tabs: list what is open, switch to one, close one, open a new one, "
        "reload, or go back. Use for 'what tabs do I have', 'switch to my email', 'close this tab'."
    ),
    params={
        "action": {"type": "string", "enum": ["list", "switch", "close", "new", "reload", "back", "forward"]},
        "index": {"type": "integer", "description": "Tab number from the list"},
        "match": {"type": "string", "description": "Words from the tab's title or address, e.g. 'gmail'"},
        "url": {"type": "string", "description": "Address for the new action"},
    },
    required=["action"],
    category="browser",
)
async def chrome_tabs(ctx: ToolContext, action: str, index: int = 0, match: str = "", url: str = "") -> str:
    action = action.lower()
    if action == "new":
        return await open_url(ctx, url or "about:blank")

    if not await _running():
        raise ToolError("Chrome is not running")

    # Bulk property access ("title of tabs of window w") is ~5x faster than
    # walking tab objects one at a time, which matters with 25 tabs open.
    raw = await _tell(
        'set out to ""\n'
        'repeat with w from 1 to count of windows\n'
        '  set ts to title of tabs of window w\n'
        '  set us to URL of tabs of window w\n'
        '  repeat with i from 1 to count of ts\n'
        '    set out to out & w & "\t" & i & "\t" & (item i of ts) & "\t" & (item i of us) & "\n"\n'
        '  end repeat\n'
        'end repeat\n'
        'return out'
    )
    tabs = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            tabs.append({"win": int(parts[0]), "tab": int(parts[1]), "title": parts[2], "url": parts[3]})
    if not tabs:
        raise ToolError("Chrome has no open tabs")

    active_idx = 0
    try:
        active = await _tell("return active tab index of front window")
        active_idx = int(active)
    except (ToolError, ValueError):
        pass

    if action == "list":
        lines = []
        for i, t in enumerate(tabs[:12], 1):
            marker = " (active)" if t["tab"] == active_idx and t["win"] == 1 else ""
            lines.append(f"{i}. {truncate(t['title'], 45, '…')} - {_domain(t['url'])}{marker}")
        more = f" …and {len(tabs) - 12} more" if len(tabs) > 12 else ""
        return f"{len(tabs)} tabs open:\n" + "\n".join(lines) + more

    if action in ("reload", "back", "forward"):
        verb = {"reload": "reload", "back": "go back", "forward": "go forward"}[action]
        await _tell(f"tell active tab of front window to {verb}")
        return f"Done ({action})."

    if action == "close" and not index and not match:  # "close this tab"
        await _tell("close active tab of front window")
        return "Closed the current tab."

    chosen = None
    if index and 1 <= int(index) <= len(tabs):
        chosen = tabs[int(index) - 1]
    elif match:
        needle = match.lower()
        for t in tabs:
            if needle in t["title"].lower() or needle in t["url"].lower():
                chosen = t
                break
    if chosen is None:
        raise ToolError(f"no tab matches {match or index!r}; call chrome_tabs list first")

    if action == "switch":
        await _tell(
            f'set active tab index of window {chosen["win"]} to {chosen["tab"]}\n'
            f'set index of window {chosen["win"]} to 1\n'
            f'activate'
        )
        return f"Switched to {truncate(chosen['title'], 60, '…')}."
    if action == "close":
        await _tell(f'close tab {chosen["tab"]} of window {chosen["win"]}')
        return f"Closed {truncate(chosen['title'], 50, '…')}."
    raise ToolError(f"unknown action {action!r}")


READ_JS = """(function(){
  var t = document.title || '';
  var b = document.body ? document.body.innerText : '';
  return JSON.stringify({title: t, url: location.href, text: b.slice(0, 12000)});
})()"""

LINKS_JS = """(function(){
  var out = [];
  var els = document.querySelectorAll('a[href]');
  for (var i = 0; i < els.length && out.length < 40; i++) {
    var e = els[i];
    if (e.offsetParent === null) continue;
    var t = (e.innerText || '').trim().replace(/\\s+/g, ' ');
    if (t) out.push({text: t.slice(0, 80), href: e.href});
  }
  return JSON.stringify({links: out, url: location.href});
})()"""

FIND_JS = """(function(){
  var sel = 'a[href],button,input,textarea,select,[role=button],[onclick]';
  var els = document.querySelectorAll(sel), out = [];
  for (var i = 0; i < els.length && out.length < 40; i++) {
    var e = els[i];
    if (e.offsetParent === null) continue;
    var label = (e.innerText || e.value || e.getAttribute('aria-label') || e.placeholder || e.title || '').trim();
    label = label.replace(/\\s+/g, ' ').slice(0, 70);
    if (!label && e.tagName !== 'INPUT') continue;
    out.push({i: out.length + 1, tag: e.tagName.toLowerCase(), type: e.type || '', label: label});
  }
  return JSON.stringify({elements: out});
})()"""


@tool(
    name="chrome_read_page",
    description=(
        "Read what is on the page in the active Chrome tab: its text, its links, or just the title. "
        "Use for 'what does this say', 'summarise this page', 'what's on screen in the browser'."
    ),
    params={"mode": {"type": "string", "enum": ["text", "links", "title"], "description": "What to extract, default text"}},
    category="browser",
)
async def chrome_read_page(ctx: ToolContext, mode: str = "text") -> str:
    if not await _running():
        raise ToolError("Chrome is not running")
    title = await _tell("return title of active tab of front window")
    url = await _tell("return URL of active tab of front window")
    if mode == "title":
        return f"{title} ({_domain(url)})"

    try:
        raw = await _js(LINKS_JS if mode == "links" else READ_JS)
        data = json.loads(raw)
        if mode == "links":
            links = data.get("links", [])
            if not links:
                return f"No links on {title}."
            listed = "\n".join(f"{i}. {l['text']} - {_domain(l['href'])}" for i, l in enumerate(links[:25], 1))
            return f"Links on {title}:\n{listed}"
        text = (data.get("text") or "").strip()
        if text:
            return f"{data.get('title') or title}\n\n{truncate(_tidy(text), 3000)}"
    except ToolError as exc:
        # JS is off: fall back to fetching the URL, which cannot see logged-in pages.
        fallback = await _fetch_readable(url)
        if fallback:
            return f"{title}\n\n{fallback}\n\n(Read by downloading the page, not from your tab: {exc})"
        raise
    return f"{title}: the page had no readable text."


def _tidy(text: str) -> str:
    lines = [l.strip() for l in text.splitlines()]
    kept = [l for l in lines if l]
    return "\n".join(kept)


async def _fetch_readable(url: str, max_chars: int = 3000) -> str:
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0, headers={"User-Agent": UA}) as c:
            r = await c.get(url)
            r.raise_for_status()
            html = r.text
    except (httpx.HTTPError, ValueError):
        return ""
    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    return truncate(re.sub(r"\n{3,}", "\n\n", text).strip(), max_chars)


@tool(
    name="chrome_act",
    description=(
        "Interact with the page in the active Chrome tab: click a button or link by its visible text, "
        "type into a field, scroll, or list the clickable elements. Use for 'click sign in', "
        "'type my email in the search box', 'scroll down'."
    ),
    params={
        "action": {"type": "string", "enum": ["click", "type", "submit", "scroll", "find"]},
        "target": {"type": "string", "description": "Visible text of the button/link, or the field's label"},
        "text": {"type": "string", "description": "Text to type for the type action"},
        "direction": {"type": "string", "enum": ["up", "down", "top", "bottom"], "description": "For scroll"},
    },
    required=["action"],
    risk=_confirm_click,
    category="browser",
)
async def chrome_act(ctx: ToolContext, action: str, target: str = "", text: str = "", direction: str = "down") -> str:
    if not await _running():
        raise ToolError("Chrome is not running")
    action = action.lower()

    if action == "find":
        data = json.loads(await _js(FIND_JS))
        els = data.get("elements", [])
        if not els:
            return "No interactive elements found."
        return "On this page:\n" + "\n".join(
            f"{e['i']}. {e['label'] or '(unlabelled ' + e['tag'] + ')'} [{e['tag']}{'/' + e['type'] if e['type'] else ''}]"
            for e in els[:25]
        )

    if action == "scroll":
        amounts = {"down": "window.scrollBy(0, window.innerHeight*0.8)",
                   "up": "window.scrollBy(0, -window.innerHeight*0.8)",
                   "top": "window.scrollTo(0,0)",
                   "bottom": "window.scrollTo(0, document.body.scrollHeight)"}
        await _js(f"(function(){{{amounts.get(direction, amounts['down'])}; return 'ok';}})()")
        return f"Scrolled {direction}."

    if action in ("click", "submit"):
        if not target and action == "click":
            raise ToolError("say what to click")
        js = _CLICK_JS.replace("__TARGET__", json.dumps(target or "")[1:-1])
        result = json.loads(await _js(js))
        if not result.get("ok"):
            raise ToolError(f"nothing on the page matches '{target}'. Try chrome_act find to list what is there.")
        return f"Clicked '{result.get('clicked', target)}'."

    if action == "type":
        if not text:
            raise ToolError("say what to type")
        js = _TYPE_JS.replace("__TARGET__", json.dumps(target or "")[1:-1]).replace("__TEXT__", json.dumps(text)[1:-1])
        result = json.loads(await _js(js))
        if not result.get("ok"):
            raise ToolError(f"could not find a field for '{target}'")
        return f"Typed into {result.get('field', 'the field')}."

    raise ToolError(f"unknown action {action!r}")


_CLICK_JS = """(function(){
  var q = "__TARGET__".trim().toLowerCase();
  var els = document.querySelectorAll('a,button,input[type=submit],input[type=button],[role=button],[onclick],label');
  var norm = function(s){ return (s||'').trim().toLowerCase().replace(/\\s+/g,' '); };
  var best = null, score = 0;
  for (var i = 0; i < els.length; i++) {
    var e = els[i];
    if (e.offsetParent === null) continue;
    var t = norm(e.innerText || e.value || e.getAttribute('aria-label') || e.title);
    if (!t) continue;
    var s = t === q ? 1 : (t.indexOf(q) === 0 ? 0.8 : (t.indexOf(q) > -1 ? 0.6 : 0));
    if (s > score) { score = s; best = e; }
  }
  if (!best || score === 0) return JSON.stringify({ok:false});
  best.scrollIntoView({block:'center'});
  best.click();
  return JSON.stringify({ok:true, clicked: norm(best.innerText || best.value).slice(0,60)});
})()"""

_TYPE_JS = """(function(){
  var q = "__TARGET__".trim().toLowerCase(), val = "__TEXT__";
  var fields = document.querySelectorAll('input:not([type=hidden]),textarea,[contenteditable=true]');
  var norm = function(s){ return (s||'').trim().toLowerCase().replace(/\\s+/g,' '); };
  var best = null;
  if (q) {
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (f.offsetParent === null) continue;
      var label = norm(f.placeholder || f.getAttribute('aria-label') || f.name || f.id);
      if (label.indexOf(q) > -1) { best = f; break; }
    }
  }
  if (!best) best = document.activeElement && document.activeElement.tagName !== 'BODY' ? document.activeElement : fields[0];
  if (!best) return JSON.stringify({ok:false});
  best.focus();
  if (best.isContentEditable) { best.textContent = val; }
  else {
    var setter = Object.getOwnPropertyDescriptor(best.constructor.prototype, 'value');
    if (setter && setter.set) { setter.set.call(best, val); } else { best.value = val; }
  }
  best.dispatchEvent(new Event('input', {bubbles:true}));
  best.dispatchEvent(new Event('change', {bubbles:true}));
  return JSON.stringify({ok:true, field: norm(best.placeholder || best.name || best.id || 'the field').slice(0,40)});
})()"""
