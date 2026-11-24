import dataclasses
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app import config, releases, toolkit

FASTBALL = "0.2.0+74a6525"
# A status script for a mod's files step: "installed" while a marker file sits in the mod folder.
STATUS = ("import pathlib, sys; "
          "print('MARKER: ' + ('modded' if pathlib.Path(sys.argv[1]).exists() else 'stock'))")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zip_bytes(members: dict) -> bytes:
    """A zip of {name or zipfile.ZipInfo: bytes}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def runtime_members(label: str) -> dict:
    lib = f"LIB {label}".encode()
    header = f"// header {label}\n".encode()
    listing = {"fastball": FASTBALL, "files": {
        "runtime/libmlbrt.a": {"base": None, "sha256": sha(lib)},
        "runtime/core/patches.h": {"base": None, "sha256": sha(header)},
    }}
    return {"runtime.json": json.dumps(listing).encode(), "runtime/libmlbrt.a": lib,
            "runtime/core/patches.h": header}


def mod_members(mod_id: str, label: str, files: dict | None = None) -> dict:
    manifest = {"name": mod_id.title(), "description": f"{mod_id} {label}"}
    if files:
        manifest["files"] = files
    return {"mod.json": json.dumps(manifest).encode(), "data/version.txt": label.encode()}


class Crash(BaseException):
    """The process dying: nothing catches it."""


def fail_folder_move(nth: int, error: BaseException, after_the_move: bool = False):
    """An os.replace that raises `error` on the nth folder move and on every retry of that
    same move (file moves, the JSON writes, always pass); after_the_move: the nth move happens,
    then `error` is raised. Moving a, then b, then the runtime out and their new folders in:
    the 4th folder move is b's new folder going in."""
    real, count, failing = os.replace, [0], []

    def replace(src, dst):
        if (src, dst) in failing:
            raise error
        if Path(src).is_dir():
            count[0] += 1
            if count[0] == nth:
                failing.append((src, dst))
                if after_the_move:
                    real(src, dst)
                raise error
        return real(src, dst)
    return replace


class ReleaseFolder:
    """A local-folder release: index.json plus the zips, built from member dicts."""

    def __init__(self, folder: Path, release: str):
        self.folder = folder
        self.release = release
        folder.mkdir(parents=True)
        self.index = {"release": release, "fastball": FASTBALL, "runtime": None, "mods": []}

    def _put(self, name: str, data: bytes) -> dict:
        (self.folder / name).write_bytes(data)
        return {"file": name, "sha256": sha(data), "size": len(data)}

    def runtime(self, members: dict | None = None) -> None:
        data = zip_bytes(members if members is not None else runtime_members(self.release))
        self.index["runtime"] = self._put(f"runtime-{FASTBALL}.zip", data)
        self.save()

    def mod(self, mod_id: str, members: dict | None = None) -> dict:
        data = zip_bytes(members if members is not None else mod_members(mod_id, self.release))
        entry = {"id": mod_id, "name": mod_id.title(), "version": self.release,
                 "description": "", "card": {}, "features": [], "parts": ["files"]}
        entry.update(self._put(f"mod-{mod_id}-{self.release}.zip", data))
        self.index["mods"] = [m for m in self.index["mods"] if m["id"] != mod_id] + [entry]
        self.save()
        return entry

    def save(self) -> None:
        (self.folder / "index.json").write_text(json.dumps(self.index), encoding="utf-8")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.use_root("main")
        self.lines = []
        self.log = self.lines.append

    def use_root(self, name: str) -> None:
        """Fresh mods, state, runtime and release folders under <temp>/with space/<name>."""
        self.tmp = Path(self._tmp.name) / "with space" / name
        self.tmp.mkdir(parents=True)
        self.settings = dataclasses.replace(
            config.Settings(), mods=self.tmp / "mods", state=self.tmp / "state",
            runtime_dir=self.tmp / "manager" / "runtime")
        self.settings.mods.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def release(self, tag: str, mod_ids=("a", "b", "c")) -> ReleaseFolder:
        rel = ReleaseFolder(self.tmp / "release" / tag, tag)
        rel.runtime()
        for mod_id in mod_ids:
            rel.mod(mod_id)
        self.settings.release_source = str(rel.folder)
        return rel

    def index(self) -> dict:
        return releases.fetch_index(self.settings, None)

    def version(self, mod_id: str) -> str:
        return (self.settings.mods / mod_id / "data" / "version.txt").read_text()

    def runtime_label(self) -> str:
        return (Path(self.settings.runtime_dir) / "runtime" / "libmlbrt.a").read_text()

    def records(self) -> dict:
        return json.loads((self.settings.state / "installed.json").read_text(encoding="utf-8"))

    def snapshot(self) -> dict:
        """Every file under the temp folder except the release sources: {relpath: bytes}."""
        out = {}
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                path = Path(root) / name
                rel = path.relative_to(self.tmp).as_posix()
                if not rel.startswith("release/"):
                    out[rel] = path.read_bytes()
        return out

    def leftovers(self) -> list[str]:
        """Staging and temp folders that must never outlive a call."""
        found = [p.name for p in Path(self.settings.mods).iterdir() if p.name.startswith(".")]
        runtime = Path(self.settings.runtime_dir)
        if runtime.parent.exists():
            found += [p.name for p in runtime.parent.iterdir() if p.name != runtime.name]
        if self.settings.state.exists():
            found += [p.name for p in self.settings.state.iterdir() if p.name != "installed.json"]
        return found

    # --- the release and its index -------------------------------------------------------
    def test_release_info_is_none_without_release_json(self):
        self.assertIsNone(releases.release_info(self.tmp))
        (self.tmp / "release.json").write_text(json.dumps(
            {"repo": "owner/name", "tag": "2026.11.1", "fastball": FASTBALL}), encoding="utf-8")
        self.assertEqual(releases.release_info(self.tmp)["tag"], "2026.11.1")

    def test_source_defaults_to_the_github_release(self):
        info = {"repo": "owner/name", "tag": "2026.11.1", "fastball": FASTBALL}
        self.assertEqual(releases.source(self.settings, info),
                         "https://github.com/owner/name/releases/download/2026.11.1/")
        self.settings.release_source = str(self.tmp)
        self.assertEqual(releases.source(self.settings, info), str(self.tmp))
        self.settings.release_source = ""
        with self.assertRaises(releases.ReleaseError):
            releases.source(self.settings, None)

    def test_fetch_index_reads_a_folder_source(self):
        rel = self.release("2026.11.1")
        index = self.index()
        self.assertEqual(index["release"], "2026.11.1")
        self.assertEqual([m["id"] for m in index["mods"]], ["a", "b", "c"])
        rel.index["mods"][0]["file"] = "../elsewhere.zip"
        rel.save()
        with self.assertRaises(releases.ReleaseError) as caught:
            self.index()
        self.assertIn("index.json", str(caught.exception))

    # --- install ------------------------------------------------------------------------
    def test_installs_ticked_mods_and_runtime(self):
        self.release("2026.11.1")
        releases.install(["a", "b"], self.index(), self.settings, self.log)
        self.assertEqual(self.version("a"), "2026.11.1")
        self.assertEqual(self.version("b"), "2026.11.1")
        self.assertFalse((self.settings.mods / "c").exists())
        self.assertEqual(toolkit.load_runtime(self.settings)["fastball"], FASTBALL)
        self.assertEqual(self.runtime_label(), "LIB 2026.11.1")
        self.assertEqual(releases.runtime_release(self.settings), "2026.11.1")
        self.assertEqual(self.records(), {"a": "2026.11.1", "b": "2026.11.1"})
        self.assertEqual(releases.installed(self.settings), self.records())
        self.assertEqual(self.leftovers(), [])

    def test_bad_sha_installs_nothing(self):
        rel = self.release("2026.11.1")
        entry = next(m for m in rel.index["mods"] if m["id"] == "b")
        entry["sha256"] = "0" * 64
        rel.save()
        before = self.snapshot()
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.install(["a", "b"], self.index(), self.settings, self.log)
        self.assertIn(entry["file"], str(caught.exception))
        self.assertIn("sha256", str(caught.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(Path(self.settings.runtime_dir).exists())
        self.assertEqual(self.leftovers(), [])

    def test_zip_slip_member_is_refused(self):
        link = zipfile.ZipInfo("data/link")
        link.external_attr = (0o120777 << 16)
        bad_members = {
            "dotdot": {"../evil.txt": b"x"},
            "nested dotdot": {"data/../../evil.txt": b"x"},
            "absolute": {"/evil.txt": b"x"},
            "drive letter": {"C:/evil.txt": b"x"},
            "drive relative": {"C:evil.txt": b"x"},
            "backslash": {"data\\..\\..\\evil.txt": b"x"},
            "symlink": {link: b"../../evil.txt"},
            "device name": {"data/NUL": b"x"},
            "trailing dot": {"data/evil.": b"x"},
        }
        self.release("2026.11.1", mod_ids=("a",))
        releases.install(["a"], self.index(), self.settings, self.log)
        for number, (label, extra) in enumerate(bad_members.items(), 1):
            with self.subTest(label):
                rel = self.release(f"2026.12.{number}", mod_ids=())
                members = dict(mod_members("a", rel.release))
                members.update(extra)
                rel.mod("a", members)
                before = self.snapshot()
                with self.assertRaises(releases.ReleaseError) as caught:
                    releases.install(["a"], self.index(), self.settings, self.log)
                self.assertIn(f"mod-a-{rel.release}.zip", str(caught.exception))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.version("a"), "2026.11.1")
                self.assertFalse(any(p.name == "evil.txt" for p in self.tmp.rglob("*")))
                self.assertEqual(self.leftovers(), [])

    def test_cut_off_download_keeps_old_folder(self):
        self.release("2026.11.1")
        releases.install(["a", "b"], self.index(), self.settings, self.log)
        rel = self.release("2026.11.2")
        entry = next(m for m in rel.index["mods"] if m["id"] == "a")
        path = rel.folder / entry["file"]
        path.write_bytes(path.read_bytes()[: entry["size"] // 2])
        before = self.snapshot()
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.install(["a"], self.index(), self.settings, self.log)
        self.assertIn(entry["file"], str(caught.exception))
        self.assertIn("cut off", str(caught.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.version("a"), "2026.11.1")
        self.assertEqual(self.runtime_label(), "LIB 2026.11.1")
        self.assertEqual(self.leftovers(), [])

    def test_runtime_not_redownloaded_when_current(self):
        rel = self.release("2026.11.1")
        releases.install(["a"], self.index(), self.settings, self.log)
        index = self.index()
        (rel.folder / rel.index["runtime"]["file"]).write_bytes(b"not the runtime")
        with mock.patch.object(releases, "_fetch", wraps=releases._fetch) as fetch:
            releases.install(["b"], index, self.settings, self.log)
        self.assertEqual([c.args[1] for c in fetch.call_args_list], ["mod-b-2026.11.1.zip"])
        self.assertEqual(self.runtime_label(), "LIB 2026.11.1")
        self.assertEqual(self.records(), {"a": "2026.11.1", "b": "2026.11.1"})

    def test_installed_mod_of_the_same_release_is_not_downloaded_again(self):
        self.release("2026.11.1")
        releases.install(["a"], self.index(), self.settings, self.log)
        with mock.patch.object(releases, "_fetch") as fetch:
            releases.install(["a"], self.index(), self.settings, self.log)
        fetch.assert_not_called()

    def test_newer_release_updates_runtime_and_all_installed_mods_together(self):
        self.release("2026.11.1")
        releases.install(["a", "b"], self.index(), self.settings, self.log)
        rel = self.release("2026.11.2")
        releases.install(["c"], self.index(), self.settings, self.log)
        for mod_id in ("a", "b", "c"):
            self.assertEqual(self.version(mod_id), "2026.11.2")
        self.assertEqual(self.runtime_label(), "LIB 2026.11.2")
        self.assertEqual(self.records(), {"a": "2026.11.2", "b": "2026.11.2", "c": "2026.11.2"})
        self.assertEqual(self.leftovers(), [])

        # All or nothing: a bad runtime in the next release changes none of the mods.
        rel = self.release("2026.11.3")
        rel.index["runtime"]["sha256"] = "f" * 64
        rel.save()
        before = self.snapshot()
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.install(["a"], self.index(), self.settings, self.log)
        self.assertIn(f"runtime-{FASTBALL}.zip", str(caught.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.leftovers(), [])

    def test_older_release_is_refused(self):
        self.release("2026.11.2")
        releases.install(["a"], self.index(), self.settings, self.log)
        self.release("2026.11.1")
        before = self.snapshot()
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.install(["b"], self.index(), self.settings, self.log)
        self.assertIn("2026.11.2", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_installed_mod_missing_from_newer_release_is_refused(self):
        self.release("2026.11.1")
        releases.install(["a", "b"], self.index(), self.settings, self.log)
        self.release("2026.11.2", mod_ids=("a",))
        before = self.snapshot()
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.install(["a"], self.index(), self.settings, self.log)
        self.assertIn("'b'", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_folder_the_manager_did_not_download_is_left_alone(self):
        own = self.settings.mods / "a"
        own.mkdir()
        (own / "mod.json").write_text('{"name": "Mine"}', encoding="utf-8")
        self.release("2026.11.1")
        before = self.snapshot()
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.install(["a"], self.index(), self.settings, self.log)
        self.assertIn(str(own), str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_failed_swap_puts_every_folder_back(self):
        self.release("2026.11.1")
        releases.install(["a", "b"], self.index(), self.settings, self.log)
        self.release("2026.11.2")
        index = self.index()
        before = self.snapshot()
        in_use = fail_folder_move(4, PermissionError(13, "in use"))
        with mock.patch("app.releases.os.replace", side_effect=in_use):
            with self.assertRaises(releases.ReleaseError) as caught:
                releases.install(["a"], index, self.settings, self.log)
        self.assertIn(str(self.settings.mods / "b"), str(caught.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.leftovers(), [])

    def test_interrupted_swap_is_rolled_back_by_the_next_call(self):
        self.release("2026.11.1")
        releases.install(["a", "b"], self.index(), self.settings, self.log)
        self.release("2026.11.2")
        index = self.index()
        before = self.snapshot()
        with mock.patch("app.releases.os.replace", side_effect=fail_folder_move(4, Crash())):
            with self.assertRaises(Crash):  # the process dies mid-swap: no rollback runs
                releases.install(["a"], index, self.settings, self.log)
        self.assertNotEqual(self.snapshot(), before)
        releases.recover(self.settings, self.log)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.leftovers(), [])

    def test_crash_after_any_move_is_rolled_back_by_recover(self):
        # Six folder moves: a out, a in, b out, b in, runtime out, runtime in. The process dies
        # before a move, or after it but before the journal records it.
        for nth in range(1, 7):
            for after in (False, True):
                with self.subTest(move=nth, after_the_move=after):
                    self.use_root(f"crash-{nth}-{after}")
                    self.release("2026.11.1")
                    releases.install(["a", "b"], self.index(), self.settings, self.log)
                    self.release("2026.11.2")
                    index = self.index()
                    before = self.snapshot()
                    crash = fail_folder_move(nth, Crash(), after_the_move=after)
                    with mock.patch("app.releases.os.replace", side_effect=crash):
                        with self.assertRaises(Crash):
                            releases.install(["a"], index, self.settings, self.log)
                    releases.recover(self.settings, self.log)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.leftovers(), [])
                    releases.install(["a"], index, self.settings, self.log)
                    self.assertEqual(self.records(), {"a": "2026.11.2", "b": "2026.11.2"})

    def test_crash_while_putting_back_is_finished_by_recover(self):
        self.release("2026.11.1")
        releases.install(["a", "b"], self.index(), self.settings, self.log)
        self.release("2026.11.2")
        index = self.index()
        before = self.snapshot()
        # b's new folder cannot go in (4th move); putting back dies after its first undo.
        real, seen = os.replace, {"moves": 0, "failed": None, "undone": 0}

        def replace(src, dst):
            if not Path(src).is_dir():
                return real(src, dst)
            if seen["failed"] == (src, dst):
                raise PermissionError(13, "in use")
            if seen["failed"]:
                seen["undone"] += 1
                if seen["undone"] == 2:
                    raise Crash()
                return real(src, dst)
            seen["moves"] += 1
            if seen["moves"] == 4:
                seen["failed"] = (src, dst)
                raise PermissionError(13, "in use")
            return real(src, dst)

        with mock.patch("app.releases.os.replace", side_effect=replace):
            with self.assertRaises(Crash):
                releases.install(["a"], index, self.settings, self.log)
        releases.recover(self.settings, self.log)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.leftovers(), [])

    def stuck_leftover(self, folder: Path):
        """A leftover folder holding a file kept open, so it cannot be deleted."""
        folder.mkdir(parents=True)
        (folder / "stuck.txt").write_text("left over")
        handle = open(folder / "stuck.txt", "rb")
        self.addCleanup(handle.close)
        return handle

    def test_undeletable_leftover_changes_nothing(self):
        for label in ("outgoing", "runtime.old"):
            with self.subTest(label):
                self.use_root(label)
                self.release("2026.11.1")
                releases.install(["a", "b"], self.index(), self.settings, self.log)
                self.release("2026.11.2")
                index = self.index()
                runtime = Path(self.settings.runtime_dir)
                leftover = (self.settings.mods / ".outgoing" / "a" if label == "outgoing"
                            else runtime.with_name(runtime.name + ".old"))
                handle = self.stuck_leftover(leftover)
                before = self.snapshot()
                with self.assertRaises(releases.ReleaseError) as caught:
                    releases.install(["a"], index, self.settings, self.log)
                self.assertIn(str(leftover), str(caught.exception))
                self.assertIn("Nothing was changed", str(caught.exception))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.version("a"), "2026.11.1")
                self.assertEqual(self.runtime_label(), "LIB 2026.11.1")
                self.assertEqual(self.records(), {"a": "2026.11.1", "b": "2026.11.1"})
                self.assertFalse((self.settings.state / "release-swap.json").exists())
                handle.close()
                releases.install(["a"], index, self.settings, self.log)
                self.assertEqual(self.version("a"), "2026.11.2")
                self.assertEqual(self.leftovers(), [])

    def test_read_only_leftover_is_cleared(self):
        self.release("2026.11.1")
        releases.install(["a"], self.index(), self.settings, self.log)
        for staging in (".outgoing", ".incoming"):  # moved onto, and unpacked into
            leftover = self.settings.mods / staging / "a"
            leftover.mkdir(parents=True)
            (leftover / "stuck.txt").write_text("left over")
            os.chmod(leftover / "stuck.txt", stat.S_IREAD)
        self.release("2026.11.2")
        releases.install(["a"], self.index(), self.settings, self.log)
        self.assertEqual(self.version("a"), "2026.11.2")
        self.assertEqual(self.leftovers(), [])

    # --- remove -------------------------------------------------------------------------
    def _files_mod_release(self) -> ReleaseFolder:
        rel = ReleaseFolder(self.tmp / "release" / "2026.11.1", "2026.11.1")
        rel.runtime()
        files = {"cwd": "{mod}", "install": ["python", "-c", "pass"],
                 "uninstall": ["python", "-c", "pass"],
                 "status": ["python", "-c", STATUS, "{mod}/marker"],
                 "installed_when": "MARKER: modded"}
        rel.mod("a", mod_members("a", "2026.11.1", files))
        rel.mod("b")
        self.settings.release_source = str(rel.folder)
        releases.install(["a", "b"], self.index(), self.settings, self.log)
        return rel

    def select(self, ids) -> None:
        config.write_json_atomic(self.settings.state / "selection.json", ids)

    def test_remove_refuses_a_mod_that_is_on(self):
        self._files_mod_release()
        folder = self.settings.mods / "a"
        # No selection file yet: every mod counts as on.
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.remove("a", self.settings, self.log)
        self.assertIn("turn it off", str(caught.exception))
        self.select(["a", "b"])
        with self.assertRaises(releases.ReleaseError):
            releases.remove("a", self.settings, self.log)
        # Off, but its files are still in the game.
        self.select(["b"])
        (folder / "marker").write_text("x")
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.remove("a", self.settings, self.log)
        self.assertIn("Play", str(caught.exception))
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.records(), {"a": "2026.11.1", "b": "2026.11.1"})
        # Off and not installed: removed with its record.
        (folder / "marker").unlink()
        releases.remove("a", self.settings, self.log)
        self.assertFalse(folder.exists())
        self.assertEqual(self.records(), {"b": "2026.11.1"})
        self.assertEqual(self.leftovers(), ["selection.json"])

    def test_remove_refuses_a_mod_whose_last_step_did_not_finish(self):
        self._files_mod_release()
        self.select(["b"])
        config.write_json_atomic(self.settings.state / "journal.json", [
            {"step": ["uninstall", "a"], "status": "failed", "output": "cannot write X.XML"}])
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.remove("a", self.settings, self.log)
        self.assertIn("Play", str(caught.exception))
        self.assertTrue((self.settings.mods / "a").is_dir())
        self.assertEqual(self.records(), {"a": "2026.11.1", "b": "2026.11.1"})
        config.write_json_atomic(self.settings.state / "journal.json", [
            {"step": ["uninstall", "a"], "status": "done", "output": ""}])
        releases.remove("a", self.settings, self.log)
        self.assertFalse((self.settings.mods / "a").exists())

    def test_remove_refuses_a_mod_it_did_not_download(self):
        own = self.settings.mods / "mine"
        own.mkdir()
        (own / "mod.json").write_text('{"name": "Mine"}', encoding="utf-8")
        self.select([])
        with self.assertRaises(releases.ReleaseError):
            releases.remove("mine", self.settings, self.log)
        self.assertTrue(own.is_dir())


class NewerReleaseTests(unittest.TestCase):
    """newer_release: the newest GitHub release's index.json, through an injected opener (never
    the network), named only when its tag is newer than this copy's."""
    INFO = {"repo": "owner/name", "tag": "2026.11.2", "fastball": FASTBALL}
    LATEST = "https://github.com/owner/name/releases/latest/download/index.json"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.settings = dataclasses.replace(config.Settings(), state=self.state, release_source="")
        self.requests = []

    @staticmethod
    def index(release: str, fastball: str = FASTBALL) -> dict:
        entry = {"file": "x.zip", "sha256": "0" * 64, "size": 1}
        return {"release": release, "fastball": fastball, "runtime": dict(entry),
                "mods": [dict(entry, id="a", name="A")]}

    def opener(self, body):
        """An urlopen stand-in that answers `body` (bytes, or an exception to raise)."""
        def urlopen(request, timeout=None):
            self.requests.append((request, timeout))
            if isinstance(body, BaseException):
                raise body
            return io.BytesIO(body)
        return urlopen

    def newer(self, body, info=INFO):
        data = body if isinstance(body, (bytes, BaseException)) else json.dumps(body).encode()
        return releases.newer_release(self.settings, info, opener=self.opener(data))

    def log_lines(self) -> list[str]:
        try:
            text = (self.state / "logs" / "manager.log").read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return text.splitlines()

    def test_a_newer_release_is_named_with_its_page(self):
        self.assertEqual(self.newer(self.index("2026.11.10")), {
            "release": "2026.11.10", "fastball": FASTBALL,
            "page": "https://github.com/owner/name/releases/tag/2026.11.10",
            "fastball_changed": False})
        (request, timeout), = self.requests
        self.assertEqual(request.full_url, self.LATEST)
        self.assertEqual(request.get_header("User-agent"), releases.USER_AGENT)
        self.assertLessEqual(timeout, 10)
        self.assertEqual(self.log_lines(), [])

    def test_a_newer_release_on_another_fastball_says_so(self):
        found = self.newer(self.index("2026.12.1", fastball="0.3.0+1234567"))
        self.assertEqual((found["fastball"], found["fastball_changed"]), ("0.3.0+1234567", True))

    def test_the_same_an_older_or_an_undated_release_is_none(self):
        for tag, info_tag in (("2026.11.2", "2026.11.2"), ("2026.11.1", "2026.11.2"),
                              ("2026.10.30", "2026.11.2"), ("preview-1", "2026.11.2"),
                              ("2026.12.1", "preview-1")):
            with self.subTest(latest=tag, this=info_tag):
                self.assertIsNone(self.newer(self.index(tag), dict(self.INFO, tag=info_tag)))
        self.assertEqual(self.log_lines(), [])

    def test_a_bad_index_is_none_and_one_log_line(self):
        bad = {"not json": b"<html>rate limited</html>", "too large": b" " * (releases.INDEX_MAX + 2),
               "no runtime": dict(self.index("2026.12.1"), runtime=None),
               "not an object": [1, 2],
               # int() refuses more than 4300 digits: ValueError, which must not escape
               "huge tag part": self.index("2026." + "9" * 5000),
               "tag over 64 characters": self.index("2026.11." + "1" * 60)}
        for label, body in bad.items():
            with self.subTest(label):
                before = len(self.log_lines())
                self.assertIsNone(self.newer(body))
                lines = self.log_lines()[before:]
                self.assertEqual(len(lines), 1, lines)
                self.assertIn("newer release", lines[0])
                self.assertNotIn("Release source", lines[0])   # a hint for downloads, not this check

    def test_a_huge_tag_in_release_json_never_raises(self):
        info = dict(self.INFO, tag="2026." + "9" * 5000)
        self.assertIsNone(self.newer(self.index("2026.12.1"), info))
        self.assertEqual(len(self.log_lines()), 1)

    def test_a_network_error_is_none_and_one_log_line(self):
        import http.client
        import urllib.error
        for err in (urllib.error.URLError("no route"), TimeoutError("timed out"),
                    urllib.error.HTTPError(self.LATEST, 404, "Not Found", {}, None),
                    http.client.RemoteDisconnected("closed"), ValueError("odd")):
            with self.subTest(type(err).__name__):
                before = len(self.log_lines())
                self.assertIsNone(self.newer(err))
                self.assertEqual(len(self.log_lines()) - before, 1)

    def test_a_failed_check_keeps_the_last_good_answer(self):
        last = {"release": "2026.11.3", "fastball": FASTBALL, "fastball_changed": False,
                "page": "https://github.com/owner/name/releases/tag/2026.11.3"}
        for label, body in (("network", OSError("unreachable")), ("bad index", b"<html>")):
            with self.subTest(label):
                found = releases.newer_release(self.settings, self.INFO, opener=self.opener(body),
                                               last=last)
                self.assertIs(found, last)

    def test_a_good_check_replaces_the_last_answer(self):
        last = {"release": "2026.11.3", "fastball": FASTBALL, "fastball_changed": False,
                "page": "https://github.com/owner/name/releases/tag/2026.11.3"}
        data = json.dumps(self.index("2026.11.2")).encode()   # this copy's own tag: nothing newer
        self.assertIsNone(releases.newer_release(self.settings, self.INFO,
                                                 opener=self.opener(data), last=last))
        data = json.dumps(self.index("2026.11.4")).encode()
        self.assertEqual(releases.newer_release(self.settings, self.INFO, opener=self.opener(data),
                                                last=last)["release"], "2026.11.4")

    def test_no_release_json_never_asks(self):
        self.assertIsNone(self.newer(self.index("2026.12.1"), info=None))
        self.assertEqual(self.requests, [])
        self.assertEqual(self.log_lines(), [])

    def test_a_release_source_of_its_own_never_asks(self):
        for src in ("C:\\releases\\test", "https://example.com/rel/"):
            with self.subTest(src):
                self.settings.release_source = src
                self.assertIsNone(self.newer(self.index("2026.12.1")))
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
