"""Release downloads: a release's index.json, mod zips and runtime zip, fetched from its GitHub
release (or the URL or folder in Settings > Release source), checked (size, sha256, every zip
member) and swapped into the mods folder and runtime_dir together; and removing a downloaded mod.

Every change of folders runs through one journaled swap: state/release-swap.json lists each
folder move, out (to mods/.outgoing, or runtime_dir.old) and in (from mods/.incoming, or
runtime_dir.new), before the first one, records each move once it is done, and is deleted once
the list of downloaded mods (state/installed.json) is written. A failure puts every folder back
at once; a crash is put back by the next install, remove or recover(). The staging folders have
no mod.json at their top, so the manager never reads them as mods."""
import dataclasses
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import applier, config, mods, toolkit
from .config import Settings, write_json_atomic

RELEASE_INFO = "release.json"   # beside the manager, written by the packager
INDEX = "index.json"            # in the release
INSTALLED = "installed.json"    # in state: {mod id: release}
JOURNAL = "release-swap.json"   # in state while folders are being swapped
SELECTION = "selection.json"    # in state: the mods switched on (server.App.selected)
RUNTIME_MARK = ".release.json"  # in runtime_dir: {"release", "sha256"} of the runtime zip it came from
INCOMING, OUTGOING = ".incoming", ".outgoing"  # in the mods folder, during a swap only
DOWNLOADS = "download-"         # temp folders in state
GITHUB = "https://github.com/{repo}/releases/download/{tag}/"
LATEST = "https://github.com/{repo}/releases/latest/download/" + INDEX  # redirects to the newest
PAGE = "https://github.com/{repo}/releases/tag/{tag}"
USER_AGENT = "ShowModManager"
TIMEOUT = 60                    # seconds without data before a download fails
LATEST_TIMEOUT = 10             # the check for a newer release: short, it only shows a line
TAG_MAX = 64                    # longest release tag that check accepts
BLOCK = 1 << 20
INDEX_MAX = 4 << 20

_REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")  # no dots: never "..", ".new" or a staging name
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.zip\Z")  # a plain name: safe in a URL and a path
_NOT_ON_WINDOWS = set('<>"|?*')
_DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)),
            *(f"lpt{i}" for i in range(10))}
_LOCK = threading.RLock()  # one install, remove or recover at a time

Log = Callable[[str], None]


class ReleaseError(Exception):
    """A download, check or swap that did not happen; the message names the file and what to do.
    Nothing was changed (or everything was put back) when it is raised."""


# --- where the release is ------------------------------------------------------------------
def release_info(manager: Path | None = None) -> dict | None:
    """MANAGER/release.json ({"repo", "tag", "fastball"}); None when there is none (a dev copy)."""
    path = Path(manager if manager is not None else config.MANAGER) / RELEASE_INFO
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as err:
        raise ReleaseError(f"Cannot read {path} ({err}); download the manager again.") from None
    try:
        info = json.loads(raw)
    except ValueError:
        info = None
    if not (isinstance(info, dict) and isinstance(info.get("repo"), str)
            and _REPO.match(info["repo"]) and isinstance(info.get("tag"), str)
            and _TAG.match(info["tag"]) and isinstance(info.get("fastball"), str)
            and info["fastball"]):
        raise ReleaseError(f"{path} is damaged; download the manager again.")
    return info


def source(settings: Settings, info: dict | None) -> str:
    """Where the release's files are: Settings > Release source (a URL or a folder) when set,
    else the GitHub release named by `info`."""
    if settings.release_source:
        return settings.release_source
    if info is None:
        raise ReleaseError("This manager has no release to download mods from (there is no "
                           f"{RELEASE_INFO} beside it); set Release source in Settings to a "
                           "release's download address or a folder holding its files.")
    return GITHUB.format(repo=info["repo"], tag=info["tag"])


def _is_url(src: str) -> bool:
    return src.lower().startswith(("http://", "https://"))


def _where(src: str, name: str) -> str:
    if _is_url(src):
        return src + ("" if src.endswith("/") else "/") + name
    return str(Path(src) / name)


def _open(src: str, name: str):
    """A binary stream of the release file `name` (a plain name, checked by the caller)."""
    if _is_url(src):
        request = urllib.request.Request(_where(src, name), headers={"User-Agent": USER_AGENT})
        return urllib.request.urlopen(request, timeout=TIMEOUT)
    return open(Path(src) / name, "rb")


# --- the index -----------------------------------------------------------------------------
def _hex(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _file_entry_problem(entry) -> str | None:
    if not isinstance(entry, dict):
        return "an entry is not an object"
    if not (isinstance(entry.get("file"), str) and _FILE.match(entry["file"])):
        return f"the file name {entry.get('file')!r} is not a plain .zip name"
    if not _hex(entry.get("sha256")):
        return f"{entry['file']} has no valid sha256"
    if type(entry.get("size")) is not int or entry["size"] < 0:
        return f"{entry['file']} has no valid size"
    return None


def _index_problem(index) -> str | None:
    if not isinstance(index, dict):
        return "it does not hold a JSON object"
    if not (isinstance(index.get("release"), str) and _TAG.match(index["release"])):
        return "it names no release"
    if not (isinstance(index.get("fastball"), str) and index["fastball"]):
        return "it names no Fastball release"
    why = _file_entry_problem(index.get("runtime"))
    if why:
        return f"runtime: {why}"
    if not isinstance(index.get("mods"), list):
        return "it has no list of mods"
    seen = set()
    for mod in index["mods"]:
        if not (isinstance(mod, dict) and isinstance(mod.get("id"), str) and _ID.match(mod["id"])):
            return f"a mod has no valid id ({mod.get('id') if isinstance(mod, dict) else mod!r})"
        if mod["id"].casefold() in seen:
            return f"the mod {mod['id']!r} is listed twice"
        seen.add(mod["id"].casefold())
        if not isinstance(mod.get("name"), str):
            return f"the mod {mod['id']!r} has no name"
        why = _file_entry_problem(mod)
        if why:
            return f"mod {mod['id']!r}: {why}"
    return None


def _check_index(index, where: str) -> dict:
    why = _index_problem(index)
    if why:
        raise ReleaseError(f"The release's list of mods ({where}) is damaged: {why}. Try again "
                           "later, or check Release source in Settings.")
    return index


def fetch_index(settings: Settings, info: dict | None) -> dict:
    """The release's index.json, checked: {"release", "fastball", "runtime": {"file", "sha256",
    "size"}, "mods": [{"id", "name", ..., "file", "sha256", "size"}]}."""
    src = source(settings, info)
    where = _where(src, INDEX)
    try:
        with _open(src, INDEX) as stream:
            raw = stream.read(INDEX_MAX + 1)
    except (OSError, http.client.HTTPException) as err:
        raise ReleaseError(f"Cannot get the release's list of mods {where} ({err}); check your "
                           "internet connection, or Release source in Settings, then try "
                           "again.") from None
    if len(raw) > INDEX_MAX:
        raise ReleaseError(f"The release's list of mods {where} is too large to be one; check "
                           "Release source in Settings.")
    try:
        index = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        index = None
    return _check_index(index, where)


# --- a newer release -------------------------------------------------------------------------
def _note(settings: Settings, text: str) -> None:
    """A line in state/logs/manager.log, as __main__.note writes them (best effort)."""
    try:
        logs = Path(settings.state) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "manager.log", "a", encoding="utf-8") as f:
            f.write(f"[manager {time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


def newer_release(settings: Settings, info: dict | None, opener=None,
                  last: dict | None = None) -> dict | None:
    """The newest release on GitHub when its tag is newer than this copy's: {"release",
    "fastball", "fastball_changed" (it names another Fastball than this copy), "page" (its
    release page)}. None otherwise: no release.json (`info` None), a Release source of its own
    (a test release), the same, an older or an undated tag. A failure (network, a bad index)
    writes one line to the manager's log and answers `last`, the previous good answer, so a
    check that could not reach GitHub changes nothing. Never raises."""
    if info is None or settings.release_source:
        return None
    url = LATEST.format(repo=info["repo"])
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with (opener or urllib.request.urlopen)(request, timeout=LATEST_TIMEOUT) as stream:
            raw = stream.read(INDEX_MAX + 1)
        if len(raw) > INDEX_MAX:
            raise ReleaseError("its list of mods is too large to be one")
        try:
            index = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            index = None
        why = _index_problem(index)   # not _check_index: its hint about Release source is for downloads
        if not why and len(index["release"]) > TAG_MAX:
            why = f"its release tag is longer than {TAG_MAX} characters"
        if why:
            raise ReleaseError(f"its list of mods is damaged: {why}")
        tag, ours = _release_key(index["release"]), _release_key(info["tag"])  # int() can refuse
    except Exception as err:  # a background check: whatever went wrong, the page keeps its line
        why = " ".join(str(err).split()) or type(err).__name__
        _note(settings, f"Cannot check for a newer release ({url}): {why}")
        return last
    if tag is None or ours is None or tag <= ours:
        return None
    return {"release": index["release"], "fastball": index["fastball"],
            "fastball_changed": index["fastball"] != info["fastball"],
            "page": PAGE.format(repo=info["repo"], tag=index["release"])}


# --- what is installed -----------------------------------------------------------------------
def _installed_path(settings: Settings) -> Path:
    return Path(settings.state) / INSTALLED


def installed(settings: Settings) -> dict:
    """{mod id: release} of the mods the manager downloaded (state/installed.json)."""
    path = _installed_path(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        raise ReleaseError(f"The list of downloaded mods {path} cannot be read ({err}); delete "
                           "it, then get your mods again.") from None
    if not (isinstance(data, dict) and all(isinstance(k, str) and _ID.match(k)
                                           and isinstance(v, str) for k, v in data.items())):
        raise ReleaseError(f"The list of downloaded mods {path} is damaged; delete it, then get "
                           "your mods again.")
    return data


def _runtime_mark(settings: Settings) -> dict | None:
    try:
        mark = json.loads((Path(settings.runtime_dir) / RUNTIME_MARK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return mark if isinstance(mark, dict) else None


def runtime_release(settings: Settings) -> str | None:
    """The release the installed runtime came from; None when it was not downloaded."""
    mark = _runtime_mark(settings)
    release = mark.get("release") if mark else None
    return release if isinstance(release, str) else None


def _runtime_current(settings: Settings, index: dict) -> bool:
    """The runtime came from this release's runtime zip and every file of it is intact."""
    mark = _runtime_mark(settings)
    if not mark or mark.get("release") != index["release"] \
            or mark.get("sha256") != index["runtime"]["sha256"]:
        return False
    try:
        toolkit.load_runtime(settings)
    except toolkit.ToolkitError:
        return False
    return True


def _release_key(tag: str) -> tuple | None:
    parts = tag.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def _older(tag: str, than: str) -> bool:
    """Dated tags (2026.11.2) compared by number; tags that are not dated are never older."""
    a, b = _release_key(tag), _release_key(than)
    return a is not None and b is not None and a < b


def _is_on(mod_id: str, settings: Settings) -> bool:
    """As server.App.selected reads state/selection.json: no file yet means every mod is on."""
    path = Path(settings.state) / SELECTION
    try:
        ids = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        ids = None
    if not isinstance(ids, list):
        raise ReleaseError(f"The mod selection {path} cannot be read; switch a mod on or off on "
                           "Home, then try again.")
    return mod_id in ids


# --- download and unpack ---------------------------------------------------------------------
def _mb(size: int) -> str:
    return f"{size / 1e6:.1f} MB"


def _fetch(src: str, name: str, dest: Path, sha256: str, size: int, log: Log) -> None:
    """Download release file `name` to `dest`, refusing more than `size` bytes; ReleaseError
    when it is cut off, too long or its sha256 differs from the release's list."""
    where = _where(src, name)
    log(f"Downloading {where} ({_mb(size)})")
    digest, got = hashlib.sha256(), 0
    try:
        with _open(src, name) as stream, open(dest, "wb") as out:
            while got <= size:
                block = stream.read(min(BLOCK, size + 1 - got))
                if not block:
                    break
                got += len(block)
                digest.update(block)
                out.write(block)
    except (OSError, http.client.HTTPException) as err:
        raise ReleaseError(f"Downloading {name} from {where} failed ({err}); check your internet "
                           "connection and free disk space, then try again.") from None
    if got < size:
        raise ReleaseError(f"The download of {name} was cut off ({got:,} of {size:,} bytes); "
                           "check your internet connection, then try again.")
    if got > size or digest.hexdigest() != sha256:
        raise ReleaseError(f"{name} from {where} is damaged (its size or sha256 does not match "
                           "the release's list); try again, and if it happens again the release "
                           "itself is damaged.")


def _member_problem(info: zipfile.ZipInfo, root: Path, seen: set) -> str | None:
    """Why zip member `info` must not be unpacked into `root`; None when it is safe."""
    name = info.filename
    if "\\" in name:
        return "a backslash in its path"
    if ":" in name:
        return "a drive letter or stream name"
    if name.startswith("/"):
        return "an absolute path"
    if any(c in _NOT_ON_WINDOWS or ord(c) < 32 for c in name):
        return "a character Windows does not allow"
    parts = (name[:-1] if info.is_dir() else name).split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "an empty, . or .. part in its path"
    if any(p != p.rstrip(". ") for p in parts):
        return "a name ending in a dot or space"
    if any(p.split(".")[0].lower() in _DEVICES for p in parts):
        return "a Windows device name"
    if stat.S_ISLNK(info.external_attr >> 16):
        return "a symbolic link"
    if info.flag_bits & 0x1:
        return "it is encrypted"
    key = "/".join(parts).casefold()
    if key in seen:
        return "a second entry with the same name"
    seen.add(key)
    if not root.joinpath(*parts).resolve().is_relative_to(root):
        return "it leads outside the folder"
    return None


def _unpack(zip_path: Path, dest: Path, name: str) -> None:
    """Unpack the zip into `dest` (created here) after every member was checked."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.infolist()
            root, seen = dest.resolve(), set()
            for info in members:
                why = _member_problem(info, root, seen)
                if why:
                    raise ReleaseError(f"{name} holds an unsafe entry {info.filename!r} ({why}), "
                                       "so nothing was installed; the release is damaged or "
                                       "tampered with.")
            dest.mkdir(parents=True)
            for info in members:
                parts = (info.filename[:-1] if info.is_dir() else info.filename).split("/")
                target = dest.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "xb") as out:
                    shutil.copyfileobj(src, out, BLOCK)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError) as err:
        raise ReleaseError(f"{name} is not a usable zip ({err}); try again, and if it happens "
                           "again the release itself is damaged.") from None
    except OSError as err:
        raise ReleaseError(f"Unpacking {name} into {dest} failed ({err}); free some disk space "
                           "or close anything using that folder, then try again.") from None


def _check_mod(folder: Path, name: str) -> None:
    try:
        manifest = json.loads((folder / "mod.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    if not isinstance(manifest, dict):
        raise ReleaseError(f"{name} has no readable mod.json at its top; the release is damaged.")


def _check_runtime(folder: Path, name: str, settings: Settings, fastball: str) -> None:
    try:
        runtime = toolkit.load_runtime(dataclasses.replace(settings, runtime_dir=folder))
    except toolkit.ToolkitError as err:
        raise ReleaseError(f"{name} does not hold a usable runtime ({err}); the release is "
                           "damaged.") from None
    if runtime["fastball"] != fastball:
        raise ReleaseError(f"{name} was made for Fastball {runtime['fastball']}, but the "
                           f"release's list says {fastball}; the release is damaged.")


# --- the journaled swap ----------------------------------------------------------------------
def _journal_path(settings: Settings) -> Path:
    return Path(settings.state) / JOURNAL


def _sibling(folder: Path, suffix: str) -> Path:
    return folder.with_name(folder.name + suffix)


def _move(src: Path, dst: Path) -> None:
    """os.replace, retried while another handle briefly holds the folder (Windows)."""
    for attempt in range(config.REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == config.REPLACE_TRIES - 1:
                raise
            time.sleep(config.REPLACE_WAIT)


def _writable(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, onexc=_writable)


def _clean(settings: Settings) -> None:
    """Remove staging and temp folders no journal needs (best effort: whatever cannot be deleted
    now is tried again next time, and _commit refuses to move a folder onto it)."""
    mods_dir, runtime = Path(settings.mods), Path(settings.runtime_dir)
    leftovers = [mods_dir / INCOMING, mods_dir / OUTGOING, _sibling(runtime, ".new"),
                 _sibling(runtime, ".old")]
    state = Path(settings.state)
    if state.is_dir():
        leftovers += state.glob(DOWNLOADS + "*")
    for leftover in leftovers:
        try:
            if leftover.exists():
                _rmtree(leftover)
        except OSError:
            pass


def _undo_move(src: Path, dst: Path) -> None:
    """Undo the move src -> dst: move dst back to src. Already undone (src there, dst not):
    nothing. Anything else is not what the journal says happened: ReleaseError, nothing moved."""
    if dst.exists() and not src.exists():
        _move(dst, src)
    elif not (src.exists() and not dst.exists()):
        raise ReleaseError(f"Cannot tell which of {src} and {dst} holds the mods' files of the "
                           f"unfinished download (both or neither are there); move the right "
                           f"one back to {src}, delete the other, then try again.")


def _roll_back(settings: Settings, log: Log) -> None:
    """Undo the moves the journal records as done, newest first, recording each undo; then
    rewrite installed.json as it was and delete the journal. Only the one move that may have
    happened without being recorded (the process stopped between the move and the journal
    write) is judged from the folders: its destination was made free before the journal was
    written, so "destination there, source gone" means it happened. OSError when a folder cannot
    be moved (the journal then stays for the next try)."""
    journal = _journal_path(settings)
    try:
        doc = json.loads(journal.read_text(encoding="utf-8"))
        moves = [(Path(src), Path(dst)) for src, dst in doc["moves"]]
        done, before = doc["done"], doc["installed"]
        if type(done) is not int or not 0 <= done <= len(moves) or not isinstance(before, dict):
            raise ValueError("its progress is not valid")
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise ReleaseError(f"The record of an unfinished mod download {journal} cannot be read "
                           f"({err}); delete it, then get your mods again.") from None
    if done < len(moves):
        src, dst = moves[done]
        if dst.exists() and not src.exists():
            done += 1
    while done > 0:
        _undo_move(*moves[done - 1])
        done -= 1
        write_json_atomic(journal, dict(doc, done=done))
    write_json_atomic(_installed_path(settings), before)
    journal.unlink()
    log("Put the mods back as they were before the unfinished download.")


def recover(settings: Settings, log: Log | None = None) -> None:
    """Put back what an interrupted install or remove left half done (its journal), then
    remove staging and temp folders. install() and remove() run this first."""
    with _LOCK:
        if _journal_path(settings).exists():
            try:
                _roll_back(settings, log or (lambda _line: None))
            except OSError as err:
                raise ReleaseError(f"Putting back the mods of an unfinished download failed "
                                   f"({err}); close anything using {settings.mods} or "
                                   f"{settings.runtime_dir}, then try again.") from None
        _clean(settings)


def _free(path: Path) -> None:
    """Make sure nothing is at `path` (a leftover of an earlier swap); ReleaseError naming it
    when it cannot be deleted."""
    if not (path.exists() or path.is_symlink()):
        return
    try:
        _rmtree(path)
    except OSError:
        pass
    if path.exists() or path.is_symlink():
        raise ReleaseError(f"{path} is left over from an earlier download and cannot be deleted; "
                           "close anything using it (a file window, an editor), or delete it "
                           "yourself, then try again. Nothing was changed.")


def _commit(settings: Settings, swaps: list[tuple[Path, Path | None, Path]], before: dict,
            after: dict, log: Log) -> None:
    """Swap every (target, new folder or None to remove, where the old one goes) and write
    installed.json as `after`, all or nothing. Every destination is made free first (else
    ReleaseError, nothing changed); the journal lists the moves and how many are done,
    rewritten after each one."""
    moves = []
    for target, new, old in swaps:
        if target.exists():
            _free(old)
            moves.append((target, old))
        elif target.is_symlink():
            raise ReleaseError(f"{target} is a broken link; delete it, then try again.")
        if new is not None:
            moves.append((new, target))
    for _src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
    journal = _journal_path(settings)
    doc = {"moves": [[str(src), str(dst)] for src, dst in moves], "done": 0, "installed": before}
    current = journal
    try:
        write_json_atomic(journal, doc)
        for done, (src, dst) in enumerate(moves, 1):
            current = dst
            _move(src, dst)
            current = journal
            write_json_atomic(journal, dict(doc, done=done))
        current = _installed_path(settings)
        write_json_atomic(current, after)
        current = journal
        journal.unlink()
    except OSError as err:
        try:
            _roll_back(settings, log)
        except (OSError, ReleaseError) as again:
            raise ReleaseError(f"Putting the new files in place failed at {current} ({err}), and "
                               f"putting the old ones back failed too ({again}); close anything "
                               f"using {settings.mods} or {settings.runtime_dir}, then try again "
                               "(the manager finishes putting them back first).") from None
        raise ReleaseError(f"Putting the new files in place failed at {current} ({err}); "
                           "everything was put back as it was. Close anything using that folder "
                           "(the game, a file window, an editor), then try again.") from None
    _clean(settings)


# --- install and remove ------------------------------------------------------------------------
def _download_source(settings: Settings, release: str) -> str:
    if settings.release_source:
        return settings.release_source
    info = release_info()
    return source(settings, dict(info, tag=release) if info else None)


def install(ids, index: dict, settings: Settings, log: Log) -> None:
    """Download and install the mods `ids` of release `index` (and its runtime when the
    installed one is not this release's, intact). Releases move together: every mod installed
    from another release is updated to this one in the same call. All or nothing: any failure
    raises ReleaseError and leaves the mod folders, runtime_dir and installed.json as they were."""
    with _LOCK:
        recover(settings, log)
        index = _check_index(index, INDEX)
        release = index["release"]
        offered = {m["id"]: m for m in index["mods"]}
        ids = list(dict.fromkeys(ids))
        for mod_id in ids:
            if mod_id not in offered:
                raise ReleaseError(f"Release {release} has no mod {mod_id!r}; refresh the list of "
                                   "mods, then try again.")
        records = installed(settings)
        for tag in (runtime_release(settings), *records.values()):
            if tag and _older(release, tag):
                raise ReleaseError(f"Your mods are from release {tag}, newer than release "
                                   f"{release}; get mods from release {tag} or later.")
        mods_dir = Path(settings.mods)
        todo = [i for i in ids if records.get(i) != release or not (mods_dir / i).is_dir()]
        todo += [i for i, tag in records.items() if tag != release and i not in todo]
        for mod_id in todo:
            if mod_id not in offered:
                raise ReleaseError(f"Release {release} no longer has the mod {mod_id!r} you got "
                                   f"from release {records[mod_id]}, and mods of one release go "
                                   "together; turn it off and remove it, then try again.")
            folder = mods_dir / mod_id
            if mod_id not in records and (folder.exists() or folder.is_symlink()):
                raise ReleaseError(f"The mod folder {folder} was not downloaded by the manager, so "
                                   f"it is left alone; move it out of {mods_dir} to download "
                                   f"{offered[mod_id]['name']}.")
        runtime = index["runtime"] if not _runtime_current(settings, index) else None
        if not todo and runtime is None:
            log(f"Everything is already from release {release}.")
            return
        src = _download_source(settings, release)
        state = Path(settings.state)
        try:
            state.mkdir(parents=True, exist_ok=True)
            temp = Path(tempfile.mkdtemp(prefix=DOWNLOADS, dir=state))
        except OSError as err:
            raise ReleaseError(f"Cannot make a download folder in {state} ({err}); free some "
                               "disk space, then try again.") from None
        try:
            entries = [offered[i] for i in todo] + ([runtime] if runtime else [])
            for entry in entries:
                _fetch(src, entry["file"], temp / entry["file"], entry["sha256"], entry["size"], log)
            swaps = []
            try:
                for mod_id in todo:
                    name = offered[mod_id]["file"]
                    new = mods_dir / INCOMING / mod_id
                    log(f"Unpacking {name}")
                    _unpack(temp / name, new, name)
                    _check_mod(new, name)
                    swaps.append((mods_dir / mod_id, new, mods_dir / OUTGOING / mod_id))
                if runtime:
                    target = Path(settings.runtime_dir)
                    new, name = _sibling(target, ".new"), runtime["file"]
                    log(f"Unpacking {name}")
                    _unpack(temp / name, new, name)
                    _check_runtime(new, name, settings, index["fastball"])
                    write_json_atomic(new / RUNTIME_MARK,
                                      {"release": release, "sha256": runtime["sha256"]})
                    target.parent.mkdir(parents=True, exist_ok=True)
                    swaps.append((target, new, _sibling(target, ".old")))
            except OSError as err:
                raise ReleaseError(f"Getting the mods ready failed ({err}); free some disk space "
                                   "or close anything using the mods folder, then try "
                                   "again.") from None
            _commit(settings, swaps, records, {**records, **{i: release for i in todo}}, log)
        except Exception:
            if not _journal_path(settings).exists():
                _clean(settings)
            raise
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        got = ", ".join(offered[i]["name"] for i in todo)
        log(f"Installed release {release}: " + " and ".join(
            p for p in (got, "the runtime" if runtime else "") if p) + ".")


def remove(mod_id: str, settings: Settings, log: Log) -> None:
    """Delete a downloaded mod's folder and its record. Only for a mod that is switched off and
    whose files step reports not installed; ReleaseError otherwise, changing nothing."""
    with _LOCK:
        recover(settings, log)
        records = installed(settings)
        folder = Path(settings.mods) / mod_id
        if mod_id not in records:
            raise ReleaseError(f"{folder} was not downloaded by the manager, so it is not removed "
                               "here; delete the folder yourself if you no longer want it.")
        mod = None
        if folder.exists():
            try:
                mod = next((m for m in mods.load_all(settings) if m.id == mod_id), None)
            except (mods.ManifestError, OSError) as err:
                raise ReleaseError(f"Cannot read the mods in {settings.mods} ({err}); fix or "
                                   "delete the mod folder it names, then try again.") from None
        name = mod.name if mod else mod_id
        if _is_on(mod_id, settings):
            raise ReleaseError(f"{name} is switched on; turn it off first, then remove it.")
        if mod_id in {i for _action, i in applier.pending_from_journal(settings)}:
            raise ReleaseError(f"{name} is off, but its last install or uninstall step did not "
                               "finish, so some of its files may still be in the game; press Play "
                               "once so they are taken out, then remove it.")
        if mod and mod.files:
            try:
                in_game = applier.is_installed(mod, settings)
            except applier.StepFailed as err:
                raise ReleaseError(f"Cannot tell whether {name}'s files are still in the game "
                                   f"({err}); fix that first, then remove it.") from None
            if in_game:
                raise ReleaseError(f"{name} is off, but its files are still in the game; press "
                                   "Play once so they are taken out, then remove it.")
        swaps = [(folder, None, Path(settings.mods) / OUTGOING / mod_id)] if folder.exists() else []
        after = {k: v for k, v in records.items() if k != mod_id}
        _commit(settings, swaps, records, after, log)
        log(f"Removed {name} ({folder}).")
