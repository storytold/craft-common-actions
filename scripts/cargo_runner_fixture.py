"""Experimental fixture-only Cargo runner; not an application runtime driver."""
import hashlib
import json
import os
import pathlib
import sys


def checked_args(executable, args, manifest, cwd):
    path = pathlib.Path(executable).resolve(strict=True)
    entry = manifest.get(str(path))
    if entry is None or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('unknown or oversized fixture executable')
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        raise ValueError('fixture executable changed')
    if pathlib.Path(cwd).resolve() != pathlib.Path(entry['cwd']).resolve():
        raise ValueError('wrong fixture package cwd')
    result = list(args)
    if entry['serial']:
        values = []
        for i, arg in enumerate(args):
            if arg == '--test-threads':
                if i + 1 == len(args):
                    raise ValueError('missing thread count')
                values.append(args[i + 1])
            elif arg.startswith('--test-threads='):
                values.append(arg.split('=', 1)[1])
        if values and values != ['1']:
            raise ValueError('conflicting thread count')
        if not values:
            result += ['--test-threads=1']
    return str(path), result


def main():
    manifest = pathlib.Path(os.environ['FIXTURE_ALLOWLIST'])
    if manifest.stat().st_size > 65536:
        raise ValueError('oversized allowlist')
    path, args = checked_args(sys.argv[1], sys.argv[2:], json.loads(manifest.read_text()), os.getcwd())
    with open(os.environ['FIXTURE_RUNNER_LOG'], 'a') as log:
        log.write(json.dumps({'executable': path, 'args': args, 'cwd': os.getcwd()}) + '\n')
    # exec preserves Cargo's environment/cwd and delivers signals directly.
    # No descendant isolation, resource sandbox, or runtime budget is claimed.
    os.execv(path, [path, *args])


if __name__ == '__main__':
    main()
