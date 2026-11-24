import _winapi
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import builder, config, launcher, mods, patchsets, procs, toolkit
from tests.test_toolkit import FASTBALL, make_install, make_runtime, make_setup, sha

OLD = {"main": "a" * 40, "fakeserver/login-probe": "b" * 40, "mod/pitch-types": "c" * 40}
NEW = dict(OLD, **{"mod/pitch-types": "d" * 40})
BRANCHES = ["fakeserver/login-probe", "mod/pitch-types"]
REAL_RUN_GEN = builder._run_gen


class FakeGit:
    """Stands in for builder._git; `conflicts` maps a merged branch to the
    conflicted paths that `git diff --diff-filter=U` reports after it."""

    def __init__(self, conflicts=None, tree="e" * 40, tools="f" * 40, clash=None,
                 abort_fails=False, stuck=False, untracked=()):
        self.conflicts = conflicts or {}
        # clash: {(earlier, later): paths}: merging `later` conflicts once `earlier` is merged
        self.clash = clash or {}
        self.tree = tree  # what `rev-parse HEAD:recomp` answers
        self.tools = tools  # what `rev-parse HEAD:tools` answers
        self.abort_fails = abort_fails  # `merge --abort` fails and leaves the merge in progress
        self.stuck = stuck  # `reset --hard HEAD` does not clean up either
        self.untracked = list(untracked)  # what `ls-files --others` answers
        self.calls = []
        self._unmerged = []
        self._merging = None
        self.merged = []

    def __call__(self, args, cwd, log):
        self.calls.append(list(args))
        if args == ["rev-parse", "HEAD:recomp"]:
            return 0, self.tree + "\n"
        if args == ["rev-parse", "HEAD:tools"]:
            return 0, self.tools + "\n"
        if args[:2] == ["reset", "--hard"]:
            if args[2] == "main":
                self.merged = []
            if args[2] == "main" or not self.stuck:
                self._unmerged, self._merging = [], None
            return 0, ""
        if args[0] == "merge" and args[1] == "--no-edit":
            branch = args[2]
            self._unmerged = list(self.conflicts.get(branch, []))
            for (earlier, later), paths in self.clash.items():
                if later == branch and earlier in self.merged:
                    self._unmerged += [p for p in paths if p not in self._unmerged]
            if self._unmerged:
                self._merging = branch
                return 1, "CONFLICT"
            self.merged.append(branch)
            return 0, "merged"
        if args[:2] == ["diff", "--name-only"]:
            return 0, "\n".join(self._unmerged)
        if args[:2] == ["merge", "--abort"]:
            if self.abort_fails:
                return 128, "fatal: could not abort the merge"
            self._unmerged, self._merging = [], None
            return 0, ""
        if args == ["commit", "--no-edit"] and self._merging:
            self.merged.append(self._merging)
            self._unmerged, self._merging = [], None
            return 0, ""
        if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
            return (0, "1" * 40) if self._merging else (1, "")
        if args == ["status", "--porcelain", "-uno"]:
            return 0, "\n".join("UU " + p for p in self._unmerged)
        if args[:2] == ["ls-files", "--others"]:
            return 0, "\n".join(self.untracked)
        return 0, ""


class FakeGen:
    """Stands in for builder._run_gen: writes `files` (+ layout.h/json) under --out."""

    def __init__(self, files=None, code=0, output="done"):
        self.files = files if files is not None else {"main/main_0000.c": "int x;"}
        self.code, self.output = code, output
        self.calls = []
        self.cwds = []

    def __call__(self, argv, cwd, log):
        self.calls.append(list(argv))
        self.cwds.append(Path(cwd))
        out = Path(argv[argv.index("--out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layout.json").write_text(json.dumps(
            [{"name": "main", "base": "0x7000000000", "size": "0x4b34000"}]), encoding="utf-8")
        (out / "layout.h").write_text("// layout", encoding="utf-8")
        for rel, text in self.files.items():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_text(text, encoding="utf-8")
        log("emitted 1/1 chunks, 0s")
        return self.code, self.output


def _patch_mod(mods_dir: Path, mod_id: str, text: str, order: int, runtime=None) -> Path:
    folder = mods_dir / mod_id
    folder.mkdir(parents=True)
    manifest = {"name": mod_id, "order": order, "patches": "patches.txt"}
    if runtime:
        manifest["runtime"] = runtime
    (folder / "mod.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "patches.txt").write_text(text, encoding="utf-8")
    return folder / "patches.txt"


class _BuildCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.settings = dataclasses.replace(
            config.Settings(), state=self.tmp / "state", build_out=self.tmp / "out",
            build_wt=self.tmp / "wt", recomp=self.tmp / "recomp", setup=self.tmp / "setup",
            mods=self.tmp / "mods", game=self.tmp / "game")
        (self.settings.recomp / ".git").mkdir(parents=True)  # a source clone: dev mode
        self.settings.mods.mkdir()
        self.settings.build_out.mkdir()
        self.exefs_main = self.settings.game / "game" / "exefs" / "main"
        self.exefs_main.parent.mkdir(parents=True)
        self.exefs_main.write_bytes(b"NSO0")
        self.exefs_sdk = self.exefs_main.with_name("sdk")
        self.exefs_sdk.write_bytes(b"NSO0sdk")
        self.gen = FakeGen()
        for p in [mock.patch("app.procs.running_games", return_value=[]),
                  mock.patch("app.builder.heads",
                             side_effect=lambda branches, s, missing_ok=(): {b: NEW[b] for b in branches}),
                  mock.patch("app.builder._run_gen", self.gen),
                  mock.patch("app.builder.BUILDS", self.tmp)]:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_info(self, built_from):
        (self.settings.build_out / "mlb.exe").write_bytes(b"OLD EXE")
        (self.settings.build_out / "build-info.json").write_text(json.dumps(
            {"built_from": built_from, "exe": str(self.settings.build_out / "mlb.exe"),
             "built_at": "2026-11-16T00:00:00"}), encoding="utf-8")


class BuilderTests(_BuildCase):
    def test_needed_heads_change_marks_out_of_date(self):
        stamp = builder.exefs_stamp(self.settings)
        self._write_info(dict(OLD, exefs=stamp))
        with mock.patch("app.builder._runtime_branches", return_value=BRANCHES):
            st = builder.state(self.settings)
        self.assertFalse(st["up_to_date"])
        self.assertEqual(st["built_from"], dict(OLD, exefs=stamp))
        self.assertEqual(st["needed"], dict(NEW, exefs=stamp))
        self.assertEqual(st["exe"], str(self.settings.build_out / "mlb.exe"))
        self.assertIsNone(st["last_error"])
        self._write_info(dict(NEW, exefs=stamp))
        with mock.patch("app.builder._runtime_branches", return_value=BRANCHES):
            self.assertTrue(builder.state(self.settings)["up_to_date"])

    def test_build_command_has_no_shell_and_exact_args(self):
        s = self.settings
        expected = [r"C:\Py\python.exe", "-I", "tools/build.py",
                    "--toolchain", str(s.setup / "llvm"),
                    "--deps", str(s.setup / "runtime/deps"),
                    "--ninja", str(s.setup / "bin/ninja.exe"),
                    "--gen", str(s.build_out.parent / "gen"),
                    "--version", "1.0.20", "--out", str(s.build_out)]
        with mock.patch("app.procs.python_exe", return_value=r"C:\Py\python.exe"):
            self.assertEqual(builder.build_command(s), expected)
        seen = {}

        def fake_popen(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            raise OSError("not really")

        with mock.patch("app.builder.subprocess.Popen", side_effect=fake_popen):
            with self.assertRaises(builder.BuildFailed):
                builder._run_build(expected, s.build_wt, lambda line: None)
        self.assertEqual(seen["argv"], expected)
        self.assertFalse(seen["kw"].get("shell", False))
        self.assertEqual(Path(seen["kw"]["cwd"]), s.build_wt)

    def test_failed_build_keeps_previous_exe(self):
        self._write_info(OLD)
        info_before = (self.settings.build_out / "build-info.json").read_bytes()
        output = "\n".join(f"line {i}" for i in range(100)) + "\nerror: link failed"
        logged = []

        def failing_link(argv, cwd, log):
            # a failed link can leave a truncated output behind
            (self.settings.build_out / "mlb.exe").write_bytes(b"PARTIAL")
            return 1, output

        with mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build", failing_link):
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build(BRANCHES, self.settings, logged.append)
        self.assertIn("error: link failed", str(ctx.exception))
        self.assertEqual(str(ctx.exception).splitlines()[0],
                         "build failed (exit 1): error: link failed — see Builds & Logs")
        self.assertEqual((self.settings.build_out / "mlb.exe").read_bytes(), b"OLD EXE")
        self.assertEqual((self.settings.build_out / "build-info.json").read_bytes(), info_before)
        with mock.patch("app.builder._runtime_branches", return_value=BRANCHES):
            st = builder.state(self.settings)
        self.assertIn("error: link failed", st["last_error"])
        self.assertNotIn("line 0\n", st["last_error"])
        self.assertFalse(st["up_to_date"])
        self.assertEqual(st["exe"], str(self.settings.build_out / "mlb.exe"))

    def test_unexpected_conflict_aborts(self):
        git = FakeGit({"mod/pitch-types": ["runtime/core/x.c"]})
        build = mock.Mock(return_value=(0, ""))
        with mock.patch("app.builder._git", git), mock.patch("app.builder._run_build", build):
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build(BRANCHES, self.settings, lambda line: None)
        self.assertIn("runtime/core/x.c", str(ctx.exception))
        self.assertIn(["merge", "--abort"], git.calls)
        build.assert_not_called()
        self.assertFalse((self.settings.build_out / "build-info.json").exists())

    def test_known_conflict_takes_pitch_side_and_builds(self):
        conflicted = ["runtime/mlb/hid.c", "runtime/gfx/gfx_stubs.c"]
        git = FakeGit({"mod/pitch-types": conflicted})

        def fake_build(argv, cwd, log):
            (self.settings.build_out / "mlb.exe").write_bytes(b"NEW EXE")
            log("[1/1] Linking mlb.exe")
            return 0, "ok"

        logged = []
        (self.settings.state).mkdir()
        (self.settings.state / "build-error.txt").write_text("old failure")
        with mock.patch("app.builder._git", git), mock.patch("app.builder._run_build", fake_build):
            exe = builder.build(BRANCHES, self.settings, logged.append)
        self.assertEqual(exe, self.settings.build_out / "mlb.exe")
        self.assertEqual(git.calls[0], ["reset", "--hard", "main"])
        self.assertIn(["checkout", "mod/pitch-types", "--"] + conflicted, git.calls)
        self.assertIn(["commit", "--no-edit"], git.calls)
        self.assertNotIn(["merge", "--abort"], git.calls)
        info = json.loads((self.settings.build_out / "build-info.json").read_text())
        self.assertEqual(info["built_from"], dict(NEW, exefs=builder.exefs_stamp(self.settings)))
        self.assertFalse((self.settings.state / "build-error.txt").exists())
        self.assertIn("[1/1] Linking mlb.exe", logged)
        log_file = (self.settings.state / "logs" / "build.log").read_text()
        self.assertIn("[1/1] Linking mlb.exe", log_file)

    def test_test_driver_conflict_in_another_branch_takes_pitch_version(self):
        # A branch cut from manager/build (Quirks+) carries an older test driver: its merge
        # conflicts in hid.c although its own commits never touched it.
        git = FakeGit({"mod/rtts-quirks": ["runtime/mlb/hid.c"]})
        with mock.patch("app.builder._git", git):
            builder._merge(BRANCHES + ["mod/rtts-quirks"], self.tmp, lambda line: None)
        self.assertIn(["checkout", "mod/pitch-types", "--", "runtime/mlb/hid.c"], git.calls)
        self.assertIn(["add", "--", "runtime/mlb/hid.c"], git.calls)
        self.assertEqual(git.calls[-1], ["commit", "--no-edit"])
        self.assertNotIn(["merge", "--abort"], git.calls)

    def test_test_driver_conflict_merging_network_branch_takes_pitch_version(self):
        git = FakeGit({"fakeserver/login-probe": ["runtime/mlb/hid.c"]})
        with mock.patch("app.builder._git", git):
            builder._merge(BRANCHES, self.tmp, lambda line: None)
        self.assertIn(["checkout", "mod/pitch-types", "--", "runtime/mlb/hid.c"], git.calls)
        self.assertNotIn(["merge", "--abort"], git.calls)

    def test_test_driver_conflict_without_pitch_branch_aborts(self):
        git = FakeGit({"mod/rtts-quirks": ["runtime/mlb/hid.c"]})
        with mock.patch("app.builder._git", git):
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder._merge(["fakeserver/login-probe", "mod/rtts-quirks"], self.tmp,
                               lambda line: None)
        self.assertIn("runtime/mlb/hid.c", str(ctx.exception))
        self.assertIn(["merge", "--abort"], git.calls)
        self.assertFalse(any(c[0] == "checkout" for c in git.calls))

    def test_mixed_conflict_in_another_branch_aborts(self):
        git = FakeGit({"mod/rtts-quirks": ["runtime/mlb/hid.c", "runtime/mlb/boot.c"]})
        with mock.patch("app.builder._git", git):
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder._merge(BRANCHES + ["mod/rtts-quirks"], self.tmp, lambda line: None)
        self.assertIn("runtime/mlb/boot.c", str(ctx.exception))
        self.assertIn(["merge", "--abort"], git.calls)

    def test_build_refuses_while_game_runs_from_build_out(self):
        exe = str(self.settings.build_out / "mlb.exe")
        git = FakeGit()
        with mock.patch("app.procs.running_games", return_value=[{"pid": 4242, "exe": exe}]), \
                mock.patch("app.builder._git", git):
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build(BRANCHES, self.settings, lambda line: None)
        self.assertIn("4242", str(ctx.exception))
        self.assertIn(exe, str(ctx.exception))
        self.assertEqual(git.calls, [])

    def test_game_running_elsewhere_does_not_block(self):
        git = FakeGit()
        with mock.patch("app.procs.running_games",
                        return_value=[{"pid": 1, "exe": r"C:\Games\MLB The Show 26\mlb.exe"}]), \
                mock.patch("app.builder._git", git), \
                mock.patch("app.builder._run_build", return_value=(0, "")):
            (self.settings.build_out / "mlb.exe").write_bytes(b"x")
            builder.build(BRANCHES, self.settings, lambda line: None)
        self.assertTrue((self.settings.build_out / "build-info.json").exists())

    def test_leftover_backup_is_restored_before_building(self):
        out = self.settings.build_out
        (out / "mlb.exe").write_bytes(b"PARTIAL")  # a killed build left these
        (out / "mlb.exe.prev").write_bytes(b"GOOD")
        seen = {}

        def failing_link(argv, cwd, log):
            seen["exe"] = (out / "mlb.exe").read_bytes()
            seen["prev"] = (out / "mlb.exe.prev").read_bytes()
            (out / "mlb.exe").write_bytes(b"PARTIAL AGAIN")
            return 1, "link error"

        with mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build", failing_link):
            with self.assertRaises(builder.BuildFailed):
                builder.build(BRANCHES, self.settings, lambda line: None)
        self.assertEqual(seen, {"exe": b"GOOD", "prev": b"GOOD"})
        self.assertEqual((out / "mlb.exe").read_bytes(), b"GOOD")
        self.assertFalse((out / "mlb.exe.prev").exists())

    def test_failed_restore_raises_build_failed_and_keeps_backup(self):
        out = self.settings.build_out
        (out / "mlb.exe").write_bytes(b"OLD EXE")
        real_replace = builder.os.replace

        def replace(src, dst):
            if Path(dst).name == "mlb.exe":
                raise PermissionError("mlb.exe is in use")
            return real_replace(src, dst)

        def failing_link(argv, cwd, log):
            (out / "mlb.exe").write_bytes(b"PARTIAL")
            return 1, "link error"

        with mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build", failing_link), \
                mock.patch("app.builder.os.replace", replace):
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build(BRANCHES, self.settings, lambda line: None)
        self.assertIn("mlb.exe.prev", str(ctx.exception))
        self.assertEqual((out / "mlb.exe.prev").read_bytes(), b"OLD EXE")
        error = (self.settings.state / "build-error.txt").read_text()
        self.assertIn("link error", error)
        self.assertIn("mlb.exe is in use", error)

    def test_oserror_in_body_becomes_build_failed(self):
        out = self.settings.build_out
        (out / "mlb.exe").write_bytes(b"OLD EXE")
        for target in ["app.builder.shutil.copy2", "app.builder._write_info"]:
            with self.subTest(target=target):
                with mock.patch("app.builder._git", FakeGit()), \
                        mock.patch("app.builder._run_build", return_value=(0, "")), \
                        mock.patch(target, side_effect=OSError("disk full")):
                    with self.assertRaises(builder.BuildFailed):
                        builder.build(BRANCHES, self.settings, lambda line: None)
                self.assertIn("disk full",
                              (self.settings.state / "build-error.txt").read_text())
                self.assertEqual((out / "mlb.exe").read_bytes(), b"OLD EXE")
                self.assertFalse((out / "build-info.json").exists())
                (self.settings.state / "build-error.txt").unlink()


class CodePatchBuildTests(_BuildCase):
    """The manager's own translation of the game code (recomp/gen.py) inside build()."""

    def _ok_build(self, argv, cwd, log):
        (self.settings.build_out / "mlb.exe").write_bytes(b"NEW EXE")
        return 0, "ok"

    def _build(self, git=None):
        logged = []
        with mock.patch("app.builder._git", git or FakeGit()), \
                mock.patch("app.builder._run_build", side_effect=self._ok_build) as run_build:
            builder.build(BRANCHES, self.settings, logged.append)
        return logged, run_build

    def _gen_dir(self):
        return self.tmp / "gen"

    def test_gen_dir_helpers_are_build_out_siblings(self):
        self.assertEqual(builder.gen_dir(self.settings), self.tmp / "gen")
        self.assertEqual(builder.combined_patch_path(self.settings), self.tmp / "patches.txt")

    def test_gen_command_without_patches(self):
        with mock.patch("app.procs.python_exe", return_value=r"C:\Py\python.exe"):
            self._build()
        self.assertEqual(self.gen.calls, [[
            r"C:\Py\python.exe", "-I", "-X", "utf8", "recomp/gen.py",
            "--exefs", str(self.settings.game / "game" / "exefs"),
            "--out", str(self.tmp / "gen.new"), "--modules", "main,sdk"]])
        self.assertFalse((self.tmp / "patches.txt").exists())

    def test_gen_runs_under_the_setup_python_when_present(self):
        bundled = self.settings.setup / "python" / "python.exe"
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b"MZ")
        self._build()
        self.assertEqual(self.gen.calls[0][:5], [str(bundled), "-I", "-X", "utf8", "recomp/gen.py"])

    def test_gen_runs_in_utf8_mode_from_the_worktree(self):
        self._build()
        argv = self.gen.calls[0]
        self.assertEqual(argv[argv.index("-X") + 1], "utf8")
        self.assertEqual(self.gen.cwds, [self.settings.build_wt])

    def test_gen_argv_line_is_marked_gen(self):
        logged = []
        with mock.patch("app.builder.subprocess.Popen", side_effect=OSError("no")):
            with self.assertRaises(builder.BuildFailed):
                REAL_RUN_GEN(["py", "recomp/gen.py"], self.tmp, logged.append)
        self.assertEqual(logged, ["[gen] $ py recomp/gen.py"])

    def test_build_out_elsewhere_keeps_gen_in_builds(self):
        other = self.tmp / "elsewhere" / "out"
        other.mkdir(parents=True)
        self.settings = dataclasses.replace(self.settings, build_out=other)
        _patch_mod(self.settings.mods, "speed", "set ev MLB_EV 1\n", 30)
        self._build()
        self.assertTrue((self.tmp / "gen" / "layout.h").is_file())
        self.assertTrue((self.tmp / "patches.txt").is_file())
        self.assertFalse((other.parent / "gen").exists())
        self.assertFalse((other.parent / "patches.txt").exists())

    def test_refuses_a_foreign_non_empty_gen_folder(self):
        foreign = self._gen_dir() / "work.c"
        foreign.parent.mkdir()
        foreign.write_text("not ours")
        with mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build") as run_build:
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build(BRANCHES, self.settings, lambda line: None)
        self.assertIn(str(self._gen_dir()), str(ctx.exception))
        self.assertEqual(foreign.read_text(), "not ours")
        self.assertEqual(self.gen.calls, [])
        run_build.assert_not_called()

    def test_new_gen_folder_gets_the_manager_marker(self):
        self._build()
        self.assertTrue((self._gen_dir() / builder.GEN_MARKER).is_file())

    def test_sync_cut_short_regenerates(self):
        self._build()
        (self._gen_dir() / builder.GEN_KEY).unlink()  # as a killed sync leaves it
        self._build()
        self.assertEqual(len(self.gen.calls), 2)

    def test_leftover_gen_new_is_removed_even_when_skipped(self):
        self._build()
        (self.tmp / "gen.new" / "main").mkdir(parents=True)
        (self.tmp / "gen.new" / "main" / "half.c").write_text("x")
        logged, _ = self._build()
        self.assertEqual(len(self.gen.calls), 1)
        self.assertFalse((self.tmp / "gen.new").exists())

    def test_leftover_gen_new_that_cannot_go_is_build_failed(self):
        self._build()
        (self.tmp / "gen.new").mkdir()
        with mock.patch("app.builder.shutil.rmtree", side_effect=PermissionError("in use")), \
                mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build") as run_build:
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build(BRANCHES, self.settings, lambda line: None)
        self.assertIn("still running", str(ctx.exception))
        self.assertIn("gen.new", str(ctx.exception))
        run_build.assert_not_called()

    def test_other_tools_tree_regenerates(self):
        self._build(FakeGit(tools="1" * 40))
        self._build(FakeGit(tools="2" * 40))
        self.assertEqual(len(self.gen.calls), 2)

    def test_other_exefs_sdk_regenerates(self):
        self._build()
        os.utime(self.exefs_sdk, ns=(1, 1))
        self._build()
        self.assertEqual(len(self.gen.calls), 2)

    def test_game_update_marks_the_build_out_of_date(self):
        self._build()
        branches = mock.patch("app.builder._runtime_branches", return_value=BRANCHES)
        branches.start()
        self.addCleanup(branches.stop)
        st = builder.state(self.settings)
        main = self.exefs_main.stat()
        self.assertEqual(st["needed"]["exefs"], f"{main.st_size}:{main.st_mtime_ns}")
        self.assertTrue(st["up_to_date"], st)
        os.utime(self.exefs_main, ns=(1, 1))
        self.assertFalse(builder.state(self.settings)["up_to_date"])

    def test_combined_file_holds_every_patch_mod_in_order(self):
        mods_dir = self.settings.mods
        b = _patch_mod(mods_dir, "bbb", "set b MLB_B 1\n", 20)
        a = _patch_mod(mods_dir, "aaa", "set a MLB_A 0\nsite a main 10 1 2\n", 10)
        (mods_dir / "plain").mkdir()
        (mods_dir / "plain" / "mod.json").write_text('{"name": "Plain", "order": 5}')
        self._build()
        combined = (self.tmp / "patches.txt").read_text(encoding="utf-8")
        self.assertEqual(combined, f"# --- aaa: {a}\nset a MLB_A 0\nsite a main 10 1 2\n"
                                   f"# --- bbb: {b}\nset b MLB_B 1\n")
        argv = self.gen.calls[0]
        self.assertEqual(argv[-2:], ["--patches", str(self.tmp / "patches.txt")])
        self.assertTrue((self._gen_dir() / "layout.h").is_file())
        self.assertFalse((self.tmp / "gen.new").exists())

    def test_build_compiles_against_the_generated_c(self):
        _, run_build = self._build()
        argv = run_build.call_args[0][0]
        self.assertEqual(argv[argv.index("--gen") + 1], str(self._gen_dir()))

    def test_unchanged_key_skips_generation(self):
        _patch_mod(self.settings.mods, "speed", "set ev MLB_EV 1\n", 30)
        self._build()
        logged, run_build = self._build()
        self.assertEqual(len(self.gen.calls), 1)
        self.assertTrue(any("up to date" in line and "skipped" in line for line in logged), logged)
        run_build.assert_called_once()

    def test_changed_patch_text_regenerates(self):
        path = _patch_mod(self.settings.mods, "speed", "set ev MLB_EV 1\n", 30)
        self._build()
        path.write_text("set ev MLB_EV 0\n", encoding="utf-8")
        self._build()
        self.assertEqual(len(self.gen.calls), 2)

    def test_other_generator_tree_regenerates(self):
        self._build(FakeGit(tree="1" * 40))
        self._build(FakeGit(tree="1" * 40))
        self.assertEqual(len(self.gen.calls), 1)
        self._build(FakeGit(tree="2" * 40))
        self.assertEqual(len(self.gen.calls), 2)

    def test_other_exefs_regenerates(self):
        self._build()
        os.utime(self.exefs_main, ns=(1, 1))
        self._build()
        self.assertEqual(len(self.gen.calls), 2)

    def test_missing_layout_regenerates(self):
        self._build()
        (self._gen_dir() / "layout.h").unlink()
        self._build()
        self.assertEqual(len(self.gen.calls), 2)

    def test_sync_copies_only_changed_files(self):
        self.gen.files = {"main/a.c": "same", "main/b.c": "old", "main/c.c": "gone soon"}
        self._build(FakeGit(tree="1" * 40))
        gen = self._gen_dir()
        (gen / "prompt_icons.h").write_text("made by build.py")
        os.utime(gen / "main" / "a.c", ns=(10**9, 10**9))
        os.utime(gen / "main" / "b.c", ns=(10**9, 10**9))
        self.gen.files = {"main/a.c": "same", "main/b.c": "new"}
        self._build(FakeGit(tree="2" * 40))
        self.assertEqual((gen / "main" / "a.c").stat().st_mtime_ns, 10**9)
        self.assertEqual((gen / "main" / "b.c").read_text(), "new")
        self.assertNotEqual((gen / "main" / "b.c").stat().st_mtime_ns, 10**9)
        self.assertFalse((gen / "main" / "c.c").exists())
        self.assertEqual((gen / "prompt_icons.h").read_text(), "made by build.py")
        self.assertFalse((self.tmp / "gen.new").exists())

    def test_failed_gen_leaves_gen_and_exe_untouched(self):
        _patch_mod(self.settings.mods, "speed",
                   "set ev MLB_EV 1\nsite ev main 1681f20 52a852c8 52a84908\n", 30)
        self.gen.files = {"main/a.c": "good"}
        self._build()
        gen = self._gen_dir()
        before = {p: p.read_bytes() for p in gen.rglob("*") if p.is_file()}
        (self.settings.build_out / "mlb.exe").write_bytes(b"OLD EXE")
        (self.settings.mods / "speed" / "patches.txt").write_text(
            "set ev MLB_EV 1\nsite ev main 1681f20 0 0\n", encoding="utf-8")
        self.gen.files = {"main/a.c": "partial"}
        self.gen.code = 1
        self.gen.output = ("Traceback (most recent call last):\n"
                           "patches.PatchError: main+0x1681f20: module has 52a852c8, the patch expects 00000000")
        with mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build") as run_build:
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build(BRANCHES, self.settings, lambda line: None)
        run_build.assert_not_called()
        first = str(ctx.exception).splitlines()[0]
        self.assertIn("speed", first)
        self.assertIn("main+0x1681f20", first)
        self.assertIn("PatchError", str(ctx.exception))
        self.assertEqual({p: p.read_bytes() for p in gen.rglob("*") if p.is_file()}, before)
        self.assertFalse((self.tmp / "gen.new").exists())
        self.assertEqual((self.settings.build_out / "mlb.exe").read_bytes(), b"OLD EXE")
        self.assertIn("speed", (self.settings.state / "build-error.txt").read_text())

    def test_gen_that_cannot_start_is_build_failed(self):
        with mock.patch("app.builder._run_gen", side_effect=builder.BuildFailed("cannot start x")), \
                mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build") as run_build:
            with self.assertRaises(builder.BuildFailed):
                builder.build(BRANCHES, self.settings, lambda line: None)
        run_build.assert_not_called()
        self.assertTrue((self.settings.state / "build-error.txt").is_file())

    def test_duplicate_switch_fails_before_gen_naming_both(self):
        _patch_mod(self.settings.mods, "first", "set a MLB_SAME 1\n", 10)
        _patch_mod(self.settings.mods, "second", "set b MLB_SAME 1\n", 20)
        with mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build") as run_build:
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build(BRANCHES, self.settings, lambda line: None)
        self.assertIn("first", str(ctx.exception))
        self.assertIn("second", str(ctx.exception))
        self.assertEqual(self.gen.calls, [])
        run_build.assert_not_called()

    def test_state_needs_patch_hash_and_lists_sets(self):
        _patch_mod(self.settings.mods, "speed", "set ev MLB_EV 1\n", 30,
                   runtime={"branch": "mod/pitch-types", "env_on": {}, "env_off": {}})
        text, _ = patchsets.combine(builder.mods.load_all(self.settings))
        st = builder.state(self.settings)
        self.assertEqual(st["needed"]["patches"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(st["needed"]["mod/pitch-types"], NEW["mod/pitch-types"])
        self.assertEqual(st["patch_sets"], [{"mod": "speed", "name": "ev", "env": "MLB_EV", "default": 1}])
        self.assertFalse(st["up_to_date"])
        with mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build", side_effect=self._ok_build):
            builder.build(["mod/pitch-types"], self.settings, lambda line: None)
        st = builder.state(self.settings)
        self.assertTrue(st["up_to_date"], st)
        info = json.loads((self.settings.build_out / "build-info.json").read_text())
        self.assertEqual(info["patch_sets"], st["patch_sets"])
        self.assertEqual(info["built_from"], st["needed"])
        (self.settings.mods / "speed" / "patches.txt").write_text("set ev MLB_EV 0\n")
        self.assertFalse(builder.state(self.settings)["up_to_date"])

    def test_state_without_patch_mods_has_no_patch_hash(self):
        st = builder.state(self.settings)
        self.assertNotIn("patches", st["needed"])
        self.assertEqual(st["patch_sets"], [])

    def test_state_runs_no_git_and_no_gen(self):
        _patch_mod(self.settings.mods, "speed", "set ev MLB_EV 1\n", 30)
        with mock.patch("app.builder._git") as git, mock.patch("app.builder.subprocess") as sp:
            builder.state(self.settings)
        git.assert_not_called()
        sp.run.assert_not_called()
        self.assertEqual(self.gen.calls, [])


BUILTIN = "set walkup_light MLB_WALKUP_LIGHT 1\nset walkup_leaders MLB_WALKUP_LEADERS 1\n"


class BuiltinPatchTests(_BuildCase):
    """The release's own patch file, tools/data/patches-<game version>.txt in the merged
    worktree, goes first in builds\\patches.txt (0.2's fixes)."""

    _ok_build = CodePatchBuildTests._ok_build
    _build = CodePatchBuildTests._build

    def setUp(self):
        super().setUp()
        self.builtin = (self.settings.build_wt / "tools" / "data"
                        / f"patches-{builder.GAME_VERSION}.txt")
        self.builtin.parent.mkdir(parents=True)
        self.builtin.write_text(BUILTIN, encoding="utf-8")

    def test_builtin_file_goes_first(self):
        mod = _patch_mod(self.settings.mods, "speed", "set ev MLB_EV 1\n", 30)
        self._build()
        combined = (self.tmp / "patches.txt").read_text(encoding="utf-8")
        self.assertEqual(combined,
                         f"# --- built-in (tools/data/patches-{builder.GAME_VERSION}.txt)\n"
                         f"{BUILTIN}# --- speed: {mod}\nset ev MLB_EV 1\n")
        self.assertEqual(self.gen.calls[0][-2:], ["--patches", str(self.tmp / "patches.txt")])

    def test_builtin_alone_is_passed_to_gen(self):
        self._build()
        self.assertTrue((self.tmp / "patches.txt").read_text(encoding="utf-8").endswith(BUILTIN))
        self.assertEqual(self.gen.calls[0][-2:], ["--patches", str(self.tmp / "patches.txt")])

    def test_no_builtin_file_no_patches(self):
        self.builtin.unlink()
        self._build()
        self.assertNotIn("--patches", self.gen.calls[0])
        self.assertFalse((self.tmp / "patches.txt").exists())

    def test_mod_reusing_a_builtin_set_name_fails_naming_both(self):
        _patch_mod(self.settings.mods, "lights", "set walkup_light MLB_MY_LIGHT 1\n", 30)
        with mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build") as run_build:
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build([], self.settings, lambda line: None)
        self.assertIn("lights", str(ctx.exception))
        self.assertIn("built-in", str(ctx.exception))
        self.assertIn("walkup_light", str(ctx.exception))
        self.assertEqual(self.gen.calls, [])
        run_build.assert_not_called()

    def test_mod_reusing_a_builtin_switch_fails_naming_both(self):
        _patch_mod(self.settings.mods, "lights", "set mine MLB_WALKUP_LEADERS 1\n", 30)
        with mock.patch("app.builder._git", FakeGit()), mock.patch("app.builder._run_build"):
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build([], self.settings, lambda line: None)
        self.assertIn("lights", str(ctx.exception))
        self.assertIn("built-in", str(ctx.exception))

    def test_changed_builtin_file_regenerates(self):
        self._build()
        self.builtin.write_text(BUILTIN.replace("leaders MLB_WALKUP_LEADERS 1",
                                                "leaders MLB_WALKUP_LEADERS 0"), encoding="utf-8")
        self._build()
        self.assertEqual(len(self.gen.calls), 2)

    def test_state_and_build_info_list_builtin_sets_first(self):
        _patch_mod(self.settings.mods, "speed", "set ev MLB_EV 1\n", 30)
        want = [{"mod": "built-in", "name": "walkup_light", "env": "MLB_WALKUP_LIGHT", "default": 1},
                {"mod": "built-in", "name": "walkup_leaders", "env": "MLB_WALKUP_LEADERS",
                 "default": 1},
                {"mod": "speed", "name": "ev", "env": "MLB_EV", "default": 1}]
        self.assertEqual(builder.state(self.settings)["patch_sets"], want)
        self._build()
        info = json.loads((self.settings.build_out / "build-info.json").read_text())
        self.assertEqual(info["patch_sets"], want)

    def test_state_needed_patches_stays_mods_only(self):
        st = builder.state(self.settings)
        self.assertNotIn("patches", st["needed"])
        self.assertEqual([s["mod"] for s in st["patch_sets"]], ["built-in", "built-in"])

    def test_generator_error_in_builtin_part_names_it(self):
        self.gen.code = 1
        self.gen.output = f"{self.tmp / 'patches.txt'}:2: bad set line"
        with mock.patch("app.builder._git", FakeGit()), mock.patch("app.builder._run_build"):
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build([], self.settings, lambda line: None)
        self.assertIn("built-in", str(ctx.exception).splitlines()[0])


class SwitchOnlyBuildTests(_BuildCase):
    """A mod with runtime env but no branch is never merged and never breaks a build."""

    _ok_build = CodePatchBuildTests._ok_build

    def setUp(self):
        super().setUp()
        folder = self.settings.mods / "switchy"
        folder.mkdir()
        (folder / "mod.json").write_text(json.dumps(
            {"name": "Switchy", "order": 5,
             "runtime": {"env_on": {"MLB_OFFLINE_PACKAGES": "1"}, "env_off": {}}}),
            encoding="utf-8")

    def test_switch_only_mod_is_not_a_runtime_branch(self):
        self.assertEqual(builder._runtime_branches(self.settings), [])
        st = builder.state(self.settings)
        self.assertEqual(set(st["needed"]), {"main", "exefs"})

    def test_build_merges_no_branch_for_it(self):
        git = FakeGit(conflicts={"mod/pitch-types": ["runtime/x.c"]})
        with mock.patch("app.builder._git", git), \
                mock.patch("app.builder._run_build", side_effect=self._ok_build):
            builder.build(["mod/pitch-types"], self.settings, lambda line: None,
                          optional=frozenset({"mod/pitch-types"}))
        merges = [c[2] for c in git.calls if c[:2] == ["merge", "--no-edit"]]
        self.assertEqual(merges, ["mod/pitch-types"])
        info = json.loads((self.settings.build_out / "build-info.json").read_text())
        self.assertEqual(info["left_out"]["mod/pitch-types"]["mods"], [])


BRANCHES3 = BRANCHES + ["mod/pitch-speed"]
NEW3 = dict(NEW, **{"mod/pitch-speed": "9" * 40})


def _fake_heads(table):
    """Stands in for builder.heads: a branch missing from `table` is "missing" when
    missing_ok allows it, else BuildFailed as the real one."""
    def heads(branches, settings, missing_ok=()):
        out = {}
        for b in branches:
            if b in table:
                out[b] = table[b]
            elif b in missing_ok:
                out[b] = builder.MISSING
            else:
                raise builder.BuildFailed(f"branch {b!r} not found in {settings.recomp}")
        return out
    return heads


class LeaveOutTests(_BuildCase):
    """A switched-off mod (its branch in `optional`) whose branch will not merge is left out."""

    def setUp(self):
        super().setUp()
        self.table = dict(NEW3)
        p = mock.patch("app.builder.heads", side_effect=_fake_heads(self.table))
        p.start()
        self.addCleanup(p.stop)

    def _ok_build(self, argv, cwd, log):
        (self.settings.build_out / "mlb.exe").write_bytes(b"NEW EXE")
        return 0, "ok"

    def _runtime_mod(self, mod_id, branch, order, patches=None):
        folder = self.settings.mods / mod_id
        folder.mkdir(parents=True)
        manifest = {"name": mod_id.title(), "order": order,
                    "runtime": {"branch": branch, "env_on": {}, "env_off": {}}}
        if patches is not None:
            manifest["patches"] = "patches.txt"
            (folder / "patches.txt").write_text(patches, encoding="utf-8")
        (folder / "mod.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _mods(self):
        self._runtime_mod("bundle", "fakeserver/login-probe", 10)
        self._runtime_mod("pitch", "mod/pitch-types", 20, patches="set pt MLB_PT 1\n")
        self._runtime_mod("speed", "mod/pitch-speed", 30, patches="set ev MLB_EV 1\n")

    def _build(self, git, optional):
        logged = []
        with mock.patch("app.builder._git", git), \
                mock.patch("app.builder._run_build", side_effect=self._ok_build):
            builder.build(BRANCHES3, self.settings, logged.append, optional=optional)
        return logged

    def _info(self):
        return json.loads((self.settings.build_out / "build-info.json").read_text())

    def test_optional_branch_that_conflicts_is_left_out_and_the_build_goes_on(self):
        self._mods()
        git = FakeGit({"mod/pitch-types": ["runtime/mlb/boot.c"]})
        logged = self._build(git, frozenset({"mod/pitch-types"}))
        merges = [c[2] for c in git.calls if c[:2] == ["merge", "--no-edit"]]
        # required branches first, then the optional ones, each group in mod order
        self.assertEqual(merges, ["fakeserver/login-probe", "mod/pitch-speed", "mod/pitch-types"])
        self.assertIn(["merge", "--abort"], git.calls)
        self.assertFalse(any(c[0] == "checkout" for c in git.calls))
        self.assertTrue(any(line.startswith("Left out mod/pitch-types") and "runtime/mlb/boot.c" in line
                            and "the mod is off" in line for line in logged), logged)
        info = self._info()
        self.assertEqual(info["left_out"], {"mod/pitch-types": {
            "head": NEW3["mod/pitch-types"], "files": ["runtime/mlb/boot.c"], "mod": "pitch",
            "mods": ["pitch"], "reason": "conflicts"}})
        self.assertEqual(info["built_from"]["mod/pitch-types"], NEW3["mod/pitch-types"])

    def test_off_branch_merging_cleanly_does_not_block_a_later_on_branch(self):
        # pitch-types (off) and pitch-speed (on) conflict with each other: in mod order the
        # off one would merge first and the on one fail; required first leaves out the off one
        self._mods()
        git = FakeGit(clash={("mod/pitch-speed", "mod/pitch-types"): ["runtime/mlb/boot.c"],
                             ("mod/pitch-types", "mod/pitch-speed"): ["runtime/mlb/boot.c"]})
        self._build(git, frozenset({"mod/pitch-types"}))
        self.assertEqual(git.merged, ["fakeserver/login-probe", "mod/pitch-speed"])
        self.assertEqual(set(self._info()["left_out"]), {"mod/pitch-types"})

    def test_left_out_mods_patch_file_is_not_in_patches_txt(self):
        self._mods()
        self._build(FakeGit({"mod/pitch-types": ["runtime/mlb/boot.c"]}),
                    frozenset({"mod/pitch-types"}))
        combined = (self.tmp / "patches.txt").read_text(encoding="utf-8")
        self.assertIn("# --- speed:", combined)
        self.assertNotIn("# --- pitch:", combined)
        self.assertNotIn("MLB_PT", combined)

    def test_every_mod_on_a_left_out_branch_loses_its_patch_file(self):
        self._mods()
        self._runtime_mod("twin", "mod/pitch-types", 25, patches="set tw MLB_TW 1\n")
        self._build(FakeGit({"mod/pitch-types": ["runtime/mlb/boot.c"]}),
                    frozenset({"mod/pitch-types"}))
        combined = (self.tmp / "patches.txt").read_text(encoding="utf-8")
        self.assertNotIn("MLB_PT", combined)
        self.assertNotIn("MLB_TW", combined)
        self.assertIn("MLB_EV", combined)
        self.assertEqual(self._info()["left_out"]["mod/pitch-types"]["mods"], ["pitch", "twin"])

    def test_state_stays_up_to_date_after_a_build_with_a_left_out_branch(self):
        self._mods()
        self._build(FakeGit({"mod/pitch-types": ["runtime/mlb/boot.c"]}),
                    frozenset({"mod/pitch-types"}))
        st = builder.state(self.settings)
        self.assertTrue(st["up_to_date"], st)
        self.assertEqual(st["left_out"]["mod/pitch-types"]["files"], ["runtime/mlb/boot.c"])
        self.assertEqual(st["left_out"]["mod/pitch-types"]["mod"], "pitch")
        (self.settings.mods / "speed" / "patches.txt").write_text("set ev MLB_EV 0\n")
        self.assertFalse(builder.state(self.settings)["up_to_date"])

    def test_state_without_left_out_is_empty(self):
        self._mods()
        self._build(FakeGit(), frozenset())
        self.assertEqual(builder.state(self.settings)["left_out"], {})
        self.assertEqual(self._info()["left_out"], {})

    def test_required_branch_that_conflicts_still_fails_naming_the_mod(self):
        self._mods()
        git = FakeGit({"mod/pitch-types": ["runtime/mlb/boot.c"]})
        with mock.patch("app.builder._git", git), \
                mock.patch("app.builder._run_build") as run_build:
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build(BRANCHES3, self.settings, lambda line: None,
                              optional=frozenset({"mod/pitch-speed"}))
        self.assertIn("runtime/mlb/boot.c", str(ctx.exception))
        self.assertIn("'Pitch'", str(ctx.exception))
        run_build.assert_not_called()
        self.assertFalse((self.settings.build_out / "build-info.json").exists())

    def test_missing_optional_branch_is_left_out_as_not_found(self):
        self._mods()
        del self.table["mod/pitch-types"]
        git = FakeGit()
        logged = self._build(git, frozenset({"mod/pitch-types"}))
        self.assertNotIn(["merge", "--no-edit", "mod/pitch-types"], git.calls)
        self.assertTrue(any(line.startswith("Left out mod/pitch-types") and "not found" in line
                            for line in logged), logged)
        info = self._info()
        self.assertEqual(info["left_out"]["mod/pitch-types"],
                         {"head": "missing", "files": [], "mod": "pitch", "mods": ["pitch"],
                          "reason": "branch not found"})
        self.assertEqual(info["built_from"]["mod/pitch-types"], "missing")
        self.assertNotIn("MLB_PT", (self.tmp / "patches.txt").read_text(encoding="utf-8"))
        st = builder.state(self.settings)  # no raise for the missing branch
        self.assertTrue(st["up_to_date"], st)
        self.table["mod/pitch-types"] = NEW3["mod/pitch-types"]  # the branch appears
        self.assertFalse(builder.state(self.settings)["up_to_date"])

    def test_missing_required_branch_fails_naming_the_mod(self):
        self._mods()
        del self.table["mod/pitch-types"]
        git = FakeGit()
        with mock.patch("app.builder._git", git), \
                mock.patch("app.builder._run_build") as run_build:
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder.build(BRANCHES3, self.settings, lambda line: None, optional=frozenset())
        self.assertIn("mod/pitch-types", str(ctx.exception))
        self.assertIn("'Pitch'", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))
        self.assertEqual(git.calls, [])
        run_build.assert_not_called()

    def test_state_does_not_raise_for_a_missing_branch(self):
        self._mods()
        del self.table["mod/pitch-speed"]
        st = builder.state(self.settings)
        self.assertEqual(st["needed"]["mod/pitch-speed"], "missing")
        self.assertFalse(st["up_to_date"])

    def test_optional_branch_whose_merge_fails_without_conflicts_is_left_out(self):
        class Unmergeable(FakeGit):
            def __call__(self, args, cwd, log):
                if args == ["merge", "--no-edit", "mod/pitch-speed"]:
                    self.calls.append(list(args))
                    return 128, "merge: mod/pitch-speed - not something we can merge"
                return super().__call__(args, cwd, log)

        logged = []
        with mock.patch("app.builder._git", Unmergeable()):
            left = builder._merge(BRANCHES3, self.tmp, logged.append,
                                  optional=frozenset({"mod/pitch-speed"}))
        self.assertEqual(list(left), ["mod/pitch-speed"])
        self.assertEqual(left["mod/pitch-speed"]["files"], [])
        self.assertIn("not something we can merge", left["mod/pitch-speed"]["reason"])
        self.assertTrue(any(line.startswith("Left out mod/pitch-speed") for line in logged), logged)

    def test_failed_abort_falls_back_to_reset_hard_head(self):
        git = FakeGit({"mod/pitch-types": ["runtime/mlb/boot.c"]}, abort_fails=True)
        with mock.patch("app.builder._git", git):
            left = builder._merge(BRANCHES3, self.tmp, lambda line: None,
                                  optional=frozenset({"mod/pitch-types"}))
        self.assertEqual(list(left), ["mod/pitch-types"])
        self.assertIn(["reset", "--hard", "HEAD"], git.calls)
        self.assertIn(["rev-parse", "-q", "--verify", "MERGE_HEAD"], git.calls)
        self.assertIn(["merge", "--no-edit", "mod/pitch-speed"], git.calls)

    def test_worktree_that_stays_dirty_after_a_leave_out_fails(self):
        git = FakeGit({"mod/pitch-types": ["runtime/mlb/boot.c"]}, abort_fails=True, stuck=True)
        with mock.patch("app.builder._git", git):
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder._merge(BRANCHES3, self.tmp, lambda line: None,
                               optional=frozenset({"mod/pitch-types"}))
        self.assertIn("mod/pitch-types", str(ctx.exception))
        self.assertIn(["reset", "--hard", "HEAD"], git.calls)

    def test_leave_out_removes_untracked_path_tilde_branch_files(self):
        wt = self.tmp / "wt-tilde"
        (wt / "runtime" / "mlb").mkdir(parents=True)
        leftover = wt / "runtime" / "mlb" / "boot.c~mod_pitch-types"
        leftover.write_text("theirs")
        keep = wt / "notes.txt"
        keep.write_text("mine")
        git = FakeGit({"mod/pitch-types": ["runtime/mlb/boot.c"]},
                      untracked=["runtime/mlb/boot.c~mod_pitch-types", "notes.txt"])
        with mock.patch("app.builder._git", git):
            builder._merge(BRANCHES3, wt, lambda line: None,
                           optional=frozenset({"mod/pitch-types"}))
        self.assertFalse(leftover.exists())
        self.assertTrue(keep.exists())

    def test_test_driver_rule_applies_before_leaving_out(self):
        # an optional branch conflicting only in the test driver is resolved, not left out
        git = FakeGit({"mod/pitch-speed": ["runtime/mlb/hid.c"]})
        with mock.patch("app.builder._git", git):
            left = builder._merge(BRANCHES3, self.tmp, lambda line: None,
                                  optional=frozenset({"mod/pitch-speed"}))
        self.assertEqual(left, {})
        self.assertIn(["checkout", "mod/pitch-types", "--", "runtime/mlb/hid.c"], git.calls)

    def test_test_driver_rule_not_used_against_a_left_out_pitch_branch(self):
        # pitch-types itself is left out; a later branch's test-driver conflict must not
        # take a copy from it
        git = FakeGit({"mod/pitch-types": ["runtime/mlb/boot.c"],
                       "mod/pitch-speed": ["runtime/mlb/hid.c"]})
        with mock.patch("app.builder._git", git):
            left = builder._merge(BRANCHES3, self.tmp, lambda line: None,
                                  optional=frozenset({"mod/pitch-types", "mod/pitch-speed"}))
        self.assertEqual(set(left), {"mod/pitch-types", "mod/pitch-speed"})
        self.assertFalse(any(c[0] == "checkout" for c in git.calls))

    def test_required_branch_borrowing_from_off_pitch_branch_that_merges(self):
        # required first: the on network branch borrows the driver of the later, off
        # pitch-types, which then merges: nothing is left out
        git = FakeGit({"fakeserver/login-probe": ["runtime/mlb/hid.c"]})
        with mock.patch("app.builder._git", git):
            left = builder._merge(BRANCHES3, self.tmp, lambda line: None,
                                  optional=frozenset({"mod/pitch-types"}))
        self.assertEqual(left, {})
        self.assertIn(["checkout", "mod/pitch-types", "--", "runtime/mlb/hid.c"], git.calls)
        self.assertEqual(git.merged, ["fakeserver/login-probe", "mod/pitch-speed", "mod/pitch-types"])

    def test_required_test_driver_conflict_after_a_left_out_pitch_branch_fails(self):
        git = FakeGit({"mod/pitch-types": ["runtime/mlb/boot.c"],
                       "mod/pitch-speed": ["runtime/mlb/hid.c"]})
        with mock.patch("app.builder._git", git):
            with self.assertRaises(builder.BuildFailed) as ctx:
                builder._merge(BRANCHES3, self.tmp, lambda line: None,
                               optional=frozenset({"mod/pitch-types"}),
                               owners={"mod/pitch-speed": "Speed"})
        text = str(ctx.exception)
        self.assertIn("runtime/mlb/hid.c", text)
        self.assertIn("'Speed'", text)
        self.assertIn("it needs mod/pitch-types' test driver, which was left out", text)
        self.assertIn("runtime/mlb/boot.c", text)  # why pitch-types was left out
        resets = [i for i, c in enumerate(git.calls) if c == ["reset", "--hard", "main"]]
        self.assertEqual(len(resets), 2)  # borrowed first, then redone without
        self.assertFalse(any(c[0] == "checkout" for c in git.calls[resets[1]:]))

    def test_borrowed_test_driver_is_redone_when_the_pitch_branch_is_left_out(self):
        # the network branch, merged before pitch-types, took pitch's test driver; then
        # pitch-types (off) does not merge: the merge is redone without borrowing from it
        git = FakeGit({"fakeserver/login-probe": ["runtime/mlb/hid.c"],
                       "mod/pitch-types": ["runtime/mlb/boot.c"]})
        with mock.patch("app.builder._git", git):
            left = builder._merge(BRANCHES3, self.tmp, lambda line: None,
                                  optional=frozenset({"fakeserver/login-probe", "mod/pitch-types"}))
        self.assertEqual({b: v["files"] for b, v in left.items()},
                         {"fakeserver/login-probe": ["runtime/mlb/hid.c"],
                          "mod/pitch-types": ["runtime/mlb/boot.c"]})
        resets = [i for i, c in enumerate(git.calls) if c == ["reset", "--hard", "main"]]
        self.assertEqual(len(resets), 2)
        after = git.calls[resets[1]:]
        self.assertFalse(any(c[0] == "checkout" for c in after))
        self.assertIn(["merge", "--no-edit", "mod/pitch-speed"], after)

    def test_pending_required_pitch_branch_lends_its_test_driver(self):
        git = FakeGit({"fakeserver/login-probe": ["runtime/mlb/hid.c"]})
        with mock.patch("app.builder._git", git):
            left = builder._merge(BRANCHES3, self.tmp, lambda line: None,
                                  optional=frozenset({"fakeserver/login-probe"}))
        self.assertEqual(left, {})
        self.assertIn(["checkout", "mod/pitch-types", "--", "runtime/mlb/hid.c"], git.calls)


class UnsetFoldersTests(unittest.TestCase):
    """Settings with no recomp clone, worktree or setup (a player's): the state poll and a
    build answer with BuildFailed, never a TypeError."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        (tmp / "mods").mkdir()
        self.settings = dataclasses.replace(config.Settings(), mods=tmp / "mods",
                                            state=tmp / "state", build_out=tmp / "out",
                                            game=tmp / "game")
        self.assertIsNone(self.settings.build_wt)

    def test_no_worktree_has_no_builtin_patches(self):
        self.assertIsNone(builder.builtin_patches(self.settings))

    def test_state_fails_plainly(self):
        with mock.patch("app.builder.subprocess") as sp, mock.patch("app.builder._git") as git:
            with self.assertRaises(builder.BuildFailed) as caught:
                builder.state(self.settings)
        self.assertEqual(str(caught.exception), toolkit.SETUP_UNSET)
        self.assertIn("Setup folder", str(caught.exception))
        sp.run.assert_not_called()
        git.assert_not_called()

    def test_no_setup_chosen_is_a_plain_error(self):
        """A player with no Setup folder chosen: the state poll, Build and Play each say
        to choose it, in one plain sentence, and nothing crashes or runs."""
        coded = mods.Mod(id="coded", name="Coded", description="", folder=self.settings.mods,
                         art="", files=None, server=None, profile=None, order=10,
                         runtime={"branch": "mod/coded", "env_on": {}, "env_off": {}})
        self.assertEqual(config.mode(self.settings), "player")
        with mock.patch("app.procs.running_games", return_value=[]),                 mock.patch("app.mods.load_all", return_value=[coded]),                 mock.patch("app.applier.plan", return_value=[]),                 mock.patch("app.builder.subprocess") as sp,                 mock.patch("app.builder._git") as git,                 mock.patch("app.builder._run_gen") as gen,                 mock.patch("app.builder._run_build") as run_build,                 mock.patch("app.launcher._run_game") as run_game:
            with self.assertRaises(builder.BuildFailed) as from_state:
                builder.state(self.settings)
            with self.assertRaises(builder.BuildFailed) as from_build:
                builder.build(["mod/coded"], self.settings, lambda line: None)
            session = launcher.Session()
            launcher.play({"coded"}, self.settings, session)
        for message in (str(from_state.exception), str(from_build.exception), session.message):
            self.assertEqual(message, toolkit.SETUP_UNSET)
        self.assertEqual(session.status, "error")
        for called in (sp.run, sp.Popen, git, gen, run_build, run_game):
            called.assert_not_called()
        self.assertFalse(self.settings.build_out.exists())

    def test_heads_without_a_source_clone_is_plain(self):
        with mock.patch("app.builder.subprocess") as sp:
            with self.assertRaises(builder.BuildFailed) as caught:
                builder.heads(["main"], self.settings)
        self.assertIn("Recomp source", str(caught.exception))
        sp.run.assert_not_called()

    def test_build_refuses_before_touching_anything(self):
        with mock.patch("app.procs.running_games", return_value=[]), \
                mock.patch("app.builder._git") as git:
            with self.assertRaises(builder.BuildFailed) as caught:
                builder.build([], self.settings, lambda line: None)
        self.assertIn("Settings", str(caught.exception))
        git.assert_not_called()
        self.assertFalse(self.settings.build_out.exists())


# A stand-in for recomp/gen.py: which patches module it and a spawned worker import, and
# what is first on sys.path (the real one also puts its own folder first; this one does not,
# so only the manager's bootstrap can).
FAKE_GEN_PY = """import multiprocessing as mp
import sys

import patches


def who(_):
    return patches.WHO


if __name__ == "__main__":
    print("path0=" + sys.path[0])
    print("main=" + patches.WHO)
    with mp.get_context("spawn").Pool(1) as pool:
        print("worker=" + pool.map(who, [0])[0])
    print("args=" + " ".join(sys.argv[1:]))
"""

# A stand-in for the setup's tools/build.py, written the way Fastball 0.2.0's is where the
# runtime's compile flags come from (RT_FLAGS; main's rt_flags appends under `if mingw:`;
# main's inc_rt), with the MSVC branch and the C++ flags that must not be taken.
FAKE_BUILD_PY = """import os
from pathlib import Path

RT_FLAGS = ['-O2', '-g', '-Wall', '-std=gnu11', '-D_CRT_SECURE_NO_WARNINGS']
CXX_FLAGS = ['-O2', '-std=c++17']


def rel(p):
    return Path(os.path.relpath(Path(p).resolve(), '.')).as_posix()


def main():
    mingw = True
    gen, deps = Path('gen'), Path('deps')
    inc_rt = ['-I', 'runtime/mlb', '-I', 'runtime/core', '-I', rel(gen), '-I', rel(deps / 'include'),
              '-I', rel(deps / 'include' / 'vma'), '-I', 'runtime/third_party/imgui']
    rt_flags, cxx_flags = list(RT_FLAGS), list(CXX_FLAGS)
    if mingw:
        rt_flags.append('-D_WIN32_WINNT=0x0A00')
        cxx_flags.append('-D_WIN32_WINNT=0x0A00')
    else:
        rt_flags.append('-DMSVC_ONLY')
"""
FAKE_FLAGS = ["-O2", "-g", "-Wall", "-std=gnu11", "-D_CRT_SECURE_NO_WARNINGS", "-D_WIN32_WINNT=0x0A00",
              "-I", "runtime/mlb", "-I", "runtime/core", "-I", "../gen", "-I", "runtime/deps/include",
              "-I", "runtime/deps/include/vma", "-I", "runtime/third_party/imgui"]


class FakeTools:
    """Stands in for builder._run_tool: clang writes its -o object (b"OBJ " + the source), llvm-ar
    appends each object to the library; `fail` names the tool (clang or ar) that fails instead."""

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def __call__(self, argv, cwd, log):
        self.calls.append((list(argv), Path(cwd)))
        tool = "ar" if Path(argv[0]).name == "llvm-ar.exe" else "clang"
        if tool == self.fail:
            return 1, f"{tool}: error: it broke"
        if tool == "clang":
            source = Path(cwd) / argv[argv.index("-c") + 1]
            (Path(cwd) / argv[argv.index("-o") + 1]).write_bytes(b"OBJ " + source.read_bytes())
        else:
            lib = Path(cwd) / argv[2]
            lib.write_bytes(lib.read_bytes() + b"+" + (Path(cwd) / argv[3]).read_bytes())
        return 0, ""


class PlayerBuildTests(unittest.TestCase):
    """Player mode: no source clone, the Fastball setup plus the release's prebuilt runtime."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = dataclasses.replace(
            config.Settings(), state=self.tmp / "state", build_out=self.tmp / "out",
            setup=self.tmp / "setup", runtime_dir=self.tmp / "runtime", mods=self.tmp / "mods",
            game=self.tmp / "game")
        self.assertEqual(config.mode(self.settings), "player")
        make_setup(self.settings.setup)
        (self.settings.setup / "tools" / "build.py").write_text(FAKE_BUILD_PY, encoding="utf-8")
        make_install(self.settings.game)
        make_runtime(self.settings.runtime_dir, self.settings.setup)
        exefs = self.settings.game / "game" / "exefs"
        exefs.mkdir(parents=True)
        (exefs / "main").write_bytes(b"NSO0")
        (exefs / "sdk").write_bytes(b"NSO0sdk")
        self.settings.mods.mkdir()
        _patch_mod(self.settings.mods, "pitch-types", "set pitch MLB_PITCH 0\n", 10,
                   runtime={"branch": "mod/pitch-types", "env_on": {}, "env_off": {}})
        self.root = self.tmp / "builds" / "toolkit"
        self.gen = FakeGen()
        self.tools = FakeTools()
        self.builds = []
        for p in [mock.patch("app.procs.running_games", return_value=[]),
                  mock.patch("app.builder._run_gen", self.gen),
                  mock.patch("app.builder._run_tool", self.tools),
                  mock.patch("app.builder._run_build", side_effect=self._ok_build),
                  mock.patch("app.builder.BUILDS", self.tmp / "builds")]:
            p.start()
            self.addCleanup(p.stop)

    def _ok_build(self, argv, cwd, log):
        self.builds.append((list(argv), Path(cwd)))
        (self.settings.build_out / "mlb.exe").write_bytes(b"NEW EXE")
        return 0, "linked"

    def _build(self):
        return builder.build(["mod/pitch-types"], self.settings, lambda line: None,
                             optional=frozenset())

    def test_player_gen_command_puts_toolkit_recomp_first(self):
        root, out, patch_file = self.tmp / "tk", self.tmp / "gen.new", self.tmp / "patches.txt"
        argv = builder.gen_command(self.settings, out, patch_file, root)
        self.assertEqual(argv, [str(self.settings.setup / "python" / "python.exe"),
                                "-I", "-X", "utf8", "-c", procs.BOOT,
                                str(root / "recomp" / "gen.py"),
                                "--exefs", str(self.settings.game / "game" / "exefs"),
                                "--out", str(out), "--modules", "main,sdk",
                                "--patches", str(patch_file)])
        # run it: the toolkit's recomp/patches.py, not one elsewhere on sys.path, in the
        # generator and in its spawned workers
        (root / "recomp").mkdir(parents=True)
        (root / "recomp" / "gen.py").write_text(FAKE_GEN_PY, encoding="utf-8")
        (root / "recomp" / "patches.py").write_text("WHO = 'toolkit'\n", encoding="utf-8")
        pythons = [sys.executable]
        real = os.environ.get("FASTBALL_SETUP")  # a real setup: its Python's ._pth lists its recomp
        if real and (Path(real) / "python" / "python.exe").is_file():
            pythons.append(str(Path(real) / "python" / "python.exe"))
        for python in pythons:
            # -B: never a .pyc beside a real setup's modules
            proc = subprocess.run([python, "-B"] + argv[1:], cwd=root, capture_output=True,
                                  text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = proc.stdout.splitlines()
            self.assertIn("path0=" + str(root / "recomp"), lines)
            self.assertIn("main=toolkit", lines)
            self.assertIn("worker=toolkit", lines)
            self.assertIn("args=" + " ".join(argv[7:]), lines)

    def test_player_build_links_the_prebuilt_runtime(self):
        with mock.patch("app.procs.python_exe", return_value=r"C:\Py\python.exe"):
            exe = self._build()
        root, s = self.root, self.settings
        self.assertEqual(exe, s.build_out / "mlb.exe")
        self.assertEqual(exe.read_bytes(), b"NEW EXE")
        self.assertEqual(self.builds, [([r"C:\Py\python.exe", "-I", "tools/build.py",
                                         "--toolchain", str(s.setup / "llvm"),
                                         "--deps", "runtime/deps",
                                         "--ninja", str(s.setup / "bin/ninja.exe"),
                                         "--runtime-lib", "../runtime-lib/libmlbrt.a",
                                         "--gen", "../gen",
                                         "--version", "1.0.20", "--out", "../../out"],
                                        root)])
        # patches.c compiled against this build's own gen/patches.h, with the setup's
        # tools/build.py runtime flags, into a copy of the library; the toolkit's stays as laid
        llvm = s.setup / "llvm" / "bin"
        self.assertEqual(self.tools.calls, [
            ([str(llvm / "clang.exe"), *FAKE_FLAGS, "-c", "runtime/core/patches.c",
              "-o", "../runtime-lib/patches.o"], root),
            ([str(llvm / "llvm-ar.exe"), "rs", "../runtime-lib/libmlbrt.a", "../runtime-lib/patches.o"],
             root)])
        self.assertEqual((self.tmp / "builds" / "runtime-lib" / "libmlbrt.a").read_bytes(),
                         b"OUR LIB+OBJ // our patches.c\n")
        self.assertEqual((root / "runtime" / "libmlbrt.a").read_bytes(), b"OUR LIB")
        self.assertEqual((root / "runtime" / "deps" / "bin" / "SDL3.dll").read_bytes(), b"SDL")
        self.assertEqual((root / "recomp" / "patches.py").read_bytes(), b"WHO = 'toolkit'\n")
        # the generator: the toolkit's gen.py through the bootstrap, run from the toolkit
        self.assertEqual(self.gen.cwds, [root])
        self.assertEqual(self.gen.calls[0][4:7], ["-c", procs.BOOT, str(root / "recomp" / "gen.py")])
        # Fastball's own patch file (from the toolkit) first, then the mod's
        combined = (self.tmp / "builds" / "patches.txt").read_text(encoding="utf-8")
        self.assertLess(combined.index("set walkup_light MLB_WALKUP_LIGHT 1"),
                        combined.index("set pitch MLB_PITCH 0"))
        self.assertIn("built-in (tools/data/patches-1.0.20.txt)", combined.splitlines()[0])
        info = json.loads((s.build_out / "build-info.json").read_text(encoding="utf-8"))
        raw = (s.runtime_dir / "runtime.json").read_bytes()
        self.assertEqual(set(info["built_from"]), {"runtime", "exefs", "patches"})
        self.assertEqual(info["built_from"]["runtime"], sha(raw))
        self.assertEqual(info["built_from"]["exefs"], builder.exefs_stamp(s))
        self.assertEqual(info["left_out"], {})
        self.assertEqual(info["patch_sets"][0]["mod"], "built-in")
        st = builder.state(s)
        self.assertEqual(st["needed"], info["built_from"])
        self.assertTrue(st["up_to_date"])
        self.assertEqual(st["patch_sets"], info["patch_sets"])
        # nothing changed: a second build neither lays the toolkit nor translates again; the
        # library copy is made afresh from the toolkit's
        self._build()
        self.assertEqual(len(self.gen.calls), 1)
        self.assertEqual(len(self.builds), 2)
        self.assertEqual((self.tmp / "builds" / "runtime-lib" / "libmlbrt.a").read_bytes(),
                         b"OUR LIB+OBJ // our patches.c\n")
        # a new runtime makes the build out of date
        make_runtime(s.runtime_dir, s.setup, indent=1)
        self.assertFalse(builder.state(s)["up_to_date"])

    def test_runtime_flags_come_from_the_setups_build_py(self):
        build_py = self.tmp / "build.py"
        build_py.write_text(FAKE_BUILD_PY, encoding="utf-8")
        self.assertEqual(builder.runtime_flags(build_py, gen="../gen", deps="runtime/deps"), FAKE_FLAGS)

    def test_a_build_py_written_another_way_is_refused(self):
        """The flags are read from tools/build.py's source; when it no longer has them where they
        were (Fastball changed how it compiles the runtime), the build stops instead of guessing."""
        drifts = {
            "no RT_FLAGS": FAKE_BUILD_PY.replace("RT_FLAGS = [", "RUNTIME_FLAGS = ["),
            "computed RT_FLAGS": FAKE_BUILD_PY.replace("'-std=gnu11', ", "'-std=' + 'gnu11', "),
            "no inc_rt": FAKE_BUILD_PY.replace("inc_rt =", "includes ="),
            "an include it cannot read": FAKE_BUILD_PY.replace("rel(gen)", "rel(gen.parent)"),
            "an include from elsewhere": FAKE_BUILD_PY.replace("rel(gen)", "rel(other)"),
            "no mingw branch": FAKE_BUILD_PY.replace("if mingw:", "if True:"),
            "a computed define": FAKE_BUILD_PY.replace("rt_flags.append('-D_WIN32_WINNT=0x0A00')",
                                                       "rt_flags.append(WINNT)"),
            "not Python": "def main(:\n",
        }
        for what, text in drifts.items():
            with self.subTest(what):
                build_py = self.tmp / "build.py"
                build_py.write_text(text, encoding="utf-8")
                with self.assertRaises(builder.BuildFailed) as caught:
                    builder.runtime_flags(build_py, gen="../gen", deps="runtime/deps")
                self.assertIn(str(build_py), str(caught.exception))
        (self.settings.setup / "tools" / "build.py").write_text(drifts["no inc_rt"], encoding="utf-8")
        with self.assertRaises(builder.BuildFailed):
            self._build()
        self.assertEqual((self.tools.calls, self.builds), ([], []))

    def test_failed_patches_compile_or_archive_stops_before_the_link(self):
        for tool in ("clang", "ar"):
            with self.subTest(tool), mock.patch("app.builder._run_tool", FakeTools(fail=tool)):
                with self.assertRaises(builder.BuildFailed) as caught:
                    self._build()
                self.assertIn("runtime/core/patches.c", str(caught.exception))
                self.assertIn("it broke", str(caught.exception))
        self.assertEqual(self.builds, [])
        self.assertFalse((self.settings.build_out / "mlb.exe").exists())

    def test_runtime_without_patches_c_is_refused(self):
        listing = self.settings.runtime_dir / "runtime.json"
        doc = json.loads(listing.read_text(encoding="utf-8"))
        del doc["files"]["runtime/core/patches.c"]
        listing.write_text(json.dumps(doc), encoding="utf-8")
        (self.settings.runtime_dir / "runtime" / "core" / "patches.c").unlink()
        with self.assertRaises(builder.BuildFailed) as caught:
            self._build()
        self.assertIn("patches.c", str(caught.exception))
        self.assertIn("get the mods again", str(caught.exception))
        self.assertEqual((self.tools.calls, self.builds), ([], []))

    def test_player_build_needs_no_git(self):
        with mock.patch("app.builder._git") as git, \
                mock.patch("app.builder.heads") as heads, \
                mock.patch("app.builder._identity") as identity, \
                mock.patch("app.builder.subprocess") as sp:
            self._build()
            st = builder.state(self.settings)
        self.assertTrue(st["up_to_date"])
        for called in (git, heads, identity, sp.run, sp.Popen):
            called.assert_not_called()
        self.assertEqual(len(self.builds), 1)

    def test_player_build_refuses_another_fastball(self):
        make_install(self.settings.game, "0.2.1+1234567")
        with self.assertRaises(builder.BuildFailed) as caught:
            self._build()
        self.assertIn(f"need Fastball {FASTBALL}", str(caught.exception))
        self.assertIn("0.2.1+1234567", str(caught.exception))
        self.assertEqual((self.gen.calls, self.builds), ([], []))
        self.assertFalse(self.root.exists())
        self.assertFalse((self.settings.build_out / "mlb.exe").exists())
        self.assertIn("0.2.1+1234567", (self.settings.state / "build-error.txt").read_text(
            encoding="utf-8"))

    def test_manager_folder_with_a_space_and_setup_elsewhere_build(self):
        """A manager under "D:\\Games\\First Last\\..." and the setup in another folder tree
        (with a space too): tools/build.py sees gen, out, deps and the library only
        relative to the toolkit, the deps copied into it; only the toolchain and ninja,
        which it quotes or runs from a list, stay absolute."""
        with tempfile.TemporaryDirectory() as m, tempfile.TemporaryDirectory() as o:
            manager, setup = Path(m) / "a b" / "Show Mod Manager", Path(o) / "Fastball Setup"
            make_setup(setup)
            runtime_dir = manager / "runtime"
            make_runtime(runtime_dir, setup)
            self.settings = settings = dataclasses.replace(
                self.settings, setup=setup, runtime_dir=runtime_dir,
                build_out=manager / "builds" / "current", state=manager / "state")
            (setup / "tools" / "build.py").write_text(FAKE_BUILD_PY, encoding="utf-8")
            with mock.patch("app.builder.BUILDS", manager / "builds"), \
                    mock.patch("app.procs.python_exe", return_value=r"C:\Py\python.exe"):
                exe = builder.build([], settings, lambda line: None)
            self.assertEqual(exe.read_bytes(), b"NEW EXE")
            root = manager / "builds" / "toolkit"
            (argv, cwd), = self.builds
            self.assertEqual(cwd, root)
            args = dict(zip(argv[3::2], argv[4::2]))
            self.assertEqual(args, {"--toolchain": str(setup / "llvm"), "--deps": "runtime/deps",
                                    "--ninja": str(setup / "bin/ninja.exe"),
                                    "--runtime-lib": "../runtime-lib/libmlbrt.a", "--gen": "../gen",
                                    "--version": "1.0.20", "--out": "../current"})
            spaced = [a for a in argv[3:] if " " in a]
            self.assertEqual(spaced, [str(setup / "llvm"), str(setup / "bin/ninja.exe")])
            self.assertTrue((root / "runtime" / "deps" / "bin" / "SDL3.dll").is_file())
            self.assertEqual(len(self.gen.calls), 1)  # no "must not contain spaces" for gen

    def test_build_output_the_toolkit_cannot_reach_is_refused(self):
        """Only what tools/build.py cannot take: a build output on another drive than the
        toolkit, or reached through a folder name with a space."""
        real = os.path.relpath

        def other_drive(path, start=os.curdir):
            if Path(path) == self.settings.build_out:
                raise ValueError("path is on mount 'D:', start on mount 'C:'")
            return real(path, start)

        for settings, relpath, says in (
                (self.settings, other_drive, "on another drive"),
                (dataclasses.replace(self.settings, build_out=self.tmp / "my out"), real,
                 "with a space")):
            with mock.patch("app.builder.os.path.relpath", side_effect=relpath):
                with self.assertRaises(builder.BuildFailed) as caught:
                    builder.build([], settings, lambda line: None)
            self.assertIn(says, str(caught.exception))
            self.assertIn(f"The build output folder {settings.build_out}", str(caught.exception))
        self.assertEqual((self.gen.calls, self.builds), ([], []))
        self.assertFalse(self.root.exists())

    def test_player_state_without_a_runtime_is_out_of_date(self):
        shutil.rmtree(self.settings.runtime_dir)
        st = builder.state(self.settings)
        self.assertEqual(st["needed"]["runtime"], "missing")
        self.assertFalse(st["up_to_date"])
        with self.assertRaises(builder.BuildFailed) as caught:
            self._build()
        self.assertIn(str(self.settings.runtime_dir / "runtime.json"), str(caught.exception))

    def test_player_mode_builds_no_branches(self):
        coded = mods.Mod(id="coded", name="Coded", description="", folder=self.settings.mods,
                         art="", files=None, server=None, profile=None, order=10,
                         runtime={"branch": "mod/coded", "env_on": {}, "env_off": {}})
        self.assertEqual(launcher.build_branches({"coded"}, [coded], self.settings),
                         ([], frozenset()))
        dev = dataclasses.replace(self.settings, recomp=self.tmp / "recomp")
        (dev.recomp / ".git").mkdir(parents=True)
        self.assertEqual(launcher.build_branches({"coded"}, [coded], dev),
                         (["mod/coded"], frozenset()))


class DevBuildUnchangedTests(_BuildCase):
    def test_dev_build_unchanged(self):
        """Dev mode: the merged worktree's own gen.py and build.py, compiling the runtime."""
        self.assertEqual(config.mode(self.settings), "dev")
        builds = []

        def ok_build(argv, cwd, log):
            builds.append((list(argv), Path(cwd)))
            (self.settings.build_out / "mlb.exe").write_bytes(b"NEW EXE")
            return 0, ""

        git = FakeGit()
        with mock.patch("app.builder._git", git), \
                mock.patch("app.builder._run_build", side_effect=ok_build), \
                mock.patch("app.procs.python_exe", return_value=r"C:\Py\python.exe"):
            builder.build(BRANCHES, self.settings, lambda line: None)
            self.assertEqual(builds, [(builder.build_command(self.settings), self.settings.build_wt)])
        self.assertNotIn("--runtime-lib", builds[0][0])
        self.assertEqual(self.gen.cwds, [self.settings.build_wt])
        self.assertEqual(self.gen.calls[0][1:5], ["-I", "-X", "utf8", "recomp/gen.py"])
        self.assertIn(["merge", "--no-edit", "mod/pitch-types"], git.calls)
        info = json.loads((self.settings.build_out / "build-info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["built_from"], dict(NEW, exefs=builder.exefs_stamp(self.settings)))
        self.assertFalse((self.tmp / "toolkit").exists())


class GenFolderTests(unittest.TestCase):
    def test_gen_folder_is_manager_owned_whatever_build_out_is(self):
        s = dataclasses.replace(config.Settings(), build_out=Path(r"C:\Games\out"))
        self.assertEqual(builder.gen_dir(s), config.MANAGER / "builds" / "gen")
        self.assertEqual(builder.combined_patch_path(s), config.MANAGER / "builds" / "patches.txt")


class StageRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.game = self.tmp / "MLB The Show 26"
        self.out = self.tmp / "out"
        self.game.mkdir()
        self.out.mkdir()
        self.settings = dataclasses.replace(config.Settings(), game=self.game,
                                            build_out=self.out, state=self.tmp / "state")
        for name, data in [("totk_xlate.exe", b"XLATE"), ("totk_xlate.id", b"id1"),
                           ("nvngx_dlss.dll", b"DLSS"), ("mlb.cfg", b"MLB_DLSS=dlaa"),
                           ("pipeline-cache.bin", b"PIPE")]:
            (self.game / name).write_bytes(data)
        (self.game / "shader-cache" / "in").mkdir(parents=True)
        (self.game / "shader-cache" / "a.use").write_bytes(b"A")
        (self.game / "shader-cache" / "in" / "b.bin").write_bytes(b"B")

    def tearDown(self):
        self._tmp.cleanup()

    def _stage(self):
        logged = []
        builder.stage_runtime(self.settings, logged.append)
        return logged

    def test_copies_when_missing(self):
        self._stage()
        for name in ["totk_xlate.exe", "totk_xlate.id", "nvngx_dlss.dll", "mlb.cfg",
                     "pipeline-cache.bin"]:
            self.assertEqual((self.out / name).read_bytes(), (self.game / name).read_bytes())
        self.assertEqual((self.out / "shader-cache" / "in" / "b.bin").read_bytes(), b"B")
        self.assertEqual((self.out / "shader-cache" / "a.use").read_bytes(), b"A")

    def test_second_call_copies_nothing(self):
        self._stage()
        with mock.patch("app.builder.shutil.copy2") as copy2, \
                mock.patch("app.builder.shutil.copytree") as copytree:
            self._stage()
        copy2.assert_not_called()
        copytree.assert_not_called()

    def test_changed_translator_is_refreshed(self):
        self._stage()
        (self.game / "totk_xlate.exe").write_bytes(b"XLATE v2, longer")
        logged = self._stage()
        self.assertEqual((self.out / "totk_xlate.exe").read_bytes(), b"XLATE v2, longer")
        self.assertTrue(any("totk_xlate.exe" in line for line in logged))

    def test_existing_cfg_and_caches_are_not_overwritten(self):
        (self.out / "mlb.cfg").write_bytes(b"game wrote this")
        (self.out / "pipeline-cache.bin").write_bytes(b"own cache")
        (self.out / "shader-cache").mkdir()
        self._stage()
        self.assertEqual((self.out / "mlb.cfg").read_bytes(), b"game wrote this")
        self.assertEqual((self.out / "pipeline-cache.bin").read_bytes(), b"own cache")
        self.assertFalse((self.out / "shader-cache" / "a.use").exists())

    def test_missing_source_only_logs(self):
        (self.game / "totk_xlate.id").unlink()
        logged = self._stage()
        self.assertFalse((self.out / "totk_xlate.id").exists())
        self.assertTrue((self.out / "totk_xlate.exe").exists())
        self.assertEqual(sum("totk_xlate.id" in line for line in logged), 1)

    def test_every_dll_beside_the_install_exe_but_sdl3_is_staged(self):
        (self.game / "amd_fidelityfx_dx12.dll").write_bytes(b"FSR")
        (self.game / "SDL3.dll").write_bytes(b"install SDL")
        (self.out / "SDL3.dll").write_bytes(b"build.py SDL")
        self._stage()
        self.assertEqual((self.out / "amd_fidelityfx_dx12.dll").read_bytes(), b"FSR")
        self.assertEqual((self.out / "nvngx_dlss.dll").read_bytes(), b"DLSS")
        self.assertEqual((self.out / "SDL3.dll").read_bytes(), b"build.py SDL")

    def test_changed_dll_is_refreshed(self):
        (self.game / "amd_fidelityfx_vk.dll").write_bytes(b"FSR1")
        self._stage()
        (self.game / "amd_fidelityfx_vk.dll").write_bytes(b"FSR2 longer")
        self._stage()
        self.assertEqual((self.out / "amd_fidelityfx_vk.dll").read_bytes(), b"FSR2 longer")

    def _packages(self):
        src = self.game / "packages"
        (src / "python" / "Lib").mkdir(parents=True)
        (src / "server.py").write_bytes(b"server v1")
        (src / "core.py").write_bytes(b"core")
        (src / "python" / "python.exe").write_bytes(b"MZ")
        (src / "python" / "Lib" / "os.py").write_bytes(b"os")
        return src

    def test_packages_folder_is_copied(self):
        src = self._packages()
        self._stage()
        for rel in ("server.py", "core.py", "python/python.exe", "python/Lib/os.py"):
            self.assertEqual((self.out / "packages" / rel).read_bytes(), (src / rel).read_bytes())

    def test_packages_sync_updates_and_removes_only_inside_packages(self):
        src = self._packages()
        self._stage()
        (self.out / "beside.txt").write_bytes(b"not packages")
        (self.out / "packages-old").mkdir()
        (self.out / "packages-old" / "keep.py").write_bytes(b"keep")
        (src / "server.py").write_bytes(b"server v2, longer")
        (src / "core.py").unlink()
        (src / "python" / "Lib" / "os.py").unlink()
        (src / "handlers.py").write_bytes(b"new")
        logged = self._stage()
        pkg = self.out / "packages"
        self.assertEqual((pkg / "server.py").read_bytes(), b"server v2, longer")
        self.assertEqual((pkg / "handlers.py").read_bytes(), b"new")
        self.assertFalse((pkg / "core.py").exists())
        self.assertFalse((pkg / "python" / "Lib" / "os.py").exists())
        self.assertTrue((pkg / "python" / "python.exe").exists())
        self.assertEqual((self.out / "beside.txt").read_bytes(), b"not packages")
        self.assertEqual((self.out / "packages-old" / "keep.py").read_bytes(), b"keep")
        self.assertTrue(any("packages" in line for line in logged), logged)

    def test_unchanged_packages_copy_nothing(self):
        self._packages()
        self._stage()
        with mock.patch("app.builder.shutil.copy2") as copy2, \
                mock.patch("app.builder.shutil.copytree") as copytree:
            self._stage()
        copy2.assert_not_called()
        copytree.assert_not_called()

    def _outside(self):
        """A folder outside build_out\\packages with files the sync must never touch."""
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "server.py").write_bytes(b"outside")
        (outside / "precious.txt").write_bytes(b"precious")
        return outside

    def _assert_outside_intact(self, outside):
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["precious.txt", "server.py"])
        self.assertEqual((outside / "server.py").read_bytes(), b"outside")
        self.assertEqual((outside / "precious.txt").read_bytes(), b"precious")

    def _link(self, make):
        try:
            make()
        except OSError as err:
            self.skipTest(f"cannot make a link here: {err}")

    def test_junction_subfolder_in_packages_is_removed_not_followed(self):
        self._packages()
        outside = self._outside()
        (self.out / "packages").mkdir()
        self._link(lambda: _winapi.CreateJunction(str(outside), str(self.out / "packages" / "evil")))
        self._stage()
        self._assert_outside_intact(outside)
        self.assertFalse(os.path.lexists(self.out / "packages" / "evil"))

    def test_junction_where_the_source_has_a_folder_is_replaced(self):
        src = self._packages()
        (src / "python" / "server.py").write_bytes(b"inside python")
        outside = self._outside()
        (self.out / "packages").mkdir()
        self._link(lambda: _winapi.CreateJunction(str(outside), str(self.out / "packages" / "python")))
        self._stage()
        self._assert_outside_intact(outside)
        pkg_python = self.out / "packages" / "python"
        self.assertFalse(os.path.isjunction(pkg_python))
        self.assertEqual((pkg_python / "server.py").read_bytes(), b"inside python")
        self.assertEqual((pkg_python / "python.exe").read_bytes(), b"MZ")

    def test_file_symlink_in_packages_is_replaced_not_written_through(self):
        self._packages()
        outside = self._outside()
        (self.out / "packages").mkdir()
        self._link(lambda: os.symlink(outside / "server.py", self.out / "packages" / "server.py"))
        self._stage()
        self._assert_outside_intact(outside)
        self.assertFalse((self.out / "packages" / "server.py").is_symlink())
        self.assertEqual((self.out / "packages" / "server.py").read_bytes(), b"server v1")

    def test_directory_symlink_in_packages_is_removed_not_followed(self):
        self._packages()
        outside = self._outside()
        (self.out / "packages").mkdir()
        self._link(lambda: os.symlink(outside, self.out / "packages" / "python",
                                      target_is_directory=True))
        self._stage()
        self._assert_outside_intact(outside)
        self.assertFalse((self.out / "packages" / "python").is_symlink())
        self.assertEqual((self.out / "packages" / "python" / "python.exe").read_bytes(), b"MZ")

    def test_packages_folder_itself_a_junction_is_replaced(self):
        self._packages()
        outside = self._outside()
        self._link(lambda: _winapi.CreateJunction(str(outside), str(self.out / "packages")))
        self._stage()
        self._assert_outside_intact(outside)
        self.assertFalse(os.path.isjunction(self.out / "packages"))
        self.assertEqual((self.out / "packages" / "server.py").read_bytes(), b"server v1")

    def test_links_in_the_source_are_skipped_with_a_log_line(self):
        src = self._packages()
        outside = self._outside()
        self._link(lambda: _winapi.CreateJunction(str(outside), str(src / "linked")))
        logged = self._stage()
        self.assertFalse(os.path.lexists(self.out / "packages" / "linked"))
        self.assertTrue(any("linked" in line and "link" in line for line in logged), logged)
        self._assert_outside_intact(outside)

    def test_locked_packages_file_is_build_failed_naming_it(self):
        self._packages()
        self._stage()
        (self.game / "packages" / "server.py").write_bytes(b"server v2, longer")
        locked = PermissionError(13, "in use", str(self.out / "packages" / "server.py"))
        with mock.patch("app.builder.shutil.copy2", side_effect=locked):
            with self.assertRaises(builder.BuildFailed) as ctx:
                self._stage()
        self.assertIn("server.py", str(ctx.exception))
        self.assertIn("packages server", str(ctx.exception))

    def test_no_packages_in_install_logs_once_and_leaves_ours(self):
        (self.out / "packages").mkdir()
        (self.out / "packages" / "server.py").write_bytes(b"ours")
        logged = self._stage()
        self.assertEqual((self.out / "packages" / "server.py").read_bytes(), b"ours")
        self.assertEqual(sum("packages" in line for line in logged), 1, logged)

    def test_build_stages_runtime_after_success(self):
        self.out.joinpath("mlb.exe").write_bytes(b"x")
        (self.game / "game" / "exefs").mkdir(parents=True)
        (self.game / "game" / "exefs" / "main").write_bytes(b"NSO0")
        (self.game / "game" / "exefs" / "sdk").write_bytes(b"NSO0sdk")
        self.settings = dataclasses.replace(self.settings, mods=self.tmp / "mods",
                                            build_wt=self.tmp / "wt", recomp=self.tmp / "recomp",
                                            setup=self.tmp / "setup")
        (self.settings.recomp / ".git").mkdir(parents=True)  # a source clone: dev mode
        self.settings.mods.mkdir()
        with mock.patch("app.procs.running_games", return_value=[]), \
                mock.patch("app.builder._run_gen", FakeGen()), \
                mock.patch("app.builder.BUILDS", self.tmp), \
                mock.patch("app.builder.heads", side_effect=lambda b, s, missing_ok=(): {k: NEW[k] for k in b}), \
                mock.patch("app.builder._git", FakeGit()), \
                mock.patch("app.builder._run_build", return_value=(0, "")):
            builder.build(BRANCHES, self.settings, lambda line: None)
        self.assertTrue((self.out / "totk_xlate.exe").exists())


if __name__ == "__main__":
    unittest.main()
