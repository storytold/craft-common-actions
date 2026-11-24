#!/usr/bin/env python3
"""App-specific nightly Linux package names, derived from the app name, never hard-coded.

The nightly package identity is the reviewed registry's Linux `package_identity` when the app
has one, else ai.storyteller.<app>.nightly (the stable bundle ID plus .nightly, as
actions/arch-package derives it). deb and rpm packages are <app>-nightly; the tarball, the
binary and the Arch alias are <app>.

  names --app-name APP [--registry nightly-apps.json]   print KEY=VALUE lines (for $GITHUB_OUTPUT)
  check --app-name APP --kind deb|rpm|arch|flatpak --value VALUE [--registry ...]
"""
import argparse
import json
from pathlib import Path
import re
import sys

APP = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
IDENTITY = re.compile(r"[A-Za-z0-9_.-]{1,255}")


def identity(app, registry=None):
    if registry is not None:
        record = registry.get("apps", {}).get(app, {})
        found = {target.get("isolation", {}).get("package_identity") for target in record.get("targets", [])
                 if target.get("target", {}).get("os") == "linux"}
        found.discard(None)
        if len(found) > 1:
            raise ValueError(f"{app} has more than one reviewed Linux package identity")
        if found:
            value = found.pop()
            if not (isinstance(value, str) and IDENTITY.fullmatch(value) and value.endswith(".nightly")):
                raise ValueError(f"invalid reviewed package identity for {app}: {value!r}")
            return value
    return f"ai.storyteller.{app}.nightly"


def names(app, registry=None):
    if not (isinstance(app, str) and APP.fullmatch(app)):
        raise ValueError(f"invalid app name: {app!r}")
    ident = identity(app, registry)
    return {"app": app, "binary": app, "tarball-prefix": app, "deb": f"{app}-nightly", "rpm": f"{app}-nightly",
            "arch": ident, "arch-stable": ident[:-len(".nightly")], "flatpak": ident}


def check(app, kind, value, registry=None):
    expected = names(app, registry)
    if kind == "flatpak":
        # An OCI org.flatpak.ref label: app/<id>/<arch>/<branch>.
        ok = isinstance(value, str) and value.startswith("app/" + expected["flatpak"] + "/")
        want = "app/" + expected["flatpak"] + "/..."
    else:
        ok = value == expected[kind]
        want = expected[kind]
    if not ok:
        raise ValueError(f"{kind} name is {value!r}, expected {want!r} for {app}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("names", "check"))
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--registry")
    parser.add_argument("--kind", choices=("deb", "rpm", "arch", "flatpak"))
    parser.add_argument("--value")
    args = parser.parse_args(argv)
    registry = json.loads(Path(args.registry).read_text(encoding="utf-8")) if args.registry else None
    try:
        if args.mode == "names":
            for key, value in names(args.app_name, registry).items():
                print(f"{key}={value}")
        else:
            if args.kind is None or args.value is None:
                parser.error("check needs --kind and --value")
            check(args.app_name, args.kind, args.value, registry)
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
