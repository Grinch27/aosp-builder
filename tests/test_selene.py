#!/usr/bin/env python3
# Requirements: synthetic public fixtures only; no private archive or build downloads.
# Ambiguity: these tests do not prove the real ROM builds or works on a phone.
# Research: keep source, output, process and credential boundaries independently tested.
# Optimization: temporary tiny Git repositories and ZIPs; standard library only.
# Risks: secret leakage, traversal, stale outputs, truncated images and fake success.
# Validation: rejection cases, real failure propagation and CMS decrypt round trip.
"""Boundary and product checks for the native selene pipeline."""

import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import warnings
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import selene


def blob(data):
    return {"size": len(data), "blob": hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()}


def manifest(path="project", revision="a" * 40, directive=""):
    return (f'<manifest><remote name="github" fetch="https://github.com"/>'
            f'<default remote="github"/><project name="public/fixture" path="{path}" '
            f'revision="{revision}">{directive}</project></manifest>').encode()


class SourceTests(unittest.TestCase):
    def test_lock_contract(self):
        self.assertEqual(selene.load_lock()["source"]["projects"], 1257)

    def test_blob_identity_rejects_same_size_changes(self):
        selene.check_blob(b"one", blob(b"one"))
        with self.assertRaises(selene.BuildError):
            selene.check_blob(b"two", blob(b"one"))

    def test_manifest_unlocked_and_path_inputs(self):
        for path in ("../escape", "/absolute", "a//b", "a/./b", "a/.git/config", "a\\b", "-option"):
            with self.subTest(path=path), self.assertRaises(selene.BuildError):
                selene.parse_manifest(manifest(path=path), {"projects": 1, "directives": 0})
        with self.assertRaises(selene.BuildError):
            selene.parse_manifest(manifest(revision="main"), {"projects": 1, "directives": 0})

    def test_manifest_remote_and_unknown_directive(self):
        for data in (manifest().replace(b"https://github.com", b"https://example.invalid"),
                     manifest(directive='<annotation name="unreviewed"/>'),
                     b'<!DOCTYPE manifest [<!ENTITY x "bad">]>' + manifest()):
            with self.assertRaises(selene.BuildError):
                selene.parse_manifest(data, {"projects": 1, "directives": 0})

    def test_duplicate_project_and_directive_counts(self):
        data = manifest(directive='<linkfile src="file" dest="link"/>')
        self.assertEqual(len(selene.parse_manifest(data, {"projects": 1, "directives": 1})), 1)
        with self.assertRaises(selene.BuildError):
            selene.parse_manifest(data, {"projects": 1, "directives": 0})
        root = ET.fromstring(data)
        root.append(copy.deepcopy(root.find("project")))
        with self.assertRaises(selene.BuildError):
            selene.parse_manifest(ET.tostring(root), {"projects": 2, "directives": 2})

    def test_redirect_rejected(self):
        with self.assertRaises(selene.BuildError):
            selene.NoRedirect().redirect_request(None, None, 302, None, None, "https://example.invalid")

    def test_api_identity_and_request_scope(self):
        data = manifest()
        lock = {"source": {"repository": "Grinch27/lineageos_selene", "commit": "b" * 40,
                           "projects": 1, "directives": 0, **blob(data)}}
        record = {"type": "file", "encoding": "base64", "content": base64.b64encode(data).decode(),
                  "sha": blob(data)["blob"], "size": len(data)}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(record).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(selene.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(selene.fetch_manifest(lock, "private-test-token"), data)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.headers["Authorization"], "Bearer private-test-token")
            self.assertEqual(request.full_url, "https://api.github.com/repos/Grinch27/lineageos_selene/contents/default.xml?ref=" + "b" * 40)
            record["sha"] = "0" * 40
            response.__enter__.return_value.read.return_value = json.dumps(record).encode()
            with self.assertRaises(selene.BuildError):
                selene.fetch_manifest(lock, "private-test-token")

    def test_child_environment_removes_credentials_and_overrides(self):
        env = selene.clean_environment({"HOME": "/unchanged/home", "PATH": "/bin", "GH_TOKEN": "secret",
                                       "LINEAGE_SOURCE_TOKEN": "secret", "SELENE_DELIVERY_CERT": "pem",
                                       "ACTIONS_RUNTIME_TOKEN": "secret", "PYTHONPATH": "/bad",
                                       "GIT_CONFIG_VALUE_9": "secret", "https_proxy": "secret"})
        self.assertEqual(env["HOME"], "/unchanged/home")
        for key in ("GH_TOKEN", "LINEAGE_SOURCE_TOKEN", "SELENE_DELIVERY_CERT", "ACTIONS_RUNTIME_TOKEN",
                    "PYTHONPATH", "GIT_CONFIG_VALUE_9", "https_proxy"):
            self.assertNotIn(key, env)


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)
        self.project = self.source / "project"
        self.project.mkdir()
        self.git("init", "--initial-branch=main", "--quiet")
        (self.project / "file").write_text("fixture\n")
        self.git("add", "file")
        self.git("commit", "-qm", "fixture")
        sha = self.git("rev-parse", "HEAD").strip()
        data = manifest(revision=sha, directive='<linkfile src="file" dest="link"/>')
        self.graph = selene.parse_manifest(data, {"projects": 1, "directives": 1})
        (self.source / "link").symlink_to("project/file")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.project), *args], text=True,
                                       stderr=subprocess.DEVNULL, env=selene.clean_environment(os.environ))

    def test_exact_clean_checkout_and_links(self):
        selene.verify_checkout(self.graph, self.source)

    def test_dirty_checkout(self):
        (self.project / "file").write_text("changed")
        with self.assertRaises(selene.BuildError):
            selene.verify_checkout(self.graph, self.source)

    def test_untracked_checkout(self):
        (self.project / "untracked").touch()
        with self.assertRaises(selene.BuildError):
            selene.verify_checkout(self.graph, self.source)

    def test_wrong_revision(self):
        self.graph[0].set("revision", "a" * 40)
        with self.assertRaises(selene.BuildError):
            selene.verify_checkout(self.graph, self.source)

    def test_wrong_link_target(self):
        (self.source / "link").unlink()
        (self.source / "link").symlink_to("project")
        with self.assertRaises(selene.BuildError):
            selene.verify_checkout(self.graph, self.source)

    def test_parser_missing_project(self):
        parsed = self.source / "parsed.xml"
        parsed.write_text("<manifest/>")
        with self.assertRaises(selene.BuildError):
            selene.verify_parser(self.graph, parsed)

    def test_nested_gitlink_not_first_entry(self):
        sha = self.git("rev-parse", "HEAD").strip()
        self.git("update-index", "--add", "--cacheinfo", "160000," + sha + ",z-submodule")
        self.git("commit", "-qm", "synthetic nested dependency")
        self.graph[0].set("revision", self.git("rev-parse", "HEAD").strip())
        with self.assertRaises(selene.BuildError):
            selene.verify_checkout(self.graph, self.source)


def fixture_products(root):
    product = root / "out/target/product/selene"
    product.mkdir(parents=True)
    image = bytearray(6144)
    image[:8] = b"ANDROID!"
    for offset, value in ((8, 2048), (16, 2048), (36, 2048), (40, 2), (1644, 1660)):
        struct.pack_into("<I", image, offset, value)
    image[2048:2054], image[4096:4103] = b"kernel", b"ramdisk"
    (product / "boot.img").write_bytes(image)
    payload = b"CrAU" + struct.pack(">QQI", 2, 8, 0) + b"manifest"
    payload_hash = base64.b64encode(hashlib.sha256(payload).digest()).decode()
    with zipfile.ZipFile(product / "lineage-20.0-fixture-selene.zip", "w") as ota:
        ota.writestr("META-INF/com/android/metadata", "ota-type=AB\npre-device=selene\n")
        ota.writestr("payload.bin", payload)
        ota.writestr("payload_properties.txt", f"FILE_SIZE=32\nMETADATA_SIZE=32\nFILE_HASH={payload_hash}\nMETADATA_HASH={payload_hash}\n")
    folder = product / "obj/PACKAGING/target_files_intermediates"
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / "fixture-target_files.zip", "w") as target:
        target.writestr("META/misc_info.txt", "ab_update=true\nuse_dynamic_partitions=true\nvirtual_ab=true\n")
        target.writestr("META/ab_partitions.txt", "system\nvendor\n")
        target.writestr("META/dynamic_partitions_info.txt", "super_partition_size=9126805504\n")
        target.writestr("IMAGES/boot.img", image)
    return product


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.product = fixture_products(self.root)

    def test_complete_products(self):
        files, report = selene.products(self.root / "out")
        self.assertEqual(set(files), {"boot.img", "rom.zip", "target-files.zip"})
        self.assertEqual(report["flash_readiness"], "NOT_ACHIEVED")

    def test_truncated_boot(self):
        (self.product / "boot.img").write_bytes(b"ANDROID!" + b"\0" * 100)
        with self.assertRaises(selene.BuildError):
            selene.products(self.root / "out")

    def test_target_files_boot_mismatch(self):
        image = bytearray((self.product / "boot.img").read_bytes())
        image[-1] = 1
        (self.product / "boot.img").write_bytes(image)
        with self.assertRaises(selene.BuildError):
            selene.products(self.root / "out")

    def test_missing_ota_is_not_success(self):
        next(self.product.glob("lineage-*.zip")).unlink()
        with self.assertRaises(selene.BuildError):
            selene.products(self.root / "out")

    def rewrite_ota(self, name, content):
        path = next(self.product.glob("lineage-*.zip"))
        with zipfile.ZipFile(path) as archive:
            entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        entries[name] = content
        with zipfile.ZipFile(path, "w") as archive:
            for key, value in entries.items():
                archive.writestr(key, value)

    def test_wrong_ota_device(self):
        self.rewrite_ota("META-INF/com/android/metadata", "ota-type=AB\npre-device=other\n")
        with self.assertRaises(selene.BuildError):
            selene.products(self.root / "out")

    def test_payload_hash_mismatch(self):
        self.rewrite_ota("payload_properties.txt", "FILE_SIZE=32\nFILE_HASH=wrong\n")
        with self.assertRaises(selene.BuildError):
            selene.products(self.root / "out")

    def test_truncated_payload_manifest(self):
        self.rewrite_ota("payload.bin", b"CrAU" + struct.pack(">QQI", 2, 999, 0) + b"short")
        with self.assertRaises(selene.BuildError):
            selene.products(self.root / "out")

    def test_output_symlink_rejected(self):
        rom = next(self.product.glob("lineage-*.zip"))
        external = self.root / "outside.zip"
        rom.rename(external)
        rom.symlink_to(external)
        with self.assertRaises(selene.BuildError):
            selene.products(self.root / "out")

    def test_zip_path_normalization_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("valid", "one")
            archive.writestr("./normalized", "two")
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive, self.assertRaises(selene.BuildError):
            selene.zip_check(archive)

    def test_duplicate_zip_member(self):
        buffer = io.BytesIO()
        with warnings.catch_warnings(), zipfile.ZipFile(buffer, "w") as archive:
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("same", "one")
            archive.writestr("same", "two")
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive, self.assertRaises(selene.BuildError):
            selene.zip_check(archive)

    @unittest.skipUnless(shutil.which("openssl"), "openssl required for CMS round trip")
    def test_encrypted_delivery_round_trip_and_allowlist(self):
        (self.root / "inputs").mkdir()
        (self.root / "logs").mkdir()
        key = self.root / "recipient.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=synthetic-test", "-keyout", str(key),
                        "-out", str(self.root / "inputs/recipient.pem")], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        files, report = selene.products(self.root / "out")
        selene.seal(files, report, self.root)
        decrypted = subprocess.check_output(["openssl", "cms", "-decrypt", "-binary", "-inform", "DER",
                                             "-in", str(self.root / "delivery/selene-output.cms"),
                                             "-recip", str(self.root / "inputs/recipient.pem"), "-inkey", str(key)])
        with tarfile.open(fileobj=io.BytesIO(decrypted)) as archive:
            self.assertEqual(set(archive.getnames()), {"boot.img", "rom.zip", "target-files.zip",
                                                       "static-validation.json", "SHA256SUMS"})
            self.assertEqual(archive.extractfile("boot.img").read(), files["boot.img"].read_bytes())


class RuntimeTests(unittest.TestCase):
    def test_real_exit_and_private_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "private.log"
            with self.assertRaises(selene.BuildError) as caught:
                selene.command([sys.executable, "-c", "print('synthetic-private-text');raise SystemExit(7)"],
                               tmp, selene.clean_environment(os.environ), log, 10)
            self.assertEqual(caught.exception.code, 7)
            self.assertNotIn("synthetic-private-text", str(caught.exception))
            self.assertIn("synthetic-private-text", log.read_text())

    def test_timeout_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(selene.BuildError) as caught:
                selene.command([sys.executable, "-c", "import time;time.sleep(30)"], tmp,
                               selene.clean_environment(os.environ), Path(tmp) / "timeout.log", 0.1)
            self.assertEqual(caught.exception.code, 124)

    def test_resource_preflight_rejects_before_creating_work(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "github-hosted"}):
            work = Path(tmp) / "new-work"
            with mock.patch.object(selene.shutil, "disk_usage", return_value=mock.Mock(free=1024)):
                with self.assertRaises(selene.BuildError):
                    selene.preflight(selene.load_lock(), work)
            self.assertFalse(work.exists())

    def test_existing_work_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(selene.BuildError):
                selene.preflight(selene.load_lock(), Path(tmp))


if __name__ == "__main__":
    unittest.main()
