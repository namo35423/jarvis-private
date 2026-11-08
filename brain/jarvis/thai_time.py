"""Thai clock times and days -> exact times.

Thai uses a six-hour clock (ตีสอง = 02:00, บ่ายสอง = 14:00, สองทุ่ม = 20:00) that
small models routinely get wrong ("สองทุ่ม" -> 2 PM). Everything here is
deterministic so a reminder or alarm lands on the hour the user meant.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_WORDS = {"หนึ่ง": 1, "นึง": 1, "สอง": 2, "สาม": 3, "สี่": 4, "ห้า": 5, "หก": 6, "เจ็ด": 7,
          "แปด": 8, "เก้า": 9, "สิบ": 10, "สิบเอ็ด": 11, "สิบสอง": 12}
N = r"(\d{1,2}|สิบเอ็ด|สิบสอง|หนึ่ง|นึง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า|สิบ)"
_HALF = r"(\s*ครึ่ง)?"
_MIN = r"(?:\s*(\d{1,2})\s*นาที)?"


def _n(s: str | None, default: int = 0) -> int:
    if not s:
        return default
    return int(s) if s.isdigit() else _WORDS[s]


def _minutes(half: str | None, mins: str | None) -> int:
    return 30 if half else (int(mins) if mins else 0)


def _six(m: re.Match, alarm: bool) -> tuple[int, int]:
    """'N โมง (เช้า|เย็น)' - the bare form depends on the hour."""
    h, part = _n(m[1], 7 if m[2] == "เช้า" else 0), m[2]
    if part in ("เย็น", "ค่ำ"):
        h = h + 12 if h < 12 else h
    elif part != "เช้า":
        if 1 <= h <= 5:
            h += 12          # สองโมง, สี่โมง: afternoon
        elif h == 6 and not alarm:
            h = 18           # "เจอกันหกโมง" is evening; "ปลุกหกโมง" is morning
    return h % 24, _minutes(m[3], m[4])


# Ordered: specific forms first. Each yields (hour, minute).
_PATTERNS: list[tuple[re.Pattern, object]] = [
    (re.compile(r"(\d{1,2}):(\d{2})(?:\s*(?:น\.|นาฬิกา))?"), lambda m, a: (int(m[1]), int(m[2]))),
    (re.compile(r"(\d{1,2})\.(\d{2})\s*(?:น\.|นาฬิกา)"), lambda m, a: (int(m[1]), int(m[2]))),
    (re.compile(r"(\d{1,2})\s*นาฬิกา" + _MIN), lambda m, a: (int(m[1]), int(m[2] or 0))),
    (re.compile(r"เที่ยงคืน|สองยาม"), lambda m, a: (0, 0)),
    (re.compile(r"ตี\s*" + N + _HALF + _MIN), lambda m, a: (_n(m[1]), _minutes(m[2], m[3]))),
    (re.compile(N + r"\s*ทุ่ม" + _HALF + _MIN), lambda m, a: ((18 + _n(m[1])) % 24, _minutes(m[2], m[3]))),
    (re.compile(r"ทุ่ม(?:นึง|หนึ่ง)?" + _HALF + _MIN), lambda m, a: (19, _minutes(m[1], m[2]))),
    (re.compile(r"เที่ยง(?:วัน)?" + _HALF + _MIN), lambda m, a: (12, _minutes(m[1], m[2]))),
    (re.compile(r"บ่าย\s*" + N + r"?\s*(?:โมง)?" + _HALF + _MIN),
     lambda m, a: (12 + _n(m[1], 1), _minutes(m[2], m[3]))),
    (re.compile(N + r"?\s*(?<!ชั่ว)โมง\s*(เช้า|เย็น|ค่ำ)?" + _HALF + _MIN), _six),
]


def find_time(text: str, alarm: bool = False) -> tuple[int, int, tuple[int, int]] | None:
    """(hour, minute, (start, end) of the match) for the first clock time in `text`."""
    best = None
    for pattern, build in _PATTERNS:
        for m in pattern.finditer(text):
            if pattern.pattern.startswith(N) and not m[1] and m[2] != "เช้า":
                continue  # a bare "โมง" with no number is not a time
            try:
                h, mi = build(m, alarm)
            except (KeyError, ValueError):
                continue
            if 0 <= h <= 23 and 0 <= mi <= 59 and (best is None or m.start() < best[2][0]):
                best = (h, mi, m.span())
            break
    return best


_DAYS = {"จันทร์": 0, "อังคาร": 1, "พุธ": 2, "พฤหัส": 3, "พฤหัสบดี": 3, "ศุกร์": 4, "เสาร์": 5, "อาทิตย์": 6}
_DAY = re.compile(r"มะรืน(?:นี้)?|พรุ่งนี้|วันนี้|คืนนี้|เย็นนี้|เช้านี้|"
                  r"(?:วัน)?(พฤหัสบดี|พฤหัส|จันทร์|อังคาร|พุธ|ศุกร์|เสาร์|อาทิตย์)(?:\s*(หน้า|นี้))?")


def find_day(text: str, today: date | None = None) -> tuple[date, tuple[int, int]] | None:
    today = today or date.today()
    m = _DAY.search(text)
    if not m:
        return None
    word = m[0]
    if word.startswith("มะรืน"):
        return today + timedelta(days=2), m.span()
    if word == "พรุ่งนี้":
        return today + timedelta(days=1), m.span()
    if word in ("วันนี้", "คืนนี้", "เย็นนี้", "เช้านี้"):
        return today, m.span()
    if m[1] == "อาทิตย์" and not word.startswith("วัน") and m[2] is None:
        return None  # "อาทิตย์" alone usually means "week"
    ahead = (_DAYS[m[1]] - today.weekday()) % 7
    if ahead == 0 and m[2] == "หน้า":
        ahead = 7  # "ศุกร์หน้า" said on a Friday; otherwise the coming one (the reply says the date)
    return today + timedelta(days=ahead), m.span()


def resolve(text: str, alarm: bool = False, now: datetime | None = None):
    """(datetime or None, text with the day/time words removed, had_time)."""
    now = now or datetime.now()
    t = find_time(text, alarm)
    d = find_day(text, now.date())
    spans = [x for x in ((t[2] if t else None), (d[1] if d else None)) if x]
    rest = text
    for start, end in sorted(spans, reverse=True):
        rest = rest[:start] + " " + rest[end:]
    rest = " ".join(rest.split())
    if not t and not d:
        return None, rest, False
    day = d[0] if d else now.date()
    hour, minute = (t[0], t[1]) if t else (9, 0)
    when = datetime(day.year, day.month, day.day, hour, minute)
    if t and not d and when <= now:
        when += timedelta(days=1)  # "สองทุ่ม" said at 21:00 means tomorrow
    return when, rest, bool(t)


def explicit_times(text: str, alarm: bool = False) -> str:
    """Rewrite every Thai clock time as HH:MM for the model ("ตอนสองทุ่ม" -> "ตอน 20:00")."""
    out = text
    for _ in range(4):
        t = find_time(out, alarm)
        if not t or re.fullmatch(r"\d{1,2}:\d{2}", out[t[2][0]:t[2][1]]):
            break
        start, end = t[2]
        out = f"{out[:start]} {t[0]:02d}:{t[1]:02d} {out[end:]}".replace("  ", " ").strip()
    return out
