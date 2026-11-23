"""Deterministic Mitsuba adapters using only files from the downloaded archive.

Legacy property renames follow the Mitsuba 3 migration rules. Serialized meshes
use Mitsuba's v3/v4 little-endian format; binary PLY retains stored precision,
normals, UVs, colors, and triangle indices without geometry approximation.
"""
from pathlib import Path, PurePosixPath
import math
import re
import struct
import xml.etree.ElementTree as ET
import zlib

PREPARATION_VERSION = 1


def relative_asset(directory: Path, value: str) -> Path:
    """Resolve an archive-local asset, rejecting absolute paths and traversal."""
    name = value.replace("\\", "/")
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or ".." in relative.parts or ":" in name:
        raise ValueError(f"Unsafe asset reference: {value!r}")
    result = directory.joinpath(*relative.parts)
    if not result.is_file() or result.is_symlink():
        raise ValueError(f"Missing referenced asset: {value}")
    return result


def _snake(name: str) -> str:
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def serialized_to_ply(source: Path, index: int, destination: Path) -> None:
    """Repack one serialized shape, keeping every attribute's original bits."""
    data = source.read_bytes()
    if len(data) < 12:
        raise ValueError(f"Truncated serialized mesh: {source.name}")
    magic, version = struct.unpack_from("<HH", data)
    if magic != 0x041C or version not in (3, 4):
        raise ValueError(f"Unsupported serialized mesh header: {source.name}")
    count = struct.unpack_from("<I", data, len(data) - 4)[0]
    width = 8 if version == 4 else 4
    table = len(data) - 4 - count * width
    if not 0 <= index < count or table < 4:
        raise ValueError(f"Invalid serialized shape index {index}")
    offsets = struct.unpack_from("<" + ("Q" if version == 4 else "I") * count, data, table)
    offset = offsets[index]
    end = offsets[index + 1] if index + 1 < count else table
    if offset < 0 or offset + 4 >= end or end > table or struct.unpack_from("<HH", data, offset) != (magic, version):
        raise ValueError("Invalid serialized shape offsets")
    payload = zlib.decompress(data[offset + 4:end])
    flags = struct.unpack_from("<I", payload)[0]
    pos = payload.index(b"\0", 4) + 1 if version == 4 else 4
    vertices, triangles = struct.unpack_from("<QQ", payload, pos)
    pos += 16
    scalar_width = 8 if flags & 0x2000 else 4
    scalar_type = "double" if scalar_width == 8 else "float"
    fields = []
    blocks = []
    for names, enabled in [(("x", "y", "z"), True), (("nx", "ny", "nz"), flags & 1), (("s", "t"), flags & 2), (("red", "green", "blue"), flags & 8)]:
        if not enabled:
            continue
        size = vertices * len(names) * scalar_width
        block = memoryview(payload)[pos:pos + size]
        if len(block) != size or not all(math.isfinite(x[0]) for x in struct.iter_unpack("<d" if scalar_width == 8 else "<f", block)):
            raise ValueError("Invalid serialized vertex attributes")
        blocks.append((len(fields) * scalar_width, len(names) * scalar_width, block))
        fields.extend(names)
        pos += size
    faces = memoryview(payload)[pos:]
    if len(faces) != triangles * 12 or any(i[0] >= vertices for i in struct.iter_unpack("<I", faces)):
        raise ValueError("Invalid serialized triangle indices")
    stride = len(fields) * scalar_width
    interleaved = bytearray(vertices * stride)
    for field_offset, size, block in blocks:
        for vertex in range(vertices):
            start = vertex * stride + field_offset
            interleaved[start:start + size] = block[vertex * size:(vertex + 1) * size]
    packed_faces = bytearray(triangles * 13)
    for face in range(triangles):
        packed_faces[face * 13] = 3
        packed_faces[face * 13 + 1:(face + 1) * 13] = faces[face * 12:(face + 1) * 12]
    header = ["ply", "format binary_little_endian 1.0", f"element vertex {vertices}", *[f"property {scalar_type} {name}" for name in fields], f"element face {triangles}", "property list uchar uint vertex_indices", "end_header"]
    destination.write_bytes(("\n".join(header) + "\n").encode("ascii") + interleaved + packed_faces)


def _verify_obj_dependencies(path: Path, seen: set[Path]) -> None:
    if path in seen:
        return
    seen.add(path)
    if path.suffix.lower() == ".obj":
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if line.lstrip().startswith("mtllib "):
                for name in line.split()[1:]:
                    _verify_obj_dependencies(relative_asset(path.parent, name), seen)
    elif path.suffix.lower() == ".mtl":
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            words = line.split()
            if words and (words[0].lower().startswith("map_") or words[0].lower() in {"bump", "disp", "decal"}):
                relative_asset(path.parent, words[-1])


def verify_references(source: Path, scene_directory: Path | None = None, seen: set[Path] | None = None) -> None:
    """Verify XML resource references (including includes and OBJ material files)."""
    seen = set() if seen is None else seen
    if source in seen:
        return
    seen.add(source)
    scene_directory = source.parent if scene_directory is None else scene_directory
    for node in ET.parse(source).iter():
        value = None
        if node.tag == "string" and node.get("name") == "filename":
            value = node.get("value", "")
        elif node.tag == "include":
            value = node.get("filename", "")
        if value is not None:
            asset = relative_asset(source.parent, value)
            if not asset.resolve().is_relative_to(scene_directory.resolve()):
                raise ValueError(f"Unsafe resource outside scene: {value}")
            if node.tag == "include":
                verify_references(asset, scene_directory, seen)
            else:
                _verify_obj_dependencies(asset, seen)


def has_authored_media(source: Path, seen: set[Path] | None = None) -> bool:
    """Inspect the prepared Mitsuba scene and its local includes."""
    seen = set() if seen is None else seen
    source = source.resolve()
    if source in seen:
        return False
    seen.add(source)
    root = ET.parse(source).getroot()
    if root.find('.//medium') is not None:
        return True
    return any(has_authored_media(relative_asset(source.parent, node.attrib['filename']), seen)
               for node in root.iter('include'))


def prepare_scene(directory: Path, entry: dict) -> Path:
    """Prepare XML in-place in a staging copy; return its local source path."""
    directory = Path(directory)
    xml_files = entry["xml_files"]
    original = relative_asset(directory, "scene_v3.xml" if "scene_v3.xml" in xml_files else xml_files[0])
    verify_references(original, directory)
    tree = ET.parse(original)
    root = tree.getroot()
    legacy = not root.get("version", "").startswith("3.")
    converted = {}
    for node in root.iter():
        if legacy and "name" in node.attrib:
            node.set("name", _snake(node.get("name")))
        if node.tag == "string" and node.get("name") == "filename":
            node.set("value", node.get("value").replace("\\", "/"))
        if legacy and node.tag == "film" and node.get("type") == "ldrfilm":
            node.set("type", "hdrfilm")
            for child in list(node):
                if child.get("name") in {"file_format", "gamma", "banner"}:
                    node.remove(child)
        if node.tag == "shape" and node.get("type") == "serialized":
            filename = node.find("string[@name='filename']")
            if filename is None:
                raise ValueError("Serialized shape has no filename")
            source = relative_asset(original.parent, filename.get("value"))
            shape_index = node.find("integer[@name='shape_index']")
            index = int(shape_index.get("value")) if shape_index is not None else 0
            key = (source, index)
            if key not in converted:
                destination = original.parent / f"converted-{len(converted)}.ply"
                serialized_to_ply(source, index, destination)
                converted[key] = destination.name
            node.set("type", "ply")
            filename.set("value", converted[key])
            if shape_index is not None:
                node.remove(shape_index)
    root.set("version", "3.0.0")
    destination = original.parent / "scene.prepared.xml"
    tree.write(destination, encoding="utf-8", xml_declaration=True)
    verify_references(destination, directory)
    return destination
