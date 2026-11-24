"""Tests for the nightly-flatpak action's manifest and metainfo rewrite."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "actions" / "nightly-flatpak"
FIXTURES = ROOT / "tests" / "fixtures" / "nightly-flatpak"


def load_module():
    spec = importlib.util.spec_from_file_location("nightly_flatpak", ACTION / "nightly_flatpak.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nf = load_module()


class ManifestRewriteTests(unittest.TestCase):
    def rewrite(self, name):
        text, info = nf.rewrite_manifest((FIXTURES / name).read_text())
        return yaml.safe_load(text), info

    def test_app_manifests_install_the_stage_files_under_the_nightly_id(self):
        # photocraft: MIME file, cups-client module; artcraft-launcher: no MIME file;
        # MaterialCraft: the manifest's file name differs from its lowercase ID.
        cases = (
            ("ai.storyteller.photocraft.bundle.yml", "ai.storyteller.photocraft", "photocraft", True),
            ("ai.storyteller.artcraft-launcher.bundle.yml", "ai.storyteller.artcraft-launcher",
             "artcraft-launcher", False),
            ("ai.storyteller.MaterialCraft.bundle.yml", "ai.storyteller.materialcraft", "materialcraft", True),
        )
        for name, stable, command, has_mime in cases:
            with self.subTest(manifest=name):
                original = yaml.safe_load((FIXTURES / name).read_text())
                manifest, info = self.rewrite(name)
                nightly = f"{stable}.nightly"
                self.assertEqual(info, {"stable-id": stable, "app-id": nightly, "command": command})
                self.assertEqual(manifest["id"], nightly)
                self.assertEqual(manifest["desktop-file-name-suffix"], " (Nightly)")
                self.assertFalse([key for key in manifest if key.startswith("rename-")])
                app = manifest["modules"][-1]["build-commands"]
                expected = [
                    f"install -Dm644 share/applications/{stable}.desktop /app/share/applications/{nightly}.desktop",
                    f"install -Dm644 share/metainfo/{stable}.metainfo.xml /app/share/metainfo/{nightly}.metainfo.xml",
                ]
                if has_mime:
                    expected.append(
                        f"install -Dm644 share/mime/packages/{stable}.xml /app/share/mime/packages/{nightly}.xml")
                self.assertEqual(sorted(c for c in app if "/app/share/" in c and "icons" not in c), sorted(expected))
                # Everything else, the binaries' names and the stage source included, is unchanged.
                original_app = original["modules"][-1]["build-commands"]
                self.assertEqual([c.replace(nightly, stable) for c in app], original_app)
                self.assertEqual({k: v for k, v in manifest.items() if k not in ("id", "desktop-file-name-suffix", "modules")},
                                 {k: v for k, v in original.items() if k not in ("id", "modules")})
                self.assertEqual(manifest["modules"][:-1], original["modules"][:-1])

    def test_refuses_manifests_it_cannot_rewrite(self):
        base = (FIXTURES / "ai.storyteller.artcraft-launcher.bundle.yml").read_text()
        cases = {
            "two ids": base + "app-id: ai.storyteller.other\n",
            "already nightly": base.replace("id: ai.storyteller.artcraft-launcher\n",
                                            "id: ai.storyteller.artcraft-launcher.nightly\n"),
            "rename key set": base + "rename-icon: something\n",
            "no metainfo": base.replace("share/metainfo/", "share/appdata/"),
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(nf.PrepareError):
                nf.rewrite_manifest(text)


class MetainfoRewriteTests(unittest.TestCase):
    def test_component_id_launchable_icon_and_name(self):
        text = (FIXTURES / "ai.storyteller.photocraft.metainfo.xml").read_text()
        out = nf.rewrite_metainfo(text, "ai.storyteller.photocraft", "ai.storyteller.photocraft.nightly")
        root = ET.fromstring(out)
        self.assertEqual(root.findtext("id"), "ai.storyteller.photocraft.nightly")
        self.assertEqual(root.find("launchable[@type='desktop-id']").text, "ai.storyteller.photocraft.nightly.desktop")
        self.assertEqual(root.find("icon[@type='stock']").text, "ai.storyteller.photocraft.nightly")
        self.assertEqual([n.text for n in root.findall("name")], ["PhotoCraft (Nightly)"] * 2)
        self.assertEqual(root.findtext("developer/name"), "Learning Machines LLC")
        # flatpak-builder adds <provides><id>OLD</id> when it rewrites a differing <id> itself;
        # with the ID already rewritten the stable ID is never claimed.
        self.assertIsNone(root.find("provides/id"))
        self.assertEqual(root.findtext("provides/binary"), "photocraft")
        self.assertEqual(root.findall("provides/mediatype"), [])

    def test_refuses_a_metainfo_for_another_app(self):
        text = (FIXTURES / "ai.storyteller.photocraft.metainfo.xml").read_text()
        with self.assertRaises(nf.PrepareError):
            nf.rewrite_metainfo(text, "ai.storyteller.wordcraft", "ai.storyteller.wordcraft.nightly")


class PrepareAndOciTests(unittest.TestCase):
    def test_prepare_rewrites_the_stage_and_writes_the_manifest_beside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            stage = work / "stage/share"
            metainfo = stage / "metainfo/ai.storyteller.photocraft.metainfo.xml"
            metainfo.parent.mkdir(parents=True)
            metainfo.write_text((FIXTURES / "ai.storyteller.photocraft.metainfo.xml").read_text())
            desktop = stage / "applications/ai.storyteller.photocraft.desktop"
            desktop.parent.mkdir(parents=True)
            desktop.write_text("[Desktop Entry]\nName=PhotoCraft\nIcon=ai.storyteller.photocraft\n"
                               "Icon[de]=ai.storyteller.photocraft\nMimeType=image/png;\nMimeType[de]=image/png;\n"
                               "StartupWMClass=ai.storyteller.photocraft\n")
            mime = stage / "mime/packages/ai.storyteller.photocraft.xml"
            mime.parent.mkdir(parents=True)
            mime.write_text('<mime-info><mime-type type="application/x-photocraft">'
                            '<icon name="ai.storyteller.photocraft"/>'
                            "<generic-icon name='ai.storyteller.photocraft'/>"
                            '<icon name="other"/></mime-type></mime-info>')
            icons = ["hicolor/16x16/apps/ai.storyteller.photocraft.png",
                     "hicolor/scalable/apps/ai.storyteller.photocraft.svg",
                     "hicolor/symbolic/apps/ai.storyteller.photocraft-symbolic.svg",
                     "hicolor/scalable/mimetypes/application-x-photocraft.svg"]
            for icon in icons:
                (stage / "icons" / icon).parent.mkdir(parents=True, exist_ok=True)
                (stage / "icons" / icon).write_text("icon")
            res = subprocess.run(
                ["python3", str(ACTION / "nightly_flatpak.py"), "prepare",
                 str(FIXTURES / "ai.storyteller.photocraft.bundle.yml"), str(work)],
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            manifest = work / "ai.storyteller.photocraft.nightly.yml"
            self.assertEqual(res.stdout.splitlines(), [
                "stable-id=ai.storyteller.photocraft",
                "app-id=ai.storyteller.photocraft.nightly",
                "command=photocraft",
                f"manifest={manifest}",
            ])
            self.assertEqual(yaml.safe_load(manifest.read_text())["id"], "ai.storyteller.photocraft.nightly")
            self.assertEqual(ET.fromstring(metainfo.read_text()).findtext("id"), "ai.storyteller.photocraft.nightly")
            self.assertEqual(desktop.read_text(), "[Desktop Entry]\nName=PhotoCraft\n"
                             "Icon=ai.storyteller.photocraft.nightly\nIcon[de]=ai.storyteller.photocraft.nightly\n"
                             "StartupWMClass=ai.storyteller.photocraft\n")
            self.assertEqual(mime.read_text(), '<mime-info><mime-type type="application/x-photocraft">'
                             '<icon name="ai.storyteller.photocraft.nightly"/>'
                             "<generic-icon name='ai.storyteller.photocraft.nightly'/>"
                             '<icon name="other"/></mime-type></mime-info>')
            self.assertEqual(sorted(str(p.relative_to(stage / "icons")) for p in (stage / "icons").rglob("*.*")), [
                "hicolor/16x16/apps/ai.storyteller.photocraft.nightly.png",
                "hicolor/scalable/apps/ai.storyteller.photocraft.nightly.svg",
                "hicolor/scalable/mimetypes/application-x-photocraft.svg",
                "hicolor/symbolic/apps/ai.storyteller.photocraft.nightly-symbolic.svg",
            ])

    def test_check_oci_reads_the_flatpak_ref_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = Path(tmp)
            blobs = layout / "blobs/sha256"
            blobs.mkdir(parents=True)
            ref = "app/ai.storyteller.photocraft.nightly/x86_64/stable"
            (blobs / "c0").write_text(json.dumps({"config": {"Labels": {"org.flatpak.ref": ref}}}))
            (blobs / "m0").write_text(json.dumps({"config": {"digest": "sha256:c0"}}))
            (layout / "index.json").write_text(json.dumps({"manifests": [{"digest": "sha256:m0"}]}))
            script = str(ACTION / "nightly_flatpak.py")
            ok = subprocess.run(["python3", script, "check-oci", str(layout), ref], capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            stable = ref.replace(".nightly", "")
            bad = subprocess.run(["python3", script, "check-oci", str(layout), stable], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)

    def make_layout(self, root):
        blobs = root / "blobs/sha256"
        blobs.mkdir(parents=True)
        manifest = b'{"config": {"digest": "sha256:c0"},\n "layers": []}\n'
        digest = hashlib.sha256(manifest).hexdigest()
        (blobs / digest).write_bytes(manifest)
        (blobs / "other").write_bytes(b"x")
        (root / "index.json").write_text(json.dumps({"manifests": [{"digest": f"sha256:{digest}"}]}))
        (root / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}')
        return manifest

    def test_oci_manifest_is_the_indexed_blob_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = Path(tmp) / "layout"
            layout.mkdir()
            manifest = self.make_layout(layout)
            out = Path(tmp) / "m.json"
            self.assertEqual(nf.main(["oci-manifest", str(layout), str(out)]), 0)
            self.assertEqual(out.read_bytes(), manifest)

    def test_oci_archive_is_deterministic(self):
        import os
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            layout = Path(tmp) / "layout"
            layout.mkdir()
            self.make_layout(layout)
            first, second = Path(tmp) / "a.tar", Path(tmp) / "b.tar"
            self.assertEqual(nf.main(["oci-archive", str(layout), str(first)]), 0)
            for path in layout.rglob("*"):
                os.utime(path, (12345, 12345))
            self.assertEqual(nf.main(["oci-archive", str(layout), str(second)]), 0)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(first) as tar:
                members = tar.getmembers()
            names = [m.name for m in members]
            self.assertEqual(names, sorted(names))
            self.assertEqual({(m.mtime, m.uid, m.gid, m.uname) for m in members}, {(0, 0, 0, "")})
            self.assertIn("index.json", names)


if __name__ == "__main__":
    unittest.main()
