"""python -m app: serve the manager on 127.0.0.1 and show it in an Edge app window.

Stays up while that window is open, and after it closes until a running Play ends;
the mod servers a Play started never outlive the manager."""
import os
import subprocess
import threading
import time
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


def open_window(settings: config.Settings, url: str) -> subprocess.Popen | None:
    if not EDGE.is_file():
        note(settings, f"Edge not found at {EDGE}; open {url} in a browser.")
        return None
    argv = [str(EDGE), f"--app={url}", "--user-data-dir=" + str(Path(settings.state) / "edge"),
            "--window-size=1600,900"]
    try:
        return subprocess.Popen(argv)
    except OSError as err:
        note(settings, f"Cannot start Edge ({err}); open {url} in a browser.")
        return None


def wait_for_exit(app: server.App, edge: subprocess.Popen | None, launched: float) -> None:
    """Return when the manager should stop: POST /api/quit, or the window we opened
    was closed and no Play/build is under way. An Edge that exits within
    HANDOFF_SECONDS handed off to an open window: then only /api/quit ends it."""
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


def main() -> None:
    settings = config.load(SETTINGS)
    url = f"http://127.0.0.1:{settings.port}/"
    app = server.App(settings, SETTINGS)
    try:
        httpd = server.make_server(app, settings.port)
    except OSError:  # the manager is already running: just show its window
        open_window(settings, url)
        return
    rotate_log(settings)  # only the instance that serves: a second one just shows the window
    recover(app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    note(settings, f"Serving {url}")
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


if __name__ == "__main__":
    main()
