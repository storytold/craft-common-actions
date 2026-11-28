#!/usr/bin/env python3
"""RFC-0018 rules 1.9 and 2.7: a Linux or FreeBSD package of a Craft CLI installs bash, zsh and fish
completions and a man page for the CLI and for each of its subcommands. Man pages sit in `man/man1`
under `share` (Linux, and FreeBSD packages that use it) or under the prefix itself (a FreeBSD tarball or
port: `man/man1`), so either is accepted.

Reads the package's file list on standard input, one installed path per line: the action lists a
tar archive, a FreeBSD .pkg, a .deb or an .rpm with the tool that opens it and pipes the list here."""
import argparse
import re
import sys

MAN_SUFFIX = r'\.1(\.(gz|xz|zst|bz2))?'


def required_patterns(cli, subcommands):
    """(description, regex) for each file the package must install."""
    name = re.escape(cli)
    wanted = [
        (f'bash completion share/bash-completion/completions/{cli}', rf'(^|/)share/bash-completion/completions/{name}$'),
        (f'zsh completion share/zsh/site-functions/_{cli}', rf'(^|/)share/zsh/site-functions/_{name}$'),
        (f'fish completion share/fish/vendor_completions.d/{cli}.fish', rf'(^|/)share/fish/vendor_completions\.d/{name}\.fish$'),
        (f'man page man/man1/{cli}.1', rf'(^|/)man/man1/{name}{MAN_SUFFIX}$'),
    ]
    for sub in subcommands:
        wanted.append((f'man page man/man1/{cli}-{sub}.1',
                       rf'(^|/)man/man1/{name}-{re.escape(sub)}{MAN_SUFFIX}$'))
    return wanted


def missing_files(paths, cli, subcommands):
    """The descriptions of the required files that no installed path matches."""
    return [text for text, pattern in required_patterns(cli, subcommands)
            if not any(re.search(pattern, p.strip()) for p in paths)]


def main(argv=None, stdin=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True, help='the binary name, for example photocraft-cli')
    parser.add_argument('--subcommand', action='append', default=[], help='a subcommand that needs a man page (repeatable)')
    args = parser.parse_args(argv)

    paths = (stdin or sys.stdin).read().splitlines()
    if not any(p.strip() for p in paths):
        print('error: the package file list is empty', file=sys.stderr)
        return 1
    missing = missing_files(paths, args.cli, args.subcommand)
    for text in missing:
        print(f'error: the package does not install the {text}', file=sys.stderr)
    if not missing:
        print(f'{args.cli}: completions and {1 + len(args.subcommand)} man pages are installed')
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
