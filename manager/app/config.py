"""Manager settings: defaults plus overrides from state/settings.json."""
import json
import os
import tempfile
import time
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

MANAGER = Path(__file__).resolve().parent.parent
GAME = Path(r"C:\Games\MLB The Show 26")


@dataclass
class Settings:
    """Neutral defaults: folders the manager owns sit beside it; the developer's own
    folders (Fastball source clone, build worktree) come only from settings.json."""
    game: Path = GAME
    theshow: Path | None = None
    mods: Path = MANAGER / "mods"
    recomp: Path | None = None  # a Fastball source clone: dev mode
    setup: Path | None = None  # the Fastball setup folder
    build_wt: Path | None = None
    build_out: Path = MANAGER / "builds" / "current"
    state: Path = MANAGER / "state"
    runtime_dir: Path = MANAGER / "runtime"
    release_source: str = ""  # "" = the release's GitHub download URL; else a URL or a local folder
    port: int = 8645
    packages_port: int = 10010
    profile_override: Path | None = None
    always_combined_exe: bool = False


_INT_KEYS = {"port", "packages_port"}
_BOOL_KEYS = {"always_combined_exe"}
_TEXT_KEYS = {"release_source"}
_OPTIONAL_PATH_KEYS = {"theshow", "recomp", "setup", "build_wt", "profile_override"}


def mode(settings: Settings) -> str:
    """"dev" when recomp names a Fastball source clone (it has .git), else "player"."""
    if settings.recomp is not None and (Path(settings.recomp) / ".git").exists():
        return "dev"
    return "player"


def check_value(key: str, value):
    """`value` as setting `key` holds it (a path string becomes a Path); ValueError
    with a message naming the key if it is unknown or not a valid value."""
    if key not in {f.name for f in fields(Settings)}:
        raise ValueError(f"unknown setting {key!r}")
    if key in _INT_KEYS:
        if type(value) is not int or not 1 <= value <= 65535:
            raise ValueError(f"{key} must be a port number (1-65535)")
        return value
    if key in _BOOL_KEYS:
        if type(value) is not bool:
            raise ValueError(f"{key} must be true or false")
        return value
    if key in _TEXT_KEYS:
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        return value.strip()
    if key in _OPTIONAL_PATH_KEYS and value is None:
        return None
    if isinstance(value, str) and value.strip():
        return Path(value)
    raise ValueError(f"{key} must be a path")


REPLACE_TRIES = 5  # os.replace fails on Windows while another handle has the target open
REPLACE_WAIT = 0.02  # seconds between tries


def write_json_atomic(path: Path, data) -> None:
    """Write `data` as JSON to `path` via a temp file and os.replace: a crash leaves
    the old file or the new one, never half of one. A target that is briefly locked
    (PermissionError) is retried a few times before the error is raised."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # target briefly open elsewhere (Windows): retry
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(REPLACE_WAIT)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _warn(path: Path, text: str) -> None:
    """A line in manager.log beside the settings file (pythonw has no console)."""
    try:
        logs = Path(path).parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "manager.log", "a", encoding="utf-8") as f:
            f.write(f"[settings {datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    except OSError:
        pass


def load(path: Path) -> Settings:
    """Defaults, overridden by the valid keys present in the JSON file at `path`.
    Never raises: an unreadable file, or a bad value, gives the default (logged)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as err:
        _warn(path, f"cannot read {path} ({err}); using the defaults")
        return Settings()
    if not isinstance(data, dict):
        _warn(path, f"{path} does not hold a JSON object; using the defaults")
        return Settings()
    known = {f.name for f in fields(Settings)}
    values = {}
    for key, value in data.items():
        if key not in known:
            continue
        try:
            values[key] = check_value(key, value)
        except ValueError as err:
            _warn(path, f"{path}: {err} (got {value!r}); using the default for {key}")
    return Settings(**values)


def save(settings: Settings, path: Path) -> None:
    data = {}
    for f in fields(Settings):
        value = getattr(settings, f.name)
        data[f.name] = str(value) if isinstance(value, Path) else value
    write_json_atomic(path, data)
