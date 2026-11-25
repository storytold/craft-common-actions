#!/usr/bin/env python3
"""Every app-specific nightly name, derived from the app's reviewed registry row.

The registry is always this checkout's own nightly-apps.json; no path is taken from the
command line. A row that has nightly targets also carries:

  display_name  "PhotoCraft"                     the version line, bundle and product names
  env_prefix    "PHOTOCRAFT"                     <PREFIX>_BUILD_CHANNEL/VERSION/BUILD_SHA/... for the build
  entrypoints   {platform: {"entrypoint", "binary"}}
                entrypoint: what the archive starts (Linux may use a short launcher script);
                binary: the GUI executable itself, which the smoke runs directly.

Commands:
  names   --app-name APP                         KEY=VALUE lines for $GITHUB_OUTPUT (#360's Linux names)
  check   --app-name APP --kind deb|rpm|arch|flatpak --value VALUE
  admit   --app-name APP --repository OWNER/REPO --ref REF
                                                 ok=true only for a reviewed row with targets on its main
  env     --app-name APP --version V --sha SHA --date YYYY-MM-DD
                                                 <PREFIX>_* lines for $GITHUB_ENV
  path    --app-name APP --platform P --kind entrypoint|binary
"""
import argparse
import json
from pathlib import Path
import re
import sys

# Workflows run this with `python3 -I`, which leaves the script's own directory off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import nightly_feed as n  # noqa: E402

REGISTRY = Path(__file__).resolve().parents[1] / "nightly-apps.json"
APP = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
DISPLAY = re.compile(r"[A-Z][A-Za-z0-9]{0,31}")
PREFIX = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
IDENTITY = re.compile(r"[A-Za-z0-9_.-]{1,255}")
PLATFORMS = {"linux-x86_64": ("linux", "x86_64"), "macos-aarch64": ("macos", "aarch64"), "windows-x86_64": ("windows", "x86_64")}
EXTENSIONS = {"linux-x86_64": ".tar.gz", "macos-aarch64": ".zip", "windows-x86_64": ".zip"}
CHANNEL = "nightly-v1"


def load_registry():
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def app_name(app):
    n.require(isinstance(app, str) and APP.fullmatch(app) is not None, f"invalid app name: {app!r}")
    return app


def identity(app, registry=None):
    """The nightly package identity: the reviewed row's Linux package_identity, else ai.storyteller.<app>.nightly."""
    app_name(app)
    if registry is not None:
        record = registry.get("apps", {}).get(app, {})
        found = {target.get("isolation", {}).get("package_identity") for target in record.get("targets", [])
                 if target.get("target", {}).get("os") == "linux"}
        found.discard(None)
        n.require(len(found) <= 1, f"{app} has more than one reviewed Linux package identity")
        if found:
            value = found.pop()
            n.require(isinstance(value, str) and IDENTITY.fullmatch(value) is not None and value.endswith(".nightly"),
                      f"invalid reviewed package identity for {app}: {value!r}")
            return value
    return f"ai.storyteller.{app}.nightly"


def relative(value, where):
    n.require(isinstance(value, str) and 0 < len(value) <= 255 and "\\" not in value and "\0" not in value and
              not value.startswith("/") and all(part not in ("", ".", "..") for part in value.split("/")),
              f"invalid {where} path")
    return value


def row(app, registry=None):
    """The reviewed row with nightly targets and its display name, env prefix and entrypoints."""
    registry = load_registry() if registry is None else registry
    record = n.app_record(registry, app_name(app))
    n.require(record["targets"], f"{app} has no reviewed nightly targets")
    display, prefix, entrypoints = record.get("display_name"), record.get("env_prefix"), record.get("entrypoints")
    n.require(isinstance(display, str) and DISPLAY.fullmatch(display) is not None, f"{app} has no valid display_name")
    n.require(isinstance(prefix, str) and PREFIX.fullmatch(prefix) is not None, f"{app} has no valid env_prefix")
    platforms = target_platforms(record)
    n.require(isinstance(entrypoints, dict) and set(platforms) <= set(entrypoints) <= set(PLATFORMS),
              f"{app} entrypoints must cover every target platform")
    for platform, value in entrypoints.items():
        n.fields(value, {"entrypoint", "binary"}, f"{app} {platform} entrypoint")
        relative(value["entrypoint"], "entrypoint"); relative(value["binary"], "binary")
    return record


def target_platforms(record):
    found = []
    for item in record["targets"]:
        target = item.get("target", {}) if isinstance(item, dict) else {}
        key = next((name for name, pair in PLATFORMS.items() if pair == (target.get("os"), target.get("arch"))), None)
        n.require(key is not None, "unsupported nightly target platform")
        found.append(key)
    n.require(len(found) == len(set(found)), "duplicate nightly target platform")
    return sorted(found)


def names(app, registry=None):
    """#360's Linux package names (no row extension required, so any reviewed app name works)."""
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
    n.require(ok, f"{kind} name is {value!r}, expected {want!r} for {app}")


class App:
    """Derived names for one admitted app."""

    def __init__(self, app, registry=None):
        self.registry = load_registry() if registry is None else registry
        self.record = row(app, self.registry)
        self.app = app
        self.display = self.record["display_name"]
        self.prefix = self.record["env_prefix"]
        self.identity = identity(app, self.registry)
        self.platforms = target_platforms(self.record)
        self.repository = self.record["repository"]

    def asset(self, version, platform):
        return f"{self.app}-{version}-{platform}-nightly{EXTENSIONS[platform]}"

    def asset_pattern(self, platform):
        return self.asset("{version}", platform)

    def entrypoint(self, platform):
        return self.record["entrypoints"][platform]["entrypoint"]

    def binary(self, platform):
        return self.record["entrypoints"][platform]["binary"]

    def version_line(self, version):
        return f"{self.display} {version} ({CHANNEL})\n"

    @property
    def bundle(self):
        return f"{self.display} Nightly.app"

    @property
    def product_name(self):
        return f"{self.display} Nightly"

    @property
    def plist_version_key(self):
        return f"{self.display}Version"

    @property
    def banned(self):
        return {f"{self.app}-cli", f"{self.app}-cli.exe"}

    def build_env(self, version, sha, date, with_override=False):
        n.require(n.HEX40.fullmatch(sha or "") is not None and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date or "") is not None and
                  isinstance(version, str) and "-nightly." in version and "\n" not in version, "invalid nightly build identity")
        result = {f"{self.prefix}_BUILD_CHANNEL": CHANNEL, f"{self.prefix}_VERSION": version}
        if with_override:
            result[f"{self.prefix}_OVERRIDE_VERSION"] = version
        result.update({f"{self.prefix}_BUILD_SHA": sha, f"{self.prefix}_BUILD_DATE": date})
        return result


DEFAULT_APP = "photocraft"


def current_name(environment=None):
    """The app this run is for: NIGHTLY_APP, which the shared workflows set from the caller's
    app-name input. Unset means PhotoCraft, the only app admitted before app-name existed."""
    import os
    value = (os.environ if environment is None else environment).get("NIGHTLY_APP") or DEFAULT_APP
    return app_name(value)


def current(environment=None, registry=None):
    return App(current_name(environment), registry)


def admit(app, repository, ref, registry=None, release=False):
    """The allowlist: a reviewed row with targets, for its own repository, on its default branch
    (or, for the release-ref signer, on its protected release branch)."""
    try:
        record = row(app, registry)
    except (n.InvalidNightly, KeyError, TypeError) as error:
        return False, str(error)
    if repository != record["repository"]:
        return False, f"{repository} is not {app}'s reviewed repository {record['repository']}"
    # The signer runs on a protected signer branch (the policy names which); the producer on main.
    branches = ("release", "nightly-signer") if release else (record["default_branch"],)
    if ref not in ["refs/heads/" + branch for branch in branches]:
        return False, f"{ref} is not {app}'s {'signer' if release else 'default'} branch"
    return True, "admitted"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("names", "check", "admit", "env", "path"))
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--kind", choices=("deb", "rpm", "arch", "flatpak", "entrypoint", "binary"))
    parser.add_argument("--value")
    parser.add_argument("--repository"); parser.add_argument("--ref")
    parser.add_argument("--version"); parser.add_argument("--sha"); parser.add_argument("--date")
    parser.add_argument("--platform", choices=tuple(PLATFORMS))
    parser.add_argument("--with-override", action="store_true", help="also <PREFIX>_OVERRIDE_VERSION (distro packages)")
    parser.add_argument("--export", action="store_true", help="print shell `export NAME=value` lines")
    parser.add_argument("--release-ref", action="store_true", help="admit on the protected release branch (signer)")
    parser.add_argument("--require", action="store_true", help="exit 1 unless admitted")
    args = parser.parse_args(argv)
    try:
        registry = load_registry()
        if args.mode == "names":
            lines = names(args.app_name, registry)
        elif args.mode == "check":
            if args.kind not in ("deb", "rpm", "arch", "flatpak") or args.value is None:
                parser.error("check needs --kind deb|rpm|arch|flatpak and --value")
            check(args.app_name, args.kind, args.value, registry); lines = {}
        elif args.mode == "admit":
            if args.repository is None or args.ref is None:
                parser.error("admit needs --repository and --ref")
            ok, reason = admit(args.app_name, args.repository, args.ref, registry, args.release_ref)
            print(f"::notice::nightly admission for {args.app_name}: {reason}", file=sys.stderr)
            n.require(ok or not args.require, f"nightly admission refused: {reason}")
            lines = {"ok": "true" if ok else "false"}
            if ok:
                app = App(args.app_name, registry)
                lines.update({"display-name": app.display, "env-prefix": app.prefix, "identity": app.identity})
        elif args.mode == "env":
            if None in (args.version, args.sha, args.date):
                parser.error("env needs --version, --sha and --date")
            lines = App(args.app_name, registry).build_env(args.version, args.sha, args.date, args.with_override)
            if args.export:
                import shlex
                for key, value in lines.items():
                    n.require("\n" not in value and "\r" not in value, "invalid output value")
                    print(f"export {key}={shlex.quote(value)}")
                return 0
        else:
            if args.platform is None or args.kind not in ("entrypoint", "binary"):
                parser.error("path needs --platform and --kind entrypoint|binary")
            app = App(args.app_name, registry)
            n.require(args.platform in app.platforms, f"{args.platform} is not a target of {args.app_name}")
            print(getattr(app, args.kind)(args.platform)); return 0
        for key, value in lines.items():
            n.require("\n" not in value and "\r" not in value, "invalid output value")
            print(f"{key}={value}")
    except (n.InvalidNightly, ValueError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
