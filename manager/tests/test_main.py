"""python -m app when things go wrong at startup: no Edge, the port taken, any other error.
No test shows a real message box or opens a real browser or Edge."""
import dataclasses
import http.server
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import __main__ as app_main
from app import config, server


class NoRealDialogs(unittest.TestCase):
    def setUp(self):
        for name in ("message_box",):
            p = mock.patch.object(app_main, name)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(app_main.webbrowser, "open", return_value=True)
        self.browser = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(app_main.subprocess, "Popen")
        self.popen = p.start()
        self.addCleanup(p.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = dataclasses.replace(config.Settings(), state=self.tmp / "state")

    def log(self):
        path = self.tmp / "state" / "logs" / "manager.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""


class OpenWindowTests(NoRealDialogs):
    URL = "http://127.0.0.1:8645/"

    def test_with_edge_it_opens_the_app_window_as_before(self):
        edge = self.tmp / "msedge.exe"
        edge.write_bytes(b"")
        with mock.patch.object(app_main, "EDGE", edge):
            proc = app_main.open_window(self.settings, self.URL)
        self.assertIs(proc, self.popen.return_value)
        self.popen.assert_called_once_with(
            [str(edge), f"--app={self.URL}",
             "--user-data-dir=" + str(self.tmp / "state" / "edge"), "--window-size=1600,900"])
        self.browser.assert_not_called()
        self.message_box.assert_not_called()
        self.assertIn("Edge", self.log())

    def test_without_edge_it_opens_the_default_browser(self):
        with mock.patch.object(app_main, "EDGE", self.tmp / "missing" / "msedge.exe"):
            proc = app_main.open_window(self.settings, self.URL)
        self.assertIsNone(proc)
        self.popen.assert_not_called()
        self.browser.assert_called_once_with(self.URL)
        self.message_box.assert_not_called()
        self.assertIn("default browser", self.log())

    def test_an_edge_that_will_not_start_falls_back_to_the_default_browser(self):
        edge = self.tmp / "msedge.exe"
        edge.write_bytes(b"")
        self.popen.side_effect = OSError("bad exe")
        with mock.patch.object(app_main, "EDGE", edge):
            proc = app_main.open_window(self.settings, self.URL)
        self.assertIsNone(proc)
        self.browser.assert_called_once_with(self.URL)
        self.assertIn("bad exe", self.log())
        self.assertIn("default browser", self.log())

    def test_no_browser_at_all_shows_the_address(self):
        self.browser.return_value = False
        with mock.patch.object(app_main, "EDGE", self.tmp / "missing.exe"):
            self.assertIsNone(app_main.open_window(self.settings, self.URL))
        self.message_box.assert_called_once()
        self.assertIn(self.URL, self.message_box.call_args.args[0])

    def test_a_browser_error_shows_the_address(self):
        self.browser.side_effect = app_main.webbrowser.Error("none")
        with mock.patch.object(app_main, "EDGE", self.tmp / "missing.exe"):
            self.assertIsNone(app_main.open_window(self.settings, self.URL))
        self.assertIn(self.URL, self.message_box.call_args.args[0])


class _Foreign(http.server.BaseHTTPRequestHandler):
    body = b"<html>someone else</html>"
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class _OldManager(_Foreign):
    """A manager from before /api/ping: 404 there, its state JSON at /api/state."""

    def do_GET(self):
        if self.path == "/api/ping":
            self.status, self.body = 404, b'{"error": "no such endpoint: GET /api/ping"}'
        else:
            self.status, self.body = 200, json.dumps({"session": {}, "mods": []}).encode()
        super().do_GET()


class PortOwnerTests(unittest.TestCase):
    def _serve(self, httpd):
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def test_our_manager_is_ours_without_asking_for_its_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = dataclasses.replace(config.Settings(), state=Path(tmp) / "state",
                                           mods=Path(tmp) / "mods")
            with mock.patch.object(server.App, "state", side_effect=AssertionError("slow")):
                app = server.App(settings, Path(tmp) / "settings.json")
                port = self._serve(server.make_server(app, 0))
                self.assertEqual(app_main.port_owner(port), "ours")

    def test_an_older_manager_without_ping_is_ours(self):
        port = self._serve(http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OldManager))
        self.assertEqual(app_main.port_owner(port), "ours")

    def test_another_web_server_is_other(self):
        port = self._serve(http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Foreign))
        self.assertEqual(app_main.port_owner(port), "other")

    def test_other_json_is_other(self):
        handler = type("Json", (_Foreign,), {"body": json.dumps({"hello": 1}).encode()})
        port = self._serve(http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler))
        self.assertEqual(app_main.port_owner(port), "other")

    def test_nothing_listening_is_other(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))  # bound, not listening: connections are refused
        self.addCleanup(sock.close)
        # Windows refuses after ~2 s of retries: the default timeout leaves room for that
        self.assertEqual(app_main.port_owner(sock.getsockname()[1]), "other")

    def test_a_manager_still_starting_is_unsure(self):
        # make_server binds and listens before the serve thread starts: the connection is
        # accepted, but nothing answers yet.
        with tempfile.TemporaryDirectory() as tmp:
            settings = dataclasses.replace(config.Settings(), state=Path(tmp) / "state")
            httpd = server.make_server(server.App(settings, Path(tmp) / "settings.json"), 0)
            self.addCleanup(httpd.server_close)
            self.assertEqual(app_main.port_owner(httpd.server_address[1], timeout=0.5),
                             "unsure")


class PortTakenTests(NoRealDialogs):
    def _main(self, owner):
        with mock.patch.object(app_main.config, "load", return_value=self.settings), \
                mock.patch.object(app_main.server, "make_server", side_effect=OSError("in use")), \
                mock.patch.object(app_main, "port_owner", return_value=owner) as probe, \
                mock.patch.object(app_main, "open_window") as open_window:
            code = app_main.main()
        probe.assert_called_once_with(8645)
        return code, open_window

    def test_our_manager_on_the_port_shows_its_window(self):
        code, open_window = self._main("ours")
        self.assertEqual(code, 0)
        open_window.assert_called_once_with(self.settings, "http://127.0.0.1:8645/")
        self.message_box.assert_not_called()

    def test_no_clear_answer_shows_the_window_as_before(self):
        code, open_window = self._main("unsure")
        self.assertEqual(code, 0)
        open_window.assert_called_once_with(self.settings, "http://127.0.0.1:8645/")
        self.message_box.assert_not_called()

    def test_another_program_on_the_port_says_how_to_change_it(self):
        code, open_window = self._main("other")
        self.assertNotEqual(code, 0)
        open_window.assert_not_called()
        self.message_box.assert_called_once()
        text = self.message_box.call_args.args[0]
        self.assertIn("8645", text)
        self.assertIn('"port"', text)
        self.assertIn("settings.json", text)
        self.assertIn('{"port": 8646}', text)  # create the file when it is missing
        self.assertIn("missing", text)
        self.assertIn("8645", self.log())


class StartupErrorTests(NoRealDialogs):
    def _run(self, error):
        with mock.patch.object(app_main.config, "load", return_value=self.settings), \
                mock.patch.object(app_main, "main", side_effect=error):
            return app_main.run()

    def test_an_error_is_logged_and_shown_with_the_log_path(self):
        code = self._run(RuntimeError("boom"))
        self.assertEqual(code, 1)
        self.assertIn("Traceback", self.log())
        self.assertIn("RuntimeError: boom", self.log())
        self.message_box.assert_called_once()
        text = self.message_box.call_args.args[0]
        self.assertIn("could not start", text)
        self.assertIn(str(self.tmp / "state" / "logs" / "manager.log"), text)

    def test_an_error_after_serving_began_says_it_stopped(self):
        def fail(serving):
            serving.set()
            raise RuntimeError("late")

        with mock.patch.object(app_main.config, "load", return_value=self.settings), \
                mock.patch.object(app_main, "main", side_effect=fail):
            self.assertEqual(app_main.run(), 1)
        self.assertIn("RuntimeError: late", self.log())
        self.assertIn("Stopped because of an error", self.log())
        text = self.message_box.call_args.args[0]
        self.assertIn("stopped because of an error", text)
        self.assertNotIn("could not start", text)
        self.assertIn(str(self.tmp / "state" / "logs" / "manager.log"), text)

    def test_an_unwritable_log_still_shows_the_error(self):
        (self.tmp / "state").write_text("a file, not a folder", encoding="utf-8")
        code = self._run(PermissionError("no access"))
        self.assertEqual(code, 1)
        text = self.message_box.call_args.args[0]
        self.assertIn("no access", text)
        self.assertNotIn("manager.log", text)

    def test_a_clean_run_returns_mains_code(self):
        with mock.patch.object(app_main, "main", return_value=None):
            self.assertEqual(app_main.run(), 0)
        with mock.patch.object(app_main, "main", return_value=1):
            self.assertEqual(app_main.run(), 1)
        self.message_box.assert_not_called()

    def test_ctrl_c_is_not_an_error(self):
        self.assertEqual(self._run(KeyboardInterrupt()), 0)
        self.message_box.assert_not_called()


class MessageBoxTests(unittest.TestCase):
    def test_uses_user32_when_there(self):
        windll = mock.Mock()
        with mock.patch("ctypes.windll", windll, create=True):
            app_main.message_box("Hello.")
        windll.user32.MessageBoxW.assert_called_once()
        args = windll.user32.MessageBoxW.call_args.args
        self.assertEqual(args[1:3], ("Hello.", "Show Mod Manager"))
        self.assertEqual(args[3], 0x10 | 0x10000 | 0x40000)  # error icon, foreground, topmost

    def test_no_windll_is_quiet(self):
        windll = mock.Mock()
        windll.user32.MessageBoxW.side_effect = OSError("no user32")
        with mock.patch("ctypes.windll", windll, create=True):
            app_main.message_box("Hello.")  # no error
        with mock.patch("ctypes.windll", None, create=True):
            app_main.message_box("Hello.")  # AttributeError inside: no error


if __name__ == "__main__":
    unittest.main()
