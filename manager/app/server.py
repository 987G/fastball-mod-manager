"""The manager's local HTTP API (127.0.0.1 only), the game art and the web page."""
import json
import os
import re
import socket
import threading
import time
from dataclasses import fields, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import actions, applier, builder, config, launcher, mods, procs, profiles, releases
from . import features as feature_list
from .config import Settings

HOST = "127.0.0.1"
WEB = config.MANAGER / "web"
ART_NAME = re.compile(r"^[A-Z0-9_]+$")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".png": "image/png",
                 ".svg": "image/svg+xml", ".webp": "image/webp"}
LOGS = {"server": "packages-server.log", "build": "build.log", "manager": "manager.log"}
ART_FILE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


PING = {"app": "show-mod-manager"}  # GET /api/ping: a second start knows the port is ours


def art_url(mod) -> str | None:
    """Where the page gets a mod's picture: its own image file, or a game art name from CROSS/LIVE."""
    if mod.art_file:
        return f"/modart/{mod.id}"
    if isinstance(mod.art, str) and ART_NAME.match(mod.art):
        return f"/art/{mod.art}"
    return None
MAX_LOG_LINES = 2000
PARTS = ("files", "runtime", "patches", "server", "profile")  # a release mod's parts, in chip order
BUSY_WORDS = {"applying": "mod files are being installed", "building": "the game is being built",
              "starting": "the game is starting", "running": "the game is running",
              "downloading": "mods are being downloaded", "removing": "a mod is being removed"}
MAX_BODY = 64 * 1024
DRAIN_TIMEOUT = 5.0  # seconds to wait for a refused request's body
GAMES_TTL = 2.0
NEWER_EVERY = 3600.0  # seconds: the check for a newer release on GitHub runs at most this often
NEWER_RETRY = 300.0  # seconds: how soon it may run again after a check that failed (offline)


class BadRequest(Exception):
    """A request the client got wrong: answered with `code` (400) and this message."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def tail(path: Path, count: int) -> list[str]:
    """The last `count` lines of a text file, reading backwards from its end."""
    try:
        f = open(path, "rb")
    except OSError:
        return []
    with f:
        f.seek(0, os.SEEK_END)
        pos, data = f.tell(), b""
        while pos > 0 and data.count(b"\n") <= count:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-count:]


def validate_settings(changes, current: Settings) -> Settings:
    """`current` with the keys of a partial settings object applied; BadRequest if invalid."""
    if not isinstance(changes, dict):
        raise BadRequest("settings must be a JSON object")
    values = {}
    for key, value in changes.items():
        try:
            values[key] = config.check_value(key, value)
        except ValueError as err:
            raise BadRequest(str(err)) from None
    return replace(current, **values)


def settings_json(settings: Settings) -> dict:
    return {f.name: (str(v) if isinstance(v := getattr(settings, f.name), Path) else v)
            for f in fields(Settings)}


def _and(names: list[str]) -> str:
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)


def catalog_mod(entry: dict, records: dict, here: dict, on: set, release: str) -> dict:
    """One mod of a release's index as the picker shows it. The index comes from the network:
    only checked text, sizes and shapes reach the page (it draws them as text, never HTML).
    `here` maps the mods on this PC to their own versions (mod.json, or None). The entry's
    version is the mod's own (tools/package.py) or, for a mod without one and in older releases,
    the release tag: own_version is it only in the first case; local_version is the installed
    mod's own version."""
    mod_id = entry["id"]
    art, parts, sections = entry.get("art"), entry.get("parts"), entry.get("features")
    try:
        card = mods.clean_card_text(entry.get("card"))
    except ValueError:
        card = None
    try:
        version = mods.clean_version(entry.get("version"))
    except ValueError:
        version = ""
    installed = mod_id in records and mod_id in here
    return {"id": mod_id, "name": entry["name"],
            "version": version, "own_version": version if version not in ("", release) else None,
            "local_version": here[mod_id] if installed else None,
            "description": (entry["description"] if isinstance(entry.get("description"), str)
                            else ""),
            "art_url": f"/art/{art}" if isinstance(art, str) and ART_NAME.match(art) else None,
            "size": entry["size"],
            "parts": [p for p in PARTS if p in parts] if isinstance(parts, list) else [],
            "card_text": card,
            "features": sections if sections and feature_list.valid(sections) else None,
            "installed": installed, "installed_release": records[mod_id] if installed else None,
            "update": installed and records[mod_id] != release,
            "on": installed and mod_id in on}


class _Stopped(Exception):
    """Ends a Get mods or Remove with status error: its first line is the status line, the
    rest goes to the log."""


PLAIN_ERRORS = (releases.ReleaseError, applier.GameRunning, applier.StepFailed, _Stopped)


class SelectionError(Exception):
    """selection.json exists but cannot be read: every mod counts as off, Play is refused."""


class App:
    """Everything the handlers share: settings, the Play/build session, caches."""

    def __init__(self, settings: Settings, settings_path: Path, web: Path = WEB):
        self.settings = settings
        self.settings_path = Path(settings_path)
        self.web = Path(web)
        self.session = launcher.Session()
        self._lock = threading.Lock()
        self._installed: dict[str, bool | None] = {}
        self._status_errors: dict[str, str] = {}  # mod id -> why its status check failed
        self._refreshes = 0  # installed-status refreshes started
        self._shown = 0  # the refresh whose answers are cached
        self._games: tuple[float, list] = (0.0, [])
        self._newer: dict | None = None  # the last check's newer release (/api/state)
        self._newer_at: float | None = None  # when that check started (time.monotonic)
        self._newer_wait = NEWER_EVERY  # seconds before the next check: NEWER_RETRY after a failure
        self.httpd: ThreadingHTTPServer | None = None  # set by make_server
        self.quit = threading.Event()  # set by POST /api/quit; main() then exits

    # selection -------------------------------------------------------------
    def _selection_path(self) -> Path:
        return Path(self.settings.state) / "selection.json"

    def selected(self, all_mods) -> list[str]:
        """Selected mod ids in mod order; no selection file yet means every mod.
        SelectionError when the file exists but is unreadable or not a list of ids."""
        path = self._selection_path()
        try:
            ids = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [m.id for m in all_mods]
        except (OSError, ValueError) as err:
            raise SelectionError(self._selection_problem(f"cannot read it ({err})")) from err
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise SelectionError(self._selection_problem("it is not a list of mod ids"))
        chosen = set(ids)
        return [m.id for m in all_mods if m.id in chosen]

    def _selection_problem(self, why: str) -> str:
        return (f"The mod selection {self._selection_path()} is broken: {why}. Every mod "
                "counts as off and Play is refused until you switch a mod on or off.")

    def _selected_or_none(self, all_mods) -> list[str]:
        """selected(), with an unreadable selection read as no mods (for display only)."""
        try:
            return self.selected(all_mods)
        except SelectionError:
            return []

    def select(self, body) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) \
                or type(body.get("on")) is not bool:
            raise BadRequest('expected {"id": str, "on": bool}')
        all_mods = mods.load_all(self.settings)
        if body["id"] not in {m.id for m in all_mods}:
            raise BadRequest(f"unknown mod {body['id']!r}")
        with self._lock:
            chosen = set(self._selected_or_none(all_mods))  # a broken file starts from none
            (chosen.add if body["on"] else chosen.discard)(body["id"])
            ids = [m.id for m in all_mods if m.id in chosen]
            config.write_json_atomic(self._selection_path(), ids)
        return {"selected": ids}

    # installed-status cache ------------------------------------------------
    def refresh_installed(self) -> None:
        """Run every mod's status script (slow) and cache the answers, unless a
        refresh started later has already cached newer ones."""
        with self._lock:
            self._refreshes += 1
            generation = self._refreshes
        try:
            all_mods = mods.load_all(self.settings)
        except (mods.ManifestError, OSError):
            return
        result, errors = {}, {}
        for mod in all_mods:
            if not mod.files:
                result[mod.id] = None
                continue
            try:
                result[mod.id] = applier.is_installed(mod, self.settings)
            except Exception as err:  # a status script that fails or cannot run: unknown
                result[mod.id] = None
                errors[mod.id] = str(err)
        with self._lock:
            if generation > self._shown:
                self._shown = generation
                self._installed, self._status_errors = result, errors

    def refresh_in_background(self) -> None:
        threading.Thread(target=self.refresh_installed, daemon=True).start()

    # a newer release on GitHub ------------------------------------------------
    def check_newer(self) -> None:
        """Ask GitHub whether a newer release is out (releases.newer_release) and keep the answer
        for /api/state. After a good check the next one waits NEWER_EVERY seconds; after a failed
        one (offline) only NEWER_RETRY, and the last good answer stays. A dev copy never asks and
        shows nothing: no release.json beside the manager, a Fastball source clone in Settings,
        or a Release source of its own."""
        settings = self.settings
        try:
            info = releases.release_info()
        except releases.ReleaseError:
            info = None
        if info is None or config.mode(settings) == "dev" or settings.release_source:
            with self._lock:
                self._newer = None
            return
        source = self._newer_source(settings)
        with self._lock:
            now = time.monotonic()
            if self._newer_source(self.settings) != source or (
                    self._newer_at is not None and now - self._newer_at < self._newer_wait):
                return
            self._newer_at, self._newer_wait = now, NEWER_EVERY  # stamped first: one check at a time
        failed = object()  # newer_release answers `last` when the check fails
        newer = releases.newer_release(settings, info, last=failed)
        with self._lock:
            if self._newer_source(self.settings) != source:
                # Release source or the Fastball clone changed while it ran: its answer is stale,
                # and update_settings already cleared the line and the stamp
                return
            if newer is failed:
                self._newer_wait = NEWER_RETRY
            else:
                self._newer = newer

    @staticmethod
    def _newer_source(settings: Settings) -> tuple:
        """What a newer-release answer depends on in Settings: a change of any other setting (a
        port, the game folder) leaves a check under way valid."""
        return settings.release_source, settings.recomp, config.mode(settings)

    def check_newer_in_background(self) -> None:
        threading.Thread(target=self.check_newer, daemon=True).start()

    def _games_running(self) -> list:
        stamp, games = self._games
        if time.monotonic() - stamp > GAMES_TTL:
            games = procs.running_games()
            self._games = (time.monotonic(), games)
        return games

    # endpoints -------------------------------------------------------------
    def state(self, refresh: bool) -> dict:
        if refresh:
            self.refresh_installed()
        session = self.session.snapshot()
        out = {"session": {"status": session["status"], "message": session["message"],
                           "last_line": session["log"][-1] if session["log"] else None},
               "games_running": self._games_running(), "mode": config.mode(self.settings),
               "newer_release": self._newer}
        block, beside = launcher.play_gate(out["games_running"], self.settings)
        out["play_block"], out["play_note"] = block, launcher.play_note(beside)
        try:
            all_mods = mods.load_all(self.settings)
        except (mods.ManifestError, OSError) as err:
            return dict(out, mods=[], selected=[], build=None, profile=None, error=str(err))
        try:
            selected = self.selected(all_mods)
        except SelectionError as err:
            selected, out["error"] = [], str(err)
        # Skip the journal while a Play/build is under way: a read could collide with
        # its atomic write (Windows), and an in-progress step reads as interrupted.
        redo = (set() if session["status"] in launcher.BUSY else
                {mod_id for _, mod_id in applier.pending_from_journal(self.settings)})
        out["mods"] = [{"id": m.id, "name": m.name, "description": m.description, "art": m.art,
                        "on": m.id in selected, "installed": self._installed.get(m.id),
                        "status_error": self._status_errors.get(m.id),
                        "redo": bool(m.files) and m.id in redo,
                        "has_files": bool(m.files), "has_runtime": bool(m.runtime),
                        "branch": (m.runtime or {}).get("branch") or None,
                        "has_server": bool(m.server), "own_profile": m.profile == "own",
                        "has_patches": bool(m.patches), "art_url": art_url(m),
                        "card_url": f"/modcard/{m.id}" if m.card else None,
                        "card_text": m.card_text, "features": m.features, "version": m.version}
                       for m in all_mods]
        out["selected"] = selected
        try:
            out["build"] = builder.state(self.settings)
        except builder.BuildFailed as err:
            out["build"] = {"error": str(err)}
        out["profile"] = str(profiles.for_selection(set(selected), all_mods, self.settings))
        return out

    def play(self) -> tuple[int, dict]:
        try:
            selected = set(self.selected(mods.load_all(self.settings)))
        except SelectionError as err:
            return HTTPStatus.CONFLICT, {"error": str(err)}
        thread = launcher.start(selected, self.settings, self.session,
                                on_applied=self.refresh_in_background)
        if thread is None:
            return HTTPStatus.CONFLICT, {"error": self.session.message}
        snap = self.session.snapshot()
        return HTTPStatus.ACCEPTED, {"status": snap["status"], "message": snap["message"]}

    def build(self) -> tuple[int, dict]:
        settings = self.settings
        all_mods = mods.load_all(settings)
        branches, optional = launcher.build_branches(set(self._selected_or_none(all_mods)),
                                                     all_mods, settings)
        if not self.session.claim():
            return HTTPStatus.CONFLICT, {"error": self.session.message}
        self.session.log_file = Path(settings.state) / "logs" / "manager.log"
        self.session.set("building", "Building the combined game exe ...")

        def run():
            try:
                builder.build(branches, settings, self.session.add, optional=optional)
            except Exception as err:  # BuildFailed, OSError, ...
                lines = str(err).strip().splitlines() or [type(err).__name__]
                for line in lines[1:]:
                    self.session.add(line)
                self.session.set("error", lines[0])
            else:
                self.session.set("idle", "The combined game exe is built.")

        threading.Thread(target=run, daemon=True).start()
        snap = self.session.snapshot()
        return HTTPStatus.ACCEPTED, {"status": snap["status"], "message": snap["message"]}

    def _busy(self, what: str) -> tuple[int, dict] | None:
        """A 409 answer for `what` while a Play/build is under way, else None."""
        status = self.session.status
        if status in launcher.BUSY:
            return HTTPStatus.CONFLICT, {"error": f"Cannot {what} while {status}; wait for it "
                                                  "to finish."}
        return None

    # release mods: the picker's list, Get mods, Remove ------------------------------
    def catalog(self) -> tuple[int, dict]:
        """The mods of the release this manager downloads from, and which of them are here.
        Opening GET MODS also looks for a newer release (at most once an hour)."""
        self.check_newer_in_background()
        settings = self.settings
        try:
            index = releases.fetch_index(settings, releases.release_info())
        except releases.ReleaseError as err:
            return HTTPStatus.BAD_GATEWAY, {"error": str(err)}
        try:
            records = releases.installed(settings)
        except releases.ReleaseError as err:
            return HTTPStatus.CONFLICT, {"error": str(err)}
        try:
            all_mods = mods.load_all(settings)
        except (mods.ManifestError, OSError):
            all_mods = []
        here, on = {m.id: m.version for m in all_mods}, set(self._selected_or_none(all_mods))
        release = index["release"]
        return HTTPStatus.OK, {"release": release, "mods": [
            catalog_mod(m, records, here, on, release) for m in index["mods"]]}

    def get_mods(self, body) -> tuple[int, dict]:
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise BadRequest('expected {"ids": [mod id, ...]}')
        refused = self._refused("download mods")
        if refused:
            return refused
        count = len(set(ids))
        message = (f"Downloading {count} mod{'s' * (count != 1)} ..." if count
                   else "Updating your mods ...")
        return self._run_session("downloading", message, self._get_mods, list(ids))

    def remove_mod(self, body) -> tuple[int, dict]:
        mod_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(mod_id, str) or not mod_id:
            raise BadRequest('expected {"id": str}')
        try:
            name = next((m.name for m in mods.load_all(self.settings) if m.id == mod_id), mod_id)
        except (mods.ManifestError, OSError):
            name = mod_id
        refused = self._refused("remove a mod")
        if refused:
            return refused
        return self._run_session("removing", f"Removing {name} ...", self._remove_mod, mod_id)

    def _refused(self, what: str) -> tuple[int, dict] | None:
        """Play's busy rule: a 409 while the game runs (play_gate) or a Play, build, download
        or removal is under way; else None, with the session claimed for `what`."""
        block, _ = launcher.play_gate(procs.running_games(), self.settings)
        if block:
            return HTTPStatus.CONFLICT, {"error": f"Cannot {what} now: {block}."}
        if not self.session.claim():
            status = self.session.status
            return HTTPStatus.CONFLICT, {"error": f"Cannot {what} while "
                                                  f"{BUSY_WORDS.get(status, status)}; wait for "
                                                  "that to finish, then try again."}
        return None

    def _run_session(self, status: str, message: str, work, *args) -> tuple[int, dict]:
        """Run work(settings, *args) in a thread as the claimed session. What it returns is
        the status line when it ends; an error's first line is (the rest goes to the log).
        The installed-status cache is refreshed after it either way."""
        settings = self.settings
        self.session.log_file = Path(settings.state) / "logs" / "manager.log"
        self.session.set(status, message)

        def run():
            try:
                done = work(settings, *args)
            except Exception as err:  # every failure ends the session with its message
                text = str(err).strip() or type(err).__name__
                if not isinstance(err, PLAIN_ERRORS):
                    text = f"{type(err).__name__}: {text}"
                lines = text.splitlines()
                for line in lines[1:]:
                    self.session.add(line)
                self.session.set("error", lines[0])
            else:
                self.session.set("idle", done)
            finally:
                self.refresh_in_background()

        threading.Thread(target=run, daemon=True).start()
        snap = self.session.snapshot()
        return HTTPStatus.ACCEPTED, {"status": snap["status"], "message": snap["message"]}

    def _get_mods(self, settings: Settings, ids: list[str]) -> str:
        log = self.session.add
        log("Getting the release's list of mods ...")
        index = releases.fetch_index(settings, releases.release_info())
        self._take_out_before_update(index, settings, log)
        releases.install(ids, index, settings, log)
        self._switch_on(ids)
        if not ids:
            return f"Your mods are from release {index['release']} now."
        names = {m["id"]: m["name"] for m in index["mods"]}
        return f"Downloaded {_and([names.get(i, i) for i in dict.fromkeys(ids)])}."

    def _take_out_before_update(self, index: dict, settings: Settings, log) -> None:
        """Run the uninstall step (the old version's) of every downloaded mod this release
        updates whose files are in the game, so the next Play installs the new version.
        _Stopped, before anything is downloaded, when one fails."""
        records, release = releases.installed(settings), index["release"]
        all_mods = mods.load_all(settings)
        pending = {mod_id for _, mod_id in applier.pending_from_journal(settings)}
        steps, names = [], {}
        for mod in all_mods:
            if not mod.files or records.get(mod.id) in (None, release):
                continue
            if mod.id not in pending and not self._files_in_game(mod, settings, "updated"):
                continue
            steps.append(("uninstall", mod.id))
            names[mod.id] = mod.name
        if not steps:
            return
        log(f"Taking the old files of {_and(list(names.values()))} out of the game before "
            "the update ...")
        try:
            applier.apply(steps, all_mods, settings, log)
        except applier.StepFailed as err:
            raise _Stopped(f"Updating {names.get(err.mod_id, err.mod_id)} stopped: taking its "
                           "old files out of the game failed, so nothing was downloaded.\n"
                           f"{err}") from None
        except applier.GameRunning as err:
            raise _Stopped(f"Updating {_and(list(names.values()))} needs the game closed, so "
                           f"nothing was downloaded: {err}.") from None

    @staticmethod
    def _files_in_game(mod, settings: Settings, what: str) -> bool:
        try:
            return applier.is_installed(mod, settings)
        except applier.StepFailed as err:
            raise _Stopped(f"Cannot tell whether {mod.name}'s files are in the game, so it was "
                           f"not {what}.\n{err}") from None

    def _switch_on(self, ids: list[str]) -> None:
        """Downloaded mods start switched on (with no selection file yet, every mod is)."""
        with self._lock:
            if not ids or not self._selection_path().exists():
                return
            chosen = self._selected_or_none(mods.load_all(self.settings))
            config.write_json_atomic(self._selection_path(),
                                     chosen + [i for i in dict.fromkeys(ids) if i not in chosen])

    def _remove_mod(self, settings: Settings, mod_id: str) -> str:
        """Its uninstall step first when its files are in the game, then releases.remove."""
        log = self.session.add
        all_mods = mods.load_all(settings)
        mod = next((m for m in all_mods if m.id == mod_id), None)
        name = mod.name if mod else mod_id
        if mod is not None and mod_id in self._selected_or_none(all_mods):
            raise _Stopped(f"{name} is switched on; turn it off first, then remove it.")
        if mod is not None and mod.files and mod_id in releases.installed(settings):
            pending = {i for _, i in applier.pending_from_journal(settings)}
            if mod_id in pending or self._files_in_game(mod, settings, "removed"):
                log(f"Taking {name}'s files out of the game ...")
                try:
                    applier.apply([("uninstall", mod_id)], all_mods, settings, log)
                except applier.StepFailed as err:
                    raise _Stopped(f"Taking {name}'s files out of the game failed, so it was "
                                   f"not removed.\n{err}") from None
        releases.remove(mod_id, settings, log)
        return f"Removed {name}."

    def request_quit(self) -> tuple[int, dict]:
        return self._busy("quit") or (HTTPStatus.OK, {"status": "quitting"})

    def finish_quit(self) -> None:
        """Stop the Play servers and the HTTP server (called after the reply is sent)."""
        launcher.shutdown(self.session)
        self.quit.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def logs(self, query: dict) -> dict:
        which = query.get("which", [""])[0]
        try:
            count = int(query.get("lines", ["200"])[0])
        except ValueError:
            raise BadRequest("lines must be a number") from None
        count = max(1, min(MAX_LOG_LINES, count))
        if which == "game":
            path = self._exe_folder() / "logs" / "mlb.log"
        elif which in LOGS:
            path = Path(self.settings.state) / "logs" / LOGS[which]
        else:
            raise BadRequest("which must be game, server, build or manager")
        return {"which": which, "path": str(path), "lines": tail(path, count)}

    def _exe_folder(self) -> Path:
        """Where the exe the current selection would launch lives."""
        settings = self.settings
        try:
            all_mods = mods.load_all(settings)
        except (mods.ManifestError, OSError):
            all_mods = []
        return launcher.exe_for(set(self._selected_or_none(all_mods)), all_mods, settings).parent

    def _current_profile(self, all_mods) -> Path:
        return profiles.for_selection(set(self._selected_or_none(all_mods)), all_mods,
                                      self.settings)

    def backup_profile(self) -> tuple[int, dict]:
        busy = self._busy("back up")
        if busy:
            return busy
        games = procs.running_games()
        if games:
            return HTTPStatus.CONFLICT, {"error": "Close the game first: " + ", ".join(
                f"PID {g['pid']} ({g['exe'] or 'path unknown'})" for g in games)}
        path = actions.backup_profile(self._current_profile(mods.load_all(self.settings)))
        return HTTPStatus.OK, {"path": str(path)}

    def open(self, body) -> dict:
        what = body.get("what") if isinstance(body, dict) else None
        if what == "release-page":   # the newer release's page, in the default browser
            page = (self._newer or {}).get("page")
            if not page:
                raise actions.ActionError("There is no newer release to open.", 404)
            actions.open_page(page)
            return {"opened": page}
        all_mods = mods.load_all(self.settings)
        path = actions.folder_for(what, self.settings, {m.id: m.folder for m in all_mods},
                                  self._current_profile(all_mods))
        actions.open_folder(path)
        return {"opened": str(path)}

    def update_settings(self, body) -> tuple[int, dict]:
        """Change settings (used at once, except the manager port: after a restart);
        refused while a Play/build runs on the current ones."""
        with self._lock:
            busy = self._busy("change settings")
            if busy:
                return busy
            old = self.settings
            new = validate_settings(body, old)
            config.save(new, self.settings_path)
            self.settings = config.load(self.settings_path)
            if self._newer_source(old) != self._newer_source(self.settings):
                # the newer-release line was found for the old ones: drop it, and check afresh
                self._newer, self._newer_at = None, None
        return HTTPStatus.OK, settings_json(self.settings)


class Handler(BaseHTTPRequestHandler):
    server_version = "ModManager"
    app: App  # set by make_server

    def log_message(self, format, *args):  # quiet: pythonw has no console
        pass

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("bad Content-Length") from None
        if length < 0:
            raise BadRequest("bad Content-Length")
        if length > MAX_BODY:
            raise BadRequest("the body is too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = self.rfile.read(length) if length else b""
        self._body_read = True
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise BadRequest("the body is not JSON") from None

    def _check_caller(self, method: str) -> None:
        """Only this manager's own page may talk to it: no foreign Host (DNS rebinding),
        no foreign Origin, and JSON POSTs only (a cross-site page cannot send those
        without a CORS preflight, which is never approved)."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if (self.headers.get("Host") or "").lower() not in hosts:
            raise BadRequest("foreign Host", HTTPStatus.FORBIDDEN)
        if method != "POST":
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {"http://" + h for h in hosts}:
            raise BadRequest("foreign Origin", HTTPStatus.FORBIDDEN)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise BadRequest("POST bodies must be application/json",
                             HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

    def _drain_body(self) -> None:
        """Read and drop a refused request's body (when its Content-Length is valid and
        at most MAX_BODY), so closing the connection after the refusal does not make
        Windows reset it, losing the reply, when body bytes arrive after the close."""
        if self._body_read:
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if not 0 < length <= MAX_BODY:
            return
        try:
            self.connection.settimeout(DRAIN_TIMEOUT)
            self.rfile.read(length)
        except OSError:
            pass
        self._body_read = True

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        app = self.app
        self._body_read = False
        try:
            self._check_caller(method)
            body = self._body() if method == "POST" else None
            if method == "GET" and url.path == "/api/ping":  # cheap "is it us?" (python -m app)
                return self._json(200, PING)
            if method == "GET" and url.path == "/api/state":
                refresh = parse_qs(url.query).get("refresh", ["0"])[0] == "1"
                return self._json(200, app.state(refresh))
            if method == "GET" and url.path == "/api/logs":
                return self._json(200, app.logs(parse_qs(url.query)))
            if method == "GET" and url.path == "/api/settings":
                return self._json(200, settings_json(app.settings))
            if method == "POST" and url.path == "/api/settings":
                return self._json(*app.update_settings(body))
            if method == "POST" and url.path == "/api/select":
                return self._json(200, app.select(body))
            if method == "POST" and url.path == "/api/play":
                return self._json(*app.play())
            if method == "POST" and url.path == "/api/build":
                return self._json(*app.build())
            if method == "GET" and url.path == "/api/catalog":
                return self._json(*app.catalog())
            if method == "POST" and url.path == "/api/get-mods":
                return self._json(*app.get_mods(body))
            if method == "POST" and url.path == "/api/remove-mod":
                return self._json(*app.remove_mod(body))
            if method == "POST" and url.path == "/api/backup-profile":
                return self._json(*app.backup_profile())
            if method == "POST" and url.path == "/api/open":
                return self._json(200, app.open(body))
            if method == "POST" and url.path == "/api/quit":
                code, reply = app.request_quit()
                self._json(code, reply)
                if code == HTTPStatus.OK:
                    self.close_connection = True
                    threading.Thread(target=app.finish_quit, daemon=True).start()
                return
            if url.path.startswith("/api/"):
                return self._json(404, {"error": f"no such endpoint: {method} {url.path}"})
            if method != "GET":
                return self._json(405, {"error": "method not allowed"})
            if url.path.startswith("/art/"):
                return self._art(unquote(url.path[len("/art/"):]))
            if url.path.startswith("/modart/"):
                return self._mod_image(unquote(url.path[len("/modart/"):]), "art_file")
            if url.path.startswith("/modcard/"):
                return self._mod_image(unquote(url.path[len("/modcard/"):]), "card")
            return self._static(unquote(url.path))
        except actions.ActionError as err:
            return self._json(err.code, {"error": str(err)})
        except BadRequest as err:
            self._drain_body()
            self.close_connection = True  # an oversize or bad-length body is left unread
            return self._json(err.code, {"error": str(err)})
        except Exception as err:  # ManifestError, OSError, ...: answer instead of hanging
            return self._json(500, {"error": f"{type(err).__name__}: {err}"})

    def _art(self, name: str) -> None:
        if not ART_NAME.match(name):
            raise BadRequest("art names are A-Z, 0-9 and _ only")
        path = Path(self.app.settings.game) / "game" / "romfs" / "CROSS" / "LIVE" / f"{name}.WEBP"
        self._file(path, "image/webp", {"Cache-Control": "max-age=86400"})

    def _mod_image(self, mod_id: str, attr: str) -> None:
        """A mod's own picture ("art_file") or title card ("card"), looked up by mod id: only the file its
        manifest names is ever served."""
        mod = next((m for m in mods.load_all(self.app.settings) if m.id == mod_id and getattr(m, attr)), None)
        if mod is None:
            return self._json(404, {"error": "not found"})
        path = getattr(mod, attr)
        ctype = ART_FILE_TYPES.get(path.suffix.lower())
        if ctype is None:
            return self._json(404, {"error": "not found"})
        self._file(path, ctype, {"Cache-Control": "no-cache"})

    def _static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        root = self.app.web.resolve()
        if "\\" in rel or ":" in rel or any(part in ("", ".", "..") for part in rel.split("/")):
            raise BadRequest("bad path")
        path = (root / rel).resolve()
        if not path.is_relative_to(root):
            raise BadRequest("bad path")
        ctype = CONTENT_TYPES.get(path.suffix.lower())
        if ctype is None:
            return self._json(404, {"error": "not found"})
        self._file(path, ctype, {"Cache-Control": "no-cache"})

    def _file(self, path: Path, ctype: str, extra: dict) -> None:
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return self._json(404, {"error": "not found"})
        self._send(200, data, ctype, extra)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second manager bind

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(app: App, port: int) -> Server:
    """Bound to 127.0.0.1:port (0 = any free port); OSError if the port is taken."""
    handler = type("BoundHandler", (Handler,), {"app": app})
    app.httpd = Server((HOST, port), handler)
    return app.httpd
