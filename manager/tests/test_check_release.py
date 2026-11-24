"""tools/check_release.py: the content and anonymity check run on the public tree before every push and in CI.

This file is published with the tree (manager/tests/ there, while the tools sit at the tree's root tools/), so it
finds the tools folder in either layout and never holds the strings it tests for: each is put together here."""
import contextlib
import hashlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path


def _tools() -> Path:
    here = Path(__file__).resolve().parent
    for folder in (here.parent / "tools", here.parent.parent / "tools"):
        if (folder / "check_release.py").is_file():
            return folder
    raise FileNotFoundError(f"no tools/check_release.py beside or above {here}")


TOOLS = _tools()
_SPEC = importlib.util.spec_from_file_location("check_release", TOOLS / "check_release.py")
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)

# Put together so that this file passes its own check.
AI_WORD = "Cla" + "ude"
AI_COMPANY = "Anth" + "ropic"
BACKSLASH = "\\"
WIN_USER_PATH = "C:" + BACKSLASH + "Users" + BACKSLASH + "someone" + BACKSLASH + "x.txt"
MAC_USER_PATH = "/" + "Users" + "/someone/x.txt"
AT = "@"
EMAIL = "some.one" + AT + "example.org"
MB2 = 2 * 1024 * 1024
RELAY = "343694001+987G" + AT + "users.noreply.github.com"
HASH = "fnv64:" + "0123456789abcdef"


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, rel: str, content) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8", newline="")
        else:
            path.write_bytes(content)
        return path

    def scan(self, words=()) -> list[str]:
        return check.scan(self.root, list(words))

    def assertFound(self, findings, prefix, reason_part):
        hits = [f for f in findings if f.startswith(prefix) and reason_part in f]
        self.assertTrue(hits, f"no finding starting {prefix!r} mentioning {reason_part!r} in {findings}")

    # -- clean -------------------------------------------------------------------------------------
    def test_clean_tree_passes(self):
        self.put("README.md", "# Show Mod Manager\n\nContact: " + RELAY + "\n")
        self.put("manager/app/x.py", "GAME = r'D:" + BACKSLASH + "Games" + BACKSLASH + "First Last'\n")
        self.put("mods/a/patches.txt", "set a MLB_A 1\nsite a main 1234 d503201f 52800020\n"
                 "data a main 2176858 " + HASH + " 0000803f   # a value\n")
        self.put("mods/a/art.png", b"\x89PNG\r\n\x1a\n\0\0\0")
        self.assertEqual(self.scan(["Zorbix"]), [])

    # -- game files and size -----------------------------------------------------------------------
    def test_game_file_suffixes_are_found(self):
        for suffix in (".webp", ".dsd", ".xbn", ".bbs", ".bc7", ".nsp", ".nca", ".xci", ".keys"):
            self.put("mods/a/file" + suffix.upper(), b"\0")
        findings = self.scan()
        for suffix in (".webp", ".dsd", ".xbn", ".bbs", ".bc7", ".nsp", ".nca", ".xci", ".keys"):
            self.assertFound(findings, "mods/a/file" + suffix.upper() + ":0:", "game file")

    def test_file_over_2_mb_is_found(self):
        self.put("mods/a/big.pack", b"\1" * (2 * 1024 * 1024 + 1))
        self.put("mods/a/edge.pack", b"\1" * (2 * 1024 * 1024))
        findings = self.scan()
        self.assertFound(findings, "mods/a/big.pack:0:", "over 2 MB")
        self.assertFalse([f for f in findings if f.startswith("mods/a/edge.pack")])

    def test_big_file_listed_with_its_sha256_is_allowed(self):
        data = b"\1" * (2 * 1024 * 1024 + 1)
        self.put("mods/a/big.pack", data)
        self.put(".release-allow", "# approved packs\n" + hashlib.sha256(data).hexdigest() + "  mods/a/big.pack\n")
        self.assertEqual(self.scan(), [])

    def test_allowed_big_file_that_changed_is_found(self):
        data = b"\1" * (2 * 1024 * 1024 + 1)
        self.put("mods/a/big.pack", data + b"\2")
        self.put(".release-allow", hashlib.sha256(data).hexdigest() + "  mods/a/big.pack\n")
        self.assertFound(self.scan(), "mods/a/big.pack:0:", "sha256")

    def test_text_entry_exempts_a_big_text_file_from_the_size_rule_only(self):
        self.put("runtime/mods.patch", "+x\n" * (MB2 // 3 + 1) + "+by zorbix\n")
        self.put(".release-allow", "text  runtime/mods.patch\n")
        findings = self.scan(["Zorbix"])
        self.assertFalse([f for f in findings if "2 MB" in f], findings)
        self.assertFound(findings, "runtime/mods.patch:" + str(MB2 // 3 + 2) + ":", "private word list")

    def test_text_entry_naming_a_binary_file_is_found(self):
        self.put("mods/a/big.pack", b"\0" * (MB2 + 1))
        self.put(".release-allow", "text  mods/a/big.pack\n")
        self.assertFound(self.scan(), "mods/a/big.pack:0:", "binary")

    def test_bad_allow_line_is_found(self):
        self.put(".release-allow", "mods/a/big.pack\n")
        self.assertFound(self.scan(), ".release-allow:1:", "sha256")

    # -- personal information ------------------------------------------------------------------------
    def test_windows_user_paths_are_found(self):
        escaped = WIN_USER_PATH.replace(BACKSLASH, BACKSLASH * 2)
        self.put("a.py", "x = 1\np = '" + escaped + "'\n")
        self.put("b.md", "see " + WIN_USER_PATH.lower() + "\n")
        self.put("c.txt", "C:/" + "Users/someone/x\n")
        self.put("d.txt", "home " + MAC_USER_PATH + "\n")
        findings = self.scan()
        self.assertFound(findings, "a.py:2:", "user path")
        self.assertFound(findings, "b.md:1:", "user path")
        self.assertFound(findings, "c.txt:1:", "user path")
        self.assertFound(findings, "d.txt:1:", "user path")

    def test_email_address_is_found(self):
        self.put("a.md", "line\nwrite to " + EMAIL + "\n")
        self.assertFound(self.scan(), "a.md:2:", "e-mail")

    def test_relay_address_is_allowed(self):
        self.put("a.md", "Author: 987G <" + RELAY + ">\n")
        self.assertEqual(self.scan(), [])

    def test_ai_tool_words_are_found_in_any_case(self):
        self.put("a.py", "# made with " + AI_WORD.upper() + "\n")
        self.put("b.md", "x\n" + AI_COMPANY.lower() + " inc\n")
        findings = self.scan()
        self.assertFound(findings, "a.py:1:", "AI tool")
        self.assertFound(findings, "b.md:2:", "AI tool")

    def test_private_words_found_without_being_in_the_source(self):
        word, mail = "Zorbix", "zorbix.q" + AT + "example.net"
        self.put("a.txt", "the zorbix folder\nZorbixes and Zorbixian are other words\n")
        self.put("b.txt", "ok\npath D:" + BACKSLASH + "zorbix_old" + BACKSLASH + "x\n")
        self.put("c.txt", "mail " + mail.upper() + "\n")
        findings = self.scan([word, mail])
        self.assertFound(findings, "a.txt:1:", "private word list")
        self.assertFalse([f for f in findings if f.startswith("a.txt:2:")], findings)
        self.assertFound(findings, "b.txt:2:", "private word list")
        self.assertFound(findings, "c.txt:1:", "private word list")
        self.assertFalse([f for f in findings if "zorbix" in f.lower()], "a finding repeats a private word")
        self.assertNotIn("zorbix", Path(check.__file__).read_text(encoding="utf-8").lower())

    def test_private_word_in_a_folder_name_is_never_printed(self):
        self.put("mods/x/Zorbix-notes/a.md", "x\nwrite to " + EMAIL + "\n")
        self.put("mods/x/zorbix/big.webp", b"\0" * (MB2 + 1))
        findings = self.scan(["Zorbix"])
        self.assertFound(findings, "mods/x/***-notes/a.md:0:", "private word list")
        self.assertFound(findings, "mods/x/***-notes/a.md:2:", "e-mail")
        self.assertFound(findings, "mods/x/***/big.webp:0:", "game file")
        self.assertFound(findings, "mods/x/***/big.webp:0:", "over 2 MB")
        self.assertFalse([f for f in findings if "zorbix" in f.lower()], findings)

    def test_file_and_folder_names_are_checked(self):
        self.put("." + AI_WORD.lower() + "/settings.json", "{}\n")
        self.assertFound(self.scan(), "." + AI_WORD.lower() + "/settings.json:0:", "AI tool")

    # -- game bytes in patch files -----------------------------------------------------------------
    def test_patch_data_line_with_raw_original_bytes_is_found(self):
        self.put("mods/a/patches.txt", "set a MLB_A 1\n"
                 "data a main 2176858 cdcc1242 5c8f4742   # raw original\n"
                 "data a main 2176860 " + HASH + " 5c8f4742\n")
        self.put("runtime/mods.patch", "+++ b/recomp/x.txt\n+data a main 10 00000a42 14ae4142\n")
        findings = self.scan()
        self.assertFound(findings, "mods/a/patches.txt:2:", "game bytes")
        self.assertFalse([f for f in findings if f.startswith("mods/a/patches.txt:3:")], findings)
        self.assertFound(findings, "runtime/mods.patch:2:", "game bytes")

    def test_raw_original_is_found_whatever_the_other_fields_hold(self):
        self.put("mods/a/patches.txt", "data a main 0x2176858 cdcc1242 5c8f4742\n"
                 "data a main 2176858 cdcc1242 @future:x1\n"
                 "data a main 2176858 cdcc1242\n"
                 "data a main 0x2176858 " + HASH + " @future:x1\n")
        findings = self.scan()
        for n in (1, 2, 3):
            self.assertFound(findings, "mods/a/patches.txt:" + str(n) + ":", "game bytes")
        self.assertFalse([f for f in findings if f.startswith("mods/a/patches.txt:4:")], findings)

    def test_site_lines_with_instruction_words_are_allowed(self):
        self.put("mods/a/patches.txt", "site a main 7001872670 b9400020 52800020 d65f03c0\n")
        self.assertEqual(self.scan(), [])

    # -- what is read as text ----------------------------------------------------------------------
    def test_binary_files_are_not_checked_for_emails_or_ai_words(self):
        # too many chance hits in compiled code and data; their bytes are checked for user paths and private words
        self.put("mods/a/masks.bin", b"\0\1" + AI_WORD.encode() + b"\0" + EMAIL.encode())
        self.assertEqual(self.scan(), [])

    def test_user_path_in_a_binary_is_found_as_latin1_and_utf16le(self):
        path = WIN_USER_PATH.encode("ascii")
        self.put("lib/a.a", b"\0\x7fELF\xe9" + path + b"\0")
        self.put("lib/b.exe", b"MZ\0\0" + MAC_USER_PATH.encode("utf-16-le") + b"\0\0")
        self.put("lib/c.exe", b"MZ\0" + WIN_USER_PATH.lower().encode("utf-16-le") + b"\0\0")  # odd offset
        findings = self.scan()
        for name in ("lib/a.a", "lib/b.exe", "lib/c.exe"):
            self.assertFound(findings, name + ":0:", "in its bytes: a Windows or Mac user path")
        self.assertEqual(len(findings), 3, findings)

    def test_private_word_in_a_binary_is_found_and_never_printed(self):
        self.put("lib/zorbix/a.a", b"\0\1by Zorbix\0")
        self.put("lib/b.dll", b"MZ\0" + "built by ZORBIX".encode("utf-16-le"))
        self.put("lib/c.dll", b"MZ\0Zorbixes\0")  # not the whole word
        findings = self.scan(["Zorbix"])
        self.assertFound(findings, "lib/***/a.a:0:", "in its bytes: word 1 of the private word list")
        self.assertFound(findings, "lib/b.dll:0:", "in its bytes: word 1 of the private word list")
        self.assertFalse([f for f in findings if f.startswith("lib/c.dll")], findings)
        self.assertFalse([f for f in findings if "zorbix" in f.lower()], findings)

    def test_binary_is_read_like_strings_a_word_inside_a_printable_path_run(self):
        # a leak sits in a long printable run (a path in debug info); a chance hit sits between non-printable bytes
        path = "D:" + BACKSLASH + "zorbix" + BACKSLASH + "work" + BACKSLASH + "x.c"
        self.put("lib/latin1.a", b"\x8f\x01" + path.encode("ascii") + b"\0\x93")
        self.put("lib/utf16.exe", b"MZ\x90" + path.encode("utf-16-le") + b"\0\0\xfe")
        self.put("lib/utf16-odd.exe", b"MZ" + path.encode("utf-16-le") + b"\0\0\xfe")
        self.put("lib/chance.pack", b"\0\xdd\xae\x02zorbix\x98\x0e" + b"\x1c\x12\xc5ZORBIX\x83"
                 + b"\xe1L\xcfzorbix\x0e")
        self.put("lib/chance16.pack", b"\xdd" + "Zorbix".encode("utf-16-le") + b"\x98\xfe")
        self.put("lib/short.pack", b"\0\x01" + ("C:" + BACKSLASH + "Users").encode("ascii") + b"\x02")  # no rule
        findings = self.scan(["Zorbix"])
        for name in ("lib/latin1.a", "lib/utf16.exe", "lib/utf16-odd.exe"):
            self.assertFound(findings, name + ":0:", "in its bytes: word 1 of the private word list")
        self.assertFalse([f for f in findings if f.startswith(("lib/chance", "lib/short"))], findings)
        self.assertFalse([f for f in findings if "zorbix" in f.lower()], findings)

    def test_sha_approved_big_binaries_are_still_read(self):
        data = b"\1" * (MB2 + 1) + b"\0" + ("/home/build/" + "zorbix" + "/obj").encode("ascii") + b"\0"
        self.put("mods/a/big.pack", data)
        self.put(".release-allow", hashlib.sha256(data).hexdigest() + "  mods/a/big.pack\n")
        self.assertEqual(self.scan(["Zorbix"]),
                         ["mods/a/big.pack:0: in its bytes: word 1 of the private word list"])

    def test_a_file_of_a_pinned_download_is_left_to_the_path_rules(self):
        # byte-for-byte a file of a pinned third-party download (the embeddable Python): not ours, so its contents
        # and size are not checked; a file at that path with other bytes is checked as any other
        dll = b"MZ\0" + b"\1" * MB2 + b" Zorbix " + WIN_USER_PATH.encode("ascii")
        notice = "Copyright someone " + EMAIL + "\n"
        self.put("python/python314.dll", dll)
        self.put("python/LICENSE.txt", notice)
        self.put("python/other.dll", dll)
        self.put("python/x.WEBP", b"\0")
        exempt = {"python/python314.dll": hashlib.sha256(dll).hexdigest(),
                  "python/LICENSE.txt": hashlib.sha256(notice.encode()).hexdigest(),
                  "python/x.WEBP": hashlib.sha256(b"\0").hexdigest(),
                  "python/changed.txt": hashlib.sha256(b"old").hexdigest()}
        self.put("python/changed.txt", "now " + EMAIL + "\n")
        findings = check.scan(self.root, ["Zorbix"], exempt=exempt)
        self.assertFalse([f for f in findings if f.startswith(("python/python314.dll", "python/LICENSE.txt"))],
                         findings)
        self.assertFound(findings, "python/other.dll:0:", "over 2 MB")
        self.assertFound(findings, "python/other.dll:0:", "user path")
        self.assertFound(findings, "python/other.dll:0:", "private word list")
        self.assertFound(findings, "python/x.WEBP:0:", "game file")
        self.assertFound(findings, "python/changed.txt:1:", "e-mail")

    def test_utf16_and_non_utf8_text_is_read(self):
        self.put("a.rc", ("x\r\n" + AI_WORD + "\r\n").encode("utf-16"))
        self.put("b.txt", b"caf\xe9 " + EMAIL.encode() + b"\n")
        findings = self.scan()
        self.assertFound(findings, "a.rc:2:", "AI tool")
        self.assertFound(findings, "b.txt:1:", "e-mail")

    def test_compiled_python_cache_files_are_found(self):
        # bytecode holds the absolute path of the folder it was built in
        self.put("manager/app/__pycache__/x.cpython-314.pyc", b"pyc bytes")
        self.put("tools/y.pyc", b"pyc bytes")
        self.put("mods/a/z.PYO", b"pyo bytes")
        (self.root / "mods" / "b" / "__pycache__").mkdir(parents=True)  # empty: still never published
        self.put("mods/a/pycache_notes.py", "x = 1\n")  # not a cache file
        reason = ": compiled Python cache file (holds build paths); never publish it"
        findings = self.scan(["Zorbix"])
        self.assertEqual(sorted(f for f in findings if "compiled Python" in f),
                         sorted(rel + ":0" + reason for rel in ("manager/app/__pycache__", "tools/y.pyc",
                                                                "mods/a/z.PYO", "mods/b/__pycache__")))
        self.put("mods/zorbix/__pycache__/w.pyc", b"pyc bytes")  # a private word in its path is never printed
        findings = self.scan(["Zorbix"])
        self.assertIn("mods/***/__pycache__:0" + reason, findings)
        self.assertFalse([f for f in findings if "zorbix" in f.lower()], findings)

    def test_git_folder_is_not_scanned(self):
        self.put(".git/config", "[user]\n\temail = " + EMAIL + "\n")
        self.assertEqual(self.scan(), [])

    def test_the_check_and_its_tests_pass_their_own_check(self):
        for src in (Path(check.__file__), Path(__file__)):
            self.put("tools/" + src.name, src.read_bytes())
        self.assertEqual(self.scan(), [])


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "tree"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = check.main([str(self.root), *args])
        return code, out.getvalue()

    def test_clean_exits_0(self):
        (self.root / "a.txt").write_text("hello\n", encoding="utf-8")
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("clean", out)

    def test_findings_exit_1_with_the_private_list(self):
        (self.root / "a.txt").write_text("x\nby zorbix\n", encoding="utf-8")
        words = Path(self._tmp.name) / "words.txt"
        words.write_text("# words never published\n\nZorbix\n", encoding="utf-8")
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        code, out = self.run_cli("--private", str(words))
        self.assertEqual(code, 1)
        self.assertIn("a.txt:2: ", out)
        self.assertNotIn("zorbix", out.lower())

    def test_missing_private_list_is_an_error(self):
        code, out = self.run_cli("--private", str(Path(self._tmp.name) / "none.txt"))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
