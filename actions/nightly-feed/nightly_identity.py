"""Nightly identity, tag families and ordering.

identity  <X.Y.Z>-nightly.<YYYYMMDD>.<run_id>.<run_attempt>.g<sha12>
nv        <X.Y.Z>.<YYYYMMDD>.<run_id>.<run_attempt>  (distro package version)
tag       v<identity> for apps in the launcher registry, nightly_<nv> for every other app

The parsing and ordering functions port craft-launcher's rules exactly
(crates/core/src/version.rs, release.rs, nightly.rs and crates/native/src/nightly_lookup.rs),
so the publisher can refuse what the launcher would refuse before anything is public.
"""
import calendar
import datetime as dt
import functools
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nightly_common import HEX40, InvalidNightly, require  # noqa: E402,F401  (InvalidNightly re-exported)

FAMILY_V = "v"
FAMILY_NIGHTLY = "nightly_"
FAMILIES = (FAMILY_V, FAMILY_NIGHTLY)
# release.rs valid_tag and version::parse both refuse longer tags.
MAX_TAG = 64
U64 = 2**64 - 1

_NUM = r"(?:0|[1-9][0-9]*)"
IDENTITY = re.compile(
    rf"(?P<base>(?P<major>{_NUM})\.(?P<minor>{_NUM})\.(?P<patch>{_NUM}))-nightly\."
    r"(?P<date>[0-9]{8})\.(?P<run_id>[1-9][0-9]*)\.(?P<attempt>[1-9][0-9]*)\.g(?P<sha12>[0-9a-f]{12})")
NV = re.compile(rf"({_NUM})\.({_NUM})\.({_NUM})\.([0-9]{{8}})\.([1-9][0-9]*)\.([1-9][0-9]*)")
BASE = re.compile(rf"{_NUM}\.{_NUM}\.{_NUM}")
_IDENT = re.compile(r"[0-9A-Za-z-]+")


# ---- SemVer, as the semver 1.x crate parses and orders it ---------------------------------

def parse_semver(text):
    """Strict SemVer 2.0 (semver crate `Version::parse`), or None.

    Returns (major, minor, patch, pre, build) with pre and build as tuples of identifiers.
    """
    if not isinstance(text, str) or not text:
        return None
    core, plus, build = text.partition("+")
    core, dash, pre = core.partition("-")
    numbers = core.split(".")
    if len(numbers) != 3 or not all(re.fullmatch(_NUM, n) for n in numbers):
        return None
    major, minor, patch = (int(n) for n in numbers)
    if max(major, minor, patch) > U64:
        return None
    pre_ids = tuple(pre.split(".")) if dash else ()
    build_ids = tuple(build.split(".")) if plus else ()
    for ident in pre_ids:
        if not _IDENT.fullmatch(ident) or (ident.isdigit() and len(ident) > 1 and ident[0] == "0"):
            return None
    if not all(_IDENT.fullmatch(ident) for ident in build_ids):
        return None
    return major, minor, patch, pre_ids, build_ids


def format_semver(version):
    major, minor, patch, pre, build = version
    text = f"{major}.{minor}.{patch}"
    if pre:
        text += "-" + ".".join(pre)
    if build:
        text += "+" + ".".join(build)
    return text


def _precedence(version):
    major, minor, patch, pre, _ = version
    # A release outranks every prerelease of the same core; numeric identifiers sort before
    # alphanumeric ones; a longer identifier list wins when the shared prefix is equal.
    pre_key = (1,) if not pre else (0, tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in pre))
    return major, minor, patch, pre_key


def precedence_key(value):
    """SemVer precedence key (version::compare): build metadata ignored.

    Accepts a version string, a tag with one leading lowercase `v`, or a parse_semver tuple.
    """
    if isinstance(value, tuple):
        return _precedence(value)
    require(isinstance(value, str), "version must be text")
    parsed = parse_semver(value[1:] if value.startswith("v") else value)
    require(parsed is not None, f"not a SemVer version: {value!r}")
    return _precedence(parsed)


# ---- craft-launcher release.rs / version.rs ------------------------------------------------

def _plain_name(text):
    return bool(text) and all(c.isascii() and (c.isalnum() or c in "._-+") for c in text)


def valid_tag(tag):
    """release.rs valid_tag."""
    return isinstance(tag, str) and len(tag) <= MAX_TAG and not tag.startswith(".") and _plain_name(tag)


def valid_file_name(name):
    """release.rs valid_file_name."""
    if not isinstance(name, str) or len(name) > 200 or name.startswith(".") or name.endswith("."):
        return False
    stem = name.split(".")[0].upper()
    device = stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)[1-9]", stem) is not None
    return _plain_name(name) and not device


def launcher_version(text):
    """version.rs parse: the version the launcher reads from a tag, or None."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    t = t.removeprefix("artcraft-v")
    if t[:1] in ("v", "V"):
        t = t[1:]
    if not t or len(t.encode()) > MAX_TAG:
        return None
    if "~" in t:
        t = t.split("-", 1)[0].replace("~", "-")
    if (parsed := parse_semver(t)) is not None:
        return parsed
    cut = min([i for i in (t.find("-"), t.find("+")) if i >= 0], default=len(t))
    core, rest = t[:cut], t[cut:]
    parts = core.split(".")
    if len(parts) == 2 and all(p and p.isascii() and p.isdigit() for p in parts):
        return parse_semver(f"{core}.0{rest}")
    return None


def _nightly_pre(version):
    return version is not None and any(part.lower() == "nightly" for part in version[3])


def is_nightly(tag, prerelease=True):
    """release.rs is_nightly: a prerelease whose parsed prerelease names `nightly`."""
    return bool(prerelease) and _nightly_pre(launcher_version(tag))


def has_nightly_part(tag):
    """nightly_lookup.rs: the tag declares a nightly when any `.`/`-` part is `nightly`."""
    return isinstance(tag, str) and any(part.lower() == "nightly" for part in re.split(r"[.-]", tag))


def canonical(tag, prerelease=True):
    """release.rs canonical_nightly_version, for a tag the release parser accepted.

    Returns the parsed version, or None when the launcher would not rank the tag.
    """
    if not prerelease or not valid_tag(tag):
        return None
    bare = tag[1:] if tag.startswith("v") else tag
    parsed = parse_semver(bare)
    if parsed is None or format_semver(parsed) != bare or launcher_version(tag) != parsed:
        return None
    if not parsed[3] or parsed[3][0] != "nightly":
        return None
    return parsed


def immutable_tag(tag, source_commit):
    """nightly.rs immutable_tag: the tag's prerelease is nightly.<date>.<run>.<attempt>.g<sha12>."""
    version = launcher_version(tag)
    if version is None or not isinstance(source_commit, str):
        return False
    pre = version[3]
    if len(pre) != 5:
        return False
    channel, date, run, attempt, revision = pre

    def positive_u64(text):
        return text.isascii() and text.isdigit() and 0 < int(text) <= U64

    return (channel == "nightly" and len(date) == 8 and date.isascii() and date.isdigit()
            and positive_u64(run) and positive_u64(attempt) and len(source_commit) == 40
            and revision == "g" + source_commit[:12])


def _ranking(a, b):
    (key_a, tag_a), (key_b, tag_b) = a, b
    if key_a != key_b:
        return -1 if key_a < key_b else 1
    # Equal precedence: the reverse tag order before the final reversal, as release.rs does.
    return (tag_b > tag_a) - (tag_b < tag_a)


def rank(tags):
    """release.rs ranked_nightly_candidates (Canonical): best first.

    Canonical prerelease tags only; others are dropped. Equal precedence prefers the
    lexically smallest tag.
    """
    candidates = [(_precedence(v), tag) for tag in tags if (v := canonical(tag)) is not None]
    candidates.sort(key=functools.cmp_to_key(_ranking))
    return [tag for _, tag in reversed(candidates)]


def launcher_scan(releases):
    """What craft-launcher's nightly lookup makes of one repository's release list.

    `releases` are GitHub API release objects (tag_name, draft, prerelease). Returns the
    candidate tags, best first. Raises InvalidNightly where the launcher's whole lookup would
    fail: a prerelease tag its release parser refuses, or a nightly-looking tag that isn't
    canonical, or a repeated tag.
    """
    seen = set()
    for item in releases:
        require(isinstance(item, dict), "release entry is not an object")
        draft, prerelease, tag = item.get("draft"), item.get("prerelease"), item.get("tag_name")
        require(isinstance(draft, bool), "release has indeterminate publication state")
        if draft:
            continue
        require(isinstance(prerelease, bool), "release has indeterminate channel state")
        if not prerelease:
            continue
        require(valid_tag(tag), f"prerelease tag {tag!r} breaks craft-launcher's nightly lookup")
        if not has_nightly_part(tag) and not is_nightly(tag):
            continue
        require(canonical(tag) is not None,
                f"prerelease {tag} declares a nightly but is not canonical; craft-launcher's lookup fails on it")
        require(tag not in seen, f"release {tag} is listed twice")
        seen.add(tag)
    return rank(seen)


# ---- Identity --------------------------------------------------------------------------------

def base_version(version):
    """The Cargo version's X.Y.Z, prerelease and build suffix dropped."""
    require(isinstance(version, str), "missing package version")
    base = version.strip().split("+", 1)[0].split("-", 1)[0]
    require(BASE.fullmatch(base) is not None, f"base version {version!r} is not X.Y.Z")
    return base


def parse_rfc3339(text):
    """time.rs parse_rfc3339: Unix seconds, or None."""
    if not isinstance(text, str):
        return None
    b = text.strip()
    if len(b) < 19 or b[4] != "-" or b[7] != "-" or b[10] not in "Tt ":
        return None

    def num(start, end):
        part = b[start:end]
        return int(part) if part.isascii() and part.isdigit() and len(part) == end - start else None

    values = [num(0, 4), num(5, 7), num(8, 10), num(11, 13), num(14, 16), num(17, 19)]
    if None in values:
        return None
    year, month, day, hour, minute, second = values
    if not 1 <= month <= 12 or not 1 <= day <= 31 or hour > 23 or minute > 59 or second > 60:
        return None
    tail = b[19:]
    if tail.startswith("."):
        tail = tail[1:].lstrip("0123456789")
    if tail in ("", "Z", "z"):
        offset = 0
    elif len(tail) == 6 and tail[0] in "+-" and tail[1:3].isdigit() and tail[4:6].isdigit():
        offset = (-1 if tail[0] == "-" else 1) * (int(tail[1:3]) * 3600 + int(tail[4:6]) * 60)
    else:
        return None
    try:
        first = calendar.timegm((year, month, 1, 0, 0, 0))
    except (ValueError, OverflowError):
        return None
    return first + (day - 1) * 86400 + hour * 3600 + minute * 60 + second - offset


def run_created(created_at):
    """(YYYYMMDD, Unix seconds) of a workflow run's created_at, in UTC."""
    seconds = parse_rfc3339(created_at)
    require(seconds is not None and seconds > 0, f"invalid run created_at {created_at!r}")
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime("%Y%m%d"), seconds


def _calendar_date(date):
    try:
        dt.datetime.strptime(date, "%Y%m%d")
    except ValueError:
        return False
    return True


def make_identity(base, date, run_id, attempt, source_commit):
    require(isinstance(base, str) and BASE.fullmatch(base) is not None, "base version must be X.Y.Z")
    require(isinstance(date, str) and re.fullmatch(r"[0-9]{8}", date) is not None and _calendar_date(date),
            "date must be YYYYMMDD")
    for value, where in ((run_id, "run ID"), (attempt, "run attempt")):
        require(type(value) is int and 0 < value <= U64, f"invalid {where}")
    require(isinstance(source_commit, str) and HEX40.fullmatch(source_commit) is not None,
            "source commit must be a full lowercase SHA-1")
    identity = f"{base}-nightly.{date}.{run_id}.{attempt}.g{source_commit[:12]}"
    require(len(FAMILY_V + identity) <= MAX_TAG,
            f"tag v{identity} is longer than craft-launcher's {MAX_TAG}-character bound")
    return identity


def identity_parts(tag):
    """Fields of a `v<identity>` tag or a bare identity.

    Returns a dict with identity, base, major, minor, patch, date, run_id, attempt, sha12 and nv.
    """
    require(isinstance(tag, str), "identity must be text")
    identity = tag[1:] if tag.startswith("v") else tag
    match = IDENTITY.fullmatch(identity)
    require(match is not None and _calendar_date(match["date"]), f"not a nightly identity: {tag!r}")
    require(len(FAMILY_V + identity) <= MAX_TAG, f"tag v{identity} is longer than {MAX_TAG} characters")
    run_id, attempt = int(match["run_id"]), int(match["attempt"])
    require(run_id <= U64 and attempt <= U64, "run ID or attempt out of range")
    return {
        "identity": identity, "base": match["base"],
        "major": int(match["major"]), "minor": int(match["minor"]), "patch": int(match["patch"]),
        "date": match["date"], "run_id": run_id, "attempt": attempt, "sha12": match["sha12"],
        "nv": f"{match['base']}.{match['date']}.{run_id}.{attempt}",
    }


def nv_from_identity(identity):
    return identity_parts(identity)["nv"]


def nv_key(value):
    """Dotted-number order key of an nv or a `nightly_<nv>` tag."""
    require(isinstance(value, str), "nv must be text")
    nv = value.removeprefix(FAMILY_NIGHTLY)
    require(NV.fullmatch(nv) is not None, f"not a nightly package version: {value!r}")
    return tuple(int(part) for part in nv.split("."))


def tag_for(identity, family):
    parts = identity_parts(identity)
    if family == FAMILY_V:
        return FAMILY_V + parts["identity"]
    require(family == FAMILY_NIGHTLY, f"unknown tag family {family!r}")
    return FAMILY_NIGHTLY + parts["nv"]


def family_of(tag):
    """The family a tag belongs to by shape, or None."""
    if not isinstance(tag, str):
        return None
    if tag.startswith(FAMILY_NIGHTLY):
        return FAMILY_NIGHTLY if NV.fullmatch(tag[len(FAMILY_NIGHTLY):]) else None
    if tag.startswith(FAMILY_V) and IDENTITY.fullmatch(tag[1:]) and len(tag) <= MAX_TAG:
        return FAMILY_V
    return None


def family_key(tag, family):
    """Order key within one tag family: SemVer precedence for `v`, nv numbers for `nightly_`."""
    if family == FAMILY_V:
        parsed = canonical(tag)
        require(parsed is not None, f"{tag} is not a canonical nightly tag")
        return precedence_key(parsed)
    require(family == FAMILY_NIGHTLY, f"unknown tag family {family!r}")
    return nv_key(tag)


def newest_in_family(releases, family):
    """The newest non-draft release of a tag family (GitHub API objects), or None."""
    best = None
    for item in releases:
        require(isinstance(item, dict), "release entry is not an object")
        tag = item.get("tag_name")
        if item.get("draft") is not False:
            continue
        if family == FAMILY_V:
            if item.get("prerelease") is not True or canonical(tag) is None:
                continue
        elif family_of(tag) != FAMILY_NIGHTLY:
            continue
        key = family_key(tag, family)
        if best is None or key > best[0]:
            best = (key, item)
    return best[1] if best else None
