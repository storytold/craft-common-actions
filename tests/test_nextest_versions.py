"""Keep shared nextest installers on the reviewed fix for GHSA-vc25-qg58-w57c.

Only YAML data is inspected. Negative controls mutate actual parsed installer values in memory;
no workflow scripts, nextest executable, test binary or recording are executed.
"""
from copy import deepcopy
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
PIN = 'cargo-nextest@0.9.150'
# install-action installs this one tool under two names, `cargo-nextest` and the documented alias
# `nextest`; matching the crate name alone would count an alias installer as nothing at all.
NEXTEST_NAMES = ('cargo-nextest', 'nextest')
GATE = '.github/workflows/gate.yml'
SONAR = '.github/workflows/sonarcloud.yml'


def workflow_documents():
    documents = {}
    for directory in (ROOT / '.github/workflows', ROOT / 'actions'):
        for path in sorted(directory.rglob('*')):
            if path.is_file() and path.suffix in ('.yml', '.yaml'):
                documents[path.relative_to(ROOT).as_posix()] = yaml.load(
                    path.read_text(), Loader=yaml.BaseLoader)
    return documents


def tool_values(value, path=()):
    """Find literal installer tool lists and reusable workflow tools defaults."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + (key,)
            if key in ('tool', 'tools') and isinstance(child, str):
                yield child_path, child
            elif key == 'tools' and isinstance(child, dict) and 'default' in child:
                yield child_path + ('default',), child['default']
            yield from tool_values(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from tool_values(child, path + (index,))


def is_nextest(spec):
    """Whether a taiki-e/install-action spec installs nextest, under either of its two names."""
    return spec.split('@', 1)[0] in NEXTEST_NAMES


def nextest_tool_values(documents):
    for filename, document in documents.items():
        for path, value in tool_values(document):
            if isinstance(value, str):
                specs = re.split(r'[,\s]+', value.strip())
                if any(is_nextest(spec) for spec in specs):
                    yield filename, path, value, specs


def replace_value(document, path, value):
    for key in path[:-1]:
        document = document[key]
    document[path[-1]] = value


class NextestVersionTests(unittest.TestCase):
    def setUp(self):
        self.documents = workflow_documents()

    def assert_fixed_installers(self, documents):
        # Discover every tool/default site, not just fixtures for the three known sites.
        found = list(nextest_tool_values(documents))
        self.assertEqual(len(found), 3, 'expected gate default and both coverage installers')
        self.assertEqual(sorted(filename for filename, _, _, _ in found), [GATE, SONAR, SONAR])
        for filename, path, _, specs in found:
            # Either name installs the tool, so either name has to reach this assertion.
            nextest = [spec for spec in specs if is_nextest(spec)]
            # Do not wrap this assertion in subTest: negative controls must catch the exception.
            self.assertEqual(nextest, [PIN],
                             f'{filename}:{path}: nextest must use the exact reviewed fixed release')

    def test_every_shared_nextest_installer_is_fixed(self):
        self.assert_fixed_installers(self.documents)

    def test_discovers_the_documented_alias_at_every_site(self):
        # Spelling every installer with the alias must still inventory the same three sites, so an
        # alias installer is examined rather than skipped.
        changed = deepcopy(self.documents)
        for filename, path, value, _ in nextest_tool_values(self.documents):
            replace_value(changed[filename], path, value.replace('cargo-nextest', 'nextest'))
        found = list(nextest_tool_values(changed))
        self.assertEqual(sorted((filename, value) for filename, _, value, _ in found),
                         sorted([(GATE, 'nextest@0.9.150'),
                                 (SONAR, 'cargo-llvm-cov,nextest@0.9.150'),
                                 (SONAR, 'nextest@0.9.150')]))

    def test_gate_default_still_reaches_the_installer_and_allows_caller_overrides(self):
        gate = self.documents[GATE]
        self.assertEqual(gate['on']['workflow_call']['inputs']['tools']['default'], PIN)
        installers = [step for step in gate['jobs']['gate']['steps']
                      if step.get('uses', '').startswith('step-security/taiki-e-install-action@')]
        self.assertEqual(len(installers), 1)
        self.assertEqual(installers[0]['with']['tool'], '${{ inputs.tools }}')
        self.assertEqual(installers[0]['if'], "inputs.tools != ''")

    def test_coverage_keeps_llvm_cov_and_the_two_nextest_installers(self):
        jobs = self.documents[SONAR]['jobs']
        for job, expected in [('build', 'cargo-llvm-cov,' + PIN), ('coverage', PIN)]:
            with self.subTest(job=job):
                installers = [step for step in jobs[job]['steps']
                              if step.get('uses', '').startswith('step-security/taiki-e-install-action@')]
                self.assertEqual(len(installers), 1)
                self.assertEqual(installers[0]['with']['tool'], expected)

    def test_rejects_vulnerable_floating_prerelease_and_duplicate_pins_at_each_real_site(self):
        # The duplicates keep one fixed selector, so the site is still discovered and still
        # counted as one site: only the validation filter can reject the vulnerable selector
        # beside it, which is what pins the alias there independently of discovery.
        bad_pins = ('cargo-nextest@0.9.149', 'cargo-nextest',
                    'cargo-nextest@latest', 'cargo-nextest@0.9.150-rc.1',
                    'nextest@0.9.149', 'nextest',
                    'nextest@latest', 'nextest@0.9.150-rc.1',
                    PIN + ',cargo-nextest@0.9.149', PIN + ',nextest@0.9.149')
        for filename, path, value, _ in nextest_tool_values(self.documents):
            for bad in bad_pins:
                with self.subTest(filename=filename, path=path, bad=bad):
                    changed = deepcopy(self.documents)
                    replace_value(changed[filename], path, value.replace(PIN, bad))
                    with self.assertRaises(AssertionError):
                        self.assert_fixed_installers(changed)

    def test_rejects_an_additional_vulnerable_installer(self):
        # One control per name: the alias installs the same tool, so an extra alias installer must
        # be counted instead of passing the inventory unnoticed.
        for tool in ('cargo-nextest@0.9.149', 'nextest@0.9.149'):
            with self.subTest(tool=tool):
                changed = deepcopy(self.documents)
                changed[SONAR]['jobs']['coverage']['steps'].append({
                    'uses': 'step-security/taiki-e-install-action@' + '0' * 40,
                    'with': {'tool': tool},
                })
                with self.assertRaises(AssertionError):
                    self.assert_fixed_installers(changed)


if __name__ == '__main__':
    unittest.main()
