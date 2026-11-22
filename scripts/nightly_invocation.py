#!/usr/bin/env python3
"""Credential-free admission for the reviewed PhotoCraft nightly caller.

Manual actors must be a reviewed literal JSON array in caller workflow source,
never an untrusted dispatch input. This prevents accidental use, not a writer
from editing or bypassing the workflow. It does not admit platforms or app code.
"""
from collections.abc import Mapping
import json
import os
import re
import sys

MAX_ACTORS_BYTES = 4096
MAX_ACTORS = 64
MAX_LOGIN_LENGTH = 39
MAX_ATTEMPT_DIGITS = 20
LOGIN = re.compile(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*')
ATTEMPT = re.compile(r'[1-9][0-9]{0,19}')


def valid_login(value):
    """GitHub login grammar, with an optional case-insensitive [bot] suffix."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_LOGIN_LENGTH + 5:
        return False
    base = value[:-5] if value.lower().endswith('[bot]') else value
    return 0 < len(base) <= MAX_LOGIN_LENGTH and LOGIN.fullmatch(base) is not None


def manual_actors(actors_json):
    """Bound parsing before allocation and return distinct normalized logins."""
    if not isinstance(actors_json, str) or len(actors_json) > MAX_ACTORS_BYTES:
        raise ValueError('manual actor JSON must be a bounded string')
    try:
        if len(actors_json.encode('utf-8')) > MAX_ACTORS_BYTES:
            raise ValueError('manual actor JSON exceeds byte limit')
    except UnicodeEncodeError:
        raise ValueError('manual actor JSON contains invalid Unicode') from None
    try:
        actors = json.loads(actors_json)
    except (ValueError, RecursionError):
        raise ValueError('manual actor JSON is invalid') from None
    if not isinstance(actors, list) or len(actors) > MAX_ACTORS:
        raise ValueError('manual actors must be a bounded JSON array')
    normalized = set()
    for actor in actors:
        if not valid_login(actor):
            raise ValueError('manual actors contain an invalid login')
        login = actor.lower()
        if login in normalized:
            raise ValueError('manual actors contain duplicate logins')
        normalized.add(login)
    return normalized


def require_invocation(environment, actors_json='[]'):
    """Allow the initial schedule, or a reviewed current manual/re-run actor.

    Never fall back to GITHUB_ACTOR: it identifies the original initiator, not the
    person requesting a re-run. Invalid configuration also refuses initial runs.
    """
    actors = manual_actors(actors_json)
    if not isinstance(environment, Mapping):
        raise ValueError('invocation environment must be a mapping')
    if environment.get('GITHUB_REPOSITORY') != 'storytold/photocraft':
        raise ValueError('invocation requires the reviewed PhotoCraft repository')
    if environment.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('invocation requires the main branch')
    event = environment.get('GITHUB_EVENT_NAME')
    if event not in ('schedule', 'workflow_dispatch'):
        raise ValueError('invocation requires schedule or workflow dispatch')
    actor = environment.get('GITHUB_TRIGGERING_ACTOR')
    if not valid_login(actor):
        raise ValueError('invocation requires a valid current triggering actor')
    attempt = environment.get('GITHUB_RUN_ATTEMPT')
    if not isinstance(attempt, str) or len(attempt) > MAX_ATTEMPT_DIGITS or not ATTEMPT.fullmatch(attempt):
        raise ValueError('invocation requires a bounded positive run attempt')
    if (event == 'workflow_dispatch' or attempt != '1') and actor.lower() not in actors:
        raise ValueError('current triggering actor is not approved for manual runs or re-runs')
    return True


def main():
    try:
        require_invocation(os.environ, os.environ.get('NIGHTLY_MANUAL_ACTORS', '[]'))
    except ValueError as error:
        # Diagnostics are fixed text: never echo environment or JSON contents.
        print(f'nightly invocation refused: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
