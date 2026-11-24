import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock

from app import config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = config.load(Path(tmp) / "missing" / "settings.json")
        manager = Path(config.__file__).resolve().parent.parent
        self.assertEqual(s.game, Path(r"C:\Games\MLB The Show 26"))
        self.assertEqual(s.mods, manager / "mods")
        self.assertIsNone(s.theshow)
        self.assertIsNone(s.recomp)
        self.assertIsNone(s.setup)
        self.assertIsNone(s.build_wt)
        self.assertEqual(s.build_out, manager / "builds" / "current")
        self.assertEqual(s.state, manager / "state")
        self.assertEqual(s.runtime_dir, manager / "runtime")
        self.assertEqual(s.release_source, "")
        self.assertEqual(s.port, 8645)
        self.assertEqual(s.packages_port, 10010)
        self.assertIsNone(s.profile_override)
        self.assertFalse(s.always_combined_exe)
        self.assertEqual(config.mode(s), "player")

    def test_defaults_have_no_user_paths(self):
        # Folders the manager owns sit beside it, wherever it was unpacked; no other
        # default may name a Windows user's folder.
        for f in fields(config.Settings):
            value = getattr(config.Settings(), f.name)
            if isinstance(value, Path) and value.is_relative_to(config.MANAGER):
                value = value.relative_to(config.MANAGER)
            with self.subTest(key=f.name):
                self.assertNotIn("\\users\\", str(value).lower())
        source = Path(config.__file__).read_text(encoding="utf-8").lower()
        self.assertFalse("\\users\\" in source, "app/config.py names a user's folder")

    def test_mode_dev_needs_a_git_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone, plain, worktree = (Path(tmp) / name for name in ("clone", "plain", "worktree"))
            (clone / ".git").mkdir(parents=True)
            plain.mkdir()
            worktree.mkdir()
            (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
            for recomp, want in ((clone, "dev"), (worktree, "dev"), (plain, "player"),
                                 (Path(tmp) / "missing", "player"), (None, "player")):
                with self.subTest(recomp=recomp):
                    self.assertEqual(config.mode(config.Settings(recomp=recomp)), want)

    def test_existing_settings_file_keeps_dev(self):
        # A developer's settings.json names every field it had before the release keys
        # existed; it must load as dev mode with exactly those values.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "TheShow"
            (root / "mlbts-native-src" / ".git").mkdir(parents=True)
            written = {
                "game": r"C:\Games\MLB The Show 26",
                "theshow": str(root),
                "mods": str(root / "mods"),
                "recomp": str(root / "mlbts-native-src"),
                "setup": str(root / "MLBTS-Setup-0.2.0"),
                "build_wt": str(root / "manager-build"),
                "build_out": str(root / "manager" / "builds" / "current"),
                "state": str(root / "manager" / "state"),
                "port": 8645,
                "packages_port": 10010,
                "profile_override": r"C:\Games\MLB The Show 26\profile-rtts-bundle",
                "always_combined_exe": False,
            }
            path = root / "manager" / "state" / "settings.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(written, indent=2), encoding="utf-8")
            s = config.load(path)
            self.assertEqual(config.mode(s), "dev")
            self.assertFalse((path.parent / "logs" / "manager.log").exists())  # no fallbacks
        for key, value in written.items():
            with self.subTest(key=key):
                got = getattr(s, key)
                self.assertEqual(str(got) if isinstance(got, Path) else got, value)
        self.assertEqual(s.runtime_dir, config.MANAGER / "runtime")
        self.assertEqual(s.release_source, "")

    def test_new_keys_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            s = config.Settings(runtime_dir=Path(tmp) / "rt", release_source=r"D:\release",
                                setup=Path(r"D:\Setup"))
            config.save(s, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            loaded = config.load(path)
        self.assertEqual(loaded, s)
        self.assertIsNone(saved["recomp"])
        self.assertEqual(saved["release_source"], r"D:\release")

    def test_optional_paths_accept_none(self):
        for key in ("theshow", "recomp", "setup", "build_wt", "profile_override"):
            with self.subTest(key=key):
                self.assertIsNone(config.check_value(key, None))
                self.assertEqual(config.check_value(key, r"D:\x"), Path(r"D:\x"))
                with self.assertRaises(ValueError):
                    config.check_value(key, "")
        for key in ("game", "mods", "build_out", "state", "runtime_dir"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                config.check_value(key, None)

    def test_release_source_is_text(self):
        self.assertEqual(config.check_value("release_source", ""), "")
        self.assertEqual(config.check_value("release_source", "https://example.com/r"),
                         "https://example.com/r")
        for bad in (None, 5, ["x"]):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                config.check_value("release_source", bad)

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "settings.json"
            s = config.load(path)
            s.profile_override = Path(r"C:\Games\MLB The Show 26\profile-custom")
            s.port = 9000
            s.always_combined_exe = True
            config.save(s, path)
            loaded = config.load(path)
        self.assertEqual(loaded.profile_override, Path(r"C:\Games\MLB The Show 26\profile-custom"))
        self.assertEqual(loaded.port, 9000)
        self.assertTrue(loaded.always_combined_exe)
        self.assertEqual(loaded.game, Path(r"C:\Games\MLB The Show 26"))

    def test_partial_file_overrides_only_present_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"game": r"D:\Other Game"}), encoding="utf-8")
            s = config.load(path)
        self.assertEqual(s.game, Path(r"D:\Other Game"))
        self.assertEqual(s.port, 8645)
        self.assertEqual(s.mods, config.MANAGER / "mods")


    def _load_text(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "state" / "settings.json"
        path.parent.mkdir()
        path.write_text(text, encoding="utf-8")
        settings = config.load(path)
        log = path.parent / "logs" / "manager.log"
        return settings, (log.read_text(encoding="utf-8") if log.exists() else "")

    def test_invalid_json_gives_defaults_and_a_log_line(self):
        for text in ("{not json", "[1, 2]", "null"):
            with self.subTest(text=text):
                s, log = self._load_text(text)
                self.assertEqual(s, config.Settings())
                self.assertIn("settings.json", log)
                self.assertIn("defaults", log)

    def test_bad_values_fall_back_per_key(self):
        s, log = self._load_text(json.dumps({
            "port": "8645", "packages_port": 70000, "game": 5, "always_combined_exe": "yes",
            "profile_override": 3, "build_wt": "", "mods": "D:/Mods", "nope": 1}))
        d = config.Settings()
        self.assertEqual((s.port, s.packages_port, s.game, s.always_combined_exe,
                          s.profile_override, s.build_wt),
                         (d.port, d.packages_port, d.game, d.always_combined_exe,
                          d.profile_override, d.build_wt))
        self.assertEqual(s.mods, Path("D:/Mods"))
        for key in ("port", "packages_port", "game", "always_combined_exe",
                    "profile_override", "build_wt"):
            self.assertIn(key, log)
        self.assertNotIn("mods", log)

    def test_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            with mock.patch("app.config.write_json_atomic",
                            wraps=config.write_json_atomic) as write:
                config.save(config.Settings(), path)
            write.assert_called_once()
            self.assertEqual(config.load(path), config.Settings())

    def test_atomic_write_retries_a_locked_target(self):
        real_replace = config.os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) <= 2:
                raise PermissionError(5, "Access is denied")
            real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            with mock.patch("app.config.os.replace", side_effect=flaky), \
                    mock.patch("app.config.time.sleep") as sleep:
                config.write_json_atomic(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})
            self.assertEqual(len(calls), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_atomic_write_gives_up_on_a_target_that_stays_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            path.write_text("old", encoding="utf-8")
            with mock.patch("app.config.os.replace",
                            side_effect=PermissionError(5, "Access is denied")) as replace, \
                    mock.patch("app.config.time.sleep"):
                with self.assertRaises(PermissionError):
                    config.write_json_atomic(path, {"a": 1})
            self.assertGreater(replace.call_count, 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
