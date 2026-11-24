"""Small UI actions: back up the profile Play would use, open a known folder in Explorer."""
import os
import shutil
from datetime import datetime
from pathlib import Path

from .config import Settings


class ActionError(Exception):
    """An action the UI cannot have: answered with `code` and this message."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_profile(profile: Path) -> Path:
    """Copy `profile` (without profile.lock) to <profile>-backup-YYYYMMDD-HHMMSS[-n]."""
    profile = Path(profile)
    if not profile.is_dir():
        raise ActionError(f"The profile {profile} does not exist yet; nothing to back up.", 404)
    base = Path(f"{profile}-backup-{_stamp()}")
    target, n = base, 1
    while target.exists():
        n += 1
        target = Path(f"{base}-{n}")
    partial = Path(str(target) + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    shutil.copytree(profile, partial, ignore=shutil.ignore_patterns("profile.lock"))
    partial.rename(target)
    return target


def folder_for(what, settings: Settings, mod_folders: dict[str, Path], profile: Path) -> Path:
    """The folder a UI "open" request names; ActionError(400) for anything not whitelisted."""
    if not isinstance(what, str):
        raise ActionError('expected {"what": "profile" | "mods" | "mod:<id>" | "logs" | "build"}')
    if what == "profile":
        return Path(profile)
    if what == "mods":
        return Path(settings.mods)
    if what == "logs":
        logs = Path(settings.state) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return logs
    if what == "build":
        return Path(settings.build_out)
    if what.startswith("mod:") and what[4:] in mod_folders:
        return Path(mod_folders[what[4:]])
    raise ActionError(f"cannot open {what!r}")


def open_folder(path: Path) -> None:
    """Show `path` in Explorer; ActionError(404) if it does not exist."""
    if not Path(path).is_dir():
        raise ActionError(f"{path} does not exist yet.", 404)
    os.startfile(str(path))


def open_page(url: str) -> None:
    """Show a GitHub page in the default browser; ActionError(400) for any other address."""
    if not (isinstance(url, str) and url.startswith("https://github.com/")):
        raise ActionError(f"cannot open {url!r}")
    os.startfile(url)
