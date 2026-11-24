"""Player mode's build tools: the Fastball setup's recomp/, tools/ and runtime/ copied into
builds\\toolkit, with the release's prebuilt runtime (the mods' patched recomp files, runtime
headers and libmlbrt.a, listed in <runtime_dir>\\runtime.json) laid over them."""
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Callable

from .config import Settings

LISTING = "runtime.json"
STAMP = ".toolkit-stamp"
# Part of the stamp: bump it whenever what assemble() copies (TREES, SKIPPED_NAMES, VERSION)
# or how it lays the runtime's files changes, so a toolkit an older manager laid is laid again.
# 1: f027fc4 (no runtime/deps); 2: runtime/deps copied into the toolkit.
LAYOUT = 2
# Copied from the setup, with VERSION. runtime/ includes deps (SDL3, Vulkan, VMA): tools/build.py
# takes --gen, --out and --deps relative to its own folder (no spaces, same drive), so the deps
# must sit inside the toolkit wherever the setup is.
TREES = ("recomp", "tools", "runtime")
SKIPPED_NAMES = {"__pycache__"}  # never copied: bytecode is per Python
# What the build cannot do without (checked in the setup before anything is copied): the
# trees copied here, and what the build uses from the setup itself (its Python with numpy
# and lz4, llvm-mingw, ninja).
NEEDED = ("VERSION", "recomp/gen.py", "tools/build.py", "runtime/core", "python/python.exe",
          "llvm/bin/clang.exe", "llvm/bin/llvm-ar.exe", "bin/ninja.exe", "runtime/deps")
SETUP_UNSET = ("Choose the Fastball setup folder in Settings (Setup folder); the game with your "
               "mods is built with it.")
GET_AGAIN = "get the mods again from the Mods page"


class ToolkitError(Exception):
    """The setup or the release's runtime cannot be used; the message says what to do."""


def folder() -> Path:
    """builds\\toolkit, beside the manager's generated C (builder.BUILDS)."""
    from . import builder  # builder imports this module
    return Path(builder.BUILDS) / "toolkit"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _setup(settings: Settings) -> Path:
    if settings.setup is None:
        raise ToolkitError(SETUP_UNSET)
    return Path(settings.setup)


def _listing(settings: Settings) -> Path:
    return Path(settings.runtime_dir) / LISTING


def runtime_sha256(settings: Settings) -> str:
    """sha256 of runtime.json ("missing" when it cannot be read): cheap, for the state poll."""
    try:
        return hashlib.sha256(_listing(settings).read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _rel_ok(rel) -> bool:
    """A relative path inside recomp/, tools/ or runtime/ (forward slashes, no .. or drive)."""
    if not isinstance(rel, str) or not rel or "\\" in rel or ":" in rel or rel.startswith("/"):
        return False
    parts = rel.split("/")
    return len(parts) > 1 and parts[0] in TREES and all(p not in ("", ".", "..") for p in parts)


def _hex(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def load_runtime(settings: Settings) -> dict:
    """runtime.json as {"fastball", "files", "json_sha256"}, after every listed file under
    runtime_dir was found with its sha256. ToolkitError naming the file otherwise."""
    path = _listing(settings)
    try:
        raw = path.read_bytes()
    except OSError:
        raise ToolkitError(f"The mods' runtime is not installed ({path} is missing); "
                           f"{GET_AGAIN}.") from None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        doc = None
    files = doc.get("files") if isinstance(doc, dict) else None
    good = (isinstance(doc, dict) and isinstance(doc.get("fastball"), str)
            and bool(doc["fastball"]) and isinstance(files, dict)
            and all(_rel_ok(rel) and isinstance(entry, dict) and _hex(entry.get("sha256"))
                    and (entry.get("base") is None or _hex(entry.get("base")))
                    for rel, entry in files.items()))
    if not good:
        raise ToolkitError(f"The mods' runtime list {path} is damaged; {GET_AGAIN}.")
    for rel, entry in files.items():
        file = Path(settings.runtime_dir) / rel
        try:
            ok = _sha256(file) == entry["sha256"]
        except OSError:
            raise ToolkitError(f"The mods' runtime is missing {file}; {GET_AGAIN}.") from None
        if not ok:
            raise ToolkitError(f"The mods' runtime file {file} is damaged (its sha256 does not "
                               f"match); {GET_AGAIN}.")
    return {"fastball": doc["fastball"],
            "files": {rel: {"base": e.get("base"), "sha256": e["sha256"]}
                      for rel, e in files.items()},
            "json_sha256": hashlib.sha256(raw).hexdigest()}


def _setup_version(settings: Settings) -> str:
    path = _setup(settings) / "VERSION"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        raise ToolkitError(f"{path} is missing; choose the Fastball setup folder in Settings "
                           f"(Setup folder).") from None


def check_versions(settings: Settings, runtime: dict) -> None:
    """The setup's VERSION and the install's fastball-install.json "release" both equal
    the Fastball release the runtime was made against."""
    want = runtime["fastball"]
    setup = _setup(settings)
    have = _setup_version(settings)
    if have != want:
        raise ToolkitError(f"The mods need Fastball {want}; the setup folder {setup} is Fastball "
                           f"{have}. Choose a Fastball {want} setup in Settings, or get mods made "
                           f"for {have}.")
    install = Path(settings.game) / "fastball-install.json"
    try:
        release = json.loads(install.read_text(encoding="utf-8")).get("release")
    except (OSError, ValueError, AttributeError):
        raise ToolkitError(f"The mods need Fastball {want}, and {install} is missing or "
                           f"unreadable; install Fastball {want} into the game folder with "
                           f"its setup.") from None
    if release != want:
        raise ToolkitError(f"The mods need Fastball {want}; the game folder {settings.game} has "
                           f"Fastball {release}. Install Fastball {want} with its setup, or get "
                           f"mods made for {release}.")


def stamp(settings: Settings, runtime: dict) -> str:
    """What the toolkit was laid from: sha256 over runtime.json's sha256, the setup's
    VERSION, LAYOUT and the resolved setup folder (another folder is laid, and its
    originals checked, again)."""
    try:
        where = os.path.normcase(str(_setup(settings).resolve()))
    except OSError:
        where = os.path.normcase(str(_setup(settings).absolute()))
    text = "\0".join([runtime["json_sha256"], _setup_version(settings), f"layout {LAYOUT}", where])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_originals(setup: Path, runtime: dict) -> None:
    """The files the build needs are in the setup, and every file the runtime replaces is
    the original it was made against (absent when its base is null)."""
    fastball = runtime["fastball"]
    for rel in NEEDED:
        if not (setup / rel).exists():
            raise ToolkitError(f"The Fastball setup folder has no {setup / rel}; choose the setup "
                               f"you installed Fastball {fastball} with in Settings (Setup folder).")
    for rel, entry in runtime["files"].items():
        original = setup / rel
        if entry["base"] is None:
            if original.exists():
                raise ToolkitError(f"{original} is not part of Fastball {fastball} as the mods' "
                                   f"runtime expects; reinstall the Fastball setup.")
            continue
        try:
            same = original.is_file() and _sha256(original) == entry["base"]
        except OSError:
            same = False
        if not same:
            raise ToolkitError(f"{original} is not the Fastball {fastball} original the mods' "
                               f"runtime was made against (missing or changed); reinstall the "
                               f"Fastball setup.")


def _copy_tree(src: Path, dst: Path, rel: str, laid: set[str]) -> None:
    """Copy the setup's tree src (at `rel`) into dst, leaving out bytecode and the files the
    runtime lays over it."""
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        sub = f"{rel}/{entry.name}"
        if sub in laid or entry.name in SKIPPED_NAMES:
            continue
        if entry.is_dir(follow_symlinks=False):
            _copy_tree(Path(entry.path), dst / entry.name, sub, laid)
        elif entry.is_file(follow_symlinks=False):
            shutil.copy2(entry.path, dst / entry.name)


def assemble(settings: Settings, runtime: dict, log: Callable[[str], None]) -> Path:
    """builds\\toolkit: the setup's recomp/, tools/, runtime/ (headers, library and
    deps) and VERSION, with every runtime file laid over them; skipped when its stamp
    matches. Built in toolkit.new and swapped in at the end: a changed original or a failed
    copy raises ToolkitError and leaves builds\\toolkit as it was."""
    setup = _setup(settings)
    dest = folder()
    key = stamp(settings, runtime)
    try:
        current = (dest / STAMP).read_text(encoding="utf-8").strip()
    except OSError:
        current = None
    if current == key:
        log(f"Build tools are up to date ({dest}).")
        return dest
    _check_originals(setup, runtime)
    new, old = dest.with_name(dest.name + ".new"), dest.with_name(dest.name + ".old")
    log(f"Laying out the build tools in {dest}: Fastball setup {setup} with the mods' "
        f"runtime {settings.runtime_dir}")
    try:
        for leftover in (new, old):
            if leftover.exists():
                shutil.rmtree(leftover)
        laid = set(runtime["files"])
        for tree in TREES:
            _copy_tree(setup / tree, new / tree, tree, laid)
        shutil.copy2(setup / "VERSION", new / "VERSION")
        for rel in sorted(laid):
            target = new / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(settings.runtime_dir) / rel, target)
        (new / STAMP).write_text(key, encoding="utf-8")
        if dest.exists():
            os.replace(dest, old)
        os.replace(new, dest)
    except OSError as err:
        shutil.rmtree(new, ignore_errors=True)
        if old.exists() and not dest.exists():
            try:
                os.replace(old, dest)
            except OSError:
                pass
        raise ToolkitError(f"Laying out the build tools in {dest} failed ({err}); close anything "
                           f"using that folder, then try again.") from err
    shutil.rmtree(old, ignore_errors=True)
    return dest
