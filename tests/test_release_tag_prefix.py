"""Exercise draft release tag selection without contacting GitHub."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


class ReleaseTagPrefixTests(unittest.TestCase):
    def test_default_and_custom_tags(self):
        action = yaml.load(
            (Path(__file__).resolve().parents[1] / "actions/draft-release/action.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        step = next(s for s in action["runs"]["steps"] if s["name"] == "Create or update draft release")
        self.assertEqual(step["env"]["TAG_PREFIX"], "${{ inputs.tag-prefix }}")
        for prefix in (action["inputs"]["tag-prefix"]["default"], "artcraft-linux-v"):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                gh = root / "gh"
                gh.write_text('#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\nwith Path("calls").open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\nsys.exit(1 if sys.argv[1:3] == ["release", "view"] else 0)\n')
                gh.chmod(0o755)
                (root / "SHA256SUMS.txt").write_text("checksum  package.deb\n")
                subprocess.run(["bash", "-e", "-c", step["run"]], cwd=root, check=True,
                               capture_output=True, env=dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                               TAG_PREFIX=prefix, VERSION="0.42.0", APP_TITLE="Studio", PRERELEASE="false",
                               GITHUB_SHA="a" * 40, GITHUB_STEP_SUMMARY=str(root / "summary")))
                calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
                self.assertEqual(calls[0][:3], ["release", "view", prefix + "0.42.0"])
                self.assertEqual(calls[1][:3], ["release", "create", prefix + "0.42.0"])
