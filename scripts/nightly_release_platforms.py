#!/usr/bin/env python3
"""Per-platform release-ref signing plug-ins (policy v3).

Each plug-in signs ONE validated unsigned payload inside the protected signer job
and re-verifies the final bytes without credentials. Plug-ins never execute
payload code. Windows and macOS live here; Linux is the separately owned
``nightly_release_linux`` module with the same interface:

    PLATFORM, METHOD, TARGET, TRUST_FIELDS, BYTES_MAY_BE_UNCHANGED,
    validate_trust(trust) -> trust
    assets(version) -> {"asset", "unsigned_asset", "built_asset"}
    sign(trust, unsigned, output, source, environment, version) -> {"verified_signer", "support"}
    verify(trust, directory, asset, support, source, environment, version) -> verified_signer
"""
import importlib
from pathlib import Path
import re
import shutil
import tempfile
import types
import nightly_app as apps
import nightly_feed as n
import nightly_signing as signing

SUPPORTED = ("linux-x86_64", "macos-aarch64", "windows-x86_64")
def isolation(app=None):
    """The app's reviewed isolation row (the run's app from NIGHTLY_APP by default)."""
    return {"associations": "none", "package_identity": (apps.App(app) if app else apps.current()).identity,
            "profile_contract": "nightly", "profile_version": 1}


# PhotoCraft's, as every PhotoCraft fixture records it.
ISOLATION = isolation("photocraft")
# Every value any plug-in may receive as a credential. Verification never sees them.
SECRET_NAMES = frozenset(signing.WINDOWS_SECRETS + signing.AZURE_SECRETS + signing.APPLE_SECRETS +
                         ("LINUX_SIGNING_KEY", "GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN",
                          "ACTIONS_ID_TOKEN_REQUEST_TOKEN"))
# notarytool --wait --timeout 1h plus stapler/spctl retries, as the stable route allows.
MAC_SIGN_TIMEOUT = 5400
PENDING = Path(__file__).resolve().parent.parent/"nightly-unix-pending.json"


def keyless(environment):
    """Verification environment: every credential stripped, even empty ones."""
    return {key: value for key, value in environment.items() if key not in SECRET_NAMES}


def credentialed(environment, names):
    """Signing environment: only this platform's own secrets plus non-secret context."""
    return {**keyless(environment), **{key: environment[key] for key in names if key in environment}}


def single_trust(trust, fields_):
    n.fields(trust, set(fields_), "platform trust")
    n.require(len(trust) == 1, "platform trust must have exactly one value")
    value = next(iter(trust.values()))
    n.require(isinstance(value, str) and 0 < len(value) <= 2048 and
              not any(c in value for c in "\r\n\0"), "invalid platform trust value")
    return value


def standard_assets(platform, version, extension):
    n.require(isinstance(version, str) and len(version) <= 128 and
              re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-nightly\.[0-9]{8}\.[1-9][0-9]*\.[1-9][0-9]*\.g[0-9a-f]{12}", version),
              "noncanonical immutable nightly version")
    asset = apps.current_name()+"-"+version+"-"+platform+"-nightly"+extension
    unsigned = asset[:-len(extension)]+".unsigned"+extension
    n.require(n.NAME.fullmatch(asset) and n.NAME.fullmatch(unsigned), "unsafe platform asset name")
    return {"asset": asset, "unsigned_asset": unsigned}


# ---------------------------------------------------------------- Windows
def _windows_trust(trust):
    subject = single_trust(trust, {"publisher_subject"})
    # Same bound as the native signer, so a bad subject refuses at policy validation, before artifact work.
    n.require(len(subject) <= signing.WINDOWS_PUBLISHER_SUBJECT_MAX, "Windows publisher subject exceeds the native signer bound")
    return trust


def _windows_assets(version):
    names = standard_assets("windows-x86_64", version, ".zip")
    # package.ps1 -Nightly -BuildOnly already writes the .unsigned.zip name.
    return {**names, "built_asset": names["unsigned_asset"]}


def _windows_env(trust, environment):
    return {**environment, "NIGHTLY_WINDOWS_PUBLISHER_SUBJECT": trust["publisher_subject"]}


def _windows_sign(trust, unsigned, output, source, environment, version):
    env = _windows_env(trust, credentialed(environment, signing.WINDOWS_SECRETS+signing.AZURE_SECRETS))
    signing.adapter("windows-x86_64", source)
    signing.require_config("windows-x86_64", env)
    asset = _windows_assets(version)["asset"]
    signer = signing.sign_windows(source, unsigned, Path(output)/asset, env, version)
    n.require(signer == trust["publisher_subject"], "native verified signer differs from protected publisher subject")
    n.require(WINDOWS.verify(trust, output, asset, {}, source, keyless(environment), version) == signer, "keyless re-verification differs")
    return {"verified_signer": signer, "support": {}}


def _windows_verify(trust, directory, asset, support, source, environment, version):
    n.require(support == {} and asset == _windows_assets(version)["asset"], "unexpected Windows signing support")
    env = _windows_env(trust, keyless(environment))
    with tempfile.TemporaryDirectory(prefix="nightly-final-verify-", dir=n.owned_path(env["RUNNER_TEMP"])) as temporary:
        root = signing.windows_unpack(Path(directory)/asset, Path(temporary)/"payload")
        subject = signing.windows_verify(root, env, version)
    n.require(subject == trust["publisher_subject"], "final native signer differs")
    return subject


WINDOWS = types.SimpleNamespace(
    PLATFORM="windows-x86_64", METHOD="authenticode",
    TARGET={"os": "windows", "arch": "x86_64", "format": "portable_zip"},
    TRUST_FIELDS=frozenset({"publisher_subject"}), BYTES_MAY_BE_UNCHANGED=False,
    validate_trust=_windows_trust, assets=_windows_assets, sign=_windows_sign, verify=_windows_verify)


# ---------------------------------------------------------------- macOS
def _mac_trust(trust):
    team = single_trust(trust, {"apple_team_id"})
    n.require(re.fullmatch(r"[A-Z0-9]{10}", team), "Apple team ID must be the exact ten-character team")
    return trust


def _mac_assets(version):
    names = standard_assets("macos-aarch64", version, ".zip")
    # packaging/macos/nightly.sh writes the final name; the handoff renames it.
    return {**names, "built_asset": names["asset"]}


def _mac_inventory(archive, version, temporary):
    """Inventory-only check of the unsigned payload under its exact asset name."""
    import nightly_unix as u
    copy = Path(temporary)/_mac_assets(version)["asset"]
    shutil.copyfile(archive, copy)
    u.verify_archive(None, "macos-aarch64", copy, version, app=apps.current_name())
    return copy


def _mac_sign(trust, unsigned, output, source, environment, version):
    """Stable route: import-cert.sh, codesign runtime+timestamp, notarytool, stapler.

    The app-owned nightly-sign.sh does the Apple work with only the six stable
    APPLE_*/KEYCHAIN_PASSWORD values; it never builds or runs payload code.
    """
    team = trust["apple_team_id"]
    env = {**credentialed(environment, signing.APPLE_SECRETS), "NIGHTLY_APPLE_TEAM_ID": team}
    signing.adapter("macos-aarch64", source)
    signing.require_config("macos-aarch64", env)
    asset = _mac_assets(version)["asset"]
    final = n.owned_path(Path(output)/asset)
    n.require(not final.exists() and not final.is_symlink(), "final Mac output must be new")
    with tempfile.TemporaryDirectory(prefix="nightly-mac-sign-", dir=n.owned_path(env["RUNNER_TEMP"])) as temporary:
        staged = _mac_inventory(unsigned, version, temporary)
        signing.command(["/bin/bash", Path(source).resolve()/"packaging/macos/nightly-sign.sh",
                         "--arch", "aarch64", "--input", staged.resolve(), "--output", final.resolve()], env=env,
                        timeout=MAC_SIGN_TIMEOUT)
    n.require(sorted(path.name for path in Path(output).iterdir()) == [asset], "Mac signer wrote unexpected files")
    signer = MACOS.verify(trust, output, asset, {}, source, keyless(environment), version)
    return {"verified_signer": signer, "support": {}}


def _mac_verify(trust, directory, asset, support, source, environment, version):
    n.require(support == {} and asset == _mac_assets(version)["asset"], "unexpected Mac signing support")
    env = {**keyless(environment), "NIGHTLY_APPLE_TEAM_ID": trust["apple_team_id"]}
    with tempfile.TemporaryDirectory(prefix="nightly-mac-verify-", dir=n.owned_path(env["RUNNER_TEMP"])) as temporary:
        team = signing.mac_verify(Path(directory)/asset, version, None, env, Path(temporary)/"payload")
    n.require(team == trust["apple_team_id"], "final Mac signer team differs")
    return team


MACOS = types.SimpleNamespace(
    PLATFORM="macos-aarch64", METHOD="developer-id-notarized",
    TARGET={"os": "macos", "arch": "aarch64", "format": "portable_zip"},
    TRUST_FIELDS=frozenset({"apple_team_id"}), BYTES_MAY_BE_UNCHANGED=False,
    validate_trust=_mac_trust, assets=_mac_assets, sign=_mac_sign, verify=_mac_verify)


def plugin(platform):
    n.require(platform in SUPPORTED, "unsupported release-ref platform")
    if platform == "windows-x86_64":
        return WINDOWS
    if platform == "macos-aarch64":
        return MACOS
    try:
        module = importlib.import_module("nightly_release_linux")
    except ImportError as error:
        raise n.Unavailable("reviewed Linux release plug-in missing") from error
    n.require(getattr(module, "PLATFORM", None) == platform, "Linux plug-in identity differs")
    return module


def required_support(platform, asset):
    """Exact published support files a platform's final asset needs (Linux: its detached .asc)."""
    names = getattr(plugin(platform), "support_names", None)
    return set(names(asset)) if names is not None else set()


def expected_signer(platform, trust):
    """The protected trust value IS the identity native verification must report."""
    module = plugin(platform)
    module.validate_trust(trust)
    n.require(set(trust) == set(module.TRUST_FIELDS), "platform trust fields differ")
    return single_trust(trust, module.TRUST_FIELDS)


def registry_platforms(registry, app=None):
    """The run's app's admitted targets as platform keys; every one must have a plug-in."""
    app = app or apps.current_name()
    result = []
    for item in n.app_record(registry, app)["targets"]:
        n.fields(item, {"target", "asset_pattern", "isolation"}, "target registry")
        target = item["target"]
        n.require(isinstance(target, dict) and set(target) == {"os", "arch", "format"}, "invalid target registry")
        platform = str(target["os"])+"-"+str(target["arch"])
        module = plugin(platform)
        n.require(platform in apps.PLATFORMS, "unsupported release-ref platform")
        expected_pattern = apps.App(app, registry).asset_pattern(platform)
        n.require(target == module.TARGET and item["asset_pattern"] == expected_pattern and
                  item["isolation"] == isolation(app), "registry row differs from reviewed platform plug-in")
        n.require(platform not in result, "duplicate admitted platform")
        result.append(platform)
    n.require(result, "no admitted release-ref platform")
    return sorted(result)


def secret_key(platform):
    n.require(platform in SUPPORTED, "unsupported release-ref platform")
    return platform.upper().replace("-", "_")
