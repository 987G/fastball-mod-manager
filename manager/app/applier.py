"""File mods: real install status, planning, and journaled install/uninstall runs."""
import json
import re
import subprocess
from pathlib import Path
from typing import Callable

from . import procs
from .config import Settings, write_json_atomic
from .mods import Mod, expand

TAIL_LINES = 20


class GameRunning(Exception):
    """mlb.exe is running; nothing was changed."""

    def __init__(self, games: list[dict]):
        self.games = games
        names = ", ".join(f"PID {g['pid']} ({g['exe'] or 'path unknown'})" for g in games)
        super().__init__(f"the game is running, close it first: {names}")


class StepFailed(Exception):
    """A file mod's install/uninstall/status script exited non-zero or could not start."""

    def __init__(self, mod_id: str, tail: str, what: str = "failed"):
        self.mod_id = mod_id
        self.tail = tail
        super().__init__(procs.failure_text(f"mod {mod_id!r} {what}", tail, TAIL_LINES))


def _run(mod: Mod, key: str, settings: Settings, log: Callable[[str], None] | None = None):
    """Run files[key] in files.cwd; return (exit code, combined stdout+stderr).
    A script that cannot start (missing cwd or executable) raises StepFailed."""
    argv = procs.python_argv(expand(mod.files[key], mod, settings))
    cwd = expand(mod.files["cwd"], mod, settings)
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as err:
        raise StepFailed(mod.id, f"cannot start {key} {argv} in {cwd}: {err}") from err
    lines = []
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        lines.append(line)
        if log:
            log(line)
    proc.stdout.close()
    return proc.wait(), "\n".join(lines)


def _tail(output: str) -> str:
    return "\n".join(output.splitlines()[-TAIL_LINES:])


def is_installed(mod: Mod, settings: Settings) -> bool:
    """The status script's answer; StepFailed if it fails (its output is then no answer)."""
    code, output = _run(mod, "status", settings)
    if code != 0:
        raise StepFailed(mod.id, _tail(output), what=f"status check failed (exit {code})")
    return re.search(mod.files["installed_when"], output) is not None


def plan(selected: set[str], all_mods: list[Mod], settings: Settings) -> list[tuple[str, str]]:
    """Steps that bring every file mod's real state in line with `selected`.
    A mod whose last step was interrupted or failed (journal) gets a step toward the
    selection whatever its status says: the status reads one marker, the scripts change
    several files, so a half-done step can look done. Journal ids that are no file mod
    are ignored."""
    redo = {mod_id for _, mod_id in pending_from_journal(settings)}
    uninstalls, installs = [], []
    for mod in all_mods:
        if not mod.files:
            continue
        if mod.id in redo:
            if mod.id in selected:
                installs.append(("install", mod.id))
            else:
                uninstalls.append(("uninstall", mod.id))
            continue
        installed = is_installed(mod, settings)
        if mod.id in selected and not installed:
            installs.append(("install", mod.id))
        elif mod.id not in selected and installed:
            uninstalls.append(("uninstall", mod.id))
    return uninstalls + installs


def _journal_path(settings: Settings) -> Path:
    return Path(settings.state) / "journal.json"


def _write_journal(settings: Settings, entries: list[dict]) -> None:
    write_json_atomic(_journal_path(settings), entries)


def apply(steps: list[tuple[str, str]], all_mods: list[Mod], settings: Settings,
          log: Callable[[str], None]) -> None:
    """Run each step's script in order, journaling it before and after.
    Refuses (GameRunning) before touching anything while mlb.exe runs and there are
    steps; with no steps it changes nothing (not even the journal) and never refuses.
    Stops at the first failing step (StepFailed). Steps an earlier apply left
    unfinished or failed stay in the journal until this apply has run a step for that mod."""
    if not steps:
        return
    games = procs.running_games()
    if games:
        raise GameRunning(games)
    by_id = {m.id: m for m in all_mods}
    carried = [e for e in _pending_entries(settings)
               if e["step"][1] in by_id and by_id[e["step"][1]].files]
    entries = list(carried)
    for action, mod_id in steps:
        mod = by_id[mod_id]
        entries = [e for e in entries
                   if not (e["step"][1] == mod_id and any(e is c for c in carried))]
        entry = {"step": [action, mod_id], "status": "running", "output": ""}
        entries.append(entry)
        _write_journal(settings, entries)
        try:
            code, output = _run(mod, action, settings, log)
        except StepFailed as err:
            entry["output"] = err.tail
            entry["status"] = "failed"
            _write_journal(settings, entries)
            raise
        entry["output"] = output
        entry["status"] = "done" if code == 0 else "failed"
        _write_journal(settings, entries)
        if code != 0:
            raise StepFailed(mod_id, _tail(output))


def _pending_entries(settings: Settings) -> list[dict]:
    """Journal entries still marked running or failed, with their status and output."""
    try:
        entries = json.loads(_journal_path(settings).read_text(encoding="utf-8"))
        pending = [{"step": [str(e["step"][0]), str(e["step"][1])], "status": e["status"],
                    "output": e.get("output", "")}
                   for e in entries if e.get("status") in ("running", "failed")]
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return []
    return pending


def pending_from_journal(settings: Settings) -> list[tuple[str, str]]:
    """Steps that did not finish: the last apply started them but never finished
    (crash, kill, power loss), or they failed. Play redoes each such mod."""
    return [tuple(e["step"]) for e in _pending_entries(settings)]
