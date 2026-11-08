"""Place names as speech recognition actually writes them.

Whisper spells Thai place names by sound, and Thai has many letters for one
sound: "ฮ่องกง" comes back as "ห้องกง", "ห้องกลง", "ห่องคอง"; "กรุงเทพ" as
"กุงเทพ". A geocoder given those finds nothing. Here every name is reduced to a
sound key (tone marks dropped, same-sounding consonants merged, r/l merged,
silent letters removed) and compared with a built-in list of Thai provinces and
the cities Thai speakers ask about most. Anything not on the list is left for
the geocoder, and then for the model (see tools/web.py).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache

# Thai canonical name (what Jarvis says), English name (what the geocoder gets), aliases.
_PLACES: list[tuple[str, str, tuple[str, ...]]] = [
    # Thai provinces and the towns people name instead of the province
    ("กรุงเทพฯ", "Bangkok", ("กรุงเทพ", "กรุงเทพมหานคร", "กทม", "บางกอก")),
    ("กระบี่", "Krabi", ()), ("กาญจนบุรี", "Kanchanaburi", ()), ("กาฬสินธุ์", "Kalasin", ()),
    ("กำแพงเพชร", "Kamphaeng Phet", ()), ("ขอนแก่น", "Khon Kaen", ()), ("จันทบุรี", "Chanthaburi", ()),
    ("ฉะเชิงเทรา", "Chachoengsao", ()), ("ชลบุรี", "Chon Buri", ()), ("ชัยนาท", "Chai Nat", ()),
    ("ชัยภูมิ", "Chaiyaphum", ()), ("ชุมพร", "Chumphon", ()), ("เชียงราย", "Chiang Rai", ()),
    ("เชียงใหม่", "Chiang Mai", ()), ("ตรัง", "Trang", ()), ("ตราด", "Trat", ()), ("ตาก", "Tak", ()),
    ("นครนายก", "Nakhon Nayok", ()), ("นครปฐม", "Nakhon Pathom", ()), ("นครพนม", "Nakhon Phanom", ()),
    ("นครราชสีมา", "Nakhon Ratchasima", ("โคราช",)), ("นครศรีธรรมราช", "Nakhon Si Thammarat", ()),
    ("นครสวรรค์", "Nakhon Sawan", ()), ("นนทบุรี", "Nonthaburi", ()), ("นราธิวาส", "Narathiwat", ()),
    ("น่าน", "Nan", ()), ("บึงกาฬ", "Bueng Kan", ()), ("บุรีรัมย์", "Buriram", ()),
    ("ปทุมธานี", "Pathum Thani", ()), ("ประจวบคีรีขันธ์", "Prachuap Khiri Khan", ()),
    ("ปราจีนบุรี", "Prachin Buri", ()), ("ปัตตานี", "Pattani", ()),
    ("อยุธยา", "Ayutthaya", ("พระนครศรีอยุธยา",)), ("พะเยา", "Phayao", ()), ("พังงา", "Phang Nga", ()),
    ("พัทลุง", "Phatthalung", ()), ("พิจิตร", "Phichit", ()), ("พิษณุโลก", "Phitsanulok", ()),
    ("เพชรบุรี", "Phetchaburi", ()), ("เพชรบูรณ์", "Phetchabun", ()), ("แพร่", "Phrae", ()),
    ("ภูเก็ต", "Phuket", ()), ("มหาสารคาม", "Maha Sarakham", ()), ("มุกดาหาร", "Mukdahan", ()),
    ("แม่ฮ่องสอน", "Mae Hong Son", ()), ("ยโสธร", "Yasothon", ()), ("ยะลา", "Yala", ()),
    ("ร้อยเอ็ด", "Roi Et", ()), ("ระนอง", "Ranong", ()), ("ระยอง", "Rayong", ()),
    ("ราชบุรี", "Ratchaburi", ()), ("ลพบุรี", "Lop Buri", ()), ("ลำปาง", "Lampang", ()),
    ("ลำพูน", "Lamphun", ()), ("เลย", "Loei", ()), ("ศรีสะเกษ", "Si Sa Ket", ()),
    ("สกลนคร", "Sakon Nakhon", ()), ("สงขลา", "Songkhla", ()), ("หาดใหญ่", "Hat Yai", ()),
    ("สตูล", "Satun", ()), ("สมุทรปราการ", "Samut Prakan", ()), ("สมุทรสงคราม", "Samut Songkhram", ()),
    ("สมุทรสาคร", "Samut Sakhon", ()), ("สระแก้ว", "Sa Kaeo", ()), ("สระบุรี", "Saraburi", ()),
    ("สิงห์บุรี", "Sing Buri", ()), ("สุโขทัย", "Sukhothai", ()), ("สุพรรณบุรี", "Suphan Buri", ()),
    ("สุราษฎร์ธานี", "Surat Thani", ()), ("สุรินทร์", "Surin", ()), ("หนองคาย", "Nong Khai", ()),
    ("หนองบัวลำภู", "Nong Bua Lam Phu", ()), ("อ่างทอง", "Ang Thong", ()), ("อำนาจเจริญ", "Amnat Charoen", ()),
    ("อุดรธานี", "Udon Thani", ("อุดร",)), ("อุตรดิตถ์", "Uttaradit", ()), ("อุทัยธานี", "Uthai Thani", ()),
    ("อุบลราชธานี", "Ubon Ratchathani", ("อุบล",)), ("พัทยา", "Pattaya", ()), ("หัวหิน", "Hua Hin", ()),
    ("เกาะสมุย", "Ko Samui", ("สมุย",)), ("เกาะช้าง", "Ko Chang", ()),
    # Abroad: where Thai speakers live, study and travel
    ("ฮ่องกง", "Hong Kong", ()), ("มาเก๊า", "Macau", ()), ("สิงคโปร์", "Singapore", ()),
    ("กัวลาลัมเปอร์", "Kuala Lumpur", ()), ("โตเกียว", "Tokyo", ()), ("โอซาก้า", "Osaka", ()),
    ("เกียวโต", "Kyoto", ()), ("ซัปโปโร", "Sapporo", ("ฮอกไกโด",)), ("ฟุกุโอกะ", "Fukuoka", ()),
    ("โซล", "Seoul", ()), ("ปูซาน", "Busan", ()), ("ปักกิ่ง", "Beijing", ()), ("เซี่ยงไฮ้", "Shanghai", ()),
    ("เซินเจิ้น", "Shenzhen", ()), ("กวางโจว", "Guangzhou", ()), ("ไทเป", "Taipei", ()),
    ("ฮานอย", "Hanoi", ()), ("โฮจิมินห์", "Ho Chi Minh City", ("ไซง่อน",)), ("ดานัง", "Da Nang", ()),
    ("เวียงจันทน์", "Vientiane", ()), ("หลวงพระบาง", "Luang Prabang", ()), ("พนมเปญ", "Phnom Penh", ()),
    ("เสียมราฐ", "Siem Reap", ()), ("ย่างกุ้ง", "Yangon", ()), ("มะนิลา", "Manila", ()),
    ("จาการ์ตา", "Jakarta", ()), ("บาหลี", "Bali", ()), ("นิวเดลี", "New Delhi", ()), ("มุมไบ", "Mumbai", ()),
    ("ดูไบ", "Dubai", ()), ("ลอนดอน", "London", ()), ("ปารีส", "Paris", ()), ("เบอร์ลิน", "Berlin", ()),
    ("โรม", "Rome", ()), ("มิลาน", "Milan", ()), ("อัมสเตอร์ดัม", "Amsterdam", ()), ("ซูริก", "Zurich", ()),
    ("นิวยอร์ก", "New York", ()), ("ลอสแอนเจลิส", "Los Angeles", ("แอลเอ",)),
    ("ซานฟรานซิสโก", "San Francisco", ()), ("ซีแอตเทิล", "Seattle", ()), ("ชิคาโก", "Chicago", ()),
    ("โทรอนโต", "Toronto", ()), ("แวนคูเวอร์", "Vancouver", ()), ("ซิดนีย์", "Sydney", ()),
    ("เมลเบิร์น", "Melbourne", ()), ("โอ๊คแลนด์", "Auckland", ()), ("มอสโก", "Moscow", ()),
    ("อิสตันบูล", "Istanbul", ()),
]

_TONES = re.compile(r"[่-๋็]")          # ่ ้ ๊ ๋ ็
_SILENT = re.compile(r"[ก-ฮ](?:[ิุ])?์")  # consonant (+ ิ/ุ) under ์
# Letters that sound alike (initial sounds), merged loosely: aspirated/unaspirated pairs too,
# because recognition confuses them ("ห่องคอง" for "ฮ่องกง").
_SAME = str.maketrans({
    "ฮ": "ห", "ข": "ก", "ฃ": "ก", "ค": "ก", "ฅ": "ก", "ฆ": "ก",
    "ฉ": "จ", "ช": "จ", "ฌ": "จ", "ซ": "ส", "ศ": "ส", "ษ": "ส", "ญ": "ย",
    "ฎ": "ด", "ฏ": "ต", "ฐ": "ต", "ฑ": "ต", "ฒ": "ต", "ถ": "ต", "ท": "ต", "ธ": "ต",
    "ณ": "น", "ผ": "ป", "พ": "ป", "ภ": "ป", "ฝ": "ฟ", "ฬ": "ล", "ร": "ล", "ฤ": "ล",
    "ฯ": "", " ": "", "-": "", ".": "",
})


def sound_key(text: str) -> str:
    """How a name sounds, roughly, so different spellings of one sound compare equal."""
    t = _SILENT.sub("", (text or "").lower())
    t = _TONES.sub("", t)
    return t.translate(_SAME)


@lru_cache(maxsize=1)
def _index() -> list[tuple[str, str, str]]:
    """(sound key, canonical Thai, English) for every name and alias."""
    out = []
    for thai, english, aliases in _PLACES:
        for name in (thai, *aliases):
            out.append((sound_key(name), thai, english))
        out.append((english.lower().replace(" ", ""), thai, english))
    out.sort(key=lambda r: -len(r[0]))  # longest first: "แม่ฮ่องสอน" before "ฮ่องกง"
    return out


def english_name(thai_or_english: str) -> str | None:
    """English name for a place on the list (exact or sound-alike), else None."""
    hit = best_match(thai_or_english, whole=True)
    return hit[1] if hit else None


def best_match(text: str, whole: bool = False, fuzzy: bool = True,
               threshold: float = 0.8) -> tuple[str, str, float] | None:
    """The listed place named in `text`: (canonical Thai, English, score).

    `whole`: `text` is only the name. Otherwise the name may sit inside `text`;
    callers pass a sentence with fuzzy=False (exact names only), or the words left
    once everything that is not a place has been removed (fuzzy).
    A sound-alike must start with the same sound: recognition rarely gets the first
    syllable wrong, and requiring it keeps "อากาศ" from turning into Osaka.
    """
    key = sound_key(text)
    if len(key) < 2:
        return None
    best: tuple[str, str, float] | None = None
    for k, thai, english in _index():
        if len(k) < 2 or (not whole and len(k) < 4):
            continue  # short names ("เลย", "น่าน") are everyday words inside a sentence
        if (key == k) if whole else (k in key):
            return thai, english, 1.0
        if not fuzzy:
            continue
        if whole:
            spans = [key]
        else:
            spans = [key[i:i + n] for i in range(len(key)) if key[i] == k[0]
                     for n in range(max(2, len(k) - 2), len(k) + 3) if i + n <= len(key)]
        for span in spans:
            if span[:1] != k[:1] or abs(len(span) - len(k)) > 2:
                continue
            score = SequenceMatcher(None, span, k).ratio()
            if score >= threshold and (best is None or score > best[2]):
                best = (thai, english, score)
    return best
