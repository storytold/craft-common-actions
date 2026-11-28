"""The FreeBSD release guest installs the same base packages as the FreeBSD CI guest."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def base_packages(workflow):
    text = (ROOT / ".github/workflows" / workflow).read_text()
    lines = [l for l in text.splitlines() if "pkg install -y bash " in l]
    assert len(lines) == 1, (workflow, lines)
    return lines[0].split("pkg install -y", 1)[1].split()


class FreebsdReleasePackageTests(unittest.TestCase):
    def test_cmake_is_the_split_cmake_core_port(self):
        # FreeBSD split the cmake port; plain `cmake` is no longer installable on every repo snapshot.
        packages = base_packages("release.yml")
        self.assertIn("cmake-core", packages)
        self.assertNotIn("cmake", packages)

    def test_release_guest_packages_are_all_installable_in_ci(self):
        # CI may add test-only packages (fonts); every release package must also be one CI installs.
        ci = set(base_packages("freebsd.yml"))
        self.assertEqual([p for p in base_packages("release.yml") if p not in ci], [])


if __name__ == "__main__":
    unittest.main()
