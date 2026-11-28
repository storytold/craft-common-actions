"""actions/gate-setup, the setup every gate.yml job shares: pinned actions, and a dependency cache
that only the shared rust-cache action keys."""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SETUP = yaml.load((ROOT / 'actions/gate-setup/action.yml').read_text(), Loader=yaml.BaseLoader)
STEPS = SETUP['runs']['steps']
OWN = 'storytold/craft-common-actions/actions/'
# First-party actions in storytold/craft-common-actions are always referenced at @main.
OWN_ACTION_REV = 'main'
CACHE = f'{OWN}rust-cache@{OWN_ACTION_REV}'


class PinTests(unittest.TestCase):
    def test_every_action_is_pinned_to_a_commit(self):
        for s in STEPS:
            if 'uses' in s:
                with self.subTest(uses=s['uses']):
                    if s['uses'].startswith(OWN):
                        self.assertTrue(s['uses'].endswith('@main'))
                    else:
                        self.assertRegex(s['uses'], r'@[0-9a-f]{40}$')

    def test_first_party_actions_share_one_reviewed_revision(self):
        names = set()
        for s in STEPS:
            if s.get('uses', '').startswith(OWN):
                name, revision = s['uses'][len(OWN):].split('@')
                self.assertEqual(revision, OWN_ACTION_REV, name)
                names.add(name)
        self.assertEqual(names, {'checkout-craft-i18n', 'install-apt-packages', 'rust-cache', 'restore-mtimes'})


class CacheTests(unittest.TestCase):
    def test_restores_through_the_shared_action_only(self):
        calls = [s for s in STEPS if s.get('uses') == CACHE]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['with']['cache-version'], '${{ inputs.cache-version }}')
        self.assertEqual(calls[0]['with']['save-if'], '${{ inputs.cache-save }}')
        self.assertEqual(calls[0]['with']['cache-on-failure'], 'false')
        for s in STEPS:
            with self.subTest(step=s.get('name', s.get('uses'))):
                self.assertNotRegex(s.get('uses', ''), r'^[^/]+/rust-cache@')

    def test_steps_before_the_cache_do_not_add_cargo_home_or_a_wrapper_to_the_key(self):
        cache = next(i for i, s in enumerate(STEPS) if s.get('uses') == CACHE)
        for s in STEPS[:cache]:
            # Conservative source guard, not shell data-flow analysis.
            self.assertNotIn('CARGO_HOME', s.get('run', ''), s.get('name', s.get('uses')))
            self.assertNotIn('RUSTC_WRAPPER', s.get('run', ''), s.get('name', s.get('uses')))
        install = next(s for s in STEPS if s.get('name') == 'Install Rust')
        # Replacing rustup with an action that exports CARGO_HOME needs a cache-contract review.
        self.assertNotIn('GITHUB_ENV', install['run'])

    def test_mtimes_are_restored_only_for_a_restored_workspace_entry(self):
        restore = next(s for s in STEPS if s.get('uses', '').startswith(f'{OWN}restore-mtimes@'))
        self.assertEqual(restore['with'], {'mode': 'restore'})
        self.assertEqual(restore['if'], "inputs.rust-cache == 'true' && inputs.cache-workspace-crates == 'true' "
                                        "&& inputs.cold-build != 'true'")
        self.assertGreater(STEPS.index(restore), next(i for i, s in enumerate(STEPS) if s.get('uses') == CACHE))


if __name__ == '__main__':
    unittest.main()
