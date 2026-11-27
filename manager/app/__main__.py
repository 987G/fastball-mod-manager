"""python -m app: serve the manager on 127.0.0.1 and show it in an Edge app window
(the default browser when Edge is missing or will not start).

Stays up while that window is open, and after it closes until a running Play ends;
in the default browser it stays up until QUIT MANAGER. The mod servers a Play started
never outlive the manager. pythonw has no console, so a startup failure is shown in a
message box and written to state/logs/manager.log."""
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

from . import config, launcher, releases, server

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
SETTINGS = config.MANAGER / "state" / "settings.json"
HANDOFF_SECONDS = 10.0  # an Edge that exits sooner handed the URL to an open window
LOG_LIMIT = 5 * 1024 * 1024  # manager.log above this moves to manager.log.1 at startup


def note(settings: config.Settings, text: str) -> None:
    """A line in state/logs/manager.log (pythonw has no console)."""
    try:
        logs = Path(settings.state) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "manager.log", "a", encoding="utf-8") as f:
            f.write(f"[manager {datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    except OSError:
        pass


def rotate_log(settings: config.Settings, limit: int = LOG_LIMIT) -> None:
    """Keep manager.log (Play and build output go there too) bounded: when it is over
    `limit` bytes, it replaces manager.log.1 and a new one starts."""
    log = Path(settings.state) / "logs" / "manager.log"
    try:
        if log.stat().st_size > limit:
            os.replace(log, log.with_name("manager.log.1"))
    except OSError:
        pass


def message_box(text: str) -> None:
    """Show `text` in a Windows message box (pythonw has no console); nothing where
    there is no user32 (not Windows)."""
    try:
        import ctypes
        # MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST: not hidden behind other windows
        ctypes.windll.user32.MessageBoxW(None, text, "Show Mod Manager", 0x10 | 0x10000 | 0x40000)
    except (AttributeError, ImportError, OSError):
        pass


def open_window(settings: config.Settings, url: str) -> subprocess.Popen | None:
    """The Edge app window (its process), or None when the page was opened in the
    default browser instead: Edge is missing or will not start."""
    if not EDGE.is_file():
        note(settings, f"Edge not found at {EDGE}.")
    else:
        argv = [str(EDGE), f"--app={url}", "--user-data-dir=" + str(Path(settings.state) / "edge"),
                "--window-size=1600,900"]
        try:
            edge = subprocess.Popen(argv)
        except OSError as err:
            note(settings, f"Cannot start Edge ({err}).")
        else:
            note(settings, f"Opened {url} in an Edge window.")
            return edge
    try:
        opened = webbrowser.open(url)
    except webbrowser.Error as err:
        note(settings, f"Cannot open the default browser ({err}).")
        opened = False
    if opened:
        note(settings, f"Opened {url} in the default browser; serving until Quit.")
    else:
        note(settings, f"No browser opened {url}.")
        message_box(f"Show Mod Manager is running, but no browser opened; open {url} in "
                    "your web browser.")
    return None


def _get_json(port: int, path: str, timeout: float):
    """GET http://127.0.0.1:<port><path>: "refused" when nothing accepts the connection,
    "unsure" when it is accepted but no reply comes in `timeout` seconds (or it breaks
    off), else the reply's JSON on a 200, or None for any other reply. http.client, not
    urllib, so no proxy setting gets in the way."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        try:
            conn.connect()
        except ConnectionRefusedError:
            return "refused"
        except OSError:
            return "unsure"
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read(4 * 1024 * 1024)
        except (OSError, http.client.HTTPException):
            return "unsure"
        if resp.status != 200:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None
    finally:
        conn.close()


def port_owner(port: int, timeout: float = 5.0) -> str:
    """Who has 127.0.0.1:port: "ours" (a manager answers /api/ping, or /api/state for one
    from before /api/ping), "other" only on a definite answer (connection refused, or a
    reply that is not ours), else "unsure": a manager still starting (bound, not serving
    yet) or busy accepts the connection but has not answered."""
    probes = (("/api/ping", lambda d: d == server.PING),
              ("/api/state", lambda d: isinstance(d.get("session"), dict) and "mods" in d))
    for path, ours in probes:
        answer = _get_json(port, path, timeout)
        if answer in ("refused", "unsure"):
            return "other" if answer == "refused" else "unsure"
        if isinstance(answer, dict) and ours(answer):
            return "ours"
    return "other"


def wait_for_exit(app: server.App, edge: subprocess.Popen | None, launched: float) -> None:
    """Return when the manager should stop: POST /api/quit, or the window we opened
    was closed and no Play/build is under way. An Edge that exits within
    HANDOFF_SECONDS handed off to an open window: then only /api/quit ends it. So does
    no Edge at all (`edge` None: the default browser has the page, and closing its tab
    leaves the manager serving; starting it again opens a new tab)."""
    while not app.quit.wait(0.5):
        if edge is None or edge.poll() is None:
            continue
        if time.monotonic() - launched < HANDOFF_SECONDS:
            note(app.settings, "Edge handed the window to one already open; serving until "
                               "Quit.")
            edge = None
            continue
        while app.session.status in launcher.BUSY:
            if app.quit.wait(1):
                return
        return


def recover(app: server.App) -> None:
    """Put back what a mod download or removal left half done when the manager stopped (only
    the instance that serves runs this, before the page can start another one); a failure
    shows in the page's status line."""
    settings = app.settings
    try:
        releases.recover(settings, lambda line: note(settings, line))
    except releases.ReleaseError as err:
        note(settings, str(err))
        app.session.set("error", str(err))


def main(serving: threading.Event | None = None) -> int:
    """Serve and show the manager; `serving` is set once the page is served."""
    settings = config.load(SETTINGS)
    url = f"http://127.0.0.1:{settings.port}/"
    app = server.App(settings, SETTINGS)
    try:
        httpd = server.make_server(app, settings.port)
    except OSError as err:
        owner = port_owner(settings.port)
        if owner != "other":  # the manager is already running (or likely is): show its window
            if owner == "unsure":
                note(settings, f"Port {settings.port} is taken and gave no clear answer; "
                               "opening the window anyway.")
            open_window(settings, url)
            return 0
        note(settings, f"Cannot use port {settings.port} ({err}); another program has it.")
        other = 8646 if settings.port != 8646 else 8647
        message_box(f"Show Mod Manager cannot start because another program is using port "
                    f"{settings.port}; set \"port\" to another number, such as {other}, in "
                    f"{SETTINGS} (if that file is missing, create it holding "
                    f"{{\"port\": {other}}}) and start it again.")
        return 1
    rotate_log(settings)  # only the instance that serves: a second one just shows the window
    recover(app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    note(settings, f"Serving {url}")
    if serving is not None:
        serving.set()
    try:
        app.refresh_in_background()
        app.check_newer_in_background()   # a dev copy (no release.json) never asks GitHub
        launched = time.monotonic()
        edge = open_window(settings, url)
        wait_for_exit(app, edge, launched)
    except KeyboardInterrupt:
        pass
    finally:
        launcher.shutdown(app.session)
        httpd.shutdown()
        httpd.server_close()
        note(app.settings, "Stopped.")
    return 0


def run() -> int:
    """main(), with any startup failure written to manager.log and shown in a message
    box instead of pythonw exiting silently ("could not start", or "stopped because of
    an error" once the page was served). The exit code: 0, or 1 on a failure."""
    serving = threading.Event()
    try:
        return main(serving) or 0
    except KeyboardInterrupt:
        return 0
    except Exception as err:
        what, head = (("stopped because of an error", "Stopped because of an error")
                      if serving.is_set() else ("could not start", "Could not start"))
        log = Path(config.load(SETTINGS).state) / "logs" / "manager.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"[manager {datetime.now():%Y-%m-%d %H:%M:%S}] {head}:\n"
                        f"{traceback.format_exc()}")
        except OSError:
            message_box(f"Show Mod Manager {what}: {err}")
        else:
            message_box(f"Show Mod Manager {what}; the details are in {log}.")
        return 1


if __name__ == "__main__":
    sys.exit(run())
