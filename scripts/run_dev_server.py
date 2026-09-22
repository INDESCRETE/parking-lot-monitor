#!/usr/bin/env python3
"""Dev convenience: restarts the server automatically whenever a .py file
under app/ or scripts/ changes, so you don't have to Ctrl+C and retype the
run command after every code update.

Just for iterating locally -- not meant to replace how the real server gets
started/managed. Uses plain polling (checks file timestamps once a second)
instead of adding a new pip dependency (e.g. the `watchdog` package), to
keep this dependency-free like the rest of the project.

Usage (instead of `python3 -m app.server`):
    python3 scripts/run_dev_server.py

Ctrl+C stops it, same as the normal server.

Known limitation: a restart sends the old server process a plain terminate
signal. That's enough to stop the web server and its background threads
cleanly, but if an ffmpeg live-view stream happened to be running at that
exact moment, that one ffmpeg process can be left running rather than
cleanly killed. Rare in practice (it only runs while someone has a live
view open), and it'll exit on its own once its stall-watchdog notices
nobody's reading from it -- just something to know about if you're
restarting a lot while testing Live View specifically.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCH_DIRS = [ROOT / "app", ROOT / "scripts"]
POLL_SECONDS = 1.0


def snapshot() -> dict[Path, float]:
    mtimes: dict[Path, float] = {}
    for directory in WATCH_DIRS:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            try:
                mtimes[path] = path.stat().st_mtime
            except OSError:
                pass  # file disappeared between listing and stat-ing it; ignore
    return mtimes


def start_server() -> subprocess.Popen:
    print("Starting server...", flush=True)
    return subprocess.Popen([sys.executable, "-m", "app.server"], cwd=ROOT)


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def describe_changes(before: dict[Path, float], after: dict[Path, float]) -> str:
    changed_paths = {p for p in after if after.get(p) != before.get(p)}
    changed_paths |= set(before) - set(after)  # files that were deleted
    names = sorted(str(p.relative_to(ROOT)) for p in changed_paths)
    return ", ".join(names) if names else "a watched file"


def main() -> None:
    print(f"Watching for .py changes under: {', '.join(str(d.relative_to(ROOT)) for d in WATCH_DIRS)}")
    proc = start_server()
    last_snapshot = snapshot()
    try:
        while True:
            time.sleep(POLL_SECONDS)
            current = snapshot()
            if current != last_snapshot:
                print(f"Change detected ({describe_changes(last_snapshot, current)}), restarting server...", flush=True)
                stop_server(proc)
                proc = start_server()
                last_snapshot = current
            elif proc.poll() is not None:
                # The server exited on its own (crash, or something else
                # killed it) rather than us restarting it -- don't spin
                # trying to restart a broken process forever.
                print(f"Server process exited on its own (code {proc.returncode}). Stopping the watcher.", flush=True)
                return
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
        stop_server(proc)


if __name__ == "__main__":
    main()
