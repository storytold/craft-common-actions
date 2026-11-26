#!/usr/bin/env python3
"""RFC-0018 rule 1.9 and 2.7: a Linux or FreeBSD package of a Craft CLI installs bash, zsh and fish
completions and a man page for the CLI and for each of its subcommands.

The package is a tar archive (.tar, .tar.gz, .tar.xz, .tar.zst, a FreeBSD .pkg), a .deb or an .rpm,
or a text file with one installed path per line (--listing). Only its file list is read."""
import argparse
import re
import subprocess
import sys

MAN_SUFFIX = r'\.1(\.(gz|xz|zst|bz2))?'
HELP_COMMANDS = re.compile(r'^Commands:\s*$')


def listing_of(package):
    """The installed paths of an archive, one string per member."""
    if package.endswith('.deb'):
        out = subprocess.run(['dpkg-deb', '-c', package], check=True, capture_output=True, text=True).stdout
        return [line.split()[-1] if ' -> ' not in line else line.split()[-3] for line in out.splitlines() if line.strip()]
    if package.endswith('.rpm'):
        out = subprocess.run(['rpm', '-qlp', package], check=True, capture_output=True, text=True).stdout
        return out.splitlines()
    out = subprocess.run(['tar', '-tf', package], check=True, capture_output=True, text=True).stdout
    return out.splitlines()


def subcommands_from_help(help_text):
    """The command names in the "Commands:" block of clap's --help, without clap's own `help`."""
    names = []
    inside = False
    for line in help_text.splitlines():
        if HELP_COMMANDS.match(line):
            inside = True
            continue
        if inside:
            if not line.strip():
                break
            token = line.split()[0]
            if token != 'help':
                names.append(token)
    return names


def required_patterns(cli, subcommands):
    """(description, regex) for each file the package must install."""
    name = re.escape(cli)
    wanted = [
        (f'bash completion share/bash-completion/completions/{cli}', rf'(^|/)share/bash-completion/completions/{name}$'),
        (f'zsh completion share/zsh/site-functions/_{cli}', rf'(^|/)share/zsh/site-functions/_{name}$'),
        (f'fish completion share/fish/vendor_completions.d/{cli}.fish', rf'(^|/)share/fish/vendor_completions\.d/{name}\.fish$'),
        (f'man page share/man/man1/{cli}.1', rf'(^|/)share/man/man1/{name}{MAN_SUFFIX}$'),
    ]
    for sub in subcommands:
        wanted.append((f'man page share/man/man1/{cli}-{sub}.1',
                       rf'(^|/)share/man/man1/{name}-{re.escape(sub)}{MAN_SUFFIX}$'))
    return wanted


def missing_files(paths, cli, subcommands):
    """The descriptions of the required files that no installed path matches."""
    return [text for text, pattern in required_patterns(cli, subcommands)
            if not any(re.search(pattern, p.strip()) for p in paths)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True, help='the binary name, for example photocraft-cli')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--package', help='the package archive')
    source.add_argument('--listing', help='a text file with one installed path per line')
    parser.add_argument('--subcommand', action='append', default=[], help='a subcommand that needs a man page (repeatable)')
    parser.add_argument('--binary', help='a built CLI: its --help "Commands:" block names the subcommands')
    args = parser.parse_args(argv)

    subcommands = list(args.subcommand)
    if args.binary:
        help_text = subprocess.run([args.binary, '--help'], check=True, capture_output=True, text=True).stdout
        found = subcommands_from_help(help_text)
        if not found:
            print(f'error: no "Commands:" block in `{args.binary} --help`', file=sys.stderr)
            return 1
        subcommands += [s for s in found if s not in subcommands]
    if args.listing:
        with open(args.listing, encoding='utf-8') as f:
            paths = f.read().splitlines()
    else:
        paths = listing_of(args.package)

    missing = missing_files(paths, args.cli, subcommands)
    for text in missing:
        print(f'error: the package does not install the {text}', file=sys.stderr)
    if not missing:
        print(f'{args.cli}: completions and {1 + len(subcommands)} man pages are installed')
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
