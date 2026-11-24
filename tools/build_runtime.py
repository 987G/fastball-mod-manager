r"""Build the mods' runtime: Fastball's runtime library with the combined mods patch, for the release's runtime zip.

    python -I tools/build_runtime.py --fastball <fresh clone of Fastball at the pinned tag> --patch runtime/mods.patch
                                     --toolchain <llvm-mingw> --gen <a local game build's gen folder> --out <dir>
                                     [--ninja <ninja.exe>] [--jobs N] [--cache <downloads folder>]

The clone must be clean and at the commit pinned for its release (VERSION + "+" + the short commit, as Fastball's
setup names it); clone it with `git -c core.autocrlf=false clone`. Then:
  1. the patch is applied to it (`git apply --check` first; only runtime/ and recomp/, never the test driver
     runtime/mlb/hid.c or runtime/gfx/gfx_stubs.c, no file deleted);
  2. Fastball's tools/fetch_deps.py fetches the Vulkan, VMA and SDL3 build inputs (its own pinned checksums), with the
     toolchain's llvm-readobj and llvm-dlltool;
  3. --gen's layout.json, layout.h, patches.h and prompt_icons.h (made by recomp/gen.py and tools/build.py on the
     maintainer's PC: they need the game's ExeFS) are copied into the clone's work/gen, and Fastball's
     `tools/build.py --toolchain <llvm-mingw> --make-runtime-lib` builds libmlbrt.a at below-normal priority, its
     debug info naming the clone "." and the toolchain "llvm-mingw" (clang's -ffile-prefix-map, added through
     CCC_OVERRIDE_OPTIONS): the library is checked to hold neither folder nor the home folder of whoever built it;
  4. runtime/core/patches.c's object is taken out of the library (`llvm-ar d`): the patch-set table it compiles in
     (gen's patches.h) depends on the mods a player has, so the manager compiles it on each player build. It must be
     the only object that includes patches.h (checked with `ninja -t deps`), so the library holds nothing of
     --gen's table;
  5. --out gets runtime/libmlbrt.a, runtime/core/patches.c, every runtime header (.h, .inc) and recomp file (.py,
     .txt: what the setup ships of recomp/) the patch changed or added, each as its patched git blob, and
     runtime.json: {"fastball": release, "patch": sha256 of the patch, "files": {path: {"base", "sha256"}}}. "base"
     is the sha256 of the file in the Fastball setup the runtime lays over: its blob at the pinned commit, null for a
     file new to the setup (patches.c: the setup has no C sources), and for libmlbrt.a the stock library's sha256
     pinned below.
Downloads (ninja) are cached and checked against their pinned sha256. Standard library only.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Fastball releases this tool builds for: the commit and the sha256 of the stock runtime library its setup ships
# (runtime/libmlbrt.a of MLBTS-Setup 0.2.0, the base the player's toolkit compares its setup with).
PINS = {
    "0.2.0+74a6525": {"commit": "74a6525bd0036335f01c1906fd11146d70a1cf12",
                      "libmlbrt.a": "e66af9ce7b618c152eecc6497178dfa1deccf238bb8f3908d9c0f7f12d938e4e"},
}
# ninja: the url and sha256 Fastball's tools/make_release.py pins
NINJA = ("https://github.com/ninja-build/ninja/releases/download/v1.13.2/ninja-win.zip",
         "07fc8261b42b20e71d1720b39068c2e14ffcee6396b76fb7a795fb460b78dc65")
CACHE = Path(tempfile.gettempdir()) / "show-mod-manager-downloads"
# what --make-runtime-lib reads of gen; patches.h only so that `ninja -t deps` shows which objects include it
GEN_FILES = ("layout.json", "layout.h", "patches.h", "prompt_icons.h")
PATCHES_C = "runtime/core/patches.c"
PATCHES_O = "patches.o"
LIB = "runtime/libmlbrt.a"
EXCLUDED = {"runtime/mlb/hid.c", "runtime/gfx/gfx_stubs.c"}  # the test driver: never in a release
SHIPPED = {"runtime": (".h", ".inc"), "recomp": (".py", ".txt")}  # what the setup has of each tree
WORK_GEN, WORK_DEPS, WORK_LIB = "work/gen", "work/deps", "work/runtime-lib"
BLOCK = 1 << 20
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# fetch_deps.py with its llvm folder (C:\Program Files\LLVM by default) set to the toolchain's
FETCH_DEPS = ("import importlib.util, sys\n"
              "from pathlib import Path\n"
              "spec = importlib.util.spec_from_file_location('fetch_deps', sys.argv[1])\n"
              "m = importlib.util.module_from_spec(spec)\n"
              "spec.loader.exec_module(m)\n"
              "m.LLVM = Path(sys.argv[2])\n"
              "sys.argv = [sys.argv[1]] + sys.argv[3:]\n"
              "m.main()\n")


class BuildError(Exception):
    """The runtime cannot be built; the message says why. --out was not written."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(repo: Path, *args, binary=False):
    proc = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotePath=false", "-C", str(repo),
                           *args], capture_output=True, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        raise BuildError(f"git {' '.join(args)} in {repo} failed: "
                         f"{proc.stderr.decode('utf-8', 'replace').strip()[-400:]}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace").strip()


def _run(argv: list, cwd: Path, what: str, priority: int = 0, env: dict | None = None) -> None:
    print("+ " + " ".join(str(a) for a in argv), flush=True)
    try:
        code = subprocess.run([str(a) for a in argv], cwd=str(cwd), creationflags=priority, env=env).returncode
    except OSError as err:
        raise BuildError(f"cannot start {what}: {err}") from None
    if code != 0:
        raise BuildError(f"{what} failed (exit {code})")


# -- the clone and the patch -----------------------------------------------------------------------------
def check_clone(clone: Path, pins: dict = PINS) -> tuple[str, dict]:
    """(release, its pin) of a clean clone at its release's pinned commit; BuildError otherwise."""
    clone = Path(clone)
    if not (clone / ".git").exists():
        raise BuildError(f"{clone} is not a git clone of Fastball")
    head = _git(clone, "rev-parse", "HEAD")
    version = _git(clone, "show", "HEAD:VERSION").strip()
    release = f"{version}+{head[:7]}"
    pin = pins.get(release)
    if pin is None:
        raise BuildError(f"{clone} is Fastball {release}, which has no pin in this tool (pinned: "
                         f"{', '.join(sorted(pins))}); check out the pinned tag, or pin the new release")
    if head != pin["commit"]:
        raise BuildError(f"{clone} is at {head}, not at Fastball {release}'s pinned commit {pin['commit']}")
    if _git(clone, "status", "--porcelain"):
        raise BuildError(f"{clone} has changes; build from a fresh clone at the pinned tag")
    return release, pin


def apply_patch(clone: Path, patch: Path) -> list[str]:
    """Apply `patch` to the clone and its index; the sorted paths it changes or adds. BuildError, with the
    clone as it was, when it does not apply, deletes a file or touches anything but runtime/ and recomp/
    (or the test driver)."""
    clone, patch = Path(clone), Path(patch).absolute()
    try:
        _git(clone, "apply", "--check", "--index", str(patch))
    except BuildError as err:
        raise BuildError(f"{patch} does not apply to {clone}: {err}") from None
    _git(clone, "apply", "--index", str(patch))
    lines = _git(clone, "diff", "--cached", "--name-status", "--no-renames", "HEAD")
    changed, wrong = [], []
    for line in lines.splitlines():
        status, _, path = line.partition("\t")
        if status not in ("M", "A"):
            wrong.append(f"{path} (deleted)" if status == "D" else f"{path} ({status})")
        elif path.split("/")[0] not in SHIPPED or path in EXCLUDED:
            wrong.append(path)
        else:
            changed.append(path)
    if wrong:
        _git(clone, "reset", "-q", "--hard", "HEAD")
        raise BuildError(f"{patch} changes what a runtime patch may not: {', '.join(wrong)} (only runtime/ and "
                         f"recomp/ files may be changed or added, never {', '.join(sorted(EXCLUDED))})")
    return sorted(changed)


# -- the library -----------------------------------------------------------------------------------------
def fetch(url: str, sha256: str, cache: Path = CACHE) -> Path:
    """The download at `url`, from `cache` when it is there with this sha256, else downloaded and checked."""
    path = Path(cache) / url.rsplit("/", 1)[1]
    if path.is_file() and sha256_file(path) == sha256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as stream, open(part, "wb") as out:
            shutil.copyfileobj(stream, out, BLOCK)
    except OSError as err:
        part.unlink(missing_ok=True)
        raise BuildError(f"downloading {url} failed: {err}") from None
    got = sha256_file(part)
    if got != sha256:
        part.unlink()
        raise BuildError(f"{url}: sha256 {got}, pinned {sha256}; not used")
    os.replace(part, path)
    return path


def _ninja(cache: Path) -> Path:
    archive = fetch(*NINJA, cache=cache)
    exe = archive.with_name(f"ninja-{NINJA[1][:12]}.exe")
    if not exe.is_file():
        with zipfile.ZipFile(archive) as zf:
            part = exe.with_name(exe.name + ".part")
            part.write_bytes(zf.read("ninja.exe"))
            os.replace(part, exe)
    return exe


def users_of(deps_text: str, header: str) -> list[str]:
    """The targets of `ninja -t deps` output that depend on `header` (a path as ninja lists it)."""
    users, target = [], None
    for line in deps_text.splitlines():
        if line and not line[0].isspace():
            target = line.split(":", 1)[0].strip()
        elif line.strip() == header and target is not None and target not in users:
            users.append(target)
    return users


def prefix_maps(clone: Path, toolchain: Path) -> str:
    """CCC_OVERRIDE_OPTIONS for clang ("#": silently) adding -ffile-prefix-map so that debug info names the clone
    "." and the toolchain "llvm-mingw" instead of their folders on this PC. BuildError for a folder with a space
    (the options are split at spaces)."""
    maps = []
    for folder, name in ((clone, "."), (toolchain, "llvm-mingw")):
        text = str(Path(folder).absolute())
        if " " in text:
            raise BuildError(f"{text} has a space in its path; build from folders without spaces")
        for form in dict.fromkeys((text.replace("\\", "/"), text.replace("/", "\\"))):
            maps.append(f"+-ffile-prefix-map={form}={name}")
    return " ".join(["#", *maps])


def local_paths(data: bytes, folders) -> list[str]:
    """The folders (either slash, any case) that `data` names: what a published file must not hold."""
    low = data.lower()
    found = []
    for folder in folders:
        text = str(Path(folder).absolute())
        forms = {text.replace("\\", "/"), text.replace("/", "\\")}
        if any(form.lower().encode("utf-8") in low for form in forms):
            found.append(text)
    return found


def check_patches_h(deps_text: str, header: str) -> None:
    """BuildError unless exactly one object, patches.o, depends on gen's patches.h (`header` as ninja lists it)."""
    users = users_of(deps_text, header)
    others = [u for u in users if Path(u).name != PATCHES_O]
    if others:
        raise BuildError(f"objects besides {PATCHES_O} include gen's patches.h ({', '.join(others)}): the "
                         f"library would carry one build's patch-set table")
    if len(users) != 1:
        raise BuildError(f"ninja -t deps lists {len(users)} objects including {header}, not {PATCHES_O} alone: "
                         f"cannot tell what the library holds of the patch-set table")


def build_lib(clone: Path, toolchain: Path, gen: Path, ninja: Path, jobs: int) -> Path:
    """libmlbrt.a built in the clone (work/runtime-lib), with patches.c's object taken out."""
    clone, toolchain, gen = Path(clone), Path(toolchain).absolute(), Path(gen)
    llvm = toolchain / "bin"
    for tool in ("clang.exe", "llvm-ar.exe", "llvm-readobj.exe", "llvm-dlltool.exe"):
        if not (llvm / tool).is_file():
            raise BuildError(f"{llvm / tool} is missing: --toolchain must be an llvm-mingw folder")
    missing = [name for name in GEN_FILES if not (gen / name).is_file()]
    if missing:
        raise BuildError(f"--gen {gen} has no {', '.join(missing)}: give the gen folder of a local game build "
                         f"(recomp/gen.py, then tools/build.py, which writes prompt_icons.h)")
    _run([sys.executable, "-I", "-c", FETCH_DEPS, clone / "tools" / "fetch_deps.py", llvm,
          "--out", clone / WORK_DEPS], clone, "tools/fetch_deps.py")
    work_gen = clone / WORK_GEN
    work_gen.mkdir(parents=True, exist_ok=True)
    for name in GEN_FILES:  # copyfile: newer than the clone's tools/make_prompts.py, so it is not run again
        shutil.copyfile(gen / name, work_gen / name)
    out = clone / WORK_LIB
    env = dict(os.environ, CCC_OVERRIDE_OPTIONS=prefix_maps(clone, toolchain))
    _run([sys.executable, "-I", "tools/build.py", "--toolchain", toolchain, "--make-runtime-lib",
          "--gen", WORK_GEN, "--deps", WORK_DEPS, "--out", WORK_LIB, "--ninja", ninja]
         + (["--jobs", str(jobs)] if jobs else []), clone, "tools/build.py --make-runtime-lib", BELOW_NORMAL, env)
    lib = out / "libmlbrt.a"
    if not lib.is_file():
        raise BuildError(f"tools/build.py finished without {lib}")
    named = local_paths(lib.read_bytes(), (clone, toolchain, Path.home()))
    if named:
        raise BuildError(f"{lib} names folders of this PC ({', '.join(named)}); it must not be published")
    deps = subprocess.run([str(ninja), "-f", f"{WORK_LIB}/build.ninja", "-t", "deps"], cwd=str(clone),
                          capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
    if deps.returncode != 0:
        raise BuildError(f"ninja -t deps failed: {deps.stderr.strip()[-400:]}")
    check_patches_h(deps.stdout, f"{WORK_GEN}/patches.h")
    members = subprocess.run([str(llvm / "llvm-ar.exe"), "t", str(lib)], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout.split()
    if members.count(PATCHES_O) != 1:
        raise BuildError(f"{lib} holds {members.count(PATCHES_O)} members named {PATCHES_O}, not one")
    _run([llvm / "llvm-ar.exe", "d", lib, PATCHES_O], clone, f"llvm-ar d {PATCHES_O}")
    left = subprocess.run([str(llvm / "llvm-ar.exe"), "t", str(lib)], capture_output=True, text=True,
                          creationflags=NO_WINDOW).stdout.split()
    if PATCHES_O in left or len(left) != len(members) - 1:
        raise BuildError(f"taking {PATCHES_O} out of {lib} did not work")
    print(f"{lib}: {len(left)} objects, {PATCHES_O} taken out", flush=True)
    return lib


# -- the runtime folder ----------------------------------------------------------------------------------
def write_runtime(clone: Path, commit: str, release: str, pin: dict, changed: list[str], lib: Path, patch: Path,
                  out: Path) -> dict:
    """Write --out (new or empty) and return runtime.json's content. Files are the patched blobs in the clone's
    index; bases are the blobs at `commit` (null when new), patches.c's null, the library's pinned."""
    clone, out = Path(clone), Path(out).absolute()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise BuildError(f"{out} is not empty; build into a new or empty folder")
    shipped = [p for p in changed if p.endswith(SHIPPED[p.split("/")[0]])]
    entries = {LIB: (Path(lib).read_bytes(), pin["libmlbrt.a"])}
    for rel in sorted(set(shipped) | {PATCHES_C}):
        data = _git(clone, "cat-file", "blob", f":{rel}", binary=True)
        if rel == PATCHES_C:
            base = None  # the setup has no C sources: patches.c is new to it
        elif rel in _git(clone, "ls-tree", "-r", "--name-only", commit, "--", rel).splitlines():
            base = sha256_bytes(_git(clone, "cat-file", "blob", f"{commit}:{rel}", binary=True))
        else:
            base = None
        entries[rel] = (data, base)
    listing = {"fastball": release, "patch": sha256_file(patch),
               "files": {rel: {"base": base, "sha256": sha256_bytes(data)}
                         for rel, (data, base) in sorted(entries.items())}}
    stage = out.parent / f".{out.name}.partial"
    if stage.exists():
        shutil.rmtree(stage)
    try:
        for rel, (data, _) in entries.items():
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            (stage / rel).write_bytes(data)
        (stage / "runtime.json").write_text(json.dumps(listing, indent=1) + "\n", encoding="utf-8")
        if out.exists():
            out.rmdir()
        os.replace(stage, out)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return listing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the mods' runtime from a Fastball clone and the mods patch.")
    parser.add_argument("--fastball", required=True, type=Path, help="a fresh clone of Fastball at the pinned tag")
    parser.add_argument("--patch", required=True, type=Path, help="the exported tree's runtime/mods.patch")
    parser.add_argument("--toolchain", required=True, type=Path, help="llvm-mingw (the Fastball setup's llvm)")
    parser.add_argument("--gen", required=True, type=Path,
                        help="a local game build's gen folder (layout.json, layout.h, patches.h, prompt_icons.h)")
    parser.add_argument("--out", required=True, type=Path, help="a new or empty folder")
    parser.add_argument("--ninja", type=Path, help="ninja.exe (default: the pinned download)")
    parser.add_argument("--jobs", type=int, default=6, help="parallel compiles (default 6)")
    parser.add_argument("--cache", type=Path, default=CACHE, help="where downloads are kept")
    args = parser.parse_args(argv)
    try:
        release, pin = check_clone(args.fastball)
        out = Path(args.out).absolute()
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise BuildError(f"{out} is not empty; build into a new or empty folder")
        changed = apply_patch(args.fastball, args.patch)
        print(f"applied {args.patch}: {len(changed)} files changed or added", flush=True)
        ninja = args.ninja or _ninja(args.cache)
        lib = build_lib(args.fastball, args.toolchain, args.gen, ninja, args.jobs)
        listing = write_runtime(args.fastball, pin["commit"], release, pin, changed, lib, args.patch, out)
    except BuildError as err:
        print(f"build_runtime refused: {err}", file=sys.stderr)
        return 1
    print(f"runtime for Fastball {release} in {out}: {len(listing['files'])} files")
    for rel, entry in listing["files"].items():
        print(f"  {rel}  base {(entry['base'] or 'new')[:12]}  sha256 {entry['sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
