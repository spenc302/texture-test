#!/usr/bin/env python3
"""Build the final OTEX V1 O2R directly from PackBuild PNG assets.

RGBA32 replacements use the engine's LOAD_AS_RAW path, so the builder uses the original N64 pixel format and row pitch metadata to calculate raw-import scaling.
The checked-in O2R supplies resource headers and byte-exact native palettes.
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
BYTES_PER_PIXEL = {"rgba32": 4.0, "rgba16": 2.0, "ci8": 1.0, "ia8": 1.0, "ia16": 2.0, "i8": 1.0, "ci4": 0.5, "ia4": 0.5, "i4": 0.5, "yuv16": 2.0}
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
    """Index authoritative XML texture formats using their complete OTR group path."""
    formats: dict[str, str] = {}
    root = Path(xml_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Decomp XML directory does not exist: {root}")

    def add(key: str, fmt: str, source: str) -> None:
        previous = formats.get(key)
        if previous is not None and previous != fmt:
            raise ValueError(f"Conflicting original formats for {key}: {previous} vs {fmt} ({source})")
        formats[key] = fmt

    for path in root.rglob("*.xml"):
        source = path.read_text(encoding="utf-8-sig")
        relative_root = path.resolve().parent.relative_to(root)
        group = relative_root.parts[0] if relative_root.parts else ""
        for fm in re.finditer(r'<File\b([^>]*)>(.*?)</File>', source, re.S):
            attrs, body = fm.group(1), fm.group(2)
            nm = re.search(r'\bName="([^"]+)"', attrs)
            out = re.search(r'\bOutName="([^"]+)"', attrs)
            if not nm:
                continue
            file_name = nm.group(1)
            archive = out.group(1) if out else file_name
            if archive.endswith(".unarchive"):
                archive = archive[:-10]
            if out:
                archive = file_name + "/" + archive
            if group in {"code", "misc", "objects", "overlays"} and not archive.startswith(group + "/"):
                archive = group + "/" + archive

            for tm in re.finditer(r'<Texture\b([^>]*)/?>', body):
                ta = tm.group(1)
                tn = re.search(r'\bName="([^"]+)"', ta)
                tf = re.search(r'\bFormat="([^"]+)"', ta)
                if not tf:
                    continue
                fmt = tf.group(1).lower()
                if fmt not in BYTES_PER_PIXEL:
                    continue
                if tn:
                    add(archive + "/" + tn.group(1), fmt, str(path))
                off = re.search(r'\bOffset="(0x[0-9A-Fa-f]+|[0-9]+)"', ta)
                if off:
                    suffix = f"{int(off.group(1), 0):06X}"
                    for alias in (
                        f"{file_name}_Tex_{suffix}", f"{file_name}Tex_{suffix}",
                        f"{file_name}_TLUT_{suffix}", f"{file_name}TLUT_{suffix}",
                    ):
                        add(archive + "/" + alias, fmt, str(path))

    return formats


def resolve_original_formats(xml_root: str | Path, resources: list[str]) -> tuple[dict[str, str], int]:
    """Resolve manifest resources from XML, then exact C display-list texture symbols."""
    xml_formats = load_original_formats(xml_root)
    c_formats, ambiguous_c_formats = load_c_texture_formats(xml_root)
    resolved: dict[str, str] = {}
    from_c = 0
    missing: list[str] = []

    for resource in resources:
        base_resource = resource[:-len("_Overflow")] if resource.endswith("_Overflow") else resource
        if base_resource in xml_formats:
            resolved[resource] = xml_formats[base_resource]
            continue
        symbol = base_resource.rsplit("/", 1)[-1]
        if symbol in ambiguous_c_formats:
            raise ValueError(f"{resource}: conflicting C display-list formats for texture symbol {symbol}")
        if symbol in c_formats:
            resolved[resource] = c_formats[symbol]
            from_c += 1
        else:
            missing.append(resource)

    if missing:
        raise ValueError(
            f"Missing original N64 formats for {len(missing)} RGBA32 resources; "
            f"examples: {missing[:20]}"
        )
    return resolved, from_c


def _split_c_arguments(arguments: str) -> list[str]:
    """Split a C macro argument list without splitting nested expressions."""
    result: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(arguments):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(arguments[start:index].strip())
            start = index + 1
    result.append(arguments[start:].strip())
    return result


def _c_texture_symbol(expression: str) -> str | None:
    """Return the referenced asset symbol from a C texture-pointer expression."""
    ignored = {
        "const", "volatile", "u8", "s8", "u16", "s16", "u32", "s32",
        "u64", "s64", "uintptr_t", "intptr_t", "void", "struct",
    }
    for token in re.findall(r"\b[A-Za-z_]\w*\b", expression):
        if token not in ignored and not token.startswith("G_"):
            if token.startswith(("g", "s", "object_", "scene_", "room_", "D_")):
                return token
    return None


def load_c_texture_formats(xml_root: str | Path) -> tuple[dict[str, str], set[str]]:
    """Read exact N64 formats from texture-load macros in decompiled C sources.

    XML definitions do not cover every anonymous texture blob. Decompiled
    display lists still specify the original G_IM_FMT and G_IM_SIZ explicitly.
    """
    decomp_root = Path(xml_root).resolve().parent.parent
    source_root = decomp_root / "src"
    if not source_root.is_dir():
        return {}, set()

    format_names = {
        ("G_IM_FMT_RGBA", "G_IM_SIZ_16b"): "rgba16",
        ("G_IM_FMT_RGBA", "G_IM_SIZ_32b"): "rgba32",
        ("G_IM_FMT_YUV", "G_IM_SIZ_16b"): "yuv16",
        ("G_IM_FMT_CI", "G_IM_SIZ_4b"): "ci4",
        ("G_IM_FMT_CI", "G_IM_SIZ_8b"): "ci8",
        ("G_IM_FMT_IA", "G_IM_SIZ_4b"): "ia4",
        ("G_IM_FMT_IA", "G_IM_SIZ_8b"): "ia8",
        ("G_IM_FMT_IA", "G_IM_SIZ_16b"): "ia16",
        ("G_IM_FMT_I", "G_IM_SIZ_4b"): "i4",
        ("G_IM_FMT_I", "G_IM_SIZ_8b"): "i8",
    }
    discovered: dict[str, str] = {}
    ambiguous: set[str] = set()
    call_pattern = re.compile(r"\b(gs|g)DPLoadTexture(Block|Tile)(_4b)?\s*\(")

    for path in source_root.rglob("*.c"):
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        source = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
        for match in call_pattern.finditer(source):
            depth = 1
            cursor = match.end()
            while cursor < len(source) and depth:
                if source[cursor] == "(":
                    depth += 1
                elif source[cursor] == ")":
                    depth -= 1
                cursor += 1
            if depth:
                continue

            args = _split_c_arguments(source[match.end():cursor - 1])
            is_static = match.group(1) == "gs"
            is_four_bit = match.group(3) == "_4b"
            texture_index = 0 if is_static else 1
            format_index = texture_index + 1
            size_index = None if is_four_bit else format_index + 1
            if len(args) <= format_index or (size_index is not None and len(args) <= size_index):
                continue

            fmt_name = args[format_index].strip()
            size_name = "G_IM_SIZ_4b" if is_four_bit else args[size_index].strip()
            pixel_format = format_names.get((fmt_name, size_name))
            symbol = _c_texture_symbol(args[texture_index])
            if pixel_format is None or symbol is None:
                continue

            previous = discovered.get(symbol)
            if previous is not None and previous != pixel_format:
                ambiguous.add(symbol)
                discovered.pop(symbol, None)
            elif symbol not in ambiguous:
                discovered[symbol] = pixel_format

    return discovered, ambiguous

def make_v1_rgba32(
    resource: str,
    template: bytes,
    rgba_pixels: bytes,
    image_size: tuple[int, int],
    manifest_entry: dict[str, Any],
    original_formats: dict[str, str],
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

    original_format = original_formats.get(resource)
    if original_format not in BYTES_PER_PIXEL:
        raise ValueError(f"{resource}: missing/unsupported original N64 texture format {original_format!r}")
    native_width, native_height = manifest_entry["_original_size"]
    h_scale = (float(width) / float(native_width)) * (4.0 / BYTES_PER_PIXEL[original_format])
    v_scale = float(height) / float(native_height)
    if not math.isfinite(h_scale) or not math.isfinite(v_scale) or h_scale <= 0 or v_scale <= 0:
        raise ValueError(f"{resource}: invalid raw-import scale {h_scale}x{v_scale}")
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
    decomp_assets_root: str | Path,
) -> collections.Counter:
    source_archive = Path(source_archive)
    output_archive = Path(output_archive)
    packbuild_root = Path(packbuild_root)
    if source_archive.resolve() == output_archive.resolve():
        raise ValueError("Input and output archives must be different")

    entries = load_manifest(manifest_path)
    original_formats, c_format_resolutions = resolve_original_formats(
        decomp_assets_root, sorted(rgba_entries)
    )
    manifest_by_resource = {entry["resource"]: entry for entry in entries}
    palette_entries = {
        key: value for key, value in manifest_by_resource.items()
        if value.get("styleMode") == "native-palette-preserve"
    }
    rgba_entries = {key: value for key, value in manifest_by_resource.items() if key not in palette_entries}

    stats: collections.Counter = collections.Counter()
    if c_format_resolutions:
        stats["formats-resolved-from-c-source"] = c_format_resolutions
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
                        name, original, rgba_pixels, image_size, entry, original_formats
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
    parser.add_argument("--decomp-assets", required=True, help="zeldaret/mm assets/xml directory")
    parser.add_argument("--packbuild", default=str(pack_root / "PackBuild"), help="PackBuild PNG root")
    parser.add_argument(
        "--manifest", default=str(pack_root / "Overrides_Manifest.json"),
        help="texture manifest",
    )
    args = parser.parse_args()

    stats = build_archive(args.source_o2r, args.output, args.packbuild, args.manifest, args.decomp_assets)
    print("PackBuild -> OTEX V1 LOAD_AS_RAW O2R build complete:")
    for name in sorted(stats):
        print(f"  {name}: {stats[name]}")
    print(f"Output size: {os.path.getsize(args.output):,} bytes")


if __name__ == "__main__":
    main()
