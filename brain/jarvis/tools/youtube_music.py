"""Local Chrome automation, restricted to YouTube Music tabs.

"play" with a song name searches YouTube Music, picks the result whose title
and artist match, starts it, and only reports success once the player shows
that track advancing. Opening/searching never claims playback. No credentials,
cookies, unofficial service API or authentication bypass.
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
from urllib.parse import quote
from jarvis.sysutil import ToolError, applescript_quote, osascript, run
from jarvis.tools import ToolContext, tool

log = logging.getLogger("jarvis.tools.youtube_music")
YTM = "https://music.youtube.com/"
JS_HELP = "อ่านสถานะ YouTube Music ไม่ได้ครับ เปิด Chrome > View > Developer > Allow JavaScript from Apple Events"


def _in_tab(body: str) -> str:
    """AppleScript that runs `body` against the first YouTube Music tab (`t`, window `w`, index `i`)."""
    return f'''tell application "Google Chrome"
repeat with w in windows
set i to 0
repeat with t in tabs of w
set i to i + 1
if URL of t starts with "{YTM}" then
{body}
end if
end repeat
end repeat
return "{{\\"error\\":\\"no_tab\\"}}"
end tell'''


async def _js_raw(code: str) -> dict:
    raw = await osascript(_in_tab(f"return execute t javascript {applescript_quote(code)}"), timeout=12)
    try:
        result = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ToolError(JS_HELP) from exc
    return result if isinstance(result, dict) else {"error": "bad_result"}


async def _js(code: str) -> dict:
    result = await _js_raw(code)
    if result.get("error"):
        raise ToolError("ไม่พบแท็บหรือเพลงใน YouTube Music ครับ เปิดเว็บและเลือกเพลงก่อน")
    return result


async def _navigate(url: str) -> None:
    """Load `url` in the existing YouTube Music tab (made the active tab), or open a new one."""
    body = f'set active tab index of w to i\nset URL of t to {applescript_quote(url)}\nreturn "{{\\"ok\\":true}}"'
    raw = await osascript(_in_tab(body), timeout=12)
    if "no_tab" not in raw:
        return
    proc = await run(["/usr/bin/open", "-a", "Google Chrome", url], timeout=15)
    proc.check("open YouTube Music")
    for _ in range(20):  # wait for Chrome to register the new tab
        await asyncio.sleep(0.4)
        if not (await _js_raw("JSON.stringify({ok:true})")).get("error"):
            return
    raise ToolError("เปิด YouTube Music ใน Chrome ไม่สำเร็จครับ")


STATE = '''JSON.stringify((()=>{const v=document.querySelector('video');return v&&v.currentSrc?{paused:v.paused,time:v.currentTime,src:v.currentSrc,title:document.title,ready:v.readyState}:{error:'no_media'}})())'''

# Song, artist and position of what is actually loaded in the player.
PLAYER = r'''JSON.stringify((()=>{
const v=document.querySelector('video'); const md=navigator.mediaSession&&navigator.mediaSession.metadata;
const tx=s=>{const e=document.querySelector(s);return e?(e.textContent||'').replace(/\s+/g,' ').trim():''};
const mp=document.querySelector('#movie_player');
let vid='';try{vid=(mp&&mp.getVideoData&&mp.getVideoData().video_id)||''}catch(e){}
if(!vid){const l=document.querySelector('#movie_player a.ytp-title-link');const m=l&&(l.href||'').match(/[?&]v=([\w-]{6,})/);if(m)vid=m[1];}
return {vid:vid||new URLSearchParams(location.search).get('v')||'', paused:v?v.paused:true, time:v?v.currentTime:0,
 ready:v?v.readyState:0, title:(md&&md.title)||tx('ytmusic-player-bar .title'),
 artist:(md&&md.artist)||tx('ytmusic-player-bar .byline'), ad:!!(mp&&mp.classList.contains('ad-showing'))};
})())'''

# Playable results on the search page, in page order (top result first).
RESULTS = r'''JSON.stringify((()=>{
const q=new URLSearchParams(location.search).get('q');
const page=document.querySelector('ytmusic-search-page');
if(!page) return {loading:true,q};
const tx=e=>e?(e.textContent||'').replace(/\s+/g,' ').trim():'';
const out=[]; const seen=new Set();
for(const it of page.querySelectorAll('ytmusic-card-shelf-renderer, ytmusic-responsive-list-item-renderer')){
  const card=it.tagName==='YTMUSIC-CARD-SHELF-RENDERER';
  const a=card?it.querySelector('.main-card-title a, .main-card-content-container .title a'):it.querySelector('.title-column a[href*="watch?v="], a[href*="watch?v="]');
  const m=a&&(a.getAttribute('href')||'').match(/[?&]v=([\w-]{6,})/);
  if(!m||seen.has(m[1])) continue; seen.add(m[1]);
  const shelf=it.closest('ytmusic-shelf-renderer');
  out.push({id:m[1], title:tx(a)||tx(it.querySelector('.title')),
    sub:card?tx(it.querySelector('.subtitle')):tx(it.querySelector('.secondary-flex-columns')),
    shelf:shelf?tx(shelf.querySelector('h2')):'', top:card});
  if(out.length>=15) break;
}
return {q, items:out};
})())'''


def _clickjs(video_id: str) -> str:
    vid = json.dumps(video_id)
    return ("JSON.stringify((()=>{const id=" + vid + ";"
            "const a=[...document.querySelectorAll('ytmusic-search-page a[href*=\"watch?v=\"]')]"
            ".find(x=>(x.getAttribute('href')||'').includes('v='+id));"
            "if(!a)return {error:'gone'};a.click();return {ok:true}})())")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (text or "").casefold()).split())


_SPLIT_ARTIST = re.compile(r"^(.+?)\s+(?:ของ|โดย|จาก)\s*(.+)$")


def split_title_artist(query: str, artist: str = "") -> tuple[str, str]:
    """'Shape of You ของ Ed Sheeran' -> ('Shape of You', 'Ed Sheeran'). An explicit artist wins."""
    query, artist = (query or "").strip(), (artist or "").strip()
    if not artist:
        m = _SPLIT_ARTIST.match(query)
        if m:
            query, artist = m[1].strip(), m[2].strip()
    if artist and _norm(query).endswith(" " + _norm(artist)):  # "Lemonade Internet Money" + artist
        query = query[: len(query) - len(artist)].strip()
    return query, artist


def title_closeness(wanted: str, shown: str) -> float:
    """1.0 for the same title; 'Shape of You' vs 'Shape of You (Official Video)' still scores high."""
    import difflib

    if not wanted or not shown:
        return 0.0
    if wanted == shown:
        return 1.0
    if wanted in shown or shown in wanted:
        return 0.75  # a version of it ("(Cover)", "(Live)"): playable, but an exact title wins
    words = set(wanted.split())
    overlap = len(words & set(shown.split())) / len(words)
    spelling = difflib.SequenceMatcher(None, wanted.replace(" ", ""), shown.replace(" ", "")).ratio()
    # Misheard spelling ("shape of u") is close letter by letter; a different song
    # with similar letters ("shake it off") is not, so demand 0.8 before trusting it.
    return min(0.74, max(overlap, spelling if spelling >= 0.8 else 0.0))


def _kind(item: dict) -> str:
    text = _norm(item.get("shelf", "") + " " + item.get("sub", ""))
    first = text.split(" ")[0] if text else ""
    if first in {"songs", "song", "เพลง"}:
        return "song"
    if first in {"videos", "video", "วิดีโอ", "วีดีโอ"}:
        return "video"
    if first in {"episodes", "episode", "podcasts", "podcast", "ตอน", "พอดแคสต์"}:
        return "episode"
    return "other"


def choose_result(items: list[dict], title: str, artist: str = "") -> dict | None:
    """Best playable result. With an artist named, only results crediting that artist qualify."""
    t, a = _norm(title), _norm(artist)
    best, best_score = None, float("-inf")
    for pos, item in enumerate(items):
        name = _norm(item.get("title", ""))
        credits = _norm(item.get("sub", ""))
        if a and a not in credits:  # credits, not the title: "X (Taylor Swift mix)" is not by Taylor Swift
            continue
        kind = _kind(item)
        if kind == "episode":
            continue
        closeness = title_closeness(t, name) if t else 1.0  # artist-only or mood: YouTube Music's order decides
        if closeness < 0.6:  # never play a different song just because the artist matched
            continue
        score = -0.15 * pos + 3 * closeness
        score += {"song": 0.6, "video": 0.2}.get(kind, 0) + (0.3 if item.get("top") else 0)
        if score > best_score:
            best, best_score = item, score
    return best


# "เพลงสบายๆ", "เพลงเศร้า", "lofi": a mood or genre, not a title - play the best match for it.
MOOD = re.compile(r"^(?:เพลง)?\s*(?:แนว\s*)?(?:สบาย|ชิล|เศร้า|เพราะ|ฮิต|ใหม่|สนุก|แดนซ์|ลูกทุ่ง|ร็อค|ร็อก|แจ๊ส|แจ๊ซ|เพื่อชีวิต|อกหัก|"
                  r"ผ่อนคลาย|ก่อนนอน|กล่อมนอน|นอน|โฟกัส|ทำงาน|อ่านหนังสือ|ออกกำลัง|ปาร์ตี้|มันส์|คลาสสิก|บรรเลง|"
                  r"lofi|lo-fi|chill|jazz|rock|pop|edm|kpop|k-pop|t-pop|ไทย|สากล|เกาหลี|ญี่ปุ่น|จีน|ฮิปฮอป|แร็พ)"
                  r"(?:\S*)\s*(?:ๆ|\S*)?$", re.I)


async def play_song(query: str, artist: str = "") -> str:
    title, artist = split_title_artist(query, artist)
    by = re.match(r"^(?:ของ|โดย|by)\s+(.+)$", title, re.I)
    if by and not artist:  # "เพลงของ Ed Sheeran": an artist, no title
        title, artist = "", by[1].strip()
    mood = bool(title) and not artist and bool(MOOD.match(title))
    if not title and not artist:
        raise ToolError("ระบุชื่อเพลงด้วยครับ")
    search = (f"เพลง{title}" if mood and not title.startswith("เพลง") and re.search(r"[\u0e00-\u0e7f]", title)
              else f"{title} {artist}".strip())
    await _navigate(YTM + "search?q=" + quote(search))

    items: list[dict] = []
    for _ in range(30):  # results render a moment after the page loads
        await asyncio.sleep(0.4)
        page = await _js_raw(RESULTS)
        if page.get("error") == "no_tab":
            raise ToolError("ไม่พบแท็บ YouTube Music ใน Chrome ครับ")
        if page.get("q") == search and page.get("items"):
            items = page["items"]
            await asyncio.sleep(0.4)  # let the remaining shelves finish rendering
            items = (await _js_raw(RESULTS)).get("items") or items
            break
    if not items:
        raise ToolError(f"ไม่พบผลค้นหา {search} ใน YouTube Music ครับ")

    pick = choose_result(items, "" if mood else title, artist)
    log.info("youtube_music %r by %r: %d results, picked %s", title, artist, len(items),
             pick and {k: pick[k] for k in ("id", "title", "sub")})
    if pick is None:
        first = items[0]
        wanted = f"{title} ของ {artist}".strip() if artist else title
        raise ToolError(f"ไม่พบเพลง {wanted} ใน YouTube Music ครับ ยังไม่ได้เล่น ผลแรกที่เจอคือ {first['title']} {first.get('sub', '')}".strip())

    before = await _js_raw(PLAYER)
    clicked = await _js_raw(_clickjs(pick["id"]))
    if clicked.get("error"):
        await _navigate(YTM + "watch?v=" + pick["id"])
    return await _verify_playing(pick, before)


def _compact(text: str) -> str:
    return _norm(text).replace(" ", "")


def _is_pick(state: dict, pick: dict) -> bool:
    """The player holds the chosen track: same video id, or the same title and artist.

    The player bar drops suffixes the search row shows ("Lemonade" vs "Lemonade
    (ร่วมกับ NAV)") and YouTube Music may swap a song for its video twin, so the
    title may be a prefix; the artist credit must still match the chosen result.
    """
    if state.get("vid") and state["vid"] == pick["id"]:
        return True
    shown, wanted = _compact(state.get("title", "")), _compact(pick.get("title", ""))
    same_title = bool(shown) and (shown == wanted or (wanted.startswith(shown) and len(shown) >= 4))
    artist = _compact(state.get("artist", ""))
    same_artist = not artist or artist in _compact(pick.get("sub", ""))
    return same_title and same_artist


async def _verify_playing(pick: dict, before: dict) -> str:
    last: dict = {}
    seen: dict = {}
    nudged = False
    start_time = None
    for step in range(26):  # ~12s: page switch, buffering, possibly an ad
        await asyncio.sleep(0.3)
        state = await _js_raw(PLAYER)
        seen = state
        if state.get("error") or not _is_pick(state, pick):
            continue
        last = state
        if state.get("ad"):
            return f"เปิดเพลง {pick['title']} แล้วครับ ตอนนี้เป็นโฆษณาก่อนเพลง"
        if state["paused"]:
            if step >= 10 and not nudged:  # loaded but not started: autoplay was blocked
                nudged = True
                await _js_raw("JSON.stringify((()=>{const v=document.querySelector('video');if(v)v.play().catch(()=>{});return {ok:true}})())")
            continue
        if start_time is None:
            start_time = state["time"]
        elif state["time"] > start_time and state.get("ready", 0) >= 2:
            name = state.get("title") or pick["title"]
            by = state.get("artist") or ""
            return f"กำลังเล่น {name}" + (f" ของ {by}" if by else "") + " ครับ"
    log.info("youtube_music could not verify %s; player showed %s", pick.get("id"), seen)
    if last and last.get("paused"):
        raise ToolError(f"เปิดเพลง {pick['title']} แล้ว แต่ Chrome ยังไม่เริ่มเล่นครับ กดปุ่มเล่นในหน้า YouTube Music หนึ่งครั้ง")
    if before.get("vid") and not last:
        raise ToolError(f"เลือกเพลง {pick['title']} แล้ว แต่ตัวเล่นยังไม่เปลี่ยนเพลงครับ ตรวจสอบหน้า YouTube Music ใน Chrome")
    raise ToolError(f"เลือกเพลง {pick['title']} แล้ว แต่ยังยืนยันไม่ได้ว่ากำลังเล่นครับ ตรวจสอบหน้า YouTube Music ใน Chrome")


@tool(name="youtube_music",
      description="YouTube Music in Chrome. play + query (song, optionally artist) finds and plays that exact song; "
                  "artist alone plays their top song; play without query resumes; search only shows results; "
                  "pause/next/previous/now_playing control the player.",
      params={"action": {"type": "string", "enum": ["open", "liked", "search", "play", "pause", "next", "previous", "now_playing"]},
              "query": {"type": "string", "description": "song title"},
              "artist": {"type": "string", "description": "artist, if the user named one"}},
      required=["action"], category="media")
async def youtube_music(ctx: ToolContext, action: str, query: str = "", artist: str = "") -> str:
    action = (action or "").strip().lower()
    if action in {"play_query", "play_song", "search_and_play", "search_play", "play_search"}:
        action = "play"
    if action == "play" and (query.strip() or artist.strip()):
        return await play_song(query, artist)
    if action in {"open", "liked", "search"}:
        url = YTM
        if action == "liked":
            url += "playlist?list=LM"
        elif action == "search":
            search = f"{query} {artist}".strip()
            if not search:
                raise ToolError("ระบุชื่อเพลงหรือศิลปินด้วยครับ")
            url += "search?q=" + quote(search)
        proc = await run(["/usr/bin/open", "-a", "Google Chrome", url], timeout=15)
        proc.check("open YouTube Music")
        if action == "search":
            return f"เปิดผลค้นหา {search} ใน YouTube Music แล้วครับ เลือกเพลงเพื่อเริ่มเล่นได้เลย"
        if action == "liked":
            return "เปิดเพลย์ลิสต์เพลงที่ชอบแล้วครับ หากยังไม่ได้ลงชื่อเข้าใช้ ให้ลงชื่อเข้าใช้ใน Chrome ก่อน"
        return "เปิด YouTube Music ให้แล้วครับ"
    if action == "now_playing":
        now = await _js_raw(PLAYER)
        if now.get("error") == "no_tab":
            raise ToolError("ไม่พบแท็บ YouTube Music ใน Chrome ครับ")
        if not now.get("title") or (now.get("time", 0) == 0 and now.get("paused")):
            return "ตอนนี้ยังไม่มีเพลงเล่นอยู่ใน YouTube Music ครับ"
        return (("หยุดอยู่ที่ " if now["paused"] else "กำลังเล่น ") + now["title"]
                + (f" ของ {now['artist']}" if now.get("artist") else "") + " ครับ")
    before = await _js(STATE)
    track = await _js_raw(PLAYER) if action in ("next", "previous") else {}  # the song before the click
    if action in {"play", "pause"}:
        method = "play().catch(()=>{})" if action == "play" else "pause()"
        await _js("JSON.stringify((()=>{const v=document.querySelector('video');if(!v)return {error:'no_media'};v." + method + ";return {ok:true}})())")
    elif action in ("next", "previous"):
        button = "next-button" if action == "next" else "previous-button"
        await _js("JSON.stringify((()=>{const b=document.querySelector('ytmusic-player-bar ." + button + "');if(!b)return {error:'no_button'};b.click();return {ok:true}})())")
    else:
        raise ToolError("คำสั่ง YouTube Music ไม่ถูกต้องครับ")
    for _ in range(12):
        await asyncio.sleep(0.25)
        after = await _js(STATE)
        if action in ("next", "previous"):
            now = await _js_raw(PLAYER)
            if now.get("title") and now.get("title") != track.get("title"):
                verb = "เปลี่ยนเพลงแล้วครับ " if action == "next" else "ย้อนไปเพลงก่อนหน้าแล้วครับ "
                return verb + now["title"] + (f" ของ {now['artist']}" if now.get("artist") else "")
            if action == "previous" and now.get("time", 99) < track.get("time", 0) - 1:
                return "เริ่มเพลงนี้ใหม่ตั้งแต่ต้นแล้วครับ ไม่มีเพลงก่อนหน้าในคิว"
        if action == "pause" and after["paused"]:
            return "หยุดเพลงแล้วครับ"
        if action == "play" and not after["paused"] and after["time"] > before["time"] and after["ready"] >= 2:
            return "เล่นเพลงต่อแล้วครับ"

    raise ToolError("ส่งคำสั่งแล้ว แต่ยังยืนยันผลไม่ได้ครับ ตรวจสอบเครื่องเล่น YouTube Music ใน Chrome")
