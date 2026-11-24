"""Released-source asset-pattern regression, NOT an old-Rust execution claim.
Only Photo's ASCII hyphen-name path is modeled. Suffixes, preference lists and
platform tokens come from blob-verified v0.19.2 source; stable controls must pick.
"""
import hashlib
import json
from pathlib import Path
import re
import unittest
ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/'tests/fixtures/launcher-v0.19.2'
SOURCE=(FIXTURE/'platform.rs').read_text()
VERSION=re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?')
SUFFIX_TEXT=SOURCE.split("fn suffix(self) -> &'static str {",1)[1].split('/// Where the launcher',1)[0]
SUFFIXES=dict(re.findall(r'Self::(\w+) => "([^"]+)"',SUFFIX_TEXT))
TOKEN_TEXT=SOURCE.split("fn tokens(self) -> &'static [&'static str] {",1)[1].split('/// Package formats',1)[0]
TOKENS={(os,arch):re.findall(r'"([^"]+)"',items) for os,arch,items in re.findall(r'\(Os::(\w+), Arch::(\w+)\) => &\[(.*?)\]',TOKEN_TEXT)}
DEFAULT_TEXT=SOURCE.split("pub fn default_kinds(self) -> &'static [PackageKind] {",1)[1].split('/// Every format',1)[0]
DEFAULTS={os:re.findall(r'PackageKind::(\w+)',items) for os,items in re.findall(r'Os::(\w+) => &\[(.*?)\]',DEFAULT_TEXT)}
KINDS=re.findall(r'PackageKind::(\w+)',re.search(r'const KINDS:.*?= \[(.*?)\];',SOURCE,re.S).group(1))


def parse(name):
    for kind in KINDS:
        suffix=SUFFIXES[kind]
        if not name.endswith(suffix): continue
        base=name[:-len(suffix)]
        if not base[:10].lower()=='photocraft' or base[10:11]!='-': return None
        rest=base[11:]; parts=rest.rsplit('-',2)
        if len(parts)!=3: return None
        version,os,arch=parts; match=VERSION.fullmatch(version)
        if not match: return None
        if match.group(4) and any(part.isdigit() and len(part)>1 and part[0]=='0' for part in match.group(4).split('.')): return None
        return kind,os+'-'+arch
    return None


def pick(names,os,arch,kinds=None):
    parsed=[(name,parse(name)) for name in names]
    for kind in DEFAULTS[os] if kinds is None else kinds:
        for token in TOKENS[(os,arch)]:
            for name,value in parsed:
                if value==(kind,token): return name
    return None


class ReleasedPatternTests(unittest.TestCase):
    def test_source_identity_and_consumed_tables_are_exact(self):
        identity=json.loads((FIXTURE/'SOURCE-IDENTITY.json').read_text()); data=(FIXTURE/'platform.rs').read_bytes()
        self.assertEqual(identity['commit'],'9f950e01ef728a3d32d997d59607051dee98a51d')
        self.assertEqual(hashlib.sha256(data).hexdigest(),identity['file']['sha256'])
        self.assertEqual(hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest(),identity['file']['git_blob'])
        self.assertEqual(len(KINDS),9); self.assertEqual(SUFFIXES['PortableZip'],'-portable.zip')
        self.assertEqual(DEFAULTS['MacOs'],['Dmg']); self.assertIn('linux-x86_64',TOKENS['Linux','X64'])
    def names(self):
        version='0.23.1-nightly.20261120.1234.1.g'+'a'*12
        targets=json.loads((ROOT/'actions'/'nightly-feed'/'apps.json').read_text())['apps']['photocraft']['targets']
        return sorted({row['asset_pattern'].replace('{version}',version) for row in targets.values()})
    def test_current_windows_nightly_does_not_match_old_installer_or_zip(self):
        names=self.names(); self.assertEqual(len(names),3)
        for arch in ('X64','X86','Arm64'): self.assertIsNone(pick(names,'Windows',arch))
        self.assertIsNone(parse(next(name for name in names if '-windows-' in name)))
    def test_windows_positive_stable_controls_and_deliberate_bad_rename(self):
        for name in ('photocraft-0.23.1-windows-x64.msi','photocraft-0.23.1-windows-x64-portable.zip'):
            self.assertEqual(pick([name],'Windows','X64'),name)
        bad='photocraft-0.23.1-nightly.20261120.1234.1.gaaaaaaaaaaaa-windows-x64-portable.zip'
        self.assertEqual(pick([bad],'Windows','X64'),bad) # matcher is not an inert rejector
    def test_macos_nightly_plain_zip_fails_even_for_explicit_portable_kind(self):
        for arch in ('X64','Arm64'):
            self.assertIsNone(pick(self.names(),'MacOs',arch)); self.assertIsNone(pick(self.names(),'MacOs',arch,['PortableZip']))
        control='photocraft-0.23.1-macos-aarch64.dmg'
        self.assertEqual(pick([control],'MacOs','Arm64'),control)
        portable='photocraft-0.23.1-macos-aarch64-portable.zip'
        self.assertEqual(pick([portable],'MacOs','Arm64',['PortableZip']),portable)
    def test_linux_nightly_tar_suffix_changes_the_parsed_token(self):
        name=next(name for name in self.names() if '-linux-' in name)
        self.assertEqual(parse(name),('TarGz','x86_64-nightly'))
        self.assertIsNone(pick(self.names(),'Linux','X64'))
        for name in ('photocraft-0.23.1-linux-x86_64.tar.gz','photocraft-0.23.1-linux-x86_64.AppImage'):
            self.assertEqual(pick([name],'Linux','X64'),name)
    def test_nightly_base_never_exceeds_its_published_stable(self):
        # Stable has no prerelease, and a future numeric base outranks this one.
        version='0.23.1-nightly.20261120.1234.1.g'+'a'*12
        match=VERSION.fullmatch(version); self.assertIsNotNone(match.group(4))
        stable=VERSION.fullmatch('0.23.1'); self.assertIsNone(stable.group(4))
        self.assertEqual(tuple(map(int,match.groups()[:3])),tuple(map(int,stable.groups()[:3])))
        self.assertLess(tuple(map(int,match.groups()[:3])),(0,24,0))

if __name__=='__main__': unittest.main()
