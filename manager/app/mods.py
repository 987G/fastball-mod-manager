"""Mod manifests: MODS/<id>/mod.json -> Mod, and placeholder expansion."""
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import features as feature_list
from .config import Settings


class ManifestError(Exception):
    """A mod.json that cannot be used; the message names the mod folder."""


@dataclass
class Mod:
    id: str
    name: str
    description: str
    folder: Path
    art: str
    files: dict | None
    runtime: dict | None
    server: dict | None
    profile: str | None
    order: int
    patches: Path | None = None  # code patch file (absolute), read by recomp/gen.py --patches
    art_file: Path | None = None  # the mod's own picture, when "art" names an image file in its folder
    card: Path | None = None      # its title card picture ("card": an image file in its folder)
    features: list | None = None  # its feature list ("features": a Markdown file in its folder), parsed
    card_text: dict | None = None  # its title card text ("card": a .json file in its folder), validated
    ship: list | None = None      # globs of the only files ever published ("ship"), relative to its folder
    version: str | None = None    # its own version ("version": short text like "6" or "1.2")


ART_FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
FEATURE_SUFFIXES = (".md", ".txt")
CARD_TEXT_SUFFIX = ".json"
CARD_TEXT_KEYS = ("tagline", "highlights", "note")
MAX_HIGHLIGHTS = 3
VERSION_MAX = 16  # characters in a mod's "version"


def clean_version(value) -> str:
    """A mod's version as the page draws it: text of 1 to VERSION_MAX characters, trimmed;
    ValueError saying what is wrong otherwise."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("version must be short text, like \"6\" or \"1.2\"")
    value = value.strip()
    if len(value) > VERSION_MAX:
        raise ValueError(f"version must be at most {VERSION_MAX} characters, not {len(value)}")
    return value


def _own_file(folder: Path, value: str, what: str) -> Path:
    """`value` as a file inside the mod's folder; ManifestError naming `what` when outside or missing."""
    root = folder.resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ManifestError(f"mod {folder.name!r}: its {what} {value} is outside the mod folder")
    if not path.is_file():
        raise ManifestError(f"mod {folder.name!r}: its {what} {path} is missing")
    return path


def _art_file(folder: Path, art) -> Path | None:
    """`art` is either a game art name (CROSS/LIVE/<name>.WEBP) or an image file in the mod's folder."""
    if not isinstance(art, str) or not art.lower().endswith(ART_FILE_SUFFIXES):
        return None
    return _own_file(folder, art, "art file")


def _card(folder: Path, card) -> Path | None:
    if card is None or (isinstance(card, str) and card.lower().endswith(CARD_TEXT_SUFFIX)):
        return None  # no card, or card text (_card_text)
    if not isinstance(card, str) or not card.lower().endswith(ART_FILE_SUFFIXES):
        raise ManifestError(f"mod {folder.name!r}: card must be an image file in the mod folder "
                            f"({', '.join(ART_FILE_SUFFIXES)}) or its card text ({CARD_TEXT_SUFFIX})")
    return _own_file(folder, card, "title card")


def _card_text(folder: Path, card) -> dict | None:
    """`card` naming a .json file: {"tagline": str, "highlights": up to 3 [head, rest], "note": str}."""
    if not isinstance(card, str) or not card.lower().endswith(CARD_TEXT_SUFFIX):
        return None
    path = _own_file(folder, card, "title card text")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as e:
        raise ManifestError(f"mod {folder.name!r}: cannot read its title card text {path}: {e}") from e

    try:
        return clean_card_text(data)
    except ValueError as e:
        raise ManifestError(f"mod {folder.name!r}: its title card text {path}: {e}") from None


def clean_card_text(data) -> dict:
    """Title card text as the page draws it: {"tagline": str, "highlights": up to 3 [head, rest],
    "note": str}; ValueError saying what is wrong otherwise."""
    if not isinstance(data, dict):
        raise ValueError("must hold a JSON object")
    unknown = sorted(set(data) - set(CARD_TEXT_KEYS))
    if unknown:
        raise ValueError(f"unknown key {unknown[0]!r} (keys: {', '.join(CARD_TEXT_KEYS)})")
    tagline, highlights, note = data.get("tagline"), data.get("highlights"), data.get("note", "")
    if not isinstance(tagline, str):
        raise ValueError("tagline must be text")
    if not isinstance(highlights, list) or len(highlights) > MAX_HIGHLIGHTS:
        raise ValueError(f"highlights must be a list of up to {MAX_HIGHLIGHTS} [head, rest] pairs")
    for item in highlights:
        if not (isinstance(item, list) and len(item) == 2 and all(isinstance(s, str) for s in item)):
            raise ValueError(f"each highlight must be [head, rest] text, not {item!r}")
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise ValueError("note must be text")
    return {"tagline": tagline, "highlights": [list(h) for h in highlights], "note": note}


def _ship_glob(folder: Path, glob) -> str:
    """One "ship" glob: relative to the mod folder, forward slashes, never leaving the folder."""
    if not isinstance(glob, str) or not glob.strip():
        raise ManifestError(f"mod {folder.name!r}: each ship entry must be a file pattern, not {glob!r}")
    if "\\" in glob:
        raise ManifestError(f"mod {folder.name!r}: ship pattern {glob!r} must use forward slashes")
    if glob.startswith("/") or ":" in glob or ".." in PurePosixPath(glob).parts:
        raise ManifestError(f"mod {folder.name!r}: ship pattern {glob!r} reaches outside the mod folder")
    if glob != glob.strip():
        raise ManifestError(f"mod {folder.name!r}: ship pattern {glob!r} starts or ends with a space")
    if any(part in ("", ".") for part in glob.split("/")):
        raise ManifestError(f"mod {folder.name!r}: ship pattern {glob!r} has an empty or \".\" part "
                            f"or ends with \"/\"; name files relative to the mod folder, like release/*.md")
    return glob


def _ship(folder: Path, value) -> list | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ManifestError(f"mod {folder.name!r}: ship must be a list of file patterns")
    return [_ship_glob(folder, g) for g in value]


def shipped_files(mod: Mod) -> list[Path]:
    """The only files of `mod` that may be published: mod.json plus every regular file its "ship"
    globs match, resolved, sorted, no duplicates. ManifestError naming the mod when it has no ship
    list, a pattern leaves the folder, or a match resolves outside it (a link or junction)."""
    folder = mod.folder
    if mod.ship is None:
        raise ManifestError(f"mod {folder.name!r}: has no ship list, so nothing of it may be published")
    root = folder.resolve()

    def inside(path: Path) -> Path:
        real = path.resolve()
        if not real.is_relative_to(root):
            raise ManifestError(f"mod {folder.name!r}: shipped file {path} leads outside the mod folder "
                                f"(to {real})")
        return real

    found = {inside(root / "mod.json")}
    for glob in mod.ship:
        for path in root.glob(_ship_glob(folder, glob)):
            if path.is_file():
                found.add(inside(path))
    return sorted(found)


def _features(folder: Path, value) -> list | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.lower().endswith(FEATURE_SUFFIXES):
        raise ManifestError(f"mod {folder.name!r}: features must be a Markdown file in the mod folder (.md)")
    path = _own_file(folder, value, "feature list")
    try:
        data = path.read_bytes()
        if len(data) > feature_list.MAX_BYTES:
            raise ManifestError(f"mod {folder.name!r}: its feature list {path} is over "
                                f"{feature_list.MAX_BYTES // 1024} KB")
        return feature_list.parse(data.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError) as e:
        raise ManifestError(f"mod {folder.name!r}: cannot read its feature list {path}: {e}") from e


def _load(folder: Path) -> Mod:
    path = folder / "mod.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise ManifestError(f"mod {folder.name!r}: cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError(f"mod {folder.name!r}: {path} must hold a JSON object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ManifestError(f"mod {folder.name!r}: {path} has no name")
    patches = data.get("patches")
    if patches is not None:
        if not isinstance(patches, str) or not patches.strip():
            raise ManifestError(f"mod {folder.name!r}: {path}: patches must be a file path")
        patches = (folder / patches).absolute()
        if not patches.is_file():
            raise ManifestError(f"mod {folder.name!r}: its patch file {patches} is missing")
    version = None
    if "version" in data:
        try:
            version = clean_version(data["version"])
        except ValueError as e:
            raise ManifestError(f"mod {folder.name!r}: {path}: {e}") from None
    return Mod(id=folder.name, name=name, description=data.get("description", ""),
               folder=folder, art=data.get("art", ""), files=data.get("files"),
               runtime=data.get("runtime"), server=data.get("server"),
               profile=data.get("profile"), order=data.get("order", 100), patches=patches,
               art_file=_art_file(folder, data.get("art")), card=_card(folder, data.get("card")),
               features=_features(folder, data.get("features")),
               card_text=_card_text(folder, data.get("card")), ship=_ship(folder, data.get("ship")),
               version=version)


def load_all(settings: Settings) -> list[Mod]:
    """Every MODS/*/mod.json, sorted by order then name; none while the mods folder does not
    exist yet (a new manager, before its first download)."""
    root = Path(settings.mods)
    if not root.exists():
        return []
    folders = [p for p in root.iterdir() if p.is_dir() and (p / "mod.json").is_file()]
    return sorted((_load(f) for f in folders), key=lambda m: (m.order, m.name))


def expand(value, mod: Mod, settings: Settings, extra: dict | None = None):
    """Replace {mod} {mods} {game} {state} {port} (+ extra keys, e.g. {profile})
    in strings, recursively through lists and dict values. Unknown {..} stay."""
    subs = {"mod": mod.folder, "mods": settings.mods, "game": settings.game,
            "state": settings.state, "port": settings.packages_port}
    subs.update(extra or {})
    subs = {k: str(v) for k, v in subs.items()}
    return _expand(value, subs)


def _expand(value, subs: dict):
    if isinstance(value, str):
        for key, text in subs.items():
            value = value.replace("{" + key + "}", text)
        return value
    if isinstance(value, list):
        return [_expand(v, subs) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, subs) for k, v in value.items()}
    return value
