"""tools/package.py: release assets from an exported tree and a built runtime.

Published with the tree (manager/tests/ there, the tools at the tree's root tools/), so it finds the tools folder in
either layout, and the manager's app/ beside this tests folder in both."""
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


def _tools() -> Path:
    here = Path(__file__).resolve().parent
    for folder in (here.parent / "tools", here.parent.parent / "tools"):
        if (folder / "package.py").is_file():
            return folder
    raise FileNotFoundError(f"no tools/package.py beside or above {here}")


TOOLS = _tools()
_SPEC = importlib.util.spec_from_file_location("package_tool", TOOLS / "package.py")
package = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(package)

APP = Path(__file__).resolve().parent.parent / "app"
FASTBALL = "0.2.0+74a6525"
TAG = "v0.1.0"
REPO = "someone/show-mod-manager"
PATCH = b"diff --git a/runtime/core/x.h b/runtime/core/x.h\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8", newline="")
    else:
        path.write_bytes(data)
    return path


def make_tree(tree: Path) -> None:
    """An exported tree: the real manager app, two mods (a uses a shared file), the runtime folder."""
    shutil.copytree(APP, tree / "manager" / "app", ignore=shutil.ignore_patterns("__pycache__"))
    put(tree / "manager" / "web" / "index.html", "<!doctype html>\n")
    card = {"tagline": "A does things", "highlights": [["One", "first thing"]], "note": ""}
    put(tree / "mods" / "a" / "mod.json", json.dumps({
        "name": "Mod A", "description": "The first mod", "art": "ICON_A_LG", "order": 1, "version": "6",
        "card": "release/card.json", "features": "release/features.md", "patches": "patches.txt",
        "files": {"install": ["python", "{mod}/tools/install.py"]}, "profile": "own"}))
    put(tree / "mods" / "a" / "release" / "card.json", json.dumps(card))
    put(tree / "mods" / "a" / "release" / "features.md", "## Pitching\n- Faster pitches\n")
    put(tree / "mods" / "a" / "patches.txt", "set a MLB_A 1\n")
    put(tree / "mods" / "a" / "tools" / "install.py", "print('a')\n")
    put(tree / "mods" / "b" / "mod.json", json.dumps({
        "name": "Mod B", "description": "The second mod", "art": "picture.png", "order": 2,
        "runtime": {"branch": "mod/b", "env": {"MLB_B": "1"}}}))
    put(tree / "mods" / "b" / "picture.png", b"\x89PNG\r\n\x1a\n")
    put(tree / "mods" / "_shared" / "romfs_index.py", "# shared\n")
    put(tree / "mods" / "_shared" / "shared.json", json.dumps({"romfs_index.py": ["a"]}))
    put(tree / "runtime" / "fastball.version", f"tag=v0.2.0\nrelease={FASTBALL}\ncommit=74a6525\n")
    put(tree / "runtime" / "mods.patch", PATCH)
    put(tree / "LICENSE", "MIT License\n")
    put(tree / "THIRD_PARTY_NOTICES.md", "# Third-party notices\n")


def make_runtime(folder: Path, patch: bytes = PATCH) -> None:
    files = {"runtime/libmlbrt.a": (b"LIB", "f" * 64), "runtime/core/patches.c": (b"// c\n", None),
             "recomp/patches.py": (b"# py\n", "e" * 64)}
    listing = {}
    for rel, (data, base) in files.items():
        put(folder / rel, data)
        listing[rel] = {"base": base, "sha256": sha(data)}
    put(folder / "runtime.json", json.dumps({"fastball": FASTBALL, "patch": sha(patch), "files": listing}))


def make_embed(path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("python314._pth", "python314.zip\n.\n\n# Uncomment to run site.main() automatically\n#import site\n")
        zf.writestr("python314.zip", b"PK stdlib")
        zf.writestr("python.exe", b"MZ python")
        zf.writestr("pythonw.exe", b"MZ pythonw")
        zf.writestr("python314.dll", b"MZ dll")
    put(path, buf.getvalue())


class PackageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.tree, self.runtime, self.out = self.tmp / "tree", self.tmp / "rt", self.tmp / "out"
        self.embed, self.launcher = self.tmp / "embed.zip", self.tmp / "launcher.exe"
        make_tree(self.tree)
        make_runtime(self.runtime)
        make_embed(self.embed)
        put(self.launcher, b"MZ launcher")

    def run_package(self, **changes):
        args = dict(tree=self.tree, runtime=self.runtime, version=TAG, repo=REPO, out=self.out,
                    embed=self.embed, launcher=self.launcher)
        args.update(changes)
        return package.package(**args)

    def names(self, name: str) -> list[str]:
        with zipfile.ZipFile(self.out / name) as zf:
            return sorted(zf.namelist())

    def read(self, name: str, member: str) -> bytes:
        with zipfile.ZipFile(self.out / name) as zf:
            return zf.read(member)

    def test_index_lists_every_mod_with_sha(self):
        self.run_package()
        index = json.loads((self.out / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["release"], TAG)
        self.assertEqual(index["fastball"], FASTBALL)
        self.assertEqual([m["id"] for m in index["mods"]], ["a", "b"])
        for entry in [index["runtime"], *index["mods"]]:
            data = (self.out / entry["file"]).read_bytes()
            self.assertEqual(entry["sha256"], sha(data))
            self.assertEqual(entry["size"], len(data))
        self.assertEqual(index["runtime"]["file"], "runtime-0.2.0-74a6525.zip")
        a, b = index["mods"]
        self.assertEqual(a["file"], f"mod-a-{TAG}.zip")
        self.assertEqual((a["name"], a["version"], a["description"]), ("Mod A", "6", "The first mod"))
        self.assertEqual(b["version"], TAG)   # no version in its mod.json: the release tag, as before
        self.assertEqual(a["art"], "ICON_A_LG")
        self.assertEqual(a["card"]["tagline"], "A does things")
        self.assertTrue(a["features"])
        self.assertEqual(a["parts"], ["files", "patches", "profile"])
        self.assertNotIn("art", b)  # its own picture file: the picker shows only CROSS/LIVE art
        self.assertEqual(b["parts"], ["runtime"])
        self.assertNotIn("card", b)
        # what the manager reads from it
        app = package.manager_code(self.tree)
        app.releases._check_index(index, "index.json")
        self.assertEqual(sorted(self.names("runtime-0.2.0-74a6525.zip")),
                         ["THIRD_PARTY_NOTICES.md", "recomp/patches.py", "runtime.json", "runtime/core/patches.c",
                          "runtime/libmlbrt.a"])

    def test_manager_zip_has_release_json_and_pth(self):
        self.run_package()
        name = f"ShowModManager-{TAG}.zip"
        names = self.names(name)
        self.assertEqual(json.loads(self.read(name, "release.json")),
                         {"repo": REPO, "tag": TAG, "fastball": FASTBALL})
        self.assertEqual(self.read(name, "python/python314._pth").decode(), "python314.zip\n.\n..\n")
        for member in ("python/pythonw.exe", "python/python314.zip", "app/__main__.py", "app/releases.py",
                       "web/index.html", "Show Mod Manager.exe"):
            self.assertIn(member, names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.startswith("mods/")])

    def test_notices_are_in_the_manager_and_runtime_zips(self):
        self.run_package()
        manager, runtime = f"ShowModManager-{TAG}.zip", "runtime-0.2.0-74a6525.zip"
        self.assertEqual(self.read(manager, "LICENSE"), b"MIT License\n")
        self.assertEqual(self.read(manager, "THIRD_PARTY_NOTICES.md"), b"# Third-party notices\n")
        self.assertEqual(self.read(runtime, "THIRD_PARTY_NOTICES.md"), b"# Third-party notices\n")
        self.assertNotIn("LICENSE", self.names(runtime))

    def test_refuses_a_tree_without_its_notices(self):
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            with self.subTest(name=name):
                data = (self.tree / name).read_bytes()
                (self.tree / name).unlink()
                with self.assertRaises(package.PackageError) as caught:
                    self.run_package()
                self.assertIn(name, str(caught.exception))
                self.assertFalse(self.out.exists())
                put(self.tree / name, data)

    def test_embed_files_are_the_unchanged_members(self):
        # what the release check may leave to the path rules: python/<member> byte-for-byte from the pinned zip
        self.run_package()
        files = package.embed_files(self.embed)
        self.assertEqual(sorted(files), ["python/python.exe", "python/python314.dll", "python/python314.zip",
                                         "python/pythonw.exe"])  # not the ._pth it rewrites
        for member, digest in files.items():
            self.assertEqual(sha(self.read(f"ShowModManager-{TAG}.zip", member)), digest)

    def test_manager_zip_without_launcher(self):
        self.run_package(launcher=None)
        self.assertNotIn("Show Mod Manager.exe", self.names(f"ShowModManager-{TAG}.zip"))

    def test_mod_zip_has_only_its_files(self):
        self.run_package()
        self.assertEqual(self.names(f"mod-a-{TAG}.zip"),
                         ["_shared/romfs_index.py", "mod.json", "patches.txt", "release/card.json",
                          "release/features.md", "tools/install.py"])
        self.assertEqual(self.names(f"mod-b-{TAG}.zip"), ["mod.json", "picture.png"])
        self.assertEqual(self.read(f"mod-a-{TAG}.zip", "_shared/romfs_index.py"), b"# shared\n")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         sorted(["index.json", f"ShowModManager-{TAG}.zip", f"mod-a-{TAG}.zip",
                                 f"mod-b-{TAG}.zip", "runtime-0.2.0-74a6525.zip"]))

    def test_zips_are_the_same_every_time(self):
        self.run_package()
        first = (self.out / "index.json").read_bytes()
        shutil.rmtree(self.out)
        self.run_package()
        self.assertEqual((self.out / "index.json").read_bytes(), first)

    def test_refuses_names_that_are_not_plain(self):
        for tag in ("v0.1.0+x", "v 1", "../v1", ""):
            with self.subTest(tag=tag), self.assertRaises(package.PackageError):
                self.run_package(version=tag)
        with self.assertRaises(package.PackageError):
            self.run_package(repo="not a repo")
        shutil.copytree(self.tree / "mods" / "b", self.tree / "mods" / "b.2")
        with self.assertRaises(package.PackageError) as caught:
            self.run_package()
        self.assertIn("b.2", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_refuses_a_runtime_built_from_another_patch(self):
        make_runtime(self.runtime, patch=b"another patch\n")
        with self.assertRaises(package.PackageError) as caught:
            self.run_package()
        self.assertIn("mods.patch", str(caught.exception))

    def test_refuses_a_damaged_runtime(self):
        (self.runtime / "runtime" / "libmlbrt.a").write_bytes(b"LIB, changed")
        with self.assertRaises(package.PackageError) as caught:
            self.run_package()
        self.assertIn("libmlbrt.a", str(caught.exception))

    def test_writes_no_bytecode_into_the_tree(self):
        # it imports the tree's own app: a __pycache__ there would hold this PC's folders and be published
        before = sys.dont_write_bytecode
        self.addCleanup(setattr, sys, "dont_write_bytecode", before)
        sys.dont_write_bytecode = False  # as when it is run by hand without -B
        self.run_package()
        cached = [p for p in self.tree.rglob("*") if p.name == "__pycache__" or p.suffix in (".pyc", ".pyo")]
        self.assertEqual(cached, [])

    def test_refuses_an_out_folder_that_is_not_empty(self):
        put(self.out / "old.zip", b"x")
        with self.assertRaises(package.PackageError):
            self.run_package()


class FetchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.source = put(self.tmp / "src" / "python-embed.zip", b"embeddable bytes")
        self.url = self.source.as_uri()
        self.cache = self.tmp / "cache"

    def test_downloads_once_then_uses_the_cache(self):
        got = package.fetch(self.url, sha(b"embeddable bytes"), self.cache)
        self.assertEqual(got.read_bytes(), b"embeddable bytes")
        self.source.unlink()
        self.assertEqual(package.fetch(self.url, sha(b"embeddable bytes"), self.cache), got)

    def test_wrong_sha_is_refused_and_not_kept(self):
        with self.assertRaises(package.PackageError) as caught:
            package.fetch(self.url, "0" * 64, self.cache)
        self.assertIn("sha256", str(caught.exception))
        self.assertEqual([p for p in self.cache.rglob("*") if p.is_file()], [])


if __name__ == "__main__":
    unittest.main()
