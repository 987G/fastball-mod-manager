import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import config, mods

# The dev setup's mods folder, from the manager's own settings (the tests that copy real manifests skip without it).
REAL_MODS = config.load(config.MANAGER / "state" / "settings.json").mods


def _settings(tmp: Path, **overrides) -> config.Settings:
    base = config.Settings(mods=tmp / "mods", state=tmp / "state")
    return dataclasses.replace(base, **overrides)


def _write(folder: Path, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mod.json").write_text(text, encoding="utf-8")


class LoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.settings = _settings(self.tmp)
        self.settings.mods.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _copy_real(self) -> None:
        """Copy the two real manifests this repo relies on, with the files they name (other
        mods, names and art belong to the mods' own sessions and may change)."""
        sources = [REAL_MODS / "rtts-quirks" / "mod.json", REAL_MODS / "pitch-types" / "mod.json"]
        if not all(p.exists() for p in sources):
            self.skipTest("real mod manifests not present")
        for src in sources:
            dest = self.settings.mods / src.parent.name
            dest.mkdir()
            shutil.copy(src, dest / "mod.json")
            data = json.loads(src.read_text(encoding="utf-8"))
            named = [data.get(k) for k in ("patches", "card", "features")]
            if str(data.get("art", "")).lower().endswith(mods.ART_FILE_SUFFIXES):
                named.append(data["art"])
            for rel in filter(None, named):  # the loader checks these files are there
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src.parent / rel, dest / rel)

    def test_no_mods_folder_yet_is_no_mods(self):
        self.settings.mods = self.settings.mods / "not-yet"
        self.assertEqual(mods.load_all(self.settings), [])

    def test_clean_card_text(self):
        self.assertEqual(mods.clean_card_text({"tagline": "t", "highlights": [["a", "b"]]}),
                         {"tagline": "t", "highlights": [["a", "b"]], "note": ""})
        for bad in ({}, {"tagline": 5, "highlights": []}, {"tagline": "t", "highlights": [["a"]]},
                    {"tagline": "t", "highlights": [], "x": 1}, []):
            with self.assertRaises(ValueError):
                mods.clean_card_text(bad)

    def test_load_all_reads_both_mods(self):
        """What this repo relies on, not the manifests' current values (the mods' own
        sessions change their switches)."""
        self._copy_real()
        loaded = mods.load_all(self.settings)
        ids = [m.id for m in loaded]
        self.assertEqual(loaded, sorted(loaded, key=lambda m: (m.order, m.name)))
        self.assertLess(ids.index("pitch-types"), ids.index("rtts-quirks"))
        by_id = {m.id: m for m in loaded}
        quirks, pitch = by_id["rtts-quirks"], by_id["pitch-types"]
        self.assertEqual(quirks.folder, self.settings.mods / "rtts-quirks")
        self.assertIsInstance(quirks.files["installed_when"], str)
        self.assertEqual(quirks.runtime["branch"], "mod/rtts-quirks")
        self.assertIsInstance(quirks.runtime["env_on"], dict)
        self.assertEqual(pitch.runtime["branch"], "mod/pitch-types")
        self.assertIsInstance(pitch.runtime["env_on"], dict)
        self.assertIsInstance(pitch.runtime["env_off"], dict)

    def test_real_env_matches_constraints(self):
        """Every placeholder in the real env_on / env_off expands."""
        self._copy_real()
        by_id = {m.id: m for m in mods.load_all(self.settings)}
        for mod in (by_id["rtts-quirks"], by_id["pitch-types"]):
            for key in ("env_on", "env_off"):
                env = mods.expand(mod.runtime.get(key) or {}, mod, self.settings)
                for name, value in env.items():
                    self.assertNotRegex(str(value), r"\{(mod|mods|game|state|port)\}",
                                        f"{mod.id} {key} {name}")

    def test_sorted_by_order_then_name(self):
        _write(self.settings.mods / "zed", '{"name": "B", "order": 5}')
        _write(self.settings.mods / "alpha", '{"name": "A"}')
        _write(self.settings.mods / "beta", '{"name": "A0", "order": 5}')
        loaded = mods.load_all(self.settings)
        self.assertEqual([m.id for m in loaded], ["beta", "zed", "alpha"])
        alpha = loaded[2]
        self.assertEqual(alpha.order, 100)
        self.assertEqual(alpha.description, "")
        self.assertEqual(alpha.art, "")
        self.assertIsNone(alpha.files)
        self.assertIsNone(alpha.runtime)

    def test_ignores_folders_without_manifest(self):
        _write(self.settings.mods / "real", '{"name": "Real"}')
        (self.settings.mods / "research").mkdir()
        (self.settings.mods / "research" / "notes.md").write_text("x", encoding="utf-8")
        (self.settings.mods / "stray.json").write_text("{}", encoding="utf-8")
        self.assertEqual([m.id for m in mods.load_all(self.settings)], ["real"])

    def test_version_is_kept_as_short_text(self):
        _write(self.settings.mods / "six", '{"name": "Six", "version": " 6 "}')
        _write(self.settings.mods / "dotted", '{"name": "Dotted", "version": "1.2"}')
        _write(self.settings.mods / "longest", json.dumps({"name": "Longest", "version": "x" * 16}))
        _write(self.settings.mods / "none", '{"name": "None"}')
        by_id = {m.id: m for m in mods.load_all(self.settings)}
        self.assertEqual({k: m.version for k, m in by_id.items()},
                         {"six": "6", "dotted": "1.2", "longest": "x" * 16, "none": None})

    def test_bad_version_reports_mod(self):
        for label, value in (("a number", 6), ("a list", ["6"]), ("too long", "x" * 17),
                             ("empty", ""), ("blank", "   "), ("null", None)):
            with self.subTest(label):
                shutil.rmtree(self.settings.mods)
                _write(self.settings.mods / "odd-version", json.dumps({"name": "Odd", "version": value}))
                with self.assertRaises(mods.ManifestError) as cm:
                    mods.load_all(self.settings)
                self.assertIn("odd-version", str(cm.exception))
                self.assertIn("version", str(cm.exception))

    def test_clean_version(self):
        self.assertEqual(mods.clean_version("6"), "6")
        self.assertEqual(mods.clean_version(" 1.2 "), "1.2")
        for bad in (6, None, "", " ", "x" * 17, ["6"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                mods.clean_version(bad)

    def test_bad_manifest_reports_mod(self):
        _write(self.settings.mods / "broken-mod", '{"name": "Broken",')
        with self.assertRaises(mods.ManifestError) as cm:
            mods.load_all(self.settings)
        self.assertIn("broken-mod", str(cm.exception))

    def test_patches_path_resolves_against_the_mod_folder(self):
        folder = self.settings.mods / "speedy"
        _write(folder, '{"name": "Speedy", "patches": "p/patches.txt",'
                       ' "runtime": {"branch": "b", "env_on": {}, "env_off": {}}}')
        (folder / "p").mkdir()
        (folder / "p" / "patches.txt").write_text("set a MLB_A 1\n", encoding="utf-8")
        [mod] = mods.load_all(self.settings)
        self.assertEqual(mod.patches, folder / "p" / "patches.txt")
        self.assertTrue(mod.patches.is_absolute())

    def test_switch_only_runtime_loads(self):
        _write(self.settings.mods / "switchy",
               '{"name": "Switchy", "runtime": {"env_on": {"MLB_OFFLINE_PACKAGES": "1"},'
               ' "env_off": {"MLB_OFFLINE_PACKAGES": "0"}}}')
        [mod] = mods.load_all(self.settings)
        self.assertNotIn("branch", mod.runtime)
        self.assertEqual(mod.runtime["env_on"], {"MLB_OFFLINE_PACKAGES": "1"})

    def test_no_patches_key_is_none(self):
        _write(self.settings.mods / "plain", '{"name": "Plain"}')
        self.assertIsNone(mods.load_all(self.settings)[0].patches)

    def test_missing_patch_file_reports_mod(self):
        _write(self.settings.mods / "lost-patch", '{"name": "Lost", "patches": "nope.txt"}')
        with self.assertRaises(mods.ManifestError) as cm:
            mods.load_all(self.settings)
        self.assertIn("lost-patch", str(cm.exception))
        self.assertIn("nope.txt", str(cm.exception))

    def test_art_file_resolves_against_the_mod_folder(self):
        folder = self.settings.mods / "pictured"
        _write(folder, '{"name": "Pictured", "art": "art.png"}')
        (folder / "art.png").write_bytes(b"\x89PNG")
        [mod] = mods.load_all(self.settings)
        self.assertEqual(mod.art_file, (folder / "art.png").resolve())

    def test_game_art_name_has_no_art_file(self):
        _write(self.settings.mods / "named", '{"name": "Named", "art": "ANN_SQUARE_GENERIC"}')
        [mod] = mods.load_all(self.settings)
        self.assertIsNone(mod.art_file)
        self.assertEqual(mod.art, "ANN_SQUARE_GENERIC")

    def test_missing_or_outside_art_file_reports_mod(self):
        for art in ("nope.png", "../elsewhere.png"):
            folder = self.settings.mods / "bad-art"
            _write(folder, json.dumps({"name": "Bad art", "art": art}))
            (self.settings.mods / "elsewhere.png").write_bytes(b"\x89PNG")
            with self.assertRaises(mods.ManifestError) as cm:
                mods.load_all(self.settings)
            self.assertIn("bad-art", str(cm.exception))

    def test_card_and_features_resolve_in_the_mod_folder(self):
        folder = self.settings.mods / "carded"
        _write(folder, '{"name": "Carded", "card": "release/card.png", "features": "release/features.md"}')
        (folder / "release").mkdir()
        (folder / "release" / "card.png").write_bytes(b"\x89PNG")
        (folder / "release" / "features.md").write_text("﻿**Hi**\n- one\n", encoding="utf-8")
        [mod] = mods.load_all(self.settings)
        self.assertEqual(mod.card, (folder / "release" / "card.png").resolve())
        self.assertEqual(mod.features, [{"title": "Hi", "blocks": [{"bullet": "one", "sub": []}]}])

    def test_no_card_or_features_is_none(self):
        _write(self.settings.mods / "plain", '{"name": "Plain"}')
        [mod] = mods.load_all(self.settings)
        self.assertIsNone(mod.card)
        self.assertIsNone(mod.features)

    def test_bad_card_or_features_reports_mod(self):
        (self.settings.mods / "outside.png").write_bytes(b"\x89PNG")
        (self.settings.mods / "outside.md").write_text("- x\n", encoding="utf-8")
        cases = [{"card": "nope.png"}, {"card": "../outside.png"}, {"card": "card.gif"}, {"card": 3},
                 {"features": "nope.md"}, {"features": "../outside.md"}, {"features": "f.html"}]
        for extra in cases:
            with self.subTest(extra=extra):
                _write(self.settings.mods / "bad-card", json.dumps({"name": "Bad", **extra}))
                with self.assertRaises(mods.ManifestError) as cm:
                    mods.load_all(self.settings)
                self.assertIn("bad-card", str(cm.exception))

    def test_ship_globs_resolve_inside_the_folder(self):
        folder = self.settings.mods / "shipper"
        _write(folder, json.dumps({"name": "Shipper", "ship": [
            "release/*.md", "tools/**/*.py", "release/features.md", "release", "nothing/*.x"]}))
        for rel in ("release/features.md", "release/notes.md", "release/card.png", "tools/run.py",
                    "tools/sub/helper.py", "tools/sub/data.txt", "research/plan.md", "work/run.py"):
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text("x", encoding="utf-8")
        [mod] = mods.load_all(self.settings)
        self.assertEqual(mod.ship, ["release/*.md", "tools/**/*.py", "release/features.md", "release",
                                    "nothing/*.x"])
        root = folder.resolve()
        expected = [root / r for r in ("mod.json", "release/features.md", "release/notes.md",
                                       "tools/run.py", "tools/sub/helper.py")]
        self.assertEqual(mods.shipped_files(mod), sorted(expected))

    def test_ship_empty_list_ships_only_the_manifest(self):
        folder = self.settings.mods / "bare"
        _write(folder, '{"name": "Bare", "ship": []}')
        (folder / "notes.md").write_text("x", encoding="utf-8")
        [mod] = mods.load_all(self.settings)
        self.assertEqual(mods.shipped_files(mod), [(folder / "mod.json").resolve()])

    def test_ship_glob_escaping_the_folder_is_an_error(self):
        (self.settings.mods / "secret.md").write_text("x", encoding="utf-8")
        cases = ["../*.md", "release/../../*.md", "/secret.md", "C:/secret.md", "C:secret.md",
                 "release\\*.md", "", 3, None]
        for glob in cases:
            with self.subTest(glob=glob):
                _write(self.settings.mods / "leaky", json.dumps({"name": "Leaky", "ship": [glob]}))
                with self.assertRaises(mods.ManifestError) as cm:
                    mods.load_all(self.settings)
                self.assertIn("leaky", str(cm.exception))
        for glob in [".", "./", "./*.md", "release/./*.md", "release//*.md", "release/", "tools/*/",
                     " release/*.md", "release/*.md ", "\trelease/*.md"]:
            with self.subTest(glob=glob):  # empty or "." parts, a trailing "/", spaces at either end
                _write(self.settings.mods / "leaky", json.dumps({"name": "Leaky", "ship": [glob]}))
                with self.assertRaises(mods.ManifestError) as cm:
                    mods.load_all(self.settings)
                self.assertIn("leaky", str(cm.exception))
        with self.subTest(ship="not a list"):
            _write(self.settings.mods / "leaky", json.dumps({"name": "Leaky", "ship": "release/*"}))
            with self.assertRaises(mods.ManifestError) as cm:
                mods.load_all(self.settings)
            self.assertIn("leaky", str(cm.exception))

    def test_ship_link_out_of_the_folder_is_an_error(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("x", encoding="utf-8")
        folder = self.settings.mods / "linked"
        _write(folder, '{"name": "Linked", "ship": ["tools/*.py"]}')
        try:
            import _winapi
            _winapi.CreateJunction(str(outside), str(folder / "tools"))
        except (ImportError, OSError):
            try:
                (folder / "tools").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("cannot make a junction or symlink here")
        for ship in (["tools/*.py"], ["**"], ["**/*.py"]):
            with self.subTest(ship=ship):  # "**" walks into the junction too
                _write(folder, json.dumps({"name": "Linked", "ship": ship}))
                [mod] = mods.load_all(self.settings)
                with self.assertRaises(mods.ManifestError) as cm:
                    mods.shipped_files(mod)
                self.assertIn("linked", str(cm.exception))
                self.assertIn("secret.py", str(cm.exception))

    def test_no_ship_means_none(self):
        _write(self.settings.mods / "plain", '{"name": "Plain"}')
        [mod] = mods.load_all(self.settings)
        self.assertIsNone(mod.ship)
        with self.assertRaises(mods.ManifestError) as cm:
            mods.shipped_files(mod)  # nothing may be published from a mod without a ship list
        self.assertIn("plain", str(cm.exception))

    def test_card_json_loads_as_text(self):
        folder = self.settings.mods / "texty"
        _write(folder, '{"name": "Texty", "card": "release/card.json"}')
        (folder / "release").mkdir()
        card = {"tagline": "Every pitcher pitches like himself",
                "highlights": [["Real pitches", "his mix by batter side and count"],
                               ["Real delivery", "release point and rubber spot"],
                               ["Own slide step", "every 2026 pitcher has one"]],
                "note": "Pitcher deliveries are editable in the player editor."}
        (folder / "release" / "card.json").write_text("\ufeff" + json.dumps(card), encoding="utf-8")
        [mod] = mods.load_all(self.settings)
        self.assertIsNone(mod.card)
        self.assertEqual(mod.card_text, card)

    def test_card_json_without_note_has_empty_note(self):
        folder = self.settings.mods / "short"
        _write(folder, '{"name": "Short", "card": "card.json"}')
        (folder / "card.json").write_text('{"tagline": "Hi", "highlights": [["One", "thing"]]}',
                                          encoding="utf-8")
        [mod] = mods.load_all(self.settings)
        self.assertEqual(mod.card_text, {"tagline": "Hi", "highlights": [["One", "thing"]], "note": ""})

    def test_image_card_has_no_card_text(self):
        folder = self.settings.mods / "pictured"
        _write(folder, '{"name": "Pictured", "card": "card.png"}')
        (folder / "card.png").write_bytes(b"\x89PNG")
        [mod] = mods.load_all(self.settings)
        self.assertEqual(mod.card, (folder / "card.png").resolve())
        self.assertIsNone(mod.card_text)

    def test_bad_card_json_reports_mod(self):
        good = {"tagline": "T", "highlights": [["a", "b"]], "note": "n"}
        bodies = ["{not json", "[]", json.dumps({**good, "tagline": 3}), json.dumps({"highlights": [["a", "b"]]}),
                  json.dumps({**good, "highlights": "a, b"}), json.dumps({**good, "highlights": [["a"]]}),
                  json.dumps({**good, "highlights": [["a", 2]]}), json.dumps({**good, "highlights": ["ab"]}),
                  json.dumps({**good, "highlights": [["a", "b"]] * 4}), json.dumps({**good, "note": 1}),
                  json.dumps({**good, "notes": "typo"})]
        folder = self.settings.mods / "bad-text"
        for body in bodies:
            with self.subTest(body=body):
                _write(folder, '{"name": "Bad", "card": "card.json"}')
                (folder / "card.json").write_text(body, encoding="utf-8")
                with self.assertRaises(mods.ManifestError) as cm:
                    mods.load_all(self.settings)
                self.assertIn("bad-text", str(cm.exception))
        with self.subTest(case="bytes that are not UTF-8"):
            (folder / "card.json").write_bytes(b"\xff\xfe\x00bad")
            with self.assertRaises(mods.ManifestError) as cm:
                mods.load_all(self.settings)
            self.assertIn("bad-text", str(cm.exception))
        with self.subTest(case="missing"):
            (folder / "card.json").unlink()
            with self.assertRaises(mods.ManifestError) as cm:
                mods.load_all(self.settings)
            self.assertIn("bad-text", str(cm.exception))

    def test_oversize_features_reports_mod(self):
        folder = self.settings.mods / "wordy"
        _write(folder, '{"name": "Wordy", "features": "f.md"}')
        (folder / "f.md").write_text("- x\n" * 20000, encoding="utf-8")
        with self.assertRaises(mods.ManifestError) as cm:
            mods.load_all(self.settings)
        self.assertIn("wordy", str(cm.exception))

    def test_patch_only_mod_loads(self):
        folder = self.settings.mods / "patch-only"
        _write(folder, '{"name": "Patch only", "patches": "patches.txt"}')
        (folder / "patches.txt").write_text("set a MLB_A 0\n", encoding="utf-8")
        [mod] = mods.load_all(self.settings)
        self.assertIsNone(mod.runtime)
        self.assertEqual(mod.patches, folder / "patches.txt")

    def test_non_object_or_missing_name_reports_mod(self):
        for text in ('["x"]', '{"order": 1}', '{"name": ""}'):
            with self.subTest(text=text):
                folder = self.settings.mods / "odd-mod"
                _write(folder, text)
                with self.assertRaises(mods.ManifestError) as cm:
                    mods.load_all(self.settings)
                self.assertIn("odd-mod", str(cm.exception))


class ExpandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.settings = _settings(self.tmp, game=Path(r"C:\Games\MLB The Show 26"))
        self.mod = mods.Mod(id="m", name="M", description="", folder=self.tmp / "mods" / "m",
                            art="", files=None, runtime=None, server=None, profile=None, order=100)

    def tearDown(self):
        self._tmp.cleanup()

    def test_placeholders_keep_spaces(self):
        out = mods.expand(["{game}/x"], self.mod, self.settings)
        self.assertEqual(out, [r"C:\Games\MLB The Show 26/x"])

    def test_all_placeholders(self):
        s = self.settings
        out = mods.expand("{mod}|{mods}|{game}|{state}|{port}|{profile}", self.mod, s,
                          {"profile": Path(r"C:\P Q")})
        self.assertEqual(out, "|".join([str(self.mod.folder), str(s.mods), str(s.game),
                                        str(s.state), "10010", r"C:\P Q"]))

    def test_recursive_and_passthrough(self):
        value = {"{game}": ["{port}", 3, None, True, {"k": "{state}"}], "n": 7}
        out = mods.expand(value, self.mod, self.settings)
        self.assertEqual(out, {"{game}": ["10010", 3, None, True, {"k": str(self.settings.state)}],
                               "n": 7})

    def test_unknown_and_regex_braces_untouched(self):
        out = mods.expand(r"{profile} {nope} a{2,3}\.XML {}", self.mod, self.settings)
        self.assertEqual(out, r"{profile} {nope} a{2,3}\.XML {}")

    def test_extra_overrides(self):
        out = mods.expand("{port}", self.mod, self.settings, {"port": "1"})
        self.assertEqual(out, "1")


if __name__ == "__main__":
    unittest.main()
