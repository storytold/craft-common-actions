"""Platform plug-in registry: secret isolation, trust shape and registry binding. Synthetic only."""
import json
from pathlib import Path
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"scripts"))
import nightly_release_platforms as plat


class PlatformPluginTests(unittest.TestCase):
    def test_live_registry_admits_exactly_reviewed_plugin_rows(self):
        registry = json.loads((ROOT/"nightly-apps.json").read_text())
        platforms = plat.registry_platforms(registry)
        self.assertIn("windows-x86_64", platforms); self.assertIn("macos-aarch64", platforms)
        self.assertEqual(platforms, sorted(platforms))

    def test_keyless_strips_every_platform_secret_and_signing_gets_only_its_own(self):
        env = {name: "x" for name in plat.SECRET_NAMES}; env.update(PATH="/usr/bin", RUNNER_TEMP="/r")
        self.assertEqual(plat.keyless(env), {"PATH": "/usr/bin", "RUNNER_TEMP": "/r"})
        apple = plat.credentialed(env, plat.signing.APPLE_SECRETS)
        self.assertEqual(set(apple) - {"PATH", "RUNNER_TEMP"}, set(plat.signing.APPLE_SECRETS))
        for name in ("WINDOWS_CERTIFICATE", "AZURE_CLIENT_SECRET", "LINUX_SIGNING_KEY", "GH_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
            self.assertNotIn(name, apple)

    def test_trust_values_are_exact_and_single(self):
        self.assertEqual(plat.expected_signer("macos-aarch64", {"apple_team_id": "ABCDE12345"}), "ABCDE12345")
        self.assertEqual(plat.expected_signer("windows-x86_64", {"publisher_subject": "CN=x"}), "CN=x")
        for platform, trust in (("macos-aarch64", {"apple_team_id": "abcde12345"}), ("macos-aarch64", {"apple_team_id": "ABCDE1234"}),
                                ("macos-aarch64", {"apple_team_id": "ABCDE12345", "extra": "x"}), ("macos-aarch64", {}),
                                ("windows-x86_64", {"publisher_subject": ""}), ("windows-x86_64", {"publisher_subject": "CN=a\nb"}),
                                ("windows-x86_64", {"apple_team_id": "ABCDE12345"}), ("freebsd-x86_64", {"x": "y"})):
            with self.subTest(platform=platform, trust=trust), self.assertRaises(ValueError):
                plat.expected_signer(platform, trust)

    def test_asset_names_are_exact_and_distinct(self):
        version = "1.2.3-nightly.20261123.100.1.gaaaaaaaaaaaa"
        mac = plat.MACOS.assets(version)
        self.assertEqual(mac, {"asset": "photocraft-"+version+"-macos-aarch64-nightly.zip",
            "unsigned_asset": "photocraft-"+version+"-macos-aarch64-nightly.unsigned.zip",
            "built_asset": "photocraft-"+version+"-macos-aarch64-nightly.zip"})
        for bad in ("1.2.3", "../1.2.3-nightly.20261123.100.1.gaaaaaaaaaaaa", "1.2.3-nightly.20261123.0.1.gaaaaaaaaaaaa"):
            with self.subTest(bad=bad), self.assertRaises(ValueError): plat.MACOS.assets(bad)
        self.assertGreaterEqual(plat.MAC_SIGN_TIMEOUT, 5400)


if __name__ == "__main__": unittest.main()
