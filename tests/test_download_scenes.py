"""CPU-only fixtures catch corrupt caches, unsafe ZIPs, and broken adapters."""
import hashlib
import http.server
import importlib
import importlib.util
import io
import inspect
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile
import zlib

REPO = Path(__file__).resolve().parents[1]


def module(name):
    assert importlib.util.find_spec(name) is not None, f"Missing implementation {name}"
    return importlib.import_module(name)


def archive(files):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as output:
        for name, content in files.items():
            output.writestr(name, content)
    return result.getvalue()


class SceneDownloadTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(dir=REPO / ".workspace")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = archive({"sample/scene.xml": '<scene version="0.5.0"><shape type="ply"><string name="filename" value="mesh.ply"/></shape><sensor><transform name="toWorld"/><film type="ldrfilm"><float name="gamma" value="2.2"/></film></sensor></scene>', "sample/mesh.ply": "mesh data"})
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(self.headers.get("Range"))
                offset = int(self.headers.get("Range", "bytes=0-")[6:-1])
                if owner.ignore_range:
                    offset = 0
                self.send_response(206 if offset else 200)
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(owner.payload)-1}/{len(owner.payload)}")
                self.send_header("Content-Length", str(len(owner.payload) - offset))
                self.end_headers()
                if owner.interrupt_once:
                    owner.interrupt_once = False
                    self.wfile.write(owner.payload[offset:offset + 23])
                else:
                    self.wfile.write(owner.payload[offset:])

            def log_message(self, *args):
                pass

        self.requests = []
        self.ignore_range = False
        self.interrupt_once = False
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.entry = {"id": "sample", "name": "Sample", "url": f"http://127.0.0.1:{server.server_port}/sample.zip", "sha256": hashlib.sha256(self.payload).hexdigest(), "expected_bytes": len(self.payload), "archive_prefix_removed": "sample/", "xml_files": ["scene.xml"]}
        (self.root / "scenes").mkdir()
        self.write_catalog()

    def write_catalog(self):
        (self.root / "scenes/catalog.lock.json").write_text(json.dumps({"schema": 1, "scenes": [self.entry]}))
        (self.root / "scenes/suite.json").write_text(json.dumps({"schema": 1, "full": ["sample"], "quick": ["sample"], "scenes": {"sample": {"expected_diagnostics": [], "expected_unsupported": {}}}}))

    def test_resume_then_reuse_verified_cache_and_relocate_offline(self):
        downloader = module("scripts.download_scenes")
        cache = self.root / ".workspace/downloads/mitsuba/archives"
        cache.mkdir(parents=True)
        (cache / "sample.zip.part").write_bytes(self.payload[:19])
        result = downloader.ensure_scenes(self.root)
        self.assertEqual(self.requests, ["bytes=19-"])
        scene = result["scenes"][0]
        xml = Path(scene["source"])
        parsed = ET.parse(xml)
        self.assertEqual(parsed.getroot().get("version"), "3.0.0")
        self.assertIsNotNone(parsed.find(".//transform[@name='to_world']"))
        self.assertEqual(parsed.find(".//film").get("type"), "hdrfilm")
        self.assertIsNone(parsed.find(".//float[@name='gamma']"))
        self.assertFalse(Path(parsed.find(".//string").get("value")).is_absolute())
        self.assertEqual(downloader.ensure_scenes(self.root, offline=True)["scenes"][0]["asset_fingerprint"], scene["asset_fingerprint"])
        ready = self.root / ".workspace/downloads/mitsuba/ready.json"
        self.assertNotIn(self.root.as_posix(), ready.read_text())
        moved = self.root / "moved"
        shutil.copytree(self.root / "scenes", moved / "scenes")
        shutil.copytree(self.root / ".workspace", moved / ".workspace")
        relocated = downloader.ensure_scenes(moved, offline=True)["scenes"][0]
        self.assertTrue(Path(relocated["source"]).is_relative_to(moved))
        self.assertEqual(relocated["asset_fingerprint"], scene["asset_fingerprint"])
        self.assertEqual(len(self.requests), 1)

    def test_cli_reports_scene_progress_and_ready_summary_without_file_dump(self):
        process = subprocess.run([sys.executable, str(REPO / "scripts/download_scenes.py"), "--root", str(self.root)], capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("[1/1] sample", process.stdout)
        self.assertIn("Prepared 1 scene", process.stdout)
        self.assertIn(str(self.root / ".workspace/downloads/mitsuba/ready.json"), process.stdout)
        self.assertNotIn("asset_fingerprint", process.stdout)
        self.assertLess(len(process.stdout), 1000)

    def test_cli_checksum_failure_has_clear_reason_and_nonzero_exit(self):
        self.entry["sha256"] = "0" * 64
        self.write_catalog()
        process = subprocess.run([sys.executable, str(REPO / "scripts/download_scenes.py"), "--root", str(self.root)], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("SHA256", process.stderr)
        self.assertIn("sample", process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        self.assertNotIn("Prepared 1 scene", process.stdout)

    def test_publication_rejects_outside_paths_before_removing_backup(self):
        downloader = module("scripts.download_scenes")
        self.assertIn("data_root", inspect.signature(downloader._publish_directory).parameters)
        data = self.root / "data"
        target = data / "prepared/sample"
        target.mkdir(parents=True)
        (target / "marker").write_text("target")
        backup = target.with_name("sample.old")
        backup.mkdir()
        (backup / "marker").write_text("backup")
        stage = self.root / "outside"
        stage.mkdir()
        (stage / "marker").write_text("stage")
        with self.assertRaisesRegex(ValueError, "outside|Unsafe|contain"):
            downloader._publish_directory(stage, target, data_root=data)
        self.assertEqual((backup / "marker").read_text(), "backup")
        self.assertEqual((target / "marker").read_text(), "target")
        self.assertEqual((stage / "marker").read_text(), "stage")
        inside_stage = data / "stage"
        inside_stage.mkdir()
        with self.assertRaisesRegex(ValueError, "outside|Unsafe|contain"):
            downloader._publish_directory(inside_stage, data / "../outside", data_root=data)
        self.assertEqual((stage / "marker").read_text(), "stage")

    def test_tampered_prepared_asset_is_rebuilt_from_verified_archive(self):
        downloader = module("scripts.download_scenes")
        first = downloader.ensure_scenes(self.root)["scenes"][0]
        asset = Path(first["directory"]) / "mesh.ply"
        asset.write_text("damaged")
        repaired = downloader.ensure_scenes(self.root, offline=True)["scenes"][0]
        self.assertEqual(asset.read_text(), "mesh data")
        self.assertEqual(first["asset_fingerprint"], repaired["asset_fingerprint"])

    def test_interrupted_http_download_resumes_without_losing_partial(self):
        downloader = module("scripts.download_scenes")
        self.interrupt_once = True
        scene = downloader.ensure_scenes(self.root)["scenes"][0]
        self.assertEqual(self.requests, [None, "bytes=23-"])
        self.assertTrue(Path(scene["source"]).is_file())

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        downloader = module("scripts.download_scenes")
        self.ignore_range = True
        cache = self.root / ".workspace/downloads/mitsuba/archives"
        cache.mkdir(parents=True)
        (cache / "sample.zip.part").write_bytes(self.payload[:19])
        downloader.ensure_scenes(self.root)
        self.assertEqual((cache / "sample.zip").read_bytes(), self.payload)

    def test_corrupt_cached_zip_is_rejected_offline_then_recovered_online(self):
        downloader = module("scripts.download_scenes")
        downloader.ensure_scenes(self.root)
        cached = self.root / ".workspace/downloads/mitsuba/archives/sample.zip"
        cached.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "size|checksum|SHA256"):
            downloader.ensure_scenes(self.root, offline=True)
        downloader.ensure_scenes(self.root)
        self.assertEqual(cached.read_bytes(), self.payload)

    def test_wrong_archive_hash_never_publishes_ready(self):
        downloader = module("scripts.download_scenes")
        self.entry["sha256"] = "0" * 64
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "SHA256|checksum"):
            downloader.ensure_scenes(self.root)
        self.assertFalse((self.root / ".workspace/downloads/mitsuba/ready.json").exists())

    def test_missing_asset_never_publishes_ready(self):
        downloader = module("scripts.download_scenes")
        self.payload = archive({"sample/scene.xml": '<scene><shape><string name="filename" value="missing.ply"/></shape></scene>'})
        self.entry.update(sha256=hashlib.sha256(self.payload).hexdigest(), expected_bytes=len(self.payload))
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "missing|Missing"):
            downloader.ensure_scenes(self.root)
        self.assertFalse((self.root / ".workspace/downloads/mitsuba/ready.json").exists())

    def test_invalid_zip_with_matching_hash_is_rejected(self):
        downloader = module("scripts.download_scenes")
        self.payload = b"this is not a zip"
        self.entry.update(sha256=hashlib.sha256(self.payload).hexdigest(), expected_bytes=len(self.payload))
        self.write_catalog()
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            downloader.ensure_scenes(self.root)

    def test_archive_cannot_escape_scene_directory(self):
        downloader = module("scripts.download_scenes")
        for evil in ["sample/../../escaped", "C:/escaped", "sample/evil:stream", "sample/../escaped"]:
            with self.subTest(evil=evil):
                self.payload = archive({evil: "escape", "sample/scene.xml": "<scene/>"})
                self.entry.update(sha256=hashlib.sha256(self.payload).hexdigest(), expected_bytes=len(self.payload))
                self.write_catalog()
                with self.assertRaisesRegex(ValueError, "Unsafe|unsafe|prefix"):
                    downloader.ensure_scenes(self.root)
        self.assertFalse((self.root / "escaped").exists())

    def test_prefix_stripping_cannot_create_an_absolute_path(self):
        downloader = module("scripts.download_scenes")
        # A directory entry tests the alias without writing an outside file.
        self.payload = archive({"sample//": b"", "sample/scene.xml": "<scene/>"})
        self.entry.update(sha256=hashlib.sha256(self.payload).hexdigest(), expected_bytes=len(self.payload))
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "Unsafe|unsafe"):
            downloader.ensure_scenes(self.root)

    def test_zip_symlink_is_rejected_before_extraction(self):
        downloader = module("scripts.download_scenes")
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w") as output:
            link = zipfile.ZipInfo("sample/asset")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            output.writestr(link, "../../outside")
            output.writestr("sample/scene.xml", "<scene/>")
        self.payload = result.getvalue()
        self.entry.update(sha256=hashlib.sha256(self.payload).hexdigest(), expected_bytes=len(self.payload))
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "Unsafe|unsafe"):
            downloader.ensure_scenes(self.root)


class SerializedPreparationTests(unittest.TestCase):
    def test_serialized_mesh_preserves_vertices_normals_uvs_and_faces(self):
        prep = module("scripts.prepare_scenes")
        import tempfile
        with tempfile.TemporaryDirectory(dir=REPO / ".workspace") as directory:
            path = Path(directory)
            # Three positions, normals and UVs in Mitsuba's planar attribute layout.
            values = (0., 0., 0., 1., 0., 0., 0., 1., 0., *(0., 0., 1.) * 3, 0., 0., 1., 0., 0., 1.)
            payload = struct.pack("<I", 0x1003) + b"triangle\0" + struct.pack("<QQ", 3, 1) + struct.pack("<24f", *values) + struct.pack("<III", 0, 1, 2)
            source = path / "mesh.serialized"
            source.write_bytes(struct.pack("<HH", 0x041c, 4) + zlib.compress(payload) + struct.pack("<QI", 0, 1))
            (path / "scene.xml").write_text('<scene version="3.0.0"><shape type="serialized"><string name="filename" value="mesh.serialized"/><integer name="shape_index" value="0"/></shape></scene>')
            prepared = prep.prepare_scene(path, {"xml_files": ["scene.xml"]})
            tree = ET.parse(prepared)
            self.assertEqual(tree.find("shape").get("type"), "ply")
            self.assertIsNone(tree.find(".//integer"))
            ply = (path / tree.find(".//string").get("value")).read_bytes()
            header, data = ply.split(b"end_header\n", 1)
            self.assertIn(b"element vertex 3", header)
            self.assertIn(b"property float nx", header)
            self.assertEqual(struct.unpack_from("<8f", data), (0., 0., 0., 0., 0., 1., 0., 0.))
            self.assertEqual(struct.unpack_from("<8f", data, 32), (1., 0., 0., 0., 0., 1., 1., 0.))
            self.assertEqual(struct.unpack_from("<BIII", data, 96), (3, 0, 1, 2))


if __name__ == "__main__":
    unittest.main()
