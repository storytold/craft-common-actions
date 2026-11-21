#!/usr/bin/env python3
"""Private keep-seven nightly pruning. Planning is read-only; apply is opt-in.

The optional Actions receipt adapter requires explicit reviewed source policy.
Release author IDs, matching text, workflow runs and a matching ref alone are
not proof of who created that ref. Unknown provenance is always retained.
"""
import argparse
import copy
import json
import os
import zipfile
from urllib.parse import quote, unquote

import nightly_feed as n
import nightly_policy as policy
from publish_nightly import APIError, HTTPTransport
from nightly_ownership import AmbiguousOwnership

SCHEMA = "craft-nightly-prune/v1"
KEEP = 7
MAX_REMOVALS = 10
MAX_PAGES = 20
PAGE_SIZE = 20


class UnknownProvenance(n.Unavailable):
    pass


class PublicationProvenance:
    """Trusted-code adapter, NOT a JSON claim or user-provided release marker.

    A future adapter must query authenticated, durable GitHub-side evidence
    binding repository, release ID, exact ref, full SHA and numeric bot actor ID
    to a successful reviewed producer publication; prove the lightweight ref
    was created by that bot and has not been replaced; and fail closed on gaps,
    ambiguity or stale/unavailable evidence. It must support rechecking the
    original publication after its release has been deleted, for exact retries.
    Returning True is a security boundary. No CLI option installs an adapter.
    """
    def verify(self, api, repository, release, bot_id):
        raise UnknownProvenance("lightweight-tag creator/publication provenance unavailable")


class PruneTransport(HTTPTransport):
    """Keep publisher transport unchanged; DELETE requires an exact empty 204."""
    def download_artifact(self, artifact_id):
        from nightly_ownership import download_artifact
        return download_artifact(self, artifact_id)

    def exchange(self, host, method, path, **kwargs):
        if method != "DELETE":
            return super().exchange(host, method, path, **kwargs)
        n.require(host == "api.github.com" and path.startswith(self.prefix + "/"), "invalid delete origin")
        suffix = path[len(self.prefix):]
        import re
        n.require(re.fullmatch(r"/releases/[1-9][0-9]*|/git/refs/tags/v[0-9A-Za-z.%_-]+", suffix), "invalid delete path")
        if suffix.startswith("/git/refs/tags/"):
            n.require(parse_tag(unquote(suffix.split("/tags/", 1)[1])) is not None, "ref deletion is limited to exact nightly tags")
        import http.client
        connection = http.client.HTTPSConnection(host, timeout=30)
        try:
            connection.request("DELETE", path, headers={
                "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "craft-nightly-prune/v1"})
            response = connection.getresponse()
            raw = response.read(1)
            if response.status != 204:
                raise APIError(response.status)
            n.require(raw == b"", "DELETE returned a nonempty response")
            return None
        finally:
            connection.close()


def parse_tag(tag):
    if not isinstance(tag, str) or len(tag) > 129 or not tag.startswith("v"):
        return None
    match = policy.NIGHTLY.fullmatch(tag[1:])
    if match is None:
        return None
    try:
        policy.semver_key(tag[1:])  # Includes real calendar date validation.
        run, attempt = int(match[3]), int(match[4])
        n.positive(run, "tag run"); n.positive(attempt, "tag attempt")
    except (ValueError, OverflowError):
        return None
    return run, attempt


def optional(api, path):
    try:
        return api.request("GET", path)
    except APIError as error:
        if error.status == 404:
            return None
        raise


def inventory(api):
    """Fetch all bounded pages, not the older Launcher's single 30-row window.

    Retain only required fields rather than all asset inventories. A full last
    page is incomplete, not evidence that a lower release can safely be removed.
    """
    rows, ids, tags = [], set(), set()
    for page in range(1, MAX_PAGES + 1):
        batch = api.request("GET", f"/releases?per_page={PAGE_SIZE}&page={page}")
        n.require(isinstance(batch, list) and len(batch) <= PAGE_SIZE, "invalid release page")
        for release in batch:
            n.require(isinstance(release, dict), "malformed inventory entry")
            rid, tag = release.get("id"), release.get("tag_name")
            n.positive(rid, "inventory release ID")
            n.require(isinstance(tag, str) and 0 < len(tag) <= 1024, "malformed inventory tag")
            n.require(rid not in ids and tag not in tags, "duplicate release ID/tag; inconsistent inventory")
            ids.add(rid); tags.add(tag)
            rows.append({key: copy.deepcopy(release.get(key)) for key in (
                "id", "tag_name", "target_commitish", "name", "body", "draft", "prerelease",
                "immutable", "author", "published_at")})
        if len(batch) < PAGE_SIZE:
            return rows
    raise n.Unavailable("complete release inventory exceeds pagination bound; nothing may be deleted")


def snapshot(release):
    n.require(isinstance(release, dict), "malformed release")
    return {key: copy.deepcopy(release.get(key)) for key in (
        "id", "tag_name", "target_commitish", "name", "body", "draft", "prerelease",
        "immutable", "author", "published_at")}


def shape(release, app, bot_id):
    """Source consistency is necessary, but is never an ownership credential."""
    n.require(isinstance(release, dict), "malformed release")
    n.positive(release.get("id"), "release ID")
    n.require(parse_tag(release.get("tag_name")) is not None, "not an exact nightly tag")
    n.require(release.get("draft") is False and release.get("prerelease") is True,
              "not a published prerelease")
    n.require(release.get("immutable") is True, "release immutability is absent")
    author = release.get("author")
    n.require(isinstance(author, dict) and type(author.get("id")) is int
              and author["id"] == bot_id and author.get("type") == "Bot", "not the authenticated numeric bot author")
    tag, source = release["tag_name"], release.get("target_commitish")
    n.require(isinstance(source, str) and n.HEX40.fullmatch(source) and tag.endswith(".g" + source[:12]), "tag/source mismatch")
    n.require(release.get("name") == app + " Nightly " + tag, "producer release name mismatch")
    policy.timestamp(release.get("published_at"))


def prove(api, registry, app, release, bot_id, provenance, absent_release=False):
    shape(release, app, bot_id)
    repository = n.app_record(registry, app)["repository"]
    # A persisted snapshot never grants authority. The adapter must independently
    # reauthenticate the original publication even in release-gone recovery.
    n.require(provenance.verify(api, repository, copy.deepcopy(release), bot_id) is True,
              "publication/ref provenance not proven")
    if not absent_release:
        prior = policy.verify_prior(api, registry, release)
        n.require(prior["identity"]["app"] == app and prior["identity"]["source_commit"] == release["target_commitish"], "publication identity mismatch")
    ref = optional(api, "/git/ref/tags/" + quote(release["tag_name"], safe=""))
    if ref is None and absent_release:
        return
    n.require(isinstance(ref, dict) and ref.get("ref") == "refs/tags/" + release["tag_name"]
              and isinstance(ref.get("object"), dict) and ref["object"].get("type") == "commit"
              and ref["object"].get("sha") == release["target_commitish"], "exact lightweight tag ref changed/missing")


def plan(api, registry, app, current_release_id, bot_id, provenance=None):
    """Read-only complete plan. Unknown/malformed/foreign releases are retained."""
    n.positive(current_release_id, "current publication ID"); n.positive(bot_id, "approved bot ID")
    repository = n.app_record(registry, app)["repository"]
    provenance = provenance or PublicationProvenance()
    rows = inventory(api)
    current = [row for row in rows if row["id"] == current_release_id]
    n.require(len(current) == 1, "successful current publication absent from complete inventory")
    shape(current[0], app, bot_id)
    # Even default read-only plans bind the supplied current ID to a real
    # published producer run. Its source/text still cannot authorize deletion.
    policy.verify_prior(api, registry, current[0])
    proven, retained, orders = [], [], {}
    for row in rows:
        order = parse_tag(row["tag_name"])
        if order is not None:
            orders[order] = orders.get(order, 0) + 1
    n.require(not any(count > 1 for count in orders.values()), "ambiguous run/attempt tie; entire prune deferred")
    for row in rows:
        if row["id"] == current_release_id:
            continue
        try:
            n.require(parse_tag(row["tag_name"]) is not None, "unrelated tag")
            prove(api, registry, app, row, bot_id, provenance)
            proven.append(row)
        except (AmbiguousOwnership, n.Unavailable) as error:
            if not isinstance(error, UnknownProvenance):
                raise
            retained.append({"release_id": row["id"], "tag": row["tag_name"], "reason": str(error)})
        except n.InvalidNightly as error:
            retained.append({"release_id": row["id"], "tag": row["tag_name"], "reason": str(error)})
    proven.sort(key=lambda row: parse_tag(row["tag_name"]), reverse=True)
    # Current publication is guarded by the publishing transaction, whose job
    # may still be running. Reserve its slot without demanding completed receipt
    # witnesses, then retain the six newest historical proven publications.
    historical = [row for row in proven if row["id"] != current_release_id]
    kept = [current[0]] + historical[:KEEP - 1]
    candidates = list(reversed(historical[KEEP - 1:]))[:MAX_REMOVALS]
    return {"schema": SCHEMA, "app": app, "repository": repository, "bot_id": bot_id,
            "current": snapshot(current[0]), "keep": [row["id"] for row in kept],
            "candidates": [snapshot(row) for row in candidates], "retained": retained}


def validate_plan(previous, registry):
    n.fields(previous, {"schema", "app", "repository", "bot_id", "current", "keep", "candidates", "retained"}, "prune plan")
    n.require(previous["schema"] == SCHEMA and previous["repository"] == n.app_record(registry, previous["app"])["repository"], "plan repository/schema mismatch")
    n.positive(previous["bot_id"], "approved bot ID")
    shape(previous["current"], previous["app"], previous["bot_id"])
    n.require(isinstance(previous["keep"], list) and all(type(rid) is int and rid > 0 for rid in previous["keep"]), "invalid plan keep set")
    n.require(isinstance(previous["candidates"], list) and len(previous["candidates"]) <= MAX_REMOVALS, "invalid candidate list")
    ids, tags, orders = set(), set(), set()
    for candidate in previous["candidates"]:
        shape(candidate, previous["app"], previous["bot_id"])
        rid, tag = candidate["id"], candidate["tag_name"]
        order = parse_tag(tag)
        n.require(rid not in ids and tag not in tags and order not in orders, "duplicate/ambiguous candidate")
        n.require(rid != previous["current"]["id"] and tag != previous["current"]["tag_name"] and rid not in previous["keep"], "attempt to delete kept/current publication")
        ids.add(rid); tags.add(tag); orders.add(order)


def preflight(api, registry, previous, provenance):
    """Validate ALL removals and fresh keep-set membership before ANY mutation."""
    validate_plan(previous, registry)
    fresh = plan(api, registry, previous["app"], previous["current"]["id"], previous["bot_id"], provenance)
    n.require(fresh["current"] == previous["current"], "current publication changed")
    eligible = {row["id"]: row for row in fresh["candidates"]}
    # A retry may have already removed some candidates. Remaining releases must
    # still be in the recomputed beyond-seven set; absent ones need independent
    # durable provenance, never a broad orphan-tag search.
    for candidate in previous["candidates"]:
        actual = optional(api, f"/releases/{candidate['id']}")
        by_tag = optional(api, "/releases/tags/" + quote(candidate["tag_name"], safe=""))
        if actual is not None:
            n.require(snapshot(actual) == candidate and eligible.get(candidate["id"]) == candidate, "candidate drifted or no longer beyond keep-seven")
            n.require(isinstance(by_tag, dict) and snapshot(by_tag) == candidate, "release/tag association changed")
        else:
            n.require(by_tag is None, "release replaced under planned tag")
        prove(api, registry, previous["app"], candidate, previous["bot_id"], provenance, absent_release=actual is None)


def execute(api, registry, previous, *, approved=False, provenance=None, checkpoint=None):
    """Apply ONLY this fully validated plan, release first, stop at first failure.

    checkpoint(plan) must durably save the exact plan before the first mutation;
    rerun that same plan to recover partial release/tag deletion. A fresh plan
    intentionally does not adopt orphan tags. Neither plan nor receipt is trusted
    provenance. The repository must serialize this with publication/settings.
    """
    n.require(approved is True, "pruning is off until explicit first-run approval")
    n.require(callable(checkpoint), "durable exact-plan checkpoint required")
    provenance = provenance or PublicationProvenance()
    preflight(api, registry, previous, provenance)
    checkpoint(copy.deepcopy(previous))
    completed = []
    for candidate in previous["candidates"]:
        # Revalidate every remaining candidate and the entire keep set again.
        # Failures stop; there is no rollback of a release already removed.
        remaining = {**previous, "candidates": previous["candidates"][len(completed):]}
        preflight(api, registry, remaining, provenance)
        rid, tag = candidate["id"], quote(candidate["tag_name"], safe="")
        release_path = f"/releases/{rid}"
        actual = optional(api, release_path)
        if actual is not None:
            n.require(snapshot(actual) == candidate, "candidate changed immediately before release deletion")
            prove(api, registry, previous["app"], actual, previous["bot_id"], provenance)
            api.request("DELETE", release_path)
        # A timeout/500 from DELETE stops here; a later retry confirms absence.
        n.require(optional(api, release_path) is None, "release deletion not confirmed")
        n.require(optional(api, "/releases/tags/" + tag) is None, "release appeared under tag; preserve ref")
        prove(api, registry, previous["app"], candidate, previous["bot_id"], provenance, absent_release=True)
        if optional(api, "/git/ref/tags/" + tag) is not None:
            # Repeat the exact SHA check at the last read before deleting.
            prove(api, registry, previous["app"], candidate, previous["bot_id"], provenance, absent_release=True)
            api.request("DELETE", "/git/refs/tags/" + tag)
        n.require(optional(api, "/git/ref/tags/" + tag) is None, "tag deletion not confirmed")
        completed.append(rid)
    return {"completed_release_ids": completed}


def save_checkpoint(path, previous):
    """Atomic local receipt; caller must retain it outside temporary build roots."""
    from pathlib import Path
    destination = Path(path)
    temporary = destination.with_name(destination.name + ".new")
    with open(temporary, "x", encoding="utf-8") as stream:
        json.dump(previous, stream, sort_keys=True)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, destination)
    descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_plan(path):
    raw = n.bounded_bytes(path, 4 * 1024 * 1024, "prune plan")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            n.require(key not in value, "duplicate prune plan key")
            value[key] = item
        return value
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(n.InvalidNightly("nonfinite plan JSON")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--registry", required=True); parser.add_argument("--app", required=True)
    parser.add_argument("--current-release-id", required=True, type=int)
    parser.add_argument("--bot-id", required=True, type=int)
    parser.add_argument("--plan"); parser.add_argument("--checkpoint")
    parser.add_argument("--config", help="reviewed Actions ownership policy; no raw receipt input")
    parser.add_argument("--approve-first-run", action="store_true")
    parser.add_argument("--checkpoint-artifact-id"); parser.add_argument("--checkpoint-artifact-sha256")
    args = parser.parse_args()
    try:
        registry = n.bounded_json(args.registry)
        repository = n.app_record(registry, args.app)["repository"]
        provenance = None
        if args.config:
            from nightly_ownership import ActionsOwnership
            provenance = ActionsOwnership(n.bounded_json(args.config))
            n.require(provenance.config["repository"] == repository and provenance.config["bot_id"] == args.bot_id, "ownership config/app/bot mismatch")
        if args.mode == "apply":
            n.require(args.approve_first_run is True and args.config and args.plan and args.checkpoint and args.checkpoint_artifact_id and args.checkpoint_artifact_sha256, "live apply is off: explicit approval, reviewed ownership config, plan and durable checkpoint required")
            previous = read_plan(args.plan)
            validate_plan(previous, registry)
            n.require(previous["app"] == args.app and previous["current"]["id"] == args.current_release_id and previous["bot_id"] == args.bot_id, "plan differs from CLI publication context")
        api = PruneTransport(repository, os.environ.get("GH_TOKEN", ""))
        if args.mode == "plan":
            result = plan(api, registry, args.app, args.current_release_id, args.bot_id, provenance)
        else:
            from nightly_prune_checkpoint import verify as verify_durable
            verify_durable(api,provenance.config,previous,args.checkpoint_artifact_id,args.checkpoint_artifact_sha256,os.environ)
            result = execute(api, registry, previous, approved=True, provenance=provenance, checkpoint=lambda value: save_checkpoint(args.checkpoint, value))
        print(json.dumps(result, sort_keys=True))
    except (n.InvalidNightly, APIError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        parser.exit(1, f"nightly pruning stopped: {error}\n")


if __name__ == "__main__":
    main()
