import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import procs

TASKLIST = (
    '"mlb.exe","4120","Console","1","1,234,567 K"\r\n'
    '"mlb.exe","9736","Console","1","2,000 K"\r\n'
)
TASKLIST_NONE = "INFO: No tasks are running which match the specified criteria.\r\n"

WMIC = (
    "ExecutablePath                                   ProcessId  \r\n"
    r"C:\Games\MLB The Show 26\mlb.exe               4120       " "\r\n"
    "                                                 9736       \r\n"
    "\r\n"
)

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1000
  TCP    127.0.0.1:10010        127.0.0.1:50000        ESTABLISHED     1234
  TCP    127.0.0.1:10010        0.0.0.0:0              LISTENING       9736
  TCP    [::]:8645              [::]:0                 LISTENING       5555
  UDP    127.0.0.1:10011        *:*                                    4444
"""


class PythonExeTests(unittest.TestCase):
    """Child Pythons run under python.exe even when the manager runs under pythonw.exe."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "Python 3"
        self.dir.mkdir()
        (self.dir / "pythonw.exe").write_bytes(b"")

    def test_pythonw_swaps_to_python_beside_it(self):
        (self.dir / "python.exe").write_bytes(b"")
        with mock.patch("sys.executable", str(self.dir / "pythonw.exe")):
            self.assertEqual(procs.python_exe(), str(self.dir / "python.exe"))
        with mock.patch("sys.executable", str(self.dir / "PythonW.EXE")):
            self.assertEqual(procs.python_exe(), str(self.dir / "python.exe"))

    def test_pythonw_without_python_beside_it_stays(self):
        with mock.patch("sys.executable", str(self.dir / "pythonw.exe")):
            self.assertEqual(procs.python_exe(), str(self.dir / "pythonw.exe"))

    def test_python_stays(self):
        (self.dir / "python.exe").write_bytes(b"")
        with mock.patch("sys.executable", str(self.dir / "python.exe")):
            self.assertEqual(procs.python_exe(), str(self.dir / "python.exe"))

    def test_python_argv_starts_a_script_through_the_boot(self):
        with mock.patch("app.procs.python_exe", return_value=r"C:\Py\python.exe"):
            self.assertEqual(procs.python_argv(["python", "-E", "-s", "s.py", "status", "x"]),
                             [r"C:\Py\python.exe", "-E", "-s", "-c", procs.BOOT, "s.py", "status", "x"])
            self.assertEqual(procs.python_argv(["python", "-X", "utf8", r"C:\m\T.PY"]),
                             [r"C:\Py\python.exe", "-X", "utf8", "-c", procs.BOOT, r"C:\m\T.PY"])
            self.assertEqual(procs.python_argv(["python", "s.py"]),
                             [r"C:\Py\python.exe", "-c", procs.BOOT, "s.py"])

    def test_non_script_argv_unchanged(self):
        with mock.patch("app.procs.python_exe", return_value=r"C:\Py\python.exe"):
            for argv in (["-u", "-c", "print(1)", "a.py"], ["-m", "http.server", "x.py"],
                         ["-Ec", "print(1)"], ["tool.exe", "a.py"], ["-", "a.py"],
                         ["--version"], []):
                with self.subTest(argv=argv):
                    self.assertEqual(procs.python_argv(["python", *argv]),
                                     [r"C:\Py\python.exe", *argv])
            self.assertEqual(procs.python_argv(["node", "python", "a.py"]), ["node", "python", "a.py"])
            self.assertEqual(procs.python_argv([]), [])


EMBEDDED_PYTHON = Path(__file__).resolve().parents[2] / "MLBTS-Setup-0.2.0" / "python" / "python.exe"


class ScriptBootTests(unittest.TestCase):
    """Scripts started through python_argv see their own folder, argv, exit code and output."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "mod tools"
        self.dir.mkdir()
        self.elsewhere = Path(self._tmp.name) / "elsewhere"
        self.elsewhere.mkdir()

    def _run(self, argv, cwd, python=None):
        exe = python or procs.python_exe()
        with mock.patch("app.procs.python_exe", return_value=exe):
            full = procs.python_argv(argv)
        return subprocess.run(full, cwd=cwd, capture_output=True, text=True, timeout=60,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    def _check_sibling_import(self, python=None):
        (self.dir / "sibling_helper_q7.py").write_text("VALUE = 'helper says hi'\n")
        (self.dir / "main.py").write_text(
            "import sys\nimport sibling_helper_q7\nprint(sibling_helper_q7.VALUE)\n"
            "print(sys.path[0])\n")
        # As the manifests do (relative name, cwd = its folder), and by full path from elsewhere.
        for script, cwd in (("main.py", self.dir), (str(self.dir / "main.py"), self.elsewhere)):
            with self.subTest(script=script):
                result = self._run(["python", "-E", "-s", script], cwd, python)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["helper says hi", str(self.dir)])

    def test_script_imports_its_sibling(self):
        self._check_sibling_import()

    @unittest.skipUnless(EMBEDDED_PYTHON.is_file(), "no embeddable Python on this PC")
    def test_script_imports_its_sibling_under_embeddable_python(self):
        self._check_sibling_import(str(EMBEDDED_PYTHON))

    def test_exit_code_and_argv_pass_through(self):
        (self.dir / "s.py").write_text(
            "import json, sys\n"
            "print(json.dumps({'argv': sys.argv, 'file': __file__, 'name': __name__,\n"
            "                  'E': sys.flags.ignore_environment, 's': sys.flags.no_user_site}))\n"
            "sys.stderr.write('to stderr\\n')\n"
            "sys.exit(3)\n")
        result = self._run(["python", "-E", "-s", "s.py", "status", "with space", "--x"], self.dir)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr, "to stderr\n")
        seen = json.loads(result.stdout)
        self.assertEqual(seen["argv"][1:], ["status", "with space", "--x"])
        self.assertEqual((self.dir / seen["argv"][0]).resolve(), (self.dir / "s.py").resolve())
        self.assertEqual(seen["file"], str(self.dir / "s.py"))
        self.assertEqual(seen["name"], "__main__")
        self.assertEqual((seen["E"], seen["s"]), (1, 1))


class ProcsTests(unittest.TestCase):
    def test_running_games_parses_tasklist(self):
        self.assertEqual(procs.parse_tasklist(TASKLIST), [4120, 9736])
        self.assertEqual(procs.parse_tasklist(TASKLIST_NONE), [])
        self.assertEqual(procs.parse_wmic(WMIC), {
            4120: r"C:\Games\MLB The Show 26\mlb.exe",
            9736: "",
        })

    def test_port_owner_parses_netstat(self):
        self.assertEqual(procs.parse_netstat(NETSTAT, 10010), 9736)
        self.assertEqual(procs.parse_netstat(NETSTAT, 8645), 5555)
        self.assertIsNone(procs.parse_netstat(NETSTAT, 10011))
        self.assertIsNone(procs.parse_netstat(NETSTAT, 10000))
        self.assertEqual(
            procs.parse_netstat("TCP 127.0.0.1:10010 0.0.0.0:0 LISTENING 9736", 10010), 9736)

    def test_port_owner_ignores_other_hosts(self):
        text = "  TCP    192.168.1.5:10010      0.0.0.0:0              LISTENING       77\n"
        self.assertIsNone(procs.parse_netstat(text, 10010))


if __name__ == "__main__":
    unittest.main()
