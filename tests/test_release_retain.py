"""Retaining a draft protects assets uploaded by other platform workflows."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


class ReleaseRetainTests(unittest.TestCase):
    def test_existing_draft_assets(self):
        action = yaml.load((Path(__file__).resolve().parents[1] /
                            "actions/draft-release/action.yml").read_text(), Loader=yaml.BaseLoader)
        step = next(s for s in action["runs"]["steps"] if s["name"] == "Create or update draft release")
        self.assertEqual(action["inputs"]["retain"]["default"], "false")
        self.assertEqual(step["env"]["RETAIN"], "${{ inputs.retain }}")
        for retain in ("false", "true"):
            with self.subTest(retain=retain), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                gh = root / "gh"
                gh.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
with Path("calls").open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["release", "view"]:
    print("true" if "isDraft" in sys.argv else "Windows.exe\\nmacOS.dmg")
''')
                gh.chmod(0o755)
                (root / "SHA256SUMS.txt").write_text("checksum  Linux.deb\n")
                subprocess.run(["bash", "-e", "-c", step["run"]], cwd=root, check=True,
                               capture_output=True, env=dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                               TAG_PREFIX="artcraft-v", RETAIN=retain, VERSION="0.42.0", APP_TITLE="ArtCraft",
                               PRERELEASE="false", GITHUB_SHA="a" * 40, GITHUB_STEP_SUMMARY=str(root / "summary")))
                calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
                deleted = [c[3] for c in calls if c[:2] == ["release", "delete-asset"]]
                self.assertEqual(deleted, [] if retain == "true" else ["Windows.exe", "macOS.dmg"])
                upload = next(c for c in calls if c[:2] == ["release", "upload"])
                self.assertIn("--clobber", upload)
                self.assertEqual(upload[2], "artcraft-v0.42.0")

    def test_published_release_rejected_by_default(self):
        action = yaml.load((Path(__file__).resolve().parents[1] /
                            "actions/draft-release/action.yml").read_text(), Loader=yaml.BaseLoader)
        step = next(s for s in action["runs"]["steps"] if s["name"] == "Create or update draft release")
        self.assertEqual(action["inputs"]["allow-published"]["default"], "false")
        self.assertEqual(step["env"]["ALLOW_PUBLISHED"], "${{ inputs.allow-published }}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = root / "gh"
            gh.write_text('''#!/usr/bin/env python3
import sys
if sys.argv[1:3] == ["release", "view"]:
    print("false" if "isDraft" in sys.argv else "Windows.exe\\nmacOS.dmg")
''')
            gh.chmod(0o755)
            (root / "SHA256SUMS.txt").write_text("checksum  Linux.deb\n")
            res = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=root,
                                 capture_output=True, text=True,
                                 env=dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                                          TAG_PREFIX="artcraft-v", ALLOW_PUBLISHED="false", RETAIN="true",
                                          VERSION="0.41.0", APP_TITLE="ArtCraft", PRERELEASE="false",
                                          GITHUB_SHA="a" * 40, GITHUB_STEP_SUMMARY=str(root / "summary")))
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("already published", res.stdout + res.stderr)

    def test_published_release_allowed_when_configured(self):
        action = yaml.load((Path(__file__).resolve().parents[1] /
                            "actions/draft-release/action.yml").read_text(), Loader=yaml.BaseLoader)
        step = next(s for s in action["runs"]["steps"] if s["name"] == "Create or update draft release")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = root / "gh"
            gh.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
with Path("calls").open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["release", "view"]:
    print("false" if "isDraft" in sys.argv else "Windows.exe\\nmacOS.dmg")
''')
            gh.chmod(0o755)
            (root / "SHA256SUMS.txt").write_text("checksum  Linux.deb\n")
            subprocess.run(["bash", "-e", "-c", step["run"]], cwd=root, check=True,
                           capture_output=True, env=dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                           TAG_PREFIX="artcraft-v", ALLOW_PUBLISHED="true", RETAIN="true",
                           VERSION="0.41.0", APP_TITLE="ArtCraft", PRERELEASE="false",
                           GITHUB_SHA="a" * 40, GITHUB_STEP_SUMMARY=str(root / "summary")))
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
            # ensure gh release edit --draft was NOT called
            edits = [c for c in calls if c[:2] == ["release", "edit"]]
            self.assertEqual(edits, [])
            deleted = [c[3] for c in calls if c[:2] == ["release", "delete-asset"]]
            self.assertEqual(deleted, [])
            upload = next(c for c in calls if c[:2] == ["release", "upload"])
            self.assertIn("--clobber", upload)
            self.assertEqual(upload[2], "artcraft-v0.41.0")
