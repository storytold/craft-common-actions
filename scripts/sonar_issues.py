#!/usr/bin/env python3
"""Download a project's unresolved SonarCloud issues, reduced to the fields an AI needs.

Usage:
    scripts/sonar_issues.py storytold_craft-common-actions
    scripts/sonar_issues.py storytold_craft-common-actions --out sonar-filtered.json

Authentication is optional for public projects. Set SONAR_TOKEN (or pass --token) for a
private project. The API is paginated; every page is fetched and the result keeps the
server's total in ``total``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://sonarcloud.io"
PAGE_SIZE = 500
SONAR_PAGING_CAP = 10_000


def _port(parsed: urllib.parse.ParseResult) -> int:
    """The effective port of an HTTPS URL."""
    return parsed.port or 443


def _validate_base_url(base_url: str) -> str:
    """Require a credential-safe base URL; the API token must never travel in cleartext."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("SonarCloud base URL must be an https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("SonarCloud base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("SonarCloud base URL must not contain a query or fragment")
    _port(parsed)  # Invalid ports are a ValueError, not a later request failure.
    return base_url.rstrip("/")


class _SameOriginHTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to the same HTTPS origin, so a token cannot be forwarded elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlparse(req.full_url)
        new = urllib.parse.urlparse(newurl)
        if (
            new.scheme.lower() != "https"
            or not new.hostname
            or new.hostname != old.hostname
            or _port(new) != _port(old)
            or new.username
            or new.password
        ):
            raise urllib.error.HTTPError(newurl, code, "refusing cross-origin redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_OPENER = urllib.request.build_opener(_SameOriginHTTPSRedirectHandler())


def fetch_issues(
    project_key: str,
    base_url: str = DEFAULT_URL,
    token: str | None = None,
    opener=None,
) -> tuple[int, list[dict]]:
    """Return ``(total, issues)`` for every unresolved issue of ``project_key``."""
    base_url = _validate_base_url(base_url)
    open_url = opener or _SAFE_OPENER.open
    issues: list[dict] = []
    page = 1
    total = 0
    while True:
        query = urllib.parse.urlencode(
            {
                "componentKeys": project_key,
                "resolved": "false",
                "p": page,
                "ps": PAGE_SIZE,
            }
        )
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"{base_url.rstrip('/')}/api/issues/search?{query}", headers=headers)
        with open_url(request, timeout=60) as response:
            payload = json.load(response)
        page_issues = payload.get("issues") or []
        issues.extend(page_issues)
        total = int((payload.get("paging") or {}).get("total") or len(issues))
        if total >= SONAR_PAGING_CAP:
            raise ValueError(
                f"SonarCloud reports {total} unresolved issues (cap {SONAR_PAGING_CAP}); "
                "cannot verify a complete export"
            )
        if not page_issues or len(issues) >= total:
            return total, issues
        page += 1


def reduce_issue(issue: dict) -> dict:
    """Keep the file, position, rule and message an AI needs to fix one finding."""
    component = issue.get("component") or ""
    file = component.split(":", 1)[1] if ":" in component else component
    text_range = issue.get("textRange") or {}
    return {
        "file": file,
        "line": text_range.get("startLine"),
        "startOffset": text_range.get("startOffset"),
        "endOffset": text_range.get("endOffset"),
        "rule": issue.get("rule"),
        "severity": issue.get("severity"),
        "type": issue.get("type"),
        "message": issue.get("message"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project_key", help="SonarCloud project key (for example storytold_craft-common-actions)")
    parser.add_argument("-o", "--out", help="write the JSON here instead of stdout")
    parser.add_argument("--base-url", default=os.environ.get("SONAR_BASE_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.environ.get("SONAR_TOKEN"), help="SonarCloud token for a private project")
    args = parser.parse_args(argv)
    try:
        total, issues = fetch_issues(args.project_key, args.base_url, args.token)
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"sonar_issues: {error}", file=sys.stderr)
        return 1
    output = {"total": total, "issues": [reduce_issue(issue) for issue in issues]}
    text = json.dumps(output, indent=2) + "\n"
    try:
        if args.out:
            with open(args.out, "w", encoding="utf-8") as output_file:
                output_file.write(text)
        else:
            sys.stdout.write(text)
    except OSError as error:
        print(f"sonar_issues: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
