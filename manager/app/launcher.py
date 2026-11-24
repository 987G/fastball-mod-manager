"""Play: bring the install in line with the selection, build if needed, start the
mods' local servers, launch the game with the right env, clean up when it exits."""
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import applier, builder, config, mods, patchsets, procs, profiles
from .config import Settings

LOG_LINES = 500
SERVER_READY_TIMEOUT = 20.0
STOP_TIMEOUT = 5.0
BUSY = {"applying", "building", "starting", "running",
        "downloading", "removing"}  # the last two: Get mods (server.App.get_mods, remove_mod)


class Session:
    """Status of the current/last Play, shared between the Play thread and the UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self.status = "idle"
        self.message = ""
        self.log: list[str] = []
        self.log_file: Path | None = None
        self.servers: list[subprocess.Popen] = []  # mod servers of the current Play

    def set(self, status: str, message: str = "") -> None:
        with self._lock:
            self.status, self.message = status, message
        self.add(f"[{status}] {message}" if message else f"[{status}]")

    def add(self, line: str) -> None:
        with self._lock:
            self.log.append(line)
            del self.log[:-LOG_LINES]
            if self.log_file is not None:
                try:
                    self.log_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.log_file, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    pass

    def claim(self) -> bool:
        """Mark the session as applying unless a Play is already under way."""
        with self._lock:
            if self.status in BUSY:
                self.message = f"Play is already under way ({self.status})."
                return False
            self.status, self.message = "applying", ""
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self.status, "message": self.message, "log": list(self.log)}


class _Stop(Exception):
    """Ends a Play with status error and this message."""


def _norm(value: str) -> str:
    return os.path.normpath(value) if ("/" in value or "\\" in value) else value


def _same_file(a, b) -> bool:
    """Whether two paths name one file: the file system's answer when both exist (8.3
    names, trailing dots, links), else the resolved paths, else the spelled ones."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        pass
    try:
        return (os.path.normcase(str(Path(a).resolve()))
                == os.path.normcase(str(Path(b).resolve())))
    except (OSError, ValueError, RuntimeError):
        return (os.path.normcase(os.path.normpath(str(a)))
                == os.path.normcase(os.path.normpath(str(b))))


def _game_text(game: dict) -> str:
    return f"PID {game['pid']} ({game['exe'] or 'path unknown'})"


MAX_BESIDE = 3  # test copies Play may start next to: the user's game may be the 4th copy (the user, 2026-11-23)


def play_gate(games: list[dict], settings: Settings) -> tuple[str | None, list[dict]]:
    """(block, beside) for the running games: block is why Play cannot start at all;
    beside are the test copies (mlb.exe outside the install and the combined build), up
    to MAX_BESIDE, that Play may start next to as long as no mod files need changing.
    Never more copies than that, never two copies of the user's own game, and a copy
    whose path is unknown blocks."""
    if not games:
        return None, []
    if len(games) > MAX_BESIDE:
        return (f"{len(games)} copies of the game are running; close all but {MAX_BESIDE} first: "
                + ", ".join(_game_text(g) for g in games)), []
    own = (Path(settings.game) / "mlb.exe", Path(settings.build_out) / "mlb.exe")
    if any(not g["exe"] or any(_same_file(g["exe"], exe) for exe in own) for g in games):
        return str(applier.GameRunning(games)), []
    return None, list(games)


def _copies_text(games: list[dict], start: bool = False) -> str:
    """"a test copy (PID ...)" / "2 test copies (PID ..., PID ...)", capitalised to start a sentence."""
    head = ("A test copy" if start else "a test copy") if len(games) == 1 else f"{len(games)} test copies"
    return f"{head} ({', '.join(_game_text(g) for g in games)})"


def play_note(games: list[dict]) -> str | None:
    """The note shown beside an enabled Play while test copies run."""
    if not games:
        return None
    return (f"{_copies_text(games, start=True)} {'is' if len(games) == 1 else 'are'} running; "
            f"Play works if no mod files need changing.")


def uses_combined_exe(selected: set[str], all_mods: list, settings: Settings) -> bool:
    """Whether Play runs the combined exe: a selected mod has code in it (a runtime
    branch or code patches), or the setting says always. A switch-only runtime (env_on /
    env_off, no branch) switches code the port has built in: the install's exe has it too."""
    return settings.always_combined_exe or any(
        mod.id in selected and ((mod.runtime and mod.runtime.get("branch")) or mod.patches)
        for mod in all_mods)


def build_branches(selected, all_mods: list,
                   settings: Settings | None = None) -> tuple[list[str], frozenset]:
    """(branches, optional) for builder.build: every mod's runtime branch in mod order
    (each once), and those that belong only to mods that are off (the build may leave
    such a branch out when it will not merge). None in player mode: the release's
    prebuilt runtime holds the mods' code."""
    if settings is not None and config.mode(settings) == "player":
        return [], frozenset()
    branches, required = [], set()
    for mod in all_mods:
        branch = mod.runtime.get("branch") if mod.runtime else None
        if not branch:
            continue
        if branch not in branches:
            branches.append(branch)
        if mod.id in selected:
            required.add(branch)
    return branches, frozenset(b for b in branches if b not in required)


def build_needed(build_state: dict, selected, all_mods: list) -> bool:
    """Whether Play must (re)build: the exe is not up to date, or a selected mod's branch
    was left out of it."""
    if not build_state.get("up_to_date"):
        return True
    branches, optional = build_branches(selected, all_mods)
    return any(b in branches and b not in optional for b in build_state.get("left_out") or {})


def exe_for(selected: set[str], all_mods: list, settings: Settings) -> Path:
    """The exe Play launches for this selection."""
    if uses_combined_exe(selected, all_mods, settings):
        return Path(settings.build_out) / "mlb.exe"
    return Path(settings.game) / "mlb.exe"


def launch_env(selected: set[str], all_mods: list, settings: Settings, profile) -> dict[str, str]:
    """The game's environment: the install's paths, every code patch set's switch ("1"
    when its mod is selected, else "0"), then the manifests' env_on / env_off, which win."""
    env = dict(os.environ)
    env["MLB_EXEFS"] = str(Path(settings.game) / "game" / "exefs")
    env["MLB_ROMFS"] = str(Path(settings.game) / "game" / "romfs")
    env["MLB_PROFILE"] = str(profile)
    for mod in all_mods:
        if not mod.patches:
            continue
        for _, switch, _ in patchsets.read_sets(patchsets.read_text(mod)):
            env[switch] = "1" if mod.id in selected else "0"
    for mod in all_mods:
        if not mod.runtime:
            continue
        values = mod.runtime.get("env_on" if mod.id in selected else "env_off") or {}
        for key, value in mods.expand(values, mod, settings).items():
            env[key] = _norm(str(value))
    return env


def _game_argv(exe: Path) -> list[str]:
    return [str(exe)]


def _spawn_server(argv: list[str], cwd: str) -> subprocess.Popen:
    return subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW)


def _start_server(mod, settings: Settings, session: Session) -> subprocess.Popen:
    owner = procs.port_owner(settings.packages_port)
    if owner:
        raise _Stop(f"Port {settings.packages_port} is in use by PID {owner}; "
                    f"close that process, then Play again.")
    state = Path(settings.state)
    (state / "logs").mkdir(parents=True, exist_ok=True)
    (state / "profiles" / mod.id).mkdir(parents=True, exist_ok=True)
    cwd = mods.expand(mod.server["cwd"], mod, settings)
    argv = procs.python_argv(mods.expand(mod.server["cmd"], mod, settings))
    ready_text = mod.server["ready"]
    session.add(f"Starting {mod.id} server: {argv}")
    try:
        proc = _spawn_server(argv, cwd)
    except OSError as err:
        raise _Stop(f"Cannot start the {mod.id} server: {err}") from err
    ready = threading.Event()

    def read():
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            session.add("[server] " + line)
            if ready_text in line:
                ready.set()
        proc.stdout.close()

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + SERVER_READY_TIMEOUT
    while not ready.wait(0.1):
        if proc.poll() is not None:
            reason = f"it exited with code {proc.returncode}"
        elif time.monotonic() >= deadline:
            reason = f"no {ready_text!r} within {SERVER_READY_TIMEOUT:g} s"
        else:
            continue
        _stop_server(proc, session)
        raise _Stop(f"The {mod.id} server did not start ({reason}); see the log.")
    return proc


def _stop_server(proc: subprocess.Popen, session: Session) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    session.add(f"Server PID {proc.pid} stopped (exit code {proc.returncode}).")


def _run_game(exe: Path, env: dict, settings: Settings, session: Session) -> int:
    logs = Path(settings.state) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "game-stdout.log", "w", encoding="utf-8", errors="replace") as out:
        try:
            game = subprocess.Popen(_game_argv(exe), cwd=str(Path(exe).parent), env=env,
                                    stdout=out, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as err:
            raise _Stop(f"Cannot launch {exe}: {err}") from err
        session.set("running", f"The game is running (PID {game.pid}).")
        return game.wait()


def _play(selected: set[str], settings: Settings, session: Session, servers: list,
          on_applied: Callable[[], None] | None = None) -> None:
    block, beside = play_gate(procs.running_games(), settings)
    if block:
        raise _Stop(block)
    all_mods = mods.load_all(settings)
    session.set("applying", "Installing and removing file mods ...")
    steps = applier.plan(selected, all_mods, settings)
    if beside and steps:
        one = len(beside) == 1
        raise _Stop(f"{_copies_text(beside, start=True)} of the game {'is' if one else 'are'} running "
                    f"and mod files need changing; close {'it' if one else 'them'} first, then Play again.")
    if beside:
        session.add(f"No mod files need changing; playing beside {_copies_text(beside)}.")
    try:
        applier.apply(steps, all_mods, settings, session.add)
    finally:
        if on_applied is not None:  # the files may have changed, even on a failed step
            on_applied()

    exe = exe_for(selected, all_mods, settings)
    if uses_combined_exe(selected, all_mods, settings):
        if build_needed(builder.state(settings), selected, all_mods):
            session.set("building", "Building the combined game exe ...")
            branches, optional = build_branches(selected, all_mods, settings)
            builder.build(branches, settings, session.add, optional=optional)
        builder.stage_runtime(settings, session.add)

    profile = profiles.for_selection(selected, all_mods, settings)
    profiles.ensure(profile, settings, session.add)

    session.set("starting", "Starting mod servers ...")
    for mod in all_mods:
        if mod.id in selected and mod.server:
            servers.append(_start_server(mod, settings, session))

    # Again just before launch: a build can take minutes, and a game started meanwhile
    # (one copy too many, or the user's own) must not get another copy beside it.
    block, _ = play_gate(procs.running_games(), settings)
    if block:
        raise _Stop(block)

    env = launch_env(selected, all_mods, settings, profile)
    session.add(f"Launching {exe} with profile {profile}")
    code = _run_game(exe, env, settings, session)
    session.add(f"The game exited with code {code}.")


def play(selected: set[str], settings: Settings, session: Session,
         on_applied: Callable[[], None] | None = None) -> None:
    """Run a whole Play synchronously. Never raises: failures end in status error.
    on_applied() is called right after the file mods were applied (or failed to)."""
    session.log_file = Path(settings.state) / "logs" / "manager.log"
    servers: list[subprocess.Popen] = []
    session.servers = servers
    error = None
    try:
        _play(set(selected), settings, session, servers, on_applied)
    except _Stop as err:
        error = str(err)
    except (applier.GameRunning, applier.StepFailed, builder.BuildFailed) as err:
        error = str(err)
    except Exception as err:  # OSError, FileNotFoundError, timeouts, bad manifests
        error = f"{type(err).__name__}: {err}"
    finally:
        for proc in servers:
            try:
                _stop_server(proc, session)
            except OSError as err:
                session.add(f"Could not stop server PID {proc.pid}: {err}")
    if error:
        lines = error.strip().splitlines() or ["Play failed."]
        for line in lines[1:]:
            session.add(line)
        session.set("error", lines[0])
    else:
        session.set("idle", "The game has exited.")


def shutdown(session: Session) -> None:
    """Stop the mod servers the current Play started (manager exit: none may outlive it)."""
    for proc in list(session.servers):
        try:
            _stop_server(proc, session)
        except OSError as err:
            session.add(f"Could not stop server PID {proc.pid}: {err}")


def start(selected: set[str], settings: Settings, session: Session,
          on_applied: Callable[[], None] | None = None) -> threading.Thread | None:
    """Run play() in a daemon thread; None (session untouched but its message) if busy."""
    if not session.claim():
        return None
    thread = threading.Thread(target=play, args=(set(selected), settings, session, on_applied),
                              daemon=True)
    thread.start()
    return thread
