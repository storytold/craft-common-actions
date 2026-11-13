"""The Linux deps action: pinned actions, and the packages the app builds look up."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / 'actions/install-linux-deps/action.yml'

# Installed before this action gained them: adding packages must not drop these
EXISTING_PACKAGES = (
    'libxkbcommon-dev',
    'libwayland-dev',
    'libx11-dev',
    'libxrandr-dev',
    'libxi-dev',
    'libgl1-mesa-dev',
    'libasound2-dev',
    'pkg-config',
)

# deckcraft's screen recording pulls scap: pipewire-sys and libdbus-sys find their headers through
# pkg-config, and bindgen needs libclang (storytold/craft-common-actions#186)
SCAP_PACKAGES = ('libpipewire-0.3-dev', 'libdbus-1-dev', 'libclang-dev')


def steps():
    return yaml.load(ACTION.read_text(), Loader=yaml.BaseLoader)['runs']['steps']


def install_command():
    step = next(s for s in steps() if s.get('name') == 'Install build and packaging tools')
    return step['run']


class InstallLinuxDepsTests(unittest.TestCase):
    def test_install_keeps_no_install_recommends(self):
        self.assertIn('install -y --no-install-recommends', install_command())

    def test_the_libraries_scap_finds_through_pkg_config_are_installed(self):
        line = install_command()
        for package in SCAP_PACKAGES:
            with self.subTest(package=package):
                self.assertIn(package, line)

    def test_the_packages_this_action_already_installed_are_kept(self):
        line = install_command()
        for package in EXISTING_PACKAGES:
            with self.subTest(package=package):
                self.assertIn(package, line)
