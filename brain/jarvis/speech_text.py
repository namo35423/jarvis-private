"""Turning model text into something worth hearing.

The model writes for a screen; the speaker needs prose. This module strips
markup, says URLs the way a person would, and cuts the stream into sentences so
speech can start while the model is still generating.
"""

from __future__ import annotations

import re

_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_IT = re.compile(r"(\*{1,3}|_{1,3})(\S.*?\S|\S)\1")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*([-*+]|\d+[.)])\s+", re.M)
_URL = re.compile(r"https?://([^\s/]+)(\S*)")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF←-⇿⬀-⯿️]"
)
_MULTISPACE = re.compile(r"[ \t]+")
_MULTINL = re.compile(r"\n{2,}")
# Sentence end: . ! ? followed by space/end, not inside a common abbreviation.
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
_ABBREV = re.compile(r"\b(mr|mrs|ms|dr|prof|sr|jr|st|vs|etc|e\.g|i\.e|approx|no|fig|dept)\.$", re.I)


def for_speech(text: str) -> str:
    """Strip anything that would be read out as punctuation soup."""
    if not text:
        return ""
    t = _CODE_FENCE.sub(" (code on screen) ", text)
    t = _MD_LINK.sub(r"\1", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _BOLD_IT.sub(r"\2", t)
    t = _HEADING.sub("", t)
    t = _BULLET.sub("", t)
    t = _URL.sub(lambda m: _spoken_url(m.group(1)), t)
    t = _EMOJI.sub("", t)
    t = t.replace("&nbsp;", " ").replace("…", "...")
    t = _MULTISPACE.sub(" ", t)
    t = _MULTINL.sub(". ", t)
    return t.strip()


def _spoken_url(host: str) -> str:
    host = host.lower().removeprefix("www.")
    return host.replace(".", " dot ") if len(host) < 24 else "a link"


def split_sentences(text: str) -> list[str]:
    """Split into speakable sentences, keeping abbreviations intact."""
    if not text:
        return []
    out: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        piece = text[start : m.start()].strip()
        if not piece:
            start = m.end()
            continue
        if _ABBREV.search(piece):  # "Dr." is not the end of a sentence
            continue
        out.append(piece)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


class SentenceStream:
    """Accumulates streaming deltas and yields whole sentences to speak.

    Speech starts on the first complete sentence, which is what makes the reply
    feel immediate: the model is usually still writing sentence three when
    sentence one is already being spoken.
    """

    def __init__(self, min_chars: int = 18):
        self.min_chars = min_chars
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        """Cut complete sentences off the front of the buffer.

        One forward pass over sentence boundaries: `start` only ever moves right,
        so this always terminates. A short opener ("Right.") is merged into the
        next sentence simply by not cutting after it.

        The previous version merged by rebuilding the buffer and re-splitting it;
        "Yeah. Fine. It's been good." rebuilt the same buffer forever and froze the
        whole brain, event loop and all.
        """
        self._buf += delta or ""
        ready: list[str] = []
        start = 0
        for m in _SENTENCE_END.finditer(self._buf):
            candidate = self._buf[start : m.start()].strip()
            if not candidate:
                start = m.end()
                continue
            if _ABBREV.search(candidate) or len(candidate) < self.min_chars:
                continue  # "Dr." or a short opener: keep reading into the next sentence
            ready.append(candidate)
            start = m.end()
        self._buf = self._buf[start:]
        return [s for s in (for_speech(r) for r in ready) if s]

    def flush(self) -> str:
        rest = for_speech(self._buf)
        self._buf = ""
        return rest


def clip_for_speech(text: str, max_chars: int = 420) -> tuple[str, bool]:
    """Trim a long reply to something bearable aloud.

    Returns (spoken_text, was_clipped). The full text still goes to the screen.
    """
    spoken = for_speech(text)
    if len(spoken) <= max_chars:
        return spoken, False
    kept: list[str] = []
    total = 0
    for s in split_sentences(spoken):
        if total + len(s) > max_chars and kept:
            break
        kept.append(s)
        total += len(s) + 1
    if not kept:
        kept = [spoken[:max_chars].rsplit(" ", 1)[0]]
    return " ".join(kept).strip() + " The rest is on screen.", True
