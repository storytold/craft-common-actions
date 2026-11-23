"""Source wiring for invocation admission; not hosted Actions or native signing proof."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'


def jobs(name):
    source = (WORKFLOWS / name).read_text().split('\njobs:\n', 1)[1]
    starts = list(re.finditer(r'^  ([a-z][a-z0-9_-]*):\n', source, re.M))
    return {match[1]: source[match.start():starts[index+1].start() if index+1 < len(starts) else len(source)]
            for index, match in enumerate(starts)}


class InvocationWorkflowTests(unittest.TestCase):
    def test_reusable_inputs_default_to_no_manual_actors_without_new_triggers(self):
        for name in ('nightly.yml', 'nightly-windows.yml', 'nightly-unix.yml', 'nightly-signing-unix.yml'):
            with self.subTest(workflow=name):
                source = (WORKFLOWS / name).read_text()
                self.assertIn("      manual-actors:\n", source)
                inputs = source.split('      manual-actors:\n', 1)[1].split('      tooling-revision:', 1)[0]
                self.assertIn("        type: string\n        default: '[]'", inputs)
                self.assertNotRegex(source, r'(?m)^  (?:schedule|workflow_dispatch|push|pull_request):')

    def test_preflight_precedes_signing_secret_exports_and_never_receives_credentials(self):
        for name, selected in (('nightly-windows.yml', ('windows',)),
                               ('nightly-signing-unix.yml', ('linux', 'macos'))):
            for job in selected:
                with self.subTest(workflow=name, job=job):
                    block = jobs(name)[job]
                    gate = block.index('run: python -I -B .nightly-tools/scripts/nightly_invocation.py')
                    self.assertLess(gate, block.index('${{ secrets.'))
                    self.assertLess(block.index('Verify exact tooling checkout'), gate)
                    gate_step = block[block.rfind('      - name:', 0, gate):gate]
                    self.assertIn('NIGHTLY_MANUAL_ACTORS: ${{ inputs.manual-actors }}', gate_step)
                    self.assertNotIn('secrets.', gate_step)
                    condition = block.split('    runs-on:', 1)[0]
                    self.assertIn('github.triggering_actor', condition)
                    self.assertIn("github.run_attempt == '1'", condition)
                    self.assertIn('contains(fromJSON(inputs.manual-actors), github.triggering_actor)', condition)
                    self.assertNotIn('github.actor', condition)

    def test_identity_and_each_mutating_path_repeat_current_attempt_admission(self):
        for job in ('identity', 'stage-nightly', 'attest-nightly', 'publish-nightly'):
            with self.subTest(job=job):
                block = jobs('nightly.yml')[job]
                gate = block.index('run: python -I -B .nightly-tools/scripts/nightly_invocation.py')
                commands = [index for token in ('nightly_policy.py prepare', 'gh attestation attest', 'publish_nightly.py', 'nightly_prune.py')
                            if (index := block.find(token)) >= 0]
                for index in commands:
                    self.assertLess(gate, index)
                self.assertNotIn('secrets.', block[block.rfind('      - name:', 0, gate):gate])
        self.assertIn('if: ${{ false }}', jobs('nightly.yml')['attest-nightly'])
        self.assertIn('if: ${{ false }}', jobs('nightly.yml')['publish-nightly'])

    def test_callees_receive_the_same_reviewed_literal_not_dispatch_data(self):
        for name in ('nightly.yml', 'nightly-unix.yml'):
            for job, block in jobs(name).items():
                if re.search(r'^    uses: .*nightly', block, re.M):
                    with self.subTest(workflow=name, job=job):
                        self.assertIn('      manual-actors: ${{ inputs.manual-actors }}', block)
        recipe = (ROOT / 'examples/photocraft-nightly.yml').read_text()
        self.assertIn("      manual-actors: '[]'", recipe)
        self.assertNotRegex(recipe, r'(?m)^  workflow_dispatch:')
        self.assertIn('on: {}', recipe)
        self.assertIn('if: ${{ false }}', recipe)
        self.assertIn('@REVIEWED_PUBLISHED_TOOLING_SHA_REQUIRED', recipe)
        self.assertNotRegex(recipe, r'(?m)^ *- cron:')


if __name__ == '__main__':
    unittest.main()
