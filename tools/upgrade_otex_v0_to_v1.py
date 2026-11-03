#!/usr/bin/env python3
"""Upgrade generated texture-test OTEX image resources from V0 to V1.

The current PackBuild archive contains 12,687 RGBA32 replacement images and
855 native RGBA16/RGB5A1 palette resources. V0 stores only type, dimensions,
byte count and pixels, so the loader cannot be told to consume the RGBA32
replacement as an image. V1 adds flags and scale fields.

This converter upgrades only type-1 RGBA32 image resources using
TEX_FLAG_LOAD_AS_IMG, HByteScale=1.0 and VPixelScale=1.0. Type-2 palette
resources are copied byte-for-byte, including any legacy alignment padding.
All resource names and the portVersion marker are preserved.

The output is a valid O2R ZIP archive using stored ZIP entries. This avoids
recompressing hundreds of megabytes of image data and is supported by ZIP.
"""

from __future__ import annotations

import argparse
import collections
import os
import struct
import zipfile

OTEX_MAGIC = 0x4F544558
OTEX_HEADER_SIZE = 0x40
V0_PIXEL_OFFSET = 0x50
V1_PIXEL_OFFSET = 0x5C
TEX_FLAG_LOAD_AS_IMG = 0x02


def convert_entry(name: str, data: bytes) -> tuple[bytes, str]:
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
    if texture_type != 1:
        return data, f"preserved-type-{texture_type}"

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
        TEX_FLAG_LOAD_AS_IMG,
        1.0,
        1.0,
        data_size,
    )
    return bytes(header) + v1_fields + pixels, "upgraded-rgba32"


def convert_archive(source_path: str, output_path: str) -> collections.Counter:
    if os.path.abspath(source_path) == os.path.abspath(output_path):
        raise ValueError("Input and output paths must be different")

    stats: collections.Counter = collections.Counter()
    with zipfile.ZipFile(source_path, "r") as source:
        source_names = source.namelist()
        source_comment = source.comment
        with zipfile.ZipFile(output_path, "w", allowZip64=True) as output:
            output.comment = source_comment
            for info in source.infolist():
                original = source.read(info.filename)  # also validates source CRC
                converted, status = convert_entry(info.filename, original)
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
    args = parser.parse_args()

    stats = convert_archive(args.input, args.output)
    print("OTEX conversion complete:")
    for name in sorted(stats):
        print(f"  {name}: {stats[name]}")
    print(f"Output size: {os.path.getsize(args.output):,} bytes")


if __name__ == "__main__":
    main()
