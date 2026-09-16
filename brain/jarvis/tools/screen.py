"""Seeing the screen: screenshot, OCR, describe, and click what you see.

Two different eyes here. Vision-framework OCR is exact and fast, and gives
coordinates, so it drives clicking. The local vision model is for questions
about what is actually going on ("what does this error say", "what am I
looking at") - qwen3.5 has vision built in, so this still costs nothing.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
import time

from jarvis.sysutil import ToolError, run, truncate
from jarvis.tools import ToolContext, tool

RISKY_WORDS = re.compile(
    r"\b(buy|purchase|order|checkout|pay|delete|remove|erase|send|post|publish|confirm|"
    r"transfer|withdraw|uninstall|format|shut down|restart)\b", re.I
)


def _confirm_click(args: dict) -> str | None:
    if (args.get("mode") or "") != "click_text":
        return None
    target = args.get("target") or ""
    return f"Click '{target}' on screen?" if RISKY_WORDS.search(target) else None


def screen_recording_allowed() -> bool:
    try:
        import Quartz

        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return True  # if we cannot tell, let the capture itself be the test


async def capture(path: str, region: str = "") -> None:
    """Silent, cursor-free capture of the main display."""
    cmd = ["/usr/sbin/screencapture", "-x", "-C", "-o", "-t", "png"]
    if region:
        cmd += ["-R", region]
    cmd.append(path)
    proc = await run(cmd, timeout=20)
    if proc.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) < 1000:
        raise ToolError(
            "I can't see the screen: Jarvis needs Screen Recording permission. "
            "Open System Settings, Privacy & Security, Screen & System Audio Recording, and switch Jarvis on."
        )


def ocr_sync(path: str) -> list[dict]:
    """Vision OCR. Returns text plus centre points in global screen coordinates."""
    import Quartz
    import Vision
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(path)
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    if source is None:
        raise ToolError("could not read the screenshot")
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        raise ToolError("could not decode the screenshot")

    pixel_w = Quartz.CGImageGetWidth(image)
    pixel_h = Quartz.CGImageGetHeight(image)
    main = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    point_w, point_h = main.size.width, main.size.height
    scale_x = point_w / pixel_w if pixel_w else 1.0
    scale_y = point_h / pixel_h if pixel_h else 1.0

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(1)  # accurate
    request.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise ToolError(f"OCR failed: {err}")

    out = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        text = candidates[0].string()
        box = observation.boundingBox()  # normalised, origin bottom-left
        cx_pixels = (box.origin.x + box.size.width / 2) * pixel_w
        cy_pixels = (1.0 - (box.origin.y + box.size.height / 2)) * pixel_h
        out.append({
            "text": text,
            "x": cx_pixels * scale_x,
            "y": cy_pixels * scale_y,
            "top": (1.0 - (box.origin.y + box.size.height)) * pixel_h * scale_y,
            "left": box.origin.x * pixel_w * scale_x,
            "confidence": float(candidates[0].confidence()),
        })
    return out


def reading_order(items: list[dict]) -> str:
    rows: list[list[dict]] = []
    for item in sorted(items, key=lambda i: (round(i["top"] / 14), i["left"])):
        if rows and abs(rows[-1][-1]["top"] - item["top"]) < 10:
            rows[-1].append(item)
        else:
            rows.append([item])
    return "\n".join(" ".join(i["text"] for i in row) for row in rows)


def downscale_b64(path: str, max_side: int = 1024) -> str:
    """Shrink before sending to the vision model - full Retina frames are huge."""
    import Quartz
    from Foundation import NSURL, NSMutableData

    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    options = {
        Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
        Quartz.kCGImageSourceThumbnailMaxPixelSize: max_side,
        Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
    }
    thumb = Quartz.CGImageSourceCreateThumbnailAtIndex(src, 0, options)
    data = NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    Quartz.CGImageDestinationAddImage(dest, thumb, {Quartz.kCGImageDestinationLossyCompressionQuality: 0.7})
    Quartz.CGImageDestinationFinalize(dest)
    return base64.b64encode(bytes(data)).decode("ascii")


@tool(
    name="screen_look",
    description=(
        "Look at what is on the screen right now. Use 'describe' to answer a question about it "
        "('what does this error say', 'what am I looking at'), 'read_text' to get the exact text, "
        "'click_text' to click something visible by its label, or 'save' to keep a screenshot."
    ),
    params={
        "mode": {"type": "string", "enum": ["describe", "read_text", "click_text", "save"]},
        "question": {"type": "string", "description": "What to answer about the screen, for describe"},
        "target": {"type": "string", "description": "The visible text to click, for click_text"},
    },
    required=["mode"],
    risk=_confirm_click,
    ack="Let me look.",
    category="screen",
)
async def screen_look(ctx: ToolContext, mode: str, question: str = "", target: str = "") -> str:
    mode = mode.lower()
    if not screen_recording_allowed():
        raise ToolError(
            "Jarvis does not have Screen Recording permission yet. Open System Settings, "
            "Privacy & Security, Screen & System Audio Recording, and switch Jarvis on."
        )

    if mode == "save":
        dest = os.path.expanduser(time.strftime("~/Desktop/Screenshot %Y-%m-%d at %H.%M.%S.png"))
        await capture(dest)
        return f"Saved the screenshot to the Desktop as {os.path.basename(dest)}."

    fd, path = tempfile.mkstemp(suffix=".png", prefix="jarvis-screen-")
    os.close(fd)
    try:
        await capture(path)
        loop = __import__("asyncio").get_running_loop()

        if mode == "describe":
            prompt = question.strip() or "Describe what is on this screen in two sentences."
            # The text on screen answers most questions, and the main model reads it in
            # about a second. The image model only wakes for screens with little text.
            items = await loop.run_in_executor(None, ocr_sync, path)
            text = reading_order(items) if items else ""
            if len(text) >= 40:
                answer = await ctx.assistant._side_chat(
                    f"This is the text currently visible on the user's screen:\n\n{text[:3500]}\n\n"
                    f"Using it, answer: {prompt}\nReply in one or two spoken sentences. Do not call any tools.", 140)
                if answer:
                    return answer
            b64 = await loop.run_in_executor(None, downscale_b64, path)
            answer = await ctx.assistant.llm.vision(prompt, b64)
            return answer or "I could not make sense of the screen."

        items = await loop.run_in_executor(None, ocr_sync, path)
        if mode == "read_text":
            if not items:
                return "There is no readable text on screen."
            return truncate(reading_order(items), 2500)

        if mode == "click_text":
            if not target:
                raise ToolError("say what to click")
            needle = target.strip().lower()
            best, score = None, 0.0
            for item in items:
                text = item["text"].strip().lower()
                if not text:
                    continue
                s = 1.0 if text == needle else (0.8 if text.startswith(needle) else (0.6 if needle in text else 0.0))
                s *= item.get("confidence", 1.0)
                if s > score:
                    best, score = item, s
            if not best:
                visible = ", ".join(i["text"] for i in items[:12])
                raise ToolError(f"I can't see '{target}' on screen. I can see: {visible}")
            from jarvis.tools.input import click_at

            await click_at(best["x"], best["y"])
            return f"Clicked '{best['text'].strip()}'."

        raise ToolError(f"unknown mode {mode!r}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
