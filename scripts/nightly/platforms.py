#!/usr/bin/env python3
"""Windows and macOS launcher signers, their keyless verifiers and the signed-payload smoke test.

    platforms sign   --platform P --admission DIR --input DIR --source DIR --output DIR
    platforms verify --platform P --admission DIR --input DIR [--output DIR]
    platforms smoke  --platform P --admission DIR --input DIR [--source DIR] [--output DIR]

sign reads the admitted producer payload from --input (re-hashed against the handoff manifest),
signs it with the app's checked-out signing scripts under --source, and writes only the final
asset to --output (a new directory). verify and smoke read --input holding exactly that final
asset; --output is an optional new work directory. Expected names, the GUI executable,
ProductName and the publisher or team come from the nightly registry, never from inputs.

Payload code never runs here except in smoke, which gets no credentials. Linux is signed by
`linux sign`, which also writes the checksums over every platform's final asset.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inventory  # noqa: E402
from inventory import c, feed  # noqa: E402

SIGNED_PLATFORMS = ("windows", "macos")
WINDOWS_SECRETS = ("WINDOWS_CERTIFICATE", "WINDOWS_CERTIFICATE_PASSWORD")
AZURE_SECRETS = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
                 "AZURE_SIGNING_ENDPOINT", "AZURE_SIGNING_ACCOUNT", "AZURE_CERT_PROFILE")
APPLE_SECRETS = ("APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD", "KEYCHAIN_PASSWORD",
                 "APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID")
# Never handed to verification tools; a signing call gets only its own platform's credentials.
SECRET_NAMES = frozenset(WINDOWS_SECRETS + AZURE_SECRETS + APPLE_SECRETS + (
    "LINUX_SIGNING_KEY", "GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL"))
SECRET_PATTERN = re.compile(r"TOKEN|SECRET|PASSWORD|PRIVATE_KEY|CERTIFICATE")
# App-owned scripts each step needs in the protected release checkout.
ADAPTERS = {
    ("windows", "sign"): ("packaging/windows/sign.ps1",),
    ("macos", "sign"): ("packaging/macos/nightly-sign.sh", "packaging/macos/import-cert.sh"),
    ("macos", "smoke"): ("packaging/macos/nightly-verify.sh",),
}
# Trusted system tools and checked-out scripts only, never payload executables.
TOOLS = {"pwsh", "/bin/bash", "/usr/bin/ditto", "/usr/bin/lipo", "/usr/bin/codesign", "/usr/bin/xcrun", "/usr/sbin/spctl"}
WINDOWS_SCRIPT = Path(__file__).resolve().with_name("windows_sign.ps1")
LIPO_ARCH = {"aarch64": "arm64", "x86_64": "x86_64"}
# notarytool --wait --timeout 1h plus stapler and spctl retries, as the stable route allows.
MAC_SIGN_TIMEOUT = 5400


class ToolFailed(c.InvalidNightly):
    pass


def command(argv, *, env, stdin=None, timeout=3900, merge=False):
    arguments = [str(value.absolute()) if isinstance(value, Path) else str(value) for value in argv]
    c.require(arguments and arguments[0] in TOOLS, "unreviewed signing executable")
    c.require(all(not any(ch in value for ch in "\0\r\n") for value in arguments), "tool arguments contain control characters")
    try:
        result = subprocess.run(arguments, input=stdin, env=env, check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT if merge else subprocess.PIPE, timeout=timeout)
    except subprocess.CalledProcessError as error:
        # The default message carries argv; app signing tools may print credentials, so neither is echoed.
        raise ToolFailed(f"{Path(arguments[0]).name} failed with exit status {error.returncode}") from None
    except subprocess.TimeoutExpired:
        raise ToolFailed(f"{Path(arguments[0]).name} timed out") from None
    c.require(len(result.stdout) <= c.MAX_JSON_BYTES and len(result.stderr or b"") <= c.MAX_JSON_BYTES,
              "tool output exceeds bound")
    return result.stdout.decode("utf-8-sig").strip()


def keyless(environment):
    """The environment minus every credential, so tools can't inherit a signer's secrets."""
    return {key: value for key, value in environment.items() if isinstance(value, str)
            and key not in SECRET_NAMES and not SECRET_PATTERN.search(key.upper())}


def credentialed(environment, names):
    return {**keyless(environment), **{key: environment[key] for key in names if key in environment}}


def work_root(environment):
    return c.owned_path(environment.get("RUNNER_TEMP") or environment.get("GITHUB_WORKSPACE") or Path.cwd())


def adapter(platform, step, source):
    source = c.owned_path(source)
    for name in ADAPTERS.get((platform, step), ()):
        path = c.owned_path(source / name)
        c.require(path.is_file() and not path.is_symlink() and source in path.resolve().parents,
                  f"the release checkout lacks {name}")
    return source


def target(manifest, platform, registry):
    c.require(platform in manifest["launcher_platforms"], f"{manifest['app']} publishes no {platform} launcher nightly")
    record, row = inventory.app_target(manifest["app"], platform, registry)
    return record, row, inventory.asset_names(row, manifest["identity"])


def only_file(directory, name):
    directory = c.owned_path(directory)
    c.require(directory.is_dir() and not directory.is_symlink(), f"{directory} is not a directory")
    names = sorted(path.name for path in directory.iterdir())
    c.require(names == [name], f"{directory} must hold exactly {name}, not {', '.join(names) or 'nothing'}")
    return directory / name


def new_directory(path):
    path = c.owned_path(path)
    c.require(not path.is_symlink() and (not path.exists() or (path.is_dir() and not any(path.iterdir()))),
              f"{path} must be a new or empty directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def copy_exclusive(source, destination):
    stream, _ = c.regular_file(source.parent, source.name)
    with stream, open(destination, "xb") as output:
        shutil.copyfileobj(stream, output, 1024 * 1024)


# ---- Windows ----------------------------------------------------------------------------------

def windows_script(mode, root, record, row, version, env, signer=None):
    trust = record["trust"]["windows"]
    argv = ["pwsh", "-NoProfile", "-NonInteractive", "-File", WINDOWS_SCRIPT, "-Mode", mode, "-Root", c.owned_path(root),
            "-Executable", row["entrypoint"], "-ExpectedProductName", record["product_name"],
            "-ExpectedVersion", version, "-ExpectedPublisher", trust["publisher_subject"],
            "-ExpectedRoot", trust["root"], "-ExpectedEkus", ",".join(trust["ekus"])]
    if signer is not None:
        argv += ["-Signer", signer]
    subject = command(argv, env=env)
    c.require(subject == trust["publisher_subject"], "the verified signer differs from the registry publisher subject")
    return subject


def repack_windows(root, archive):
    with zipfile.ZipFile(c.owned_path(archive), "x", compression=zipfile.ZIP_DEFLATED) as container:
        for path in sorted(Path(root).iterdir()):
            c.require(path.is_file() and not path.is_symlink(), "non-regular signed payload")
            container.write(path, path.name)


def verify_windows(manifest, archive, registry, environment, work):
    record, row, names = target(manifest, "windows", registry)
    c.require(Path(archive).name == names["asset"], "the signed Windows asset has the wrong name")
    root = Path(tempfile.mkdtemp(prefix="verify-", dir=work)) / "payload"
    inventory.verify_archive(manifest["app"], "windows", archive, manifest["identity"], root, registry)
    return windows_script("verify", root, record, row, manifest["identity"], keyless(environment))


def sign_windows(manifest, unsigned, source, output, registry, environment, work):
    record, row, names = target(manifest, "windows", registry)
    env = credentialed(environment, WINDOWS_SECRETS + AZURE_SECRETS)
    # sign.ps1 picks the PFX route whenever a certificate is set, so a partial PFX is refused
    # rather than silently falling back to Azure.
    if env.get("WINDOWS_CERTIFICATE"):
        c.require(all(env.get(key) for key in WINDOWS_SECRETS), "complete Windows PFX pair required")
    else:
        c.require(all(env.get(key) for key in AZURE_SECRETS), "a complete Windows PFX pair or all six Azure values required")
    signer = adapter("windows", "sign", source) / "packaging/windows/sign.ps1"
    final = output / names["asset"]
    payload = Path(tempfile.mkdtemp(prefix="sign-", dir=work)) / "payload"
    inventory.verify_archive(manifest["app"], "windows", unsigned, manifest["identity"], payload, registry)
    before = c.digest_file(payload, row["entrypoint"])
    windows_script("sign", payload, record, row, manifest["identity"], env, signer)
    # A repacked zip always differs from its input, so the GUI itself must have changed.
    c.require(c.digest_file(payload, row["entrypoint"]) != before, "signing left the GUI unchanged")
    windows_script("verify", payload, record, row, manifest["identity"], keyless(environment))
    repack_windows(payload, final)
    return verify_windows(manifest, final, registry, environment, work)


# ---- macOS ------------------------------------------------------------------------------------

def hardened_runtime(details):
    """The codesign flags field, read in linear time."""
    for line in details.splitlines():
        if not line.startswith("CodeDirectory "):
            continue
        for field in line.split()[1:]:
            if not field.startswith("flags=0x"):
                continue
            value, separator, names = field[8:].partition("(")
            if not separator or not names.endswith(")") or not re.fullmatch("[0-9a-f]+", value):
                return False
            return bool(int(value, 16) & 0x10000) and "runtime" in names[:-1].split(",")
    return False


def verify_macos(manifest, archive, registry, environment, work):
    record, row, names = target(manifest, "macos", registry)
    c.require(Path(archive).name == names["asset"], "the signed macOS asset has the wrong name")
    team = record["trust"]["macos"]["apple_team_id"]
    # Inventory is no signature proof; the system tools below check that separately.
    inventory.verify_archive(manifest["app"], "macos", archive, manifest["identity"], None, registry)
    clean = keyless(environment)
    output = Path(tempfile.mkdtemp(prefix="verify-", dir=work)) / "payload"
    # Unpack as the launcher does: no resource forks or extended attributes.
    command(["/usr/bin/ditto", "-x", "-k", "--norsrc", "--noextattr", archive, output], env=clean)
    app = output / row["entrypoint"].split("/", 1)[0]
    executable = output / row["entrypoint"]
    arch = LIPO_ARCH[row["target"]["arch"]]
    c.require(command(["/usr/bin/lipo", "-archs", executable], env=clean) == arch, f"the Mac GUI must be thin {arch}")
    command(["/usr/bin/codesign", "--verify", "--strict", "--deep", app], env=clean)
    details = command(["/usr/bin/codesign", "-d", "--verbose=4", app], env=clean, timeout=60, merge=True)
    framed = "\n" + details + "\n"
    c.require(f"\nIdentifier={row['isolation']['package_identity']}\n" in framed
              and "\nAuthority=Developer ID Application:" in framed and f"\nTeamIdentifier={team}\n" in framed
              and re.search(r"^Timestamp=.+$", details, re.M) and hardened_runtime(details),
              "the Mac app needs the nightly identifier, Developer ID, the registry team, a secure timestamp "
              "and the hardened runtime")
    command(["/usr/bin/xcrun", "stapler", "validate", app], env=clean)
    assessment = command(["/usr/sbin/spctl", "--assess", "--type", "execute", "-vv", app], env=clean, timeout=120, merge=True)
    lines = assessment.splitlines()
    c.require(lines and lines[0].endswith(": accepted") and "source=Notarized Developer ID" in lines
              and any(line.startswith("origin=Developer ID Application: ") and line.endswith(f"({team})") for line in lines),
              "Gatekeeper must accept the app as notarized Developer ID from the registry team")
    return team


def sign_macos(manifest, unsigned, source, output, registry, environment, work):
    record, row, names = target(manifest, "macos", registry)
    team = record["trust"]["macos"]["apple_team_id"]
    env = credentialed(environment, APPLE_SECRETS)
    c.require(all(env.get(key) for key in APPLE_SECRETS), "all six Apple signing and notarization secrets required")
    c.require(env["APPLE_TEAM_ID"] == team, "APPLE_TEAM_ID differs from the registry's Apple team")
    env["NIGHTLY_APPLE_TEAM_ID"] = team
    script = adapter("macos", "sign", source) / "packaging/macos/nightly-sign.sh"
    staged = Path(tempfile.mkdtemp(prefix="sign-", dir=work)) / names["asset"]
    copy_exclusive(Path(unsigned), staged)
    inventory.verify_archive(manifest["app"], "macos", staged, manifest["identity"], None, registry)
    final = output / names["asset"]
    command(["/bin/bash", script, "--arch", row["target"]["arch"], "--input", staged, "--output", final],
            env=env, timeout=MAC_SIGN_TIMEOUT)
    return verify_macos(manifest, final, registry, environment, work)


SIGNERS = {"windows": sign_windows, "macos": sign_macos}
VERIFIERS = {"windows": verify_windows, "macos": verify_macos}


# ---- Steps ------------------------------------------------------------------------------------

def sign(platform, admission, input_dir, source, output, environment, registry=None):
    """Sign one launcher payload; returns {asset, sha256, signer}."""
    registry = registry if registry is not None else feed.load_registry()
    c.require(platform in SIGNED_PLATFORMS, f"{platform} is not signed here; Linux is signed by `linux sign`")
    manifest, _ = inventory.load_admission(admission, registry)
    _, _, names = target(manifest, platform, registry)
    unsigned = inventory.admitted_file(manifest, input_dir, names["unsigned"])
    entry = inventory.manifest_entry(manifest, names["unsigned"])
    output = new_directory(output)
    with tempfile.TemporaryDirectory(prefix="nightly-platform-", dir=work_root(environment)) as work:
        signer = SIGNERS[platform](manifest, unsigned, source, output, registry, environment, Path(work))
    final = only_file(output, names["asset"])
    _, digest = c.digest_file(output, final.name)
    c.require(c.digest_file(unsigned.parent, unsigned.name)[1] == entry["sha256"], "the unsigned payload changed during signing")
    c.require(digest != entry["sha256"], "signing left the payload unchanged; an unsigned asset is refused")
    return {"asset": final.name, "sha256": digest, "signer": signer}


def verify(platform, admission, input_dir, environment, work=None, registry=None):
    """Keyless verification of one signed launcher asset; returns {asset, sha256, signer}."""
    registry = registry if registry is not None else feed.load_registry()
    c.require(platform in SIGNED_PLATFORMS, f"{platform} is verified by `linux verify-final`")
    manifest, _ = inventory.load_admission(admission, registry)
    _, _, names = target(manifest, platform, registry)
    final = only_file(input_dir, names["asset"])
    _, before = c.digest_file(final.parent, final.name)
    with tempfile.TemporaryDirectory(prefix="nightly-platform-", dir=new_directory(work) if work else work_root(environment)) as scratch:
        signer = VERIFIERS[platform](manifest, final, registry, environment, Path(scratch))
    c.require(c.digest_file(final.parent, final.name)[1] == before, "the signed asset changed during verification")
    return {"asset": final.name, "sha256": before, "signer": signer}


def smoke(platform, admission, input_dir, source, environment, work=None, registry=None):
    """Launch the final asset once without credentials.

    macOS runs the app's packaging/macos/nightly-verify.sh against the registry team, as the
    stable route does; Windows and Linux run the launcher's version query (smoke.py).
    """
    registry = registry if registry is not None else feed.load_registry()
    manifest, _ = inventory.load_admission(admission, registry)
    record, row, names = target(manifest, platform, registry)
    final = only_file(input_dir, names["asset"])
    clean = keyless(environment)
    if platform == "macos":
        c.require(source, "the macOS smoke test runs the app's nightly-verify.sh from --source")
        script = adapter("macos", "smoke", source) / "packaging/macos/nightly-verify.sh"
        command(["/bin/bash", script, "--input", final, "--expect", "signed",
                 "--team", record["trust"]["macos"]["apple_team_id"]], env=clean, timeout=900)
        return {"asset": final.name, "smoke": "nightly-verify.sh"}
    import smoke as probe
    c.require(inventory.host_runs(row), f"this host cannot run a {platform}-{row['target']['arch']} payload")
    root = Path(tempfile.mkdtemp(prefix="nightly-smoke-", dir=new_directory(work) if work else work_root(environment)))
    inventory.verify_archive(manifest["app"], platform, final, manifest["identity"], root / "payload", registry)
    return {"asset": final.name, "smoke": probe.smoke(manifest["app"], root / "payload" / row["entrypoint"],
                                                      root / "profile", manifest["identity"], registry)}


def github_output(values):
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with open(destination, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                c.require("\n" not in str(value) and "\r" not in str(value), f"invalid action output {key}")
                stream.write(f"{key}={value}\n")


def main(argv):
    parser = argparse.ArgumentParser(prog="platforms", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("sign", "verify", "smoke"))
    parser.add_argument("--platform", required=True, choices=feed.LAUNCHER_PLATFORMS)
    parser.add_argument("--admission", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--source")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    try:
        if args.mode == "sign":
            c.require(args.source and args.output, "sign needs --source and --output")
            result = sign(args.platform, args.admission, args.input, args.source, args.output, environment)
        elif args.mode == "verify":
            result = verify(args.platform, args.admission, args.input, environment, args.output)
        else:
            result = smoke(args.platform, args.admission, args.input, args.source, environment, args.output)
        github_output({key: result[key] for key in ("asset", "sha256") if key in result})
        print(json.dumps(result, sort_keys=True))
        return 0
    except (c.InvalidNightly, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"::error::nightly {args.mode} ({args.platform}) refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
