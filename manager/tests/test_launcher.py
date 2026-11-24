import dataclasses
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import __main__ as app_main
from app import applier, builder, config, launcher, mods

FAKE_GAME = [sys.executable, "-c", "import time; time.sleep(1)"]
FAKE_SERVER = ["python", "-c",
               "print('listening on 127.0.0.1:10010', flush=True); import time; time.sleep(60)"]


class LaunchEnvTests(unittest.TestCase):
    def setUp(self):
        self.settings = config.load(app_main.SETTINGS)  # this manager's own settings
        if not Path(self.settings.mods).is_dir():
            self.skipTest("real mods not present")
        self.all_mods = mods.load_all(self.settings)  # the real manifests, read only
        self.profile = self.settings.game / "profile-rtts-bundle"

    def _mod(self, mod_id):
        mod = next((m for m in self.all_mods if m.id == mod_id), None)
        if mod is None or not mod.runtime:
            self.skipTest(f"{mod_id} has no runtime here")
        return mod

    def _expanded(self, mod, key):
        values = mods.expand(mod.runtime.get(key) or {}, mod, self.settings)
        return {k: launcher._norm(str(v)) for k, v in values.items()}

    def test_launch_env_has_exact_paths(self):
        quirks, pitch = self._mod("rtts-quirks"), self._mod("pitch-types")
        env = launcher.launch_env({"rtts-quirks"}, self.all_mods, self.settings, self.profile)
        self.assertEqual(env["MLB_ROMFS"], r"C:\Games\MLB The Show 26\game\romfs")
        self.assertEqual(env["MLB_EXEFS"], r"C:\Games\MLB The Show 26\game\exefs")
        self.assertEqual(env["MLB_PROFILE"], r"C:\Games\MLB The Show 26\profile-rtts-bundle")
        for key, value in self._expanded(pitch, "env_off").items():
            self.assertEqual(env[key], value, key)
        for key, value in self._expanded(quirks, "env_on").items():   # {mods} expanded to an exact path
            self.assertEqual(env[key], value, key)
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_pitch_on_quirks_off(self):
        quirks, pitch = self._mod("rtts-quirks"), self._mod("pitch-types")
        env = launcher.launch_env({"pitch-types"}, self.all_mods, self.settings,
                                  self.settings.game / "profile")
        for key, value in self._expanded(pitch, "env_on").items():
            self.assertEqual(env[key], value, key)
        for key, value in self._expanded(quirks, "env_off").items():
            self.assertEqual(env[key], value, key)

    def test_real_pitch_speed_switch_follows_selection(self):
        speed = next((m for m in self.all_mods if m.id == "pitch-speed"), None)
        if speed is None or speed.patches is None:
            self.skipTest("Real Pitch Speed has no code patches here")
        on = launcher.launch_env({"pitch-speed"}, self.all_mods, self.settings, self.profile)
        off = launcher.launch_env(set(), self.all_mods, self.settings, self.profile)
        self.assertEqual((on["MLB_PITCH_EV"], off["MLB_PITCH_EV"]), ("1", "0"))


class PatchSwitchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.settings = dataclasses.replace(config.Settings(), mods=root / "mods",
                                            build_out=root / "build")
        folder = root / "mods" / "patchy"
        folder.mkdir(parents=True)
        (folder / "patches.txt").write_text(
            "# two sets\nset fast MLB_FAST 1\nset slow MLB_SLOW 0\npage fast 40 00\n", encoding="utf-8")
        self.patchy = mods.Mod(id="patchy", name="Patchy", description="", folder=folder, art="",
                               files=None, runtime=None, server=None, profile=None, order=10,
                               patches=folder / "patches.txt")
        self.plain = mods.Mod(id="plain", name="Plain", description="", folder=root / "mods" / "plain",
                              art="", files={"cwd": "x"}, runtime=None, server=None, profile=None,
                              order=20)

    def _env(self, selected, all_mods=None):
        with mock.patch.dict(os.environ, {"MLB_FAST": "stale", "MLB_SLOW": "stale"}):
            return launcher.launch_env(selected, all_mods or [self.patchy, self.plain],
                                       self.settings, Path("P"))

    def test_switches_follow_selection(self):
        on, off = self._env({"patchy"}), self._env(set())
        self.assertEqual((on["MLB_FAST"], on["MLB_SLOW"]), ("1", "1"))
        self.assertEqual((off["MLB_FAST"], off["MLB_SLOW"]), ("0", "0"))

    def test_manifest_env_wins(self):
        self.patchy.runtime = {"branch": "b", "env_on": {"MLB_SLOW": "0"},
                               "env_off": {"MLB_FAST": "1"}}
        on, off = self._env({"patchy"}), self._env(set())
        self.assertEqual((on["MLB_FAST"], on["MLB_SLOW"]), ("1", "0"))
        self.assertEqual((off["MLB_FAST"], off["MLB_SLOW"]), ("1", "0"))

    def test_patch_only_mod_uses_combined_exe(self):
        s = self.settings
        all_mods = [self.patchy, self.plain]
        self.assertTrue(launcher.uses_combined_exe({"patchy"}, all_mods, s))
        self.assertFalse(launcher.uses_combined_exe({"plain"}, all_mods, s))
        self.assertFalse(launcher.uses_combined_exe(set(), all_mods, s))
        self.assertTrue(launcher.uses_combined_exe(
            set(), all_mods, dataclasses.replace(s, always_combined_exe=True)))
        self.assertEqual(launcher.exe_for({"patchy"}, all_mods, s), s.build_out / "mlb.exe")
        self.assertEqual(launcher.exe_for({"plain"}, all_mods, s), s.game / "mlb.exe")


class SwitchOnlyModTests(unittest.TestCase):
    """A mod whose runtime has env_on / env_off but no branch: it switches code the
    port has built in (0.2's offline Packages), nothing to merge."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.settings = dataclasses.replace(config.Settings(), mods=root / "mods",
                                            build_out=root / "build", build_wt=root / "wt")
        self.switchy = mods.Mod(id="switchy", name="Switchy", description="",
                                folder=root / "mods" / "switchy", art="", files=None,
                                runtime={"env_on": {"MLB_OFFLINE_PACKAGES": "1"},
                                         "env_off": {"MLB_OFFLINE_PACKAGES": "0"}},
                                server=None, profile=None, order=10)
        self.coded = mods.Mod(id="coded", name="Coded", description="",
                              folder=root / "mods" / "coded", art="", files=None,
                              runtime={"branch": "mod/coded", "env_on": {}, "env_off": {}},
                              server=None, profile=None, order=20)

    def test_switch_only_mod_does_not_need_the_combined_exe(self):
        all_mods = [self.switchy, self.coded]
        self.assertFalse(launcher.uses_combined_exe({"switchy"}, all_mods, self.settings))
        self.assertEqual(launcher.exe_for({"switchy"}, all_mods, self.settings),
                         self.settings.game / "mlb.exe")
        self.assertTrue(launcher.uses_combined_exe({"switchy", "coded"}, all_mods, self.settings))

    def test_switch_only_mod_with_patches_needs_the_combined_exe(self):
        self.switchy.patches = Path("p.txt")
        self.assertTrue(launcher.uses_combined_exe({"switchy"}, [self.switchy], self.settings))

    def test_switch_only_mod_env_is_applied(self):
        all_mods = [self.switchy, self.coded]
        on = launcher.launch_env({"switchy"}, all_mods, self.settings, Path("P"))
        off = launcher.launch_env(set(), all_mods, self.settings, Path("P"))
        self.assertEqual((on["MLB_OFFLINE_PACKAGES"], off["MLB_OFFLINE_PACKAGES"]), ("1", "0"))

    def test_switch_only_mod_has_no_branch_to_build(self):
        self.assertEqual(launcher.build_branches({"switchy"}, [self.switchy, self.coded]),
                         (["mod/coded"], frozenset({"mod/coded"})))
        self.assertEqual(launcher.build_branches({"switchy", "coded"}, [self.switchy, self.coded]),
                         (["mod/coded"], frozenset()))
        st = {"up_to_date": True, "left_out": {"mod/coded": {}}}
        self.assertFalse(launcher.build_needed(st, {"switchy"}, [self.switchy, self.coded]))

    def test_built_in_patch_sets_get_no_manager_env(self):
        data = self.settings.build_wt / "tools" / "data"
        data.mkdir(parents=True)
        (data / f"patches-{builder.GAME_VERSION}.txt").write_text(
            "set walkup_light MLB_WALKUP_LIGHT 1\nset walkup_leaders MLB_WALKUP_LEADERS 1\n",
            encoding="utf-8")
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("MLB_WALKUP_LIGHT", "MLB_WALKUP_LEADERS")}
        with mock.patch.dict(os.environ, clean, clear=True):
            for selected in (set(), {"switchy"}):
                env = launcher.launch_env(selected, [self.switchy], self.settings, Path("P"))
                self.assertNotIn("MLB_WALKUP_LIGHT", env)
                self.assertNotIn("MLB_WALKUP_LEADERS", env)
            # a mod's own env naming the switch is the one way to set it
            self.switchy.runtime["env_on"]["MLB_WALKUP_LIGHT"] = "0"
            env = launcher.launch_env({"switchy"}, [self.switchy], self.settings, Path("P"))
            self.assertEqual(env["MLB_WALKUP_LIGHT"], "0")
            self.assertNotIn("MLB_WALKUP_LEADERS", env)


class PlayTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.game = root / "MLB The Show 26"
        self.game.mkdir()
        self.settings = dataclasses.replace(
            config.Settings(), game=self.game, mods=root / "mods", state=root / "state",
            build_out=root / "build", recomp=root / "recomp")
        (self.settings.recomp / ".git").mkdir(parents=True)  # a source clone: dev mode
        self.settings.build_out.mkdir()
        self.bundle = mods.Mod(
            id="rtts-bundle", name="Bundle", description="", folder=root / "mods" / "rtts-bundle",
            art="", files={"cwd": "{mod}"}, profile="own", order=10,
            runtime={"branch": "fakeserver/login-probe", "env_on": {"MLB_NETPROBE": "1"},
                     "env_off": {}},
            server={"cwd": "{state}", "cmd": FAKE_SERVER, "ready": "listening on 127.0.0.1"})
        self.pitch = mods.Mod(
            id="pitch-types", name="Pitch", description="", folder=root / "mods" / "pitch-types",
            art="", files=None, server=None, profile=None, order=20,
            runtime={"branch": "mod/pitch-types", "env_on": {"MLB_PITCHMOD": "on"},
                     "env_off": {"MLB_PITCHMOD": "off"}})
        self.session = launcher.Session()
        self.calls = []
        self.optional = []
        self.servers = []
        real_spawn = launcher._spawn_server

        def spawn(argv, cwd):
            proc = real_spawn(argv, cwd)
            self.servers.append(proc)
            return proc

        patches = [
            mock.patch("app.mods.load_all", return_value=[self.bundle, self.pitch]),
            mock.patch("app.procs.running_games", return_value=[]),
            mock.patch("app.procs.port_owner", return_value=None),
            mock.patch("app.applier.plan", return_value=[("install", "rtts-bundle")]),
            mock.patch("app.applier.apply", side_effect=lambda *a: self.calls.append("apply")),
            mock.patch("app.builder.state", return_value={"up_to_date": True}),
            mock.patch("app.builder.build", side_effect=self._build),
            mock.patch("app.builder.stage_runtime", side_effect=lambda *a: self.calls.append("stage")),
            mock.patch("app.profiles.for_selection", return_value=self.game / "profile"),
            mock.patch("app.profiles.ensure", side_effect=lambda *a: self.calls.append("profile")),
            mock.patch("app.launcher._game_argv", side_effect=self._game_argv),
            mock.patch("app.launcher._spawn_server", side_effect=spawn),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._kill_servers)
        self.addCleanup(self._tmp.cleanup)

    def _build(self, branches, settings, log, optional=frozenset()):
        self.calls.append(("build", branches))
        self.optional.append(optional)

    def _game_argv(self, exe):
        self.calls.append(("launch", Path(exe)))
        return FAKE_GAME

    def _kill_servers(self):
        for proc in self.servers:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def _launched(self):
        return [c for c in self.calls if isinstance(c, tuple) and c[0] == "launch"]

    def test_play_refuses_while_game_runs(self):
        exe = str(self.game / "mlb.exe")  # the install's own game
        with mock.patch("app.procs.running_games", return_value=[{"pid": 777, "exe": exe}]):
            launcher.play({"rtts-bundle"}, self.settings, self.session)
        self.assertEqual(self.session.status, "error")
        self.assertIn("777", self.session.message)
        self.assertIn(exe, self.session.message)
        applier.plan.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.servers, [])

    TEST_COPY = r"D:\Tests\scratchpad\perktest\mlb.exe"

    def _play_beside(self, games, steps, selected=("pitch-types",)):
        """Play with `games` running (a list, or a list of lists for successive
        queries) and `steps` planned; returns the plan mock."""
        queries = games if games and isinstance(games[0], list) else None
        running = (mock.patch("app.procs.running_games", side_effect=queries) if queries else
                   mock.patch("app.procs.running_games", return_value=games))
        with running, mock.patch("app.applier.plan", return_value=steps) as plan:
            launcher.play(set(selected), self.settings, self.session)
        return plan

    def _assert_refused_early(self, plan, pid):
        self.assertEqual(self.session.status, "error")
        self.assertIn(str(pid), self.session.message)
        plan.assert_not_called()
        self.assertNotIn("apply", self.calls)
        self.assertEqual(self.servers, [])
        self.assertEqual(self._launched(), [])

    def test_play_starts_beside_one_test_copy_when_no_files_change(self):
        self._play_beside([{"pid": 555, "exe": self.TEST_COPY}], [])
        self.assertEqual(self.session.status, "idle", self.session.message)
        self.assertEqual(len(self._launched()), 1)
        self.assertIn("profile", self.calls)

    def test_play_beside_test_copy_refuses_when_files_change(self):
        self._play_beside([{"pid": 555, "exe": self.TEST_COPY}], [("install", "rtts-bundle")])
        self.assertEqual(self.session.status, "error")
        self.assertIn("555", self.session.message)
        self.assertIn(self.TEST_COPY, self.session.message)
        self.assertIn("mod files", self.session.message)
        self.assertNotIn("apply", self.calls)
        self.assertEqual(self._launched(), [])
        self.assertEqual(self.servers, [])

    def test_play_refuses_beside_own_install_exe(self):
        exe = str(self.game / "mlb.exe").upper()  # case differs, same file
        plan = self._play_beside([{"pid": 556, "exe": exe}], [], selected=("rtts-bundle",))
        self._assert_refused_early(plan, 556)

    def test_play_refuses_beside_combined_exe(self):
        exe = str(self.settings.build_out).replace("\\", "/") + "/./mlb.exe"  # same file
        plan = self._play_beside([{"pid": 557, "exe": exe}], [], selected=("rtts-bundle",))
        self._assert_refused_early(plan, 557)

    def _copies(self, n, first_pid=570):
        return [{"pid": first_pid + i, "exe": rf"D:\Tests\copy{i}\mlb.exe"} for i in range(n)]

    def test_play_starts_beside_three_test_copies_when_no_files_change(self):
        self._play_beside(self._copies(3), [])
        self.assertEqual(self.session.status, "idle", self.session.message)
        self.assertEqual(len(self._launched()), 1)
        self.assertIn("playing beside 3 test copies", "\n".join(self.session.log))

    def test_play_beside_test_copies_refuses_when_files_change(self):
        self._play_beside(self._copies(2), [("install", "rtts-bundle")])
        self.assertEqual(self.session.status, "error")
        self.assertIn("2 test copies", self.session.message)
        self.assertIn("close them first", self.session.message)
        self.assertEqual(self._launched(), [])

    def test_play_refuses_beside_four_copies(self):
        plan = self._play_beside(self._copies(4), [], selected=("rtts-bundle",))
        self._assert_refused_early(plan, 573)
        self.assertIn("4 copies of the game are running", self.session.message)
        self.assertIn("close all but 3", self.session.message)

    def test_play_refuses_when_one_of_several_copies_is_the_own_game(self):
        games = self._copies(1) + [{"pid": 580, "exe": str(self.game / "mlb.exe")}]
        plan = self._play_beside(games, [], selected=("rtts-bundle",))
        self._assert_refused_early(plan, 580)

    def test_play_refuses_beside_copy_with_unknown_path(self):
        plan = self._play_beside([{"pid": 559, "exe": ""}], [], selected=("rtts-bundle",))
        self._assert_refused_early(plan, 559)

    def test_play_rechecks_games_just_before_launch(self):
        first = [{"pid": 555, "exe": self.TEST_COPY}]
        later = first + self._copies(2) + [{"pid": 560, "exe": r"D:\Tests\other\mlb.exe"}]  # 4: one too many
        self._play_beside([first, later], [], selected=("rtts-bundle",))
        self.assertEqual(self.session.status, "error")
        self.assertIn("560", self.session.message)
        self.assertEqual(self._launched(), [])
        self.assertEqual(len(self.servers), 1)  # the bundle's server was started ...
        self.assertIsNotNone(self.servers[0].poll())  # ... and stopped again

    def test_play_rechecks_and_refuses_own_game_started_meanwhile(self):
        own = [{"pid": 561, "exe": str(self.game / "mlb.exe")}]
        self._play_beside([[], own], [], selected=("pitch-types",))
        self.assertEqual(self.session.status, "error")
        self.assertIn("561", self.session.message)
        self.assertEqual(self._launched(), [])

    def test_same_file_through_other_spellings(self):
        exe = self.game / "mlb.exe"
        exe.write_bytes(b"MZ")
        self.assertTrue(launcher._same_file(str(exe) + ".", exe))  # trailing dot
        self.assertTrue(launcher._same_file(str(exe).upper(), exe))
        self.assertTrue(launcher._same_file(str(self.game / "x" / ".." / "mlb.exe"), exe))
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(exe), buf, 1024) and buf.value != str(exe):
            self.assertTrue(launcher._same_file(buf.value, exe))  # 8.3 short name
        other = self.game / "other.exe"
        other.write_bytes(b"MZ")
        self.assertFalse(launcher._same_file(other, exe))
        self.assertFalse(launcher._same_file(r"C:\nowhere\mlb.exe", exe))
        self.assertTrue(launcher._same_file(r"C:\Nowhere\.\MLB.exe", r"c:\nowhere\mlb.exe"))

    def test_play_beside_real_install_exe_through_trailing_dot_refuses(self):
        exe = self.game / "mlb.exe"
        exe.write_bytes(b"MZ")
        plan = self._play_beside([{"pid": 562, "exe": str(exe) + "."}], [])
        self._assert_refused_early(plan, 562)

    def test_play_gate(self):
        own = str(self.game / "mlb.exe")
        test = {"pid": 1, "exe": self.TEST_COPY}
        self.assertEqual(launcher.play_gate([], self.settings), (None, []))
        self.assertEqual(launcher.play_gate([test], self.settings), (None, [test]))
        block, beside = launcher.play_gate([{"pid": 2, "exe": own}], self.settings)
        self.assertIn("PID 2", block)
        self.assertEqual(beside, [])
        two = [test, {"pid": 3, "exe": self.TEST_COPY}]
        self.assertEqual(launcher.play_gate(two, self.settings), (None, two))
        unknown = [test, {"pid": 3, "exe": self.TEST_COPY}, {"pid": 4, "exe": ""}]
        block, _ = launcher.play_gate(unknown, self.settings)
        self.assertIn("PID 4", block)
        four = unknown[:2] + [{"pid": 4, "exe": self.TEST_COPY}, {"pid": 5, "exe": self.TEST_COPY}]
        block, _ = launcher.play_gate(four, self.settings)
        self.assertIn("4 copies of the game are running", block)
        self.assertEqual(launcher.play_note([test]),
                         f"A test copy (PID 1 ({self.TEST_COPY})) is running; Play works if no mod files need changing.")
        self.assertTrue(launcher.play_note(two).startswith("2 test copies (PID 1"))
        self.assertIsNone(launcher.play_note([]))

    def test_on_applied_runs_right_after_apply(self):
        launcher.play({"pitch-types"}, self.settings, self.session,
                      on_applied=lambda: self.calls.append("applied"))
        self.assertEqual(self.session.status, "idle", self.session.message)
        self.assertEqual(self.calls.index("applied"), self.calls.index("apply") + 1)
        self.assertEqual(len(self._launched()), 1)

    def test_on_applied_runs_when_apply_fails(self):
        applied = []
        with mock.patch("app.applier.apply", side_effect=applier.StepFailed("rtts-bundle", "x")):
            launcher.play({"rtts-bundle"}, self.settings, self.session,
                          on_applied=lambda: applied.append(1))
        self.assertEqual(self.session.status, "error")
        self.assertEqual(applied, [1])

    def test_start_passes_on_applied(self):
        applied = []
        thread = launcher.start({"pitch-types"}, self.settings, self.session,
                                on_applied=lambda: applied.append(1))
        thread.join(30)
        self.assertEqual(applied, [1])

    def test_play_stops_on_build_failure(self):
        with mock.patch("app.builder.state", return_value={"up_to_date": False}), \
                mock.patch("app.builder.build",
                           side_effect=builder.BuildFailed("merging x conflicts in: a.c")):
            launcher.play({"pitch-types"}, self.settings, self.session)
        self.assertEqual(self.session.status, "error")
        self.assertIn("conflicts", self.session.message)
        self.assertEqual(self._launched(), [])
        self.assertNotIn("stage", self.calls)
        self.assertEqual(self.servers, [])

    def test_play_stops_when_server_port_busy(self):
        with mock.patch("app.procs.port_owner", return_value=4242):
            launcher.play({"rtts-bundle"}, self.settings, self.session)
        self.assertEqual(self.session.status, "error")
        self.assertIn("10010", self.session.message)
        self.assertIn("4242", self.session.message)
        self.assertEqual(self._launched(), [])
        self.assertEqual(self.servers, [])

    def test_play_stops_when_server_never_ready(self):
        self.bundle.server = dict(self.bundle.server, cmd=["python", "-c", "print('nope')"])
        launcher.play({"rtts-bundle"}, self.settings, self.session)
        self.assertEqual(self.session.status, "error")
        self.assertEqual(self._launched(), [])

    def test_server_python_is_the_console_python(self):
        with mock.patch("app.procs.python_exe", return_value=sys.executable) as exe:
            launcher.play({"rtts-bundle"}, self.settings, self.session)
        self.assertEqual(self.session.status, "idle", self.session.message)
        exe.assert_called()
        self.assertEqual(self.servers[0].args[0], sys.executable)

    def test_servers_stopped_after_game_exits(self):
        launcher.play({"rtts-bundle"}, self.settings, self.session)
        self.assertEqual(self.session.status, "idle", self.session.message)
        self.assertEqual(len(self.servers), 1)
        self.assertIsNotNone(self.servers[0].poll())
        self.assertEqual(self._launched(), [("launch", self.settings.build_out / "mlb.exe")])
        self.assertTrue(any(line.startswith("[server] listening") for line in self.session.log))
        self.assertTrue((self.settings.state / "logs" / "manager.log").is_file())
        self.assertTrue((self.settings.state / "profiles" / "rtts-bundle").is_dir())
        self.assertLess(self.calls.index("stage"), self.calls.index(self._launched()[0]))

    def test_build_uses_every_runtime_branch(self):
        with mock.patch("app.builder.state", return_value={"up_to_date": False}):
            launcher.play({"pitch-types"}, self.settings, self.session)
        self.assertEqual(self.session.status, "idle", self.session.message)
        self.assertIn(("build", ["fakeserver/login-probe", "mod/pitch-types"]), self.calls)

    def test_build_passes_the_off_mods_branches_as_optional(self):
        with mock.patch("app.builder.state", return_value={"up_to_date": False}):
            launcher.play({"pitch-types"}, self.settings, self.session)
        self.assertEqual(self.optional, [frozenset({"fakeserver/login-probe"})])

    def test_build_branches_helper(self):
        self.assertEqual(launcher.build_branches({"pitch-types"}, [self.bundle, self.pitch]),
                         (["fakeserver/login-probe", "mod/pitch-types"],
                          frozenset({"fakeserver/login-probe"})))
        twin = mods.Mod(id="twin", name="Twin", description="", folder=self.pitch.folder,
                        art="", files=None, server=None, profile=None, order=30,
                        runtime={"branch": "mod/pitch-types"})
        # a branch shared with a selected mod is required; each branch listed once
        self.assertEqual(launcher.build_branches({"twin"}, [self.bundle, self.pitch, twin]),
                         (["fakeserver/login-probe", "mod/pitch-types"],
                          frozenset({"fakeserver/login-probe"})))

    def test_selected_left_out_mod_rebuilds_and_stops_naming_mod_and_files(self):
        left = {"mod/pitch-types": {"head": "c" * 40, "files": ["runtime/mlb/boot.c"],
                                    "mod": "pitch-types"}}
        failure = builder.BuildFailed(
            "merging mod/pitch-types (mod 'Pitch') conflicts in: runtime/mlb/boot.c")
        with mock.patch("app.builder.state", return_value={"up_to_date": True, "left_out": left}), \
                mock.patch("app.builder.build", side_effect=failure) as build:
            launcher.play({"pitch-types"}, self.settings, self.session)
        build.assert_called_once()
        self.assertEqual(build.call_args.kwargs["optional"], frozenset({"fakeserver/login-probe"}))
        self.assertEqual(self.session.status, "error")
        self.assertIn("'Pitch'", self.session.message)
        self.assertIn("runtime/mlb/boot.c", self.session.message)
        self.assertEqual(self._launched(), [])
        self.assertEqual(self.servers, [])

    def test_unselected_left_out_mod_does_not_rebuild(self):
        left = {"mod/pitch-types": {"head": "c" * 40, "files": ["runtime/mlb/boot.c"],
                                    "mod": "pitch-types"}}
        with mock.patch("app.builder.state", return_value={"up_to_date": True, "left_out": left}):
            launcher.play({"rtts-bundle"}, self.settings, self.session)
        self.assertEqual(self.session.status, "idle", self.session.message)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "build" for c in self.calls))
        self.assertEqual(self._launched(), [("launch", self.settings.build_out / "mlb.exe")])

    def test_no_runtime_mod_uses_installed_exe(self):
        self.bundle.runtime = None
        self.bundle.server = None
        launcher.play({"rtts-bundle"}, self.settings, self.session)
        self.assertEqual(self.session.status, "idle", self.session.message)
        self.assertEqual(self._launched(), [("launch", self.game / "mlb.exe")])
        self.assertNotIn("stage", self.calls)
        builder.state.assert_not_called()

    def test_selected_patch_only_mod_plays_the_combined_exe(self):
        self.bundle.runtime = None
        self.bundle.server = None
        self.bundle.patches = Path(self._tmp.name) / "patches.txt"
        self.bundle.patches.write_text("set a MLB_A 1\n", encoding="utf-8")
        with mock.patch("app.builder.state", return_value={"up_to_date": False}):
            launcher.play({"rtts-bundle"}, self.settings, self.session)
        self.assertEqual(self.session.status, "idle", self.session.message)
        self.assertIn(("build", ["mod/pitch-types"]), self.calls)
        self.assertEqual(self._launched(), [("launch", self.settings.build_out / "mlb.exe")])

    def test_game_launch_failure_stops_servers(self):
        with mock.patch("app.launcher._game_argv", return_value=[str(self.game / "missing.exe")]):
            launcher.play({"rtts-bundle"}, self.settings, self.session)
        self.assertEqual(self.session.status, "error")
        self.assertEqual(len(self.servers), 1)
        self.assertIsNotNone(self.servers[0].poll())

    def test_start_refuses_when_busy(self):
        self.session.set("running", "playing")
        self.assertIsNone(launcher.start({"rtts-bundle"}, self.settings, self.session))
        self.assertEqual(self.session.status, "running")

    def test_start_runs_play_in_thread(self):
        thread = launcher.start({"pitch-types"}, self.settings, self.session)
        self.assertIsNotNone(thread)
        thread.join(30)
        self.assertEqual(self.session.status, "idle", self.session.message)

    def test_shutdown_stops_the_play_servers(self):
        with mock.patch("app.launcher._game_argv",
                        return_value=[sys.executable, "-c", "import time; time.sleep(3)"]):
            thread = launcher.start({"rtts-bundle"}, self.settings, self.session)
            self.addCleanup(thread.join, 30)
            for _ in range(200):
                if self.session.status == "running":
                    break
                time.sleep(0.05)
            self.assertEqual(self.session.status, "running", self.session.message)
            self.assertEqual(len(self.servers), 1)
            launcher.shutdown(self.session)
            self.assertIsNotNone(self.servers[0].poll())
            thread.join(30)


class SessionTests(unittest.TestCase):
    def test_log_keeps_last_500(self):
        session = launcher.Session()
        for i in range(600):
            session.add(str(i))
        self.assertEqual(len(session.log), 500)
        self.assertEqual(session.log[0], "100")


if __name__ == "__main__":
    unittest.main()
