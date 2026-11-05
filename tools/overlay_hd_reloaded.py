#!/usr/bin/env python3
"""Overlay PackBuild artwork onto the official MM-Reloaded 2Ship HD O2R.

The upstream archive is the base: all of its resources, ordering, metadata,
and unrelated textures are retained. Manifest resources are replaced with
the user's PackBuild artwork. Resources absent upstream are added from the
repository's original 2Ship template archive as a compatibility fallback.
"""

from __future__ import annotations

import argparse
import collections
import json
import struct
import zipfile
from pathlib import Path

from PIL import Image

try:
    from .build_texture_pack import (
    BYTES_PER_PIXEL,
    OTEX_MAGIC,
    OTEX_HEADER_SIZE,
    V0_PIXEL_OFFSET,
    V1_PIXEL_OFFSET,
    PORT_VERSION,
    TEX_FLAG_LOAD_AS_RAW,
    load_manifest,
    resolve_original_formats,
        _zip_info_like,
    )
except ImportError:  # Direct script execution from tools/ also needs to work.
    from build_texture_pack import (
        BYTES_PER_PIXEL, OTEX_MAGIC, OTEX_HEADER_SIZE, V0_PIXEL_OFFSET,
        V1_PIXEL_OFFSET, PORT_VERSION, TEX_FLAG_LOAD_AS_RAW, load_manifest,
        resolve_original_formats, _zip_info_like,
    )

def build_rgba32(template: bytes, resource: str, pixels: bytes,
                 image_size: tuple[int, int], entry: dict,
                 original_formats: dict[str, str]) -> bytes:
    if len(template) < V0_PIXEL_OFFSET or struct.unpack_from("<I", template, 4)[0] != OTEX_MAGIC:
        raise ValueError(f"{resource}: base resource is not OTEX")
    version = struct.unpack_from("<I", template, 8)[0]
    if version not in (0, 1):
        raise ValueError(f"{resource}: unsupported OTEX version {version}")
    if version == 1 and len(template) < V1_PIXEL_OFFSET:
        raise ValueError(f"{resource}: truncated OTEX V1 template")
    texture_type = struct.unpack_from("<I", template, OTEX_HEADER_SIZE)[0]
    if texture_type != 1:
        raise ValueError(f"{resource}: expected RGBA32 resource, found texture type {texture_type}")
    width, height = entry["_override_size"]
    if image_size != (width, height):
        raise ValueError(f"{resource}: PackBuild image is {image_size}, expected {(width, height)}")
    expected_bytes = width * height * 4
    if len(pixels) != expected_bytes:
        raise ValueError(f"{resource}: decoded PNG byte count {len(pixels)} != {expected_bytes}")
    fmt = original_formats.get(resource)
    if fmt not in BYTES_PER_PIXEL:
        raise ValueError(f"{resource}: no authoritative original N64 format")
    native_width, native_height = entry["_original_size"]
    h_scale = (width / native_width) * (4.0 / BYTES_PER_PIXEL[fmt])
    v_scale = height / native_height
    if h_scale <= 0 or v_scale <= 0:
        raise ValueError(f"{resource}: invalid raw-import scale")
    header = bytearray(template[:OTEX_HEADER_SIZE])
    struct.pack_into("<I", header, 8, 1)
    fields = struct.pack("<IIIIffI", 1, width, height, TEX_FLAG_LOAD_AS_RAW,
                         h_scale, v_scale, expected_bytes)
    return bytes(header) + fields + pixels


def overlay(base_path: str, fallback_path: str, output_path: str,
            packbuild_root: str, manifest_path: str, decomp_root: str,
            port_metadata_path: str) -> collections.Counter:
    entries = load_manifest(manifest_path)
    by_name = {e["resource"]: e for e in entries}
    palettes = {n for n, e in by_name.items() if e.get("styleMode") == "native-palette-preserve"}
    rgba = sorted(set(by_name) - palettes)
    original_formats, _, _ = resolve_original_formats(
        decomp_root, rgba, port_metadata_path,
        {n: by_name[n]["_original_size"] for n in rgba},
    )
    root = Path(packbuild_root)
    stats: collections.Counter = collections.Counter()

    with zipfile.ZipFile(base_path) as base, zipfile.ZipFile(fallback_path) as fallback:
        base_names = base.namelist()
        fallback_names = set(fallback.namelist())
        if len(base_names) != len(set(base_names)):
            raise ValueError("HD base archive contains duplicate resource names")
        if "portVersion" not in base_names:
            raise ValueError("HD base archive has no portVersion marker")
        marker = base.read("portVersion")
        if marker != PORT_VERSION:
            raise ValueError(f"HD base portVersion {marker.hex()} is incompatible; expected {PORT_VERSION.hex()}")
        missing = sorted(set(by_name) - set(base_names))
        unavailable = sorted(set(missing) - fallback_names)
        if unavailable:
            raise ValueError(f"{len(unavailable)} manifest resources are missing from both archives: {unavailable[:12]}")

        with zipfile.ZipFile(output_path, "w", allowZip64=True) as out:
            out.comment = base.comment
            written = set()
            for info in base.infolist():
                name = info.filename
                original = base.read(name)
                if name == "portVersion":
                    converted = original
                    stats["preserved-port-version"] += 1
                elif name not in by_name:
                    converted = original
                    stats["preserved-upstream-resource"] += 1
                elif name in palettes:
                    converted = original
                    stats["preserved-upstream-native-palette"] += 1
                else:
                    asset = root / by_name[name]["asset"]
                    if not asset.is_file():
                        raise FileNotFoundError(f"{name}: PackBuild asset missing: {asset}")
                    with Image.open(asset) as image:
                        image.load()
                        image_size = image.size
                        pixels = image.convert("RGBA").tobytes()
                    converted = build_rgba32(original, name, pixels, image_size, by_name[name], original_formats)
                    stats["overlaid-packbuild-artwork"] += 1
                out.writestr(_zip_info_like(info), converted)
                written.add(name)

            for info in fallback.infolist():
                name = info.filename
                if name not in missing:
                    continue
                original = fallback.read(name)
                entry = by_name[name]
                if name in palettes:
                    converted = original
                    stats["added-fallback-native-palette"] += 1
                else:
                    asset = root / entry["asset"]
                    if not asset.is_file():
                        raise FileNotFoundError(f"{name}: PackBuild asset missing: {asset}")
                    with Image.open(asset) as image:
                        image.load()
                        image_size = image.size
                        pixels = image.convert("RGBA").tobytes()
                    converted = build_rgba32(original, name, pixels, image_size, entry, original_formats)
                    stats["added-fallback-packbuild-artwork"] += 1
                out.writestr(_zip_info_like(info), converted)
                written.add(name)

    with zipfile.ZipFile(output_path) as check, zipfile.ZipFile(base_path) as base:
        names = check.namelist()
        if names[:len(base.namelist())] != base.namelist():
            raise RuntimeError("Upstream resource ordering changed")
        if check.read("portVersion") != PORT_VERSION:
            raise RuntimeError("portVersion changed")
        missing_after = set(by_name) - set(names)
        if missing_after:
            raise RuntimeError(f"Output is missing {len(missing_after)} manifest resources")
        for name in (names[0], names[len(names)//2], names[-1]):
            check.read(name)
    if stats["overlaid-packbuild-artwork"] + stats["added-fallback-packbuild-artwork"] == 0:
        raise RuntimeError("No PackBuild artwork was overlaid")
    stats["manifest-resources"] = len(by_name)
    stats["upstream-resources"] = len(base_names)
    stats["added-from-fallback"] = len(missing)
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", required=True, help="extracted MM-Reloaded HD O2R")
    p.add_argument("--fallback", required=True, help="repository's original 2Ship template O2R")
    p.add_argument("--packbuild", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--decomp-assets", required=True)
    p.add_argument("--port-metadata", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    stats = overlay(a.base, a.fallback, a.output, a.packbuild, a.manifest,
                    a.decomp_assets, a.port_metadata)
    print("MM-Reloaded HD overlay complete:")
    for key, value in sorted(stats.items()):
        print(f"  {key}: {value}")
    print(f"Output: {Path(a.output).stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
