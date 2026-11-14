"""The cached apt install action: pinned actions, PR runs that never write the cache, and the
mirror list that keeps apt working when ubuntu.com is down."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / 'actions/install-apt-packages/action.yml'
MIRRORS = ROOT / 'actions/configure-apt-mirrors/configure-apt-mirrors.sh'


def steps():
    return yaml.load(ACTION.read_text(), Loader=yaml.BaseLoader)['runs']['steps']


class InstallAptPackagesTests(unittest.TestCase):
    def test_every_used_action_is_pinned_to_a_commit(self):
        for step in steps():
            if 'uses' in step:
                with self.subTest(step=step.get('name')):
                    self.assertRegex(step['uses'], r'@[0-9a-f]{40}$')

    def test_restore_and_save_use_the_same_key_and_path(self):
        restore = next(s for s in steps() if s.get('id') == 'restore')
        save = next(s for s in steps() if 'cache/save' in s.get('uses', ''))
        self.assertEqual(restore['with']['key'], save['with']['key'])
        self.assertEqual(restore['with']['path'], save['with']['path'])

    def test_by_default_only_pushes_to_the_default_branch_save(self):
        save = next(s for s in steps() if 'cache/save' in s.get('uses', ''))
        condition = re.sub(r'\s+', ' ', save['if'])
        self.assertIn("github.event_name == 'push'", condition)
        self.assertIn('github.ref_name == github.event.repository.default_branch', condition)
        self.assertIn("cache-hit != 'true'", condition)

    def test_install_step_uses_option_terminator(self):
        install_step = next(s for s in steps() if s.get('name') == 'Install packages')
        self.assertIn('install -y --no-install-recommends -- $PACKAGE_LIST', install_step['run'])

    def _run_key_step(self, packages):
        key_step = next(s for s in steps() if s.get('id') == 'key')
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "output"
            res = subprocess.run(
                ['bash', '-euo', 'pipefail', '-c', key_step['run']],
                env={**os.environ, 'PACKAGES': packages, 'GITHUB_OUTPUT': str(out_file)},
                capture_output=True,
                text=True,
            )
            outputs = {}
            if out_file.exists():
                for line in out_file.read_text().splitlines():
                    if '=' in line:
                        k, v = line.split('=', 1)
                        outputs[k] = v
            return res, outputs

    def test_valid_packages_accepted(self):
        cases = [
            ("curl", "curl "),
            ("curl git build-essential", "build-essential curl git "),
            ("libx11-dev:amd64 pkg-config=1.8.0-1", "libx11-dev:amd64 pkg-config=1.8.0-1 "),
            ("curl\ngit\nbuild-essential", "build-essential curl git "),
        ]
        for packages, expected_list in cases:
            with self.subTest(packages=packages):
                res, outputs = self._run_key_step(packages)
                self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
                self.assertEqual(outputs.get('list'), expected_list)

    def test_invalid_package_names_rejected(self):
        invalid_cases = [
            "-o APT::Update=1",
            "--config",
            "-y",
            "../traversal.deb",
            "/tmp/evil.deb",
            "curl; touch injected",
            "curl && touch injected",
            "curl | touch injected",
            "$(touch injected)",
            "`touch injected`",
            "curl>injected",
            "",
            "   \n  \t  ",
        ]
        for bad_pkg in invalid_cases:
            with self.subTest(bad_pkg=bad_pkg):
                res, _ = self._run_key_step(bad_pkg)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("::error::install-apt-packages:", res.stdout + res.stderr)



# apt-get that logs its operation and stalls (sends nothing, never exits) while STALLS says so.
FAKE_APT = """#!/bin/bash
op=update
for arg in "$@"; do case "$arg" in update|install|autoclean) op=$arg ;; --download-only) op=download ;; esac; done
echo "$op" >> "$APT_LOG"
n=$(grep -c "^$op$" "$APT_LOG")
if [ "$op" = "${STALL_OP:-}" ] && [ "$n" -le "${STALLS:-0}" ]; then exec /usr/bin/sleep 60; fi
"""


# Stands in for configure-apt-mirrors: logs each call, and fails when MIRRORS_FAIL is set.
FAKE_MIRRORS = """#!/bin/bash
echo mirrors >> "$APT_LOG"
[ -z "${MIRRORS_FAIL:-}" ]
"""


class StalledMirrorTests(unittest.TestCase):
    """The install step against a fake apt-get, with a one-second limit per mirror download."""

    def run_install(self, stall_op='', stalls=0, mirrors_fail=False, keep_mirror_calls=False):
        install_step = next(s for s in steps() if s.get('name') == 'Install packages')
        self.assertEqual(install_step['env']['APT_NETWORK_TIMEOUT'], '300')
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = Path(tmpdir) / 'bin'
            bin_dir.mkdir()
            # Keep the action's system-only man-db cleanup inside the fixture too.
            for name, body in [
                ('apt-get', FAKE_APT),
                ('configure-apt-mirrors', FAKE_MIRRORS),
                ('sudo', '#!/bin/bash\nexec "$@"\n'),
                ('sleep', '#!/bin/bash\n'),
                ('rm', '#!/bin/bash\nprintf "%s\\n" "$*" >> "$RM_LOG"\n[[ "$*" == "-f /var/lib/man-db/auto-update" ]]\n'),
            ]:
                (bin_dir / name).write_text(body, newline='\n')
                (bin_dir / name).chmod(0o755)
            log = Path(tmpdir) / 'apt.log'
            log.touch()
            rm_log = Path(tmpdir) / 'rm.log'
            rm_log.touch()
            res = subprocess.run(
                ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', install_step['run']],
                env={**os.environ, 'PATH': f"{bin_dir}{os.pathsep}{os.environ['PATH']}", 'HOME': tmpdir,
                     'PACKAGE_LIST': 'curl git ', 'APT_NETWORK_TIMEOUT': '1', 'APT_NETWORK_ATTEMPTS': '3',
                     'APT_LOG': str(log), 'RM_LOG': str(rm_log), 'STALL_OP': stall_op, 'STALLS': str(stalls),
                     'CONFIGURE_MIRRORS': str(bin_dir / 'configure-apt-mirrors'),
                     'MIRRORS_FAIL': '1' if mirrors_fail else ''},
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(rm_log.read_text().splitlines(), ['-f /var/lib/man-db/auto-update'])
            ops = log.read_text().split()
            if not keep_mirror_calls:
                ops = [op for op in ops if op != 'mirrors']
            return res, ops

    def test_a_stalled_update_is_retried(self):
        res, ops = self.run_install('update', stalls=1)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(ops, ['update', 'update', 'download', 'install', 'autoclean'])
        self.assertIn('apt-get update attempt 1 exited 124', res.stdout)

    def test_a_stalled_download_is_retried(self):
        res, ops = self.run_install('download', stalls=2)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(ops, ['update', 'download', 'download', 'download', 'install', 'autoclean'])

    def test_an_update_that_keeps_failing_continues_with_a_warning(self):
        res, ops = self.run_install('update', stalls=99)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(ops, ['update', 'update', 'update', 'download', 'install', 'autoclean'])
        self.assertIn('::warning::install-apt-packages: apt-get update failed 3 times', res.stdout)

    def test_a_download_that_keeps_stalling_fails_before_installing(self):
        res, ops = self.run_install('download', stalls=99)
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(ops, ['update', 'download', 'download', 'download'])
        self.assertIn('::error::install-apt-packages: downloading the packages failed 3 times', res.stdout)

    def test_the_mirrors_are_checked_before_every_download_attempt(self):
        res, ops = self.run_install('update', stalls=1, keep_mirror_calls=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(ops, ['mirrors', 'update', 'mirrors', 'update', 'mirrors', 'download',
                               'install', 'autoclean'])

    def test_a_failed_mirror_check_keeps_the_sources_and_installs(self):
        res, ops = self.run_install(mirrors_fail=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(ops, ['update', 'download', 'install', 'autoclean'])
        self.assertIn('::warning::install-apt-packages: configuring the apt mirrors failed', res.stdout)

    def test_the_mirror_script_sits_where_the_action_looks(self):
        install_step = next(s for s in steps() if s.get('name') == 'Install packages')
        self.assertEqual(install_step['env']['CONFIGURE_MIRRORS'],
                         '${{ github.action_path }}/../configure-apt-mirrors/configure-apt-mirrors.sh')
        self.assertTrue(MIRRORS.is_file())

    def test_a_partly_failed_update_is_an_error_so_it_is_retried(self):
        install_step = next(s for s in steps() if s.get('name') == 'Install packages')
        self.assertIn('network update -o APT::Update::Error-Mode=any', install_step['run'])


# curl that fails, as an unresolvable name does, for the hosts in DOWN, and logs what it fetched.
FAKE_CURL = """#!/bin/bash
url=${@: -1}
echo "$url" >> "$CURL_LOG"
host=${url#*://}
host=${host%%/*}
for down in ${DOWN:-}; do
  if [ "$host" = "$down" ]; then
    echo "curl: (6) Could not resolve host: $host" >&2
    exit 6
  fi
done
"""

# cloud-init that, like the real one on 11 Nov 2026, writes the image's ubuntu.sources (from
# IMAGE_SOURCES) just before `status --wait` returns.
FAKE_CLOUD_INIT = """#!/bin/bash
[ "$*" = "status --wait" ] || exit 2
sleep "${CLOUD_INIT_DELAY:-0}"
cp "$IMAGE_SOURCES" "$CRAFT_APT_ROOT/etc/apt/sources.list.d/ubuntu.sources"
"""

UBUNTU_HOSTS = 'azure.archive.ubuntu.com archive.ubuntu.com security.ubuntu.com ports.ubuntu.com'

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

PORTS_SOURCES = """Types: deb
URIs: http://ports.ubuntu.com/ubuntu-ports/
Suites: noble noble-updates noble-backports noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""

JAMMY_SOURCES_LIST = """# deb-src lines are commented out
deb mirror+file:/etc/apt/apt-mirrors.txt jammy main restricted
deb mirror+file:/etc/apt/apt-mirrors.txt jammy-updates main restricted
deb http://security.ubuntu.com/ubuntu jammy-security main restricted
deb https://esm.ubuntu.com/apps/ubuntu jammy-apps-security main
"""

IMAGE_MIRRORS = """http://azure.archive.ubuntu.com/ubuntu/\tpriority:1
https://archive.ubuntu.com/ubuntu/\tpriority:2
https://security.ubuntu.com/ubuntu/\tpriority:3
"""

# What install-riscv64-cross adds: its amd64 source must stay paired with its riscv64 source.
RISCV_LIST = 'deb [arch=amd64] https://archive.ubuntu.com/ubuntu noble main universe\n'


class ConfigureAptMirrorsTests(unittest.TestCase):
    """configure-apt-mirrors.sh against a fake /etc and a fake curl."""

    def configure(self, files, arch='amd64', codename='noble', down='', runs=1, cloud_init=False):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'root'
            files = {'etc/os-release': f'NAME="Ubuntu"\nVERSION_CODENAME={codename}\n', **files}
            for name, body in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(body, newline='\n')
            bin_dir = Path(tmpdir) / 'bin'
            bin_dir.mkdir()
            (bin_dir / 'curl').write_text(FAKE_CURL, newline='\n')
            (bin_dir / 'curl').chmod(0o755)
            curl_log = Path(tmpdir) / 'curl.log'
            curl_log.touch()
            (bin_dir / 'cloud-init').write_text(FAKE_CLOUD_INIT, newline='\n')
            (bin_dir / 'cloud-init').chmod(0o755)
            image_sources = Path(tmpdir) / 'image.sources'
            image_sources.write_text(NOBLE_SOURCES, newline='\n')
            for _ in range(runs):
                res = subprocess.run(
                    ['bash', str(MIRRORS)],
                    env={**os.environ, 'PATH': f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                         'CRAFT_APT_ROOT': str(root), 'CRAFT_APT_ARCH': arch, 'DOWN': down,
                         'CURL_LOG': str(curl_log), 'IMAGE_SOURCES': str(image_sources),
                         'CRAFT_CLOUD_INIT': str(bin_dir / 'cloud-init') if cloud_init else 'no-cloud-init-here'},
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            mirror_list = root / 'etc/apt/craft-apt-mirrors.txt'
            result = {name: (root / name).read_text() for name in files}
            result['list'] = mirror_list.read_text().splitlines() if mirror_list.exists() else None
            result['curl'] = curl_log.read_text().split()
            srv = root / 'etc/apt/apt.conf.d/99craft-no-srv'
            result['srv'] = srv.read_text() if srv.exists() else None
            return res, result

    def test_healthy_mirrors_keep_the_image_order_then_the_fallbacks(self):
        res, out = self.configure({'etc/apt/apt-mirrors.txt': IMAGE_MIRRORS,
                                   'etc/apt/sources.list.d/ubuntu.sources': NOBLE_SOURCES})
        self.assertEqual(out['list'], [
            'http://azure.archive.ubuntu.com/ubuntu/\tpriority:1',
            'https://archive.ubuntu.com/ubuntu/\tpriority:2',
            'https://security.ubuntu.com/ubuntu/\tpriority:3',
            'https://mirrors.edge.kernel.org/ubuntu/\tpriority:4',
            'https://mirrors.dotsrc.org/ubuntu/\tpriority:5',
        ])
        self.assertIn('https://mirrors.edge.kernel.org/ubuntu/dists/noble/InRelease', out['curl'])
        self.assertNotIn('::warning::', res.stdout)
        self.assertEqual(out['srv'], 'Acquire::EnableSrvRecords "false";\n')

    def test_mirrors_outside_ubuntu_com_lead_when_it_is_down(self):
        res, out = self.configure({'etc/apt/apt-mirrors.txt': IMAGE_MIRRORS,
                                   'etc/apt/sources.list.d/ubuntu.sources': NOBLE_SOURCES},
                                  down=UBUNTU_HOSTS)
        self.assertEqual([line.split('\t')[0] for line in out['list']], [
            'https://mirrors.edge.kernel.org/ubuntu/',
            'https://mirrors.dotsrc.org/ubuntu/',
            'http://azure.archive.ubuntu.com/ubuntu/',
            'https://archive.ubuntu.com/ubuntu/',
            'https://security.ubuntu.com/ubuntu/',
        ])
        self.assertIn('::warning::configure-apt-mirrors: http://azure.archive.ubuntu.com/ubuntu/ did not serve '
                      'noble/InRelease (curl: (6) Could not resolve host: azure.archive.ubuntu.com', res.stdout)

    def test_cloud_init_rewriting_the_sources_is_waited_out(self):
        res, out = self.configure({'etc/apt/apt-mirrors.txt': IMAGE_MIRRORS,
                                   'etc/apt/sources.list.d/ubuntu.sources': NOBLE_SOURCES},
                                  cloud_init=True)
        self.assertEqual(out['etc/apt/sources.list.d/ubuntu.sources'], NOBLE_SOURCES.replace(
            'mirror+file:/etc/apt/apt-mirrors.txt', 'mirror+file:/etc/apt/craft-apt-mirrors.txt'))

    def test_deb822_sources_read_the_new_list_and_keep_their_other_fields(self):
        _, out = self.configure({'etc/apt/apt-mirrors.txt': IMAGE_MIRRORS,
                                 'etc/apt/sources.list.d/ubuntu.sources': NOBLE_SOURCES})
        self.assertEqual(out['etc/apt/sources.list.d/ubuntu.sources'], NOBLE_SOURCES.replace(
            'mirror+file:/etc/apt/apt-mirrors.txt', 'mirror+file:/etc/apt/craft-apt-mirrors.txt'))

    def test_one_line_sources_change_only_the_ubuntu_archive(self):
        _, out = self.configure({'etc/apt/apt-mirrors.txt': IMAGE_MIRRORS,
                                 'etc/apt/sources.list': JAMMY_SOURCES_LIST}, codename='jammy')
        self.assertEqual(out['etc/apt/sources.list'], """# deb-src lines are commented out
deb mirror+file:/etc/apt/craft-apt-mirrors.txt jammy main restricted
deb mirror+file:/etc/apt/craft-apt-mirrors.txt jammy-updates main restricted
deb mirror+file:/etc/apt/craft-apt-mirrors.txt jammy-security main restricted
deb https://esm.ubuntu.com/apps/ubuntu jammy-apps-security main
""")
        self.assertIn('https://mirrors.edge.kernel.org/ubuntu/dists/jammy/InRelease', out['curl'])

    def test_arm_reads_ports_mirrors(self):
        _, out = self.configure({'etc/apt/sources.list.d/ubuntu.sources': PORTS_SOURCES},
                                arch='arm64', down=UBUNTU_HOSTS)
        self.assertEqual([line.split('\t')[0] for line in out['list']], [
            'https://mirrors.dotsrc.org/ubuntu-ports/',
            'https://ftp.tu-chemnitz.de/pub/linux/ubuntu-ports/',
            'https://ports.ubuntu.com/ubuntu-ports/',
        ])
        self.assertIn('URIs: mirror+file:/etc/apt/craft-apt-mirrors.txt\n',
                      out['etc/apt/sources.list.d/ubuntu.sources'])

    def test_several_archive_uris_in_one_field_become_one(self):
        sources = PORTS_SOURCES.replace(
            'URIs: http://ports.ubuntu.com/ubuntu-ports/',
            'URIs: http://ports.ubuntu.com/ubuntu-ports/ https://ports.ubuntu.com/ubuntu-ports')
        _, out = self.configure({'etc/apt/sources.list.d/ubuntu.sources': sources}, arch='arm64')
        self.assertIn('URIs: mirror+file:/etc/apt/craft-apt-mirrors.txt\n',
                      out['etc/apt/sources.list.d/ubuntu.sources'])

    def test_lists_other_actions_add_keep_their_sources(self):
        _, out = self.configure({'etc/apt/apt-mirrors.txt': IMAGE_MIRRORS,
                                 'etc/apt/sources.list.d/ubuntu.sources': NOBLE_SOURCES,
                                 'etc/apt/sources.list.d/amd64-primary.list': RISCV_LIST})
        self.assertEqual(out['etc/apt/sources.list.d/amd64-primary.list'], RISCV_LIST)

    def test_running_again_checks_anew_and_changes_nothing_else(self):
        files = {'etc/apt/apt-mirrors.txt': IMAGE_MIRRORS,
                 'etc/apt/sources.list.d/ubuntu.sources': NOBLE_SOURCES}
        _, once = self.configure(files)
        _, twice = self.configure(files, runs=2)
        self.assertEqual(once['etc/apt/sources.list.d/ubuntu.sources'],
                         twice['etc/apt/sources.list.d/ubuntu.sources'])
        self.assertEqual(once['list'], twice['list'])
        self.assertEqual(len(twice['curl']), 2 * len(once['curl']))

    def test_without_the_image_sources_nothing_changes(self):
        res, out = self.configure({})
        self.assertIsNone(out['list'])
        self.assertIn('::warning::configure-apt-mirrors: no sources.list or ubuntu.sources', res.stdout)


class AllowedMirrorTests(unittest.TestCase):
    """Every job that blocks egress and allows the Ubuntu archive also allows the fallbacks."""

    def test_block_mode_jobs_allow_the_fallback_mirrors(self):
        fallbacks = set(re.findall(r'https://([^/]+)/ubuntu/$', MIRRORS.read_text(), re.M))
        self.assertEqual(fallbacks, {'archive.ubuntu.com', 'security.ubuntu.com',
                                     'mirrors.edge.kernel.org', 'mirrors.dotsrc.org'})
        fallbacks -= {'archive.ubuntu.com', 'security.ubuntu.com'}
        checked = 0
        for workflow in sorted((ROOT / '.github/workflows').glob('*.yml')):
            jobs = yaml.load(workflow.read_text(), Loader=yaml.BaseLoader).get('jobs', {})
            for name, job in jobs.items():
                for step in job.get('steps', []):
                    allowed = step.get('with', {}).get('allowed-endpoints', '').split()
                    if 'harden-runner' in step.get('uses', '') and 'archive.ubuntu.com:443' in allowed:
                        checked += 1
                        for host in sorted(fallbacks):
                            with self.subTest(workflow=workflow.name, job=name, host=host):
                                self.assertIn(f'{host}:443', allowed)
        self.assertGreaterEqual(checked, 8)


WAIT = ROOT / 'actions/configure-apt-mirrors/wait-for-cloud-init.sh'


class WaitForCloudInitTests(unittest.TestCase):
    def wait(self, body, limit='300'):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = Path(tmpdir) / 'cloud-init'
            fake.write_text('#!/bin/bash\n' + body, newline='\n')
            fake.chmod(0o755)
            return subprocess.run(['bash', str(WAIT)], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, 'CRAFT_CLOUD_INIT': str(fake),
                                       'CRAFT_CLOUD_INIT_TIMEOUT': limit})

    def test_waits_until_cloud_init_is_done(self):
        res = self.wait('[ "$*" = "status --wait" ] && sleep 2\n')
        self.assertEqual(res.returncode, 0)
        self.assertRegex(res.stdout, r'Waited [23]s for cloud-init')

    def test_a_cloud_init_that_never_finishes_only_warns(self):
        res = self.wait('sleep 30\n', limit='1')
        self.assertEqual(res.returncode, 0)
        self.assertIn('::warning::cloud-init was still running after 1s', res.stdout)

    def test_a_cloud_init_that_reports_an_error_does_not_fail_the_job(self):
        res = self.wait('exit 1\n')
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, '')

    def test_without_cloud_init_it_returns_at_once(self):
        res = subprocess.run(['bash', str(WAIT)], capture_output=True, text=True, timeout=10,
                             env={**os.environ, 'CRAFT_CLOUD_INIT': 'no-cloud-init-here'})
        self.assertEqual((res.returncode, res.stdout), (0, ''))


if __name__ == '__main__':
    unittest.main()
