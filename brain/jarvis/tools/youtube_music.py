"""Local Chrome automation, restricted to an existing YouTube Music tab.

Opening/searching never claims playback. Controls inspect the video element.
No credentials, cookies, unofficial service API or authentication bypass.
"""
from __future__ import annotations
import asyncio
import json
from urllib.parse import quote
from jarvis.sysutil import ToolError, applescript_quote, osascript, run
from jarvis.tools import ToolContext, tool

async def _js(code: str) -> dict:
    script = f'''tell application "Google Chrome"
repeat with w in windows
repeat with t in tabs of w
if URL of t starts with "https://music.youtube.com/" then
return execute t javascript {applescript_quote(code)}
end if
end repeat
end repeat
return "{{\\"error\\":\\"no_tab\\"}}"
end tell'''
    raw = await osascript(script, timeout=12)
    try:
        result = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ToolError("อ่านสถานะ YouTube Music ไม่ได้ครับ เปิด Chrome > View > Developer > Allow JavaScript from Apple Events") from exc
    if result.get("error"):
        raise ToolError("ไม่พบแท็บหรือเพลงใน YouTube Music ครับ เปิดเว็บและเลือกเพลงก่อน")
    return result

STATE = '''JSON.stringify((()=>{const v=document.querySelector('video');return v&&v.currentSrc?{paused:v.paused,time:v.currentTime,src:v.currentSrc,title:document.title,ready:v.readyState}:{error:'no_media'}})())'''

@tool(name="youtube_music", description="Open YouTube Music, liked playlist or song search. Control an existing Chrome player with verified play/pause/next; login and JavaScript Apple Events permission may be needed.",
      params={"action": {"type": "string", "enum": ["open", "liked", "search", "play", "pause", "next", "now_playing"]}, "query": {"type": "string"}}, required=["action"], category="media")
async def youtube_music(ctx: ToolContext, action: str, query: str = "") -> str:
    if action in {"open", "liked", "search"}:
        url = "https://music.youtube.com/"
        if action == "liked":
            url += "playlist?list=LM"
        elif action == "search":
            if not query.strip():
                raise ToolError("ระบุชื่อเพลงหรือศิลปินด้วยครับ")
            url += "search?q=" + quote(query.strip())
        proc = await run(["/usr/bin/open", "-a", "Google Chrome", url], timeout=15)
        proc.check("open YouTube Music")
        if action == "search":
            return f"เปิดผลค้นหา {query} ใน YouTube Music แล้วครับ เลือกเพลงเพื่อเริ่มเล่นได้เลย"
        if action == "liked":
            return "เปิดเพลย์ลิสต์เพลงที่ชอบแล้วครับ หากยังไม่ได้ลงชื่อเข้าใช้ ให้ลงชื่อเข้าใช้ใน Chrome ก่อน"
        return "เปิด YouTube Music ให้แล้วครับ"
    before = await _js(STATE)
    if action == "now_playing":
        return ("หยุดอยู่ที่ " if before["paused"] else "กำลังเล่น ") + before["title"]
    if action in {"play", "pause"}:
        method = "play().catch(()=>{})" if action == "play" else "pause()"
        await _js("JSON.stringify((()=>{const v=document.querySelector('video');if(!v)return {error:'no_media'};v." + method + ";return {ok:true}})())")
    elif action == "next":
        await _js("JSON.stringify((()=>{const b=document.querySelector('ytmusic-player-bar .next-button');if(!b)return {error:'no_next'};b.click();return {ok:true}})())")
    else:
        raise ToolError("คำสั่ง YouTube Music ไม่ถูกต้องครับ")
    for _ in range(8):
        await asyncio.sleep(0.25)
        after = await _js(STATE)
        if action == "pause" and after["paused"]:
            return "หยุดเพลงแล้วครับ"
        if action == "play" and not after["paused"] and after["time"] > before["time"] and after["ready"] >= 2:
            return "เล่นเพลงต่อแล้วครับ"
        if action == "next" and after["src"] != before["src"]:
            return "เปลี่ยนเพลงแล้วครับ " + after["title"]
    raise ToolError("ส่งคำสั่งแล้ว แต่ยังยืนยันผลไม่ได้ครับ ตรวจสอบเครื่องเล่น YouTube Music ใน Chrome")
