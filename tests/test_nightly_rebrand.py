"""Tests for actions/nightly-rebrand: the rename rules and the deb/tar/rpm round trips."""

from pathlib import Path
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'actions/nightly-rebrand'))
import nightly_rebrand as nr  # noqa: E402
from nightly_rebrand import Entry  # noqa: E402

APP_ID = 'ai.storyteller.photocraft'

DESKTOP = f"""[Desktop Entry]
Type=Application
Name=PhotoCraft
Name[de]=PhotoCraft
GenericName=Image Editor
Exec=photocraft %F
TryExec=photocraft
Icon={APP_ID}
StartupWMClass={APP_ID}
MimeType=application/x-photocraft;image/png;

[Desktop Action new]
Name=New Window
Exec=/usr/bin/photocraft --new
"""

METAINFO = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- AppStream metadata -->
<component type="desktop-application">
  <id>{APP_ID}</id>
  <name>PhotoCraft</name>
  <name xml:lang="de">PhotoCraft</name>
  <developer id="ai.storyteller">
    <name>Learning Machines LLC</name>
  </developer>
  <launchable type="desktop-id">{APP_ID}.desktop</launchable>
  <icon type="stock">{APP_ID}</icon>
  <url type="homepage">https://getartcraft.com/apps/photocraft</url>
  <keywords>
    <keyword>photocraft</keyword>
  </keywords>
  <provides>
    <binary>photocraft</binary>
    <mediatype>application/x-photocraft</mediatype>
  </provides>
</component>
"""

MIME = f"""<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-photocraft">
    <comment>PhotoCraft document</comment>
    <icon name="{APP_ID}"/>
    <glob pattern="*.photocraft"/>
  </mime-type>
</mime-info>
"""

BASH = """_photocraft-cli() {
    cmd="photocraft__cli"
}
if [[ "${BASH_VERSINFO[0]}" -ge 4 ]]; then
    complete -F _photocraft-cli -o nosort -o bashdefault -o default photocraft-cli
fi
"""

ZSH = """#compdef photocraft-cli

_photocraft-cli() {
    _photocraft-cli_commands
}
if [ "$funcstack[1]" = "_photocraft-cli" ]; then
    _photocraft-cli "$@"
else
    compdef _photocraft-cli photocraft-cli
fi
"""

THUMBNAILER = """[Thumbnailer Entry]
TryExec=photocraft-cli
Exec=photocraft-cli thumbnail %i %o %s
MimeType=application/x-photocraft;
"""

FISH = """function __fish_photocraft_cli_needs_command
end
complete -c photocraft-cli -n "__fish_photocraft_cli_needs_command" -s h -l help
"""


def fixture_entries():
    """A stable tree laid out like photocraft's deb (nfpm.yaml plus package.sh's staging)."""
    def f(path, data=b'x', mode=0o644):
        return Entry(path, 'file', mode, data.encode() if isinstance(data, str) else data, mtime=1700000000)
    dirs = ['/usr', '/usr/bin', '/usr/share', '/usr/share/applications', '/usr/share/metainfo',
            '/usr/share/mime/packages', '/usr/share/icons/hicolor/256x256/apps',
            '/usr/share/doc/photocraft', '/usr/share/man/man1', '/usr/share/thumbnailers',
            '/usr/share/icons/hicolor/symbolic/apps',
            '/usr/share/bash-completion/completions', '/usr/share/zsh/site-functions',
            '/usr/share/zsh/vendor-completions', '/usr/share/fish/vendor_completions.d']
    return [Entry(d, 'dir', 0o755, mtime=1700000000) for d in dirs] + [
        f('/usr/bin/photocraft', b'\x7fELF photocraft', 0o755),
        f('/usr/bin/photocraft-cli', b'\x7fELF photocraft-cli', 0o755),
        f(f'/usr/share/applications/{APP_ID}.desktop', DESKTOP),
        f(f'/usr/share/metainfo/{APP_ID}.metainfo.xml', METAINFO),
        f(f'/usr/share/mime/packages/{APP_ID}.xml', MIME),
        f(f'/usr/share/icons/hicolor/256x256/apps/{APP_ID}.png'),
        f(f'/usr/share/icons/hicolor/256x256/apps/{APP_ID}.png.attribution'),
        f(f'/usr/share/icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg'),
        f('/usr/share/thumbnailers/photocraft.thumbnailer', THUMBNAILER),
        f('/usr/share/doc/photocraft/README.md'),
        f('/usr/share/man/man1/photocraft-cli.1'),
        f('/usr/share/man/man1/photocraft-cli-convert.1'),
        f('/usr/share/bash-completion/completions/photocraft-cli', BASH),
        f('/usr/share/zsh/site-functions/_photocraft-cli', ZSH),
        f('/usr/share/fish/vendor_completions.d/photocraft-cli.fish', FISH),
        Entry('/usr/share/zsh/vendor-completions/_photocraft-cli', 'symlink', 0o777,
              link='/usr/share/zsh/site-functions/_photocraft-cli'),
    ]


class RenameRuleTests(unittest.TestCase):
    def setUp(self):
        entries = fixture_entries()
        self.names = nr.Names.from_entries('photocraft', entries)
        converted, self.path_map = nr.convert(entries, self.names)
        self.out = {e.path: e for e in converted}

    def text(self, path):
        return self.out[path].data.decode()

    def test_paths(self):
        renamed = {old: new for old, new in self.path_map.items()
                   if old != new and not old.endswith(('/usr', '/share'))}
        self.assertEqual(renamed, {
            '/usr/bin/photocraft': '/usr/bin/photocraft-nightly',
            '/usr/bin/photocraft-cli': '/usr/bin/photocraft-cli-nightly',
            f'/usr/share/applications/{APP_ID}.desktop': f'/usr/share/applications/{APP_ID}.nightly.desktop',
            f'/usr/share/metainfo/{APP_ID}.metainfo.xml': f'/usr/share/metainfo/{APP_ID}.nightly.metainfo.xml',
            f'/usr/share/mime/packages/{APP_ID}.xml': f'/usr/share/mime/packages/{APP_ID}.nightly.xml',
            f'/usr/share/icons/hicolor/256x256/apps/{APP_ID}.png':
                f'/usr/share/icons/hicolor/256x256/apps/{APP_ID}.nightly.png',
            f'/usr/share/icons/hicolor/256x256/apps/{APP_ID}.png.attribution':
                f'/usr/share/icons/hicolor/256x256/apps/{APP_ID}.nightly.png.attribution',
            f'/usr/share/icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg':
                f'/usr/share/icons/hicolor/symbolic/apps/{APP_ID}.nightly-symbolic.svg',
            '/usr/share/thumbnailers/photocraft.thumbnailer': '/usr/share/thumbnailers/photocraft-nightly.thumbnailer',
            '/usr/share/doc/photocraft': '/usr/share/doc/photocraft-nightly',
            '/usr/share/doc/photocraft/README.md': '/usr/share/doc/photocraft-nightly/README.md',
            '/usr/share/man/man1/photocraft-cli.1': '/usr/share/man/man1/photocraft-cli-nightly.1',
            '/usr/share/man/man1/photocraft-cli-convert.1': '/usr/share/man/man1/photocraft-cli-nightly-convert.1',
            '/usr/share/bash-completion/completions/photocraft-cli':
                '/usr/share/bash-completion/completions/photocraft-cli-nightly',
            '/usr/share/zsh/site-functions/_photocraft-cli': '/usr/share/zsh/site-functions/_photocraft-cli-nightly',
            '/usr/share/zsh/vendor-completions/_photocraft-cli':
                '/usr/share/zsh/vendor-completions/_photocraft-cli-nightly',
            '/usr/share/fish/vendor_completions.d/photocraft-cli.fish':
                '/usr/share/fish/vendor_completions.d/photocraft-cli-nightly.fish',
        })
        link = self.out['/usr/share/zsh/vendor-completions/_photocraft-cli-nightly']
        self.assertEqual(link.link, '/usr/share/zsh/site-functions/_photocraft-cli-nightly')
        self.assertEqual(self.out['/usr/bin/photocraft-nightly'].mode, 0o755)

    def test_desktop_entry(self):
        lines = self.text(f'/usr/share/applications/{APP_ID}.nightly.desktop').splitlines()
        for line in ('Name=PhotoCraft (Nightly)', 'Name[de]=PhotoCraft (Nightly)', 'GenericName=Image Editor',
                     'Exec=photocraft-nightly %F', 'TryExec=photocraft-nightly', f'Icon={APP_ID}.nightly',
                     f'StartupWMClass={APP_ID}', 'Name=New Window', 'Exec=/usr/bin/photocraft-nightly --new'):
            self.assertIn(line, lines)
        self.assertFalse([line for line in lines if line.startswith('MimeType')])

    def test_symbolic_icon_names(self):
        self.assertEqual(self.names.rename_id(f'{APP_ID}-symbolic'), f'{APP_ID}.nightly-symbolic')
        self.assertEqual(self.names.rename_id(f'{APP_ID}-symbolic.svg'), f'{APP_ID}.nightly-symbolic.svg')
        self.assertEqual(self.names.rename_id(f'{APP_ID}-other'), f'{APP_ID}-other')

    def test_thumbnailer(self):
        text = self.text('/usr/share/thumbnailers/photocraft-nightly.thumbnailer')
        self.assertEqual(text, THUMBNAILER.replace('photocraft-cli', 'photocraft-cli-nightly'))

    def test_metainfo(self):
        text = self.text(f'/usr/share/metainfo/{APP_ID}.nightly.metainfo.xml')
        for snippet in (f'<id>{APP_ID}.nightly</id>', '<name>PhotoCraft (Nightly)</name>',
                        '<name xml:lang="de">PhotoCraft (Nightly)</name>',
                        '<name>Learning Machines LLC</name>',
                        f'<launchable type="desktop-id">{APP_ID}.nightly.desktop</launchable>',
                        f'<icon type="stock">{APP_ID}.nightly</icon>',
                        '<url type="homepage">https://getartcraft.com/apps/photocraft</url>',
                        '<keyword>photocraft</keyword>', '<binary>photocraft-nightly</binary>',
                        '<!-- AppStream metadata -->'):
            self.assertIn(snippet, text)
        self.assertNotIn('mediatype', text)
        self.assertIn('<provides>\n    <binary>photocraft-nightly</binary>\n  </provides>', text)
        only_mime = '<component><provides>\n  <mediatype>a/b</mediatype>\n</provides></component>'
        self.assertEqual(nr.rewrite_metainfo(only_mime, self.names, {}), '<component></component>')

    def test_mime_types_unchanged(self):
        text = self.text(f'/usr/share/mime/packages/{APP_ID}.nightly.xml')
        self.assertEqual(text, MIME.replace(f'<icon name="{APP_ID}"/>', f'<icon name="{APP_ID}.nightly"/>'))

    def test_completions(self):
        bash = self.text('/usr/share/bash-completion/completions/photocraft-cli-nightly')
        self.assertIn('_photocraft-cli-nightly() {', bash)
        self.assertIn('complete -F _photocraft-cli-nightly -o nosort -o bashdefault -o default photocraft-cli-nightly', bash)
        self.assertIn('cmd="photocraft__cli"', bash)
        zsh = self.text('/usr/share/zsh/site-functions/_photocraft-cli-nightly')
        self.assertTrue(zsh.startswith('#compdef photocraft-cli-nightly\n'))
        self.assertIn('_photocraft-cli-nightly() {\n    _photocraft-cli_commands', zsh)
        self.assertIn('"$funcstack[1]" = "_photocraft-cli-nightly"', zsh)
        self.assertIn('compdef _photocraft-cli-nightly photocraft-cli-nightly', zsh)
        fish = self.text('/usr/share/fish/vendor_completions.d/photocraft-cli-nightly.fish')
        self.assertIn('function __fish_photocraft_cli_nightly_needs_command\n', fish)
        self.assertIn('complete -c photocraft-cli-nightly -n "__fish_photocraft_cli_nightly_needs_command"', fish)

    def test_script_rewrite(self):
        script = (f'update-desktop-database -q /usr/share/applications\n'
                  f'xdg-mime default {APP_ID}.desktop application/x-photocraft\n'
                  f'ln -sf /usr/bin/photocraft /usr/local/bin/photocraft-tool\n')
        self.assertEqual(nr.rewrite_tokens(script, self.names, self.path_map), (
            'update-desktop-database -q /usr/share/applications\n'
            f'xdg-mime default {APP_ID}.nightly.desktop application/x-photocraft\n'
            'ln -sf /usr/bin/photocraft-nightly /usr/local/bin/photocraft-tool\n'))

    def test_shared_path_is_refused(self):
        entries = fixture_entries() + [Entry('/usr/share/pixmaps/craft.png', 'file', 0o644, b'x')]
        with self.assertRaisesRegex(nr.RebrandError, '/usr/share/pixmaps/craft.png'):
            nr.convert(entries, nr.Names.from_entries('photocraft', entries))

    def test_tarball_lib_is_checked_but_top_files_are_not(self):
        entries = fixture_entries() + [Entry('/usr/lib/craft/plugin.so', 'file', 0o644, b'x')]
        with self.assertRaisesRegex(nr.RebrandError, '/usr/lib/craft/plugin.so'):
            nr.convert(entries, nr.Names.from_entries('photocraft', entries), tarball=True)
        entries = fixture_entries() + [Entry('/usr/install.sh', 'file', 0o755, b'x')]
        nr.convert(entries, nr.Names.from_entries('photocraft', entries), tarball=True)

    def test_control_comments_are_skipped(self):
        self.assertEqual(nr.parse_control('# note\nPackage: a\n# inner\nDescription: d\n more\n'),
                         [['Package', 'a'], ['Description', 'd\n more']])

    def test_control_drops_stable_relations(self):
        control = ('Package: photocraft\nVersion: 0.24.1-1\nConflicts: photocraft, oldcraft\n'
                   'Provides: photocraft (= 0.24.1)\nReplaces: photocraft-legacy | photocraft\n'
                   'Description: Image editor\n Longer text.\n')
        self.assertEqual(nr.rebrand_control(control, 'photocraft-nightly', '0.24.1.2026-1'), (
            'Package: photocraft-nightly\nVersion: 0.24.1.2026-1\nConflicts: oldcraft\n'
            'Replaces: photocraft-legacy\nDescription: Image editor (Nightly)\n Longer text.\n'))


def newc(members):
    """A newc cpio archive, the rpm payload format: (name, mode, body) members."""
    out = b''
    for index, member in enumerate(members + [('TRAILER!!!', 0, b'')]):
        name, mode, body = member[:3]
        ino, nlink = member[3:5] if len(member) > 3 else (index, 1)
        encoded = name.encode() + b'\0'
        fields = [ino, mode, 0, 0, nlink, 1700000000, len(body), 0, 0, 0, 0, len(encoded), 0]
        out += b'070701' + b''.join(b'%08X' % f for f in fields) + encoded
        out += b'\0' * (-len(out) % 4) + body
        out += b'\0' * (-len(out) % 4)
    return out


class CpioTests(unittest.TestCase):
    def test_rpm_payload_entries(self):
        entries = nr.entries_from_cpio(newc([
            ('./usr/bin/photocraft', 0o100755, b'#!/bin/sh\n'),
            ('./usr/share/doc/photocraft', 0o40755, b''),
            ('./etc/photocraft.conf', 0o100644, b'a=1\n'),
            ('./usr/share/zsh/vendor-completions/_photocraft', 0o120777, b'../site-functions/_photocraft'),
        ]), {'/etc/photocraft.conf'})
        self.assertEqual(entries, [
            Entry('/usr/bin/photocraft', 'file', 0o755, b'#!/bin/sh\n', mtime=1700000000),
            Entry('/usr/share/doc/photocraft', 'dir', 0o755, mtime=1700000000),
            Entry('/etc/photocraft.conf', 'file', 0o644, b'a=1\n', mtime=1700000000, config=True),
            Entry('/usr/share/zsh/vendor-completions/_photocraft', 'symlink', 0o777,
                  link='../site-functions/_photocraft', mtime=1700000000),
        ])


class CpioHardlinkTests(unittest.TestCase):
    def test_every_link_gets_the_payload(self):
        entries = nr.entries_from_cpio(newc([
            ('./usr/bin/photocraft', 0o100755, b'', 7, 2),
            ('./usr/bin/photocraft-cli', 0o100755, b'ELF', 7, 2),
        ]), set())
        self.assertEqual([e.data for e in entries], [b'ELF', b'ELF'])

    def test_conflicting_link_data_is_refused(self):
        with self.assertRaises(nr.RebrandError):
            nr.entries_from_cpio(newc([('./a', 0o100644, b'x', 7, 2), ('./b', 0o100644, b'y', 7, 2)]), set())


class ConfinementTests(unittest.TestCase):
    def tar_with(self, *names):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = 1
                tar.addfile(info, io.BytesIO(b'x'))
        buf.seek(0)
        return tarfile.open(fileobj=buf)

    def test_dotdot_member_is_refused(self):
        with self.assertRaisesRegex(nr.RebrandError, 'escapes'):
            nr.entries_from_tar(self.tar_with('top/../../etc/passwd'), 'top/', '/usr')

    def test_absolute_member_is_refused(self):
        with self.assertRaisesRegex(nr.RebrandError, 'outside /usr'):
            nr.entries_from_tar(self.tar_with('top//etc/passwd'), 'top/', '/usr')

    def test_write_tarball_refuses_entries_outside_usr(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(nr.RebrandError, 'outside /usr'):
                nr.write_tarball(Path(tmp) / 'x.tar.gz', 'top', [Entry('/etc/x', 'file', 0o644, b'x')])


# filmcraft's packaging/linux/install.sh, trimmed to the parts that name packaged files.
FILMCRAFT_INSTALL = r"""#!/usr/bin/env bash
set -euo pipefail
fail() { echo "error: $*" >&2; exit 1; }
prefix="$2"
source_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)"
app_id=ai.storyteller.filmcraft
for binary in filmcraft filmcraft-cli; do
  [[ -f "$source_dir/bin/$binary" ]] || fail "missing bin/$binary; extract the complete tarball first"
done
[[ -f "$source_dir/share/applications/$app_id.desktop" ]] || fail 'missing desktop entry; extract the complete tarball first'

mkdir -p -- "$prefix/bin" "$prefix/share"
install -m755 -- "$source_dir/bin/filmcraft" "$prefix/bin/filmcraft"
install -m755 -- "$source_dir/bin/filmcraft-cli" "$prefix/bin/filmcraft-cli"
cp -R -- "$source_dir/share/." "$prefix/share/"
exec_path="$(printf '%s' "$prefix/bin/filmcraft" | sed 's/[\\"`$]/\\&/g; s/\\/\\\\/g')"
while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in
    Exec=*) printf 'Exec="%s" %%F\n' "$exec_path" ;;
    *) printf '%s\n' "$line" ;;
  esac
done <"$source_dir/share/applications/$app_id.desktop" >"$prefix/share/applications/$app_id.desktop"
printf 'Installed FilmCraft in %s\nLaunch it from the application menu or run "%s/bin/filmcraft".\n' "$prefix" "$prefix"
"""


class TarballExtraFileTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('bash'), 'needs bash')
    def test_root_install_script_installs_the_nightly(self):
        filmcraft_id = 'ai.storyteller.filmcraft'
        entries = [
            Entry('/usr/bin/filmcraft', 'file', 0o755, b'#!/bin/sh\n'),
            Entry('/usr/bin/filmcraft-cli', 'file', 0o755, b'#!/bin/sh\n'),
            Entry(f'/usr/share/applications/{filmcraft_id}.desktop', 'file', 0o644,
                  b'[Desktop Entry]\nName=FilmCraft\nExec=filmcraft %F\n'),
            Entry('/usr/install.sh', 'file', 0o755, FILMCRAFT_INSTALL.encode()),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stable_top, nightly_top = 'filmcraft-0.9.0-linux-x86_64', 'filmcraft-nightly-0.9.0.1-linux-x86_64'
            make_tarball(tmp / 'stable.tar.gz', stable_top, entries)
            nr.convert_tarball(tmp / 'stable.tar.gz', tmp / 'nightly.tar.gz', 'filmcraft', stable_top, nightly_top)
            with tarfile.open(tmp / 'nightly.tar.gz') as tar:
                tar.extractall(tmp / 'x', **({'filter': 'tar'} if hasattr(tarfile, 'tar_filter') else {}))
            script = (tmp / 'x' / nightly_top / 'install.sh').read_text()
            self.assertIn('app_id=ai.storyteller.filmcraft.nightly\n', script)
            self.assertIn('for binary in filmcraft-nightly filmcraft-cli-nightly; do', script)
            self.assertIn("printf 'Installed FilmCraft in %s", script)
            result = subprocess.run(['bash', str(tmp / 'x' / nightly_top / 'install.sh'), '--prefix',
                                     str(tmp / 'prefix')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((tmp / 'prefix/bin/filmcraft-nightly').is_file())
            self.assertTrue((tmp / 'prefix/bin/filmcraft-cli-nightly').is_file())
            desktop = (tmp / f'prefix/share/applications/{filmcraft_id}.nightly.desktop').read_text()
            self.assertIn(f'Exec="{tmp}/prefix/bin/filmcraft-nightly" %F', desktop)
            self.assertIn('Name=FilmCraft (Nightly)', desktop)


class InputTests(unittest.TestCase):
    def test_app_prefix_beside_cli_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('pdfcraft-1.2.0-linux-x86_64.tar.gz', 'pdfcraft-1.2.0-linux-x86_64.deb',
                         'pdfcraft-cli-1.2.0-linux-x86_64.tar.gz', 'pdfcraft-1.2.0-linux-x86_64.AppImage',
                         'pdfcraft-1.2.0-linux-x86_64.AppImage.zsync'):
                (Path(tmp) / name).write_bytes(b'')
            prefix, arch, inputs = nr.find_inputs(Path(tmp), '1.2.0')
        self.assertEqual((prefix, arch, sorted(inputs)), ('pdfcraft', 'x86_64', ['deb', 'tar.gz']))

    def test_refuses_nightly_channel_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = '0.24.1-nightly.20261124.153.1.gabcdef012345'
            (Path(tmp) / f'photocraft-{version}-linux-x86_64.deb').write_bytes(b'')
            with self.assertRaisesRegex(nr.RebrandError, 'nightly channel version'):
                nr.rebrand(Path(tmp), Path(tmp) / 'out', version, '0.24.1.202611240228')

    def test_refuses_files_already_named_nightly(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = '0.24.1'
            (Path(tmp) / f'photocraft-nightly-{version}-linux-x86_64.deb').write_bytes(b'')
            with self.assertRaisesRegex(nr.RebrandError, 'photocraft-nightly-.*already produced nightly packages'):
                nr.rebrand(Path(tmp), Path(tmp) / 'out', version, '0.24.1.202611240228')

    def test_refuses_tree_already_nightly(self):
        renamed = [e if not e.path.startswith('/usr/share/applications/') else
                   Entry(e.path.replace(APP_ID, APP_ID + '.nightly'), e.kind, e.mode, e.data)
                   for e in fixture_entries()]
        with self.assertRaisesRegex(nr.RebrandError, f'{APP_ID}.nightly already ends in .nightly'):
            nr.Names.from_entries('photocraft', renamed)
        with_bin = fixture_entries() + [Entry('/usr/bin/photocraft-nightly', 'file', 0o755, b'x')]
        with self.assertRaisesRegex(nr.RebrandError, 'already produced nightly packages'):
            nr.Names.from_entries('photocraft', with_bin)

    def test_nightly_version_must_be_numeric(self):
        with self.assertRaisesRegex(nr.RebrandError, 'digits and dots'):
            nr.rebrand(Path('.'), Path('.'), '0.24.1', '0.24.1~nightly')


def write_stable_tree(root, entries):
    for e in entries:
        target = root / e.path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        if e.kind == 'dir':
            target.mkdir(exist_ok=True)
        elif e.kind == 'symlink':
            os.symlink(e.link, target)
        else:
            target.write_bytes(e.data)
            os.chmod(target, e.mode)


def make_tarball(path, top, entries):
    with tarfile.open(path, 'w:gz') as tar:
        for e in entries:
            info = tarfile.TarInfo(top + e.path[len('/usr'):])
            info.mode = e.mode
            if e.kind == 'dir':
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif e.kind == 'file':
                info.size = len(e.data)
                tar.addfile(info, io.BytesIO(e.data))


POSTINST = '#!/bin/sh\nupdate-desktop-database -q /usr/share/applications || true\nexit 0\n'


@unittest.skipUnless(shutil.which('dpkg-deb'), 'needs dpkg-deb')
class DebAndTarballRoundTripTests(unittest.TestCase):
    def test_round_trip(self):
        self.round_trip('0.24.1', '0.24.1-1')

    def build_stable_deb(self, path, package='photocraft', version='0.24.1-1', entries=None):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            write_stable_tree(stage, fixture_entries() if entries is None else entries)
            (stage / 'DEBIAN').mkdir()
            (stage / 'DEBIAN/control').write_text(
                f'Package: {package}\nVersion: {version}\nSection: graphics\nPriority: optional\n'
                'Architecture: amd64\nMaintainer: PhotoCraft maintainers <photocraft@example.com>\n'
                'Installed-Size: 10\nDepends: libc6 (>= 2.35), libvulkan1 | libegl1\n'
                'Recommends: mesa-vulkan-drivers\nConflicts: photocraft-old, photocraft\n'
                'Homepage: https://getartcraft.com/apps/photocraft\n'
                'Description: Open-source, native image editor\n Layers, masks and brushes.\n')
            (stage / 'DEBIAN/postinst').write_text(POSTINST)
            os.chmod(stage / 'DEBIAN/postinst', 0o755)
            subprocess.run(['dpkg-deb', '--root-owner-group', '-b', str(stage), str(path)],
                           check=True, capture_output=True)

    def test_refuses_deb_already_named_nightly(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = Path(tmp) / 'in'
            inputs.mkdir()
            self.build_stable_deb(inputs / 'photocraft-0.24.1-linux-x86_64.deb', package='photocraft-nightly')
            with self.assertRaisesRegex(nr.RebrandError, 'already produced nightly packages'):
                nr.rebrand(inputs, Path(tmp) / 'out', '0.24.1', '0.24.1.202611240228')
            self.assertFalse((Path(tmp) / 'out' / 'photocraft-nightly-0.24.1.202611240228-linux-x86_64.deb').exists())

    def round_trip(self, version, deb_version):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            inputs, out = tmp / 'in', tmp / 'out'
            inputs.mkdir()
            entries = fixture_entries()
            stable_base = f'photocraft-{version}-linux-x86_64'
            self.build_stable_deb(inputs / f'{stable_base}.deb', version=deb_version)
            make_tarball(inputs / f'{stable_base}.tar.gz', stable_base,
                         [e for e in entries if e.kind != 'symlink'])
            (inputs / f'{stable_base}.AppImage').write_bytes(b'')

            result = nr.rebrand(inputs, out, version, '0.24.1.202611240228')

            base = out / 'photocraft-nightly-0.24.1.202611240228-linux-x86_64'
            self.assertEqual(result, {'prefix': 'photocraft', 'tarball': f'{base}.tar.gz',
                                      'files': [f'{base}.tar.gz', f'{base}.deb']})
            deb = f'{base}.deb'

            def field(name):
                return subprocess.run(['dpkg-deb', '-f', deb, name], check=True, capture_output=True,
                                      text=True).stdout.strip()
            self.assertEqual(field('Package'), 'photocraft-nightly')
            self.assertEqual(field('Version'), '0.24.1.202611240228-1')
            self.assertEqual(field('Depends'), 'libc6 (>= 2.35), libvulkan1 | libegl1')
            self.assertEqual(field('Recommends'), 'mesa-vulkan-drivers')
            self.assertEqual(field('Conflicts'), 'photocraft-old')
            self.assertEqual(field('Section'), 'graphics')
            self.assertEqual(field('Homepage'), 'https://getartcraft.com/apps/photocraft')
            self.assertEqual(field('Description'),
                             'Open-source, native image editor (Nightly)\n Layers, masks and brushes.')

            listing = subprocess.run(['dpkg-deb', '-c', deb], check=True, capture_output=True,
                                     text=True).stdout.splitlines()
            files = {line.split()[5]: line.split() for line in listing}
            self.assertEqual(files['./usr/bin/photocraft-nightly'][:2], ['-rwxr-xr-x', 'root/root'])
            self.assertEqual(files[f'./usr/share/applications/{APP_ID}.nightly.desktop'][0], '-rw-r--r--')
            self.assertEqual(files['./usr/share/zsh/vendor-completions/_photocraft-cli-nightly'][-1],
                             '/usr/share/zsh/site-functions/_photocraft-cli-nightly')
            stable = {line.split()[5] for line in subprocess.run(
                ['dpkg-deb', '-c', str(inputs / f'{stable_base}.deb')],
                check=True, capture_output=True, text=True).stdout.splitlines()}
            shared = {p for p, cols in files.items() if not cols[0].startswith('d')} & stable
            self.assertEqual(shared, set())

            control = tmp / 'control'
            subprocess.run(['dpkg-deb', '-e', deb, str(control)], check=True)
            self.assertEqual((control / 'postinst').read_text(), POSTINST)
            self.assertIn('usr/bin/photocraft-nightly\n', (control / 'md5sums').read_text())

            with tarfile.open(f'{base}.tar.gz') as tar:
                names = tar.getnames()
                desktop = tar.extractfile(
                    f'{base.name}/share/applications/{APP_ID}.nightly.desktop').read().decode()
                self.assertEqual(tar.getmember(f'{base.name}/bin/photocraft-nightly').mode, 0o755)
            self.assertIn(f'{base.name}/share/doc/photocraft-nightly/README.md', names)
            self.assertTrue(all(n == base.name or n.startswith(base.name + '/') for n in names))
            self.assertIn('Exec=photocraft-nightly %F', desktop)


@unittest.skipUnless(shutil.which('rpm') and shutil.which('nfpm'), 'needs rpm and nfpm')
class RpmRoundTripTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            inputs, out, stage = tmp / 'in', tmp / 'out', tmp / 'stage'
            inputs.mkdir()
            write_stable_tree(stage, [e for e in fixture_entries() if e.kind != 'symlink'])
            (tmp / 'postinst.sh').write_text(POSTINST)
            (tmp / 'nfpm.yaml').write_text(f"""name: photocraft
arch: amd64
platform: linux
version: 0.24.1
release: 1
maintainer: PhotoCraft maintainers <photocraft@example.com>
vendor: Learning Machines LLC
homepage: https://getartcraft.com/apps/photocraft
license: MIT OR Apache-2.0
description: |-
  Open-source, native image editor.
  Layers, masks and brushes.
depends:
  - libX11.so.6()(64bit)
recommends:
  - mesa-vulkan-drivers
contents:
  - src: {stage}/usr/bin/
    dst: /usr/bin/
    type: tree
  - src: {stage}/usr/share/
    dst: /usr/share/
    type: tree
scripts:
  postinstall: {tmp}/postinst.sh
""")
            subprocess.run(['nfpm', 'package', '-f', str(tmp / 'nfpm.yaml'), '-p', 'rpm', '-t',
                            str(inputs / 'photocraft-0.24.1-linux-x86_64.rpm')], check=True, capture_output=True)
            nr.rebrand(inputs, out, '0.24.1', '0.24.1.202611240228')
            rpm = str(out / 'photocraft-nightly-0.24.1.202611240228-linux-x86_64.rpm')

            def query(*args):
                return subprocess.run(['rpm', '-qp', '--nosignature', *args, rpm], check=True,
                                      capture_output=True, text=True).stdout
            self.assertEqual(query('--qf', '%{NAME}|%{VERSION}|%{RELEASE}|%{EPOCH}|%{SUMMARY}'),
                             'photocraft-nightly|0.24.1.202611240228|1|(none)|'
                             'Open-source, native image editor. (Nightly)')
            self.assertIn('libX11.so.6()(64bit)', query('--requires'))
            self.assertIn('mesa-vulkan-drivers', query('--recommends'))
            self.assertIn('update-desktop-database', query('--scripts'))
            listed = query('--list').split()
            self.assertIn('/usr/bin/photocraft-nightly', listed)
            self.assertNotIn('/usr/bin/photocraft', listed)


if __name__ == '__main__':
    unittest.main()
