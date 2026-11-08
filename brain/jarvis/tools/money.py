"""Currency conversion with a live rate, so amounts are never recalled from memory."""
from __future__ import annotations

import httpx

from jarvis.sysutil import ToolError
from jarvis.tools import ToolContext, tool

RATES_URL = "https://open.er-api.com/v6/latest/{base}"  # free, keyless, daily rates
NAMES_TH = {"THB": "บาท", "USD": "ดอลลาร์สหรัฐ", "EUR": "ยูโร", "JPY": "เยน", "CNY": "หยวน", "KRW": "วอน",
            "GBP": "ปอนด์", "SGD": "ดอลลาร์สิงคโปร์", "HKD": "ดอลลาร์ฮ่องกง", "AUD": "ดอลลาร์ออสเตรเลีย",
            "TWD": "ดอลลาร์ไต้หวัน", "MYR": "ริงกิต", "INR": "รูปี", "VND": "ดอง", "LAK": "กีบ", "CHF": "ฟรังก์สวิส"}


_MONTHS = {"Jan": "มกราคม", "Feb": "กุมภาพันธ์", "Mar": "มีนาคม", "Apr": "เมษายน", "May": "พฤษภาคม", "Jun": "มิถุนายน",
           "Jul": "กรกฎาคม", "Aug": "สิงหาคม", "Sep": "กันยายน", "Oct": "ตุลาคม", "Nov": "พฤศจิกายน", "Dec": "ธันวาคม"}


def _fmt(x: float) -> str:
    if float(x).is_integer():
        return f"{x:,.0f}"
    return f"{x:,.2f}" if x < 1000 else f"{x:,.0f}"


def _thai_date(stamp: str) -> str:
    """'Sat, 07 Nov 2026 00:00:01 +0000' -> 'วันที่ 7 พฤศจิกายน'."""
    parts = stamp.split()
    if len(parts) >= 3 and parts[1].isdigit() and parts[2] in _MONTHS:
        return f"วันที่ {int(parts[1])} {_MONTHS[parts[2]]}"
    return ""


@tool(
    name="convert_currency",
    description="Convert money between currencies at today's rate (e.g. USD to THB). Use for any exchange-rate question.",
    params={
        "amount": {"type": "number", "description": "Amount to convert, default 1"},
        "from_currency": {"type": "string", "description": "ISO code, e.g. USD"},
        "to_currency": {"type": "string", "description": "ISO code, e.g. THB"},
    },
    required=["from_currency"],
    category="web",
)
async def convert_currency(ctx: ToolContext, from_currency: str, to_currency: str = "THB", amount: float = 1) -> str:
    src, dst = (from_currency or "").strip().upper(), (to_currency or "THB").strip().upper()
    if len(src) != 3 or len(dst) != 3:
        raise ToolError("ระบุสกุลเงินเป็นรหัสสามตัว เช่น USD หรือ THB")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(RATES_URL.format(base=src))
            data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolError("ดึงอัตราแลกเปลี่ยนไม่ได้ครับ ตรวจการเชื่อมต่ออินเทอร์เน็ต") from exc
    rate = (data.get("rates") or {}).get(dst) if data.get("result") == "success" else None
    if not rate:
        raise ToolError(f"ไม่พบอัตราแลกเปลี่ยน {src} เป็น {dst} ครับ")
    amount = float(amount or 1)
    updated = _thai_date(data.get("time_last_update_utc") or "")
    return (f"{_fmt(amount)} {NAMES_TH.get(src, src)} เท่ากับประมาณ {_fmt(amount * rate)} {NAMES_TH.get(dst, dst)}ครับ"
            + (f" ตามอัตรา{updated}" if updated else ""))
