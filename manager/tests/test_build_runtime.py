"""tools/build_runtime.py: the checks and the runtime listing around the library build (the build itself
needs llvm-mingw and the game's gen headers, so it runs on the maintainer's PC only).

Published with the tree (manager/tests/ there, the tools at the tree's root tools/), so it finds the tools folder in
either layout. Needs git."""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


def _tools() -> Path:
    here = Path(__file__).resolve().parent
    for folder in (here.parent / "tools", here.parent.parent / "tools"):
        if (folder / "build_runtime.py").is_file():
            return folder
    raise FileNotFoundError(f"no tools/build_runtime.py beside or above {here}")


TOOLS = _tools()
_SPEC = importlib.util.spec_from_file_location("build_runtime", TOOLS / "build_runtime.py")
br = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(br)

# the commit address is put together so that this published file holds no e-mail address
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t" + "@" + "localhost.invalid", "-c",
       "core.autocrlf=false"]
LIB_BASE = "a" * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo: Path, *args) -> str:
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def put(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


STOCK = {
    "VERSION": b"0.2.0\n",
    ".gitattributes": b"* text=auto eol=lf\n",
    "runtime/core/host.h": b"// host\n",
    "runtime/core/patches.c": b"// stock patches.c\n",
    "runtime/mlb/mlb.c": b"// mlb\n",
    "runtime/mlb/hid.c": b"// hid\n",
    "recomp/patches.py": b"WHO = 'stock'\n",
    "recomp/notes.md": b"notes\n",
    "tools/build.py": b"# build\n",
}
MODDED = {
    "runtime/core/host.h": b"// host, modded\n",
    "runtime/core/pitch.h": b"// new header\n",
    "runtime/core/patches.c": b"// modded patches.c\n",
    "runtime/mlb/mlb.c": b"// mlb, modded\n",
    "recomp/patches.py": b"WHO = 'modded'\n",
    "recomp/notes.md": b"notes, modded\n",
}


class _Repo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "src"
        self.src.mkdir()
        git(self.src, "init", "-q")
        for rel, data in STOCK.items():
            put(self.src, rel, data)
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "stock")
        self.commit = git(self.src, "rev-parse", "HEAD")
        self.release = f"0.2.0+{self.commit[:7]}"
        self.pins = {self.release: {"commit": self.commit, "libmlbrt.a": LIB_BASE}}
        self.patch = self.make_patch(MODDED)
        self.clone = self.tmp / "clone"
        subprocess.run([*GIT, "clone", "-q", str(self.src), str(self.clone)], check=True)

    def make_patch(self, files: dict, name="mods.patch") -> Path:
        work = self.tmp / ("work-" + name)
        subprocess.run([*GIT, "clone", "-q", str(self.src), str(work)], check=True)
        for rel, data in files.items():
            put(work, rel, data)
        git(work, "add", "-A")
        diff = subprocess.run([*GIT, "-C", str(work), "diff", "--binary", "--cached", "HEAD"], check=True,
                              capture_output=True).stdout
        path = self.tmp / name
        path.write_bytes(diff)
        return path


class CloneTests(_Repo):
    def test_pinned_clean_clone_is_accepted(self):
        self.assertEqual(br.check_clone(self.clone, self.pins), (self.release, self.pins[self.release]))

    def test_release_without_a_pin_is_refused(self):
        with self.assertRaises(br.BuildError) as caught:
            br.check_clone(self.clone, {"0.2.0+1234567": {"commit": "1" * 40, "libmlbrt.a": LIB_BASE}})
        self.assertIn(self.release, str(caught.exception))
        self.assertIn("no pin", str(caught.exception))

    def test_clone_at_another_commit_is_refused(self):
        put(self.clone, "runtime/core/host.h", b"// later\n")
        git(self.clone, "commit", "-q", "-am", "later")
        with self.assertRaises(br.BuildError):
            br.check_clone(self.clone, self.pins)

    def test_changed_clone_is_refused(self):
        put(self.clone, "runtime/mlb/extra.c", b"// stray\n")
        with self.assertRaises(br.BuildError) as caught:
            br.check_clone(self.clone, self.pins)
        self.assertIn("fresh clone", str(caught.exception))


class PatchTests(_Repo):
    def test_patch_is_applied_and_its_files_listed(self):
        changed = br.apply_patch(self.clone, self.patch)
        self.assertEqual(changed, sorted(MODDED))
        self.assertEqual((self.clone / "runtime" / "core" / "pitch.h").read_bytes(), b"// new header\n")

    def test_patch_that_does_not_apply_changes_nothing(self):
        put(self.clone, "runtime/core/host.h", b"// someone else's\n")
        git(self.clone, "commit", "-q", "-am", "x")
        with self.assertRaises(br.BuildError) as caught:
            br.apply_patch(self.clone, self.patch)
        self.assertIn("does not apply", str(caught.exception))
        self.assertEqual(git(self.clone, "status", "--porcelain"), "")

    def test_patch_outside_runtime_and_recomp_or_on_the_test_driver_is_refused(self):
        for files, named in (({"tools/build.py": b"# changed\n"}, "tools/build.py"),
                             ({"runtime/mlb/hid.c": b"// driver\n"}, "runtime/mlb/hid.c")):
            with self.subTest(named):
                patch = self.make_patch(files, name=named.replace("/", "-") + ".patch")
                with self.assertRaises(br.BuildError) as caught:
                    br.apply_patch(self.clone, patch)
                self.assertIn(named, str(caught.exception))
                self.assertEqual(git(self.clone, "status", "--porcelain"), "")

    def test_patch_deleting_a_file_is_refused(self):
        work = self.tmp / "del"
        subprocess.run([*GIT, "clone", "-q", str(self.src), str(work)], check=True)
        git(work, "rm", "-q", "runtime/core/host.h")
        patch = self.tmp / "del.patch"
        patch.write_bytes(subprocess.run([*GIT, "-C", str(work), "diff", "--cached", "HEAD"], check=True,
                                         capture_output=True).stdout)
        with self.assertRaises(br.BuildError) as caught:
            br.apply_patch(self.clone, patch)
        self.assertIn("runtime/core/host.h", str(caught.exception))


class ListingTests(_Repo):
    def test_runtime_lists_lib_headers_recomp_and_patches_c_with_bases(self):
        changed = br.apply_patch(self.clone, self.patch)
        lib = self.tmp / "libmlbrt.a"
        lib.write_bytes(b"STRIPPED LIB")
        out = self.tmp / "out"
        br.write_runtime(self.clone, self.commit, self.release, self.pins[self.release], changed, lib,
                         self.patch, out)
        doc = json.loads((out / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["fastball"], self.release)
        self.assertEqual(doc["patch"], sha(self.patch.read_bytes()))
        want = {
            "runtime/libmlbrt.a": (b"STRIPPED LIB", LIB_BASE),
            "runtime/core/host.h": (MODDED["runtime/core/host.h"], sha(STOCK["runtime/core/host.h"])),
            "runtime/core/pitch.h": (MODDED["runtime/core/pitch.h"], None),
            "runtime/core/patches.c": (MODDED["runtime/core/patches.c"], None),
            "recomp/patches.py": (MODDED["recomp/patches.py"], sha(STOCK["recomp/patches.py"])),
        }
        self.assertEqual(doc["files"], {rel: {"base": base, "sha256": sha(data)}
                                        for rel, (data, base) in want.items()})
        for rel, (data, _) in want.items():
            self.assertEqual((out / rel).read_bytes(), data, rel)
        # mlb.c is in the library; recomp/notes.md is not a file the setup has
        self.assertFalse((out / "runtime" / "mlb" / "mlb.c").exists())
        self.assertFalse((out / "recomp" / "notes.md").exists())

    def test_overlay_files_are_the_patched_blobs(self):
        """What is written comes from git, not from the working tree (an eol conversion there must not
        change the bytes the bases and the player's setup are compared with)."""
        changed = br.apply_patch(self.clone, self.patch)
        (self.clone / "runtime" / "core" / "host.h").write_bytes(b"// host, modded\r\n")
        lib = self.tmp / "libmlbrt.a"
        lib.write_bytes(b"LIB")
        br.write_runtime(self.clone, self.commit, self.release, self.pins[self.release], changed, lib,
                         self.patch, self.tmp / "out")
        self.assertEqual((self.tmp / "out" / "runtime" / "core" / "host.h").read_bytes(), b"// host, modded\n")

    def test_out_that_is_not_empty_is_refused(self):
        changed = br.apply_patch(self.clone, self.patch)
        put(self.tmp, "out/old.txt", b"x")
        with self.assertRaises(br.BuildError):
            br.write_runtime(self.clone, self.commit, self.release, self.pins[self.release], changed,
                             self.tmp / "lib", self.patch, self.tmp / "out")


class DepsTests(unittest.TestCase):
    def test_only_patches_o_may_include_patches_h(self):
        text = ("obj/rt/runtime/core/patches.o: #deps 3, deps mtime 1 (VALID)\n"
                "    runtime/core/patches.c\n    work/gen/patches.h\n    runtime/core/host.h\n\n"
                "obj/rt/runtime/mlb/mlb.o: #deps 2, deps mtime 1 (VALID)\n"
                "    runtime/mlb/mlb.c\n    work/gen/layout.h\n\n")
        self.assertEqual(br.users_of(text, "work/gen/patches.h"), ["obj/rt/runtime/core/patches.o"])
        bad = text + "obj/rt/runtime/mlb/pitch.o: #deps 1, deps mtime 1 (VALID)\n    work/gen/patches.h\n"
        self.assertEqual(br.users_of(bad, "work/gen/patches.h"),
                         ["obj/rt/runtime/core/patches.o", "obj/rt/runtime/mlb/pitch.o"])
        br.check_patches_h(text, "work/gen/patches.h")
        with self.assertRaises(br.BuildError) as caught:
            br.check_patches_h(bad, "work/gen/patches.h")
        self.assertIn("obj/rt/runtime/mlb/pitch.o", str(caught.exception))
        # no object at all: the header was not seen, so nothing is proven
        with self.assertRaises(br.BuildError):
            br.check_patches_h(text.replace("    work/gen/patches.h\n", ""), "work/gen/patches.h")


class LocalPathTests(unittest.TestCase):
    def test_prefix_maps_name_the_clone_and_toolchain_plainly(self):
        clone, toolchain = Path("D:/Build/fastball"), Path("D:/Setup/llvm")
        value = br.prefix_maps(clone, toolchain)
        self.assertTrue(value.startswith("# "))  # clang applies it without printing
        self.assertIn(f"+-ffile-prefix-map={str(clone.absolute()).replace(chr(92), '/')}=.", value.split())
        self.assertIn(f"+-ffile-prefix-map={str(toolchain.absolute()).replace(chr(92), '/')}=llvm-mingw",
                      value.split())
        with self.assertRaises(br.BuildError):
            br.prefix_maps(Path("D:/Build/my fastball"), toolchain)

    def test_local_paths_are_found_in_either_slash_and_case(self):
        home = Path("D:/Home/Someone")
        self.assertEqual(br.local_paths(b"x\0d:/home/someone/llvm\0", [home]), [str(home.absolute())])
        self.assertEqual(br.local_paths(rb"D:\HOME\SOMEONE\x", [home]), [str(home.absolute())])
        self.assertEqual(br.local_paths(b"./runtime/core\0llvm-mingw/include", [home]), [])


if __name__ == "__main__":
    unittest.main()
