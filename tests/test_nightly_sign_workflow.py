"""Structure of the reusable signer workflow: graph, secrets, environments, checkouts, handoffs."""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/nightly-sign.yml"

WINDOWS_SECRETS = {"WINDOWS_CERTIFICATE", "WINDOWS_CERTIFICATE_PASSWORD", "AZURE_TENANT_ID", "AZURE_CLIENT_ID",
                   "AZURE_CLIENT_SECRET", "AZURE_SIGNING_ENDPOINT", "AZURE_SIGNING_ACCOUNT", "AZURE_CERT_PROFILE"}
APPLE_SECRETS = {"APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD", "KEYCHAIN_PASSWORD", "APPLE_ID",
                 "APPLE_PASSWORD", "APPLE_TEAM_ID"}
# secret -> {(job, step name)} that may read it; nothing else may.
SECRET_STEPS = {
    **{name: {("sign-windows", "Sign the Windows payload")} for name in WINDOWS_SECRETS},
    **{name: {("sign-macos", "Sign, notarize and staple the macOS payload")} for name in APPLE_SECRETS},
    "LINUX_SIGNING_KEY": {("sign-linux", "Sign the Linux packages, NIGHTLY.json and SHA256SUMS.txt")},
    "DOCKERHUB_USERNAME": {("publish", "Push the Flatpak images"), ("publish", "Move the nightly-<arch> image tags")},
    "DOCKERHUB_TOKEN": {("publish", "Push the Flatpak images"), ("publish", "Move the nightly-<arch> image tags")},
    "ARTCRAFT_SERVICES_DISPATCH_TOKEN": {("retain", "Keep the newest 3 nightlies and dispatch the website update")},
}
SIGN_JOBS = {"sign-windows", "sign-macos", "sign-linux"}
PLATFORM_JOBS = {"sign-windows": "windows", "verify-windows": "windows", "sign-macos": "macos",
                 "verify-macos": "macos", "smoke-macos": "macos"}
RELEASE_CHECKOUT_JOBS = {"sign-windows", "sign-macos", "smoke-macos"}
# The Docker Hub and website secrets live in `release` too; these jobs run only signer tools.
ENVIRONMENT_JOBS = SIGN_JOBS | {"publish", "retain"}


def flat(value):
    return " ".join(str(value).split())


class SignWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()
        cls.workflow = yaml.safe_load(cls.text)
        cls.jobs = cls.workflow["jobs"]

    def steps(self, job):
        return self.jobs[job]["steps"]

    def test_reusable_only_with_declared_inputs_and_secrets(self):
        on = self.workflow[True]
        self.assertEqual(set(on), {"workflow_call"})
        call = on["workflow_call"]
        self.assertEqual(set(call["inputs"]), {"app-name", "app-title", "producer-workflow", "producer_run_id",
                                               "producer_run_attempt", "manual-actors", "package-prefix",
                                               "flatpak-id"})
        self.assertIs(call["inputs"]["package-prefix"]["required"], True)
        self.assertIs(call["inputs"]["flatpak-id"]["required"], True)
        self.assertEqual(set(call["secrets"]), set(SECRET_STEPS))
        self.assertNotIn("inherit", self.text)
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["concurrency"]["group"], "nightly-sign-${{ github.repository }}")
        self.assertIs(self.workflow["concurrency"]["cancel-in-progress"], False)

    def test_job_graph(self):
        needs = {name: job.get("needs") for name, job in self.jobs.items()}
        self.assertEqual(needs, {
            "admit": None,
            "sign-windows": "admit", "verify-windows": ["admit", "sign-windows"],
            "sign-macos": "admit", "verify-macos": ["admit", "sign-macos"],
            "smoke-macos": ["admit", "sign-macos", "verify-macos"],
            "sign-linux": ["admit", "sign-windows", "verify-windows", "sign-macos", "verify-macos", "smoke-macos"],
            "verify-final": ["admit", "sign-linux"],
            "publish": ["admit", "sign-linux", "verify-final"],
            "retain": ["admit", "publish"],
        })

    def test_secrets_reach_only_their_one_step(self):
        seen = {}
        for name, job in self.jobs.items():
            # No job-level env, `with:` or service block may carry a secret.
            self.assertNotIn("secrets.", flat({k: v for k, v in job.items() if k != "steps"}), name)
            for step in job["steps"]:
                self.assertNotIn("secrets.", flat(step.get("with", {})), (name, step.get("name")))
                self.assertNotIn("secrets.", flat(step.get("run", "")), (name, step.get("name")))
                for key, value in step.get("env", {}).items():
                    for secret in re.findall(r"secrets\.([A-Z_]+)", str(value)):
                        self.assertEqual(key, secret)
                        self.assertIn((name, step["name"]), SECRET_STEPS[secret], secret)
                        seen.setdefault(secret, set()).add((name, step["name"]))
        self.assertEqual(seen, SECRET_STEPS)

    def test_key_steps_get_no_github_token(self):
        for job in SIGN_JOBS:
            for step in self.steps(job):
                env = flat(step.get("env", {}))
                if "secrets." in env:
                    self.assertNotIn("github.token", env, job)
                    self.assertNotIn("GH_TOKEN", env, job)

    def test_environment_only_on_secret_jobs(self):
        for name, job in self.jobs.items():
            if name in ENVIRONMENT_JOBS:
                self.assertEqual(job.get("environment"), "release", name)
            else:
                self.assertNotIn("environment", job, name)

    def test_write_permissions_only_where_publishing(self):
        for name, job in self.jobs.items():
            permissions = job["permissions"]
            self.assertNotIn("id-token", permissions, name)
            writes = {key for key, value in permissions.items() if value == "write"}
            self.assertEqual(writes, {"contents", "packages"} if name in ("publish", "retain") else set(), name)

    def test_platform_jobs_run_only_for_admitted_platforms(self):
        for name, platform in PLATFORM_JOBS.items():
            condition = flat(self.jobs[name]["if"])
            self.assertIn(f"contains(fromJSON(needs.admit.outputs.platforms), '{platform}')", condition, name)
            self.assertIn("github.ref == 'refs/heads/release'", condition, name)

    def test_linux_key_waits_for_every_admitted_platform(self):
        condition = flat(self.jobs["sign-linux"]["if"])
        self.assertTrue(condition.startswith("!cancelled() && needs.admit.result == 'success'"))
        self.assertNotIn("always()", condition)
        self.assertIn("(!contains(fromJSON(needs.admit.outputs.platforms), 'windows') || "
                      "(needs.sign-windows.result == 'success' && needs.verify-windows.result == 'success'))",
                      condition)
        self.assertIn("(!contains(fromJSON(needs.admit.outputs.platforms), 'macos') || "
                      "(needs.sign-macos.result == 'success' && needs.verify-macos.result == 'success' "
                      "&& needs.smoke-macos.result == 'success'))", condition)

    def test_final_chain_requires_success_and_tolerates_skipped_platforms(self):
        for job, required in (("verify-final", "sign-linux"), ("publish", "verify-final"), ("retain", "publish")):
            condition = flat(self.jobs[job]["if"])
            self.assertTrue(condition.startswith("!cancelled()"), job)
            self.assertIn(f"needs.{required}.result == 'success'", condition, job)
        self.assertIs(self.jobs["retain"]["continue-on-error"], True)
        move = next(s for s in self.steps("publish") if s["name"] == "Move the nightly-<arch> image tags")
        self.assertIs(move.get("continue-on-error"), True)

    def test_checkouts(self):
        for name, job in self.jobs.items():
            checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
            tools = [s["with"] for s in checkouts if s["with"].get("path") == ".nightly-tools"]
            self.assertEqual(tools, [{"repository": "${{ job.workflow_repository }}", "ref": "${{ job.workflow_sha }}",
                                      "path": ".nightly-tools", "persist-credentials": False}], name)
            source = [s["with"] for s in checkouts if s["with"].get("path") != ".nightly-tools"]
            if name in RELEASE_CHECKOUT_JOBS:
                # The protected app checkout: this run's own release commit, no other repository.
                self.assertEqual(source, [{"ref": "${{ github.sha }}", "path": ".release-source",
                                           "persist-credentials": False}], name)
            else:
                self.assertEqual(source, [], name)
                self.assertNotIn(".release-source", flat(job["steps"]), name)
            bind = next(s for s in job["steps"] if s.get("name", "").startswith("Bind the"))
            self.assertEqual(bind["env"], {"DEFINING_REPOSITORY": "${{ job.workflow_repository }}",
                                           "DEFINING_SHA": "${{ job.workflow_sha }}"}, name)
            self.assertIn("storytold/craft-common-actions", bind["run"], name)
            tools_step = next(s for s in checkouts if s["with"].get("path") == ".nightly-tools")
            self.assertLess(job["steps"].index(bind), job["steps"].index(tools_step), name)

    def test_handoffs_by_artifact_id_with_digest_checks(self):
        for name, job in self.jobs.items():
            for step in job["steps"]:
                if not str(step.get("uses", "")).startswith("actions/download-artifact@"):
                    continue
                spec = step["with"]
                self.assertIn("artifact-ids", spec, (name, step["name"]))
                self.assertNotIn("name", spec)
                self.assertNotIn("pattern", spec)
                self.assertEqual(spec["digest-mismatch"], "error", (name, step["name"]))
                if "run-id" in spec:
                    self.assertEqual(spec["run-id"], "${{ inputs.producer_run_id }}")
                    self.assertEqual(spec["repository"], "${{ github.repository }}")
                    self.assertIn(job["permissions"].get("actions"), ("read",), name)
            runs = flat([s.get("run", "") for s in job["steps"]])
            if name != "admit":
                self.assertIn("admit admission --admission .admission --sha256", runs, name)
        uploads = {name: [s["with"]["name"] for s in job["steps"]
                          if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
                   for name, job in self.jobs.items()}
        suffix = "-${{ github.run_id }}-${{ github.run_attempt }}"
        self.assertEqual({k: v for k, v in uploads.items() if v}, {
            "admit": ["nightly-admission" + suffix], "sign-windows": ["nightly-signed-windows" + suffix],
            "sign-macos": ["nightly-signed-macos" + suffix], "sign-linux": ["nightly-final" + suffix]})
        # Every consumer of a signed or final handoff re-checks its seal digest.
        for job, output in (("verify-windows", "sign-windows"), ("verify-macos", "sign-macos"),
                            ("smoke-macos", "sign-macos"), ("verify-final", "sign-linux"), ("publish", "sign-linux")):
            text = flat(self.jobs[job]["steps"])
            self.assertIn(f"needs.{output}.outputs.artifact-id", text, job)
            self.assertIn("admit unseal --directory", text, job)

    def test_tools_run_isolated_through_the_runner(self):
        modules = set()
        for job in self.jobs.values():
            for step in job["steps"]:
                run = step.get("run", "")
                for match in re.finditer(r"python3? (-I -B )?(\S*run\.py|\"\$tools\") (\w+)", run):
                    self.assertEqual(match[1], "-I -B ", run)
                    modules.add(match[3])
        self.assertEqual(modules, {"admit", "platforms", "linux", "publish", "retain"})

    def test_admit_runs_every_keyless_check_before_upload(self):
        names = [s.get("name") for s in self.steps("admit")]
        runs = [flat(s.get("run", "")) for s in self.steps("admit")]
        order = [next(i for i, r in enumerate(runs) if marker in r) for marker in (
            "admit request", "admit attest", "admit files", "linux precheck", "publish preflight")]
        self.assertEqual(order, sorted(order))
        self.assertGreater(names.index("Upload the admission"), order[-1])

    def test_linux_sign_passes_every_signed_directory_to_one_option(self):
        # `linux sign` reads --signed as one nargs list; a repeated option keeps only the last.
        step = next(s for s in self.steps("sign-linux") if s["name"].startswith("Sign the Linux"))
        self.assertEqual(step["run"].count("--signed"), 1)
        self.assertIn('--signed "${signed[@]}"', step["run"])

    def test_publish_order(self):
        runs = [flat(s.get("run", "")) for s in self.steps("publish")]
        order = [next(i for i, r in enumerate(runs) if marker in r)
                 for marker in ("publish oci", "publish release", "publish move-tags")]
        self.assertEqual(order, sorted(order))
        install = next(s for s in self.steps("publish") if "install-apt-packages" in str(s.get("uses", "")))
        self.assertIn("skopeo", install["with"]["packages"])

    def test_publish_takes_the_layouts_from_the_sealed_final_set(self):
        # sign-linux stages the .oci.tar layouts into the sealed final set that verify-final checks.
        for step in self.steps("publish"):
            self.assertNotIn("run-id", step.get("with", {}), step.get("name"))
            self.assertNotIn("cp ", step.get("run", ""), step.get("name"))
        self.assertNotIn("actions", self.jobs["publish"]["permissions"])

    def test_apt_packages_come_from_the_tools_checkout(self):
        # An action ref like @main would run later main code on the key runners.
        for name, job in self.jobs.items():
            uses = [str(s.get("uses", "")) for s in job["steps"]]
            for ref in filter(None, uses):
                self.assertTrue(ref.startswith(("actions/", "step-security/", "./.nightly-tools/")), (name, ref))
            installs = [i for i, ref in enumerate(uses) if "install-apt-packages" in ref]
            tools = next((i for i, s in enumerate(job["steps"]) if s.get("with", {}).get("path") == ".nightly-tools"),
                         None)
            for index in installs:
                self.assertEqual(uses[index], "./.nightly-tools/actions/install-apt-packages", name)
                self.assertLess(tools, index, name)

    def test_linux_checks_get_the_caller_pins(self):
        for job, mode in (("admit", "linux precheck"), ("sign-linux", "linux sign"), ("verify-final", "linux verify-final")):
            step = next(s for s in self.steps(job) if mode in flat(s.get("run", "")))
            self.assertEqual({k: v for k, v in step["env"].items() if k in ("PACKAGE_PREFIX", "FLATPAK_ID")},
                             {"PACKAGE_PREFIX": "${{ inputs.package-prefix }}", "FLATPAK_ID": "${{ inputs.flatpak-id }}"},
                             job)
            self.assertIn('--prefix "$PACKAGE_PREFIX" --flatpak-id "$FLATPAK_ID"', flat(step["run"]), job)


if __name__ == "__main__":
    unittest.main()
