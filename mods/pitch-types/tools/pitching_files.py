r"""Pitching++ file step (the Show Mod Manager's mod.json "files"): the delivery copies, the player editor's list and
the Airbender's names (its pitch name, the editor's pitch label, Road to the Show's pitch tile).

  install    deliveries_install.py install (the delivery copies, clips and slide steps), then, only if that worked,
             delivery_menu.py install (the copies in the player editor's delivery list), then airbender_files.py install
  uninstall  airbender_files.py uninstall, delivery_menu.py uninstall (the list never names a delivery the game lacks), then
             deliveries_install.py uninstall; stops at the first that fails
  status     all three, deliveries first ("deliveries: installed" ... "delivery menu: installed (...)" ... "airbender: installed")

Each runs as its own process with this Python and its flags; the exit code is the first non-zero one. The game must be
closed. Extra arguments (--game, and deliveries_install.py's --pack / --slides) go to both scripts that take them.
usage: python -E -s pitching_files.py install|uninstall|status [--game "C:\Games\MLB The Show 26"] [--pack f] [--slides f]
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(script, action, extra):
    sys.stdout.flush()
    return subprocess.run([sys.executable, '-E', '-s', str(HERE / script), action] + extra, cwd=str(HERE)).returncode


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ('install', 'uninstall', 'status'):
        raise SystemExit(__doc__)
    action, extra = args[0], args[1:]
    menu_extra = extra[extra.index('--game'):extra.index('--game') + 2] if '--game' in extra else []
    steps = [('deliveries_install.py', extra), ('delivery_menu.py', menu_extra), ('airbender_files.py', menu_extra)]
    if action == 'uninstall':
        steps.reverse()
    for script, ex in steps:
        code = run(script, action, ex)
        if code and action != 'status':
            sys.exit(code)
        if code:
            print('%s status: exit %d' % (script, code))
    sys.exit(0)


if __name__ == '__main__':
    main()
