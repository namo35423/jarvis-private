"""Conservative Thai command parsing. Ambiguous/multiple intents stay with the LLM.

Everyday commands are matched here and run without the model: the model is slow
for these, and on its own it sometimes answers from memory ("28 องศา", "4,500
บาท") instead of calling the tool. Questions whose answer is data (weather,
calendar, files) are returned as plans the core runs first and only then asks the
model to word - see WORDED.
"""
from __future__ import annotations
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from jarvis import thai_time

THAI = re.compile(r"[฀-๿]")
APPS = {"chrome": "Google Chrome", "google chrome": "Google Chrome", "โครม": "Google Chrome", "กูเกิลโครม": "Google Chrome",
        "กูเกิล โครม": "Google Chrome", "vs code": "Visual Studio Code", "vscode": "Visual Studio Code",
        "วีเอสโค้ด": "Visual Studio Code", "ซาฟารี": "Safari", "safari": "Safari", "เครื่องคิดเลข": "Calculator",
        "ไลน์": "LINE", "line": "LINE", "ปฏิทิน": "Calendar", "โน้ต": "Notes", "โน๊ต": "Notes", "บันทึก": "Notes",
        "ไฟน์เดอร์": "Finder", "สปอติฟาย": "Spotify", "ตั้งค่า": "System Settings", "การตั้งค่า": "System Settings",
        "ตั้งค่าระบบ": "System Settings", "รูปภาพ": "Photos", "รูป": "Photos", "อีเมล": "Mail", "เมล": "Mail",
        "แผนที่": "Maps", "ข้อความ": "Messages", "เตือนความจำ": "Reminders", "เทอร์มินัล": "Terminal",
        "แอปสโตร์": "App Store", "เวิร์ด": "Microsoft Word", "เอ็กเซล": "Microsoft Excel",
        "พาวเวอร์พอยต์": "Microsoft PowerPoint", "ซูม": "zoom.us", "zoom": "zoom.us", "ดิสคอร์ด": "Discord",
        "เมสเซนเจอร์": "Messenger", "ทีมส์": "Microsoft Teams", "เฟซไทม์": "FaceTime", "นาฬิกา": "Clock",
        "พรีวิว": "Preview", "เพลงแอปเปิล": "Music", "แอปเพลง": "Music", "โฟลเดอร์ดาวน์โหลด": "Finder"}
SITES = {"ยูทูป": "youtube.com", "ยูทูบ": "youtube.com", "youtube": "youtube.com", "เฟซบุ๊ก": "facebook.com",
         "เฟสบุ๊ค": "facebook.com", "เฟสบุ๊ก": "facebook.com", "เฟซบุ๊ค": "facebook.com", "facebook": "facebook.com",
         "กูเกิล": "google.com", "กูเกิ้ล": "google.com", "google": "google.com", "จีเมล": "mail.google.com",
         "gmail": "mail.google.com", "ไอจี": "instagram.com", "อินสตาแกรม": "instagram.com", "instagram": "instagram.com",
         "ทวิตเตอร์": "x.com", "twitter": "x.com", "ติ๊กต็อก": "tiktok.com", "tiktok": "tiktok.com",
         "ชอปปี้": "shopee.co.th", "ช้อปปี้": "shopee.co.th", "shopee": "shopee.co.th", "ลาซาด้า": "lazada.co.th",
         "lazada": "lazada.co.th", "เน็ตฟลิกซ์": "netflix.com", "netflix": "netflix.com", "แชทจีพีที": "chatgpt.com",
         "chatgpt": "chatgpt.com", "พันทิป": "pantip.com", "pantip": "pantip.com", "วิกิพีเดีย": "th.wikipedia.org",
         "กูเกิลแมพ": "maps.google.com", "google maps": "maps.google.com", "กูเกิลไดรฟ์": "drive.google.com",
         "google drive": "drive.google.com", "github": "github.com", "กิตฮับ": "github.com"}
YTM = r"(?:youtube\s*music|ยูทูบ\s*มิวสิ[กค]|ยูทูป\s*มิวสิ[กค])"
# "เปิดเพลง X", "เล่นเพลง X ของ Y", "เปิดเพลง X จาก YouTube Music". Whisper often hears เพลง as เพียง.
SONG = re.compile(r"(?:(?:เปิด|เล่น)\s*(?:เล่น\s*)?(?:เพลง|เพียง)|(?:อยาก|ขอ)\s*ฟัง\s*(?:เพลง)?|ฟังเพลง)\s*(?P<title>.+?)"
                  r"(?:\s+(?:ของ|โดย)\s*(?P<artist>.+?))?"
                  r"(?:\s*(?:ฟัง)?\s*(?:จาก|ใน|บน|ที่)\s*" + YTM + r")?(?:\s*ฟัง)?")
NOT_A_SONG = {"ต่อ", "ต่อเลย", "หน่อย", "ที่ชอบ", "ที่ฉันชอบ", "", "เลย", "ต่อหน่อย"}
NOW_PLAYING = re.compile(r"(?:ตอนนี้)?(?:เพลง(?:ที่)?(?:กำลัง)?(?:เล่น|เปิด)อยู่|เพลงนี้)(?:ชื่อ)?(?:เพลง)?(?:อะไร|ไร)"
                         r"|(?:ตอนนี้)?(?:กำลัง)?(?:เล่น|เปิด|ฟัง)เพลงอะไร(?:อยู่)?|เพลงอะไร(?:อยู่)?|นี่เพลงอะไร")
# "ส่งข้อความหา Somchai ทางไลน์ว่า สวัสดี", "ส่งไลน์หา Somchai ว่า สวัสดี", "ไลน์หา Somchai ว่า ..."
LINE_APP = r"(?:line|ไลน์|ไลน)"
LINE_MSG = re.compile(
    r"(?:ส่ง\s*(?:ข้อความ|แชท|แชต)?\s*(?:" + LINE_APP + r")?|" + LINE_APP + r")\s*"
    r"(?:ไป\s*)?(?:ถึง|หา|ให้|ไปหา|ไปถึง|ไปให้)\s*(?:คุณ\s*)?(?P<who>.+?)\s*"
    r"(?:(?:ผ่าน|ทาง|ใน|ด้วย)\s*(?:แอป\s*)?" + LINE_APP + r"\s*)?"
    r"(?:ว่า|บอกว่า|ข้อความว่า)\s*(?P<text>.+)", re.I | re.S)
CURRENCY = {"ดอลลาร์สิงคโปร์": "SGD", "ดอลลาร์ฮ่องกง": "HKD", "ดอลลาร์ออสเตรเลีย": "AUD", "ดอลลาร์ไต้หวัน": "TWD",
            "ดอลลาร์สหรัฐ": "USD", "ดอลลาร์": "USD", "ดอลล่าร์": "USD", "เหรียญสหรัฐ": "USD", "ยูเอสดี": "USD",
            "usd": "USD", "ยูโร": "EUR", "eur": "EUR", "เยน": "JPY", "jpy": "JPY", "หยวน": "CNY", "cny": "CNY",
            "เงินวอน": "KRW", "krw": "KRW", "ปอนด์": "GBP", "gbp": "GBP", "ริงกิต": "MYR", "รูปี": "INR",
            "เงินดอง": "VND", "เงินกีบ": "LAK", "บาท": "THB", "thb": "THB"}
_CUR = "(" + "|".join(sorted(map(re.escape, CURRENCY), key=len, reverse=True)) + ")"
WEATHER = re.compile(r"อากาศ|ฝน(?:จะ)?ตก|ฝนตก|ร้อนไหม|ร้อนมั้ย|ร้อนป่าว|หนาวไหม|หนาวมั้ย|อุณหภูมิ|กี่องศา|พยากรณ์|"
                     r"ฟ้าครึ้ม|แดดออก|มีพายุ|ร่มไหม|ต้องพกร่ม|เอาร่มไป")

# Plans whose tool output is data to be worded by the model (in Thai), not spoken raw.
WORDED = {"find_files", "daily_briefing", "web_search", "chrome_read_page", "screen_look_describe"}


def needs_wording(plan: list[tuple[str, dict]]) -> bool:
    return any(name in WORDED or (name == "screen_look" and args.get("mode") != "save") or (name in {"calendar", "reminders"} and
                                  str(args.get("action", "")).startswith("list")) for name, args in plan)


def normalise(text: str) -> str:
    text = text.translate(str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789"))
    text = re.sub(r"^(?:เฮ้\s*)?(?:จาร์วิส|จาวิส)\s*", "", text)
    text = re.sub(r"^(?:แล้ว\s*)?(?:ช่วย\s*)?(?:ขอ(?=(?:ดู|เช็ค|เช็ก)))?", "", text)
    text = re.sub(r"\s+(?:ดิ|สิ|ซิ|เว้ย|เด้|วะ|เลย|อ่ะ|อะ|หน่อยดิ)$", "", text.strip())  # spoken particles, only as a separate word
    text = re.sub(r"(?:ให้หน่อย|ให้ด้วย|หน่อย|ด้วย)?\s*(?:นะครับ|นะคะ|ครับ|ค่ะ|คะ|จ้า|จ้ะ)?$", "", text).strip()
    return text


@lru_cache(maxsize=1)
def _installed_apps() -> dict[str, str]:
    """casefolded name -> app name, for every .app in the usual places."""
    found: dict[str, str] = {}
    for folder in ("/Applications", "/Applications/Utilities", "/System/Applications",
                   "/System/Applications/Utilities", str(Path.home() / "Applications"), "/System/Library/CoreServices"):
        try:
            for p in Path(folder).iterdir():
                if p.suffix == ".app":
                    found.setdefault(p.stem.casefold(), p.stem)
        except OSError:
            continue
    return found


def app_name(spoken: str) -> str | None:
    key = " ".join(spoken.strip().split()).casefold()
    key = re.sub(r"^แอป(?:พลิเคชัน)?\s*", "", key)
    if key in APPS:
        return APPS[key]
    apps = _installed_apps()
    return apps.get(key) or apps.get(key.replace(" ", ""))


def _duration(text: str) -> str | None:
    """Thai length of time -> what set_timer accepts, or None."""
    from jarvis.tools.timers import parse_duration
    from jarvis.sysutil import ToolError

    t = re.sub(r"(\S+?)\s*ชั่วโมงครึ่ง", r"\1 ชั่วโมง 30 นาที", text)
    t = re.sub(r"^ชั่วโมงครึ่ง", "1 ชั่วโมง 30 นาที", t)
    t = re.sub(r"(\S+?)\s*นาทีครึ่ง", r"\1 นาที 30 วินาที", t)
    try:
        seconds = parse_duration(t)
    except (ToolError, ValueError):
        return None
    if seconds < 1:
        return None
    seconds = int(seconds)
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hours"
    return f"{seconds // 60} minutes" if seconds % 60 == 0 else f"{seconds} seconds"


def _thai_when(when: datetime) -> str:
    today = datetime.now().date()
    days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
    day = ("วันนี้" if when.date() == today else "พรุ่งนี้" if (when.date() - today).days == 1
           else f"วัน{days[when.weekday()]}ที่ {when.day}")
    return f"{day} เวลา {when:%H:%M}"


def _clean_task(text: str) -> str:
    t = re.sub(r"^(?:ช่วย)?(?:ตั้ง)?(?:แจ้ง)?เตือน(?:ความจำ)?\s*(?:ฉัน|ผม|หนู|เรา|ด้วย)?\s*(?:ว่า|ให้)?", "", text.strip())
    t = re.sub(r"(?:^|\s)(?:ตอน|เวลา|ช่วง|ใน|วัน)\s*$", "", t)
    t = re.sub(r"^(?:ตอน|เวลา)\s*", "", t)
    t = re.sub(r"\s*(?:ตอน|เวลา)\s*(?=$)", "", t)
    t = re.sub(r"(?:เตือน(?:ฉัน|ผม)?(?:ให้|ว่า)?)", " ", t)
    return " ".join(t.split()).strip(" ,")


def _weather(t: str) -> list[tuple[str, dict]] | None:
    if not WEATHER.search(t) or re.search(r"^(?:เปิด|ปิด|ตั้ง|ปรับ)(?!.*(?:ไหม|มั้ย|ยังไง|เป็นไง))", t):
        return None
    when = "now"
    if "พรุ่งนี้" in t:
        when = "tomorrow"
    elif re.search(r"สัปดาห์นี้|อาทิตย์นี้|ทั้งอาทิตย์|ทั้งสัปดาห์|เสาร์อาทิตย์|วันหยุด|หลายวัน", t):
        when = "week"
    elif re.search(r"วันนี้|ทั้งวัน|เย็นนี้|คืนนี้|บ่ายนี้", t):
        when = "today"
    args: dict = {"when": when}
    m = re.search(r"(?:ที่|แถว|จังหวัด|เมือง)\s*(?P<loc>[ก-๙A-Za-z][ก-๙A-Za-z .]*?)\s*"
                  r"(?=วันนี้|พรุ่งนี้|ตอนนี้|สัปดาห์|อาทิตย์|เป็น|ยังไง|ไหม|มั้ย|กี่|ร้อน|หนาว|ฝน|อากาศ|จะ|มี|$)", t)
    if m and m["loc"].strip() not in {"นี่", "นี้", "นั่น", "บ้าน"}:
        args["location"] = m["loc"].strip()
    return [("get_weather", args)]


def _currency(t: str) -> list[tuple[str, dict]] | None:
    m = re.search(r"(?P<amt>\d[\d,]*(?:\.\d+)?)?\s*(?:เงิน)?" + _CUR + r"\s*(?:เป็น|ได้|เท่ากับ|แลก(?:ได้)?|คิดเป็น)?\s*"
                  r"(?:เงิน)?(?:กี่|เท่าไหร่|เท่าไร)?\s*" + _CUR + r"?", t, re.I)
    if not m or not re.search(r"กี่|เท่าไหร่|เท่าไร|อัตรา|ค่าเงิน|แลก", t) or re.search(r"ราคา|ค่า(?!เงิน)", t):
        return None  # "ราคาทองกี่บาท" is a price question, not a conversion
    src, dst = CURRENCY[m[2].casefold()], CURRENCY.get((m[3] or "บาท").casefold(), "THB")
    if src == dst:
        return None
    return [("convert_currency", {"from_currency": src, "to_currency": dst,
                                  "amount": float((m["amt"] or "1").replace(",", ""))})]


def parse(text: str, last_app: str = "", media: str = "youtube_music") -> list[tuple[str, dict]] | None:
    t = normalise(text)
    player = "youtube_music" if media == "youtube_music" else "media_control"
    # Explicit target interaction; no generic splitting of Thai names/titles.
    if re.fullmatch(r"เปิด\s*(?:google\s*)?chrome\s*แล้ว(?:เข้า|เปิด)\s*" + YTM, t):
        return [("open_app", {"name": "Google Chrome"}), ("youtube_music", {"action": "open"})]
    if re.search(r"แล้ว(?!ไหม|มั้ย|ยัง|หรือยัง|$)|และ", t) and not re.search(r"(?:วันนี้|พรุ่งนี้)มี", t):
        return None
    if re.fullmatch(r"เปิด\s*" + YTM, t):
        return [("youtube_music", {"action": "open"})]
    if t in {"เปิดเพลย์ลิสต์เพลงที่ฉันชอบ", "เปิดเพลงที่ชอบ", "เปิดเพลงที่ฉันชอบ", "เปิดเพลย์ลิสต์ที่ชอบ"}:
        return [("youtube_music", {"action": "liked"})]
    music = {"หยุดเพลง": "pause", "พักเพลง": "pause", "ปิดเพลง": "pause", "หยุดเพลงก่อน": "pause", "หยุดก่อน": "pause",
             "เล่นเพลงต่อ": "play", "เล่นต่อ": "play", "เปิดเพลงต่อ": "play", "เปิดเพลง": "play", "เล่นเพลง": "play",
             "ข้ามเพลงนี้": "next", "ข้ามเพลง": "next", "เพลงถัดไป": "next", "เปลี่ยนเพลง": "next", "ข้าม": "next",
             "เพลงก่อนหน้า": "previous", "ย้อนเพลง": "previous", "เพลงที่แล้ว": "previous", "ย้อนกลับเพลงก่อน": "previous"}
    if t in music:
        return [(player, {"action": music[t]})]
    if NOW_PLAYING.fullmatch(t):
        return [(player, {"action": "now_playing"})]
    m = re.fullmatch(r"(?:ค้นหา|หา|เสิร์ช)\s*เพลง\s*(.+)", t)
    if m:
        return [("youtube_music", {"action": "search", "query": m[1].strip()})]
    m = SONG.fullmatch(t)
    if m and m["title"].strip() not in NOT_A_SONG:
        title, artist = m["title"].strip(), (m["artist"] or "").strip()
        by = re.fullmatch(r"(?:ของ|โดย)\s*(.+)", title)
        if by and not artist:  # "เปิดเพลงของ Ed Sheeran": an artist, no title
            return [("youtube_music", {"action": "play", "artist": by[1].strip()})]
        args = {"action": "play", "query": title}
        if artist:
            args["artist"] = artist
        return [("youtube_music", args)]

    # ---- sound and the Mac itself
    m = re.fullmatch(r"(?:ปรับ|ตั้ง|ลด|รส|เพิ่ม|เร่ง)(?:ระดับ)?เสียง(?:ลง|ขึ้น)?\s*(?:ให้)?(?:เหลือ|เป็น|ที่)?\s*(\d{1,3})\s*(?:เปอร์เซ็นต์|%)?", t)
    if m and 0 <= int(m[1]) <= 100:
        return [("system_control", {"action": "volume_set", "value": int(m[1])})]
    volume = {"ลดเสียง": "volume_down", "ลดเสียงลง": "volume_down", "เบาเสียง": "volume_down", "เบาเสียงลง": "volume_down",
              "เสียงเบาลง": "volume_down", "ลดเสียงลงอีก": "volume_down", "เสียงดังไป": "volume_down",
              "เพิ่มเสียง": "volume_up", "เพิ่มเสียงขึ้น": "volume_up", "เร่งเสียง": "volume_up", "เสียงดังขึ้น": "volume_up",
              "เพิ่มเสียงอีก": "volume_up", "เปิดเสียงดังขึ้น": "volume_up", "เสียงเบาไป": "volume_up",
              "ปิดเสียง": "mute", "ปิดเสียงเครื่อง": "mute", "เปิดเสียง": "unmute", "เปิดเสียงเครื่อง": "unmute"}
    if t in volume:
        return [("system_control", {"action": volume[t]})]
    if re.fullmatch(r"(?:ตอนนี้)?(?:เสียง(?:ดัง)?|ระดับเสียง)(?:อยู่)?(?:ที่)?(?:เท่าไหร่|เท่าไร|กี่เปอร์เซ็นต์)", t):
        return [("system_control", {"action": "volume_status"})]
    if re.search(r"แบต", t) and re.search(r"เหลือ|เท่าไหร่|เท่าไร|กี่|เช็ค|เช็ก|ดู|หมด|พอ", t):
        return [("system_control", {"action": "battery"})]
    if re.search(r"พื้นที่|ความจุ|ดิสก์|disk|สตอเรจ|storage|ที่ว่าง", t, re.I) and re.search(r"เหลือ|ว่าง|เท่าไหร่|เท่าไร|กี่|เต็ม", t):
        return [("system_control", {"action": "disk_space"})]
    if re.fullmatch(r"(?:เปิด|เปลี่ยนเป็น|ใช้)?\s*(?:โหมดมืด|ดาร์กโหมด|dark\s*mode|โหมดกลางคืน)", t, re.I):
        return [("system_control", {"action": "dark_mode"})]
    if re.fullmatch(r"(?:เปิด|เปลี่ยนเป็น|ใช้)?\s*(?:โหมดสว่าง|ไลท์โหมด|light\s*mode)|ปิดโหมดมืด|ปิดดาร์กโหมด", t, re.I):
        return [("system_control", {"action": "light_mode"})]
    if re.fullmatch(r"(?:เพิ่มความสว่าง(?:หน้าจอ|จอ)?|(?:หน้า)?จอสว่างขึ้น|ปรับ(?:หน้า)?จอให้สว่างขึ้น|เร่งแสงจอ)", t):
        return [("system_control", {"action": "brightness_up"})]
    if re.fullmatch(r"(?:ลดความสว่าง(?:หน้าจอ|จอ)?|(?:หน้า)?จอมืดลง|หรี่(?:หน้า)?จอ|ลดแสงจอ)", t):
        return [("system_control", {"action": "brightness_down"})]
    if re.fullmatch(r"(?:ล็อก|ล็อค|lock)\s*(?:หน้าจอ|จอ|เครื่อง|คอม)", t, re.I):
        return [("system_control", {"action": "lock_screen"})]
    if re.fullmatch(r"(?:เช็ค|เช็ก|ดู)?\s*(?:ไวไฟ|wifi|wi-fi|เน็ต)\s*(?:หน่อย|ต่ออยู่ไหม|ติดไหม|ใช้ได้ไหม|ต่อ(?:อยู่)?กับอะไร)?", t, re.I) \
            and not t.startswith(("เปิด", "ปิด")):
        return [("system_control", {"action": "wifi_status"})]
    if re.fullmatch(r"(?:เปิด|ปิด)\s*(?:ไวไฟ|wifi|wi-fi)", t, re.I):
        return [("system_control", {"action": "wifi_on" if t.startswith("เปิด") else "wifi_off"})]

    # ---- apps, websites, tabs, files
    if t in {"ปิดมัน", "ปิดแอปนี้", "ปิดแอปนั้น"} and last_app:
        return [("quit_app", {"name": last_app})]
    m = re.fullmatch(r"(?:เปิด|เข้า)\s*(?:เว็บไซต์\s*|เว็บ\s*)?((?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)", t)
    if m:
        return [("open_url", {"url": m[1]})]
    m = re.fullmatch(r"(?:เปิด|เข้า)\s*(?:เว็บ(?:ไซต์)?\s*)?(.+?)(?:\s*(?:ให้ดู|หน่อย))?", t)
    if m and m[1].strip().casefold() in SITES:
        return [("open_url", {"url": SITES[m[1].strip().casefold()]})]
    if re.fullmatch(r"เปิด(?:โฟลเดอร์)?\s*(?:ดาวน์โหลด|downloads?)", t, re.I):
        return [("file_action", {"action": "open", "path": "~/Downloads"})]
    if re.fullmatch(r"เปิด(?:โฟลเดอร์)?\s*(?:เดสก์ท็อป|เดสก์ทอป|desktop)", t, re.I):
        return [("file_action", {"action": "open", "path": "~/Desktop"})]
    if re.fullmatch(r"เปิด(?:โฟลเดอร์)?\s*(?:เอกสาร|documents?)", t, re.I):
        return [("file_action", {"action": "open", "path": "~/Documents"})]
    m = re.fullmatch(r"(?:เปิด|ปิด|ออกจาก)\s*(?:แอป(?:พลิเคชัน)?\s*)?(.+)", t)
    if m:
        name = app_name(m[1])
        if name:
            return [("open_app" if t.startswith("เปิด") else "quit_app", {"name": name})]
    if re.fullmatch(r"ปิดแท็บ(?:นี้)?|ปิดหน้า(?:เว็บ)?นี้", t):
        return [("chrome_tabs", {"action": "close"})]
    if re.fullmatch(r"เปิดแท็บใหม่|แท็บใหม่", t):
        return [("chrome_tabs", {"action": "new"})]
    if re.fullmatch(r"(?:แคป|แคปเจอร์|ถ่ายภาพ|ถ่ายรูป)\s*(?:หน้า)?จอ(?:ไว้)?|สกรีนช็อต|สกรีนชอต|screenshot", t, re.I):
        return [("screen_look", {"mode": "save"})]
    m = re.fullmatch(r"(?:ค้นหา|หา|ช่วยหา)\s*(?P<kind>ไฟล์|เอกสาร|รูป(?:ภาพ)?|โฟลเดอร์)\s*(?:ชื่อ|ที่ชื่อ|เกี่ยวกับ)?\s*(?P<q>.+)"
                     r"|(?P<kind2>ไฟล์|เอกสาร)\s*(?P<q2>.+?)\s*อยู่(?:ที่)?ไหน", t)
    if m:
        q, kind_word = (m["q"] or m["q2"]).strip(), m["kind"] or m["kind2"]
        kind = "any"
        if re.search(r"\bpdf\b|พีดีเอฟ", q, re.I):
            kind, q = "pdf", re.sub(r"\s*(?:\bpdf\b|พีดีเอฟ)\s*", " ", q, flags=re.I).strip()
        elif kind_word.startswith("รูป"):
            kind = "image"
        elif kind_word == "โฟลเดอร์":
            kind = "folder"
        return [("find_files", {"query": q or kind, "kind": kind})]
    m = re.fullmatch(r"(?:ค้นหา|หา|เสิร์ช|กูเกิล)\s*(.+)", t)
    if m:
        return [("open_url", {"url": "https://www.google.com/search?q=" + quote(m[1])})]

    # ---- time: timers, alarms, reminders, calendar
    if re.fullmatch(r"(?:ตั้งเวลา|เหลือเวลา|นับถอยหลัง|จับเวลา|ปลุก)(?:เหลือ)?(?:อีก)?(?:เท่าไหร่|เท่าไร|กี่นาที)|ตั้งเวลาอะไรไว้บ้าง|มีตั้งเวลาไหม", t):
        return [("timers", {"action": "list"})]
    if re.fullmatch(r"ยกเลิก(?:การ)?(?:ตั้งเวลา|จับเวลา|นาฬิกาปลุก|ปลุก|ไทม์เมอร์)(?:ทั้งหมด)?", t):
        return [("timers", {"action": "cancel_all"})]
    m = re.fullmatch(r"(?:ตั้งเวลา|ตั้งนาฬิกาจับเวลา|จับเวลา|ตั้งไทม์เมอร์|นับถอยหลัง)\s*(?P<d>.+?)\s*(?:(?:เพื่อ|สำหรับ)\s*(?P<label>.+))?", t)
    if m and not thai_time.find_time(m["d"]):
        duration = _duration(m["d"])
        if duration:
            return [("set_timer", {"duration": duration, **({"label": m["label"]} if m["label"] else {})})]
    m = re.fullmatch(r"(?:ในอีก|อีก)\s*(?P<d>.+?(?:วินาที|นาที|ชั่วโมง)(?:ครึ่ง)?)\s*(?:ช่วย)?(?:เตือน|บอก|ปลุก|เรียก)\s*"
                     r"(?:ฉัน|ผม|หนู)?\s*(?:ให้|ว่า)?\s*(?P<label>.*)", t)
    if m:
        duration = _duration(m["d"])
        if duration:
            return [("set_timer", {"duration": duration, **({"label": m["label"].strip()} if m["label"].strip() else {})})]
    if re.search(r"ปลุก", t) and not re.search(r"ยกเลิก|เหลือ", t):
        when, _, had_time = thai_time.resolve(t, alarm=True)
        if when and had_time:
            seconds = int((when - datetime.now()).total_seconds())
            if seconds > 0:
                return [("set_timer", {"duration": f"{seconds} seconds", "label": f"ปลุก{_thai_when(when)}"})]
    if re.search(r"เตือน", t) and not re.search(r"ยกเลิก|มีอะไร|อะไรบ้าง|ดู|เช็ค|รายการ", t):
        when, rest, _ = thai_time.resolve(t)
        task = _clean_task(rest)
        if task:
            args = {"action": "create", "text": task}
            if when:
                args["when"] = f"{when:%Y-%m-%d %H:%M}"
            return [("reminders", args)]
    if re.fullmatch(r"(?:วันนี้)?(?:มี)?อะไร(?:ที่)?ต้องทำ(?:บ้าง|ไหม|มั้ย)?(?:วันนี้)?|(?:วันนี้)?มีงานอะไรบ้าง", t):
        return [("calendar", {"action": "list", "day": "today"}), ("reminders", {"action": "list_today"})]
    m = re.fullmatch(r"(?P<day>วันนี้|พรุ่งนี้|มะรืน(?:นี้)?|วัน\S+?)?\s*(?:ฉัน|ผม)?(?:มี)?\s*(?:นัด|ประชุม|ตาราง(?:งาน)?|กิจกรรม|อีเวนต์|อะไรใน(?:ปฏิทิน|ตาราง))"
                     r"(?:อะไร)?(?:บ้าง|ไหม|มั้ย|หรือเปล่า|ป่าว)?(?:\s*(?P<day2>วันนี้|พรุ่งนี้))?|ดูปฏิทิน\s*(?P<day3>วันนี้|พรุ่งนี้)?", t)
    if m:
        word = m["day"] or m["day2"] or m["day3"] or "วันนี้"
        day = {"วันนี้": "today", "พรุ่งนี้": "tomorrow"}.get(word)
        if day is None:
            found = thai_time.find_day(word)
            day = f"{found[0]:%Y-%m-%d}" if found else "today"
        return [("calendar", {"action": "list", "day": day})]
    if t in {"พรุ่งนี้ฉันมีอะไรต้องทำบ้าง", "พรุ่งนี้มีอะไรต้องทำบ้าง", "พรุ่งนี้มีอะไรในปฏิทิน", "ดูปฏิทินพรุ่งนี้"}:
        return [("calendar", {"action": "list", "day": "tomorrow"})]
    m = re.fullmatch(r"(?:ลง|เพิ่ม|ใส่|จด)?\s*(?:นัด|ประชุม)(?:หมาย)?\s*(?P<rest>.+)", t)
    if m:
        when, rest, had_time = thai_time.resolve(m["rest"])
        title = re.sub(r"(?:^|\s)(?:ตอน|เวลา|วัน|ใน|ลงปฏิทิน|ในปฏิทิน)(?=\s|$)", " ", rest)
        title = " ".join(title.split())
        if when and had_time and title:
            return [("calendar", {"action": "create", "title": "นัด" + title if not title.startswith(("นัด", "ประชุม")) else title,
                                  "start": f"{when:%Y-%m-%d %H:%M}"})]

    # ---- questions whose answer is live data
    plan = _weather(t) or _currency(t)
    if plan:
        return plan
    m = re.fullmatch(r"(?:มี)?(?:ข่าว|อัปเดตข่าว)(?:อะไร)?(?:วันนี้|ล่าสุด|เด่น|ตอนนี้)?(?:มี)?(?:อะไร)?(?:บ้าง)?(?:วันนี้)?"
                     r"|(?:ขอ)?(?:ฟัง)?ข่าว(?:เกี่ยวกับ|เรื่อง)\s*(?P<topic>.+?)(?:\s*(?:หน่อย|ล่าสุด|วันนี้))?", t)
    if m:
        return [("news_headlines", {"topic": m["topic"].strip()} if m["topic"] else {})]
    if re.search(r"ราคา\S*.*(?:วันนี้|เท่าไหร่|เท่าไร|กี่บาท|ตอนนี้|ล่าสุด)|"
                 r"ผล(?:บอล|ฟุตบอล|การแข่ง|หวย|สลาก)|หวยออก|ใครชนะ", t):
        return [("web_search", {"query": t})]
    if re.fullmatch(r"(?:สรุป|อ่าน|ย่อ)\s*(?:หน้า)?(?:เว็บ|เพจ|บทความ|แท็บ)(?:นี้|ที่เปิดอยู่)?(?:ให้ฟัง)?|(?:หน้า)?เว็บนี้(?:เขียน|พูด)?(?:ว่า)?(?:อะไร|เกี่ยวกับอะไร)", t):
        return [("chrome_read_page", {"mode": "text"})]
    if re.search(r"(?:บน|ใน)?(?:หน้า)?จอ(?:มี|เขียน|ขึ้น|แสดง)(?:ว่า)?(?:อะไร|อะไรอยู่)|ดูหน้าจอ(?:ให้)?|(?:หน้า)?จอ(?:นี้)?(?:คือ)?อะไร|error\s*(?:นี้)?คืออะไร", t, re.I):
        return [("screen_look", {"mode": "describe", "question": t})]
    if re.fullmatch(r"(?:สรุป|บรีฟ|บรีฟฟิ่ง)(?:เช้านี้|ประจำวัน|วันนี้|ตอนเช้า)|มอร์นิ่งบรีฟ(?:ฟิ่ง)?|สรุปข่าวเช้า", t):
        return [("daily_briefing", {})]

    # ---- notes and memory
    m = re.fullmatch(r"(?:จด|บันทึก)\s*(?:โน้ต|โน๊ต|บันทึก)?\s*(?:ไว้)?\s*(?:ว่า)?\s*(?!ไฟล์|หน้าจอ|เสียง|ภาพ|วิดีโอ)(?P<body>.+)", t)
    if m and len(m["body"]) > 1:
        body = m["body"].strip()
        return [("notes", {"action": "create", "title": body[:40], "body": body})]
    m = re.fullmatch(r"จำ(?:ไว้)?(?:ด้วย)?(?:นะ)?\s*ว่า\s*(?P<fact>.+)", t)
    if m:
        return [("remember", {"fact": m["fact"].strip()})]
    return None


def parse_line(text: str) -> tuple[str, str] | None:
    """(recipient, message) when LINE is named as the app; case and wording kept as spoken."""
    t = re.sub(r"^(?:(?:hey|เฮ้)\s*)?(?:jarvis|จาร์วิส|จาวิส)[\s,]*", "", (text or "").strip(), flags=re.I)
    t = re.sub(r"^(?:แล้ว\s*)?(?:ช่วย\s*)?", "", t)
    if not re.search(LINE_APP, t, re.I):
        return None  # no app named: never guess between LINE and Messages
    m = LINE_MSG.fullmatch(t)
    if not m:
        return None
    who = re.sub(r"\s*(?:ผ่าน|ทาง|ใน)\s*(?:แอป\s*)?" + LINE_APP + r"$", "", m["who"], flags=re.I).strip()
    message = re.sub(r"\s*(?:ให้หน่อย|ให้ด้วย|หน่อย)?\s*(?:นะครับ|นะคะ)?$", "", m["text"]).strip()
    if not who or not message or re.search(LINE_APP, who, re.I):
        return None
    return who, message


def clock_reply(text: str) -> str | None:
    t = normalise(text)
    now = datetime.now()
    if re.fullmatch(r"(?:ตอนนี้)?(?:กี่โมง(?:แล้ว)?|เวลาเท่าไหร่|เวลาเท่าไร|กี่ทุ่ม(?:แล้ว)?|เวลาตอนนี้)", t):
        return f"ตอนนี้ {now.hour} นาฬิกา {now.minute} นาทีครับ"
    if re.fullmatch(r"(?:วันนี้)?(?:วันที่เท่าไหร่|วันที่เท่าไร|วันอะไร|วันที่อะไร)(?:วันนี้)?|วันนี้วันที่", t):
        days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
        months = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม",
                  "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
        return f"วันนี้วัน{days[now.weekday()]} ที่ {now.day} {months[now.month - 1]} {now.year + 543} ครับ"
    return None


_OPS = {"บวก": "+", "+": "+", "ลบ": "-", "-": "-", "คูณ": "*", "x": "*", "×": "*", "*": "*", "หาร": "/", "/": "/", "÷": "/"}
_NUM = r"(-?\d[\d,]*(?:\.\d+)?)"


def instant_reply(text: str) -> str | None:
    """Greetings, thanks and plain arithmetic: answered exactly, without the model."""
    t = normalise(text)
    if re.fullmatch(r"(?:สวัสดี|หวัดดี|ดีจ้า|ดีครับ|ไง|ฮัลโหล|hello|hi)(?:\s*(?:จาร์วิส|จาวิส|jarvis))?", t, re.I):
        return "สวัสดีครับ มีอะไรให้ช่วยไหมครับ"
    if re.fullmatch(r"(?:ขอบคุณ|ขอบใจ|แต๊งกิ้ว|thank you|thanks)(?:มาก|มากๆ|นะ|จ้า)?", t, re.I):
        return "ยินดีครับ"
    m = re.fullmatch(_NUM + r"\s*(บวก|ลบ|คูณ|หาร|[+\-x×*/÷])\s*" + _NUM + r"\s*(?:ได้|เท่ากับ|=)?\s*(?:เท่าไหร่|เท่าไร|กี่)?", t)
    if m:
        a, op, b = float(m[1].replace(",", "")), _OPS[m[2]], float(m[3].replace(",", ""))
        if op == "/" and b == 0:
            return "หารด้วยศูนย์ไม่ได้ครับ"
        value = {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else 0}[op]
        shown = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.4f}".rstrip("0").rstrip(".")
        return f"{m[1]} {m[2]} {m[3]} ได้ {shown} ครับ"
    return None


_UNITS = ((r"(\d+) hours?", r"\1 ชั่วโมง"), (r"(\d+) minutes?", r"\1 นาที"), (r"(\d+) seconds?", r"\1 วินาที"))


def _thai_units(text: str) -> str:
    for pattern, repl in _UNITS:
        text = re.sub(pattern, repl, text)
    return text


def localise_result(name: str, args: dict, result: str) -> str:
    """Translate only known factual result forms; never turn a failure into success."""
    if result.startswith("Error:"):
        m = re.search(r"could not find a place called '(.+?)'", result)
        if m:
            return f"หาสถานที่ชื่อ {m[1]} ไม่เจอครับ"
        if "weather service" in result:
            return "ติดต่อบริการพยากรณ์อากาศไม่ได้ตอนนี้ครับ"
        return "ทำรายการไม่สำเร็จครับ: " + result.removeprefix("Error: ")
    if result.startswith("Cancelled by the user"):
        return "ยกเลิกแล้วครับ"
    label = str(args.get("label", ""))
    if name == "set_timer" and label.startswith("ปลุก") and "เวลา" in label and not result.startswith("Error"):
        return f"ตั้ง{label} แล้วครับ"
    if THAI.search(result) and name not in {"reminders", "calendar", "timers"}:
        return result
    m = re.fullmatch(r"Volume (\d+) percent\.", result)
    if m:
        return f"ปรับเสียงเป็น {m[1]} เปอร์เซ็นต์แล้วครับ"
    m = re.fullmatch(r"Volume is (\d+) percent( but muted)?\.", result)
    if m:
        return f"ตอนนี้เสียงอยู่ที่ {m[1]} เปอร์เซ็นต์" + (" แต่ปิดเสียงอยู่" if m[2] else "") + "ครับ"
    if result in {"Muted.", "Unmuted."}:
        return "ปิดเสียงแล้วครับ" if result == "Muted." else "เปิดเสียงแล้วครับ"
    m = re.fullmatch(r"Brightness (\d+) percent\.", result)
    if m:
        return f"ปรับความสว่างเป็น {m[1]} เปอร์เซ็นต์แล้วครับ"
    m = re.fullmatch(r"Battery (\d+) percent, (.+)\.", result)
    if m:
        rest = m[2]
        extra = ("กำลังชาร์จ" + (f" อีก {_thai_units(rest.split(', ', 1)[1].replace(' to full', ''))} จะเต็ม" if ", " in rest else "")
                 if rest.startswith("charging") else
                 "ชาร์จเต็มแล้ว" if rest.startswith("fully charged") else
                 f"ใช้ได้อีกประมาณ {_thai_units(rest.replace(' remaining', ''))}" if rest.endswith("remaining") else "")
        return f"แบตเตอรี่เหลือ {m[1]} เปอร์เซ็นต์" + (f" {extra}" if extra else "") + "ครับ"
    m = re.fullmatch(r"Disk (\d+) GB free of (\d+) GB\.", result)
    if m:
        return f"พื้นที่ว่างเหลือ {m[1]} GB จากทั้งหมด {m[2]} GB ครับ"
    m = re.fullmatch(r"Wi-Fi is on, connected to (.+)\.", result)
    if m:
        return f"Wi-Fi เปิดอยู่ เชื่อมต่อกับ {m[1]} ครับ"
    wifi = {"Wi-Fi is off.": "Wi-Fi ปิดอยู่ครับ", "Wi-Fi is on.": "เปิด Wi-Fi แล้วครับ",
            "Wi-Fi is on but not connected to a network.": "Wi-Fi เปิดอยู่ แต่ยังไม่ได้เชื่อมต่อเครือข่ายครับ",
            "Wi-Fi is on and connected.": "Wi-Fi เปิดอยู่และเชื่อมต่อแล้วครับ",
            "Dark mode is on.": "เปิดโหมดมืดแล้วครับ", "Light mode is on.": "เปลี่ยนเป็นโหมดสว่างแล้วครับ",
            "Screen locked.": "ล็อกหน้าจอแล้วครับ", "Display asleep.": "ปิดหน้าจอแล้วครับ",
            "No timers to cancel.": "ตอนนี้ไม่มีการตั้งเวลาครับ", "No timers running.": "ตอนนี้ไม่มีการตั้งเวลาครับ"}
    if result in wifi:
        return wifi[result]
    m = re.fullmatch(r"(.+) is (already )?open\.", result)
    if name == "open_app" and m:
        return f"เปิด {m[1]} ให้แล้วครับ"
    m = re.fullmatch(r"(.+) is closed\.", result)
    if name == "quit_app" and m:
        return f"ปิด {m[1]} แล้วครับ"
    m = re.fullmatch(r"(.+) was not running\.", result)
    if name == "quit_app" and m:
        return f"{m[1]} ไม่ได้เปิดอยู่ครับ"
    if name == "open_url" and result.startswith("Opened "):
        return "เปิดหน้าเว็บให้แล้วครับ"
    if name == "file_action" and result.startswith("Opened "):
        return "เปิดโฟลเดอร์ให้แล้วครับ"
    if name == "set_timer" and result.startswith("Timer set for "):
        return "ตั้งเวลาให้แล้วครับ " + _thai_units(result.removeprefix("Timer set for ").rstrip("."))
    m = re.fullmatch(r"(?:One timer|(\d+) timers): (.+)\.", result)
    if name == "timers" and m:
        parts = [re.sub(r"^(.+), (.+) left$", r"\1 เหลืออีก \2", p) for p in m[2].split("; ")]
        parts = [_thai_units(re.sub(r"^(.+) left$", r"เหลืออีก \1", p)) for p in parts]
        return f"มีตั้งเวลา {m[1] or 1} รายการ: " + " และ ".join(parts) + " ครับ"
    m = re.fullmatch(r"Cancelled (\d+) timers?\.", result)
    if m:
        return f"ยกเลิกการตั้งเวลา {m[1]} รายการแล้วครับ"
    if name == "timers" and result.startswith("Cancelled the "):
        return "ยกเลิกการตั้งเวลาแล้วครับ"
    if name == "reminders" and result.startswith("Reminder set:"):
        when = args.get("when")
        try:
            spoken = _thai_when(datetime.strptime(when, "%Y-%m-%d %H:%M")) if when else ""
        except ValueError:
            spoken = when or ""
        return f"ตั้งเตือน {args.get('text', '')}" + (f" {spoken}" if spoken else "") + " แล้วครับ"
    if name == "calendar" and result.startswith("Added "):
        try:
            spoken = _thai_when(datetime.strptime(args.get("start", ""), "%Y-%m-%d %H:%M"))
        except ValueError:
            spoken = ""
        return f"ลง{args.get('title', 'นัด')} ในปฏิทิน {spoken} แล้วครับ".replace("  ", " ")
    if name == "notes" and result.startswith("Note saved"):
        return "จดโน้ตไว้แล้วครับ"
    if name == "remember" and result.startswith("Noted"):
        return "จำไว้แล้วครับ"
    if name == "screen_look" and result.startswith("Saved the screenshot"):
        return "แคปหน้าจอแล้วครับ บันทึกไว้ที่เดสก์ท็อป"
    m = re.fullmatch(r"Closed (.+)\.", result)
    if name == "chrome_tabs" and m:
        return "ปิดแท็บแล้วครับ"
    if name == "chrome_tabs" and args.get("action") == "new" and not result.startswith("Error"):
        return "เปิดแท็บใหม่แล้วครับ"
    return result  # preserve partial outcomes and permission diagnostics verbatim
