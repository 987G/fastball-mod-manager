import tempfile
import unittest
from pathlib import Path

from app import mods, patchsets

SPEED = """# Real Pitch Speed: exit velocity
set  pitch_ev  MLB_PITCH_EV  1   # the switch

site pitch_ev  main  1681f20  52a852c8  52a84908   # low speed 50
site pitch_ev  main  1681f24  52a85909  52a85bc9
"""


def _mod(root: Path, mod_id: str, text: str | None, order: int = 100) -> mods.Mod:
    folder = root / mod_id
    folder.mkdir(parents=True, exist_ok=True)
    path = None
    if text is not None:
        path = folder / "patches.txt"
        path.write_text(text, encoding="utf-8")
    return mods.Mod(id=mod_id, name=mod_id.title(), description="", folder=folder, art="",
                    files=None, runtime=None, server=None, profile=None, order=order,
                    patches=path)


class ReadSetsTests(unittest.TestCase):
    def test_set_lines_with_comments_and_blanks(self):
        text = "# head\n\n   \nset a MLB_A 1 # on by default\n  set b MLB_B 0\n#set c MLB_C 1\n"
        self.assertEqual(patchsets.read_sets(text), [("a", "MLB_A", 1), ("b", "MLB_B", 0)])

    def test_other_kinds_are_ignored(self):
        text = ("set a MLB_A 1\n"
                "site a main 10 d503201f d503201f\n"
                "spin main 100 104\n"
                "data a main 200 0102\n"
                "page a 40 deadbeef\n"
                "frobnicate whatever 1 2 3\n"
                "set b MLB_B 0\n")
        self.assertEqual(patchsets.read_sets(text), [("a", "MLB_A", 1), ("b", "MLB_B", 0)])

    def test_malformed_set_line_is_left_to_the_generator(self):
        self.assertEqual(patchsets.read_sets("set a MLB_A\nset b MLB_B x\n"),
                         [("b", "MLB_B", "x")])


class CollectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_lists_every_set_in_mod_order(self):
        all_mods = [_mod(self.root, "speed", SPEED), _mod(self.root, "plain", None),
                    _mod(self.root, "other", "set x MLB_X 0\nset y MLB_Y 1\n")]
        self.assertEqual(patchsets.collect(all_mods), [
            {"mod": "speed", "name": "pitch_ev", "env": "MLB_PITCH_EV", "default": 1},
            {"mod": "other", "name": "x", "env": "MLB_X", "default": 0},
            {"mod": "other", "name": "y", "env": "MLB_Y", "default": 1}])

    def test_same_set_name_in_two_mods_names_both(self):
        all_mods = [_mod(self.root, "first", "set shared MLB_A 1\n"),
                    _mod(self.root, "second", "set shared MLB_B 1\n")]
        with self.assertRaises(patchsets.PatchSetError) as cm:
            patchsets.collect(all_mods)
        self.assertIn("first", str(cm.exception))
        self.assertIn("second", str(cm.exception))
        self.assertIn("shared", str(cm.exception))

    def test_same_switch_in_two_mods_names_both(self):
        all_mods = [_mod(self.root, "first", "set a MLB_SAME 1\n"),
                    _mod(self.root, "second", "set b MLB_SAME 1\n")]
        with self.assertRaises(patchsets.PatchSetError) as cm:
            patchsets.collect(all_mods)
        self.assertIn("first", str(cm.exception))
        self.assertIn("second", str(cm.exception))
        self.assertIn("MLB_SAME", str(cm.exception))

    def test_unreadable_file_names_the_mod(self):
        mod = _mod(self.root, "gone", "set a MLB_A 1\n")
        mod.patches.unlink()
        for call in (lambda: patchsets.collect([mod]), lambda: patchsets.combine([mod])):
            with self.subTest(call=call):
                with self.assertRaises(patchsets.PatchSetError) as cm:
                    call()
                self.assertIn("gone", str(cm.exception))

    def test_unchecked_collect_lists_duplicates_without_raising(self):
        all_mods = [_mod(self.root, "first", "set a MLB_A 1\n"),
                    _mod(self.root, "second", "set a MLB_A 1\n")]
        self.assertEqual(len(patchsets.collect(all_mods, check=False)), 2)


class CombineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_content_and_order(self):
        a = _mod(self.root, "aaa", "set a MLB_A 1\nsite a main 10 1 2")  # no final newline
        b = _mod(self.root, "bbb", None)
        c = _mod(self.root, "ccc", "set c MLB_C 0\n")
        text, spans = patchsets.combine([a, b, c])
        self.assertEqual(text, f"# --- aaa: {a.patches}\nset a MLB_A 1\nsite a main 10 1 2\n"
                               f"# --- ccc: {c.patches}\nset c MLB_C 0\n")
        self.assertEqual(spans, [("aaa", a.patches, 1, 3), ("ccc", c.patches, 4, 5)])

    def test_nothing_without_patch_mods(self):
        self.assertEqual(patchsets.combine([_mod(self.root, "plain", None)]), ("", []))


class LocateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.other = _mod(self.root, "other", "set x MLB_X 0\nsite x main 100 1 2\n")
        self.speed = _mod(self.root, "speed", SPEED)
        self.all = [self.other, self.speed]
        self.text, self.spans = patchsets.combine(self.all)
        self.layout = [{"name": "main", "base": "0x7000000000", "size": "0x4b34000"},
                       {"name": "sdk", "base": "0x7004c00000", "size": "0xc5e000"}]

    def test_module_offset_in_error(self):
        err = "patches.PatchError: main+0x1681f24: module has 00000000, the patch expects 52a85909"
        hint = patchsets.locate(err, self.all, self.spans, self.layout)
        self.assertIn("speed", hint)
        self.assertIn(str(self.speed.patches), hint)
        self.assertNotIn("other", hint)

    def test_absolute_address_in_error(self):
        err = "patches.PatchError: 0x7001681f20: patched instruction is a br"
        self.assertIn("speed", patchsets.locate(err, self.all, self.spans, self.layout))

    def test_combined_file_line_in_error(self):
        err = f"patches.PatchError: C:\\x\\patches.txt:{self.spans[1][2] + 1}: set pitch_ev defined twice"
        self.assertIn("speed", patchsets.locate(err, self.all, self.spans, self.layout))

    def test_nothing_found(self):
        self.assertIsNone(patchsets.locate("MemoryError", self.all, self.spans, self.layout))
        self.assertIsNone(patchsets.locate("sdk+0x10: bad", self.all, self.spans, None))


if __name__ == "__main__":
    unittest.main()
