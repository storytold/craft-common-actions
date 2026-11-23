"""The unreached-rs check, run on small Cargo workspaces built in temporary directories."""
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "actions/unreached-rs/unreached_rs.py"


def run(directory, *args):
    done = subprocess.run([sys.executable, str(SCRIPT), "--directory", str(directory), *args],
                          capture_output=True, text=True)
    files = sorted(line.removeprefix("::error file=").split("::", 1)[0] for line in done.stdout.splitlines()
                   if line.startswith("::error file="))
    return done.returncode, files


def write(root, files):
    for name, text in files.items():
        path = Path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


CRATE = {
    "Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\n',
    "src/lib.rs": 'mod used;\nmod inline { mod deep; }\n// mod commented;\n/* mod blocked; */\n'
                  'include!("gen.rs");\n#[cfg_attr(test, path = "elsewhere/p.rs")]\nmod renamed;\n',
    "src/used.rs": "mod child;\n",
    "src/used/child.rs": "",
    "src/inline/deep.rs": "",
    "src/gen.rs": "",
    "src/elsewhere/p.rs": "mod kid;\n",
    "src/elsewhere/kid.rs": "",
    "src/commented.rs": "",
    "src/blocked.rs": "",
    "src/orphan.rs": "",
    "tests/it.rs": "",
    "tests/multi/main.rs": "mod helper;\n",
    "tests/multi/helper.rs": "",
    "src/bin/tool.rs": "",
}


class UnreachedRsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write(self.root, CRATE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_strict_follows_mod_path_and_include(self):
        code, files = run(self.root, "--mode", "strict")
        self.assertEqual(code, 1)
        self.assertEqual(files, ["src/blocked.rs", "src/commented.rs", "src/orphan.rs"])

    def test_allowlist_and_off(self):
        allow = "# on purpose\nsrc/*.rs\n"
        self.assertEqual(run(self.root, "--mode", "strict", "--allow", allow), (0, []))
        self.assertEqual(run(self.root, "--mode", "off")[0], 0)

    def test_workspace_members_and_nested_manifests(self):
        write(self.root, {
            "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\nexclude = ["crates/skip"]\n',
            "crates/a/Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\n',
            "crates/a/src/lib.rs": "",
            "crates/a/src/stray.rs": "",
            "crates/a/fixture/Cargo.toml": '[package]\nname = "f"\nversion = "0.1.0"\n',
            "crates/a/fixture/src/lib.rs": "mod nothing;\n",
            "crates/skip/Cargo.toml": '[package]\nname = "s"\nversion = "0.1.0"\n',
            "crates/skip/src/lib.rs": "",
            "crates/skip/src/stray.rs": "",
        })
        code, files = run(self.root, "--mode", "strict")
        self.assertIn("crates/a/src/stray.rs", files)
        self.assertNotIn("crates/skip/src/stray.rs", files)
        self.assertFalse([f for f in files if "fixture" in f])

    def test_diff_mode_checks_only_added_or_modified_files(self):
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")
        base = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        # The existing orphans are a backlog diff mode ignores.
        self.assertEqual(run(self.root, "--mode", "diff", "--base", base), (0, []))
        write(self.root, {"src/reached_new.rs": "", "src/lib.rs": CRATE["src/lib.rs"] + "mod reached_new;\n"})
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "reached")
        self.assertEqual(run(self.root, "--mode", "diff", "--base", base), (0, []))
        write(self.root, {"src/added_orphan.rs": "", "src/orphan.rs": "// touched\n"})
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "orphans")
        self.assertEqual(run(self.root, "--mode", "diff", "--base", base),
                         (1, ["src/added_orphan.rs", "src/orphan.rs"]))
        self.assertEqual(run(self.root, "--mode", "diff", "--base", base, "--allow", "src/*orphan.rs"), (0, []))

    def test_diff_mode_without_base_passes(self):
        self.assertEqual(run(self.root, "--mode", "diff"), (0, []))

    def test_diff_mode_with_an_unfetchable_base_passes(self):
        self.assertEqual(run(self.root, "--mode", "diff", "--base", "1" * 40), (0, []))


    def strict(self, files, manifest=""):
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write(self.root, {"Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\n' + manifest, **files})
        return run(self.root, "--mode", "strict")

    def assertStrict(self, result, expected):
        self.assertEqual(result, (1 if expected else 0, expected))

    def tree(self, files, *args):
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write(self.root, files)
        return run(self.root, "--mode", "strict", *args)

    def test_unicode_and_raw_identifiers(self):
        self.assertStrict(self.strict({"src/lib.rs": '#[path = "café.rs"]\nmod café;\nmod r#type;\nfn é() {}\n',
                                      "src/café.rs": "", "src/type.rs": ""}), [])

    def test_a_file_is_scanned_as_module_and_as_target_root(self):
        files = {"src/lib.rs": "mod shared;\n", "src/shared.rs": "mod child;\nfn main() {}\n",
                 "src/shared/child.rs": "", "src/child.rs": ""}
        self.assertStrict(self.strict(files, '[[bin]]\nname = "shared"\npath = "src/shared.rs"\n'), [])

    def test_cfg_attr_path_keeps_every_alternative(self):
        files = {"src/lib.rs": '#[cfg_attr(test, path = "mock.rs")]\n#[cfg_attr(unix, path = "unix.rs")]\nmod backend;\n'
                               '#[path = "only.rs"]\nmod fixed;\n',
                 "src/backend.rs": "", "src/mock.rs": "", "src/unix.rs": "", "src/only.rs": "", "src/fixed.rs": ""}
        self.assertStrict(self.strict(files), ["src/fixed.rs"])

    def test_path_on_inline_module_sets_its_directory(self):
        files = {"src/lib.rs": '#[path = "alternate"]\nmod outer { mod child; }\n',
                 "src/alternate/child.rs": "", "src/outer/child.rs": ""}
        self.assertStrict(self.strict(files), ["src/outer/child.rs"])

    def test_included_file_resolves_modules_in_its_own_directory(self):
        files = {"src/lib.rs": 'include!("fragments/body.rs");\n', "src/fragments/body.rs": "mod child;\n",
                 "src/fragments/child.rs": "", "src/child.rs": ""}
        self.assertStrict(self.strict(files), ["src/child.rs"])

    def test_path_dependencies_inside_the_workspace_are_crates(self):
        files = {"src/lib.rs": "", "helper/Cargo.toml": '[package]\nname = "helper"\nversion = "0.1.0"\n',
                 "helper/src/lib.rs": "", "helper/src/orphan.rs": ""}
        self.assertStrict(self.strict(files, '[dependencies]\nhelper = { path = "helper" }\n'),
                         ["helper/src/orphan.rs"])

    def test_manifest_paths_replace_the_default_roots(self):
        files = {"src/lib.rs": "mod old;\n", "src/old.rs": "", "src/actual.rs": "", "build.rs": "mod b;\n",
                 "src/b.rs": "", "ci/build.rs": ""}
        manifest = 'build = "ci/build.rs"\n[lib]\npath = "src/actual.rs"\n'
        self.assertStrict(self.strict(files, manifest), ["build.rs", "src/b.rs", "src/lib.rs", "src/old.rs"])

    def test_disabled_auto_discovery_keeps_only_explicit_targets(self):
        files = {"src/lib.rs": "", "tests/orphan.rs": "", "tests/named.rs": "", "examples/e.rs": ""}
        manifest = 'autotests = false\nautoexamples = false\n[[test]]\nname = "named"\n'
        self.assertStrict(self.strict(files, manifest), ["examples/e.rs", "tests/orphan.rs"])

    def test_diff_mode_reads_non_ascii_paths(self):
        write(self.root, {"src/café.rs": ""})
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")
        base = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        write(self.root, {"src/naïve.rs": ""})
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "orphan")
        self.assertEqual(run(self.root, "--mode", "diff", "--base", base), (1, ["src/naïve.rs"]))


    def test_inline_path_replaces_the_implicit_directory_of_a_module_file(self):
        files = {"src/lib.rs": "mod parent;\n", "src/parent.rs": '#[path = "alt"]\nmod inner { mod child; }\n',
                 "src/alt/child.rs": "", "src/parent/alt/child.rs": ""}
        self.assertStrict(self.strict(files), ["src/parent/alt/child.rs"])

    def test_conditional_path_on_inline_module_keeps_both_directories(self):
        files = {"src/lib.rs": '#[cfg_attr(test, path = "fixtures")]\nmod outer { mod child; }\n',
                 "src/fixtures/child.rs": "", "src/outer/child.rs": ""}
        self.assertStrict(self.strict(files), [])

    def test_autolib_false_drops_the_conventional_library(self):
        files = {"src/lib.rs": "mod m;\n", "src/m.rs": "", "src/main.rs": ""}
        self.assertStrict(self.strict(files, "autolib = false\n"), ["src/lib.rs", "src/m.rs"])

    def test_explicit_target_shadows_inferred_target_of_the_same_name(self):
        files = {"src/lib.rs": "", "tests/api.rs": "", "tests/other.rs": "", "qa/api.rs": ""}
        manifest = 'edition = "2021"\n[[test]]\nname = "api"\npath = "qa/api.rs"\n'
        self.assertStrict(self.strict(files, manifest), ["tests/api.rs"])
        files = {"src/lib.rs": "", "tests/api.rs": "", "tests/main.rs": ""}
        self.assertStrict(self.strict(files, 'edition = "2021"\n[[test]]\nname = "main"\npath = "tests/api.rs"\n'),
                          ["tests/main.rs"])

    def test_edition_2015_infers_a_kind_only_without_explicit_targets_of_it(self):
        files = {"src/lib.rs": "", "tests/named.rs": "", "tests/orphan.rs": "", "examples/e.rs": ""}
        manifest = '[[test]]\nname = "named"\n'
        self.assertStrict(self.strict(files, manifest), ["tests/orphan.rs"])
        self.assertStrict(self.strict(files, 'edition = "2021"\n' + manifest), [])

    def test_virtual_workspace_members_inherit_workspace_path_dependencies(self):
        files = {
            "Cargo.toml": '[workspace]\nmembers = ["app"]\n[workspace.dependencies]\n'
                          'helper = { path = "helper" }\nunused = { path = "fixtures/unused" }\n',
            "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n[dependencies]\nhelper.workspace = true\n',
            "app/src/lib.rs": "",
            "helper/Cargo.toml": '[package]\nname = "helper"\nversion = "0.1.0"\n',
            "helper/src/lib.rs": "", "helper/src/orphan.rs": "",
            "fixtures/unused/Cargo.toml": '[package]\nname = "unused"\nversion = "0.1.0"\n',
            "fixtures/unused/src/lib.rs": "", "fixtures/unused/src/orphan.rs": "",
        }
        self.assertEqual(self.tree(files), (1, ["helper/src/orphan.rs"]))

    def test_symlink_pointing_outside_the_checkout_is_reported_by_its_repository_path(self):
        with tempfile.TemporaryDirectory() as outside:
            Path(outside, "x.rs").write_text("")
            self.assertEqual(self.tree({"Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\n', "src/lib.rs": ""}),
                             (0, []))
            (self.root / "src/local.rs").symlink_to(Path(outside, "x.rs"))
            self.assertEqual(run(self.root, "--mode", "strict"), (1, ["src/local.rs"]))
            self.assertEqual(run(self.root, "--mode", "strict", "--allow", "src/local.rs"), (0, []))

    def test_diff_mode_reports_a_changed_file_in_no_crate(self):
        self.tree({"Cargo.toml": '[workspace]\nmembers = ["crates/app"]\n',
                   "crates/app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
                   "crates/app/src/lib.rs": ""})
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")
        base = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        write(self.root, {"tools/orphan.rs": "", "tools/kept.rs": ""})
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "tools")
        self.assertEqual(run(self.root, "--mode", "diff", "--base", base), (1, ["tools/kept.rs", "tools/orphan.rs"]))
        self.assertEqual(run(self.root, "--mode", "diff", "--base", base, "--allow", "tools/**"), (0, []))


if __name__ == "__main__":
    unittest.main()
