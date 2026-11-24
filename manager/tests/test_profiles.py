import dataclasses
import tempfile
import unittest
from pathlib import Path

from app import config, mods, profiles


def _snapshot(root: Path) -> dict:
    """Relative path -> bytes (None for folders) of everything under root."""
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.game = Path(self._tmp.name) / "MLB The Show 26"
        self.normal = self.game / "profile"
        (self.normal / "saves").mkdir(parents=True)
        (self.normal / "saves" / "slot1.dat").write_bytes(b"\x00save\x01")
        (self.normal / "settings.ini").write_text("volume=7", encoding="utf-8")
        (self.normal / "profile.lock").write_text("locked", encoding="utf-8")
        self.settings = dataclasses.replace(config.Settings(), game=self.game,
                                            mods=Path(self._tmp.name) / "mods")
        self.lines = []

    def tearDown(self):
        self._tmp.cleanup()

    def _mod(self, mod_id: str, profile: str | None = None) -> mods.Mod:
        return mods.Mod(id=mod_id, name=mod_id, description="",
                        folder=self.settings.mods / mod_id, art="", files=None,
                        runtime={}, server=None, profile=profile, order=100)

    def _all(self):
        return [self._mod("pitch-types"), self._mod("rtts-bundle", "own"),
                self._mod("other-own", "own")]

    def test_bundle_uses_own_profile(self):
        path = profiles.for_selection({"pitch-types", "rtts-bundle"}, self._all(), self.settings)
        self.assertEqual(path, self.game / "profile-rtts-bundle")

    def test_first_own_mod_in_list_order_wins(self):
        path = profiles.for_selection({"other-own", "rtts-bundle"}, self._all(), self.settings)
        self.assertEqual(path, self.game / "profile-rtts-bundle")

    def test_no_own_mod_uses_normal_profile(self):
        self.assertEqual(profiles.for_selection({"pitch-types"}, self._all(), self.settings),
                         self.normal)
        self.assertEqual(profiles.for_selection(set(), self._all(), self.settings), self.normal)

    def test_override_wins(self):
        override = Path(self._tmp.name) / "my profile"
        settings = dataclasses.replace(self.settings, profile_override=override)
        self.assertEqual(profiles.for_selection({"rtts-bundle"}, self._all(), settings), override)
        self.assertEqual(profiles.for_selection(set(), self._all(), settings), override)

    def test_ensure_copies_once_without_lock(self):
        before = _snapshot(self.normal)
        target = self.game / "profile-rtts-bundle"
        profiles.ensure(target, self.settings, self.lines.append)
        self.assertEqual((target / "saves" / "slot1.dat").read_bytes(), b"\x00save\x01")
        self.assertEqual((target / "settings.ini").read_text(encoding="utf-8"), "volume=7")
        self.assertFalse((target / "profile.lock").exists())
        self.assertFalse(Path(str(target) + ".partial").exists())
        self.assertEqual(len(self.lines), 2)
        self.assertTrue(all(str(target) in line for line in self.lines))

        (target / "settings.ini").write_text("volume=1", encoding="utf-8")
        profiles.ensure(target, self.settings, self.lines.append)
        self.assertEqual((target / "settings.ini").read_text(encoding="utf-8"), "volume=1")
        self.assertEqual(len(self.lines), 2)
        self.assertEqual(_snapshot(self.normal), before)

    def test_ensure_replaces_stale_partial(self):
        target = self.game / "profile-rtts-bundle"
        partial = Path(str(target) + ".partial")
        partial.mkdir()
        (partial / "junk.bin").write_bytes(b"half")
        profiles.ensure(target, self.settings, self.lines.append)
        self.assertFalse(partial.exists())
        self.assertFalse((target / "junk.bin").exists())
        self.assertTrue((target / "settings.ini").is_file())

    def test_ensure_leaves_normal_profile_alone(self):
        before = _snapshot(self.normal)
        same = Path(str(self.game).upper()) / "PROFILE" / "."
        profiles.ensure(same, self.settings, self.lines.append)
        profiles.ensure(self.normal, self.settings, self.lines.append)
        self.assertEqual(_snapshot(self.normal), before)
        self.assertEqual(self.lines, [])

    def test_ensure_normal_profile_missing_is_not_created(self):
        game = Path(self._tmp.name) / "Empty Game"
        settings = dataclasses.replace(self.settings, game=game)
        profiles.ensure(game / "profile", settings, self.lines.append)
        self.assertFalse((game / "profile").exists())

    def test_ensure_missing_override_raises(self):
        override = Path(self._tmp.name) / "my profile"
        settings = dataclasses.replace(self.settings, profile_override=override)
        with self.assertRaises(FileNotFoundError) as ctx:
            profiles.ensure(override, settings, self.lines.append)
        self.assertIn(str(override), str(ctx.exception))
        self.assertFalse(override.exists())

    def test_ensure_existing_override_untouched(self):
        override = Path(self._tmp.name) / "my profile"
        override.mkdir()
        settings = dataclasses.replace(self.settings, profile_override=override)
        profiles.ensure(override, settings, self.lines.append)
        self.assertEqual(list(override.iterdir()), [])
        self.assertEqual(self.lines, [])

    def test_ensure_missing_normal_profile_raises(self):
        game = Path(self._tmp.name) / "Empty Game"
        game.mkdir()
        settings = dataclasses.replace(self.settings, game=game)
        target = game / "profile-rtts-bundle"
        with self.assertRaises(FileNotFoundError) as ctx:
            profiles.ensure(target, settings, self.lines.append)
        self.assertIn(str(game / "profile"), str(ctx.exception))
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
