#!/usr/bin/env python3
"""Keyless check of the merged macOS verify path against a REAL PhotoCraft nightly app.

Runs on a hosted Apple-silicon runner with no secrets, environment or Apple account.
It signs the real app with a throwaway self-signed identity made in the job, packages
it the way nightly-sign.sh does, and runs scripts/nightly_signing.mac_verify. Expected:
inventory, smoke and packaging pass; verification refuses for the wrong team, for a
missing staple and for a missing notarization, each at its own step. Nothing here is
Developer ID, notarization or Gatekeeper acceptance evidence for a real nightly.
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"scripts"))
import nightly_feed as n            # noqa: E402
import nightly_signing as s         # noqa: E402
import nightly_unix as u            # noqa: E402

TEAM = "TESTTEAM01"   # throwaway identity's OU; never a real Apple team
WRONG = "WRONGTEAM9"
APP = "PhotoCraft Nightly.app"
REAL_COMMAND, REAL_RUN = s.command, subprocess.run


def sh(argv, timeout=600, **kwargs):
    """Driver-owned setup tools (openssl/security); output bounded and recorded, never hangs."""
    try:
        result = REAL_RUN([str(value) for value in argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return 124, "timed out after "+str(timeout)+" s"
    return result.returncode, result.stdout.decode("utf-8", "replace")[-4000:]


def must(argv, **kwargs):
    code, output = sh(argv, **kwargs)
    if code:
        raise RuntimeError("setup command failed: "+" ".join(map(str, argv[:3]))+": "+output)
    return output


class Trace:
    """Records every verification tool call and its exit status, calling the real tools."""
    def __init__(self, details_patch=None, stub_stapler=False):
        self.calls, self.details_patch, self.stub_stapler, self.substituted = [], details_patch, stub_stapler, []
        self.inside = False  # command() itself calls subprocess.run; record it once.

    def command(self, argv, **kwargs):
        names = [str(value) for value in argv]
        if self.stub_stapler and names[:3] == ["/usr/bin/xcrun", "stapler", "validate"]:
            self.calls.append({"argv": names[:3], "status": "stubbed-success"})
            self.substituted.append("stapler validate stubbed to succeed (precondition for the Gatekeeper case)")
            return ""
        self.inside = True
        try:
            result = REAL_COMMAND(argv, **kwargs)
        except subprocess.CalledProcessError as error:
            self.calls.append({"argv": names[:3], "status": error.returncode})
            raise
        finally:
            self.inside = False
        self.calls.append({"argv": names[:3], "status": 0})
        return result

    def run(self, argv, *args, **kwargs):
        if self.inside:
            return REAL_RUN(argv, *args, **kwargs)
        names = [str(value) for value in argv]
        try:
            result = REAL_RUN(argv, *args, **kwargs)
        except subprocess.CalledProcessError as error:
            self.calls.append({"argv": names[:3], "status": error.returncode,
                               "output": (error.output or b"").decode("utf-8", "replace")[-2000:]})
            raise
        self.calls.append({"argv": names[:3], "status": result.returncode})
        if names[:2] == ["/usr/bin/codesign", "-d"] and self.details_patch:
            text, changed = self.details_patch(result.stdout.decode())
            self.substituted.extend(changed)
            result = subprocess.CompletedProcess(result.args, result.returncode, text.encode(), result.stderr)
        return result


def details_for_team(team, *, set_team=True):
    """Make ONLY the fields a throwaway identity cannot carry look Apple-issued."""
    def patch(text):
        changed = []
        if set_team and "\nTeamIdentifier="+team+"\n" not in "\n"+text+"\n":
            old = re.search(r"^TeamIdentifier=.*$", text, re.M)
            text = re.sub(r"^TeamIdentifier=.*$", "TeamIdentifier="+team, text, flags=re.M) if old else text+"TeamIdentifier="+team+"\n"
            changed.append("codesign -d TeamIdentifier "+(old.group(0) if old else "(absent)")+" -> "+team)
        if not re.search(r"^Timestamp=.+$", text, re.M):
            text += "Timestamp=synthetic precondition\n"; changed.append("codesign -d Timestamp (absent) -> synthetic")
        return text, changed
    return patch


def refusal(error):
    if isinstance(error, n.InvalidNightly):
        return {"type": "InvalidNightly", "message": str(error)}
    if isinstance(error, subprocess.CalledProcessError):
        cmd = [str(value) for value in (error.cmd if isinstance(error.cmd, (list, tuple)) else [error.cmd])]
        output = error.output if error.output is not None else getattr(error, "stdout", b"")
        if isinstance(output, bytes): output = output.decode("utf-8", "replace")
        stderr = getattr(error, "stderr", b"") or b""
        if isinstance(stderr, bytes): stderr = stderr.decode("utf-8", "replace")
        return {"type": "CalledProcessError", "tool": cmd[:3], "status": error.returncode, "output": (output+stderr)[-2000:]}
    return {"type": type(error).__name__, "message": str(error)[:2000]}


def verify(final, version, pending, team, out, trace):
    env = {**os.environ, "NIGHTLY_APPLE_TEAM_ID": team}
    with mock.patch.object(s, "command", trace.command), mock.patch.object(s.subprocess, "run", trace.run):
        try:
            return {"result": "accepted", "team": s.mac_verify(final, version, pending, env, out)}
        except (n.InvalidNightly, subprocess.CalledProcessError, OSError, ValueError) as error:
            return {"result": "refused", "refusal": refusal(error)}


def openssl():
    """Prefer Homebrew OpenSSL 3 (with -legacy PKCS#12 that `security` can import); else system LibreSSL."""
    for candidate in ("/opt/homebrew/opt/openssl@3/bin/openssl", "/usr/local/opt/openssl@3/bin/openssl"):
        if Path(candidate).is_file():
            return candidate, True
    return "/usr/bin/openssl", False


def identity(work):
    """Throwaway self-signed code-signing identity in a private keychain (no secrets)."""
    key, cert, p12, keychain = work/"key.pem", work/"cert.pem", work/"id.p12", work/"throwaway.keychain-db"
    tool, legacy = openssl()
    name = "Developer ID Application: Throwaway Nightly Test ("+TEAM+")"
    subject = "/CN="+name+"/OU="+TEAM+"/O=Throwaway Nightly Test"
    diag = {"openssl": tool+" "+must([tool, "version"]).strip()}
    must([tool, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "2",
          "-subj", subject, "-addext", "basicConstraints=critical,CA:false",
          "-addext", "keyUsage=critical,digitalSignature", "-addext", "extendedKeyUsage=critical,codeSigning"])
    must([tool, "pkcs12", "-export", *(["-legacy"] if legacy else []), "-inkey", key, "-in", cert, "-name", name,
          "-out", p12, "-passout", "pass:throwaway"])
    must(["security", "create-keychain", "-p", "throwaway", keychain])
    must(["security", "set-keychain-settings", "-lut", "3600", keychain])
    must(["security", "unlock-keychain", "-p", "throwaway", keychain])
    diag["import"] = must(["security", "import", p12, "-k", keychain, "-P", "throwaway", "-T", "/usr/bin/codesign",
                           "-T", "/usr/bin/security", "-f", "pkcs12"])
    must(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", "throwaway", keychain])
    listed = must(["security", "list-keychains", "-d", "user"])
    must(["security", "list-keychains", "-d", "user", "-s", keychain, *[line.strip().strip('"') for line in listed.splitlines() if line.strip()]])
    must(["security", "default-keychain", "-d", "user", "-s", keychain])
    # Ephemeral hosted VM only: trust the throwaway root for code signing so codesign accepts it.
    # No trust changes: codesign can sign with an untrusted identity; find-identity (without -v)
    # lists it with CSSMERR_TP_NOT_TRUSTED, and that listed SHA-1 is what we sign with.
    diag["identities_all"] = sh(["security", "find-identity", "-p", "codesigning", keychain], timeout=60)[1]
    diag["identities_valid"] = sh(["security", "find-identity", "-v", "-p", "codesigning", keychain], timeout=60)[1]
    fingerprint = must([tool, "x509", "-noout", "-fingerprint", "-sha1", "-in", cert]).split("=", 1)[1].replace(":", "").strip().upper()
    found = re.findall(r"\b([0-9A-F]{40})\b", diag["identities_all"])
    return {"sha1": fingerprint, "name": name, "keychain": keychain, "listed": found, "diagnostics": diag}


def sign_and_package(unsigned, version, work, ident):
    """nightly-sign.sh's local steps (no notarization/staple): unzip, codesign runtime, re-zip."""
    stage = work/"stage"; stage.mkdir()
    must(["/usr/bin/ditto", "-x", "-k", "--norsrc", "--noextattr", unsigned, stage])
    app = stage/APP
    # Selector order: the SHA-1 that `security` lists for the identity, our certificate hash, then the name.
    selectors = [*ident["listed"], ident["sha1"], ident["name"]]
    attempts, chosen = [], None
    for selector in dict.fromkeys(selectors):
        for timestamp in ("--timestamp", "--timestamp=none"):
            code, output = sh(["/usr/bin/codesign", "--force", "--sign", selector, "--keychain", ident["keychain"],
                               timestamp, "--options", "runtime", app/"Contents/MacOS/PhotoCraft"], timeout=180)
            attempts.append({"selector": selector, "timestamp": timestamp, "exit": code, "output": output[-300:]})
            if code == 0:
                chosen = (selector, timestamp); break
        if chosen: break
    if not chosen:
        raise RuntimeError("throwaway codesign could not use the identity: "+json.dumps(attempts)[-3000:])
    selector, timestamp = chosen
    must(["/usr/bin/codesign", "--force", "--sign", selector, "--keychain", ident["keychain"], timestamp, "--options", "runtime", app])
    integrity = sh(["/usr/bin/codesign", "--verify", "--strict", "--deep", "--verbose=2", app])
    final_dir = work/"final"; final_dir.mkdir()
    final = final_dir/("photocraft-"+version+"-macos-aarch64-nightly.zip")
    must(["/usr/bin/ditto", "-c", "-k", "--norsrc", "--noextattr", "--noacl", "--keepParent", app, final])
    details = sh(["/usr/bin/codesign", "-d", "--verbose=4", app])[1]
    return final, {"timestamp_flag": timestamp, "selector": selector, "attempts": attempts, "integrity_exit": integrity[0], "integrity_output": integrity[1][-800:],
                   "codesign_details": "\n".join(line for line in details.splitlines()
                       if line.split("=", 1)[0] in ("Identifier", "Authority", "TeamIdentifier", "Timestamp", "CodeDirectory v"))}


def tampered(final, version, work):
    """Same signed package with one resource byte changed: the seal must break."""
    root = work/"tamper"; root.mkdir()
    must(["/usr/bin/ditto", "-x", "-k", "--norsrc", "--noextattr", final, root])
    victims = sorted(path for path in (root/APP/"Contents/Resources").rglob("*") if path.is_file())
    if not victims:
        victims = [root/APP/"Contents/Info.plist"]
    with victims[0].open("ab") as stream: stream.write(b"\n")
    out_dir = work/"tampered"; out_dir.mkdir()
    out = out_dir/final.name
    must(["/usr/bin/ditto", "-c", "-k", "--norsrc", "--noextattr", "--noacl", "--keepParent", root/APP, out])
    return out, str(victims[0].relative_to(root))


def version_diagnosis(executable, work, version):
    """Report-only: how the real binary answers each --version form within 60 s (bounded output)."""
    forms = {"plain": [str(executable), "--version"],
             "producer-smoke": [str(executable), "--launch-profile", "nightly-v1", "--profile-root", str(work/"diag-profile"), "--version"]}
    result = {}
    for name, argv in forms.items():
        import time
        started = time.monotonic()
        try:
            done = REAL_RUN(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=60,
                            env={key: value for key, value in os.environ.items() if not key.startswith("PHOTOCRAFT_")})
            result[name] = {"exit": done.returncode, "seconds": round(time.monotonic()-started, 1),
                            "stdout": done.stdout.decode("utf-8", "replace")[:600], "stderr": done.stderr.decode("utf-8", "replace")[:1200],
                            "exact": done.stdout.decode("utf-8", "replace").replace("\r\n", "\n") == "PhotoCraft "+version+" (nightly-v1)\n"}
        except subprocess.TimeoutExpired as error:
            result[name] = {"timed_out_after": 60, "stdout": (error.stdout or b"").decode("utf-8", "replace")[:600],
                            "stderr": (error.stderr or b"").decode("utf-8", "replace")[:1200]}
    return result


def check(cases, name, ok, **data):
    cases.append({"case": name, "ok": bool(ok), **data})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True); parser.add_argument("--version", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    u.canonical_version(args.version)
    temp = Path(os.environ["RUNNER_TEMP"]).resolve()
    work = Path(tempfile.mkdtemp(prefix="mac-keyless-", dir=temp))
    archive = Path(args.archive).resolve()
    pending = n.bounded_json(ROOT/"nightly-unix-pending.json")
    cases = []

    # 1. Positive controls on the real unsigned bundle.
    try:
        inventory = u.verify_archive(pending, "macos-aarch64", archive, args.version)
        check(cases, "inventory accepts the real unsigned bundle", True, inventory=inventory)
    except Exception as error:  # noqa: BLE001 - recorded as a failed expectation
        check(cases, "inventory accepts the real unsigned bundle", False, refusal=refusal(error))
    import time
    import nightly_smoke as smoke
    unpacked = work/"smoke-app"
    try:
        u.verify_archive(pending, "macos-aarch64", archive, args.version, unpacked)
        # The producer smoke allows 15 s. A first launch of a fresh 280 MB binary may spend longer in
        # macOS's first-run scan, so this measures the real duration under a 180 s bound and reports it.
        started = time.monotonic()
        with mock.patch.object(smoke, "TIMEOUT", 180):
            result = smoke.smoke(unpacked/APP/"Contents/MacOS/PhotoCraft", work/"smoke-profile", args.version)
        first = round(time.monotonic()-started, 1)
        started = time.monotonic()
        smoke.smoke(unpacked/APP/"Contents/MacOS/PhotoCraft", work/"smoke-profile-2", args.version)
        second = round(time.monotonic()-started, 1)
        check(cases, "real unsigned app reports the exact nightly version", True, smoke=result,
              first_launch_seconds=first, second_launch_seconds=second, producer_bound_seconds=smoke.TIMEOUT)
    except Exception as error:  # noqa: BLE001
        check(cases, "real unsigned app reports the exact nightly version", False, refusal=refusal(error),
              diagnosis=version_diagnosis(unpacked/APP/"Contents/MacOS/PhotoCraft", work, args.version))

    # 2. Throwaway signing and Launcher-style packaging must keep the seal.
    ident = {}
    try:
        ident = identity(work)
        final, signed = sign_and_package(archive, args.version, work, ident)
    except Exception as error:  # noqa: BLE001 - setup failure still produces a report
        check(cases, "throwaway signing setup", False, error=str(error)[-4000:], identity=ident)
        return finish(args, archive, cases)
    check(cases, "throwaway-signed app passes codesign --verify --strict --deep", signed["integrity_exit"] == 0,
          signing=signed, identity_diagnostics=ident["diagnostics"])
    try:
        check(cases, "inventory accepts the signed, re-zipped package", True,
              inventory=u.verify_archive(pending, "macos-aarch64", final, args.version))
    except Exception as error:  # noqa: BLE001
        check(cases, "inventory accepts the signed, re-zipped package", False, refusal=refusal(error))

    # 3. Refusals, each at its own step.
    # Only a missing timestamp (Apple's TSA may refuse a throwaway identity) is filled in;
    # the team stays the identity's real one, so the team is the only failing field.
    trace = Trace(details_patch=details_for_team(WRONG, set_team=False))
    outcome = verify(final, args.version, pending, WRONG, work/"verify-wrong-team", trace)
    reached = [call["argv"][:2] for call in trace.calls]
    details = signed["codesign_details"]
    check(cases, "wrong team is refused at the signature-detail step",
          outcome["result"] == "refused" and outcome["refusal"].get("type") == "InvalidNightly" and
          "exact team" in outcome["refusal"].get("message", "") and
          ["/usr/bin/codesign", "--verify"] in reached and ["/usr/bin/xcrun", "stapler"] not in reached and
          "TeamIdentifier="+WRONG not in details and "Identifier=ai.storyteller.photocraft.nightly" in details and
          "Authority=Developer ID Application:" in details and "runtime" in details,
          outcome=outcome, tools=trace.calls, codesign_details=details, preconditions=trace.substituted)

    trace = Trace(details_patch=details_for_team(TEAM))
    outcome = verify(final, args.version, pending, TEAM, work/"verify-not-stapled", trace)
    check(cases, "missing staple is refused by the real stapler validate",
          outcome["result"] == "refused" and outcome["refusal"].get("type") == "CalledProcessError" and
          outcome["refusal"].get("tool", [])[:3] == ["/usr/bin/xcrun", "stapler", "validate"],
          outcome=outcome, tools=trace.calls, preconditions=trace.substituted)

    trace = Trace(details_patch=details_for_team(TEAM), stub_stapler=True)
    outcome = verify(final, args.version, pending, TEAM, work/"verify-not-notarized", trace)
    refused = outcome["refusal"] if outcome["result"] == "refused" else {}
    gatekeeper = (refused.get("type") == "CalledProcessError" and refused.get("tool", [])[:1] == ["/usr/sbin/spctl"]) or \
                 (refused.get("type") == "InvalidNightly" and "Gatekeeper" in refused.get("message", ""))
    check(cases, "missing notarization is refused by the real Gatekeeper assessment",
          gatekeeper and "source=Notarized Developer ID" not in refused.get("output", ""),
          outcome=outcome, tools=trace.calls, preconditions=trace.substituted)

    broken, victim = tampered(final, args.version, work)
    trace = Trace()
    outcome = verify(broken, args.version, pending, TEAM, work/"verify-tampered", trace)
    check(cases, "a changed resource byte is refused by codesign --verify (seal)",
          outcome["result"] == "refused" and outcome["refusal"].get("tool", [])[:2] == ["/usr/bin/codesign", "--verify"],
          outcome=outcome, tools=trace.calls, changed=victim)

    return finish(args, archive, cases)


def finish(args, archive, cases):
    report = {"schema": "craft-nightly-macos-keyless-test/v1", "version": args.version, "archive": archive.name,
              "archive_sha256": s.sha(archive), "tooling": os.environ.get("GITHUB_SHA"),
              "all_expectations_met": bool(cases) and all(case["ok"] for case in cases), "cases": cases,
              "caveat": "Throwaway self-signed identity; no Apple credentials, notarization or Developer ID. "
                        "Precondition substitutions are listed per case."}
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True, default=str)+"\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## Keyless macOS verification against the real nightly app\n\n")
            for case in cases:
                stream.write(("- ✅ " if case["ok"] else "- ❌ ")+case["case"]+"\n")
    print(json.dumps(report, indent=2, sort_keys=True, default=str)[-20000:])
    return 0 if report["all_expectations_met"] else 1


if __name__ == "__main__":
    sys.exit(main())
