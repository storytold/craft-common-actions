#!/usr/bin/env python3
"""Nightly retention, then the artcraft-services version update. Every failure only warns.

  retain --admission DIR [--keep 3]

- Keeps the newest KEEP prereleases of the app's tag family by publication time and deletes the
  rest with their tags.
- GHCR: deletes only image versions whose every tag is <...>-nightly-<arch>, that don't carry
  nightly-<arch>, and that don't belong to a kept nightly. Stable and untagged images are never
  candidates.
- Dispatches storytold/artcraft-services update-craft-release-versions.yml when
  ARTCRAFT_SERVICES_DISPATCH_TOKEN is set.
"""
import argparse
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish  # noqa: E402
from publish import APIError, InvalidNightly, ni, warn, summary  # noqa: E402

WEBSITE_REPOSITORY = "storytold/artcraft-services"
WEBSITE_WORKFLOW = "update-craft-release-versions.yml"
# Package versions accumulate per stable release and per nightly run.
MAX_PACKAGE_PAGES = 50


def in_family(item, family):
    if not isinstance(item, dict) or item.get("draft") is not False or item.get("prerelease") is not True:
        return False
    tag = item.get("tag_name")
    if family == ni.FAMILY_V:
        return ni.canonical(tag) is not None
    return ni.family_of(tag) == ni.FAMILY_NIGHTLY


def by_publication(releases, family):
    """The family's published prereleases, newest publication first."""
    items = [item for item in releases if in_family(item, family)]
    keyed = []
    for item in items:
        published = ni.parse_rfc3339(item.get("published_at"))
        if published is None:
            raise InvalidNightly(f"{item['tag_name']} has no readable publication time")
        keyed.append(((published, ni.family_key(item["tag_name"], family)), item))
    keyed.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in keyed]


def nv_of(tag, family):
    return ni.nv_from_identity(tag[1:]) if family == ni.FAMILY_V else tag[len(ni.FAMILY_NIGHTLY):]


def stale_images(versions, kept_nvs, arches=publish.OCI_ARCHES):
    """[(version id, tags)] of nightly image versions no kept nightly and no nightly-<arch> tag uses."""
    stale = []
    for version in versions:
        tags = ((version.get("metadata") or {}).get("container") or {}).get("tags") or []
        if not tags or not all(isinstance(t, str) for t in tags) or type(version.get("id")) is not int:
            continue
        for arch in arches:
            if not all(re.search(rf"(^|-)nightly-{arch}$", t) for t in tags):
                continue
            moving = f"nightly-{arch}" in tags
            kept = any(t == f"{nv}-nightly-{arch}" for nv in kept_nvs for t in tags)
            if not moving and not kept:
                stale.append((version["id"], tags))
    return stale


def delete_release(api, repository, item):
    tag = item["tag_name"]
    try:
        api.request("DELETE", f"repos/{repository}/releases/{item['id']}")
    except APIError as error:
        warn(f"Could not delete the nightly prerelease {tag}: {error}")
        return
    try:
        api.request("DELETE", f"repos/{repository}/git/refs/tags/{quote(tag, safe='')}")
    except APIError as error:
        if error.status != 404:
            warn(f"Deleted the prerelease {tag} but not its tag: {error}")
            return
    summary(f"Deleted the nightly prerelease {tag}.")


def prune_releases(api, nightly, keep):
    """Delete the family's prereleases past the newest `keep`; returns the kept tags, or None."""
    ordered = by_publication(publish.releases_of(api, nightly.repository), nightly.family)
    kept, old = ordered[:keep], ordered[keep:]
    tags = [item["tag_name"] for item in kept]
    if nightly.tag not in tags:
        # Our own release must be among the newest; anything else means the listing is not current.
        warn(f"{nightly.tag} is not among the newest {keep} nightly prereleases ({', '.join(tags) or 'none'}); "
             "nothing deleted.")
        return None
    for item in old:
        delete_release(api, nightly.repository, item)
    return tags


def prune_images(api, nightly, kept_tags):
    owner, name = nightly.repository.split("/", 1)
    package = name.lower()
    account = api.request("GET", f"users/{owner}")
    scope = "orgs" if isinstance(account, dict) and account.get("type") == "Organization" else "users"
    base = f"{scope}/{owner}/packages/container/{package}/versions"
    versions = api.pages(base, MAX_PACKAGE_PAGES)
    kept_nvs = [nv_of(tag, nightly.family) for tag in kept_tags]
    for version_id, tags in stale_images(versions, kept_nvs):
        try:
            api.request("DELETE", f"{base}/{version_id}")
            summary(f"Deleted ghcr.io/{owner}/{package} version {version_id} ({', '.join(tags)}).")
        except APIError as error:
            warn(f"Could not delete ghcr.io/{owner}/{package} version {version_id}: {error}")


def dispatch_website(api, environ):
    token = environ.get("ARTCRAFT_SERVICES_DISPATCH_TOKEN")
    if not token:
        warn(f"ARTCRAFT_SERVICES_DISPATCH_TOKEN is not set; skipping {WEBSITE_REPOSITORY} {WEBSITE_WORKFLOW} dispatch.")
        return
    if api.dispatch(WEBSITE_REPOSITORY, WEBSITE_WORKFLOW, token):
        summary(f"Dispatched {WEBSITE_REPOSITORY} {WEBSITE_WORKFLOW}.")
    else:
        warn(f"Failed to dispatch {WEBSITE_WORKFLOW} on {WEBSITE_REPOSITORY}.")


def main(argv, api=None, environ=None):
    parser = argparse.ArgumentParser(prog="retain", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--admission", required=True)
    parser.add_argument("--keep", type=int, default=3)
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    api = api if api is not None else publish.GitHub()
    environ = os.environ if environ is None else environ
    errors = (InvalidNightly, OSError, ValueError, KeyError, TypeError)
    try:
        nightly = publish.load_admission(args.admission, environ=environ)
    except errors as error:
        warn(f"Could not read the admission; no nightly deleted: {error}")
        nightly = None
    kept = None
    if nightly is not None:
        try:
            kept = prune_releases(api, nightly, args.keep)
        except errors as error:
            warn(f"Could not prune the nightly prereleases; none deleted: {error}")
    if kept:
        try:
            prune_images(api, nightly, kept)
        except errors as error:
            warn(f"Could not prune the nightly images; none deleted: {error}")
    dispatch_website(api, environ)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
