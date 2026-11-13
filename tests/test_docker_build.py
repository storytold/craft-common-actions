"""Verify the shared docker-build workflow handles GHCR and Docker Hub."""

from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/docker-build.yml"
WORKFLOW = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


class DockerBuildWorkflowTests(unittest.TestCase):
    def test_workflow_interface(self):
        call = WORKFLOW["on"]["workflow_call"]
        self.assertIn("images", call["inputs"])
        self.assertIn("DOCKERHUB_USERNAME", call["secrets"])
        self.assertIn("DOCKERHUB_TOKEN", call["secrets"])
        self.assertNotIn("DIGITALOCEAN_TOKEN", call["secrets"])

    def test_write_permission_is_job_scoped(self):
        """Only the pushing jobs get `packages: write` (SonarCloud githubactions:S8233)."""
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
        for job in ("build", "merge"):
            self.assertEqual(
                WORKFLOW["jobs"][job]["permissions"],
                {"contents": "read", "packages": "write"},
            )

    def test_dockerhub_login_in_build_job(self):
        steps = WORKFLOW["jobs"]["build"]["steps"]
        dh_step = next(
            (s for s in steps if s.get("name") == "Log in to Docker Hub"),
            None,
        )
        self.assertIsNotNone(dh_step, "Build job missing 'Log in to Docker Hub' step")
        self.assertIn("docker.io/", dh_step["if"])
        self.assertEqual(dh_step["with"]["username"], "${{ secrets.DOCKERHUB_USERNAME }}")
        self.assertEqual(dh_step["with"]["password"], "${{ secrets.DOCKERHUB_TOKEN }}")
        self.assertFalse(any("DigitalOcean" in s.get("name", "") for s in steps))

        buildx_index = next(i for i, s in enumerate(steps) if s.get("name") == "Set up Buildx")
        dh_index = steps.index(dh_step)
        self.assertLess(buildx_index, dh_index, "Set up Buildx must run before Docker Hub login")

    def test_dockerhub_login_in_merge_job(self):
        steps = WORKFLOW["jobs"]["merge"]["steps"]
        dh_step = next(
            (s for s in steps if s.get("name") == "Log in to Docker Hub"),
            None,
        )
        self.assertIsNotNone(dh_step, "Merge job missing 'Log in to Docker Hub' step")
        self.assertIn("docker.io/", dh_step["if"])
        self.assertEqual(dh_step["with"]["username"], "${{ secrets.DOCKERHUB_USERNAME }}")
        self.assertEqual(dh_step["with"]["password"], "${{ secrets.DOCKERHUB_TOKEN }}")
        self.assertFalse(any("DigitalOcean" in s.get("name", "") for s in steps))

        buildx_index = next(i for i, s in enumerate(steps) if s.get("name") == "Set up Buildx")
        dh_index = steps.index(dh_step)
        self.assertLess(buildx_index, dh_index, "Set up Buildx must run before Docker Hub login")

    def test_published_images_are_attested_after_the_merge(self):
        """Provenance names the published digest, not the image buildx pushed."""
        build_step = next(s for s in WORKFLOW["jobs"]["build"]["steps"]
                          if s.get("name") == "Build and push by digest")
        # Buildx provenance would give each registry a different digest.
        self.assertIn("--provenance=false", build_step["run"])
        merge = WORKFLOW["jobs"]["merge"]
        self.assertEqual(merge["outputs"]["subjects"], "${{ steps.publish.outputs.subjects }}")
        publish = next(s for s in merge["steps"] if s.get("name") == "Create manifest lists and push")
        self.assertEqual(publish["id"], "publish")
        self.assertIn('subjects=$subjects" >> "$GITHUB_OUTPUT', publish["run"])
        attest = WORKFLOW["jobs"]["attest"]
        self.assertEqual(attest["needs"], "merge")
        self.assertIn("inputs.attest", attest["if"])
        self.assertEqual(attest["permissions"]["id-token"], "write")
        self.assertEqual(attest["permissions"]["attestations"], "write")
        self.assertEqual(attest["strategy"]["matrix"]["include"],
                         "${{ fromJSON(needs.merge.outputs.subjects) }}")
        step = next(s for s in attest["steps"] if s.get("uses", "").startswith("actions/attest@"))
        self.assertEqual(step["with"]["subject-name"], "${{ matrix.image }}")
        self.assertEqual(step["with"]["subject-digest"], "${{ matrix.digest }}")
        self.assertIn("ghcr.io/", step["with"]["push-to-registry"])
        spec = WORKFLOW["on"]["workflow_call"]["inputs"]["attest"]
        self.assertEqual((spec["type"], spec["default"]), ("boolean", "true"))

    def test_build_step_accepts_docker_io_repository(self):
        steps = WORKFLOW["jobs"]["build"]["steps"]
        build_step = next(s for s in steps if s.get("name") == "Build and push by digest")
        script = build_step["run"]
        self.assertIn("docker.io/*", script)
        self.assertIn("ghcr.io/* | docker.io/*", script)
        self.assertNotIn("registry.digitalocean.com", script)


if __name__ == "__main__":
    unittest.main()
