#!/usr/bin/env python3
"""Upgrade generated texture-test OTEX image resources from V0 to V1.

The PackBuild archive contains 12,687 RGBA32 replacement images and 855
native RGBA16 resources. V0 stores only type, dimensions, byte count and
pixels; V1 adds flags and scale fields.

RGBA32 replacements must use LOAD_AS_RAW, not LOAD_AS_IMG. The raw importer
uses HByteScale and VPixelScale to preserve N64 tile/stride semantics when
display lists load only part of a texture. LOAD_AS_IMG uploads the entire
replacement directly and bypasses that tile-aware path. Scale values come
from originalSize and overrideSize in Overrides_Manifest.json. Native type-2
RGBA16 resources are copied byte-for-byte, including legacy alignment padding.
All resource names and the portVersion marker are preserved.

The output is a valid O2R ZIP archive using stored ZIP entries. This avoids
recompressing hundreds of megabytes of image data and is supported by ZIP.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import struct
import zipfile
import re

OTEX_MAGIC = 0x4F544558
OTEX_HEADER_SIZE = 0x40
V0_PIXEL_OFFSET = 0x50
V1_PIXEL_OFFSET = 0x5C
TEX_FLAG_LOAD_AS_RAW = 0x01
BYTES_PER_PIXEL = {"rgba32": 4.0, "rgba16": 2.0, "ci8": 1.0, "ia8": 1.0, "ia16": 2.0, "i8": 1.0, "ci4": 0.5, "ia4": 0.5, "i4": 0.5, "yuv16": 2.0}


def load_override_manifest(path: str) -> dict[str, tuple[int, int, int, int]]:
    """Map each resource key to (native width, native height, override width, override height)."""
    with open(path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{path}: manifest has no entries array")
    overrides = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: malformed manifest entry")
        resource, original, replacement = entry.get("resource"), entry.get("originalSize"), entry.get("overrideSize")
        if not isinstance(resource, str) or not isinstance(original, list) or not isinstance(replacement, list):
            raise ValueError(f"{path}: entry missing resource/originalSize/overrideSize")
        if len(original) != 2 or len(replacement) != 2:
            raise ValueError(f"{path}: {resource}: dimensions must be [width, height]")
        try:
            ow, oh = int(original[0]), int(original[1])
            rw, rh = int(replacement[0]), int(replacement[1])
        except (TypeError, ValueError):
            raise ValueError(f"{path}: {resource}: non-integer dimensions")
        if min(ow, oh, rw, rh) <= 0:
            raise ValueError(f"{path}: {resource}: dimensions must be positive")
        if resource in overrides:
            raise ValueError(f"{path}: duplicate resource key {resource}")
        overrides[resource] = (ow, oh, rw, rh)
    if not overrides:
        raise ValueError(f"{path}: no texture entries found")
    return overrides



def load_original_formats(xml_root: str) -> dict[str, str]:
    """Read original N64 formats and index both symbolic and offset-based OTR names.

    The decomp XML often names a texture symbolically (for example gHilite1Tex),
    while 2Ship's extracted OTR resource is named from its source-file offset
    (for example gameplay_keep_Tex_00C830). Indexing only the XML symbol misses
    thousands of valid resources, especially object textures.
    """
    formats: dict[str, str] = {}

    def add(key: str, fmt: str, source: str) -> None:
        previous = formats.get(key)
        if previous is not None and previous != fmt:
            raise ValueError(
                f"Conflicting original formats for {key}: {previous} vs {fmt} ({source})"
            )
        formats[key] = fmt

    for root, _, files in os.walk(xml_root):
        for filename in files:
            if not filename.lower().endswith(".xml"):
                continue
            path = os.path.join(root, filename)
            with open(path, "r", encoding="utf-8-sig") as fh:
                source = fh.read()

            xml_group = os.path.basename(root)
            for file_match in re.finditer(r'<File\\b([^>]*)>(.*?)</File>', source, re.S):
                file_attrs, body = file_match.group(1), file_match.group(2)
                file_name_match = re.search(r'\\bName="([^"]+)"', file_attrs)
                out_name_match = re.search(r'\\bOutName="([^"]+)"', file_attrs)
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

                for tex_match in re.finditer(r'<Texture\\b([^>]*)/?>', body):
                    attrs = tex_match.group(1)
                    nm = re.search(r'\\bName="([^"]+)"', attrs)
                    fm = re.search(r'\\bFormat="([^"]+)"', attrs)
                    if not fm:
                        continue
                    fmt = fm.group(1).lower()
                    if fmt not in BYTES_PER_PIXEL:
                        continue

                    if nm:
                        add(archive + "/" + nm.group(1), fmt, path)

                    # 2Ship resource names commonly encode the texture's byte
                    # offset instead of the XML symbol. Index the documented
                    # Offset directly; do not infer a format from dimensions.
                    offset_match = re.search(r'\\bOffset="(0x[0-9A-Fa-f]+|[0-9]+)"', attrs)
                    if offset_match:
                        offset = int(offset_match.group(1), 0)
                        suffix = f"{offset:06X}"
                        aliases = (
                            f"{file_name}_Tex_{suffix}",
                            f"{file_name}Tex_{suffix}",
                            f"{file_name}_TLUT_{suffix}",
                            f"{file_name}TLUT_{suffix}",
                        )
                        for alias in aliases:
                            add(archive + "/" + alias, fmt, path)

    if not formats:
        raise ValueError(f"No texture format definitions found under {xml_root}")
    return formats

def convert_entry(name: str, data: bytes, overrides: dict[str, tuple[int, int, int, int]], original_formats: dict[str, str]) -> tuple[bytes, str]:
    """Return (resource bytes, conversion status)."""
    if name == "portVersion":
        return data, "port-marker"

    if len(data) < V0_PIXEL_OFFSET or struct.unpack_from("<I", data, 4)[0] != OTEX_MAGIC:
        return data, "non-otex"

    version = struct.unpack_from("<I", data, 8)[0]
    if version == 1:
        if len(data) < V1_PIXEL_OFFSET:
            raise ValueError(f"{name}: truncated OTEX V1 resource")
        return data, "already-v1"
    if version != 0:
        raise ValueError(f"{name}: unsupported OTEX version {version}")

    texture_type, width, height, data_size = struct.unpack_from("<IIII", data, OTEX_HEADER_SIZE)
    if width == 0 or height == 0:
        raise ValueError(f"{name}: invalid dimensions {width}x{height}")

    # Native-format palettes (type 2) must remain byte-for-byte identical.
    # They can contain alignment padding beyond their declared pixel bytes.
    if texture_type == 2:
        return data, "preserved-rgba16"
    if texture_type != 1:
        raise ValueError(f"{name}: unsupported V0 texture type {texture_type}")

    if name not in overrides:
        raise ValueError(f"{name}: RGBA32 resource has no Overrides_Manifest.json entry")
    native_width, native_height, replacement_width, replacement_height = overrides[name]
    if (width, height) != (replacement_width, replacement_height):
        raise ValueError(
            f"{name}: archive dimensions {width}x{height} do not match manifest "
            f"overrideSize {replacement_width}x{replacement_height}"
        )
    original_format = original_formats.get(name)
    if original_format not in BYTES_PER_PIXEL:
        raise ValueError(f"{name}: missing/unsupported original N64 texture format {original_format!r}")
    # RAW import scales original N64 row bytes into RGBA32 replacement row bytes.
    h_scale = (float(replacement_width) / float(native_width)) * (4.0 / BYTES_PER_PIXEL[original_format])
    v_scale = float(replacement_height) / float(native_height)
    if not math.isfinite(h_scale) or not math.isfinite(v_scale) or h_scale <= 0 or v_scale <= 0:
        raise ValueError(f"{name}: invalid scale factors {h_scale}x{v_scale}")

    expected_size = width * height * 4
    if data_size != expected_size:
        raise ValueError(
            f"{name}: RGBA32 byte count {data_size} does not match "
            f"{width}x{height}x4 ({expected_size})"
        )
    if len(data) != V0_PIXEL_OFFSET + data_size:
        raise ValueError(
            f"{name}: malformed V0 RGBA32 length "
            f"({len(data)} != {V0_PIXEL_OFFSET + data_size})"
        )

    pixels = data[V0_PIXEL_OFFSET:]
    header = bytearray(data[:OTEX_HEADER_SIZE])
    struct.pack_into("<I", header, 8, 1)  # OTEX V1

    # V1 body: type, width, height, flags, HByteScale, VPixelScale, dataSize.
    v1_fields = struct.pack(
        "<IIIIffI",
        texture_type,
        width,
        height,
        TEX_FLAG_LOAD_AS_RAW,
        h_scale,
        v_scale,
        data_size,
    )
    return bytes(header) + v1_fields + pixels, "upgraded-rgba32"


def convert_archive(source_path: str, output_path: str, manifest_path: str, xml_root: str) -> collections.Counter:
    if os.path.abspath(source_path) == os.path.abspath(output_path):
        raise ValueError("Input and output paths must be different")

    overrides = load_override_manifest(manifest_path)
    original_formats = load_original_formats(xml_root)
    missing_formats = sorted(set(overrides) - set(original_formats))
    if missing_formats:
        raise ValueError(f"Missing original N64 formats for {len(missing_formats)} manifest resources; examples: {missing_formats[:20]}")
    stats: collections.Counter = collections.Counter()
    with zipfile.ZipFile(source_path, "r") as source:
        source_names = source.namelist()
        source_comment = source.comment
        with zipfile.ZipFile(output_path, "w", allowZip64=True) as output:
            output.comment = source_comment
            for info in source.infolist():
                original = source.read(info.filename)  # also validates source CRC
                converted, status = convert_entry(info.filename, original, overrides, original_formats)
                stats[status] += 1

                # Keep the resource names and timestamps. Store entries rather
                # than recompressing image payloads; O2R is a standard ZIP.
                out_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                out_info.compress_type = zipfile.ZIP_STORED
                out_info.comment = info.comment
                out_info.extra = info.extra
                out_info.internal_attr = info.internal_attr
                out_info.external_attr = info.external_attr
                out_info.create_system = info.create_system
                output.writestr(out_info, converted)

    with zipfile.ZipFile(output_path, "r") as check:
        if check.namelist() != source_names:
            raise RuntimeError("Resource names/order changed during conversion")
        if check.read("portVersion") != b"\x01\x00\x05\x00\x00\x00\x01":
            raise RuntimeError("portVersion marker changed or is missing")
        # Verify local headers and CRCs for representative entries. Every
        # input entry was also CRC-checked while reading during conversion.
        for sample in (source_names[0], source_names[len(source_names) // 2], source_names[-1]):
            check.read(sample)

    if stats["upgraded-rgba32"] == 0:
        raise RuntimeError("No V0 RGBA32 resources were upgraded")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="source .o2r archive")
    parser.add_argument("output", help="destination .o2r archive")
    parser.add_argument(
        "--manifest",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                             "MajorasMask_UnrealInspired_FullReskin", "Overrides_Manifest.json"),
        help="override manifest containing originalSize and overrideSize (default: repository manifest)",
    )
    parser.add_argument(
        "--decomp-assets",
        required=True,
        help="path to zeldaret/mm assets/xml containing authoritative original texture formats",
    )
    args = parser.parse_args()

    stats = convert_archive(args.input, args.output, args.manifest, args.decomp_assets)
    print("OTEX conversion complete:")
    for name in sorted(stats):
        print(f"  {name}: {stats[name]}")
    print(f"Output size: {os.path.getsize(args.output):,} bytes")


if __name__ == "__main__":
    main()
