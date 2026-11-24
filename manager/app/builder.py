"""The combined game exe, compiled into settings.build_out. Dev mode: integration branch
manager/build (main + every runtime mod branch) in its own worktree, its game code
translated by that worktree's recomp/gen.py with every mod's code patches. Player mode (no
source clone): the same from builds\\toolkit, the Fastball setup with the release's prebuilt
runtime laid over it (app/toolkit.py), linking a copy of that runtime's libmlbrt.a with
runtime/core/patches.c compiled into it against this build's own gen/patches.h (the patch-set
table depends on which mods' code patches the build translates, so the release cannot
prebuild it)."""
import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config, mods, patchsets, procs, toolkit
from .config import MANAGER, Settings, write_json_atomic

GAME_VERSION = "1.0.20"
TAIL_LINES = 40
PITCH_BRANCH = "mod/pitch-types"
# Several mod branches carry the test driver (copied between them, or inherited by a branch cut
# from manager/build); the pitch branch's copy is the superset. When a merge conflicts only in
# these files and the pitch branch is part of the build, they are taken from the pitch branch.
PITCH_WINS = {"runtime/mlb/hid.c", "runtime/gfx/gfx_stubs.c"}
# Runtime files from the game install that must sit beside mlb.exe.
REFRESHED = ["totk_xlate.exe", "totk_xlate.id"]  # kept identical, with every DLL but these:
SKIPPED_DLLS = {"sdl3.dll"}  # lower case; tools/build.py puts its own SDL3 beside the exe
KEPT = ["mlb.cfg", "pipeline-cache.bin", "shader-cache"]  # copied once, then the game's
PACKAGES = "packages"  # the offline Packages server folder (0.2 runtime), kept in sync
# The modules the installer translates (installer/steps.py; the ExeFS has no subsdk0).
GEN_MODULES = "main,sdk"
GEN_KEY = ".gen-key"
GEN_MARKER = ".manager-gen"  # written when the manager first makes the gen folder: the folder is ours
# In the gen folder but not made by gen.py: kept by the sync (tools/build.py writes the icons there).
GEN_KEEP = {GEN_KEY, GEN_MARKER, "prompt_icons.h"}
# The manager's own build folder: the generated C and the combined patch file live here whatever
# build_out says (a sync deletes files, so never in a folder a setting can point elsewhere).
BUILDS = MANAGER / "builds"
MISSING = "missing"  # the head of a branch that does not exist (tolerated for switched-off mods)


class BuildFailed(Exception):
    """The combined build failed; the previous exe and build-info.json are kept."""


def _identity() -> list[str]:
    """`-c user.name=.. -c user.email=..` from the manager repo, for merge commits."""
    args = []
    for key in ("user.name", "user.email"):
        try:
            value = subprocess.run(["git", "-C", str(MANAGER), "config", key],
                                   capture_output=True, text=True, errors="replace",
                                   creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        except OSError:
            value = ""
        if value:
            args += ["-c", f"{key}={value}"]
    return args


def _stream(argv: list[str], cwd, log: Callable[[str], None]) -> tuple[int, str]:
    """Run argv (no shell) in cwd, sending each output line to log; (exit code, output)."""
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as err:
        raise BuildFailed(f"cannot start {argv} in {cwd}: {err}") from err
    lines = []
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        lines.append(line)
        log(line)
    proc.stdout.close()
    return proc.wait(), "\n".join(lines)


def _git(args: list[str], cwd, log: Callable[[str], None]) -> tuple[int, str]:
    """git <args> in the worktree cwd. Replaced in tests."""
    log("$ git " + " ".join(args))
    return _stream(["git"] + _identity() + ["-C", str(cwd)] + args, cwd, log)


def _run_build(argv: list[str], cwd, log: Callable[[str], None]) -> tuple[int, str]:
    """The compiler run. Replaced in tests."""
    log("$ " + " ".join(argv))
    return _stream(argv, cwd, log)


def _run_tool(argv: list[str], cwd, log: Callable[[str], None]) -> tuple[int, str]:
    """A compiler or archiver run of a player build (patches.c). Replaced in tests."""
    log("$ " + " ".join(argv))
    return _stream(argv, cwd, log)


def _run_gen(argv: list[str], cwd, log: Callable[[str], None]) -> tuple[int, str]:
    """The generator run (recomp/gen.py), its lines marked "[gen]". Replaced in tests."""
    log("[gen] $ " + " ".join(argv))
    return _stream(argv, cwd, lambda line: log("[gen] " + line))


def gen_dir(settings: Settings | None = None) -> Path:
    """The manager's own generated C, MANAGER\\builds\\gen (never derived from build_out)."""
    return BUILDS / "gen"


def runtime_lib_dir() -> Path:
    """Player mode: MANAGER\\builds\\runtime-lib, the per-build copy of the runtime library with
    this build's patches.o in it."""
    return BUILDS / "runtime-lib"


def combined_patch_path(settings: Settings | None = None) -> Path:
    """Every mod's code patches in one file, for gen.py --patches (MANAGER\\builds)."""
    return BUILDS / "patches.txt"


def _exefs(settings: Settings) -> Path:
    return Path(settings.game) / "game" / "exefs"


def gen_python(settings: Settings) -> str:
    """The Python for recomp/gen.py, which needs numpy and lz4: the setup's bundled one
    (the release's, tools/requirements-release.txt) when present, else the manager's."""
    bundled = Path(settings.setup) / "python" / "python.exe"
    return str(bundled) if bundled.is_file() else procs.python_exe()


def _player(settings: Settings) -> bool:
    return config.mode(settings) == "player"


def gen_command(settings: Settings, out: Path, patch_file: Path | None,
                root: Path | None = None) -> list[str]:
    """recomp/gen.py of the worktree (run from it), or with `root` (player mode: the
    toolkit) <root>/recomp/gen.py through procs.BOOT, which puts <root>/recomp first on
    sys.path: the setup Python's ._pth lists the setup's own recomp/ (its patches.py)."""
    # -X utf8: gen.py reads the patch file with the locale's code page otherwise
    script = (["recomp/gen.py"] if root is None else
              ["-c", procs.BOOT, str(Path(root) / "recomp" / "gen.py")])
    argv = ([gen_python(settings), "-I", "-X", "utf8"] + script
            + ["--exefs", str(_exefs(settings)), "--out", str(out), "--modules", GEN_MODULES])
    if patch_file is not None:
        argv += ["--patches", str(patch_file)]
    return argv


def _from_toolkit(path, root: Path) -> str:
    """path relative to the toolkit `root`, with forward slashes; ValueError on another drive."""
    return Path(os.path.relpath(path, root)).as_posix()


def build_command(settings: Settings, root: Path | None = None,
                  runtime_lib: str = "runtime/libmlbrt.a") -> list[str]:
    """tools/build.py, run from the worktree; or in player mode from `root`, the toolkit,
    linking the prebuilt runtime library `runtime_lib` (relative to the toolkit) instead of
    compiling the runtime. tools/build.py
    writes --gen, --out, --deps and --runtime-lib into the compiler and ninja lines relative
    to its own folder (spaces refused there), so player mode passes them relative to the
    toolkit (deps and library inside it); the toolchain and ninja stay absolute, as
    tools/build.py quotes clang, clang++, windres and llvm-ar and runs ninja from a list."""
    setup = Path(settings.setup)
    if root is not None:
        return [procs.python_exe(), "-I", "tools/build.py",
                "--toolchain", str(setup / "llvm"),
                "--deps", "runtime/deps",
                "--ninja", str(setup / "bin/ninja.exe"),
                "--runtime-lib", runtime_lib,
                "--gen", _from_toolkit(gen_dir(settings), root),
                "--version", GAME_VERSION, "--out", _from_toolkit(settings.build_out, root)]
    return [procs.python_exe(), "-I", "tools/build.py",
            "--toolchain", str(setup / "llvm"),
            "--deps", str(setup / "runtime/deps"),
            "--ninja", str(setup / "bin/ninja.exe"),
            "--gen", str(gen_dir(settings)),
            "--version", GAME_VERSION, "--out", str(settings.build_out)]


def heads(branches: list[str], settings: Settings, missing_ok=()) -> dict[str, str]:
    """{branch: commit sha} in the recomp repo; a branch in missing_ok that does not
    exist is MISSING, any other is BuildFailed."""
    if settings.recomp is None:
        raise BuildFailed("Building from source needs the Recomp source folder set in Settings.")
    result = {}
    for branch in branches:
        try:
            proc = subprocess.run(["git", "-C", str(settings.recomp), "rev-parse",
                                   "--verify", "--quiet", branch + "^{commit}"],
                                  capture_output=True, text=True, errors="replace",
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as err:
            raise BuildFailed(f"cannot run git: {err}") from err
        if proc.returncode != 0 or not proc.stdout.strip():
            if branch in missing_ok:
                result[branch] = MISSING
                continue
            raise BuildFailed(f"branch {branch!r} not found in {settings.recomp}")
        result[branch] = proc.stdout.strip()
    return result


def _runtime_branches(settings: Settings) -> list[str]:
    return [m.runtime["branch"] for m in mods.load_all(settings)
            if m.runtime and m.runtime.get("branch")]


def _info_path(settings: Settings) -> Path:
    return Path(settings.build_out) / "build-info.json"


def _error_path(settings: Settings) -> Path:
    return Path(settings.state) / "build-error.txt"


def _patch_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def exefs_stamp(settings: Settings) -> str:
    """The ExeFS main as "<size>:<mtime_ns>" ("missing" when absent): a game update changes it."""
    try:
        st = (_exefs(settings) / "main").stat()
    except OSError:
        return "missing"
    return f"{st.st_size}:{st.st_mtime_ns}"


def builtin_patches(settings: Settings, root: Path | None = None) -> patchsets.Builtin | None:
    """The release's own patch file, tools/data/patches-<game version>.txt (the installer
    translates with it: 0.2's fixes), in `root` (the tree the build runs from), else in
    the build worktree, or in player mode the toolkit (the setup before one is laid out);
    None when absent."""
    label = f"tools/data/patches-{GAME_VERSION}.txt"
    if root is None and _player(settings):
        if settings.setup is None:
            return None
        made = toolkit.folder()
        root = made if (made / label).is_file() else settings.setup
    elif root is None:
        if settings.build_wt is None:
            return None
        root = settings.build_wt
    path = Path(root) / label
    return patchsets.Builtin(path, label) if path.is_file() else None


def _needed(branches: list[str], all_mods, settings: Settings, missing_ok=(),
            builtin: patchsets.Builtin | None = None) -> tuple[dict, str, list, list]:
    """(needed, the mods' combined patch text, its spans, patch sets): the branch heads
    (MISSING for a branch in missing_ok that does not exist; in player mode instead
    "runtime", runtime.json's sha256), the ExeFS main's stamp ("exefs"), plus "patches"
    (the mods' combined text's sha256) when any mod has code patches; the built-in file is
    covered by main's head (in player mode by the setup's VERSION, which must equal the
    runtime's Fastball release). The patch sets list the built-in file's first when it
    is given. PatchSetError when a patch file cannot be
    read."""
    if _player(settings):
        needed = {"runtime": toolkit.runtime_sha256(settings)}
    else:
        needed = heads(["main"] + list(branches), settings, missing_ok=frozenset(missing_ok))
    needed["exefs"] = exefs_stamp(settings)
    text, spans = patchsets.combine(all_mods)
    if spans:
        needed["patches"] = _patch_hash(text)
    return needed, text, spans, patchsets.collect(all_mods, check=False, builtin=builtin)


def state(settings: Settings) -> dict:
    """The combined exe's state. Cheap enough for every UI poll: the branch heads (git
    rev-parse in the recomp repo; in player mode runtime.json's sha256 and no git), the
    mods' small patch files and the built-in one, nothing more."""
    player = _player(settings)
    if player and settings.setup is None:
        raise BuildFailed(toolkit.SETUP_UNSET)
    exe = Path(settings.build_out) / "mlb.exe"
    exe = str(exe) if exe.is_file() else None
    try:
        info = json.loads(_info_path(settings).read_text(encoding="utf-8"))
        built_from = info["built_from"]
        left_out = info.get("left_out") or {}
        if not isinstance(left_out, dict):
            left_out = {}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        built_from, left_out = None, {}
    all_mods = mods.load_all(settings)
    try:
        branches = [] if player else _runtime_branches(settings)
        needed, _, _, sets = _needed(branches, all_mods, settings, missing_ok=branches,
                                     builtin=builtin_patches(settings))
    except patchsets.PatchSetError as err:
        raise BuildFailed(str(err)) from err
    try:
        last_error = _error_path(settings).read_text(encoding="utf-8")
    except OSError:
        last_error = None
    return {"exe": exe, "built_from": built_from, "needed": needed,
            "up_to_date": exe is not None and built_from == needed,
            "last_error": last_error, "patch_sets": sets, "left_out": left_out}


def _failed(head: str, output: str) -> str:
    return procs.failure_text(head, output, TAIL_LINES)


def _tidy(wt: Path, branch: str, log) -> None:
    """After leaving `branch` out: no merge in progress, no tracked changes, no untracked
    `path~branch` copies a conflict left behind. Falls back to `git reset --hard HEAD`;
    BuildFailed when the worktree is still not clean."""
    suffixes = tuple("~" + name for name in {branch, branch.replace("/", "_")})
    for attempt in (1, 2):
        _, others = _git(["ls-files", "--others", "--exclude-standard"], wt, log)
        for rel in (line.strip() for line in others.splitlines()):
            if rel and rel.endswith(suffixes):
                try:
                    (Path(wt) / rel).unlink(missing_ok=True)
                except OSError as err:
                    raise BuildFailed(f"cannot remove {rel} left by merging {branch} in {wt}: "
                                      f"{err}") from err
        merging, _ = _git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], wt, log)
        _, changes = _git(["status", "--porcelain", "-uno"], wt, log)
        if merging != 0 and not changes.strip():
            return
        if attempt == 1:
            log(f"The worktree is not clean after leaving out {branch}: git reset --hard HEAD")
            _git(["reset", "--hard", "HEAD"], wt, log)
    raise BuildFailed(f"leaving out {branch} left {wt} with a merge in progress or changes "
                      f"that git reset --hard HEAD did not clear; clean it up, then build again")


def _merge(branches: list[str], wt: Path, log, optional=frozenset(), owners: dict | None = None,
           missing=frozenset(), _pitch_gone: str | None = None) -> dict[str, dict]:
    """manager/build = main + every required branch (mod order), then every optional one
    (mod order): a switched-off mod's branch that merges cleanly then cannot stand in the
    way of a switched-on one. An optional branch (a switched-off mod's) whose merge
    conflicts or fails, or that is in `missing`, is left out (its merge aborted, the
    worktree checked clean); any other failing branch is BuildFailed, named with its mod
    from `owners` ({branch: mod name}) when known.
    Returns {left-out branch: {"files": conflicted paths, "reason": why}}.

    The test-driver rule (PITCH_WINS conflicts take PITCH_BRANCH's copy) is used only when
    the pitch branch is merged already, is the branch being merged, or comes later (and is
    not missing). When a later optional pitch branch lent its copy and is then left out
    itself, the whole merge is redone (`_pitch_gone` = why it was left out) lending only
    from a merged or required pitch branch."""
    owners = owners or {}
    order = ([b for b in branches if b not in optional]
             + [b for b in branches if b in optional])
    code, out = _git(["reset", "--hard", "main"], wt, log)
    if code != 0:
        raise BuildFailed(_failed(f"git reset --hard main failed in {wt}", out))
    merged, left_out, lent = set(), {}, False

    def leave_out(branch, files, reason, why):
        left_out[branch] = {"files": files, "reason": reason}
        log(f"Left out {branch}: {why}; the mod is off, so the build goes on without it")

    for i, branch in enumerate(order):
        who = f" (mod {owners[branch]!r})" if branch in owners else ""
        if branch in missing:
            if branch not in optional:
                raise BuildFailed(f"branch {branch!r}{who} not found")
            leave_out(branch, [], "branch not found", "its branch was not found")
            continue
        code, out = _git(["merge", "--no-edit", branch], wt, log)
        if code == 0:
            merged.add(branch)
            continue
        _, listed = _git(["diff", "--name-only", "--diff-filter=U"], wt, log)
        conflicted = [p.strip() for p in listed.splitlines() if p.strip()]
        pitch_ahead = (PITCH_BRANCH in order[i + 1:] and PITCH_BRANCH not in missing
                       and (_pitch_gone is None or PITCH_BRANCH not in optional))
        pitch_there = PITCH_BRANCH in merged or branch == PITCH_BRANCH or pitch_ahead
        driver_only = bool(conflicted) and set(conflicted) <= PITCH_WINS
        if pitch_there and driver_only:
            for step in (["checkout", PITCH_BRANCH, "--"] + conflicted,
                         ["add", "--"] + conflicted, ["commit", "--no-edit"]):
                code, step_out = _git(step, wt, log)
                if code != 0:
                    _git(["merge", "--abort"], wt, log)
                    raise BuildFailed(_failed(f"resolving {branch} failed at git "
                                              f"{' '.join(step)}", step_out))
            if PITCH_BRANCH not in merged and branch != PITCH_BRANCH:
                lent = True
            merged.add(branch)
            continue
        _git(["merge", "--abort"], wt, log)
        if branch in optional:
            _tidy(wt, branch, log)
            if conflicted:
                why = f"its merge conflicts in {', '.join(conflicted)}"
                leave_out(branch, conflicted, "conflicts", why)
            else:
                last = (out.strip().splitlines() or ["no output"])[-1]
                why = f"its merge failed ({last})"
                leave_out(branch, [], f"merge failed: {last}", why)
            if branch == PITCH_BRANCH and lent:
                log(f"An earlier branch took the test driver from {PITCH_BRANCH}, which is "
                    f"left out: merging again without it")
                return _merge(branches, wt, log, optional, owners, missing, _pitch_gone=why)
            continue
        if conflicted:
            text = f"merging {branch}{who} conflicts in: {', '.join(conflicted)}"
            pitch_why = (_pitch_gone if _pitch_gone is not None else
                         "its branch was not found" if PITCH_BRANCH in missing else None)
            if driver_only and pitch_why:
                text += (f"; it needs {PITCH_BRANCH}' test driver, which was left out "
                         f"({pitch_why})")
            raise BuildFailed(text)
        raise BuildFailed(_failed(f"git merge {branch}{who} failed", out))
    return left_out


def _same_stat(a: Path, b: Path) -> bool:
    """Whether b exists as a file with a's size and mtime."""
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return b.is_file() and sa.st_size == sb.st_size and sa.st_mtime_ns == sb.st_mtime_ns


def _install_dlls(src_dir: Path) -> list[str]:
    """Every *.dll beside the install's mlb.exe (DLSS, AMD FSR, ...) but SDL3.dll, which
    tools/build.py puts beside the combined exe itself."""
    try:
        return sorted(p.name for p in src_dir.glob("*.dll")
                      if p.is_file() and p.name.lower() not in SKIPPED_DLLS)
    except OSError:
        return []


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DIRECTORY = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)


def _is_link(path) -> bool:
    """Whether path is a symlink, junction or any other reparse point (never followed)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _remove_link(path) -> None:
    """Remove the link itself, never what it points to: rmdir for a directory link or
    junction, unlink for a file link."""
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & _DIRECTORY:
        os.rmdir(path)
    else:
        os.unlink(path)


def _walk(root: Path):
    """(path, kind) under root, parents before children, kind "file", "dir" or "link";
    links (symlinks, junctions, reparse points) are leaves, never descended into."""
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except FileNotFoundError:
        return
    for entry in entries:
        path = Path(entry.path)
        if _is_link(path):
            yield path, "link"
        elif entry.is_dir(follow_symlinks=False):
            yield path, "dir"
            yield from _walk(path)
        elif entry.is_file(follow_symlinks=False):
            yield path, "file"


def _remove_tree(path: Path) -> None:
    """Remove a real folder inside build_out\\packages; links in it are removed, not followed."""
    for child, kind in sorted(_walk(path), key=lambda pk: len(pk[0].parts), reverse=True):
        if kind == "link":
            _remove_link(child)
        elif kind == "dir":
            os.rmdir(child)
        else:
            os.unlink(child)
    os.rmdir(path)


def _real_dir(path: Path, root: Path) -> None:
    """Make every folder from root down to path a real folder: a link or a file in the
    way is removed (the link itself), never written through."""
    for part in [root] + [root.joinpath(*path.relative_to(root).parts[:i + 1])
                          for i in range(len(path.relative_to(root).parts))]:
        if _is_link(part):
            _remove_link(part)
        elif part.exists() and not part.is_dir():
            os.unlink(part)
        if not part.exists():
            os.mkdir(part)


def sync_packages(src: Path, dst: Path, log: Callable[[str], None]) -> tuple[int, int]:
    """Make dst (build_out\\packages) hold src's files (the install's packages folder, the
    offline Packages server the runtime starts): missing or different (size/mtime) ones
    copied, ones gone from src removed, empty folders gone from src removed. Links are
    never followed: in dst the link itself is removed (and the real file copied in its
    place), in src it is skipped with a log line, so nothing outside dst is touched.
    __pycache__ folders the server's Python writes are kept. (copied, removed).
    BuildFailed naming the file when one cannot be replaced (an old packages server
    still running from the combined exe holds it)."""
    copied = removed = 0
    path = dst
    try:
        files, dirs = {}, set()
        for path, kind in _walk(src):
            rel = path.relative_to(src)
            if kind == "link":
                log(f"stage: {path} is a link; skipped")
            elif kind == "dir":
                dirs.add(rel)
            else:
                files[rel] = path
        path = dst
        _real_dir(dst, dst)
        # links first, then files gone from src: afterwards dst holds no link at all
        for path, kind in list(_walk(dst)):
            rel = path.relative_to(dst)
            if "__pycache__" in rel.parts and kind != "link":
                continue
            if kind == "link":
                _remove_link(path)
                removed += 1
            elif kind == "file" and rel not in files:
                os.unlink(path)
                removed += 1
        for rel, s in files.items():
            d = path = dst / rel
            _real_dir(d.parent, dst)
            if _is_link(d):
                _remove_link(d)
            elif d.is_dir():
                _remove_tree(d)
            if _same_stat(s, d):
                continue
            shutil.copy2(s, d)
            copied += 1
        for path, kind in sorted(_walk(dst), key=lambda pk: len(pk[0].parts), reverse=True):
            if kind == "dir" and path.relative_to(dst) not in dirs and not any(path.iterdir()):
                os.rmdir(path)
    except OSError as err:
        where = err.filename or path
        raise BuildFailed(f"cannot update {where} in {dst} ({err}); an old packages server "
                          f"started by the combined exe may still be running: close the game and "
                          f"that server (python.exe in {dst}), then try again") from err
    if copied or removed:
        log(f"stage: packages {src} -> {dst}: {copied} file(s) copied, {removed} removed")
    return copied, removed


def stage_runtime(settings: Settings, log: Callable[[str], None]) -> None:
    """Copy the files the runtime needs beside mlb.exe from the game install:
    the translator and every DLL (DLSS, FSR; not SDL3) when missing or changed
    (size/mtime), the packages folder kept in sync, the config and warm caches only
    when missing (the game writes its own copies afterwards)."""
    src_dir, out = Path(settings.game), Path(settings.build_out)
    out.mkdir(parents=True, exist_ok=True)
    for name in REFRESHED + _install_dlls(src_dir) + KEPT:
        src, dst = src_dir / name, out / name
        if not src.exists():
            log(f"stage: {src} not found, skipped")
            continue
        if name not in KEPT and _is_link(dst):
            _remove_link(dst)  # never write through a link into another folder
        if dst.exists():
            if name in KEPT or _same_stat(src, dst):
                continue
        log(f"stage: {src} -> {dst}")
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    packages = src_dir / PACKAGES
    if packages.is_dir():
        sync_packages(packages, out / PACKAGES, log)
    else:
        log(f"stage: {packages} not found; {out / PACKAGES} left as it is")


def _write_info(settings: Settings, built_from: dict, exe: Path, patch_sets=(),
                left_out: dict | None = None) -> None:
    path = _info_path(settings)
    write_json_atomic(path, {"built_from": built_from, "exe": str(exe),
                             "patch_sets": list(patch_sets), "left_out": left_out or {},
                             "built_at": datetime.now().isoformat(timespec="seconds")})


def _gen_key(text: str, settings: Settings, wt: Path, log, tools_key: str | None = None) -> str:
    """sha256 over what the generated C depends on: the combined patch text, the
    generator's trees after the merge (recomp/ and tools/; in player mode the toolkit's
    stamp, `tools_key`), and the ExeFS main and sdk (size, mtime)."""
    parts = [text]
    for tree in () if tools_key is not None else ("recomp", "tools"):
        code, out = _git(["rev-parse", f"HEAD:{tree}"], wt, log)
        if code != 0 or not out.strip():
            raise BuildFailed(_failed(f"git rev-parse HEAD:{tree} failed in {wt}", out))
        parts.append(out.strip())
    if tools_key is not None:
        parts.append("toolkit " + tools_key)
    for name in ("main", "sdk"):
        st = (_exefs(settings) / name).stat()
        parts += [str(st.st_size), str(st.st_mtime_ns)]
    key = hashlib.sha256()
    for part in parts:
        key.update(part.encode("utf-8") + b"\0")
    return key.hexdigest()


def _claim_gen(gen: Path) -> None:
    """Make sure gen is the manager's own folder before anything is synced into it: new or
    empty, it gets the marker; non-empty without the marker or a gen key, BuildFailed."""
    if gen.is_dir() and any(gen.iterdir()):
        if not ((gen / GEN_MARKER).is_file() or (gen / GEN_KEY).is_file()):
            raise BuildFailed(f"{gen} holds files the manager did not make (no {GEN_MARKER} or "
                              f"{GEN_KEY}); move them away, then build again")
    gen.mkdir(parents=True, exist_ok=True)
    marker = gen / GEN_MARKER
    if not marker.is_file():
        marker.write_text("generated C of Show Mod Manager (recomp/gen.py); synced by app/builder.py\n",
                          encoding="utf-8")


def _remove_leftover(new: Path) -> None:
    """Remove a gen.new left by a killed build; BuildFailed when something still holds it."""
    if not new.exists():
        return
    try:
        shutil.rmtree(new)
    except OSError as err:
        raise BuildFailed(f"a previous translation is still running or holds files in {new}; wait "
                          f"for it to finish or end recomp/gen.py, then retry ({err})") from err


def _same_bytes(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(1 << 20), fb.read(1 << 20)
            if x != y:
                return False
            if not x:
                return True


def sync_gen(src: Path, dst: Path, log) -> tuple[int, int, int]:
    """Make dst hold src's files: changed or new ones moved over, ones gone from src
    deleted (but GEN_KEEP at the top), identical ones untouched so their mtimes stay
    and ninja recompiles only what changed. (copied, removed, unchanged)."""
    dst.mkdir(parents=True, exist_ok=True)
    copied = unchanged = removed = 0
    fresh = set()
    for s in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = s.relative_to(src)
        fresh.add(rel)
        d = dst / rel
        if d.is_file() and _same_bytes(s, d):
            unchanged += 1
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        os.replace(s, d)
        copied += 1
    for d in sorted((p for p in dst.rglob("*") if p.is_file()), reverse=True):
        rel = d.relative_to(dst)
        if rel in fresh or (len(rel.parts) == 1 and rel.name in GEN_KEEP):
            continue
        d.unlink()
        removed += 1
    for folder in sorted((p for p in dst.rglob("*") if p.is_dir()), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    log(f"generated C: {copied} file(s) changed, {removed} removed, {unchanged} unchanged")
    return copied, removed, unchanged


def _generate(text: str, spans, all_mods, settings: Settings, log,
              root: Path | None = None, tools_key: str | None = None) -> None:
    """Translate the game code with the merged worktree's recomp/gen.py (player mode: the
    toolkit `root`'s, keyed by its stamp `tools_key`) into gen_dir, unless the gen key
    says the C there is current. A failure leaves gen_dir as it was."""
    wt, gen = Path(root if root is not None else settings.build_wt), gen_dir(settings)
    new = gen.with_name(gen.name + ".new")
    # dev: tools/build.py runs from the worktree; player mode checked gen relative to the toolkit
    for folder in (gen, new) if root is None else ():
        if " " in str(folder):
            raise BuildFailed(f"the generated C folder must not contain spaces: {folder}")
    _remove_leftover(new)
    _claim_gen(gen)
    patch_file = combined_patch_path(settings)
    if text:
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        with open(patch_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    else:
        patch_file.unlink(missing_ok=True)
    key = _gen_key(text, settings, wt, log, tools_key)
    try:
        current = (gen / GEN_KEY).read_text(encoding="utf-8").strip()
    except OSError:
        current = None
    if current == key and (gen / "layout.h").is_file():
        log(f"Game code translation is up to date (gen key {key[:12]}); generation skipped.")
        return
    log("Translating game code (recomp/gen.py)…")
    try:
        code, output = _run_gen(gen_command(settings, new, patch_file if text else None,
                                            root), wt, log)
        if code != 0 or not (new / "layout.h").is_file():
            head = (f"translating the game code failed (exit {code})" if code != 0 else
                    f"translating the game code finished without {new / 'layout.h'}")
            try:
                layout = json.loads((new / "layout.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                layout = None
            hint = patchsets.locate("\n".join(output.splitlines()[-TAIL_LINES:]),
                                    all_mods, spans, layout)
            if hint:
                head += f" [{hint}]"
            raise BuildFailed(_failed(head, output))
        (gen / GEN_KEY).unlink(missing_ok=True)  # a sync cut short must not pass as current
        sync_gen(new, gen, log)
        (gen / GEN_KEY).write_text(key, encoding="utf-8")
    finally:
        if new.exists():
            shutil.rmtree(new, ignore_errors=True)


PATCHES_C = "runtime/core/patches.c"
RUNTIME_LIB = "runtime/libmlbrt.a"


def runtime_flags(build_py: Path, gen: str, deps: str) -> list[str]:
    """The flags `build_py` (Fastball's tools/build.py) compiles a runtime C file with for
    llvm-mingw, read from its source without running it: RT_FLAGS, what main() appends to
    rt_flags under `if mingw:`, and main()'s inc_rt with its gen and deps folders given here
    (relative to the folder build.py runs from). BuildFailed naming the file when any of them
    is not written the way it is read here, so a Fastball that compiles its runtime another way
    stops the build instead of compiling patches.c differently."""
    def drifted(what: str):
        return BuildFailed(f"{build_py} {what}; the manager compiles the mods' patches.c with "
                           f"the runtime flags of Fastball 0.2.0's tools/build.py, so this setup "
                           f"needs a manager made for it.")

    try:
        tree = ast.parse(Path(build_py).read_text(encoding="utf-8"), str(build_py))
    except (OSError, UnicodeDecodeError, SyntaxError) as err:
        raise drifted(f"cannot be read ({err})") from None

    def assigned(body, name):
        for node in body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
                return node.value
        return None

    def strings(node, what):
        try:
            value = ast.literal_eval(node) if node is not None else None
        except ValueError:
            value = None
        if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise drifted(f"has no {what} written as a list of text")
        return value

    flags = strings(assigned(tree.body, "RT_FLAGS"), "RT_FLAGS")
    main = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main is None:
        raise drifted("has no main()")
    branches = [n for n in ast.walk(main) if isinstance(n, ast.If)
                and isinstance(n.test, ast.Name) and n.test.id == "mingw"]
    if not branches:
        raise drifted("has no `if mingw:` in main()")
    under = {id(n): n for branch in branches for stmt in branch.body for n in ast.walk(stmt)}
    for node in under.values():
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "rt_flags"):
            if not (len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                raise drifted("appends a computed flag to rt_flags for llvm-mingw")
            flags.append(node.args[0].value)
    folders = {"gen": gen, "deps": deps}

    def folder(node):
        if isinstance(node, ast.Name) and node.id in folders:
            return folders[node.id]
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            return folder(node.left) + "/" + node.right.value
        raise drifted("has a runtime include folder in inc_rt that is not gen or deps")

    includes = assigned(main.body, "inc_rt")
    if not isinstance(includes, ast.List):
        raise drifted("has no inc_rt list in main()")
    for item in includes.elts:
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            flags.append(item.value)
        elif (isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "rel"
              and len(item.args) == 1):
            flags.append(folder(item.args[0]))
        else:
            raise drifted("has an entry in inc_rt that is neither text nor rel(folder)")
    return flags


def _player_runtime_lib(settings: Settings, root: Path, log) -> str:
    """Compile the toolkit's runtime/core/patches.c against this build's gen/patches.h with the
    setup's runtime flags, and add it to a fresh copy of the toolkit's libmlbrt.a in
    runtime_lib_dir(); the copy's path relative to the toolkit, for --runtime-lib. BuildFailed
    when patches.c is missing or does not compile or archive."""
    source = root / PATCHES_C
    if not source.is_file():
        raise BuildFailed(f"The mods' runtime has no {PATCHES_C} (the toolkit {root} lacks it); "
                          f"{toolkit.GET_AGAIN}.")
    flags = runtime_flags(root / "tools" / "build.py", gen=_from_toolkit(gen_dir(settings), root),
                          deps="runtime/deps")
    work = runtime_lib_dir()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    obj, lib = work / "patches.o", work / "libmlbrt.a"
    shutil.copyfile(root / RUNTIME_LIB, lib)
    llvm = Path(settings.setup) / "llvm" / "bin"
    obj_rel, lib_rel = _from_toolkit(obj, root), _from_toolkit(lib, root)
    for what, argv in (("compiling", [str(llvm / "clang.exe"), *flags, "-c", PATCHES_C, "-o", obj_rel]),
                       ("adding to the runtime library", [str(llvm / "llvm-ar.exe"), "rs", lib_rel, obj_rel])):
        code, output = _run_tool(argv, root, log)
        if code != 0:
            raise BuildFailed(_failed(f"{what} the mods' {PATCHES_C} failed (exit {code})", output))
    return lib_rel


def _player_toolkit(settings: Settings, log) -> tuple[Path, str, str]:
    """(toolkit folder, its stamp, runtime.json's sha256): the release's runtime checked
    and laid over the setup (app/toolkit.py). BuildFailed with toolkit's message, or when
    the build output cannot be reached from the toolkit as tools/build.py needs it (same
    drive, no space in the relative path)."""
    tools = toolkit.folder()
    for label, path in (("build output folder", settings.build_out),
                        ("generated C folder", gen_dir(settings))):
        try:
            between = _from_toolkit(path, tools)
        except ValueError:
            raise BuildFailed(f"The {label} {path} is on another drive than the manager's build "
                              f"tools ({tools}); choose a build output folder on that drive in "
                              f"Settings.") from None
        if " " in between:
            raise BuildFailed(f"The {label} {path} is reached from the manager's build tools "
                              f"through a folder name with a space ({between}); choose a build "
                              f"output folder without spaces in Settings.")
    try:
        runtime = toolkit.load_runtime(settings)
        toolkit.check_versions(settings, runtime)
        root = toolkit.assemble(settings, runtime, log)
        return root, toolkit.stamp(settings, runtime), runtime["json_sha256"]
    except toolkit.ToolkitError as err:
        raise BuildFailed(str(err)) from err


def build(branches: list[str], settings: Settings, log: Callable[[str], None],
          optional=frozenset()) -> Path:
    """Merge main + branches into manager/build and compile it into build_out.
    `optional` are the branches of switched-off mods: one that does not merge is left
    out (and its mod's code patches with it) and recorded in build-info.json's
    left_out; built_from still holds every head, so the build stays up to date until
    something moves. Player mode takes no branches (the release's runtime holds the
    mods' code): no git, the toolkit instead of the worktree, built_from "runtime"
    instead of the heads. Raises BuildFailed (before touching git if a game runs from
    build_out); on failure the previous mlb.exe and build-info.json stay and
    last_error is set."""
    player = _player(settings)
    if player and settings.setup is None:
        raise BuildFailed(toolkit.SETUP_UNSET)
    unset = [label for key, label in (("recomp", "Recomp source"), ("build_wt", "Build worktree"),
                                      ("setup", "Setup folder"))
             if getattr(settings, key) is None and not player]
    if unset:
        raise BuildFailed("Building needs these set in Settings: " + ", ".join(unset) + ".")
    out = Path(settings.build_out)
    inside = []
    for game in procs.running_games():
        try:
            if game["exe"] and Path(game["exe"]).resolve().is_relative_to(out.resolve()):
                inside.append(game)
        except (OSError, ValueError):
            continue
    if inside:
        names = ", ".join(f"PID {g['pid']} ({g['exe']})" for g in inside)
        raise BuildFailed(f"the game is running from the build folder, close it first: {names}")
    if " " in str(out) and not player:  # player mode: checked relative to the toolkit
        raise BuildFailed(f"the build output folder must not contain spaces: {out}")

    exe, backup = out / "mlb.exe", out / "mlb.exe.prev"
    log_file = None

    def both(line: str) -> None:
        if log_file:
            log_file.write(line + "\n")
            log_file.flush()
        log(line)

    try:
        try:
            logs = Path(settings.state) / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            log_file = open(logs / "build.log", "w", encoding="utf-8")
            if backup.is_file():
                # left by a build that was killed mid-way: it is the last known-good exe
                both(f"restoring the previous exe from {backup}")
                os.replace(backup, exe)
            all_mods = mods.load_all(settings)
            if player:
                branches, optional = [], frozenset()
            optional = frozenset(optional)
            try:
                patchsets.collect(all_mods)  # two mods on one set name or switch: stop here
                built_from, text, spans, sets = _needed(branches, all_mods, settings,
                                                        missing_ok=branches)
            except patchsets.PatchSetError as err:
                raise BuildFailed(str(err)) from err
            owners = {}  # branch -> its mods, in mod order
            for mod in all_mods:
                if mod.runtime and mod.runtime.get("branch"):
                    owners.setdefault(mod.runtime["branch"], []).append(mod)
            if player:
                root, tools_key, built_from["runtime"] = _player_toolkit(settings, both)
                left = {}
            else:
                root, tools_key = Path(settings.build_wt), None
                missing = frozenset(b for b in branches if built_from.get(b) == MISSING)
                for b in branches:
                    if b in missing and b not in optional:
                        names = ", ".join(repr(m.name) for m in owners.get(b, []))
                        raise BuildFailed(f"branch {b!r}" + (f" (mod {names})" if names else "")
                                          + f" not found in {settings.recomp}")
                left = _merge(branches, root, both, optional,
                              {b: ms[0].name for b, ms in owners.items()}, missing)
            left_out = {}
            for b, why in left.items():
                ids = [m.id for m in owners.get(b, [])]
                left_out[b] = {"head": built_from.get(b), "files": why["files"],
                               "mod": ids[0] if ids else None, "mods": ids,
                               "reason": why["reason"]}
            # every mod on a left-out branch: its patch lines may need that branch's generator
            gone = {mod.id for b in left for mod in owners.get(b, [])}
            for mod in all_mods:
                if mod.id in gone and mod.patches:
                    both(f"Left out {mod.id}'s code patches with its branch "
                         f"{mod.runtime['branch']}")
            # the release's own patch file, as the merge (or the toolkit) has it, goes first
            builtin = builtin_patches(settings, root)
            if builtin is not None:
                both(f"Built-in code patches: {builtin.label}")
            try:
                sets = patchsets.collect(all_mods, builtin=builtin)  # a mod on a built-in set: stop
                text, spans = patchsets.combine([m for m in all_mods if m.id not in gone], builtin)
            except patchsets.PatchSetError as err:
                raise BuildFailed(str(err)) from err
            _generate(text, spans, all_mods, settings, both, root if player else None, tools_key)
            if player:
                command = build_command(settings, root, _player_runtime_lib(settings, root, both))
            else:
                command = build_command(settings)
            out.mkdir(parents=True, exist_ok=True)
            if exe.is_file():
                # via a temp name, so a half-written copy never poses as the backup
                partial = out / "mlb.exe.prev.tmp"
                shutil.copy2(exe, partial)
                os.replace(partial, backup)
            code, output = _run_build(command, root, both)
            if code != 0 or not exe.is_file():
                if code != 0:
                    text = _failed(f"build failed (exit {code})", output)
                else:
                    text = _failed(f"build finished but {exe} is missing", output)
                # a failed link may delete or truncate the output: put the old exe back
                if backup.is_file():
                    try:
                        os.replace(backup, exe)
                    except OSError as err:
                        raise BuildFailed(f"{text}\nrestoring the previous exe failed ({err}); "
                                          f"it is kept as {backup}") from err
                raise BuildFailed(text)
            backup.unlink(missing_ok=True)
            stage_runtime(settings, both)
            _write_info(settings, built_from, exe, sets, left_out)
            _error_path(settings).unlink(missing_ok=True)
        except OSError as err:
            raise BuildFailed(f"build failed: {err}") from err
    except BuildFailed as err:
        try:
            both(f"BUILD FAILED: {err}")
        except OSError:
            pass
        try:
            _error_path(settings).parent.mkdir(parents=True, exist_ok=True)
            _error_path(settings).write_text(str(err), encoding="utf-8")
        except OSError:
            pass
        raise
    finally:
        if log_file:
            log_file.close()
    return exe
