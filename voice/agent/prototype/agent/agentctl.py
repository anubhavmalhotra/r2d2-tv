"""r2d2-agentctl (kill switch levels 2 and 3). Offline prototype.

    python3 -m agent.agentctl --config-dir DIR {pause|resume|tv-off|tv-on|status}

--config-dir is required so the prototype can never touch a real
~/.config/r2d2-agent by default. `handoff arm` needs IPC to a running agent and
is not part of P0 (see bridge.HandoffWindow.arm).
"""
import argparse
import sys

from .killswitch import KillSwitch


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(prog='r2d2-agentctl')
    parser.add_argument('--config-dir', required=True)
    parser.add_argument('command', choices=('pause', 'resume', 'tv-off', 'tv-on', 'status'))
    args = parser.parse_args(argv)
    switch = KillSwitch(args.config_dir)
    if args.command == 'pause':
        ok = switch.pause('OWNER')
    elif args.command == 'resume':
        switch.resume()
        ok = True
    elif args.command == 'tv-off':
        ok = switch.set_tv_off('OWNER')
    elif args.command == 'tv-on':
        switch.set_tv_on()
        ok = True
    else:
        ok = True
    out.write('paused=%s tv_off=%s\n' % (switch.paused(), switch.tv_off()))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
