"""Dry runs are signing-free on any ref: every step that can see a signing secret is gated on not dry-run."""
from pathlib import Path
import json
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)
SIGNING = re.compile(r'\bsecrets\.(LINUX_SIGNING_KEY|AZURE_[A-Z_]+|WINDOWS_CERTIFICATE[A-Z_]*|APPLE_[A-Z_]+|KEYCHAIN_PASSWORD)\b')
# A secret expression that is empty on dry runs, e.g. ${{ !inputs.dry-run && secrets.X || '' }}.
BLANKED = re.compile(r"\$\{\{\s*!inputs\.dry-run\s*&&\s*secrets\.([A-Z_]+)\s*\|\|\s*''\s*\}\}")


def top_level(text):
    """Split an expression at top-level && and ||, outside parentheses and quoted strings.

    Returns (conjuncts, has_or). Parenthesized groups and strings stay whole.
    """
    parts, operators, depth, quoted, start, i = [], [], 0, False, 0, 0
    while i < len(text):
        char = text[i]
        if quoted:
            if char == "'":
                if text[i+1:i+2] == "'":
                    i += 1  # '' is an escaped quote inside a string
                else:
                    quoted = False
        elif char == "'":
            quoted = True
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif depth == 0 and text[i:i+2] in ('&&', '||'):
            parts.append(text[start:i].strip()); operators.append(text[i:i+2]); start = i + 2; i += 1
        i += 1
    parts.append(text[start:].strip())
    return parts, '||' in operators


def gated(condition):
    """True only when !inputs.dry-run is a top-level conjunct and nothing is ORed in at top level."""
    text = (condition or '').strip()
    if text.startswith('${{') and text.endswith('}}'):
        text = text[3:-2].strip()
    conjuncts, has_or = top_level(text)
    return not has_or and '!inputs.dry-run' in conjuncts


def signing_steps():
    for job_name, job in DATA['jobs'].items():
        for step in job.get('steps', []):
            names = set(SIGNING.findall(json.dumps(step)))
            if names:
                yield job_name, job, step, names


class DryRunSigningTests(unittest.TestCase):
    def test_every_step_that_sees_a_signing_secret_is_dry_run_free(self):
        steps = list(signing_steps())
        self.assertGreaterEqual(len(steps), 9)  # macOS 4, Windows 2, Linux key imports 3, draft release 1.
        for job_name, job, step, names in steps:
            label = f"{job_name}: {step.get('name') or step.get('uses')}"
            with self.subTest(step=label):
                if gated(job.get('if')) or gated(step.get('if')):
                    continue
                # A step that also builds (Windows) gets each secret blanked on dry runs instead.
                text = json.dumps(step)
                blanked = set(BLANKED.findall(text))
                self.assertTrue(names <= blanked, f'{label} sees {sorted(names - blanked)} on dry runs')
                self.assertEqual(len(SIGNING.findall(text)), len(BLANKED.findall(text)), label)

    def test_signing_requirements_and_key_imports_skip_dry_runs_on_the_release_branch(self):
        for job_name, job in DATA['jobs'].items():
            for step in job.get('steps', []):
                if step.get('name') in ('Require signing secrets on the release branch', 'Import the Linux signing key'):
                    with self.subTest(job=job_name, step=step['name']):
                        self.assertEqual(step['if'], "${{ !inputs.dry-run && github.ref == 'refs/heads/release' }}")

    def test_signing_that_follows_the_key_import_is_skipped_with_it(self):
        for job_name, job in DATA['jobs'].items():
            ids = {step.get('id') for step in job.get('steps', []) if step.get('name') == 'Import the Linux signing key'}
            for step in job.get('steps', []):
                condition = step.get('if') or ''
                if 'steps.linux_signing.outputs' in condition:
                    with self.subTest(job=job_name, step=step.get('name')):
                        self.assertIn('linux_signing', ids)
                        self.assertEqual(condition, "steps.linux_signing.outputs.enabled == 'true'")

    def test_dry_run_gate_helper(self):
        self.assertTrue(gated('${{ !inputs.dry-run }}'))
        self.assertTrue(gated("!inputs.dry-run && !cancelled()"))
        self.assertFalse(gated("github.ref == 'refs/heads/release'"))
        self.assertFalse(gated("inputs.dry-run || github.ref == 'refs/heads/release'"))
        self.assertFalse(gated("!inputs.dry-run || always()"))
        # An overriding OR would let a main dry run sign (CodeRabbit on #358).
        self.assertFalse(gated("!inputs.dry-run && github.ref == 'refs/heads/release' || github.ref == 'refs/heads/main'"))
        self.assertFalse(gated("${{ !inputs.dry-run && github.ref == 'refs/heads/release' || github.ref == 'refs/heads/main' }}"))
        self.assertFalse(gated("(!inputs.dry-run) && x"))  # Only a bare top-level conjunct counts.
        self.assertTrue(gated("${{ !inputs.dry-run && github.ref == 'refs/heads/release' }}"))
        self.assertTrue(gated("inputs.publish && !inputs.dry-run && (a || b)"))  # OR inside parentheses is fine.
        self.assertFalse(gated("github.ref == 'a || !inputs.dry-run' && b"))  # Quoted text is not an operator.
        self.assertFalse(gated("!inputs.dry-run-later && b"))


if __name__ == '__main__':
    unittest.main()
