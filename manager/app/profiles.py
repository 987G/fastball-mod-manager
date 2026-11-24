"""Which save profile a selection plays on, and creating a mod's own profile copy.

GAME/profile holds the user's normal saves; nothing here ever writes inside it.
"""
import os
import shutil
from pathlib import Path
from typing import Callable

from .config import Settings


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def for_selection(selected: set[str], all_mods, settings: Settings) -> Path:
    """The override if set; else GAME/profile-<id> for the first selected mod
    (in all_mods order) with profile "own"; else GAME/profile."""
    if settings.profile_override is not None:
        return Path(settings.profile_override)
    for mod in all_mods:
        if mod.id in selected and mod.profile == "own":
            return Path(settings.game) / f"profile-{mod.id}"
    return Path(settings.game) / "profile"


def ensure(path: Path, settings: Settings, log: Callable[[str], None]) -> None:
    """Make sure `path` exists, copying GAME/profile to it (without profile.lock)
    when it is a missing own profile. The normal profile and an override are
    used as they are."""
    path = Path(path)
    normal = Path(settings.game) / "profile"
    if _same(path, normal):
        return
    if settings.profile_override is not None and _same(path, settings.profile_override):
        if not path.exists():
            raise FileNotFoundError(f"profile override {path} does not exist")
        return
    if path.exists():
        return
    if not normal.is_dir():
        raise FileNotFoundError(f"normal profile {normal} does not exist; cannot create {path}")
    partial = Path(str(path) + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    log(f"Creating profile {path} from {normal} ...")
    shutil.copytree(normal, partial, ignore=shutil.ignore_patterns("profile.lock"))
    partial.rename(path)
    log(f"Profile {path} ready.")
