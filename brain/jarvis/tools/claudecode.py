"""Handing hard work to Claude Code.

The local 4B model is fast and free but shallow. Claude can genuinely do things:
write code, build a deck, work across many files, research properly. The point
of this tool is to make that hand-off CHEAP - Jarvis writes the brief itself, so
Claude starts with the context instead of paying tokens to rediscover it.
"""

from __future__ import annotations

from jarvis.sysutil import ToolError
from jarvis.tools import ToolContext, tool

# Harder work gets a stronger model; routine work should not burn credits on one.
COMPLEXITY = {
    "simple": {"model": "sonnet", "effort": "low"},
    "standard": {"model": "sonnet", "effort": "medium"},
    "hard": {"model": "opus", "effort": "high"},
}


def _available() -> bool:
    return True  # the handler reports the real reason if the CLI is missing


@tool(
    name="ask_claude",
    description=(
        "Hand a substantial task to Claude, which works in the background with full access to this Mac "
        "and reports back out loud when finished. Use for: writing or fixing code, making a document, "
        "spreadsheet or presentation, deep research across many sources, long multi-step web workflows, "
        "organising or analysing lots of files, and anything you have already tried and failed at. "
        "Do NOT use it for things you can do yourself in one or two tool calls."
    ),
    params={
        "task": {
            "type": "string",
            "description": "The complete task in one self-contained paragraph, including every detail the user gave (names, paths, dates, preferences). Claude cannot see this conversation.",
        },
        "context": {
            "type": "string",
            "description": "What you already know that saves Claude time: files or apps involved, what you already tried and what happened, relevant facts about the user.",
        },
        "done_when": {
            "type": "string",
            "description": "What finished looks like, e.g. 'a 10-slide deck saved to the Desktop'.",
        },
        "complexity": {
            "type": "string",
            "enum": ["simple", "standard", "hard"],
            "description": "simple = mechanical and well-defined; standard = normal; hard = needs real reasoning or a lot of steps. Pick the cheapest that will work.",
        },
    },
    required=["task"],
    ack="Right, I'll put Claude on that.",
    category="delegate",
    available=_available,
)
async def ask_claude(
    ctx: ToolContext,
    task: str,
    context: str = "",
    done_when: str = "",
    complexity: str = "standard",
) -> str:
    mode = ctx.cfg.get("claude.mode", "auto")
    if mode == "off":
        raise ToolError("Claude hand-off is switched off in settings, so handle it yourself or tell the user.")
    await ctx.jobs.require_claude()

    active = ctx.jobs.active()
    if len(active) >= int(ctx.cfg.get("claude.max_concurrent", 2)):
        raise ToolError(f"Claude is already working on {len(active)} task(s). Wait for one to finish.")

    profile = COMPLEXITY.get((complexity or "standard").lower(), COMPLEXITY["standard"])
    brief = _brief(ctx, task, context, done_when)

    job = await ctx.jobs.start_claude(
        brief,
        title=task.strip()[:70],
        model=ctx.cfg.get("claude.model") or profile["model"],
        effort=profile["effort"],
    )
    return "Claude's on it. I'll tell you when it's done."


def _brief(ctx: ToolContext, task: str, context: str, done_when: str) -> str:
    """Everything Claude needs, and nothing it would have to ask for."""
    user = ctx.cfg.get("user_name") or "the user"
    facts = [f.text for f in ctx.memory.search_facts(task, limit=4)]
    parts = [
        f"TASK (spoken aloud by {user} to their voice assistant, then handed to you):",
        task.strip(),
    ]
    if context.strip():
        parts += ["", "WHAT THE ASSISTANT ALREADY KNOWS:", context.strip()]
    if facts:
        parts += ["", "RELEVANT FACTS ABOUT THE USER:"] + [f"- {f}" for f in facts]
    if done_when.strip():
        parts += ["", "DONE WHEN:", done_when.strip()]
    parts += [
        "",
        "You are on their Mac with their permission. Work autonomously and do not ask questions - "
        "make the reasonable choice and mention it in your summary.",
    ]
    return "\n".join(parts)


@tool(
    name="claude_jobs",
    description=(
        "Check on or cancel the background tasks Claude is running. Use for 'what's Claude doing', "
        "'is it done yet', 'cancel that task'."
    ),
    params={
        "action": {"type": "string", "enum": ["list", "status", "cancel", "cancel_all"]},
        "which": {"type": "string", "description": "Job id, or 'last' for the most recent"},
    },
    required=["action"],
    category="delegate",
)
async def claude_jobs(ctx: ToolContext, action: str, which: str = "last") -> str:
    action = action.lower()
    if action in ("list", "status"):
        active = ctx.jobs.active()
        if action == "status" or not active:
            recent = ctx.jobs.recent(3)
            if not recent:
                return "Claude has not been given anything to do."
            lines = []
            for j in recent:
                if j.status == "running":
                    lines.append(f"{j.title}: running, {j.progress.lower()}")
                elif j.status == "done":
                    lines.append(f"{j.title}: finished. {j.result[:200]}")
                else:
                    lines.append(f"{j.title}: {j.status}.")
            return " ".join(lines)
        return " ".join(f"{j.title}: {j.progress.lower()}" for j in active)

    if action == "cancel":
        job = ctx.jobs.get(which or "last")
        if not job:
            return "No such job."
        ok = await ctx.jobs.cancel(job.id)
        return f"Cancelled {job.title}." if ok else f"{job.title} had already finished."

    if action == "cancel_all":
        n = await ctx.jobs.cancel_all()
        return f"Cancelled {n} task(s)." if n else "Nothing was running."

    raise ToolError(f"unknown action {action!r}")
