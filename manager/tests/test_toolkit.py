import dataclasses
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import config, toolkit

FASTBALL = "0.2.0+74a6525"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_setup(setup: Path, version: str = FASTBALL) -> None:
    """A fake Fastball setup: what the toolkit copies, plus what it must leave behind."""
    files = {
        "VERSION": version + "\n",
        "recomp/gen.py": "# gen\n",
        "recomp/patches.py": "WHO = 'setup'\n",
        "recomp/__pycache__/patches.cpython-314.pyc": "stale",
        "tools/build.py": "# build\n",
        "tools/data/patches-1.0.20.txt": "set walkup_light MLB_WALKUP_LIGHT 1\n",
        "runtime/libmlbrt.a": "SETUP LIB",
        "runtime/core/host.h": "// host\n",
        "runtime/core/patches.h": "// setup patches.h\n",
        "runtime/mlb/mlb.h": "// mlb\n",
        "runtime/deps/bin/SDL3.dll": "SDL",
        "llvm/bin/clang.exe": "clang",
        "llvm/bin/llvm-ar.exe": "llvm-ar",
        "bin/ninja.exe": "ninja",
        "python/python.exe": "python",
    }
    for rel, text in files.items():
        (setup / rel).parent.mkdir(parents=True, exist_ok=True)
        (setup / rel).write_text(text, encoding="utf-8")


OURS = {
    "runtime/libmlbrt.a": (b"OUR LIB", "runtime/libmlbrt.a"),
    "runtime/core/patches.h": (b"// our patches.h\n", "runtime/core/patches.h"),
    "recomp/patches.py": (b"WHO = 'toolkit'\n", "recomp/patches.py"),
    "runtime/core/pitch.h": (b"// new header\n", None),
    "runtime/core/patches.c": (b"// our patches.c\n", None),  # compiled per player build
}


def make_runtime(runtime_dir: Path, setup: Path, fastball: str = FASTBALL, indent=None) -> dict:
    """A fake release runtime laid over `setup`: every file in OURS, its base the setup's
    original (None: a file the setup does not have)."""
    files = {}
    for rel, (data, base) in OURS.items():
        (runtime_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (runtime_dir / rel).write_bytes(data)
        files[rel] = {"base": sha((setup / base).read_bytes()) if base else None,
                      "sha256": sha(data)}
    doc = {"fastball": fastball, "files": files}
    (runtime_dir / "runtime.json").write_text(json.dumps(doc, indent=indent), encoding="utf-8")
    return doc


def make_install(game: Path, release: str = FASTBALL) -> None:
    game.mkdir(parents=True, exist_ok=True)
    (game / "fastball-install.json").write_text(json.dumps({"release": release}), encoding="utf-8")


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


class _ToolkitCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = dataclasses.replace(
            config.Settings(), setup=self.tmp / "setup", game=self.tmp / "game",
            runtime_dir=self.tmp / "runtime", state=self.tmp / "state", mods=self.tmp / "mods")
        make_setup(self.settings.setup)
        make_install(self.settings.game)
        make_runtime(self.settings.runtime_dir, self.settings.setup)
        p = mock.patch("app.builder.BUILDS", self.tmp / "builds")
        p.start()
        self.addCleanup(p.stop)
        self.toolkit = self.tmp / "builds" / "toolkit"
        self.logged = []

    def _assemble(self):
        runtime = toolkit.load_runtime(self.settings)
        return toolkit.assemble(self.settings, runtime, self.logged.append)


class LoadRuntimeTests(_ToolkitCase):
    def test_reads_the_list(self):
        runtime = toolkit.load_runtime(self.settings)
        self.assertEqual(runtime["fastball"], FASTBALL)
        self.assertEqual(set(runtime["files"]), set(OURS))
        raw = (self.settings.runtime_dir / "runtime.json").read_bytes()
        self.assertEqual(runtime["json_sha256"], sha(raw))
        self.assertEqual(toolkit.runtime_sha256(self.settings), sha(raw))

    def test_runtime_file_with_wrong_sha_is_refused(self):
        lib = self.settings.runtime_dir / "runtime" / "libmlbrt.a"
        lib.write_bytes(b"OUR LIB, cut off")
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.load_runtime(self.settings)
        self.assertIn(str(lib), str(caught.exception))
        self.assertIn("again", str(caught.exception))  # says what to do
        lib.unlink()
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.load_runtime(self.settings)
        self.assertIn(str(lib), str(caught.exception))

    def test_missing_or_bad_list_is_a_plain_error(self):
        listing = self.settings.runtime_dir / "runtime.json"
        for text in ("{not json", json.dumps([1]), json.dumps({"fastball": FASTBALL}),
                     json.dumps({"fastball": FASTBALL, "files": {"../x": {"base": None,
                                                                         "sha256": "0" * 64}}}),
                     json.dumps({"fastball": FASTBALL, "files": {"C:/x": {"base": None,
                                                                          "sha256": "0" * 64}}}),
                     json.dumps({"fastball": FASTBALL, "files": {"bin/ninja.exe": {
                         "base": None, "sha256": "0" * 64}}})):
            listing.write_text(text, encoding="utf-8")
            with self.assertRaises(toolkit.ToolkitError, msg=text) as caught:
                toolkit.load_runtime(self.settings)
            self.assertIn("runtime", str(caught.exception))
        listing.unlink()
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.load_runtime(self.settings)
        self.assertIn(str(listing), str(caught.exception))
        self.assertEqual(toolkit.runtime_sha256(self.settings), "missing")


class VersionTests(_ToolkitCase):
    def test_versions_must_match_setup_and_install(self):
        runtime = toolkit.load_runtime(self.settings)
        toolkit.check_versions(self.settings, runtime)  # both match: no error
        (self.settings.setup / "VERSION").write_text("0.2.0+deadbee\n", encoding="utf-8")
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.check_versions(self.settings, runtime)
        self.assertIn(f"need Fastball {FASTBALL}", str(caught.exception))
        self.assertIn("0.2.0+deadbee", str(caught.exception))
        self.assertIn(str(self.settings.setup), str(caught.exception))
        (self.settings.setup / "VERSION").write_text(FASTBALL + "\n", encoding="utf-8")
        make_install(self.settings.game, "0.2.1+1234567")
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.check_versions(self.settings, runtime)
        self.assertIn(f"need Fastball {FASTBALL}", str(caught.exception))
        self.assertIn("0.2.1+1234567", str(caught.exception))
        (self.settings.game / "fastball-install.json").unlink()
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.check_versions(self.settings, runtime)
        self.assertIn("fastball-install.json", str(caught.exception))
        (self.settings.setup / "VERSION").unlink()
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.check_versions(self.settings, runtime)
        self.assertIn("VERSION", str(caught.exception))

    def test_no_setup_chosen_names_the_setting(self):
        runtime = toolkit.load_runtime(self.settings)
        settings = dataclasses.replace(self.settings, setup=None)
        for call in (lambda: toolkit.check_versions(settings, runtime),
                     lambda: toolkit.assemble(settings, runtime, self.logged.append)):
            with self.assertRaises(toolkit.ToolkitError) as caught:
                call()
            self.assertIn("Setup folder", str(caught.exception))
        self.assertFalse(self.toolkit.exists())


class AssembleTests(_ToolkitCase):
    def test_overlay_lays_files_and_skips_when_stamp_matches(self):
        root = self._assemble()
        self.assertEqual(root, self.toolkit)
        setup = self.settings.setup
        for rel, (data, _) in OURS.items():
            self.assertEqual((root / rel).read_bytes(), data, rel)
        for rel in ("VERSION", "recomp/gen.py", "tools/build.py", "tools/data/patches-1.0.20.txt",
                    "runtime/core/host.h", "runtime/mlb/mlb.h", "runtime/deps/bin/SDL3.dll"):
            self.assertEqual((root / rel).read_bytes(), (setup / rel).read_bytes(), rel)
        # the toolchain and the setup's Python stay in the setup; no stale bytecode
        for rel in ("llvm", "python", "bin", "recomp/__pycache__"):
            self.assertFalse((root / rel).exists(), rel)
        self.assertFalse(self.toolkit.with_name("toolkit.new").exists())
        before = snapshot(root)
        mtime = (root / "recomp" / "gen.py").stat().st_mtime_ns

        self.logged.clear()
        with mock.patch("app.toolkit.shutil.copy2") as copy2, \
                mock.patch("app.toolkit.shutil.copytree") as copytree:
            self.assertEqual(self._assemble(), root)
        copy2.assert_not_called()
        copytree.assert_not_called()
        self.assertTrue(any("up to date" in line for line in self.logged), self.logged)
        self.assertEqual(snapshot(root), before)
        self.assertEqual((root / "recomp" / "gen.py").stat().st_mtime_ns, mtime)

        # a new runtime (another runtime.json) lays the toolkit again
        new_lib = b"OUR LIB v2"
        (self.settings.runtime_dir / "runtime" / "libmlbrt.a").write_bytes(new_lib)
        doc = json.loads((self.settings.runtime_dir / "runtime.json").read_text(encoding="utf-8"))
        doc["files"]["runtime/libmlbrt.a"]["sha256"] = sha(new_lib)
        (self.settings.runtime_dir / "runtime.json").write_text(json.dumps(doc), encoding="utf-8")
        self._assemble()
        self.assertEqual((root / "runtime" / "libmlbrt.a").read_bytes(), new_lib)

    def test_toolkit_from_an_older_layout_is_laid_again(self):
        root = self._assemble()
        mtime = (root / "recomp" / "gen.py").stat().st_mtime_ns
        self.assertEqual(self._assemble(), root)  # unchanged: skipped
        self.assertEqual((root / "recomp" / "gen.py").stat().st_mtime_ns, mtime)
        # as an older manager left it: same runtime and VERSION, other layout, no deps
        shutil.rmtree(root / "runtime" / "deps")
        with mock.patch("app.toolkit.LAYOUT", toolkit.LAYOUT - 1):
            old = toolkit.stamp(self.settings, toolkit.load_runtime(self.settings))
        (root / toolkit.STAMP).write_text(old, encoding="utf-8")
        self.logged.clear()
        self._assemble()
        self.assertTrue(any("Laying out" in line for line in self.logged), self.logged)
        self.assertEqual((root / "runtime" / "deps" / "bin" / "SDL3.dll").read_bytes(), b"SDL")

    def test_another_setup_folder_is_laid_again_and_checked(self):
        root = self._assemble()
        other = self.tmp / "other setup"
        make_setup(other)  # the same VERSION
        (other / "tools" / "build.py").write_text("# other build\n", encoding="utf-8")
        self.settings = dataclasses.replace(self.settings, setup=other)
        self._assemble()
        self.assertEqual((root / "tools" / "build.py").read_text(encoding="utf-8"), "# other build\n")
        self.assertEqual((root / "recomp" / "patches.py").read_bytes(), OURS["recomp/patches.py"][0])
        # and its originals are checked: an edited one there is refused, the toolkit kept
        before = snapshot(root)
        third = self.tmp / "third setup"
        make_setup(third)
        (third / "recomp" / "patches.py").write_text("WHO = 'edited'\n", encoding="utf-8")
        self.settings = dataclasses.replace(self.settings, setup=third)
        with self.assertRaises(toolkit.ToolkitError) as caught:
            self._assemble()
        self.assertIn(str(third / "recomp" / "patches.py"), str(caught.exception))
        self.assertEqual(snapshot(root), before)

    def test_overlay_refuses_a_changed_original(self):
        root = self._assemble()
        before = snapshot(root)
        setup, rdir = self.settings.setup, self.settings.runtime_dir
        # another runtime.json, so the stamp no longer matches and the toolkit is laid again
        make_runtime(rdir, setup, indent=1)
        (setup / "recomp" / "patches.py").write_text("WHO = 'edited setup'\n", encoding="utf-8")
        with self.assertRaises(toolkit.ToolkitError) as caught:
            self._assemble()
        self.assertIn(str(setup / "recomp" / "patches.py"), str(caught.exception))
        self.assertIn(FASTBALL, str(caught.exception))
        self.assertEqual(snapshot(root), before)
        self.assertFalse(self.toolkit.with_name("toolkit.new").exists())

        # an original the runtime expects to be absent (base null) is there
        make_runtime(rdir, setup, indent=2)
        (setup / "runtime" / "core" / "pitch.h").write_text("// theirs\n", encoding="utf-8")
        with self.assertRaises(toolkit.ToolkitError) as caught:
            self._assemble()
        self.assertIn(str(setup / "runtime" / "core" / "pitch.h"), str(caught.exception))
        self.assertEqual(snapshot(root), before)

        # an original the runtime expects is missing
        (setup / "runtime" / "core" / "pitch.h").unlink()
        make_runtime(rdir, setup, indent=3)
        (setup / "runtime" / "core" / "patches.h").unlink()
        with self.assertRaises(toolkit.ToolkitError) as caught:
            self._assemble()
        self.assertIn(str(setup / "runtime" / "core" / "patches.h"), str(caught.exception))
        self.assertEqual(snapshot(root), before)

    def test_copy_failure_leaves_the_old_toolkit(self):
        root = self._assemble()
        before = snapshot(root)
        make_runtime(self.settings.runtime_dir, self.settings.setup, indent=1)
        with mock.patch("app.toolkit.shutil.copy2", side_effect=OSError("disk full")):
            with self.assertRaises(toolkit.ToolkitError) as caught:
                self._assemble()
        self.assertIn("disk full", str(caught.exception))
        self.assertEqual(snapshot(root), before)
        self.assertFalse(self.toolkit.with_name("toolkit.new").exists())

    def test_setup_missing_what_the_build_needs_is_refused(self):
        for rel in ("recomp/gen.py", "bin/ninja.exe", "llvm/bin/llvm-ar.exe"):
            (self.settings.setup / rel).rename(self.tmp / "moved")
            with self.assertRaises(toolkit.ToolkitError) as caught:
                self._assemble()
            self.assertIn(str(self.settings.setup / rel), str(caught.exception))
            self.assertIn("Setup folder", str(caught.exception))
            self.assertFalse(self.toolkit.exists())
            (self.tmp / "moved").rename(self.settings.setup / rel)


if __name__ == "__main__":
    unittest.main()
