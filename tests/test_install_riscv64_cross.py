"""install-riscv64-cross: amd64 and riscv64 come from one pair of mirrors serving one snapshot."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / 'actions/install-riscv64-cross/action.yml'
SCRIPT = ROOT / 'actions/install-riscv64-cross/select-mirrors.sh'

# curl that fails, as an unresolvable name does, for the hosts in DOWN, and otherwise writes an
# InRelease whose Date is OLD_DATE for the URL prefixes in STALE and NEW_DATE for the rest.
FAKE_CURL = """#!/bin/bash
out=
while [ $# -gt 1 ]; do
  if [ "$1" = -o ]; then out=$2; shift; fi
  shift
done
url=$1
host=${url#*://}
host=${host%%/*}
for down in ${DOWN:-}; do
  if [ "$host" = "$down" ]; then
    echo "curl: (6) Could not resolve host: $host" >&2
    exit 6
  fi
done
date="Wed, 11 Nov 2026  9:35:45 UTC"
for stale in ${STALE:-}; do
  case "$url" in "$stale"*) date="Sun, 08 Nov 2026  0:30:00 UTC" ;; esac
done
printf 'Origin: Ubuntu\\nDate: %s\\n %s main/binary-riscv64/Packages.xz\\n' "$date" "$RANDOM" > "$out"
"""

# cloud-init that, like the real one on 11 Nov 2026, writes the image's ubuntu.sources (from
# IMAGE_SOURCES) just before `status --wait` returns.
FAKE_CLOUD_INIT = """#!/bin/bash
[ "$*" = "status --wait" ] || exit 2
sleep "${CLOUD_INIT_DELAY:-0}"
cp "$IMAGE_SOURCES" "$CRAFT_APT_ROOT/etc/apt/sources.list.d/ubuntu.sources"
"""

UBUNTU_HOSTS = 'archive.ubuntu.com security.ubuntu.com ports.ubuntu.com azure.archive.ubuntu.com'

NOBLE_SOURCES = """Types: deb
URIs: mirror+file:/etc/apt/apt-mirrors.txt
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: mirror+file:/etc/apt/apt-mirrors.txt
Suites: noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""

COMPONENTS = 'main universe restricted multiverse'


def lists(amd64, security, ports):
    return {
        'amd64-primary.list': f'deb [arch=amd64] {amd64} noble {COMPONENTS}\n'
                              f'deb [arch=amd64] {amd64} noble-updates {COMPONENTS}\n',
        'amd64-security.list': f'deb [arch=amd64] {security} noble-security {COMPONENTS}\n',
        'riscv64.list': f'deb [arch=riscv64] {ports} noble {COMPONENTS}\n'
                        f'deb [arch=riscv64] {ports} noble-updates {COMPONENTS}\n'
                        f'deb [arch=riscv64] {ports} noble-security {COMPONENTS}\n',
    }


UBUNTU = lists('https://archive.ubuntu.com/ubuntu', 'https://security.ubuntu.com/ubuntu',
               'https://ports.ubuntu.com/ubuntu-ports')
DOTSRC = lists('https://mirrors.dotsrc.org/ubuntu', 'https://mirrors.dotsrc.org/ubuntu',
               'https://mirrors.dotsrc.org/ubuntu-ports')
KERNEL = lists('https://mirrors.edge.kernel.org/ubuntu', 'https://mirrors.edge.kernel.org/ubuntu',
               'https://ftp.tu-chemnitz.de/pub/linux/ubuntu-ports')


class SelectMirrorsTests(unittest.TestCase):
    def select(self, files=None, down='', stale='', runs=1, cloud_init=False):
        files = {'etc/apt/sources.list.d/ubuntu.sources': NOBLE_SOURCES} if files is None else files
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'root'
            files = {'etc/os-release': 'NAME="Ubuntu"\nVERSION_CODENAME=noble\n', **files}
            for name, body in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(body, newline='\n')
            bin_dir = Path(tmpdir) / 'bin'
            bin_dir.mkdir()
            (bin_dir / 'curl').write_text(FAKE_CURL, newline='\n')
            (bin_dir / 'curl').chmod(0o755)
            (bin_dir / 'cloud-init').write_text(FAKE_CLOUD_INIT, newline='\n')
            (bin_dir / 'cloud-init').chmod(0o755)
            image_sources = Path(tmpdir) / 'image.sources'
            image_sources.write_text(NOBLE_SOURCES, newline='\n')
            for _ in range(runs):
                res = subprocess.run(
                    ['bash', str(SCRIPT)],
                    env={**os.environ, 'PATH': f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                         'CRAFT_APT_ROOT': str(root), 'DOWN': down, 'STALE': stale,
                         'IMAGE_SOURCES': str(image_sources),
                         'CRAFT_CLOUD_INIT': str(bin_dir / 'cloud-init') if cloud_init else 'no-cloud-init-here'},
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            parts = root / 'etc/apt/sources.list.d'
            written = {name: (parts / name).read_text() for name in UBUNTU}
            image = {name: (root / name).read_text() for name in files}
            return res, written, image

    def test_ubuntu_itself_when_it_answers(self):
        res, written, _ = self.select()
        self.assertEqual(written, UBUNTU)
        self.assertNotIn('::warning::', res.stdout)

    def test_dotsrc_for_both_architectures_when_ubuntu_com_is_down(self):
        res, written, _ = self.select(down=UBUNTU_HOSTS)
        self.assertEqual(written, DOTSRC)
        self.assertIn('::warning::install-riscv64-cross: no noble InRelease for amd64 from pair 1 '
                      '(curl: (6) Could not resolve host: archive.ubuntu.com', res.stdout)

    def test_a_pair_with_mismatched_snapshots_is_passed_over(self):
        res, written, _ = self.select(down=UBUNTU_HOSTS, stale='https://mirrors.dotsrc.org/ubuntu-ports/')
        self.assertEqual(written, KERNEL)
        self.assertIn('::warning::install-riscv64-cross: pair 2 serves different amd64 and riscv64 snapshots',
                      res.stdout)

    def test_without_a_matching_pair_the_first_that_answers_is_used(self):
        res, written, _ = self.select(down=UBUNTU_HOSTS, stale='https://mirrors.dotsrc.org/ubuntu-ports/ '
                                                                 'https://ftp.tu-chemnitz.de/')
        self.assertEqual(written, DOTSRC)
        self.assertIn('no mirror pair serves one snapshot for both architectures; using pair 2', res.stdout)

    def test_with_nothing_answering_ubuntu_itself_is_kept(self):
        res, written, _ = self.select(down=UBUNTU_HOSTS + ' mirrors.dotsrc.org mirrors.edge.kernel.org '
                                                          'ftp.tu-chemnitz.de')
        self.assertEqual(written, UBUNTU)
        self.assertIn('using pair 1', res.stdout)

    def test_the_image_sources_are_disabled_once(self):
        _, _, image = self.select(runs=2)
        sources = image['etc/apt/sources.list.d/ubuntu.sources']
        self.assertEqual(sources.count('Enabled: no'), 2)
        self.assertEqual(sources.replace('Enabled: no\n', ''), NOBLE_SOURCES)

    def test_cloud_init_re_enabling_the_image_sources_is_waited_out(self):
        _, _, image = self.select(cloud_init=True)
        self.assertEqual(image['etc/apt/sources.list.d/ubuntu.sources'].count('Enabled: no'), 2)

    def test_one_line_image_sources_are_commented_out(self):
        _, _, image = self.select({'etc/apt/sources.list': '# comment\n'
                                   'deb mirror+file:/etc/apt/apt-mirrors.txt noble main\n'
                                   'deb-src mirror+file:/etc/apt/apt-mirrors.txt noble main\n'})
        self.assertEqual(image['etc/apt/sources.list'], '# comment\n'
                         '# install-riscv64-cross: deb mirror+file:/etc/apt/apt-mirrors.txt noble main\n'
                         '# install-riscv64-cross: deb-src mirror+file:/etc/apt/apt-mirrors.txt noble main\n')

    def test_the_action_runs_the_script_before_apt(self):
        run = yaml.load(ACTION.read_text(), Loader=yaml.BaseLoader)['runs']['steps'][0]['run']
        self.assertLess(run.index('dpkg --add-architecture riscv64'),
                        run.index('sudo "$GITHUB_ACTION_PATH/select-mirrors.sh"'))
        self.assertLess(run.index('select-mirrors.sh'), run.index('apt-get update'))


if __name__ == '__main__':
    unittest.main()
