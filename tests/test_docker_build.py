"""Verify the shared docker-build workflow handles Docker Hub alongside DOCR and GHCR."""

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

    def test_build_step_accepts_docker_io_repository(self):
        steps = WORKFLOW["jobs"]["build"]["steps"]
        build_step = next(s for s in steps if s.get("name") == "Build and push by digest")
        script = build_step["run"]
        self.assertIn("docker.io/*", script)
        self.assertIn("registry.digitalocean.com/* | ghcr.io/* | docker.io/*", script)


if __name__ == "__main__":
    unittest.main()
