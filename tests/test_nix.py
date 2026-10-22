"""Exercise the reusable Nix workflow's argument boundary without installing Nix."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / ".github/workflows/nix.yml").read_text(), Loader=yaml.BaseLoader)
STEP = next(step for step in DATA["jobs"]["eval"]["steps"] if "run" in step)
DEFAULT = DATA["on"]["workflow_call"]["inputs"]["extra-args"]["default"]


class NixWorkflowTests(unittest.TestCase):
    def run_check(self, extra_args=DEFAULT, flake=True, nix_exit=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if flake:
                (root / "flake.nix").touch()
            tools = root / "bin"
            tools.mkdir()
            nix = tools / "nix"
            nix.write_text(f"#!{sys.executable}\n" + '''import json
import os
from pathlib import Path
import sys
Path("nix-args.json").write_text(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ["NIX_EXIT_CODE"]))
''')
            nix.chmod(0o755)
            expression = "${{ inputs.extra-args }}"
            # Model expression substitution in both run source and env values so
            # reintroducing direct interpolation reproduces the vulnerability.
            script = STEP["run"].replace(expression, extra_args)
            env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                   "NIX_EXIT_CODE": str(nix_exit)}
            env.update({key: value.replace(expression, extra_args)
                        for key, value in STEP.get("env", {}).items()})
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                cwd=root, env=env, text=True, capture_output=True, timeout=10,
            )
            log = root / "nix-args.json"
            args = json.loads(log.read_text()) if log.exists() else None
            return result, args, (root / "injected").exists()

    def test_default_arguments(self):
        result, args, _ = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args, ["flake", "check", "--no-build", "--all-systems"])

    def test_quotes_escapes_and_whitespace_preserve_arguments(self):
        result, args, _ = self.run_check(
            '--option name "value with spaces"\n\t--option empty "" '
            "--option escaped path\\ with\\ spaces --option quote 'say \"hi\"'"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args, ["flake", "check", "--option", "name", "value with spaces",
                                "--option", "empty", "", "--option", "escaped",
                                "path with spaces", "--option", "quote", 'say "hi"'])

    def test_shell_syntax_is_passed_as_data(self):
        cases = [
            ("--no-build; touch injected", ["--no-build;", "touch", "injected"]),
            ("--no-build\ntouch injected", ["--no-build", "touch", "injected"]),
            ("--no-build && touch injected", ["--no-build", "&&", "touch", "injected"]),
            ("--no-build | touch injected", ["--no-build", "|", "touch", "injected"]),
            ("--no-build > injected", ["--no-build", ">", "injected"]),
            ('"$(touch injected)"', ["$(touch injected)"]),
            ('"`touch injected`"', ["`touch injected`"]),
            ('"$HOME" *', ["$HOME", "*"]),
            ("--no-build\nPY\ntouch injected\n#", ["--no-build", "PY", "touch", "injected", "#"]),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                result, args, injected = self.run_check(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(injected)
                self.assertEqual(args, ["flake", "check", *expected])

    def test_empty_arguments(self):
        result, args, _ = self.run_check("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args, ["flake", "check"])

    def test_malformed_quotes_fail_before_nix(self):
        result, args, _ = self.run_check('--option name "unterminated')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(args)

    def test_nix_failure_is_preserved(self):
        result, args, _ = self.run_check(nix_exit=42)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIsNotNone(args)

    def test_missing_flake_skips_argument_parsing_and_nix(self):
        result, args, injected = self.run_check('"; touch injected', flake=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(args)
        self.assertFalse(injected)
        self.assertIn("No flake.nix present", result.stdout)


if __name__ == "__main__":
    unittest.main()
