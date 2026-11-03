#!/usr/bin/env python3
"""Build the final OTEX V1 O2R directly from PackBuild PNG assets.

The checked-in O2R is used only as a resource/header template and as the
byte-exact source for native RGBA16 palette resources. RGBA32 pixels are read
from PackBuild PNGs and written directly into V1 resources; no intermediate
V0 archive conversion is performed.
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

from PIL import Image

OTEX_MAGIC = 0x4F544558
OTEX_HEADER_SIZE = 0x40
V0_PIXEL_OFFSET = 0x50
V1_PIXEL_OFFSET = 0x5C
TEX_FLAG_LOAD_AS_RAW = 0x01
BYTES_PER_PIXEL = {
    "rgba32": 4.0,
    "rgba16": 2.0,
    "ci8": 1.0,
    "ia8": 1.0,
    "ia16": 2.0,
    "i8": 1.0,
    "ci4": 0.5,
    "ia4": 0.5,
    "i4": 0.5,
    "yuv16": 2.0,
}
PORT_VERSION = b"\x01\x00\x05\x00\x00\x00\x01"


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
        resource = entry.get("resource")
        asset = entry.get("asset")
        original = entry.get("originalSize")
        replacement = entry.get("overrideSize")
        if not isinstance(resource, str) or not resource:
            raise ValueError(f"{path}: entry has no resource key")
        if resource in seen:
            raise ValueError(f"{path}: duplicate resource key {resource}")
        seen.add(resource)
        if not isinstance(asset, str) or not asset:
            raise ValueError(f"{path}: {resource}: missing asset path")
        if not isinstance(original, list) or len(original) != 2:
            raise ValueError(f"{path}: {resource}: originalSize must be [width, height]")
        if not isinstance(replacement, list) or len(replacement) != 2:
            raise ValueError(f"{path}: {resource}: overrideSize must be [width, height]")
        try:
            dimensions = [int(value) for value in (*original, *replacement)]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: {resource}: dimensions must be integers") from exc
        if min(dimensions) <= 0:
            raise ValueError(f"{path}: {resource}: dimensions must be positive")
        entry["_original_size"] = tuple(dimensions[:2])
        entry["_override_size"] = tuple(dimensions[2:])
    return entries


def load_original_formats(xml_root: str | Path) -> dict[str, str]:
    """Index authoritative N64 texture formats by symbolic and OTR offset names."""
    formats: dict[str, str] = {}

    def add(key: str, fmt: str, source: str) -> None:
        previous = formats.get(key)
        if previous is not None and previous != fmt:
            raise ValueError(f"Conflicting original formats for {key}: {previous} vs {fmt} ({source})")
        formats[key] = fmt

    for root, _, files in os.walk(xml_root):
        for filename in files:
            if not filename.lower().endswith(".xml"):
                continue
            path = os.path.join(root, filename)
            with open(path, "r", encoding="utf-8-sig") as stream:
                source = stream.read()
            xml_group = os.path.basename(root)
            for file_match in re.finditer(r'<File\b([^>]*)>(.*?)</File>', source, re.S):
                file_attrs, body = file_match.group(1), file_match.group(2)
                file_name_match = re.search(r'\bName="([^"]+)"', file_attrs)
                out_name_match = re.search(r'\bOutName="([^"]+)"', file_attrs)
                if not file_name_match:
                    continue
                file_name = file_name_match.group(1)
                archive = out_name_match.group(1) if out_name_match else file_name
                if archive.endswith(".unarchive"):
                    archive = archive[:-len(".unarchive")]
                if out_name_match:
                    archive = file_name + "/" + archive
                if xml_group in {"code", "misc", "objects"} and not archive.startswith(xml_group + "/"):
                    archive = xml_group + "/" + archive
                for tex_match in re.finditer(r'<Texture\b([^>]*)/?>', body):
                    attrs = tex_match.group(1)
                    name_match = re.search(r'\bName="([^"]+)"', attrs)
                    format_match = re.search(r'\bFormat="([^"]+)"', attrs)
                    if not format_match:
                        continue
                    fmt = format_match.group(1).lower()
                    if fmt not in BYTES_PER_PIXEL:
                        continue
                    if name_match:
                        add(archive + "/" + name_match.group(1), fmt, path)
                    offset_match = re.search(r'\bOffset="(0x[0-9A-Fa-f]+|[0-9]+)"', attrs)
                    if offset_match:
                        offset = int(offset_match.group(1), 0)
                        suffix = f"{offset:06X}"
                        for alias in (
                            f"{file_name}_Tex_{suffix}",
                            f"{file_name}Tex_{suffix}",
                            f"{file_name}_TLUT_{suffix}",
                            f"{file_name}TLUT_{suffix}",
                        ):
                            add(archive + "/" + alias, fmt, path)
    if not formats:
        raise ValueError(f"No texture format definitions found under {xml_root}")
    return formats


def make_v1_rgba32(
    resource: str,
    template: bytes,
    rgba_pixels: bytes,
    image_size: tuple[int, int],
    manifest_entry: dict[str, Any],
    original_format: str,
) -> bytes:
    if len(template) < V0_PIXEL_OFFSET or struct.unpack_from("<I", template, 4)[0] != OTEX_MAGIC:
        raise ValueError(f"{resource}: template is not an OTEX resource")
    version = struct.unpack_from("<I", template, 8)[0]
    if version != 0:
        raise ValueError(f"{resource}: expected V0 template, found OTEX version {version}")
    texture_type, width, height, data_size = struct.unpack_from("<IIII", template, OTEX_HEADER_SIZE)
    expected_width, expected_height = manifest_entry["_override_size"]
    if (width, height) != (expected_width, expected_height):
        raise ValueError(
            f"{resource}: template dimensions {width}x{height} do not match manifest "
            f"overrideSize {expected_width}x{expected_height}"
        )
    if image_size != (width, height):
        raise ValueError(f"{resource}: PNG dimensions {image_size} do not match template {width}x{height}")
    if texture_type != 1:
        raise ValueError(f"{resource}: RGBA32 PNG maps to unexpected OTEX type {texture_type}")
    expected_size = width * height * 4
    if len(rgba_pixels) != expected_size:
        raise ValueError(f"{resource}: decoded PNG is {len(rgba_pixels)} bytes; expected {expected_size}")
    if data_size != expected_size:
        raise ValueError(f"{resource}: V0 template declares {data_size} bytes; expected {expected_size}")
    if len(template) != V0_PIXEL_OFFSET + data_size:
        raise ValueError(f"{resource}: malformed V0 template length {len(template)}")

    native_width, native_height = manifest_entry["_original_size"]
    if original_format not in BYTES_PER_PIXEL:
        raise ValueError(f"{resource}: unsupported original N64 format {original_format!r}")
    h_scale = (float(width) / native_width) * (4.0 / BYTES_PER_PIXEL[original_format])
    v_scale = float(height) / native_height
    if not math.isfinite(h_scale) or not math.isfinite(v_scale) or h_scale <= 0 or v_scale <= 0:
        raise ValueError(f"{resource}: invalid RAW import scale {h_scale}x{v_scale}")

    header = bytearray(template[:OTEX_HEADER_SIZE])
    struct.pack_into("<I", header, 8, 1)
    body = struct.pack(
        "<IIIIffI",
        texture_type,
        width,
        height,
        TEX_FLAG_LOAD_AS_RAW,
        h_scale,
        v_scale,
        expected_size,
    )
    return bytes(header) + body + rgba_pixels


def _zip_info_like(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    result = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    result.compress_type = zipfile.ZIP_STORED
    result.comment = info.comment
    result.extra = info.extra
    result.internal_attr = info.internal_attr
    result.external_attr = info.external_attr
    result.create_system = info.create_system
    return result


def build_archive(
    source_archive: str | Path,
    output_archive: str | Path,
    packbuild_root: str | Path,
    manifest_path: str | Path,
    xml_root: str | Path,
) -> collections.Counter:
    source_archive = Path(source_archive)
    output_archive = Path(output_archive)
    packbuild_root = Path(packbuild_root)
    if source_archive.resolve() == output_archive.resolve():
        raise ValueError("Input and output archives must be different")

    entries = load_manifest(manifest_path)
    manifest_by_resource = {entry["resource"]: entry for entry in entries}
    formats = load_original_formats(xml_root)
    palette_entries = {
        key: value for key, value in manifest_by_resource.items()
        if value.get("styleMode") == "native-palette-preserve"
    }
    rgba_entries = {key: value for key, value in manifest_by_resource.items() if key not in palette_entries}

    missing_formats = sorted(set(rgba_entries) - set(formats))
    if missing_formats:
        raise ValueError(
            f"Missing original N64 formats for {len(missing_formats)} RGBA32 resources; "
            f"examples: {missing_formats[:20]}"
        )

    stats: collections.Counter = collections.Counter()
    with zipfile.ZipFile(source_archive, "r") as source:
        source_names = source.namelist()
        if len(source_names) != len(set(source_names)):
            raise ValueError("Source O2R contains duplicate resource names")
        if "portVersion" not in source_names:
            raise ValueError("Source O2R is missing portVersion")
        missing_resources = sorted(set(manifest_by_resource) - set(source_names))
        unexpected_resources = sorted(set(source_names) - set(manifest_by_resource) - {"portVersion"})
        if missing_resources or unexpected_resources:
            raise ValueError(
                f"Archive/manifest resource mismatch: {len(missing_resources)} missing, "
                f"{len(unexpected_resources)} unexpected; missing examples={missing_resources[:10]}, "
                f"unexpected examples={unexpected_resources[:10]}"
            )

        with zipfile.ZipFile(output_archive, "w", allowZip64=True) as output:
            output.comment = source.comment
            for info in source.infolist():
                original = source.read(info.filename)
                name = info.filename
                if name == "portVersion":
                    if original != PORT_VERSION:
                        raise ValueError(f"Unexpected portVersion marker: {original!r}")
                    output.writestr(_zip_info_like(info), original)
                    stats["port-marker"] += 1
                    continue

                entry = manifest_by_resource[name]
                asset_path = packbuild_root / entry["asset"]
                if not asset_path.is_file():
                    raise FileNotFoundError(f"{name}: missing PackBuild asset {asset_path}")

                if name in palette_entries:
                    if len(original) < V0_PIXEL_OFFSET or struct.unpack_from("<I", original, 4)[0] != OTEX_MAGIC:
                        raise ValueError(f"{name}: native palette template is not OTEX")
                    version = struct.unpack_from("<I", original, 8)[0]
                    texture_type = struct.unpack_from("<I", original, OTEX_HEADER_SIZE)[0]
                    if version != 0 or texture_type != 2:
                        raise ValueError(
                            f"{name}: expected native V0 RGBA16 palette (version=0,type=2), "
                            f"found version={version}, type={texture_type}"
                        )
                    # The RGB5A1 PNG is an inspection/source asset only. Keep
                    # the archive's original palette bytes, including padding.
                    converted = original
                    stats["preserved-native-palette"] += 1
                else:
                    with Image.open(asset_path) as image:
                        image.load()
                        image_size = image.size
                        rgba_pixels = image.convert("RGBA").tobytes()
                    converted = make_v1_rgba32(
                        name, original, rgba_pixels, image_size, entry, formats[name]
                    )
                    stats["built-v1-rgba32-from-png"] += 1

                output.writestr(_zip_info_like(info), converted)

    if stats["built-v1-rgba32-from-png"] != len(rgba_entries):
        raise RuntimeError(
            f"Built {stats['built-v1-rgba32-from-png']} RGBA32 resources; "
            f"manifest requires {len(rgba_entries)}"
        )
    if stats["preserved-native-palette"] != len(palette_entries):
        raise RuntimeError(
            f"Preserved {stats['preserved-native-palette']} palettes; "
            f"manifest requires {len(palette_entries)}"
        )

    with zipfile.ZipFile(output_archive, "r") as check, zipfile.ZipFile(source_archive, "r") as source:
        if check.namelist() != source.namelist():
            raise RuntimeError("Resource names/order changed while building final O2R")
        if check.read("portVersion") != PORT_VERSION:
            raise RuntimeError("portVersion marker changed")
        for name in palette_entries:
            if check.read(name) != source.read(name):
                raise RuntimeError(f"Native palette resource changed: {name}")
        for name in list(rgba_entries)[:3]:
            data = check.read(name)
            if struct.unpack_from("<I", data, 8)[0] != 1:
                raise RuntimeError(f"{name}: final resource is not OTEX V1")
            if struct.unpack_from("<I", data, 0x4C)[0] != TEX_FLAG_LOAD_AS_RAW:
                raise RuntimeError(f"{name}: final resource is not flagged LOAD_AS_RAW")
    return stats


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    pack_root = root / "MajorasMask_UnrealInspired_FullReskin"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-o2r",
        default=str(pack_root / "MajorasMask_UnrealInspired_Environment.o2r"),
        help="V0 O2R resource/header template (default: checked-in LFS archive)",
    )
    parser.add_argument("--output", default="texture-pack-v1.o2r", help="final O2R output path")
    parser.add_argument("--packbuild", default=str(pack_root / "PackBuild"), help="PackBuild PNG root")
    parser.add_argument(
        "--manifest", default=str(pack_root / "Overrides_Manifest.json"),
        help="texture manifest",
    )
    parser.add_argument(
        "--decomp-assets", required=True,
        help="zeldaret/mm assets/xml directory with authoritative original texture formats",
    )
    args = parser.parse_args()

    stats = build_archive(args.source_o2r, args.output, args.packbuild, args.manifest, args.decomp_assets)
    print("Direct PackBuild -> OTEX V1 O2R build complete:")
    for name in sorted(stats):
        print(f"  {name}: {stats[name]}")
    print(f"Output size: {os.path.getsize(args.output):,} bytes")


if __name__ == "__main__":
    main()
