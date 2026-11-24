"""Process and port queries (tasklist / wmic / netstat), with pure text parsers."""
import csv
import subprocess
import sys
from pathlib import Path

GAME_IMAGE = "mlb.exe"
_LOCAL_HOSTS = {"127.0.0.1", "0.0.0.0", "[::]", "[::1]"}


def python_exe() -> str:
    """The Python to run child scripts with: python.exe beside pythonw.exe when the
    manager runs windowless (a GUI-subsystem parent would give each console child,
    e.g. ninja under tools/build.py, its own visible console and lose its output)."""
    exe = Path(sys.executable)
    if exe.name.lower().startswith("pythonw"):
        console = exe.with_name("python.exe")
        if console.is_file():
            return str(console)
    return sys.executable


# Runs sys.argv[1] (a script) the way `python script.py` would, but with the script's own
# folder first on sys.path even under an embeddable Python, whose ._pth file keeps it off.
# The -c entry "" (the working folder) is dropped, as it is for a script. __file__ is the
# script's full path; sys.argv is [script, *args], runpy giving argv[0] that full path too.
BOOT = ("import os,runpy,sys;s=os.path.abspath(sys.argv[1]);sys.argv=sys.argv[1:];"
        "sys.path[:]=[os.path.dirname(s)]+[p for p in sys.path if p!=''];"
        "runpy.run_path(s,run_name='__main__')")

_FLAGS_WITH_VALUE = "XW"   # -X opt / -W arg: the value is the rest of the word or the next word
_FLAGS_ENDING_OPTIONS = "cm"   # -c cmd / -m mod: no script follows


def _script_index(args: list[str]) -> int | None:
    """Index in `args` (what follows "python") of a .py script, or None when the command
    is something else (-c, -m, stdin, a long option, a non-.py file)."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and arg != "-" and not arg.startswith("--"):
            for pos, letter in enumerate(arg[1:], 1):
                if letter in _FLAGS_ENDING_OPTIONS:
                    return None
                if letter in _FLAGS_WITH_VALUE:
                    if pos == len(arg) - 1:
                        i += 1
                    break
            i += 1
            continue
        return i if arg.lower().endswith(".py") else None
    return None


def python_argv(argv: list[str]) -> list[str]:
    """argv with a leading "python" (a manifest's word for "the manager's Python")
    replaced by python_exe(). A script (`python [flags] x.py args`) starts through BOOT,
    so it can import the modules beside it under any Python; flags stay before -c."""
    if not argv or argv[0] != "python":
        return list(argv)
    args = list(argv[1:])
    at = _script_index(args)
    if at is None:
        return [python_exe()] + args
    return [python_exe()] + args[:at] + ["-c", BOOT] + args[at:]


def failure_text(head: str, output: str, tail_lines: int) -> str:
    """A failure as one useful line (head, the output's last non-empty line, where to
    look) followed by the output's last `tail_lines` lines."""
    lines = output.splitlines()[-tail_lines:]
    last = next((line.strip() for line in reversed(lines) if line.strip()), "")
    first = f"{head}: {last} — see Builds & Logs" if last else f"{head} — see Builds & Logs"
    return "\n".join([first] + lines)


def _run(argv: list[str]) -> str:
    """Run a command and return its stdout; any failure gives ""."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def parse_tasklist(text: str) -> list[int]:
    """PIDs of mlb.exe rows in `tasklist /FO CSV /NH` output."""
    pids = []
    for row in csv.reader(text.splitlines()):
        if len(row) >= 2 and row[0].lower() == GAME_IMAGE and row[1].strip().isdigit():
            pids.append(int(row[1]))
    return pids


def parse_wmic(text: str) -> dict[int, str]:
    """{pid: exe path} from `wmic ... get ProcessId,ExecutablePath` output."""
    exes = {}
    for line in text.splitlines():
        parts = line.strip().rsplit(None, 1)
        if not parts or not parts[-1].isdigit():
            continue
        exes[int(parts[-1])] = parts[0].strip() if len(parts) == 2 else ""
    return exes


def parse_netstat(text: str, port: int) -> int | None:
    """PID of the TCP socket LISTENING on a local address at `port`, else None."""
    for line in text.splitlines():
        cols = line.split()
        if len(cols) < 5 or cols[0].upper() != "TCP" or cols[3].upper() != "LISTENING":
            continue
        host, _, local_port = cols[1].rpartition(":")
        if host in _LOCAL_HOSTS and local_port == str(port) and cols[4].isdigit():
            return int(cols[4])
    return None


def running_games() -> list[dict]:
    """Every running mlb.exe as {"pid": int, "exe": str} ("" when the path is unknown)."""
    pids = parse_tasklist(_run(["tasklist", "/FO", "CSV", "/NH",
                                "/FI", f"IMAGENAME eq {GAME_IMAGE}"]))
    if not pids:
        return []
    exes = parse_wmic(_run(["wmic", "process", "where", f"name='{GAME_IMAGE}'",
                            "get", "ProcessId,ExecutablePath"]))
    return [{"pid": pid, "exe": exes.get(pid, "")} for pid in pids]


def port_owner(port: int) -> int | None:
    """PID listening on 127.0.0.1:port (or a wildcard address), from `netstat -ano`."""
    return parse_netstat(_run(["netstat", "-ano"]), port)
