"""Entry point: python -m jarvis

Starts the assistant and the local server, then waits. Logs go to stdout (the
app captures them into ~/.jarvis/logs/brain.log) and to a rotating file when
run standalone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler

from jarvis.config import LOGS_DIR, Config, ensure_dirs
from jarvis.core import Assistant
from jarvis.server import Server


def setup_logging() -> None:
    ensure_dirs()
    level = os.environ.get("JARVIS_LOG_LEVEL", "INFO").upper()
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)-18s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    try:
        rotating = RotatingFileHandler(LOGS_DIR / "jarvis.log", maxBytes=2_000_000, backupCount=2)
        rotating.setFormatter(fmt)
        root.addHandler(rotating)
    except OSError:
        pass

    for noisy in ("httpx", "httpcore", "aiohttp.access", "urllib3", "filelock", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _loop_watchdog(limit: float = 30.0) -> None:
    """If the event loop is ever stuck for `limit` seconds, dump every thread's
    stack to logs/hang.log and exit, so the app restarts the brain.

    The design rule is that nothing blocks the loop; this enforces it when a bug
    breaks the rule. It uses a heartbeat on time.monotonic, which pauses while
    the Mac sleeps - a wall-clock timer would "detect a hang" on every wake.
    """
    import faulthandler
    import threading
    import time as _time

    beat = [_time.monotonic()]

    def watch() -> None:
        while True:
            _time.sleep(5)
            if _time.monotonic() - beat[0] > limit:
                with open(LOGS_DIR / "hang.log", "a") as sink:
                    sink.write(f"\n==== event loop stuck >{limit:.0f}s at {_time.ctime()} ====\n")
                    faulthandler.dump_traceback(file=sink, all_threads=True)
                os._exit(3)

    threading.Thread(target=watch, name="loop-watchdog", daemon=True).start()
    while True:
        beat[0] = _time.monotonic()
        await asyncio.sleep(2)


async def main() -> int:
    setup_logging()
    log = logging.getLogger("jarvis")

    cfg = Config()
    if os.environ.get("JARVIS_PORT"):
        cfg.set("server.port", int(os.environ["JARVIS_PORT"]))

    assistant = Assistant(cfg)
    server = Server(assistant)

    # Serve immediately so the app window can show status while models load.
    await server.start()
    await assistant.start()

    asyncio.create_task(_loop_watchdog())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with_suppress = getattr(loop, "add_signal_handler", None)
        if with_suppress:
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass

    log.info("jarvis is up - say '%s' or press the hotkey", cfg.get("listening.wake_word", "hey jarvis").replace("_", " "))
    try:
        await stop.wait()
    finally:
        log.info("stopping")
        await assistant.stop()
        await server.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
