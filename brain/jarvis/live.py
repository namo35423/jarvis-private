"""Live readings for the Command Center: CPU, memory, battery, the local AI service.

Everything comes from the machine itself (Mach host statistics, vm_stat, pmset,
Ollama's own API). Nothing is estimated: a reading that fails is returned as
None and the UI says "ไม่พร้อมใช้งาน" instead of inventing a number.
Readings are cached for a couple of seconds so several open windows cost the
same as one.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import re
import time
from typing import Any

import httpx

_libc = ctypes.CDLL(ctypes.util.find_library("c"))
_libc.mach_host_self.restype = ctypes.c_uint32
_HOST = _libc.mach_host_self()  # one send right for the process lifetime
_HOST_CPU_LOAD_INFO = 3

_prev_ticks: list[int] | None = None
_cache: dict[str, tuple[float, Any]] = {}


def _cpu_ticks() -> list[int] | None:
    """Cumulative user, system, idle, nice ticks across every core."""
    info = (ctypes.c_uint32 * 4)()
    count = ctypes.c_uint32(4)
    kr = _libc.host_statistics(ctypes.c_uint32(_HOST), ctypes.c_int(_HOST_CPU_LOAD_INFO),
                               ctypes.cast(info, ctypes.POINTER(ctypes.c_int32)), ctypes.byref(count))
    return list(info) if kr == 0 else None


def cpu_percent() -> float | None:
    """Whole-machine CPU use since the previous reading (the first one samples 0.25 s)."""
    global _prev_ticks
    now = _cpu_ticks()
    if now is None:
        return None
    if _prev_ticks is None:
        _prev_ticks = now
        time.sleep(0.25)
        now = _cpu_ticks() or now
    d = [b - a for a, b in zip(_prev_ticks, now)]
    _prev_ticks = now
    total = sum(d)
    if total <= 0:
        return None
    return round(100.0 * (d[0] + d[1] + d[3]) / total, 1)


def _sysctl_int(name: str) -> int | None:
    size = ctypes.c_size_t(8)
    val = ctypes.c_int64(0)
    if _libc.sysctlbyname(name.encode(), ctypes.byref(val), ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
        return None
    return int(val.value) if size.value == 8 else int(ctypes.c_int32.from_buffer(val).value)


async def _out(*cmd: str, timeout: float = 3.0) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode(errors="replace")


async def memory() -> dict | None:
    """Used and total, counted the way Activity Monitor does: app + wired + compressed."""
    total = _sysctl_int("hw.memsize")
    text = await _out("/usr/bin/vm_stat")
    m = re.search(r"page size of (\d+) bytes", text)
    if not total or not m:
        return None
    page = int(m[1])

    def pages(label: str) -> int:
        hit = re.search(rf"^{re.escape(label)}:\s+(\d+)\.", text, re.M)
        return int(hit[1]) if hit else 0

    app = max(0, pages("Anonymous pages") - pages("Pages purgeable"))
    used = (app + pages("Pages wired down") + pages("Pages occupied by compressor")) * page
    free_level = _sysctl_int("kern.memorystatus_level")  # % of memory the kernel considers free
    return {"used": min(used, total), "total": total,
            "percent": round(100.0 * min(used, total) / total, 1),
            "pressure_free": free_level}


async def battery() -> dict | None:
    text = await _out("/usr/bin/pmset", "-g", "batt")
    m = re.search(r"(\d+)%;\s*([^;]+);", text)
    if not m:
        return None  # a desktop Mac, or pmset said nothing useful
    state = m[2].strip().lower()
    remaining = re.search(r"(\d+:\d+) remaining", text)
    return {
        "percent": int(m[1]),
        "state": state,                       # charging, discharging, charged, AC attached, finishing charge
        "charging": state in ("charging", "finishing charge"),
        "plugged": "AC Power" in text,
        "remaining": remaining[1] if remaining and remaining[1] != "0:00" else None,
    }


_ollama_misses = 0
_ollama_last: dict | None = None


async def ollama(url: str, model: str) -> dict:
    """Down only after two failed checks in a row: one slow answer under heavy load is not an outage."""
    global _ollama_misses, _ollama_last
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url.rstrip("/") + "/api/ps")
            resp.raise_for_status()
            running = resp.json().get("models") or []
    except Exception:
        _ollama_misses += 1
        if _ollama_misses < 2 and _ollama_last is not None:
            return _ollama_last
        return {"up": False, "model": model, "loaded": False}
    _ollama_misses = 0
    hit = next((m for m in running if m.get("name") == model or m.get("model") == model), None)
    _ollama_last = {"up": True, "model": model, "loaded": hit is not None,
                    "vram": (hit or {}).get("size_vram"), "running": [m.get("name") for m in running]}
    return _ollama_last


async def snapshot(cfg) -> dict:
    """Everything the right-hand panel shows. Cached for 2 s."""
    hit = _cache.get("live")
    if hit and time.monotonic() - hit[0] < 2.0:
        return hit[1]
    loop = asyncio.get_running_loop()
    cpu, mem, batt, ai = await asyncio.gather(
        loop.run_in_executor(None, cpu_percent),
        memory(), battery(),
        ollama(cfg.get("llm.ollama_url", "http://127.0.0.1:11434"), cfg.get("llm.model", "")),
        return_exceptions=True,
    )
    data = {
        "time": time.time(),
        "cpu": cpu if isinstance(cpu, (int, float)) else None,
        "cores": _sysctl_int("hw.ncpu"),
        "memory": mem if isinstance(mem, dict) else None,
        "battery": batt if isinstance(batt, dict) else None,
        "ollama": ai if isinstance(ai, dict) else {"up": False, "model": cfg.get("llm.model", ""), "loaded": False},
    }
    _cache["live"] = (time.monotonic(), data)
    return data
