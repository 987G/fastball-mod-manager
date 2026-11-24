r"""Release assets from an exported tree (tools/export.py) and a built runtime (tools/build_runtime.py).

    python -I tools/package.py --tree <export> --runtime <built runtime folder> --version <tag>
                               --repo <owner/name> --out <new or empty folder>
                               [--launcher "Show Mod Manager.exe"] [--cache <downloads folder>]

Writes into --out:
  ShowModManager-<tag>.zip   the exported manager, python/ (python.org's embeddable Python, pinned below, its ._pth
                             listing python3XX.zip, . and ..: the manager folder), release.json ({"repo", "tag",
                             "fastball"}), the tree's LICENSE and THIRD_PARTY_NOTICES.md, and the launcher exe when
                             given
  mod-<id>-<tag>.zip         each mod folder of the tree, plus the shared files mods/_shared/shared.json lists for it
                             under _shared/ (a mod zip is installed into mods/<id>/ alone)
  runtime-<fastball>.zip     runtime.json and every file it lists ("+" in the Fastball release becomes "-"), and the
                             tree's THIRD_PARTY_NOTICES.md (the library holds Fastball's code and what it uses)
  index.json                 {"release", "fastball", "runtime": {"file", "sha256", "size"}, "mods": [{"id", "name",
                             "version", "description", "art", "card", "features", "parts", "file", "sha256",
                             "size"}]}: what the manager's mod picker shows, read by the exported manager's own loader
                             ("version" is the mod's own from its mod.json, else the release tag)

Mods, the runtime listing and every check come from the exported manager's own code (app.mods, app.toolkit,
app.releases), so the release holds what the manager reads. Before --out is filled, each zip is unpacked and checked
with the manager's own readers; a failure leaves --out untouched. Downloads are cached (--cache) and checked against
the pinned sha256. Zips are written the same way every time (sorted entries, fixed dates). Standard library only.
"""
import argparse
import hashlib
import importlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

PY_VERSION = "3.14.9"
# python.org's embeddable Python; the url and sha256 Fastball's tools/make_release.py pins for its setup
EMBED = (f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip",
         "3c01d7cfc666f746a6f064f60fb2156ab0ec464d3d738ab796371a46a9bb24a2")
CACHE = Path(tempfile.gettempdir()) / "show-mod-manager-downloads"
LAUNCHER = "Show Mod Manager.exe"
RELEASE_INFO = "release.json"
INDEX = "index.json"
SHARED = "_shared"
SHARED_INDEX = "shared.json"
PYTHON = "python"
LICENSE, THIRD_PARTY = "LICENSE", "THIRD_PARTY_NOTICES.md"
NOTICES = (LICENSE, THIRD_PARTY)  # at the tree's top (docs/public)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")  # asset names: safe in a URL, a path and a GitHub asset
_PTH = re.compile(r"(python3\d+)\._pth\Z")
DATE = (2026, 1, 1, 0, 0, 0)  # every zip entry's date: the same zip from the same files
BLOCK = 1 << 20
_loads = itertools.count()


class PackageError(Exception):
    """The release cannot be packaged; the message says why. --out was not written."""


# -- the exported manager's own code ---------------------------------------------------------------------
def manager_code(tree: Path) -> SimpleNamespace:
    """The tree's manager/app package (loaded under a name of its own, beside any other copy) as
    {config, mods, releases, toolkit, server}. No bytecode is written from here on (this process): a __pycache__ in
    the tree would hold this PC's folders and be published with it."""
    init = Path(tree) / "manager" / "app" / "__init__.py"
    if not init.is_file():
        raise PackageError(f"{init} is missing: --tree must be an exported tree (tools/export.py)")
    sys.dont_write_bytecode = True  # left set: the app imports some modules later, when first used
    name = f"_release_app_{next(_loads)}"
    spec = importlib.util.spec_from_file_location(name, init, submodule_search_locations=[str(init.parent)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return SimpleNamespace(**{m: importlib.import_module(f"{name}.{m}")
                              for m in ("config", "mods", "releases", "toolkit", "server")})


# -- downloads -------------------------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, sha256: str, cache: Path = CACHE) -> Path:
    """The download at `url`, from `cache` when it is there with this sha256, else downloaded into it and
    checked; PackageError (nothing kept) when the sha256 differs."""
    cache = Path(cache)
    path = cache / url.rsplit("/", 1)[1]
    if path.is_file() and sha256_file(path) == sha256:
        return path
    cache.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as stream, open(part, "wb") as out:
            shutil.copyfileobj(stream, out, BLOCK)
    except OSError as err:
        part.unlink(missing_ok=True)
        raise PackageError(f"downloading {url} failed: {err}") from None
    got = sha256_file(part)
    if got != sha256:
        part.unlink()
        raise PackageError(f"{url}: sha256 {got}, pinned {sha256}; not used")
    os.replace(part, path)
    return path


# -- zips ------------------------------------------------------------------------------------------------
def _write_zip(path: Path, members: dict) -> dict:
    """Write {name: source file or bytes} sorted by name, every entry dated DATE; {"file", "sha256", "size"}."""
    seen = {}
    for name in members:
        key = name.casefold()
        if key in seen:
            raise PackageError(f"{path.name} would hold {seen[key]} and {name}, one file on Windows")
        seen[key] = name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            source = members[name]
            zf.writestr(info, source if isinstance(source, bytes) else Path(source).read_bytes())
    return {"file": path.name, "sha256": sha256_file(path), "size": path.stat().st_size}


def _files(folder: Path) -> dict:
    """{posix path relative to folder: file} of every file under it (bytecode left out)."""
    out = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if path.is_file() and "__pycache__" not in rel.split("/") and not rel.endswith((".pyc", ".pyo")):
            out[rel] = path
    return out


def _embed_members(embed: Path) -> dict:
    """python/<member> of the embeddable zip, its ._pth rewritten to list python3XX.zip, . and .."""
    try:
        with zipfile.ZipFile(embed) as zf:
            members = {i.filename: zf.read(i) for i in zf.infolist() if not i.is_dir()}
    except (OSError, zipfile.BadZipFile) as err:
        raise PackageError(f"the embeddable Python {embed} is not a usable zip: {err}") from None
    pths = [n for n in members if _PTH.match(n)]
    if len(pths) != 1:
        raise PackageError(f"the embeddable Python {embed} has {len(pths)} ._pth files at its top, not one")
    stem = _PTH.match(pths[0]).group(1)
    for needed in (f"{stem}.zip", "pythonw.exe"):
        if needed not in members:
            raise PackageError(f"the embeddable Python {embed} has no {needed}")
    members[pths[0]] = f"{stem}.zip\n.\n..\n".encode("ascii")
    return {f"{PYTHON}/{name}": data for name, data in members.items()}


def embed_files(embed: Path) -> dict:
    """{python/<member>: sha256} of the manager zip's files that are byte-for-byte those of the embeddable zip (all
    but the ._pth rewritten here): tools/check_release.py leaves these to its path rules, since python.org made them."""
    with zipfile.ZipFile(embed) as zf:
        original = {i.filename: zf.read(i) for i in zf.infolist() if not i.is_dir()}
    return {name: hashlib.sha256(data).hexdigest() for name, data in _embed_members(embed).items()
            if original.get(name[len(PYTHON) + 1:]) == data}


def _notices(tree: Path) -> dict:
    """{name: file} of the tree's LICENSE and THIRD_PARTY_NOTICES.md (exported from docs/public)."""
    found = {name: tree / name for name in NOTICES}
    missing = [name for name, path in found.items() if not path.is_file()]
    if missing:
        raise PackageError(f"the tree has no {' or '.join(missing)} at its top; every release carries the license "
                           f"and the third-party notices (docs/public, exported by tools/export.py)")
    return found


# -- the release ------------------------------------------------------------------------------------------
def _plain(value: str, what: str, pattern) -> str:
    if not (isinstance(value, str) and NAME.match(value) and pattern.match(value)):
        raise PackageError(f"{what} {value!r} cannot be part of a release file name (letters, digits, . _ - only)")
    return value


def _fastball_release(tree: Path) -> str:
    path = tree / "runtime" / "fastball.version"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as err:
        raise PackageError(f"cannot read {path}: {err}") from None
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() == "release" and value.strip():
            return value.strip()
    raise PackageError(f"{path} names no Fastball release")


def _runtime(app, tree: Path, runtime: Path) -> tuple[dict, dict]:
    """(runtime.json as toolkit.load_runtime reads it, {zip member: file}) after every check."""
    settings = app.config.Settings(runtime_dir=Path(runtime))
    try:
        listing = app.toolkit.load_runtime(settings)
    except app.toolkit.ToolkitError as err:
        raise PackageError(f"the runtime {runtime} cannot be used: {err}") from None
    want = _fastball_release(tree)
    if listing["fastball"] != want:
        raise PackageError(f"the runtime {runtime} was built for Fastball {listing['fastball']}, but the tree's "
                           f"runtime/fastball.version names {want}")
    raw = json.loads((Path(runtime) / app.toolkit.LISTING).read_text(encoding="utf-8"))
    patch = tree / "runtime" / "mods.patch"
    if raw.get("patch") != sha256_file(patch):
        raise PackageError(f"the runtime {runtime} was not built from the tree's {patch} (the sha256 in its "
                           f"runtime.json differs); build it again from this tree")
    members = {app.toolkit.LISTING: Path(runtime) / app.toolkit.LISTING}
    members.update({rel: Path(runtime) / rel for rel in listing["files"]})
    return listing, members


def _shared(tree: Path, ids: list[str]) -> dict:
    """{mod id: {"_shared/<file>": source}} from mods/_shared/shared.json."""
    folder = tree / "mods" / SHARED
    out = {mod_id: {} for mod_id in ids}
    if not (folder / SHARED_INDEX).is_file():
        return out
    try:
        listing = json.loads((folder / SHARED_INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise PackageError(f"cannot read {folder / SHARED_INDEX}: {err}") from None
    if not isinstance(listing, dict):
        raise PackageError(f"{folder / SHARED_INDEX} must map a file name to the mods that use it")
    for name, users in listing.items():
        if not (isinstance(name, str) and NAME.match(name) and (folder / name).is_file()):
            raise PackageError(f"{folder / SHARED_INDEX} names {name!r}, which is not a file in {folder}")
        if not (isinstance(users, list) and all(isinstance(u, str) for u in users)):
            raise PackageError(f"{folder / SHARED_INDEX}: {name} must list mod ids")
        for user in users:
            if user not in out:
                raise PackageError(f"{folder / SHARED_INDEX}: {name} is listed for {user!r}, which is not a mod "
                                   f"of the tree")
            out[user][f"{SHARED}/{name}"] = folder / name
    return out


def _mod_entry(app, mod, tag: str) -> dict:
    """A mod's index entry as the picker reads it (server.catalog_mod), before its file is known."""
    flags = {"files": bool(mod.files), "runtime": bool(mod.runtime), "patches": bool(mod.patches),
             "server": bool(mod.server), "profile": mod.profile == "own"}
    entry = {"id": mod.id, "name": mod.name, "version": getattr(mod, "version", None) or tag,  # its own, else the tag
             "description": mod.description if isinstance(mod.description, str) else ""}
    if isinstance(mod.art, str) and app.server.ART_NAME.match(mod.art):
        entry["art"] = mod.art
    if mod.card_text is not None:
        entry["card"] = mod.card_text
    if mod.features is not None:
        entry["features"] = mod.features
    entry["parts"] = [p for p in app.server.PARTS if flags[p]]
    return entry


def _plan(app, tree: Path, runtime: Path, tag: str, repo: str, embed: Path, launcher: Path | None) -> dict:
    """{zip name: members} and the index without files, every check made; nothing written."""
    _plain(tag, "the release tag", app.releases._TAG)
    if not (isinstance(repo, str) and app.releases._REPO.match(repo)):
        raise PackageError(f"--repo {repo!r} must be owner/name")
    notices = _notices(tree)
    listing, runtime_members = _runtime(app, tree, runtime)
    runtime_members[THIRD_PARTY] = notices[THIRD_PARTY]
    fastball = listing["fastball"]
    runtime_zip = f"runtime-{fastball.replace('+', '-')}.zip"
    _plain(runtime_zip, "the runtime file name", app.releases._FILE)

    settings = app.config.Settings(mods=tree / "mods")
    try:
        found = app.mods.load_all(settings)
    except app.mods.ManifestError as err:
        raise PackageError(f"a mod of the tree does not load: {err}") from None
    for mod in found:
        _plain(mod.id, "the mod id", app.releases._ID)
    shared = _shared(tree, [m.id for m in found])

    zips, entries = {}, []
    for mod in found:
        members = _files(mod.folder)
        clash = [n for n in members if n.split("/")[0].casefold() == SHARED]
        if clash:
            raise PackageError(f"mod {mod.id!r} has its own {clash[0]}, where its shared files go")
        members.update(shared[mod.id])
        name = _plain(f"mod-{mod.id}-{tag}.zip", "the mod file name", app.releases._FILE)
        zips[name] = members
        entries.append((name, _mod_entry(app, mod, tag)))
    zips[runtime_zip] = runtime_members

    manager = _files(tree / "manager")
    manager.update(_embed_members(embed))
    manager.update(notices)
    manager[RELEASE_INFO] = (json.dumps({"repo": repo, "tag": tag, "fastball": fastball}, indent=1)
                             + "\n").encode("utf-8")
    if launcher is not None:
        if not Path(launcher).is_file():
            raise PackageError(f"the launcher {launcher} is missing")
        manager[LAUNCHER] = Path(launcher)
    zips[_plain(f"ShowModManager-{tag}.zip", "the manager file name", app.releases._FILE)] = manager
    return {"zips": zips, "mods": entries, "runtime": runtime_zip, "fastball": fastball, "tag": tag}


def _verify(app, folder: Path, index: dict, manager_zip: str) -> None:
    """Every asset in `folder` unpacked and read as the manager reads a download."""
    releases = app.releases
    try:
        releases._check_index(index, INDEX)
        with tempfile.TemporaryDirectory(prefix="smm-package-") as tmp:
            tmp = Path(tmp)
            for entry in index["mods"]:
                dest = tmp / "mods" / entry["id"]
                releases._unpack(folder / entry["file"], dest, entry["file"])
                releases._check_mod(dest, entry["file"])
            loaded = [m.id for m in app.mods.load_all(app.config.Settings(mods=tmp / "mods"))]
            if loaded != [m["id"] for m in index["mods"]]:
                raise PackageError(f"the unpacked mods load as {loaded}, not as the index lists them")
            name = index["runtime"]["file"]
            releases._unpack(folder / name, tmp / "runtime", name)
            releases._check_runtime(tmp / "runtime", name, app.config.Settings(), index["fastball"])
            releases._unpack(folder / manager_zip, tmp / "manager", manager_zip)
            if releases.release_info(tmp / "manager") is None:
                raise PackageError(f"{manager_zip} has no {RELEASE_INFO} at its top")
    except (releases.ReleaseError, app.mods.ManifestError) as err:
        raise PackageError(f"the packaged release does not read back: {err}") from None


def package(tree: Path, runtime: Path, version: str, repo: str, out: Path, embed: Path,
            launcher: Path | None = None) -> dict:
    """Write the release into `out` (new or empty) and return its index; PackageError, `out` untouched,
    when a check fails. `embed` is the embeddable Python zip (fetch(*EMBED) in main)."""
    tree, out = Path(tree), Path(out).absolute()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise PackageError(f"{out} is not empty; package into a new or empty folder")
    app = manager_code(tree)
    plan = _plan(app, tree, Path(runtime), version, repo, Path(embed), launcher)
    stage = out.parent / f".{out.name}.partial"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        written = {name: _write_zip(stage / name, members) for name, members in plan["zips"].items()}
        index = {"release": plan["tag"], "fastball": plan["fastball"], "runtime": written[plan["runtime"]],
                 "mods": [dict(entry, **written[name]) for name, entry in plan["mods"]]}
        (stage / INDEX).write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        _verify(app, stage, index, f"ShowModManager-{plan['tag']}.zip")
        if out.exists():
            out.rmdir()
        os.replace(stage, out)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return index


def _size(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.1f} KB"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Release assets from an exported tree and a built runtime.")
    parser.add_argument("--tree", required=True, type=Path, help="the exported tree (tools/export.py)")
    parser.add_argument("--runtime", required=True, type=Path, help="the built runtime (tools/build_runtime.py)")
    parser.add_argument("--version", required=True, help="the release tag, like v0.1.0")
    parser.add_argument("--repo", required=True, help="the GitHub repository, owner/name")
    parser.add_argument("--out", required=True, type=Path, help="a new or empty folder")
    parser.add_argument("--launcher", type=Path, help=f"{LAUNCHER} (launcher/build.py)")
    parser.add_argument("--cache", type=Path, default=CACHE, help="where downloads are kept")
    args = parser.parse_args(argv)
    try:
        embed = fetch(*EMBED, cache=args.cache)
        index = package(args.tree, args.runtime, args.version, args.repo, args.out, embed, args.launcher)
    except PackageError as err:
        print(f"package refused: {err}", file=sys.stderr)
        return 1
    out = Path(args.out).absolute()
    print(f"packaged release {index['release']} (Fastball {index['fastball']}) in {out}")
    for path in sorted(out.iterdir()):
        print(f"  {path.name}: {_size(path.stat().st_size)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
