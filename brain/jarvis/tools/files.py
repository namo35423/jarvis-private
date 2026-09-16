"""Finding, reading and tidying files.

Deletion always means the Trash, never rm: a voice assistant acting on a
misheard word must never be able to destroy something irreversibly.
"""

from __future__ import annotations

import os
import shutil
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from jarvis.sysutil import ToolError, applescript_quote, osascript, run, truncate
from jarvis.tools import ToolContext, tool

SHORTHAND = {
    "desktop": "~/Desktop", "downloads": "~/Downloads", "documents": "~/Documents",
    "docs": "~/Documents", "home": "~", "pictures": "~/Pictures", "photos": "~/Pictures",
    "music": "~/Music", "movies": "~/Movies", "applications": "/Applications",
    "apps": "/Applications", "trash": "~/.Trash", "icloud": "~/Library/Mobile Documents/com~apple~CloudDocs",
}

KIND_QUERY = {
    "any": "", "folder": "kMDItemContentType == 'public.folder'",
    "image": "kMDItemContentTypeTree == 'public.image'",
    "pdf": "kMDItemContentType == 'com.adobe.pdf'",
    "document": "kMDItemContentTypeTree == 'public.content'",
    "audio": "kMDItemContentTypeTree == 'public.audio'",
    "video": "kMDItemContentTypeTree == 'public.movie'",
    "code": "kMDItemContentTypeTree == 'public.source-code'",
    "screenshot": "kMDItemIsScreenCapture == 1",
}

CATEGORIES = {
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".svg", ".bmp", ".tiff"},
    "Documents": {".pdf", ".doc", ".docx", ".pages", ".txt", ".rtf", ".md", ".epub"},
    "Spreadsheets": {".xls", ".xlsx", ".numbers", ".csv", ".tsv"},
    "Presentations": {".ppt", ".pptx", ".key"},
    "Video": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"},
    "Audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".aiff"},
    "Archives": {".zip", ".tar", ".gz", ".dmg", ".pkg", ".rar", ".7z"},
    "Code": {".py", ".js", ".ts", ".swift", ".c", ".cpp", ".h", ".java", ".rb", ".go", ".rs", ".sh", ".json", ".html", ".css"},
    "Installers": {".dmg", ".pkg", ".app"},
}

TEXT_SUFFIXES = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".html", ".css", ".swift",
                 ".c", ".h", ".cpp", ".sh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".log", ".xml", ".rtf"}


async def off_loop(fn, *args, timeout: float = 25.0):
    """Run blocking filesystem work in a worker, with a deadline.

    Touching ~/Downloads, ~/Desktop or ~/Documents for the first time makes
    macOS raise a consent prompt, and the call blocks inside the kernel until it
    is answered - which may be never if nobody is at the screen. On the event
    loop that would freeze the entire assistant, so it never runs there.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, fn, *args), timeout)
    except asyncio.TimeoutError:
        raise ToolError(
            "macOS is waiting for you to allow Jarvis access to that folder. "
            "Approve the prompt on screen, or grant it in System Settings, Privacy & Security, Files and Folders."
        ) from None


PROTECTED = {"Downloads", "Desktop", "Documents", "Pictures", "Movies", "Music"}


def protected_warning(folder: Path, found: int) -> str:
    """macOS can hand back an empty listing instead of an error.

    A protected folder that reports nothing is far more often a missing consent
    than a genuinely empty folder, and silently reporting "0 files" would be a
    confidently wrong answer. Say which it might be.
    """
    if found == 0 and folder.name in PROTECTED and folder.parent == Path.home():
        return (
            f" Note: {folder.name} appears empty, which usually means macOS has not "
            "granted Jarvis access to it yet rather than that it is really empty. "
            "Check System Settings, Privacy & Security, Files and Folders."
        )
    return ""


def resolve(path: str) -> Path:
    raw = (path or "").strip().strip("'\"")
    if not raw:
        raise ToolError("no path given")
    key = raw.lower().strip("/ ")
    if key in SHORTHAND:
        raw = SHORTHAND[key]
    elif key.startswith(tuple(f"{k}/" for k in SHORTHAND)):
        head, _, tail = key.partition("/")
        raw = f"{SHORTHAND[head]}/{tail}"
    elif not raw.startswith(("/", "~")):
        raw = f"~/{raw}"
    return Path(os.path.expanduser(raw)).resolve()


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def describe(p: Path) -> str:
    try:
        st = p.stat()
    except OSError:
        return p.name
    when = datetime.fromtimestamp(st.st_mtime).strftime("%-d %b")
    kind = "folder" if p.is_dir() else human_size(st.st_size)
    return f"{p.name} ({kind}, {when})"


@tool(
    name="find_files",
    description=(
        "Find files or folders on this Mac by name or content using Spotlight. Use for 'where is my tax pdf', "
        "'find the screenshots from yesterday', 'do I have a file called budget'."
    ),
    params={
        "query": {"type": "string", "description": "Words in the file name or contents, e.g. 'invoice march'"},
        "kind": {"type": "string", "enum": list(KIND_QUERY), "description": "Restrict to a file type"},
        "location": {"type": "string", "description": "Where to look, e.g. 'downloads', 'desktop', or a path. Defaults to the home folder."},
        "limit": {"type": "integer", "description": "How many results, default 10"},
    },
    required=["query"],
    category="files",
)
async def find_files(ctx: ToolContext, query: str, kind: str = "any", location: str = "", limit: int = 10) -> str:
    where = resolve(location) if location else Path.home()
    clauses = [f'kMDItemDisplayName == "*{query}*"c', f'kMDItemTextContent == "*{query}*"c']
    expr = f"({' || '.join(clauses)})"
    if KIND_QUERY.get(kind):
        expr = f"{expr} && {KIND_QUERY[kind]}"
    proc = await run(["/usr/bin/mdfind", "-onlyin", str(where), expr], timeout=20)
    paths = [Path(p) for p in proc.stdout.splitlines() if p.strip()]
    if not paths:
        return (f"Nothing found for '{query}' in {where.name or where}."
                + protected_warning(where, 0))
    # stat() on a protected folder can block on a consent prompt too.
    def _sorted():
        alive = [q for q in paths if q.exists()]
        alive.sort(key=lambda q: q.stat().st_mtime if q.exists() else 0, reverse=True)
        return alive
    paths = await off_loop(_sorted)
    shown = paths[: max(1, min(int(limit or 10), 25))]
    lines = [f"{i}. {describe(p)} - {p.parent}" for i, p in enumerate(shown, 1)]
    more = f" ({len(paths)} matches, showing {len(shown)})" if len(paths) > len(shown) else ""
    return f"Found {len(paths)} match(es){more}:\n" + "\n".join(lines)


def _confirm_file_action(args: dict) -> str | None:
    action = (args.get("action") or "").lower()
    path = args.get("path") or ""
    dest = args.get("dest") or ""
    name = Path(os.path.expanduser(path)).name or path
    if action == "trash":
        return f"Move {name} to the Trash?"
    if action in ("write", "append"):
        try:
            if resolve(path).exists() and action == "write":
                return f"Overwrite {name}?"
        except ToolError:
            return None
        return None
    if action in ("move", "copy"):
        try:
            if dest and resolve(dest).exists() and resolve(dest).is_file():
                return f"Replace {Path(dest).name}?"
        except ToolError:
            return None
    return None


@tool(
    name="file_action",
    description=(
        "Work with a specific file or folder: open it, read it, write it, move it, copy it, list a folder, "
        "or move it to the Trash. Use after find_files, or when the user names a path directly."
    ),
    params={
        "action": {"type": "string", "enum": ["open", "reveal", "read", "write", "append", "trash", "move", "copy", "mkdir", "info", "list"],
                   "description": "What to do"},
        "path": {"type": "string", "description": "The file or folder, e.g. '~/Desktop/notes.txt' or 'downloads'"},
        "dest": {"type": "string", "description": "Destination path for move and copy"},
        "content": {"type": "string", "description": "Text to write for write and append"},
    },
    required=["action", "path"],
    risk=_confirm_file_action,
    category="files",
)
async def file_action(ctx: ToolContext, action: str, path: str, dest: str = "", content: str = "") -> str:
    p = resolve(path)
    action = action.lower().strip()

    if action in ("open", "reveal"):
        if not p.exists():
            raise ToolError(f"{p} does not exist")
        args = ["-R", str(p)] if action == "reveal" else [str(p)]
        proc = await run(["/usr/bin/open", *args], timeout=15)
        proc.check("open")
        return f"{'Revealed' if action == 'reveal' else 'Opened'} {p.name}."

    if action == "read":
        if not p.is_file():
            raise ToolError(f"{p} is not a file")
        if p.suffix.lower() not in TEXT_SUFFIXES and p.stat().st_size > 200_000:
            raise ToolError(f"{p.name} is not a text file I can read out")
        try:
            text = await off_loop(lambda: p.read_text(errors="replace"))
        except (OSError, UnicodeDecodeError) as exc:
            raise ToolError(f"could not read {p.name}: {exc}") from exc
        return f"{p.name}:\n{truncate(text, 3000)}"

    if action in ("write", "append"):
        def _write():
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a" if action == "append" else "w") as fh:
                fh.write(content or "")
        await off_loop(_write)
        return f"{'Appended to' if action == 'append' else 'Wrote'} {p.name}."

    if action == "trash":
        if not p.exists():
            raise ToolError(f"{p} does not exist")
        await osascript(f'tell application "Finder" to delete POSIX file {applescript_quote(str(p))}')
        return f"Moved {p.name} to the Trash."

    if action in ("move", "copy"):
        if not p.exists():
            raise ToolError(f"{p} does not exist")
        if not dest:
            raise ToolError("a destination is required")
        d = resolve(dest)
        if d.is_dir():
            d = d / p.name
        d.parent.mkdir(parents=True, exist_ok=True)
        if action == "move":
            await off_loop(shutil.move, str(p), str(d), timeout=120)
        elif p.is_dir():
            await off_loop(lambda: shutil.copytree(str(p), str(d), dirs_exist_ok=True), timeout=300)
        else:
            await off_loop(shutil.copy2, str(p), str(d), timeout=120)
        return f"{'Moved' if action == 'move' else 'Copied'} {p.name} to {d.parent.name}."

    if action == "mkdir":
        p.mkdir(parents=True, exist_ok=True)
        return f"Created {p.name}."

    if action == "info":
        if not p.exists():
            raise ToolError(f"{p} does not exist")
        st = p.stat()
        return (f"{p.name}: {'folder' if p.is_dir() else human_size(st.st_size)}, "
                f"modified {datetime.fromtimestamp(st.st_mtime).strftime('%-d %B at %-I:%M %p')}, at {p.parent}")

    if action == "list":
        if not p.is_dir():
            raise ToolError(f"{p} is not a folder")
        entries = await off_loop(
            lambda: sorted(
                (e for e in p.iterdir() if not e.name.startswith(".")),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
        )
        if not entries:
            return f"{p.name} is empty." + protected_warning(p, 0)
        head = [describe(e) for e in entries[:25]]
        extra = f" and {len(entries) - 25} more" if len(entries) > 25 else ""
        return f"{p.name} contains {len(entries)} item(s): " + "; ".join(head) + extra

    raise ToolError(f"unknown action {action!r}")


def _confirm_organize(args: dict) -> str | None:
    if args.get("dry_run", True):
        return None
    folder = args.get("folder") or "that folder"
    return f"Sort the files in {folder} into subfolders?"


@tool(
    name="organize_files",
    description=(
        "Tidy a folder by sorting its loose files into subfolders, by file type or by month. "
        "Use for 'organize my downloads', 'clean up my desktop'. Preview first with dry_run true, "
        "then call again with dry_run false to actually move the files."
    ),
    params={
        "folder": {"type": "string", "description": "Folder to tidy, e.g. 'downloads' or 'desktop'"},
        "by": {"type": "string", "enum": ["type", "month"], "description": "Group by file type (default) or by month modified"},
        "dry_run": {"type": "boolean", "description": "True (default) previews the plan without moving anything"},
    },
    required=["folder"],
    risk=_confirm_organize,
    category="files",
)
async def organize_files(ctx: ToolContext, folder: str, by: str = "type", dry_run: bool = True) -> str:
    root = resolve(folder)
    if not root.is_dir():
        raise ToolError(f"{root} is not a folder")

    plan: dict[str, list[Path]] = defaultdict(list)
    for entry in await off_loop(lambda: list(root.iterdir())):
        if entry.name.startswith(".") or entry.is_dir():
            continue
        if by == "month":
            bucket = datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m %B")
        else:
            suffix = entry.suffix.lower()
            bucket = next((name for name, exts in CATEGORIES.items() if suffix in exts), "Other")
        plan[bucket].append(entry)

    if not plan:
        return f"{root.name} has no loose files to sort." + protected_warning(root, 0)

    summary = ", ".join(f"{len(v)} into {k}" for k, v in sorted(plan.items(), key=lambda kv: -len(kv[1])))
    total = sum(len(v) for v in plan.values())
    if dry_run:
        return (f"Plan for {root.name}: {total} files - {summary}. "
                f"Call organize_files again with dry_run false to do it.")

    moved = 0
    collisions = 0
    for bucket, entries in plan.items():
        target = root / bucket
        target.mkdir(exist_ok=True)
        for entry in entries:
            dest = target / entry.name
            if dest.exists():
                dest = target / f"{entry.stem}-{int(time.time())}{entry.suffix}"
                collisions += 1
            try:
                shutil.move(str(entry), str(dest))
                moved += 1
            except OSError:
                continue
    note = f" {collisions} renamed to avoid clashes." if collisions else ""
    return f"Sorted {moved} files in {root.name} into {len(plan)} folders.{note}"
