"""Conservative Thai command parsing. Ambiguous/multiple intents stay with the LLM."""
from __future__ import annotations
import re
from datetime import datetime
from urllib.parse import quote

THAI = re.compile(r"[\u0e00-\u0e7f]")
APPS = {"chrome": "Google Chrome", "google chrome": "Google Chrome", "โครม": "Google Chrome", "กูเกิลโครม": "Google Chrome", "กูเกิล โครม": "Google Chrome", "vs code": "Visual Studio Code", "vscode": "Visual Studio Code", "วีเอสโค้ด": "Visual Studio Code", "ซาฟารี": "Safari", "safari": "Safari", "เครื่องคิดเลข": "Calculator"}
YTM = r"(?:youtube\s*music|ยูทูบ\s*มิวสิ[กค])"

def normalise(text: str) -> str:
    text = text.translate(str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789"))
    text = re.sub(r"^(?:เฮ้\s*)?(?:จาร์วิส|จาวิส)\s*", "", text)
    text = re.sub(r"^(?:แล้ว\s*)?(?:ช่วย\s*)?", "", text)
    text = re.sub(r"(?:ให้หน่อย|ให้ด้วย|หน่อย|ด้วย)?\s*(?:นะครับ|นะคะ|ครับ|ค่ะ|คะ)?$", "", text).strip()
    return text

def parse(text: str, last_app: str = "", media: str = "youtube_music") -> list[tuple[str, dict]] | None:
    t = normalise(text)
    # Explicit target interaction; no generic splitting of Thai names/titles.
    if re.fullmatch(r"เปิด\s*(?:google\s*)?chrome\s*แล้ว(?:เข้า|เปิด)\s*" + YTM, t):
        return [("open_app", {"name": "Google Chrome"}), ("youtube_music", {"action": "open"})]
    if "แล้ว" in t or "และ" in t:
        return None
    if re.fullmatch(r"เปิด\s*" + YTM, t):
        return [("youtube_music", {"action": "open"})]
    if t in {"เปิดเพลย์ลิสต์เพลงที่ฉันชอบ", "เปิดเพลงที่ชอบ"}:
        return [("youtube_music", {"action": "liked"})]
    music = {"หยุดเพลง": "pause", "พักเพลง": "pause", "เล่นเพลงต่อ": "play", "เล่นต่อ": "play", "ข้ามเพลงนี้": "next", "ข้ามเพลง": "next", "เพลงถัดไป": "next"}
    if t in music:
        return [("youtube_music" if media == "youtube_music" else "media_control", {"action": music[t]})]
    m = re.fullmatch(r"เล่นเพลง\s*(.+)", t)
    if m:
        return [("youtube_music", {"action": "search", "query": m[1]})]
    m = re.fullmatch(r"(?:ปรับ|ตั้ง|ลด|รส|เพิ่ม)(?:ระดับ)?เสียง(?:ลง|ขึ้น)?\s*(?:ให้)?(?:เหลือ|เป็น|ที่)?\s*(\d{1,3})\s*(?:เปอร์เซ็นต์|%)?", t)
    if m and 0 <= int(m[1]) <= 100:
        return [("system_control", {"action": "volume_set", "value": int(m[1])})]
    volume = {"ลดเสียง": "volume_down", "ลดเสียงลง": "volume_down", "เพิ่มเสียง": "volume_up", "เพิ่มเสียงขึ้น": "volume_up", "ปิดเสียง": "mute", "เปิดเสียง": "unmute"}
    if t in volume:
        return [("system_control", {"action": volume[t]})]
    if t in {"ปิดมัน", "ปิดแอปนี้", "ปิดแอปนั้น"} and last_app:
        return [("quit_app", {"name": last_app})]
    m = re.fullmatch(r"(?:เปิด|เข้า)\s*(?:เว็บไซต์\s*)?((?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)", t)
    if m:
        return [("open_url", {"url": m[1]})]
    m = re.fullmatch(r"(?:เปิด|ปิด|ออกจาก)\s*(?:แอป\s*)?(.+)", t)
    if m and m[1] in APPS:
        return [("open_app" if t.startswith("เปิด") else "quit_app", {"name": APPS[m[1]]})]
    m = re.fullmatch(r"(?:ค้นหา|หา|เสิร์ช)\s*(.+)", t)
    if m:
        return [("open_url", {"url": "https://www.google.com/search?q=" + quote(m[1])})]
    m = re.fullmatch(r"(?:ตั้งเวลา|ตั้งนาฬิกาจับเวลา|จับเวลา)\s*(\d+(?:\.\d+)?)\s*(วินาที|นาที|ชั่วโมง)", t)
    if m:
        unit = {"วินาที": "seconds", "นาที": "minutes", "ชั่วโมง": "hours"}[m[2]]
        return [("set_timer", {"duration": f"{m[1]} {unit}"})]
    if t in {"พรุ่งนี้ฉันมีอะไรต้องทำบ้าง", "พรุ่งนี้มีอะไรในปฏิทิน", "ดูปฏิทินพรุ่งนี้"}:
        return [("calendar", {"action": "list", "day": "tomorrow"})]
    return None

def clock_reply(text: str) -> str | None:
    t = normalise(text)
    now = datetime.now()
    if t in {"ตอนนี้กี่โมง", "ตอนนี้กี่โมงแล้ว", "กี่โมงแล้ว", "เวลาเท่าไหร่"}:
        return f"ตอนนี้ {now.hour} นาฬิกา {now.minute} นาทีครับ"
    if t in {"วันนี้วันที่เท่าไหร่", "วันนี้วันอะไร"}:
        days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
        return f"วันนี้วัน{days[now.weekday()]} วันที่ {now.day} เดือน {now.month} ปี {now.year + 543} ครับ"
    return None

def localise_result(name: str, args: dict, result: str) -> str:
    """Translate only known factual result forms; never turn a failure into success."""
    if result.startswith("Error:"):
        return "ทำรายการไม่สำเร็จครับ: " + result.removeprefix("Error: ")
    if result.startswith("Cancelled by the user"):
        return "ยกเลิกแล้วครับ"
    if THAI.search(result):
        return result
    m = re.fullmatch(r"Volume (\d+) percent\.", result)
    if m:
        return f"ปรับเสียงเป็น {m[1]} เปอร์เซ็นต์แล้วครับ"
    if result in {"Muted.", "Unmuted."}:
        return "ปิดเสียงแล้วครับ" if result == "Muted." else "เปิดเสียงแล้วครับ"
    m = re.fullmatch(r"(.+) is (already )?open\.", result)
    if name == "open_app" and m:
        return f"เปิด {m[1]} ให้แล้วครับ"
    m = re.fullmatch(r"(.+) is closed\.", result)
    if name == "quit_app" and m:
        return f"ปิด {m[1]} แล้วครับ"
    if name == "open_url" and result.startswith("Opened "):
        return "เปิดหน้าเว็บให้แล้วครับ"
    if name == "set_timer" and result.startswith("Timer set for "):
        return "ตั้งเวลาให้แล้วครับ " + args.get("duration", "")
    return result  # preserve partial outcomes and permission diagnostics verbatim
