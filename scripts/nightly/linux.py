#!/usr/bin/env python3
"""Linux nightly packages: keyless identity checks, signing with the stable key, final verification.

    linux precheck     --admission DIR --directory DIR --prefix P --flatpak-id ID
    linux sign         --admission DIR --directory DIR --prefix P --flatpak-id ID [--signed DIR ...] --output DIR
    linux verify-final --admission DIR --directory DIR --prefix P --flatpak-id ID

One stable OpenPGP key signs stable and nightly packages alike, so a nightly package that claims a
stable identity would be trusted by apt, dnf and pacman. The package prefix and the stable Flatpak
ID are pinned by the signer's caller on its release branch (and by the registry for launcher
apps), because the producer on main names its own files. precheck proves, before any key exists,
that the manifest's prefix is the pinned one and that every package names itself `-nightly` with
the nightly version: deb Package and Version, rpm Name and Version, `.PKGINFO` pkgname and pkgver,
the Flatpak ref `app/<ID>.nightly/<arch>/stable` in the OCI image config and the bundle header,
the distro tarball's top directory, and the launcher tarball's inventory. Package relations
(provides, replaces, obsoletes, conflicts, breaks) may name only the app's own nightly packages.

sign re-runs precheck, imports LINUX_SIGNING_KEY into a private keyring and refuses unless it is
the key the registry pins, signs through actions/draft-release/sign-linux.sh, writes NIGHTLY.json
for launcher apps, and writes and signs SHA256SUMS.txt over every final asset. `--directory`
holds the admitted producer files (handoff layout or flat), each `--signed` directory one signed
Windows or macOS launcher asset.

verify-final checks a final release directory without any secret: the exact asset set, every
signature against the pinned public key in craft-common-actions, the checksums, the package
identities again (signing rewrites the deb and rpm) and NIGHTLY.json by the launcher's rules.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inventory  # noqa: E402
from inventory import ROOT, c, feed  # noqa: E402

SIGN_LINUX = ROOT / "actions/draft-release/sign-linux.sh"
DEB_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "riscv64": "riscv64"}
MAX_CONTROL = 16 * 1024 * 1024
MAX_RPM_HEADER = 32 * 1024 * 1024
MAX_FLATPAK_METADATA = 16 * 1024 * 1024
HASH_ALGORITHMS = {"8", "9", "10"}  # OpenPGP SHA-256, SHA-384, SHA-512
BAD_STATUS = {"BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "NO_PUBKEY"}
SIGNING_CONTEXT = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "RUNNER_TEMP", "GITHUB_WORKSPACE")
# Files signing leaves byte for byte as the producer built them; sign-linux.sh rewrites deb and
# rpm (embedded signatures) and the macOS and Windows zips arrive signed.
UNCHANGED_KINDS = {"tar_gz", "flatpak", "oci_manifest", "arch_pkg"}
RPM_TAGS = {1000: "NAME", 1001: "VERSION", 1002: "RELEASE", 1022: "ARCH", 1047: "PROVIDENAME", 1054: "CONFLICTNAME",
            1090: "OBSOLETENAME"}
DEB_RELATIONS = ("Provides", "Replaces", "Conflicts", "Breaks")
RPM_RELATIONS = {"PROVIDENAME": "Provides", "OBSOLETENAME": "Obsoletes", "CONFLICTNAME": "Conflicts"}
PACMAN_RELATIONS = ("provides", "replaces", "conflicts")


def clean_env(environment, extra=None):
    env = {key: environment[key] for key in SIGNING_CONTEXT if isinstance(environment.get(key), str)}
    env.setdefault("PATH", os.defpath)
    env.update(extra or {})
    return env


def tool(name, environment):
    found = shutil.which(name, path=environment.get("PATH", os.defpath))
    c.require(found is not None, f"{name} is not installed")
    return found


def run(argv, environment, *, stdin=None, timeout=600):
    try:
        return subprocess.run([str(a) for a in argv], input=stdin, env=environment, check=True, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    except subprocess.CalledProcessError as error:
        # Never echo argv or output: a failing gpg import could print key material.
        raise c.InvalidNightly(f"{Path(str(argv[0])).name} failed with exit status {error.returncode}") from None
    except subprocess.TimeoutExpired:
        raise c.InvalidNightly(f"{Path(str(argv[0])).name} timed out") from None


# ---- The pinned key ---------------------------------------------------------------------------

def pinned_key(app, registry):
    """(public key path, primary fingerprint, signing subkey) of the stable Linux release key.

    Launcher apps pin it in their registry entry. Distro-only apps use the same org-wide key,
    taken from the registry's pins, which must all agree.
    """
    record = feed.app_record(registry, app)
    if record is not None:
        c.require("linux" in record["trust"], f"{app} has no pinned Linux key")
        pins = [record["trust"]["linux"]]
    else:
        pins = [r["trust"]["linux"] for r in registry["apps"].values() if "linux" in r["trust"]]
    keys = {(p["public_key"], p["primary_fingerprint"], p["signing_subkey"]) for p in pins}
    c.require(len(keys) == 1, "the registry does not pin exactly one Linux release key")
    name, primary, subkey = keys.pop()
    return feed.HERE / name, primary, subkey


def gpg_listing(home, environment, secret=False):
    """[(primary fingerprint, [(fingerprint, capabilities, validity)])] for every key in `home`."""
    out = run([tool("gpg", environment), "--homedir", home, "--batch", "--with-colons",
               "--list-secret-keys" if secret else "--list-keys"], environment).decode()
    keys, last = [], None
    for line in out.splitlines():
        parts = line.split(":")
        if parts[0] in ("pub", "sec", "sub", "ssb"):
            last = parts
            if parts[0] in ("pub", "sec"):
                keys.append([None, []])
        elif parts[0] == "fpr" and last is not None and keys:
            entry = (parts[9], last[11] if len(last) > 11 else "", last[1])
            if last[0] in ("pub", "sec"):
                keys[-1][0] = parts[9]
            keys[-1][1].append(entry)
            last = None
    return keys


def pinned_bytes(key_file):
    """The pinned key is our own source file, so it is read outside the job's owned roots."""
    data = Path(key_file).read_bytes()
    c.require(0 < len(data) <= c.MAX_JSON_BYTES, "pinned public key is empty or exceeds bound")
    return data


def public_keyring(data, primary, subkey, home, environment, what="the public key"):
    """Import one public key into a fresh homedir and export a gpgv keyring of it.

    Refuses unless `data` holds exactly one primary with the pinned fingerprint and the
    pinned signing subkey.
    """
    home = Path(home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    gpg = tool("gpg", environment)
    run([gpg, "--homedir", home, "--batch", "--quiet", "--import"], environment, stdin=data)
    keys = gpg_listing(home, environment)
    c.require(len(keys) == 1 and keys[0][0] == primary and any(f == subkey for f, _, _ in keys[0][1]),
              f"{what} is not the pinned key {primary} with signing subkey {subkey}")
    keyring = home / "trusted.gpg"
    keyring.write_bytes(run([gpg, "--homedir", home, "--batch", "--export", primary], environment))
    return keyring


def stop_agent(home, environment):
    try:
        run([tool("gpgconf", environment), "--homedir", home, "--kill", "all"], environment, timeout=30)
    except (c.InvalidNightly, OSError):
        pass  # The homedir is removed next, which also ends a live agent.


def check_status(status, primary, subkey):
    records = [line.split()[1:] for line in status.splitlines() if line.startswith("[GNUPG:] ")]
    kinds = [record[0] for record in records if record]
    c.require(not BAD_STATUS & set(kinds) and kinds.count("GOODSIG") == 1 and kinds.count("VALIDSIG") == 1,
              "exactly one good, unexpired, unrevoked signature required")
    valid = next(record for record in records if record and record[0] == "VALIDSIG")
    c.require(len(valid) == 11 and valid[1] == subkey and valid[10] == primary and valid[8] in HASH_ALGORITHMS
              and valid[9] == "00", "the signature is not a SHA-2 binary signature by the pinned signing subkey")


def verify_signature(keyring, signature, payload, pin, environment, armored=None):
    """gpgv of one detached signature over exact bytes, with only the pinned key."""
    signature = c.owned_path(signature)
    c.require(signature.is_file() and not signature.is_symlink() and 0 < signature.stat().st_size <= feed.MAX_SIGNATURE_BYTES,
              f"{signature.name} is missing or exceeds 16 KiB")
    raw = signature.read_bytes()
    is_armored = raw.startswith(b"-----BEGIN PGP SIGNATURE-----")
    c.require(armored is None or armored == is_armored,
              f"{signature.name} must be {'an ASCII-armored' if armored else 'a binary'} detached signature")
    try:
        status = run([tool("gpgv", environment), "--homedir", keyring.parent, "--status-fd", "1", "--keyring", keyring,
                      signature, payload], environment).decode()
    except c.InvalidNightly:
        raise c.InvalidNightly(f"{signature.name} does not verify against the pinned key") from None
    check_status(status, *pin)


def verify_signed_message(keyring, signature, pin, environment, work):
    """gpgv of an OpenPGP signed message (a containers/image signature); returns its content."""
    content = Path(tempfile.mkdtemp(prefix="msg-", dir=work)) / "content"
    try:
        status = run([tool("gpgv", environment), "--homedir", keyring.parent, "--status-fd", "1", "--keyring", keyring,
                      "--output", content, signature], environment).decode()
    except c.InvalidNightly:
        raise c.InvalidNightly(f"{Path(signature).name} does not verify against the pinned key") from None
    check_status(status, *pin)
    return c.bounded_bytes(content, c.MAX_JSON_BYTES, "signed message")


# ---- Package metadata, read as data -----------------------------------------------------------

def ar_members(path):
    """[(name, offset, size)] of an ar archive (deb)."""
    members = []
    with open(path, "rb") as stream:
        c.require(stream.read(8) == b"!<arch>\n", f"{Path(path).name} is not an ar archive")
        while header := stream.read(60):
            c.require(len(header) == 60 and header[58:60] == b"`\n", f"{Path(path).name} has a broken ar header")
            name = header[:16].decode("ascii").rstrip(" ").rstrip("/")
            size = int(header[48:58].decode("ascii").strip())
            c.require(re.fullmatch(r"[A-Za-z0-9_.+-]{1,15}", name) and 0 <= size <= c.MAX_ASSET_BYTES
                      and len(members) < 16, f"{Path(path).name} has an unexpected ar member")
            members.append((name, stream.tell(), size))
            stream.seek(size + (size & 1), os.SEEK_CUR)
    return members


def ar_read(path, offset, size):
    with open(path, "rb") as stream:
        stream.seek(offset)
        data = stream.read(size)
    c.require(len(data) == size, f"{Path(path).name} is truncated")
    return data


def zstd_decompress(data, environment):
    return run([tool("zstd", environment), "-dcq", "--no-progress"], environment, stdin=data)


def parse_control(text):
    fields, key = {}, None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if line[0] in " \t":
            c.require(key is not None, "continuation line without a field")
            fields[key] += "\n" + line.strip()
            continue
        key, sep, value = line.partition(":")
        c.require(sep and key not in fields, f"invalid control line {line!r}")
        fields[key] = value.strip()
    return fields


def deb_control(path, environment):
    members = ar_members(path)
    c.require([m[0] for m in members[:1]] == ["debian-binary"], f"{Path(path).name} does not start with debian-binary")
    control = [m for m in members if m[0].startswith("control.tar")]
    c.require(len(control) == 1 and control[0][2] <= MAX_CONTROL, f"{Path(path).name} lacks one bounded control.tar")
    name, offset, size = control[0]
    data = ar_read(path, offset, size)
    if name.endswith(".zst"):
        data = zstd_decompress(data, environment)
    c.require(len(data) <= 4 * MAX_CONTROL, "control.tar expands beyond its bound")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for member in tar:
            if member.name in ("control", "./control") and member.isfile():
                c.require(member.size <= c.MAX_JSON_BYTES, "deb control file exceeds bound")
                return parse_control(tar.extractfile(member).read().decode("utf-8"))
    raise c.InvalidNightly(f"{Path(path).name} has no control file")


def relation_names(value):
    """Package names in a deb relation field (Provides: a (= 1), b | c)."""
    names = set()
    for part in re.split(r"[,|]", value or ""):
        word = part.strip().split(" ", 1)[0].split("(", 1)[0].split(":", 1)[0]
        if word:
            names.add(word)
    return names


def rpm_header(path):
    """{tag name: value} for the identity tags of an rpm's main header."""
    with open(path, "rb") as stream:
        lead = stream.read(96)
        c.require(len(lead) == 96 and lead[:4] == b"\xed\xab\xee\xdb", f"{Path(path).name} is not an rpm")

        def header():
            intro = stream.read(16)
            c.require(len(intro) == 16 and intro[:4] == b"\x8e\xad\xe8\x01", f"{Path(path).name} has a broken rpm header")
            count, size = int.from_bytes(intro[8:12], "big"), int.from_bytes(intro[12:16], "big")
            c.require(count <= 65536 and count * 16 + size <= MAX_RPM_HEADER, "rpm header exceeds bound")
            index, store = stream.read(count * 16), stream.read(size)
            c.require(len(index) == count * 16 and len(store) == size, f"{Path(path).name} is truncated")
            return index, store, 16 + count * 16 + size

        _, _, length = header()
        stream.read((8 - length % 8) % 8)  # the signature header is padded to 8 bytes
        index, store, _ = header()
    values = {}
    for start in range(0, len(index), 16):
        tag, kind, offset, count = (int.from_bytes(index[start + i:start + i + 4], "big") for i in (0, 4, 8, 12))
        if tag not in RPM_TAGS:
            continue
        c.require(kind in (6, 8, 9) and offset < len(store) and 0 < count <= 65536, f"rpm tag {tag} has an unexpected type")
        strings = store[offset:].split(b"\0")[: 1 if kind == 6 else count]
        c.require(len(strings) == (1 if kind == 6 else count), f"rpm tag {tag} is truncated")
        decoded = [s.decode("utf-8") for s in strings]
        values[RPM_TAGS[tag]] = decoded[0] if kind in (6, 9) else decoded
    return values


def pkginfo(path, environment):
    """.PKGINFO of a pacman package, as {key: [values]}."""
    zstd = tool("zstd", environment)
    process = subprocess.Popen([zstd, "-dcq", "--no-progress", "--", str(path)], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=environment)
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
            for count, member in enumerate(tar):
                c.require(count < 8, ".PKGINFO must be among the first members")
                if member.name == ".PKGINFO" and member.isfile():
                    c.require(member.size <= c.MAX_JSON_BYTES, ".PKGINFO exceeds bound")
                    text = tar.extractfile(member).read().decode("utf-8")
                    break
            else:
                raise c.InvalidNightly(f"{Path(path).name} has no .PKGINFO")
    finally:
        process.kill()
        process.stdout.close()
        process.wait(timeout=30)
    values = {}
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            key, sep, value = line.partition(" = ")
            c.require(sep, f"invalid .PKGINFO line {line!r}")
            values.setdefault(key.strip(), []).append(value.strip())
    return values


def _gvariant_offset_size(size):
    return 0 if size == 0 else 1 if size <= 0xFF else 2 if size <= 0xFFFF else 4 if size <= 0xFFFFFFFF else 8


def _gvariant_offset(data, position, width):
    return int.from_bytes(data[position:position + width], "little")


def flatpak_bundle_ref(path):
    """The `ref` in a .flatpak bundle's metadata.

    A bundle is an OSTree static-delta superblock, a GVariant tuple whose first member is the
    metadata a{sv}. GVariant stores a tuple's framing offsets in reverse order at its end, so the
    file's last offset is the end of that dictionary.
    """
    path = Path(path)
    size = path.stat().st_size
    width = _gvariant_offset_size(size)
    c.require(size > width, f"{path.name} is not a Flatpak bundle")
    with open(path, "rb") as stream:
        stream.seek(size - width)
        end = int.from_bytes(stream.read(width), "little")
        c.require(0 < end <= min(MAX_FLATPAK_METADATA, size - width), f"{path.name} has no bounded bundle metadata")
        stream.seek(0)
        data = stream.read(end)
    width = _gvariant_offset_size(end)
    last = _gvariant_offset(data, end - width, width)
    c.require(0 < last <= end - width and (end - last) % width == 0, f"{path.name} has broken bundle metadata")
    entries, start, found = (end - last) // width, 0, []
    for index in range(entries):
        stop = _gvariant_offset(data, last + index * width, width)
        c.require(start <= stop <= last, f"{path.name} has broken bundle metadata")
        entry = data[start:stop]
        ew = _gvariant_offset_size(len(entry))
        key_end = _gvariant_offset(entry, len(entry) - ew, ew)
        c.require(0 < key_end <= len(entry) - ew and entry[key_end - 1] == 0, f"{path.name} has a broken metadata key")
        key = entry[:key_end - 1].decode("utf-8", "replace")
        value = entry[(key_end + 7) & ~7:len(entry) - ew]
        if key == "ref":
            separator = value.rfind(b"\0")
            c.require(separator > 0 and value[separator + 1:] == b"s" and value[separator - 1] == 0,
                      f"{path.name}'s ref is not a string")
            found.append(value[:separator - 1].decode("utf-8"))
        start = (stop + 7) & ~7
    c.require(len(found) == 1, f"{path.name} carries no single ref")
    return found[0]


def oci_archive_ref(archive, manifest_file):
    """The org.flatpak.ref of the OCI layout tar, after checking index.json points at manifest_file."""
    manifest_bytes = c.bounded_bytes(manifest_file, c.MAX_JSON_BYTES, "OCI manifest")
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    with tarfile.open(archive, mode="r:") as tar:
        def blob(name):
            member = tar.getmember(name)
            c.require(member.isfile() and member.size <= c.MAX_JSON_BYTES, f"{name} is not a bounded file")
            return tar.extractfile(member).read()
        try:
            index = feed.strict_json(blob("index.json"), "OCI index")
            manifests = index.get("manifests") if isinstance(index, dict) else None
            c.require(isinstance(manifests, list) and len(manifests) == 1, "the OCI layout must hold exactly one image")
            c.require(manifests[0].get("digest") == "sha256:" + digest,
                      f"index.json points at another manifest than {Path(manifest_file).name}")
            c.require(blob(f"blobs/sha256/{digest}") == manifest_bytes, "the layout's manifest blob differs")
            manifest = feed.strict_json(manifest_bytes, "OCI manifest")
            config_digest = manifest["config"]["digest"]
            c.require(isinstance(config_digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest),
                      "invalid OCI config digest")
            config = blob("blobs/sha256/" + config_digest[7:])
            c.require(hashlib.sha256(config).hexdigest() == config_digest[7:], "the OCI config blob differs from its digest")
            labels = (feed.strict_json(config, "OCI config").get("config") or {}).get("Labels") or {}
        except KeyError as error:
            raise c.InvalidNightly(f"the OCI layout lacks {error}") from None
    ref = labels.get("org.flatpak.ref")
    c.require(isinstance(ref, str), "the OCI image has no org.flatpak.ref label")
    return ref


def tarball_top(path):
    tops = set()
    with tarfile.open(path, mode="r:gz") as tar:
        for member in tar:
            name = member.name[2:] if member.name.startswith("./") else member.name
            tops.add(name.split("/", 1)[0])
    return tops


# ---- Identity checks --------------------------------------------------------------------------

def check_pins(manifest, pins, registry):
    """The pinned stable Flatpak ID, once the manifest's prefix is the pinned prefix."""
    prefix, flatpak_id = feed.pinned_packages(manifest["app"], *pins, registry)
    c.require(manifest["prefix"] == prefix,
              f"the producer's package prefix {manifest['prefix']!r} is not the pinned {prefix!r}")
    return flatpak_id


def nightly_relation(name, prefix):
    """Whether a package relation may name `name`: only the app's own nightly packages.

    Signed by the stable key, a relation naming any other package would let the nightly take
    over or remove it. arch-package names `<P>-nightly-bin` and `-git` as conflicts.
    """
    nightly = f"{prefix}-nightly"
    return name in (nightly, "ai.storyteller." + nightly) or name.startswith(nightly + "-")


def check_relations(name, what, names, prefix):
    foreign = sorted(n for n in names if not nightly_relation(n, prefix))
    c.require(not foreign, f"{name}: {what} names {', '.join(map(repr, foreign))}, not only {prefix}-nightly packages")


def rpm_relation_name(value):
    """The package an rpm relation names: config(N) and N(arch) both name N."""
    value = value.strip()
    if value.startswith("config(") and value.endswith(")"):
        return value[len("config("):-1]
    return value.split("(", 1)[0].split(" ", 1)[0].strip()


def check_identity(manifest, name, path, environment, registry, flatpak_id):
    """Refuse a package whose own metadata isn't the nightly identity its name claims."""
    info = feed.classify(name)
    prefix, nv, kind = manifest["prefix"], manifest["nv"], info["kind"]
    package, arch = f"{prefix}-nightly", info.get("arch")
    if info["part"] == "launcher":
        inventory.verify_archive(manifest["app"], info["platform"], path, manifest["identity"], None, registry)
    elif kind == "deb":
        control = deb_control(path, environment)
        c.require(control.get("Package") == package, f"{name}: deb Package is {control.get('Package')!r}, not {package}")
        c.require(re.fullmatch(re.escape(nv) + r"(-[1-9][0-9]*)?", control.get("Version", "")),
                  f"{name}: deb Version is {control.get('Version')!r}, not {nv}")
        c.require(control.get("Architecture") == DEB_ARCH[arch], f"{name}: deb Architecture differs from {arch}")
        for field in DEB_RELATIONS:
            check_relations(name, f"deb {field}", relation_names(control.get(field)), prefix)
    elif kind == "rpm":
        header = rpm_header(path)
        c.require(header.get("NAME") == package, f"{name}: rpm Name is {header.get('NAME')!r}, not {package}")
        c.require(header.get("VERSION") == nv, f"{name}: rpm Version is {header.get('VERSION')!r}, not {nv}")
        c.require(header.get("ARCH") == arch, f"{name}: rpm Arch differs from {arch}")
        for tag, field in RPM_RELATIONS.items():
            check_relations(name, f"rpm {field}", {rpm_relation_name(v) for v in header.get(tag, [])}, prefix)
    elif kind == "arch_pkg":
        values = pkginfo(path, environment)
        stable = f"ai.storyteller.{prefix}"
        c.require(values.get("pkgname") == [stable + "-nightly"], f"{name}: pkgname is {values.get('pkgname')}")
        c.require(len(values.get("pkgver", [])) == 1 and values["pkgver"][0].rsplit("-", 1)[0] == nv,
                  f"{name}: pkgver is {values.get('pkgver')}, not {nv}")
        c.require(values.get("arch") == [arch], f"{name}: pacman arch differs from {arch}")
        for key in PACMAN_RELATIONS:
            check_relations(name, f"pacman {key}", {re.split(r"[<>=]", v, maxsplit=1)[0] for v in values.get(key, [])},
                            prefix)
    elif kind == "tar_gz":
        stem = name[: -len(".tar.gz")]
        c.require(tarball_top(path) == {stem}, f"{name}: every member must sit under {stem}/")
    elif kind == "flatpak":
        return flatpak_ref(name, flatpak_bundle_ref(path), arch, flatpak_id)
    return None


def flatpak_ref(name, ref, arch, flatpak_id):
    expected = feed.nightly_flatpak_ref(flatpak_id, arch)
    c.require(ref == expected, f"{name}: Flatpak ref {ref!r} is not {expected}")
    return ref


def precheck_files(manifest, files, environment, registry, flatpak_id):
    """Identity checks over {name: path}; the OCI layout tar, when present, must match its bundle."""
    refs = {}
    for name in sorted(files):
        info = feed.classify(name)
        if info["part"] == "launcher" and info["platform"] != "linux":
            continue
        if info["kind"] in ("oci_manifest", "oci_archive"):
            continue
        ref = check_identity(manifest, name, files[name], environment, registry, flatpak_id)
        if ref:
            refs[info["arch"]] = ref
    for name, path in sorted(files.items()):
        info = feed.classify(name)
        if info["kind"] == "oci_archive":
            manifest_name = name[: -len("oci.tar")] + "oci-manifest.json"
            c.require(manifest_name in files, f"{manifest_name} must accompany {name}")
            ref = flatpak_ref(name, oci_archive_ref(path, files[manifest_name]), info["arch"], flatpak_id)
            c.require(refs.get(info["arch"]) == ref, f"{name}: the OCI ref differs from the bundle's {refs.get(info['arch'])}")
    return refs


def admitted_distro(manifest, directory):
    """{name: path} of the admitted files sign-linux handles: distro files and the Linux launcher tar."""
    names = [e["name"] for e in manifest["files"]
             if not (e.get("platform") and e["platform"] != "linux")]
    return {name: inventory.admitted_file(manifest, directory, name) for name in names}


def precheck(admission, directory, environment, pins, registry=None):
    registry = registry if registry is not None else feed.load_registry()
    manifest, _ = inventory.load_admission(admission, registry)
    flatpak_id = check_pins(manifest, pins, registry)
    files = admitted_distro(manifest, directory)
    refs = precheck_files(manifest, files, clean_env(environment), registry, flatpak_id)
    return {"checked": sorted(files), "flatpak_refs": refs}


# ---- Signing ----------------------------------------------------------------------------------

def import_signing_key(home, armored, primary, subkey, environment):
    """Import LINUX_SIGNING_KEY into `home`; return the fingerprint gpg will sign with.

    The key gpg picks for --local-user (the newest usable signing key, as import-signing-key
    finds it) must be the pinned subkey of the pinned primary.
    """
    gpg = tool("gpg", environment)
    run([gpg, "--homedir", home, "--batch", "--quiet", "--import"], environment, stdin=armored.encode() + b"\n")
    keys = gpg_listing(home, environment, secret=True)
    c.require(len(keys) == 1 and keys[0][0] == primary,
              f"LINUX_SIGNING_KEY is not the registry's pinned key {primary}")
    usable = [f for f, caps, validity in keys[0][1] if "s" in caps and not re.search("[erd]", validity)]
    c.require(usable and usable[-1] == subkey, f"LINUX_SIGNING_KEY does not sign with the pinned subkey {subkey}")
    try:
        run([gpg, "--homedir", home, "--batch", "--pinentry-mode", "error", "--local-user", subkey,
             "--detach-sign", "-o", os.devnull], environment, stdin=b"check")
    except c.InvalidNightly:
        raise c.InvalidNightly("LINUX_SIGNING_KEY can't sign without a passphrase") from None
    return subkey


def collect_signed(manifest, signed_dirs, registry):
    """{name: path} of the signed Windows and macOS launcher assets, exactly one per platform."""
    expected = {}
    for platform in manifest["launcher_platforms"]:
        if platform != "linux":
            _, row = inventory.app_target(manifest["app"], platform, registry)
            expected[inventory.asset_names(row, manifest["identity"])["asset"]] = platform
    found = {}
    for directory in signed_dirs:
        directory = c.owned_path(directory)
        c.require(directory.is_dir() and not directory.is_symlink(), f"{directory} is not a directory")
        for path in directory.iterdir():
            c.require(path.name in expected and path.name not in found and stat.S_ISREG(path.lstat().st_mode),
                      f"unexpected signed file {path.name}")
            found[path.name] = path
    c.require(set(found) == set(expected), "signed launcher assets missing: " + ", ".join(sorted(set(expected) - set(found))))
    return found


def is_layout(name):
    info = feed.classify(name)
    return info is not None and info["kind"] == "oci_archive"


def write_sums(directory):
    names = sorted(p.name for p in Path(directory).iterdir()
                   if p.name not in (feed.SUMS_NAME, feed.SUMS_SIGNATURE) and not is_layout(p.name))
    lines = "".join(f"{c.digest_file(directory, name)[1]}  {name}\n" for name in names)
    with open(c.owned_path(Path(directory) / feed.SUMS_NAME), "x", encoding="utf-8") as stream:
        stream.write(lines)
    return names


def sign(admission, directory, signed_dirs, output, environment, pins, registry=None):
    registry = registry if registry is not None else feed.load_registry()
    manifest, _ = inventory.load_admission(admission, registry)
    env = clean_env(environment)
    flatpak_id = check_pins(manifest, pins, registry)
    key_file, primary, subkey = pinned_key(manifest["app"], registry)
    files = admitted_distro(manifest, directory)
    # Every identity is proven before the key is imported.
    precheck_files(manifest, files, env, registry, flatpak_id)
    signed = collect_signed(manifest, signed_dirs or [], registry)
    armored = environment.get("LINUX_SIGNING_KEY", "")
    c.require(armored.strip(), "LINUX_SIGNING_KEY is required; an unsigned nightly is refused")
    output = c.owned_path(output)
    c.require(not output.is_symlink() and (not output.exists() or (output.is_dir() and not any(output.iterdir()))),
              f"{output} must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    def stage(entries):
        for name, path in sorted(entries.items()):
            before = c.digest_file(path.parent, name)
            stream, _ = c.regular_file(path.parent, name)
            with stream, open(output / name, "xb") as copy:
                shutil.copyfileobj(stream, copy, 1024 * 1024)
            c.require(c.digest_file(output, name) == before, f"{name} changed while staging")

    stage({**{n: p for n, p in files.items() if not is_layout(n)}, **signed})
    with tempfile.TemporaryDirectory(prefix="nightly-gpg-", dir=inventory_root(environment)) as temporary:
        home = Path(temporary) / "secret"
        home.mkdir(mode=0o700)
        try:
            fingerprint = import_signing_key(home, armored, primary, subkey, env)
            run(["bash", SIGN_LINUX, output],
                clean_env(environment, {"GNUPGHOME": str(home), "SIGNING_FPR": fingerprint,
                                        "OCI_REGISTRY": "ghcr.io/" + manifest["repository"].lower()}), timeout=3600)
            if feed.app_record(registry, manifest["app"]):
                feed.write_descriptor(manifest, output, registry)
            write_sums(output)
            run([tool("gpg", env), "--homedir", home, "--batch", "--yes", "--pinentry-mode", "error",
                 "--local-user", fingerprint, "--armor", "--detach-sign", "-o", output / feed.SUMS_SIGNATURE,
                 output / feed.SUMS_NAME], env)
        finally:
            stop_agent(home, env)
    # The OCI layout tars ride along for the publisher, which pushes them and never uploads them.
    # They arrive only now: sign-linux.sh signs every *-linux-* file it finds.
    stage({n: p for n, p in files.items() if is_layout(n)})
    result = verify_final_files(manifest, output, environment, registry, flatpak_id)
    _, sums = c.digest_file(output, feed.SUMS_NAME)
    return {**result, "sha256sums_sha256": sums}


def inventory_root(environment):
    return c.owned_path(environment.get("RUNNER_TEMP") or environment.get("GITHUB_WORKSPACE") or Path.cwd())


# ---- Final verification -----------------------------------------------------------------------

def deb_origin(keyring, path, pin, environment, work):
    """debsigs' _gpgorigin: a signature over debian-binary, control and data, in archive order."""
    members = ar_members(path)
    origins = [m for m in members if m[0] == "_gpgorigin"]
    c.require(len(origins) == 1 and all(not m[0].startswith("_gpg") or m[0] == "_gpgorigin" for m in members),
              f"{Path(path).name} lacks exactly one _gpgorigin signature")
    folder = Path(tempfile.mkdtemp(prefix="deb-", dir=work))
    signature = folder / "_gpgorigin"
    signature.write_bytes(ar_read(path, origins[0][1], origins[0][2]))
    with open(folder / "signed", "wb") as stream:
        for name, offset, size in members:
            if not name.startswith("_gpg"):
                stream.write(ar_read(path, offset, size))
    verify_signature(keyring, signature, folder / "signed", pin, environment)


def check_oci_signature(keyring, signature, manifest_file, registry_ref, pin, environment, work):
    body = feed.strict_json(verify_signed_message(keyring, signature, pin, environment, work), "OCI signature")
    critical = body.get("critical") if isinstance(body, dict) else None
    digest = "sha256:" + hashlib.sha256(c.bounded_bytes(manifest_file, c.MAX_JSON_BYTES, "OCI manifest")).hexdigest()
    c.require(isinstance(critical, dict) and critical.get("type") == "atomic container signature"
              and critical.get("image", {}).get("docker-manifest-digest") == digest
              and critical.get("identity", {}).get("docker-reference") == registry_ref,
              f"{Path(signature).name} does not sign {digest} for {registry_ref}")


def verify_final_files(manifest, directory, environment, registry, flatpak_id):
    directory = c.owned_path(directory)
    env = clean_env(environment)
    # Both are required: a check that is skipped when its tool is missing proves nothing.
    skopeo, rpmkeys = tool("skopeo", env), tool("rpmkeys", env)
    names, layouts = [], []
    for path in directory.iterdir():
        c.require(stat.S_ISREG(path.lstat().st_mode), f"{path.name} is not a regular file")
        (layouts if is_layout(path.name) else names).append(path.name)
    expected = feed.check_asset_set(names, manifest, registry, "final")
    produced = {e["name"]: e for e in manifest["files"]}
    # Producer layout tars may sit beside the assets for the publisher; they are never released.
    for name in layouts:
        c.require(name in produced and c.digest_file(directory, name) == (produced[name]["size"], produced[name]["sha256"]),
                  f"{name} differs from the producer's bytes")
    for name, info in expected.items():
        if name in produced and info["kind"] in UNCHANGED_KINDS:
            c.require(c.digest_file(directory, name) == (produced[name]["size"], produced[name]["sha256"]),
                      f"{name} differs from the producer's bytes")
    pin_file, primary, subkey = pinned_key(manifest["app"], registry)
    pin = (primary, subkey)
    oci_ref = "ghcr.io/" + manifest["repository"].lower()
    with tempfile.TemporaryDirectory(prefix="nightly-verify-", dir=inventory_root(environment)) as temporary:
        work = Path(temporary)
        keyring = public_keyring(pinned_bytes(pin_file), primary, subkey, work / "pinned", env)
        published = work / "published"
        try:
            # Trust never comes from the published key; it must merely be the same key.
            public_keyring(c.bounded_bytes(directory / feed.KEY_NAME, c.MAX_JSON_BYTES, feed.KEY_NAME),
                           primary, subkey, published, env, feed.KEY_NAME)
            for name, info in sorted(expected.items()):
                if info["kind"] in ("asc", "sig"):
                    verify_signature(keyring, directory / name, directory / info["signs"], pin, env,
                                     armored=info["kind"] == "asc")
                elif info["kind"] == "sums_signature":
                    verify_signature(keyring, directory / name, directory / feed.SUMS_NAME, pin, env, armored=True)
                elif info["kind"] == "oci_sig":
                    check_oci_signature(keyring, directory / name, directory / info["signs"], oci_ref, pin, env, work)
                    run([skopeo, "standalone-verify", directory / info["signs"], oci_ref, subkey, directory / name],
                        {**env, "GNUPGHOME": str(keyring.parent)})
                elif info["kind"] == "deb":
                    deb_origin(keyring, directory / name, pin, env, work)
                    check_identity(manifest, name, directory / name, env, registry, flatpak_id)
                elif info["kind"] == "rpm":
                    check_identity(manifest, name, directory / name, env, registry, flatpak_id)
            rpms = sorted(n for n, i in expected.items() if i["kind"] == "rpm")
            if rpms:
                db = work / "rpmdb"
                run([rpmkeys, "--dbpath", db, "--import", pin_file], env)
                for name in rpms:
                    out = run([rpmkeys, "--dbpath", db, "--checksig", directory / name], env).decode()
                    c.require("signatures OK" in out, f"{name}: rpm signature check failed")
        finally:
            stop_agent(work / "pinned", env)
            stop_agent(published, env)
    sums_text = c.bounded_bytes(directory / feed.SUMS_NAME, c.MAX_JSON_BYTES, "SHA256SUMS.txt").decode("utf-8")
    listed = dict((name, digest) for digest, name in feed.parse_sha256sums(sums_text))
    c.require(len(listed) == len(sums_text.splitlines())
              and set(listed) == set(names) - {feed.SUMS_NAME, feed.SUMS_SIGNATURE},
              "SHA256SUMS.txt does not list exactly every other asset")
    for name, digest in listed.items():
        c.require(c.digest_file(directory, name)[1] == digest, f"{name} differs from SHA256SUMS.txt")
    descriptor = None
    if feed.app_record(registry, manifest["app"]):
        body = c.bounded_bytes(directory / feed.MANIFEST_NAME, c.MAX_JSON_BYTES, "NIGHTLY.json")
        descriptor = feed.check_descriptor(body, feed.synthetic_release(manifest, directory), manifest["app"],
                                           registry, sums=sums_text)
    return {"assets": sorted(names), "descriptor": descriptor is not None}


def verify_final(admission, directory, environment, pins, registry=None):
    registry = registry if registry is not None else feed.load_registry()
    manifest, _ = inventory.load_admission(admission, registry)
    flatpak_id = check_pins(manifest, pins, registry)
    environment = {k: v for k, v in environment.items() if k != "LINUX_SIGNING_KEY"}
    return verify_final_files(manifest, directory, environment, registry, flatpak_id)


# ---- CLI --------------------------------------------------------------------------------------

def main(argv):
    parser = argparse.ArgumentParser(prog="linux", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("precheck", "sign", "verify-final"))
    parser.add_argument("--admission", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--prefix", required=True, help="the app's stable package name, pinned by the caller")
    parser.add_argument("--flatpak-id", required=True, help="the app's stable Flatpak ID, pinned by the caller")
    parser.add_argument("--signed", nargs="*", default=[])
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    pins = (args.prefix, args.flatpak_id)
    try:
        if args.mode == "precheck":
            result = precheck(args.admission, args.directory, environment, pins)
        elif args.mode == "sign":
            c.require(args.output, "sign needs --output")
            result = sign(args.admission, args.directory, args.signed, args.output, environment, pins)
            if destination := os.environ.get("GITHUB_OUTPUT"):
                with open(destination, "a", encoding="utf-8") as stream:
                    stream.write(f"sha256sums-sha256={result['sha256sums_sha256']}\n")
        else:
            c.require(not args.signed and not args.output, "verify-final takes only --admission and --directory")
            result = verify_final(args.admission, args.directory, environment, pins)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (c.InvalidNightly, OSError, ValueError, KeyError, TypeError, UnicodeError, tarfile.TarError) as error:
        print(f"::error::nightly linux {args.mode} refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
