#!/usr/bin/env python3
"""Build a fresh OTEX V1 O2R from PackBuild PNGs and the manifest.

No existing O2R/OTR archive is used as a template. Resource names come from
Overrides_Manifest.json; original N64 formats come from ZAPD XML and the
source C texture includes. RGBA32 replacements use LOAD_AS_RAW. Native
RGBA16 palette PNGs are encoded back into N64 RGBA5551 bytes.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import struct
import zipfile
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from PIL import Image

OTEX_MAGIC = 0x4F544558
COMMON_HEADER_SIZE = 0x40
V0_PIXEL_OFFSET = 0x50
V1_PIXEL_OFFSET = 0x5C
TEX_FLAG_LOAD_AS_RAW = 0x01
BYTES_PER_PIXEL = {
    "rgba32": 4.0, "rgba16": 2.0, "ci8": 1.0, "ia8": 1.0,
    "ia16": 2.0, "i8": 1.0, "ci4": 0.5, "ia4": 0.5,
    "i4": 0.5, "yuv16": 2.0,
}
PORT_VERSION = b"\x01\x00\x05\x00\x00\x00\x01"
RESOURCE_ID = 0xDEADBEEFDEADBEEF


def load_manifest(path: str | Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: manifest has no entries array")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: malformed manifest entry")
        resource, asset = entry.get("resource"), entry.get("asset")
        original, replacement = entry.get("originalSize"), entry.get("overrideSize")
        if not isinstance(resource, str) or not resource or resource in seen:
            raise ValueError(f"{path}: missing or duplicate resource key {resource!r}")
        if resource.startswith("/") or ".." in Path(resource).parts:
            raise ValueError(f"{path}: unsafe resource key {resource!r}")
        if not isinstance(asset, str) or not asset:
            raise ValueError(f"{path}: {resource}: missing asset path")
        if asset.startswith("/") or ".." in Path(asset).parts:
            raise ValueError(f"{path}: {resource}: unsafe asset path {asset!r}")
        if not isinstance(original, list) or len(original) != 2:
            raise ValueError(f"{path}: {resource}: originalSize must be [width, height]")
        if not isinstance(replacement, list) or len(replacement) != 2:
            raise ValueError(f"{path}: {resource}: overrideSize must be [width, height]")
        try:
            dims = [int(v) for v in (*original, *replacement)]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: {resource}: dimensions must be integers") from exc
        if min(dims) <= 0:
            raise ValueError(f"{path}: {resource}: dimensions must be positive")
        seen.add(resource)
        entry["_original_size"] = (dims[0], dims[1])
        entry["_override_size"] = (dims[2], dims[3])
    return entries


def _resource_archive_name(
    file_name: str, out_name: str | None, directory_parts: tuple[str, ...]
) -> str:
    archive = out_name or file_name
    if archive.endswith(".unarchive"):
        archive = archive[:-10]
    if out_name:
        archive = file_name + "/" + archive
    # XML nesting is meaningful: scenes/nonmq/KAKUSIANA.xml maps to
    # scenes/nonmq/KAKUSIANA, not merely KAKUSIANA.
    prefix = "/".join(directory_parts)
    if prefix and not archive.startswith(prefix + "/"):
        archive = prefix + "/" + archive
    return archive


def load_original_formats(xml_root: str | Path, source_root: str | Path | None = None) -> dict[str, str]:
    """Index formats from ZAPD XML and aliases from C arrays' included assets."""
    root = Path(xml_root)
    if not root.is_dir():
        raise FileNotFoundError(f"Decomp XML directory does not exist: {root}")
    formats: dict[str, str] = {}

    def add(key: str, fmt: str, source: str) -> None:
        old = formats.get(key)
        if old is not None and old != fmt:
            raise ValueError(f"Conflicting original formats for {key}: {old} vs {fmt} ({source})")
        formats[key] = fmt

    for xml_path in root.rglob("*.xml"):
        try:
            xml = ET.parse(xml_path).getroot()
        except ET.ParseError as exc:
            raise ValueError(f"Invalid decomp XML {xml_path}: {exc}") from exc
        directory_parts = xml_path.parent.relative_to(root).parts
        for file_node in xml.findall(".//File"):
            file_name = file_node.get("Name")
            if not file_name:
                continue
            resource_dirs: tuple[str, ...] = ()
            if directory_parts and directory_parts[0] in {"code", "misc", "objects", "overlays"}:
                # These XML folders are also the first O2R resource path segment.
                resource_dirs = (directory_parts[0],)
            elif directory_parts and directory_parts[0] == "scenes":
                # Scene XMLs live in assets/xml/scenes/<scene>/<scene>.xml,
                # while O2R keys are scenes/nonmq/<File Name> or scenes/mq/<File Name>.
                game = (file_node.get("Game") or "MM").upper()
                mode = "mq" if game == "MQ" else "nonmq"
                resource_dirs = ("scenes", mode)
            # interface/, archives/, and boot/ are XML organization folders,
            # not O2R path prefixes (e.g. interface/do_action_static.xml ->
            # do_action_static/<texture>).
            archive = _resource_archive_name(file_name, file_node.get("OutName"), resource_dirs)
            for texture in file_node.findall(".//Texture"):
                fmt = (texture.get("Format") or "").lower()
                if fmt not in BYTES_PER_PIXEL:
                    continue
                name = texture.get("Name")
                if name:
                    add(archive + "/" + name, fmt, str(xml_path))
                offset = texture.get("Offset")
                if offset:
                    try:
                        suffix = f"{int(offset, 0):06X}"
                    except ValueError as exc:
                        raise ValueError(f"{xml_path}: invalid texture offset {offset!r}") from exc
                    for alias in (
                        f"{file_name}_Tex_{suffix}", f"{file_name}Tex_{suffix}",
                        f"{file_name}_TLUT_{suffix}", f"{file_name}TLUT_{suffix}",
                    ):
                        add(archive + "/" + alias, fmt, str(xml_path))

    # A C symbol may be declared in a different source file from the display
    # list that references it. Resolve the declaration's included ZAPD asset,
    # e.g. gCircleTex -> assets/code/circle_tex/circle.i4.inc.c.
    decomp_root = Path(source_root) if source_root is not None else root.parent.parent
    source_roots = [decomp_root / "src", decomp_root / "include"]
    symbol_formats: dict[str, str] = {}
    include_pattern = re.compile(
        r'\b([A-Za-z_]\w*)\s*\[\s*\]\s*=\s*\{\s*#include\s+"assets/[^"]+\.'
        r'(rgba32|rgba16|ci8|ia8|ia16|i8|ci4|ia4|i4|yuv16)\.inc\.c"',
        re.I | re.S,
    )
    for source_root in source_roots:
        if not source_root.is_dir():
            continue
        for source_path in source_root.rglob("*"):
            if source_path.suffix.lower() not in {".c", ".h"}:
                continue
            source = source_path.read_text(encoding="utf-8", errors="replace")
            for match in include_pattern.finditer(source):
                symbol, fmt = match.group(1), match.group(2).lower()
                previous = symbol_formats.get(symbol)
                if previous is not None and previous != fmt:
                    raise ValueError(
                        f"Conflicting C include formats for symbol {symbol}: {previous} vs {fmt}"
                    )
                symbol_formats[symbol] = fmt

    # Resource paths in O2R manifests can use the consuming overlay/symbol
    # name rather than the XML extraction name. Match the terminal symbol.
    for symbol, fmt in symbol_formats.items():
        add_alias = symbol
        formats.setdefault(add_alias, fmt)
    return formats


def original_format_for(resource: str, formats: dict[str, str]) -> str | None:
    # Do not collapse _Overflow resources onto their base texture: an
    # overflow view can intentionally use a different source format.
    if resource in formats:
        return formats[resource]
    return formats.get(resource.rsplit("/", 1)[-1])


def make_common_header(resource_version: int) -> bytes:
    """Create the standard OTR header used by OTRExporter::WriteHeader."""
    header = bytearray(COMMON_HEADER_SIZE)
    header[0] = 0  # Endianness::Little
    header[1] = 0  # isCustom (OTRExporter writes false for texture resources)
    header[2] = 0
    header[3] = 0
    struct.pack_into("<I", header, 4, OTEX_MAGIC)
    struct.pack_into("<I", header, 8, resource_version)
    struct.pack_into("<Q", header, 0x0C, RESOURCE_ID)
    # OTRExporter writes resourceVersions[OTEX] at 0x10. Texture version
    # registration currently uses 0; this intentionally overwrites the upper
    # four bytes of the legacy 64-bit ID field, matching the native writer.
    struct.pack_into("<I", header, 0x10, 0)
    struct.pack_into("<Q", header, 0x14, 0)  # ROM CRC
    struct.pack_into("<I", header, 0x1C, 0)  # ROM enum
    return bytes(header)


def make_v1_rgba32(
    resource: str,
    rgba_pixels: bytes,
    image_size: tuple[int, int],
    entry: dict[str, Any],
    original_formats: dict[str, str],
) -> bytes:
    width, height = image_size
    if image_size != entry["_override_size"]:
        raise ValueError(
            f"{resource}: PNG dimensions {width}x{height} do not match "
            f"manifest overrideSize {entry['_override_size']}"
        )
    native_width, native_height = entry["_original_size"]
    original_format = original_format_for(resource, original_formats)
    if original_format not in BYTES_PER_PIXEL:
        raise ValueError(f"{resource}: missing/unsupported original N64 format {original_format!r}")
    expected_size = width * height * 4
    if len(rgba_pixels) != expected_size:
        raise ValueError(f"{resource}: PNG decoded to {len(rgba_pixels)} bytes; expected {expected_size}")
    h_scale = (width / native_width) * (4.0 / BYTES_PER_PIXEL[original_format])
    v_scale = height / native_height
    if not all(math.isfinite(v) and v > 0 for v in (h_scale, v_scale)):
        raise ValueError(f"{resource}: invalid raw-import scale {h_scale}x{v_scale}")
    body = struct.pack(
        "<IIIIffI", 1, width, height, TEX_FLAG_LOAD_AS_RAW,
        h_scale, v_scale, expected_size,
    )
    return make_common_header(1) + body + rgba_pixels


def encode_rgba5551(image: Image.Image, resource: str) -> bytes:
    """Encode RGBA PNG pixels to N64 RGBA16 (5/5/5/1), high byte first."""
    rgba = image.convert("RGBA").tobytes()
    if len(rgba) != image.width * image.height * 4:
        raise ValueError(f"{resource}: invalid decoded palette pixel buffer")
    result = bytearray(image.width * image.height * 2)
    out = 0
    for pos in range(0, len(rgba), 4):
        r, g, b, a = rgba[pos:pos + 4]
        word = ((r >> 3) << 11) | ((g >> 3) << 6) | ((b >> 3) << 1) | (1 if a >= 128 else 0)
        result[out] = (word >> 8) & 0xFF
        result[out + 1] = word & 0xFF
        out += 2
    return bytes(result)


def make_v0_rgba16_palette(resource: str, image: Image.Image, entry: dict[str, Any]) -> bytes:
    if image.size != entry["_override_size"]:
        raise ValueError(
            f"{resource}: palette PNG dimensions {image.size} do not match "
            f"manifest overrideSize {entry['_override_size']}"
        )
    pixels = encode_rgba5551(image, resource)
    width, height = image.size
    body = struct.pack("<IIII", 2, width, height, len(pixels))
    return make_common_header(0) + body + pixels


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_archive(
    output_archive: str | Path,
    packbuild_root: str | Path,
    manifest_path: str | Path,
    decomp_assets_root: str | Path,
    decomp_source_root: str | Path | None = None,
) -> collections.Counter:
    output_archive = Path(output_archive)
    packbuild_root = Path(packbuild_root)
    entries = load_manifest(manifest_path)
    original_formats = load_original_formats(decomp_assets_root, decomp_source_root)
    palette_entries = [e for e in entries if e.get("styleMode") == "native-palette-preserve"]
    rgba_entries = [e for e in entries if e.get("styleMode") != "native-palette-preserve"]
    missing_formats = sorted(
        e["resource"] for e in rgba_entries
        if original_format_for(e["resource"], original_formats) not in BYTES_PER_PIXEL
    )
    if missing_formats:
        raise ValueError(
            f"Missing original N64 formats for {len(missing_formats)} RGBA32 resources; "
            f"examples={missing_formats[:20]}"
        )

    stats: collections.Counter = collections.Counter()
    output_archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_archive, "w", allowZip64=True) as output:
        output.writestr(_zip_info("portVersion"), PORT_VERSION)
        stats["port-marker"] += 1
        for entry in entries:
            resource = entry["resource"]
            asset_path = packbuild_root / entry["asset"]
            if not asset_path.is_file():
                raise FileNotFoundError(f"{resource}: missing PackBuild asset {asset_path}")
            with Image.open(asset_path) as source_image:
                source_image.load()
                image = source_image.copy()
            if entry.get("styleMode") == "native-palette-preserve":
                converted = make_v0_rgba16_palette(resource, image, entry)
                stats["built-v0-rgba16-palette-from-png"] += 1
            else:
                rgba_pixels = image.convert("RGBA").tobytes()
                converted = make_v1_rgba32(
                    resource, rgba_pixels, image.size, entry, original_formats
                )
                stats["built-v1-rgba32-from-png"] += 1
            output.writestr(_zip_info(resource), converted)

    # Re-open and fully CRC-check every entry; then validate each generated
    # resource header, payload size, and texture version.
    with zipfile.ZipFile(output_archive, "r") as check:
        expected_names = ["portVersion"] + [e["resource"] for e in entries]
        if check.namelist() != expected_names:
            raise RuntimeError("Generated O2R resource names/order differ from manifest")
        if check.read("portVersion") != PORT_VERSION:
            raise RuntimeError("Generated O2R has an invalid portVersion marker")
        for entry in entries:
            name = entry["resource"]
            data = check.read(name)  # validates ZIP CRC
            if len(data) < V0_PIXEL_OFFSET:
                raise RuntimeError(f"{name}: truncated OTEX resource")
            if data[0] != 0 or data[1] != 0:
                raise RuntimeError(f"{name}: invalid common resource header")
            if struct.unpack_from("<I", data, 4)[0] != OTEX_MAGIC:
                raise RuntimeError(f"{name}: invalid OTEX FourCC")
            expected_version = 0 if entry.get("styleMode") == "native-palette-preserve" else 1
            if struct.unpack_from("<I", data, 8)[0] != expected_version:
                raise RuntimeError(f"{name}: incorrect OTEX version")
            if expected_version == 1:
                if struct.unpack_from("<I", data, 0x4C)[0] != TEX_FLAG_LOAD_AS_RAW:
                    raise RuntimeError(f"{name}: RGBA32 resource is not LOAD_AS_RAW")
                width, height = struct.unpack_from("<II", data, 0x44)
                size = struct.unpack_from("<I", data, 0x58)[0]
                if size != width * height * 4 or len(data) != V1_PIXEL_OFFSET + size:
                    raise RuntimeError(f"{name}: invalid RGBA32 payload length")
            else:
                texture_type, width, height, size = struct.unpack_from("<IIII", data, 0x40)
                if texture_type != 2 or size != width * height * 2:
                    raise RuntimeError(f"{name}: invalid RGBA16 palette payload")
                if len(data) != V0_PIXEL_OFFSET + size:
                    raise RuntimeError(f"{name}: invalid RGBA16 palette resource length")

    if stats["built-v1-rgba32-from-png"] != len(rgba_entries):
        raise RuntimeError("Not all RGBA32 manifest entries were built")
    if stats["built-v0-rgba16-palette-from-png"] != len(palette_entries):
        raise RuntimeError("Not all native palette manifest entries were built")
    return stats


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    pack_root = root / "MajorasMask_UnrealInspired_FullReskin"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="texture-pack-v1.o2r")
    parser.add_argument("--decomp-assets", required=True, help="Majora's Mask N64 XML directory")
    parser.add_argument("--decomp-source-root", help="Repository root containing src/ and include/")
    parser.add_argument("--packbuild", default=str(pack_root / "PackBuild"))
    parser.add_argument("--manifest", default=str(pack_root / "Overrides_Manifest.json"))
    args = parser.parse_args()
    stats = build_archive(
        args.output, args.packbuild, args.manifest, args.decomp_assets, args.decomp_source_root
    )
    print("Fresh O2R build complete (no source archive used):")
    for name in sorted(stats):
        print(f"  {name}: {stats[name]}")
    print(f"Output size: {os.path.getsize(args.output):,} bytes")


if __name__ == "__main__":
    main()
