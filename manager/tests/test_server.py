import dataclasses
import http.client
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from app import __main__ as app_main
from app import builder, config, launcher, mods, server

REAL_BUILD_STATE = builder.state

ALPHA = {"name": "Alpha", "order": 10, "art": "ART_A", "profile": "own", "description": "a",
         "files": {"cwd": "{mod}", "install": ["x"], "uninstall": ["x"], "status": ["x"],
                   "installed_when": "yes"},
         "runtime": {"branch": "br/alpha", "env_on": {}, "env_off": {}},
         "server": {"cwd": "{mod}", "cmd": ["x"], "ready": "ok"}}
BETA = {"name": "Beta", "order": 20, "art": "ART_B", "description": "b",
        "runtime": {"branch": "br/beta", "env_on": {}, "env_off": {}}}
BUILD_STATE = {"exe": None, "built_from": None, "needed": {}, "up_to_date": False,
               "last_error": None}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.game = root / "MLB The Show 26"
        (self.game / "game" / "romfs" / "CROSS" / "LIVE").mkdir(parents=True)
        (self.game / "game" / "romfs" / "CROSS" / "LIVE" / "ART_A.WEBP").write_bytes(b"RIFFwebp")
        (root / "secret.txt").write_text("secret", encoding="utf-8")
        for mod_id, manifest in (("alpha", ALPHA), ("beta", BETA)):
            (root / "mods" / mod_id).mkdir(parents=True)
            (root / "mods" / mod_id / "mod.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.web = root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<p>hi</p>", encoding="utf-8")
        (self.web / "app.js").write_text("1;", encoding="utf-8")
        self.state = root / "state"
        self.settings = dataclasses.replace(
            config.Settings(), game=self.game, mods=root / "mods", state=self.state,
            build_out=root / "build", build_wt=root / "build-wt",  # no built-in patch file there
            profile_override=None, always_combined_exe=False)
        self.settings_path = self.state / "settings.json"
        patches = [
            mock.patch("app.builder.state", return_value=BUILD_STATE),
            mock.patch("app.applier.is_installed", return_value=True),
            mock.patch("app.procs.running_games", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = server.App(self.settings, self.settings_path, web=self.web)
        self.httpd = server.make_server(self.app, 0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, path, body=None, raw=False, headers=None):
        data = None if body is None else json.dumps(body).encode()
        sent = {} if body is None else {"Content-Type": "application/json"}
        sent.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=sent,
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                content, code, headers = resp.read(), resp.status, resp.headers
        except urllib.error.HTTPError as err:
            content, code, headers = err.read(), err.code, err.headers
            err.close()
        return code, (content if raw else json.loads(content or b"null")), headers

    def test_server_binds_localhost_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")
        with self.assertRaises(OSError):
            server.make_server(self.app, self.httpd.server_address[1])

    def test_state_lists_mods(self):
        code, body, _ = self.request("/api/state?refresh=1")
        self.assertEqual(code, 200)
        self.assertEqual([m["id"] for m in body["mods"]], ["alpha", "beta"])
        alpha, beta = body["mods"]
        self.assertEqual(alpha, {"id": "alpha", "name": "Alpha", "description": "a", "art": "ART_A",
                                 "on": True, "installed": True, "status_error": None,
                                 "redo": False, "has_files": True,
                                 "has_runtime": True, "branch": "br/alpha",
                                 "has_server": True, "own_profile": True,
                                 "has_patches": False, "art_url": "/art/ART_A",
                                 "card_url": None, "card_text": None, "features": None})
        self.assertIsNone(beta["installed"])  # no file part: nothing to install
        self.assertFalse(beta["has_server"])
        self.assertFalse(beta["has_files"])
        self.assertEqual(body["selected"], ["alpha", "beta"])
        self.assertEqual(body["session"], {"status": "idle", "message": "", "last_line": None})
        self.assertEqual(body["build"], BUILD_STATE)
        self.assertEqual(body["profile"], str(self.game / "profile-alpha"))
        self.assertEqual(body["games_running"], [])

    def test_switch_only_mod_has_no_branch(self):
        folder = self.settings.mods / "gamma"
        folder.mkdir()
        (folder / "mod.json").write_text(json.dumps(
            {"name": "Gamma", "order": 30,
             "runtime": {"env_on": {"MLB_OFFLINE_PACKAGES": "1"},
                         "env_off": {"MLB_OFFLINE_PACKAGES": "0"}}}), encoding="utf-8")
        _, body, _ = self.request("/api/state?refresh=1")
        gamma = next(m for m in body["mods"] if m["id"] == "gamma")
        self.assertIsNone(gamma["branch"])
        self.assertTrue(gamma["has_runtime"])

    def _state_with_games(self, games):
        self.app._games = (float("-inf"), [])  # force a fresh process query
        with mock.patch("app.procs.running_games", return_value=games):
            _, body, _ = self.request("/api/state")
        return body

    def test_state_play_gate_without_games(self):
        body = self._state_with_games([])
        self.assertIsNone(body["play_block"])
        self.assertIsNone(body["play_note"])

    def test_state_play_note_beside_one_test_copy(self):
        body = self._state_with_games([{"pid": 4242, "exe": r"C:\scratch\perktest\mlb.exe"}])
        self.assertIsNone(body["play_block"])
        self.assertIn("4242", body["play_note"])
        self.assertIn("mod files", body["play_note"])

    def test_state_play_blocked_by_own_game(self):
        body = self._state_with_games([{"pid": 4242, "exe": str(self.game / "mlb.exe")}])
        self.assertIn("4242", body["play_block"])
        self.assertIsNone(body["play_note"])

    def test_state_play_note_beside_three_copies_blocked_by_four(self):
        three = [{"pid": n, "exe": rf"C:\{c}\mlb.exe"} for n, c in ((1, "a"), (2, "b"), (3, "c"))]
        body = self._state_with_games(three)
        self.assertIsNone(body["play_block"])
        self.assertIn("3 test copies", body["play_note"])
        body = self._state_with_games(three + [{"pid": 4, "exe": r"C:\d\mlb.exe"}])
        self.assertIn("PID 4", body["play_block"])
        self.assertIsNone(body["play_note"])

    def test_failing_status_check_is_unknown_with_error(self):
        from app import applier
        _, body, _ = self.request("/api/state")
        self.assertEqual((body["mods"][0]["installed"], body["mods"][0]["status_error"]),
                         (None, None))  # not checked yet: "checking"
        failed = applier.StepFailed("alpha", "boom", what="status check failed (exit 2)")
        with mock.patch("app.applier.is_installed", side_effect=failed):
            _, body, _ = self.request("/api/state?refresh=1")
        alpha = body["mods"][0]
        self.assertIsNone(alpha["installed"])
        self.assertIn("status check failed (exit 2): boom", alpha["status_error"])
        _, body, _ = self.request("/api/state?refresh=1")  # a good check clears it
        self.assertEqual((body["mods"][0]["installed"], body["mods"][0]["status_error"]),
                         (True, None))

    def test_state_flags_mods_with_a_forced_redo(self):
        self.state.mkdir(exist_ok=True)
        (self.state / "journal.json").write_text(json.dumps(
            [{"step": ["install", "alpha"], "status": "failed", "output": "boom"},
             {"step": ["install", "beta"], "status": "running", "output": ""}]),
            encoding="utf-8")
        _, body, _ = self.request("/api/state")
        alpha, beta = body["mods"]
        self.assertIs(alpha["redo"], True)
        self.assertIs(beta["redo"], False)  # no file part: nothing to redo

    def test_state_skips_the_journal_while_busy(self):
        self.state.mkdir(exist_ok=True)
        (self.state / "journal.json").write_text(json.dumps(
            [{"step": ["install", "alpha"], "status": "running", "output": ""}]),
            encoding="utf-8")
        for status in sorted(launcher.BUSY):
            with self.subTest(status=status):
                self.app.session.set(status, "busy")
                with mock.patch("app.applier.pending_from_journal",
                                side_effect=AssertionError("journal read while busy")) as pending:
                    code, body, _ = self.request("/api/state")
                self.assertEqual(code, 200)
                pending.assert_not_called()
                self.assertEqual([m["redo"] for m in body["mods"]], [False, False])
        self.app.session.set("idle", "")
        _, body, _ = self.request("/api/state")
        self.assertIs(body["mods"][0]["redo"], True)  # idle again: the journal is read

    def test_state_does_not_run_status_scripts_without_refresh(self):
        from app import applier
        self.request("/api/state")
        applier.is_installed.assert_not_called()

    def test_state_survives_build_state_error(self):
        with mock.patch("app.builder.state", side_effect=builder.BuildFailed("no branch br/x")):
            code, body, _ = self.request("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(body["build"], {"error": "no branch br/x"})
        self.assertEqual(len(body["mods"]), 2)

    def test_state_survives_manifest_error(self):
        with mock.patch("app.mods.load_all", side_effect=mods.ManifestError("mod 'x': bad")):
            code, body, _ = self.request("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(body["mods"], [])
        self.assertIn("bad", body["error"])

    def test_state_has_last_session_line(self):
        self.app.session.set("building", "Building the combined game exe ...")
        self.app.session.add("[120/333] CC runtime/mlb/hid.c")
        _, body, _ = self.request("/api/state")
        self.assertEqual(body["session"]["last_line"], "[120/333] CC runtime/mlb/hid.c")
        self.assertEqual(body["session"]["status"], "building")

    # backup-profile -----------------------------------------------------------
    def make_profile(self):
        profile = self.game / "profile-alpha"  # alpha has its own profile and is selected
        (profile / "saves").mkdir(parents=True)
        (profile / "saves" / "a.dat").write_bytes(b"save")
        (profile / "profile.lock").write_bytes(b"lock")
        return profile

    def test_backup_profile_copies_without_lock(self):
        profile = self.make_profile()
        code, body, _ = self.request("/api/backup-profile", {})
        self.assertEqual(code, 200)
        backup = Path(body["path"])
        self.assertEqual(backup.parent, profile.parent)
        self.assertRegex(backup.name, r"^profile-alpha-backup-\d{8}-\d{6}$")
        self.assertEqual((backup / "saves" / "a.dat").read_bytes(), b"save")
        self.assertFalse((backup / "profile.lock").exists())
        self.assertTrue((profile / "profile.lock").exists())  # source untouched

    def test_backup_profile_twice_in_a_second_keeps_both(self):
        self.make_profile()
        with mock.patch("app.actions._stamp", return_value="20261116-120000"):
            _, first, _ = self.request("/api/backup-profile", {})
            code, second, _ = self.request("/api/backup-profile", {})
        self.assertEqual(code, 200)
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(Path(first["path"]).is_dir() and Path(second["path"]).is_dir())

    def test_backup_profile_refused_while_game_runs(self):
        self.make_profile()
        games = [{"pid": 4242, "exe": r"C:\g\mlb.exe"}]
        with mock.patch("app.procs.running_games", return_value=games):
            code, body, _ = self.request("/api/backup-profile", {})
        self.assertEqual(code, 409)
        self.assertIn("4242", body["error"])
        self.assertEqual([p.name for p in self.game.iterdir() if "backup" in p.name], [])

    def test_backup_profile_refused_while_busy(self):
        self.make_profile()
        self.assertTrue(self.app.session.claim())
        code, _, _ = self.request("/api/backup-profile", {})
        self.assertEqual(code, 409)

    def test_backup_profile_missing_profile(self):
        code, body, _ = self.request("/api/backup-profile", {})
        self.assertEqual(code, 404)
        self.assertIn("profile-alpha", body["error"])

    # open -----------------------------------------------------------------------
    def open_ok(self, what):
        with mock.patch("os.startfile", create=True) as startfile:
            code, body, _ = self.request("/api/open", {"what": what})
        self.assertEqual(code, 200, body)
        startfile.assert_called_once()
        return Path(startfile.call_args.args[0])

    def test_open_whitelisted_folders(self):
        self.make_profile()
        (self.settings.build_out).mkdir(parents=True)
        self.assertEqual(self.open_ok("profile"), self.game / "profile-alpha")
        self.assertEqual(self.open_ok("mods"), self.settings.mods)
        self.assertEqual(self.open_ok("mod:beta"), self.settings.mods / "beta")
        self.assertEqual(self.open_ok("logs"), self.state / "logs")  # created if missing
        self.assertEqual(self.open_ok("build"), self.settings.build_out)

    def test_open_refuses_anything_else(self):
        for what in (r"C:\Windows", "mod:nope", "mod:../alpha", "", None, 5, "state"):
            with mock.patch("os.startfile", create=True) as startfile:
                code, _, _ = self.request("/api/open", {"what": what})
            self.assertEqual(code, 400, what)
            startfile.assert_not_called()

    def test_open_missing_folder_is_404(self):
        with mock.patch("os.startfile", create=True) as startfile:
            code, _, _ = self.request("/api/open", {"what": "build"})
        self.assertEqual(code, 404)
        startfile.assert_not_called()

    def test_select_persists(self):
        code, body, _ = self.request("/api/select", {"id": "beta", "on": False})
        self.assertEqual(code, 200)
        saved = json.loads((self.state / "selection.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, ["alpha"])
        _, body, _ = self.request("/api/state")
        self.assertEqual(body["selected"], ["alpha"])
        self.assertEqual([m["on"] for m in body["mods"]], [True, False])
        self.request("/api/select", {"id": "beta", "on": True})
        saved = json.loads((self.state / "selection.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, ["alpha", "beta"])

    def test_select_writes_atomically(self):
        with mock.patch("app.config.write_json_atomic",
                        wraps=config.write_json_atomic) as write:
            self.request("/api/select", {"id": "beta", "on": False})
        write.assert_called_once_with(self.state / "selection.json", ["alpha"])

    def write_selection(self, text):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "selection.json").write_text(text, encoding="utf-8")

    def test_unreadable_selection_keeps_mods_off_and_says_so(self):
        for text in ("[\"alpha\", ", '{"alpha": true}', "[1]"):
            with self.subTest(text=text):
                self.write_selection(text)
                code, body, _ = self.request("/api/state")
                self.assertEqual(code, 200)
                self.assertEqual(body["selected"], [])
                self.assertEqual([m["on"] for m in body["mods"]], [False, False])
                self.assertIn("selection.json", body["error"])

    def test_play_refused_with_unreadable_selection(self):
        self.write_selection("[\"alpha\", ")
        with mock.patch("app.launcher.start") as start:
            code, body, _ = self.request("/api/play", {})
        self.assertEqual(code, 409)
        self.assertIn("selection.json", body["error"])
        start.assert_not_called()

    def test_select_rewrites_an_unreadable_selection(self):
        self.write_selection("garbage")
        code, body, _ = self.request("/api/select", {"id": "beta", "on": True})
        self.assertEqual((code, body["selected"]), (200, ["beta"]))
        _, body, _ = self.request("/api/state")
        self.assertNotIn("error", body)
        self.assertEqual(body["selected"], ["beta"])

    def test_select_rejects_unknown_id(self):
        code, body, _ = self.request("/api/select", {"id": "nope", "on": True})
        self.assertEqual(code, 400)
        self.assertIn("nope", body["error"])
        code, _, _ = self.request("/api/select", {"id": "alpha", "on": "yes"})
        self.assertEqual(code, 400)
        self.assertFalse((self.state / "selection.json").exists())

    def test_art_serves_webp(self):
        code, content, headers = self.request("/art/ART_A", raw=True)
        self.assertEqual(code, 200)
        self.assertEqual(content, b"RIFFwebp")
        self.assertEqual(headers["Content-Type"], "image/webp")
        self.assertIn("max-age", headers["Cache-Control"])
        code, _, _ = self.request("/art/NOT_THERE", raw=True)
        self.assertEqual(code, 404)

    def test_mod_art_file_is_served_by_mod_id(self):
        folder = self.settings.mods / "pictured"
        folder.mkdir()
        (folder / "mod.json").write_text(json.dumps({"name": "Pictured", "art": "art.png"}), encoding="utf-8")
        (folder / "art.png").write_bytes(b"\x89PNGdata")
        code, body, _ = self.request("/api/state")
        urls = {m["id"]: m["art_url"] for m in body["mods"]}
        self.assertEqual(urls["pictured"], "/modart/pictured")
        self.assertEqual(urls["alpha"], f"/art/{ALPHA['art']}" if ALPHA.get("art") else None)
        code, content, headers = self.request("/modart/pictured", raw=True)
        self.assertEqual((code, content), (200, b"\x89PNGdata"))
        self.assertEqual(headers["Content-Type"], "image/png")
        for path in ("/modart/alpha", "/modart/nope", "/modart/..%2Fsecret.txt", "/modart/"):
            code, _, _ = self.request(path, raw=True)
            self.assertEqual(code, 404, path)

    def test_title_card_and_features_reach_the_page(self):
        folder = self.settings.mods / "carded"
        (folder / "release").mkdir(parents=True)
        (folder / "mod.json").write_text(json.dumps(
            {"name": "Carded", "card": "release/title-card.png", "features": "release/features.md"}),
            encoding="utf-8")
        (folder / "release" / "title-card.png").write_bytes(b"\x89PNGcard")
        (folder / "release" / "features.md").write_text("**When**\n- **Draft:** a pick\n", encoding="utf-8")
        _, body, _ = self.request("/api/state")
        by_id = {m["id"]: m for m in body["mods"]}
        self.assertEqual(by_id["carded"]["card_url"], "/modcard/carded")
        self.assertEqual(by_id["carded"]["features"],
                         [{"title": "When", "blocks": [{"bullet": "**Draft:** a pick", "sub": []}]}])
        self.assertIsNone(by_id["alpha"]["card_url"])
        self.assertIsNone(by_id["alpha"]["features"])
        code, content, headers = self.request("/modcard/carded", raw=True)
        self.assertEqual((code, content), (200, b"\x89PNGcard"))
        self.assertEqual(headers["Content-Type"], "image/png")
        for path in ("/modcard/alpha", "/modcard/nope", "/modcard/..%2Fsecret.txt", "/modcard/"):
            code, _, _ = self.request(path, raw=True)
            self.assertEqual(code, 404, path)

    def test_state_carries_card_text(self):
        folder = self.settings.mods / "texted"
        (folder / "release").mkdir(parents=True)
        (folder / "mod.json").write_text(json.dumps(
            {"name": "Texted++", "art": "ART_A", "card": "release/card.json"}), encoding="utf-8")
        text = {"tagline": "A line", "highlights": [["Head", "rest"], ["Two", "more"]], "note": "Pass."}
        (folder / "release" / "card.json").write_text(json.dumps(text), encoding="utf-8")
        _, body, _ = self.request("/api/state")
        by_id = {m["id"]: m for m in body["mods"]}
        self.assertEqual(by_id["texted"]["card_text"], text)
        self.assertIsNone(by_id["texted"]["card_url"])  # no picture: the page draws the card
        self.assertEqual(by_id["texted"]["art_url"], "/art/ART_A")
        self.assertIsNone(by_id["alpha"]["card_text"])

    def test_art_rejects_path_tricks(self):
        for path in ("/art/..%5Cfoo", "/art/..%2F..%2Fsecret", "/art/art_a", "/art/ART_A.WEBP", "/art/"):
            code, _, _ = self.request(path, raw=True)
            self.assertEqual(code, 400, path)

    def test_static_files(self):
        code, content, headers = self.request("/", raw=True)
        self.assertEqual((code, content), (200, b"<p>hi</p>"))
        self.assertIn("text/html", headers["Content-Type"])
        code, _, headers = self.request("/app.js", raw=True)
        self.assertEqual(code, 200)
        self.assertIn("javascript", headers["Content-Type"])
        code, _, _ = self.request("/missing.css", raw=True)
        self.assertEqual(code, 404)

    def test_static_refuses_traversal(self):
        for path in ("/..%5Csecret.txt", "/..%2Fsecret.txt", "/%2e%2e/secret.txt",
                     "/C:%5CWindows%5Cwin.ini"):
            code, content, _ = self.request(path, raw=True)
            self.assertIn(code, (400, 404), path)
            self.assertNotEqual(content, b"secret")

    def test_logs_tail(self):
        logs = self.state / "logs"
        logs.mkdir(parents=True)
        (logs / "build.log").write_text("".join(f"line {i}\n" for i in range(5000)),
                                        encoding="utf-8")
        code, body, _ = self.request("/api/logs?which=build&lines=3")
        self.assertEqual(code, 200)
        self.assertEqual(body["which"], "build")
        self.assertEqual(body["path"], str(logs / "build.log"))
        self.assertEqual(body["lines"], ["line 4997", "line 4998", "line 4999"])
        _, body, _ = self.request("/api/logs?which=build&lines=99999")
        self.assertEqual(len(body["lines"]), 2000)
        self.assertEqual(body["lines"][-1], "line 4999")
        _, body, _ = self.request("/api/logs?which=build&lines=0")
        self.assertEqual(body["lines"], ["line 4999"])
        _, body, _ = self.request("/api/logs?which=server")
        self.assertEqual(body["lines"], [])

    def test_logs_rejects_unknown_which(self):
        code, _, _ = self.request("/api/logs?which=..%5Csecret")
        self.assertEqual(code, 400)
        code, _, _ = self.request("/api/logs?which=build&lines=abc")
        self.assertEqual(code, 400)

    def test_game_log_follows_selection(self):
        _, body, _ = self.request("/api/logs?which=game")
        self.assertEqual(body["path"], str(self.settings.build_out / "logs" / "mlb.log"))
        (self.state).mkdir(parents=True, exist_ok=True)
        (self.state / "selection.json").write_text("[]", encoding="utf-8")
        _, body, _ = self.request("/api/logs?which=game")
        self.assertEqual(body["path"], str(self.game / "logs" / "mlb.log"))

    def _dev_mode(self):
        """A source clone in the settings: the real build state reads branch heads."""
        clone = Path(self._tmp.name) / "recomp"
        (clone / ".git").mkdir(parents=True)
        self.app.settings.recomp = clone

    def _add_patch_mod(self):
        folder = Path(self.settings.mods) / "gamma"
        folder.mkdir()
        (folder / "mod.json").write_text(json.dumps(
            {"name": "Gamma", "order": 30, "patches": "patches.txt"}), encoding="utf-8")
        (folder / "patches.txt").write_text("set ev MLB_EV 1\nsite ev main 10 1 2\n",
                                            encoding="utf-8")

    def test_patch_only_selection_plays_the_combined_exe(self):
        self._add_patch_mod()
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "selection.json").write_text('["gamma"]', encoding="utf-8")
        _, body, _ = self.request("/api/logs?which=game")
        self.assertEqual(body["path"], str(self.settings.build_out / "logs" / "mlb.log"))

    def test_state_flags_patch_mods_and_lists_patch_sets(self):
        self._dev_mode()
        self._add_patch_mod()
        with mock.patch("app.builder.state", REAL_BUILD_STATE), \
                mock.patch("app.builder.heads", side_effect=lambda b, s, missing_ok=(): {k: "a" * 40 for k in b}):
            _, body, _ = self.request("/api/state")
        flags = {m["id"]: m["has_patches"] for m in body["mods"]}
        self.assertEqual(flags, {"alpha": False, "beta": False, "gamma": True})
        self.assertEqual(body["build"]["patch_sets"],
                         [{"mod": "gamma", "name": "ev", "env": "MLB_EV", "default": 1}])
        self.assertIn("patches", body["build"]["needed"])

    def test_settings_get_and_post(self):
        code, body, _ = self.request("/api/settings")
        self.assertEqual(code, 200)
        self.assertEqual(body["game"], str(self.game))
        self.assertEqual(body["port"], 8645)
        code, body, _ = self.request("/api/settings", {"packages_port": 10011,
                                                       "always_combined_exe": True})
        self.assertEqual(code, 200)
        self.assertEqual(body["packages_port"], 10011)
        self.assertEqual(self.app.settings.packages_port, 10011)
        self.assertTrue(self.app.settings.always_combined_exe)
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["packages_port"], 10011)
        self.assertEqual(saved["game"], str(self.game))

    def test_settings_roundtrip_release_keys(self):
        code, body, _ = self.request("/api/settings")
        self.assertEqual((body["release_source"], body["recomp"], body["setup"]), ("", None, None))
        self.assertEqual(body["runtime_dir"], str(config.MANAGER / "runtime"))
        runtime = Path(self._tmp.name) / "runtime"
        code, body, _ = self.request("/api/settings", {
            "release_source": r"D:\release", "runtime_dir": str(runtime),
            "setup": r"D:\Setup", "recomp": None, "build_wt": None, "theshow": None})
        self.assertEqual(code, 200, body)
        self.assertEqual((body["release_source"], body["runtime_dir"], body["setup"]),
                         (r"D:\release", str(runtime), r"D:\Setup"))
        self.assertEqual(self.app.settings.runtime_dir, runtime)
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual((saved["release_source"], saved["recomp"]), (r"D:\release", None))
        code, body, _ = self.request("/api/settings", {"release_source": ""})
        self.assertEqual((code, body["release_source"]), (200, ""))

    def test_state_reports_mode(self):
        code, body, _ = self.request("/api/state")
        self.assertEqual((code, body["mode"]), (200, "player"))  # no recomp clone set
        clone = Path(self._tmp.name) / "recomp"
        (clone / ".git").mkdir(parents=True)
        code, _, _ = self.request("/api/settings", {"recomp": str(clone)})
        self.assertEqual(code, 200)
        code, body, _ = self.request("/api/state")
        self.assertEqual((code, body["mode"]), (200, "dev"))

    def test_settings_refused_while_busy(self):
        self.assertTrue(self.app.session.claim())
        code, body, _ = self.request("/api/settings", {"packages_port": 10011})
        self.assertEqual(code, 409)
        self.assertIn("applying", body["error"])
        self.assertFalse(self.settings_path.exists())
        self.assertEqual(self.app.settings.packages_port, 10010)

    def test_settings_rejects_bad_input(self):
        for bad in ({"nope": 1}, {"port": "8645"}, {"port": True}, {"port": 70000},
                    {"always_combined_exe": 1}, {"profile_override": 5}, {"game": None},
                    {"runtime_dir": None}, {"release_source": None}, {"setup": ""}, [1]):
            code, _, _ = self.request("/api/settings", bad)
            self.assertEqual(code, 400, bad)
        self.assertFalse(self.settings_path.exists())
        code, body, _ = self.request("/api/settings", {"profile_override": r"D:\p"})
        self.assertEqual((code, body["profile_override"]), (200, r"D:\p"))
        code, body, _ = self.request("/api/settings", {"profile_override": None})
        self.assertEqual((code, body["profile_override"]), (200, None))

    def test_play_starts_with_selection(self):
        with mock.patch("app.launcher.start") as start:
            start.return_value = threading.Thread(target=lambda: None)
            start.return_value.start()
            code, body, _ = self.request("/api/play", {})
        self.assertEqual(code, 202)
        self.assertEqual(set(body), {"status", "message"})
        selected, settings, session = start.call_args.args
        self.assertEqual(set(selected), {"alpha", "beta"})
        self.assertIs(session, self.app.session)

    def test_play_refreshes_installed_after_apply(self):
        from app import applier
        with mock.patch("app.launcher.start") as start:
            start.return_value = threading.Thread(target=lambda: None)
            start.return_value.start()
            self.request("/api/play", {})
        on_applied = start.call_args.kwargs["on_applied"]
        applier.is_installed.return_value = False
        on_applied()
        for _ in range(100):
            if self.request("/api/state")[1]["mods"][0]["installed"] is False:
                break
            time.sleep(0.05)
        self.assertIs(self.request("/api/state")[1]["mods"][0]["installed"], False)

    def test_older_refresh_never_overwrites_a_newer_one(self):
        from app import applier
        release, first_running = threading.Event(), threading.Event()

        def slow_then_fast(mod, settings):
            if not first_running.is_set():
                first_running.set()
                release.wait(10)
                return True  # what the slow startup check saw (before the apply)
            return False

        applier.is_installed.side_effect = slow_then_fast
        slow = threading.Thread(target=self.app.refresh_installed)
        slow.start()
        self.assertTrue(first_running.wait(5))
        fast = threading.Thread(target=self.app.refresh_installed)  # started later
        fast.start()
        fast.join(3)
        self.assertFalse(fast.is_alive(), "a refresh must not queue behind a slow one")
        self.assertIs(self.request("/api/state")[1]["mods"][0]["installed"], False)
        release.set()
        slow.join(10)
        self.assertIs(self.request("/api/state")[1]["mods"][0]["installed"], False)

    def test_play_refused_while_busy(self):
        self.assertTrue(self.app.session.claim())
        with mock.patch("app.launcher._play") as inner:
            code, body, _ = self.request("/api/play", {})
        self.assertEqual(code, 409)
        self.assertIn("already", body["error"])
        inner.assert_not_called()

    def test_build_runs_runtime_branches(self):
        self._dev_mode()
        done = threading.Event()
        calls = []

        def fake_build(branches, settings, log, optional=frozenset()):
            calls.append((branches, optional))
            log("compiling ...")
            done.set()

        with mock.patch("app.builder.build", side_effect=fake_build):
            code, body, _ = self.request("/api/build", {})
            self.assertEqual(code, 202)
            self.assertTrue(done.wait(5))
            for _ in range(50):
                if self.app.session.status == "idle":
                    break
                time.sleep(0.05)
        self.assertEqual(calls, [(["br/alpha", "br/beta"], frozenset())])
        self.assertEqual(self.app.session.status, "idle")
        self.assertIn("compiling ...", self.app.session.snapshot()["log"])

    def test_build_in_player_mode_passes_no_branches(self):
        done = threading.Event()
        calls = []

        def fake_build(branches, settings, log, optional=frozenset()):
            calls.append((branches, optional))
            done.set()

        with mock.patch("app.builder.build", side_effect=fake_build):
            code, _, _ = self.request("/api/build", {})
            self.assertEqual(code, 202)
            self.assertTrue(done.wait(5))
        self.assertEqual(calls, [([], frozenset())])

    def test_build_passes_the_off_mods_branches_as_optional(self):
        self._dev_mode()
        self.assertEqual(self.request("/api/select", {"id": "alpha", "on": False})[0], 200)
        done = threading.Event()
        calls = []

        def fake_build(branches, settings, log, optional=frozenset()):
            calls.append((branches, optional))
            done.set()

        with mock.patch("app.builder.build", side_effect=fake_build):
            code, _, _ = self.request("/api/build", {})
            self.assertEqual(code, 202)
            self.assertTrue(done.wait(5))
        self.assertEqual(calls, [(["br/alpha", "br/beta"], frozenset({"br/alpha"}))])

    def test_state_carries_left_out(self):
        self._dev_mode()
        self.settings.build_out.mkdir(parents=True, exist_ok=True)
        left = {"br/beta": {"head": "a" * 40, "files": ["runtime/mlb/boot.c"], "mod": "beta"}}
        (self.settings.build_out / "build-info.json").write_text(json.dumps(
            {"built_from": {}, "exe": "x", "left_out": left}), encoding="utf-8")
        with mock.patch("app.builder.state", REAL_BUILD_STATE), \
                mock.patch("app.builder.heads", side_effect=lambda b, s, missing_ok=(): {k: "a" * 40 for k in b}):
            _, body, _ = self.request("/api/state")
        self.assertEqual(body["build"]["left_out"], left)

    def test_build_failure_shows_in_session(self):
        with mock.patch("app.builder.build", side_effect=builder.BuildFailed("conflicts in: a.c")):
            self.request("/api/build", {})
            for _ in range(50):
                if self.app.session.status == "error":
                    break
                time.sleep(0.05)
        self.assertEqual(self.app.session.status, "error")
        self.assertIn("conflicts", self.app.session.message)

    def test_build_refused_while_busy(self):
        self.assertTrue(self.app.session.claim())
        with mock.patch("app.builder.build") as build:
            code, _, _ = self.request("/api/build", {})
        self.assertEqual(code, 409)
        build.assert_not_called()

    def raw_post(self, path, headers, body=b""):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        conn.putrequest("POST", path, skip_accept_encoding=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status

    def _refused_with_body(self, headers, size):
        """POST the head, then (late) `size` body bytes, then read the reply. A server
        that answers before the body came and closes gets the body on a closed socket;
        Windows then resets the connection and the reply is lost (WinError 10053/10054)."""
        import socket
        port = self.httpd.server_address[1]
        head = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json",
                "Content-Length": str(size)}
        head.update(headers)
        request = ("POST /api/select HTTP/1.1\r\n"
                   + "".join(f"{k}: {v}\r\n" for k, v in head.items()) + "\r\n").encode()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(request)  # urllib, too, sends the head and the body apart
            time.sleep(0.3)
            sock.sendall(b" " * size)
            time.sleep(0.3)
            reply = b""
            while b"\r\n" not in reply:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply += chunk
        return int(reply.split(b" ", 2)[1])

    def test_refusal_reads_the_body_first(self):
        for _ in range(3):
            self.assertEqual(self._refused_with_body({"Origin": "http://evil.example"}, 60000), 403)
            self.assertEqual(self._refused_with_body({"Content-Type": "text/plain"}, 60000), 415)

    def test_foreign_host_refused(self):
        evil = {"Host": "evil.example:%d" % self.httpd.server_address[1]}
        code, _, _ = self.request("/api/state", headers=evil)
        self.assertEqual(code, 403)
        code, _, _ = self.request("/api/play", {}, headers=evil)
        self.assertEqual(code, 403)
        code, _, _ = self.request("/api/state", headers={"Host": "127.0.0.1:1"})
        self.assertEqual(code, 403)
        code, _, _ = self.request(
            "/api/state", headers={"Host": "localhost:%d" % self.httpd.server_address[1]})
        self.assertEqual(code, 200)

    def test_foreign_origin_refused(self):
        with mock.patch("app.launcher.start") as start:
            for origin in ("http://evil.example", "null", "http://127.0.0.1:1"):
                code, _, _ = self.request("/api/play", {}, headers={"Origin": origin})
                self.assertEqual(code, 403, origin)
            start.assert_not_called()
        code, _, _ = self.request("/api/select", {"id": "beta", "on": False},
                                  headers={"Origin": self.base})
        self.assertEqual(code, 200)

    def test_post_requires_json_content_type(self):
        code, _, _ = self.request("/api/settings", {"mods": r"\\attacker\share"},
                                  headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 415)
        self.assertFalse(self.settings_path.exists())
        self.assertEqual(self.app.settings.mods, self.settings.mods)

    def test_body_length_checked(self):
        port = self.httpd.server_address[1]
        base = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
        self.assertEqual(self.raw_post("/api/select", dict(base, **{"Content-Length": "999999999"})), 413)
        self.assertEqual(self.raw_post("/api/select", dict(base, **{"Content-Length": "-1"})), 400)
        self.assertEqual(self.raw_post("/api/select", dict(base, **{"Content-Length": "abc"})), 400)

    def test_quit_refused_while_busy(self):
        self.assertTrue(self.app.session.claim())
        code, body, _ = self.request("/api/quit", {})
        self.assertEqual(code, 409)
        self.assertIn("applying", body["error"])
        self.assertFalse(self.app.quit.is_set())
        self.assertEqual(self.request("/api/state")[0], 200)

    def test_quit_stops_the_server(self):
        with mock.patch("app.launcher.shutdown") as shutdown:
            code, _, _ = self.request("/api/quit", {})
            self.assertEqual(code, 200)
            self.assertTrue(self.app.quit.wait(5))
            for _ in range(50):
                if shutdown.called:
                    break
                time.sleep(0.05)
        shutdown.assert_called_once_with(self.app.session)
        with self.assertRaises(OSError):
            urllib.request.urlopen(self.base + "/api/state", timeout=3)

    def test_unknown_api_is_404(self):
        code, _, _ = self.request("/api/nope")
        self.assertEqual(code, 404)

    # --- the mod picker: release catalog, get mods, remove a mod ----------------------------
    def _release(self, release="2026.11.2"):
        """A local-folder release in Settings > Release source: alpha (also on this PC, from
        an older release) and delta (not here). Returns its index."""
        folder = Path(self._tmp.name) / "release"
        folder.mkdir(exist_ok=True)
        card = {"tagline": "Pitch it", "highlights": [["Grip", "new"]], "note": ""}
        sections = [{"title": "Pitches", "blocks": [{"bullet": "**18Ks** a game", "sub": []}]}]
        entry = {"file": "x.zip", "sha256": "0" * 64, "size": 4_200_000}
        index = {"release": release, "fastball": "0.2.0+74a6525", "runtime": dict(entry),
                 "mods": [dict(entry, id="alpha", name="Alpha", version=release,
                               description="a", art="ART_A", card=card, features=sections,
                               parts=["files", "runtime", "<b>x</b>"]),
                          dict(entry, id="delta", name="Delta", version=release,
                               description="d", card={}, features=[], parts=["patches"],
                               size=900)]}
        (folder / "index.json").write_text(json.dumps(index), encoding="utf-8")
        self.app.settings.release_source = str(folder)
        return index

    def _downloaded(self, records):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "installed.json").write_text(json.dumps(records), encoding="utf-8")

    def _wait_idle(self):
        for _ in range(250):
            if self.app.session.status not in launcher.BUSY:
                return
            time.sleep(0.02)
        self.fail(f"still {self.app.session.status}")

    def test_catalog_lists_release_mods_and_installed(self):
        self._release()
        self._downloaded({"alpha": "2026.11.1"})
        code, body, _ = self.request("/api/catalog")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["release"], "2026.11.2")
        alpha, delta = body["mods"]
        self.assertEqual(alpha, {
            "id": "alpha", "name": "Alpha", "version": "2026.11.2", "description": "a",
            "art_url": "/art/ART_A", "size": 4_200_000, "parts": ["files", "runtime"],
            "card_text": {"tagline": "Pitch it", "highlights": [["Grip", "new"]], "note": ""},
            "features": [{"title": "Pitches",
                          "blocks": [{"bullet": "**18Ks** a game", "sub": []}]}],
            "installed": True, "installed_release": "2026.11.1", "update": True, "on": True})
        self.assertEqual((delta["installed"], delta["update"], delta["installed_release"]),
                         (False, False, None))
        self.assertEqual((delta["card_text"], delta["features"], delta["art_url"], delta["size"]),
                         (None, None, None, 900))

    def test_catalog_drops_card_text_and_features_of_the_wrong_shape(self):
        index = self._release()
        index["mods"][0]["card"] = {"tagline": 5}
        index["mods"][0]["features"] = [{"title": "x", "blocks": [{"html": "<b>"}]}]
        index["mods"][0]["art"] = "../secret"
        (Path(self.app.settings.release_source) / "index.json").write_text(
            json.dumps(index), encoding="utf-8")
        code, body, _ = self.request("/api/catalog")
        self.assertEqual(code, 200, body)
        alpha = body["mods"][0]
        self.assertEqual((alpha["card_text"], alpha["features"], alpha["art_url"]),
                         (None, None, None))

    def test_catalog_error_is_plain(self):
        self.app.settings.release_source = str(Path(self._tmp.name) / "no-such-release")
        code, body, _ = self.request("/api/catalog")
        self.assertEqual(code, 502)
        self.assertIn("list of mods", body["error"])

    def test_get_mods_is_refused_while_busy(self):
        self._release()
        self.assertTrue(self.app.session.claim())
        with mock.patch("app.releases.install") as install:
            code, body, _ = self.request("/api/get-mods", {"ids": ["delta"]})
        self.assertEqual(code, 409)
        self.assertIn("wait", body["error"])
        install.assert_not_called()

    def test_get_mods_is_refused_while_the_game_runs(self):
        self._release()
        game = [{"pid": 4242, "exe": str(self.game / "mlb.exe")}]
        with mock.patch("app.releases.install") as install, \
                mock.patch("app.procs.running_games", return_value=game):
            code, body, _ = self.request("/api/get-mods", {"ids": ["delta"]})
        self.assertEqual(code, 409)
        self.assertIn("4242", body["error"])
        install.assert_not_called()
        self.assertEqual(self.app.session.status, "idle")

    def test_get_mods_checks_its_body(self):
        for bad in ({}, {"ids": "delta"}, {"ids": [5]}, [1]):
            code, _, _ = self.request("/api/get-mods", bad)
            self.assertEqual(code, 400, bad)

    def test_get_mods_installs_through_releases(self):
        index = self._release()
        calls = []

        def install(ids, got_index, settings, log):
            calls.append((ids, got_index, settings))
            log("Installed release 2026.11.2: Delta.")

        with mock.patch("app.releases.install", side_effect=install), \
                mock.patch("app.applier.apply") as apply:
            code, body, _ = self.request("/api/get-mods", {"ids": ["delta"]})
            self.assertEqual(code, 202, body)
            self._wait_idle()
        self.assertEqual(calls, [(["delta"], index, self.app.settings)])
        apply.assert_not_called()  # alpha was not downloaded, so it is not being updated
        snap = self.app.session.snapshot()
        self.assertEqual(snap["status"], "idle", snap)
        self.assertIn("Delta", snap["message"])
        self.assertIn("Installed release 2026.11.2: Delta.", snap["log"])

    def test_get_mods_takes_out_an_installed_mod_before_updating_it(self):
        self._release()
        self._downloaded({"alpha": "2026.11.1"})
        order = []
        with mock.patch("app.releases.install", side_effect=lambda *a: order.append("install")), \
                mock.patch("app.applier.apply",
                           side_effect=lambda steps, *a: order.append(steps)):
            code, _, _ = self.request("/api/get-mods", {"ids": ["delta"]})
            self.assertEqual(code, 202)
            self._wait_idle()
        self.assertEqual(order, [[("uninstall", "alpha")], "install"])
        self.assertEqual(self.app.session.status, "idle")

    def test_get_mods_leaves_a_current_mod_alone(self):
        self._release()
        self._downloaded({"alpha": "2026.11.2"})
        with mock.patch("app.releases.install"), mock.patch("app.applier.apply") as apply:
            self.request("/api/get-mods", {"ids": ["delta"]})
            self._wait_idle()
        apply.assert_not_called()

    def test_get_mods_stops_when_the_uninstall_fails(self):
        from app import applier
        self._release()
        self._downloaded({"alpha": "2026.11.1"})
        with mock.patch("app.releases.install") as install, \
                mock.patch("app.applier.apply",
                           side_effect=applier.StepFailed("alpha", "boom")):
            self.request("/api/get-mods", {"ids": ["delta"]})
            self._wait_idle()
        install.assert_not_called()
        snap = self.app.session.snapshot()
        self.assertEqual(snap["status"], "error")
        self.assertIn("Alpha", snap["message"])
        self.assertIn("nothing was downloaded", snap["message"])

    def test_get_mods_shows_a_release_error_plainly(self):
        from app import releases
        self._release()
        with mock.patch("app.releases.install",
                        side_effect=releases.ReleaseError("x.zip is damaged; try again.")):
            self.request("/api/get-mods", {"ids": ["delta"]})
            self._wait_idle()
        snap = self.app.session.snapshot()
        self.assertEqual((snap["status"], snap["message"]),
                         ("error", "x.zip is damaged; try again."))

    def test_get_mods_switches_the_new_mods_on(self):
        self._release()
        self.assertEqual(self.request("/api/select", {"id": "beta", "on": False})[0], 200)
        with mock.patch("app.releases.install"):
            self.request("/api/get-mods", {"ids": ["delta"]})
            self._wait_idle()
        saved = json.loads((self.state / "selection.json").read_text(encoding="utf-8"))
        self.assertIn("delta", saved)
        self.assertNotIn("beta", saved)

    def test_get_and_remove_refresh_the_installed_status(self):
        self._release()
        self._downloaded({"beta": "2026.11.2"})
        self.assertEqual(self.request("/api/select", {"id": "beta", "on": False})[0], 200)
        ended = threading.Event()  # set by the refresh, the job thread's last act (after the session goes idle)
        with mock.patch("app.releases.install"), mock.patch("app.releases.remove"), \
                mock.patch.object(self.app, "refresh_in_background", side_effect=ended.set) as refresh:
            self.request("/api/get-mods", {"ids": ["delta"]})
            self.assertTrue(ended.wait(10), "the download job never ended")
            self._wait_idle()
            self.assertEqual(refresh.call_count, 1)
            ended.clear()
            self.request("/api/remove-mod", {"id": "beta"})
            self.assertTrue(ended.wait(10), "the removal job never ended")
            self._wait_idle()
            self.assertEqual(refresh.call_count, 2)

    def test_remove_mod_route(self):
        self._downloaded({"beta": "2026.11.2"})
        self.assertEqual(self.request("/api/select", {"id": "beta", "on": False})[0], 200)
        with mock.patch("app.releases.remove") as remove:
            code, body, _ = self.request("/api/remove-mod", {"id": "beta"})
            self.assertEqual(code, 202, body)
            self._wait_idle()
        self.assertEqual(remove.call_args.args[:2], ("beta", self.app.settings))
        self.assertEqual(self.app.session.status, "idle")
        self.assertIn("Beta", self.app.session.message)
        for bad in ({}, {"id": 5}, [1]):
            self.assertEqual(self.request("/api/remove-mod", bad)[0], 400, bad)

    def test_remove_mod_takes_its_files_out_first(self):
        from app import applier
        self._downloaded({"alpha": "2026.11.2"})
        self.assertEqual(self.request("/api/select", {"id": "alpha", "on": False})[0], 200)
        order = []
        with mock.patch("app.releases.remove", side_effect=lambda *a: order.append("remove")), \
                mock.patch("app.applier.apply",
                           side_effect=lambda steps, *a: order.append(steps)):
            self.request("/api/remove-mod", {"id": "alpha"})
            self._wait_idle()
            self.assertEqual(order, [[("uninstall", "alpha")], "remove"])
            applier.is_installed.return_value = False
            order.clear()
            self.request("/api/remove-mod", {"id": "alpha"})
            self._wait_idle()
        self.assertEqual(order, ["remove"])

    def test_remove_mod_refuses_a_mod_that_is_on(self):
        self._downloaded({"alpha": "2026.11.2"})
        with mock.patch("app.releases.remove") as remove, \
                mock.patch("app.applier.apply") as apply:
            self.request("/api/remove-mod", {"id": "alpha"})
            self._wait_idle()
        remove.assert_not_called()
        apply.assert_not_called()
        snap = self.app.session.snapshot()
        self.assertEqual(snap["status"], "error")
        self.assertIn("turn it off first", snap["message"])

    def test_remove_mod_is_refused_while_busy(self):
        self.assertTrue(self.app.session.claim())
        with mock.patch("app.releases.remove") as remove:
            code, body, _ = self.request("/api/remove-mod", {"id": "beta"})
        self.assertEqual(code, 409)
        self.assertIn("wait", body["error"])
        remove.assert_not_called()

    def test_state_with_no_mods_folder_is_no_mods(self):
        self.app.settings.mods = Path(self._tmp.name) / "not-yet"
        code, body, _ = self.request("/api/state")
        self.assertEqual((code, body["mods"], body.get("error")), (200, [], None))


class StartupTests(unittest.TestCase):
    """python -m app: an unfinished mod download is put back before the page is served."""

    def _main(self, recover):
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            settings = dataclasses.replace(config.Settings(), state=Path(tmp))
            httpd = mock.Mock()
            httpd.serve_forever.side_effect = lambda: events.append("serve")
            apps = []

            def make_server(app, port):
                apps.append(app)
                events.append("bound")
                return httpd

            def wait(app, edge, launched):
                for _ in range(100):  # serve_forever runs in a thread
                    if "serve" in events:
                        break
                    time.sleep(0.01)

            with mock.patch.object(app_main.config, "load", return_value=settings), \
                    mock.patch.object(app_main.server, "make_server", side_effect=make_server), \
                    mock.patch.object(app_main, "open_window", return_value=None), \
                    mock.patch.object(app_main, "wait_for_exit", side_effect=wait), \
                    mock.patch.object(app_main.releases, "recover",
                                      side_effect=lambda s, log=None: (events.append("recover"),
                                                                       recover())):
                app_main.main()
        return events, apps[0]

    def test_recover_runs_once_after_binding_and_before_serving(self):
        events, _ = self._main(lambda: None)
        self.assertEqual(events, ["bound", "recover", "serve"])

    def test_a_failed_recover_shows_in_the_status_line(self):
        from app import releases

        def fail():
            raise releases.ReleaseError("Putting back the mods failed; close the folder.")

        events, app = self._main(fail)
        self.assertEqual(events, ["bound", "recover", "serve"])
        self.assertEqual((app.session.status, app.session.message),
                         ("error", "Putting back the mods failed; close the folder."))


class WindowTests(unittest.TestCase):
    def _wait(self, seconds, handoff):
        app = server.App(config.Settings(), Path("unused.json"))
        launched = time.monotonic()
        edge = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
        self.addCleanup(edge.kill)
        with mock.patch.object(app_main, "HANDOFF_SECONDS", handoff),                 mock.patch.object(app_main, "note"):
            thread = threading.Thread(target=app_main.wait_for_exit, args=(app, edge, launched),
                                      daemon=True)
            thread.start()
            thread.join(3)
            alive = thread.is_alive()
            app.quit.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        return alive

    def test_quick_exit_is_a_handoff(self):
        self.assertTrue(self._wait(0, handoff=5.0))  # still serving until quit

    def test_late_exit_is_the_window_closing(self):
        self.assertFalse(self._wait(1, handoff=0.5))  # returned by itself

    def test_no_edge_serves_until_quit(self):
        app = server.App(config.Settings(), Path("unused.json"))
        thread = threading.Thread(target=app_main.wait_for_exit, args=(app, None, 0.0),
                                  daemon=True)
        thread.start()
        thread.join(1)
        self.assertTrue(thread.is_alive())
        app.quit.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())



class LogRotationTests(unittest.TestCase):
    def test_big_manager_log_moves_to_dot_1_at_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = dataclasses.replace(config.Settings(), state=Path(tmp))
            logs = Path(tmp) / "logs"
            logs.mkdir()
            log, old = logs / "manager.log", logs / "manager.log.1"
            log.write_text("small", encoding="utf-8")
            app_main.rotate_log(settings, limit=10)
            self.assertEqual(log.read_text(encoding="utf-8"), "small")
            self.assertFalse(old.exists())
            old.write_text("older", encoding="utf-8")
            log.write_text("x" * 11, encoding="utf-8")
            app_main.rotate_log(settings, limit=10)
            self.assertFalse(log.exists())
            self.assertEqual(old.read_text(encoding="utf-8"), "x" * 11)
            app_main.rotate_log(settings, limit=10)  # nothing to rotate: no error

    def test_limit_is_5_mb(self):
        self.assertEqual(app_main.LOG_LIMIT, 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
