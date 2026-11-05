#!/usr/bin/env python3
"""Restore original colour character to indexed-texture PackBuild artwork.

PackBuild PNGs are reskinned RGBA artwork, not recoverable palette-index maps.
This pass transfers the palette-decoded original coarse colour grid onto the
artwork chroma while retaining the artwork luminance, detail, and alpha.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import struct
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

HDR = 0x40
OTEX_MAGIC = 0x4F544558
OTEX_TYPE_RGBA32 = 1
OTEX_TYPE_RGBA16 = 2
OTEX_TYPE_CI4 = 3
OTEX_TYPE_CI8 = 4
CI_BITS = {"ci4": 4, "ci8": 8}
POLY = 0x42F0E1EBA9EA3693


def resource_crc64(name: str) -> int:
    crc = 0xFFFFFFFFFFFFFFFF
    for byte in name.encode():
        crc ^= byte << 56
        for _ in range(8):
            crc = ((crc << 1) ^ POLY) & 0xFFFFFFFFFFFFFFFF if crc & (1 << 63) else (crc << 1) & 0xFFFFFFFFFFFFFFFF
    return crc


def resource_type(data: bytes) -> str | None:
    if len(data) < HDR:
        return None
    return struct.unpack_from("<I", data, 4)[0].to_bytes(4, "big").decode("latin1")


def display_list_texture_uses(data: bytes):
    """Return (texture hash, currently loaded TLUT hash) pairs from an ODLT."""
    position = HDR + 4
    while position % 8:
        position += 1
    last_image = None
    tlut = None
    uses = []
    while position + 8 <= len(data):
        word0, _word1 = struct.unpack_from("<II", data, position)
        opcode = word0 >> 24
        if opcode == 0x20:  # G_SETTIMG_OTR_HASH; hash follows as two words
            if position + 16 > len(data):
                break
            hi, lo = struct.unpack_from("<II", data, position + 8)
            last_image = (hi << 32) | lo
            position += 16
            continue
        if opcode in (0x31, 0x32, 0x33, 0x35, 0x36):
            position += 16
            continue
        if opcode == 0xF0 and last_image is not None:  # G_LOADTLUT
            tlut = last_image
        elif opcode in (0xF3, 0xF4) and last_image is not None:
            uses.append((last_image, tlut))
        elif opcode == 0xDF:
            break
        position += 8
    return uses


def collect_palette_usage(archive: zipfile.ZipFile):
    names = [name for name in archive.namelist() if not name.endswith("/")]
    hash_to_name = {resource_crc64(name): name for name in names}
    direct = collections.defaultdict(collections.Counter)
    carried = collections.defaultdict(collections.Counter)
    last_palette_by_room = {}
    palette_names = set()

    for name in sorted(names):
        data = archive.read(name)
        kind = resource_type(data)
        if kind == "OTEX" and len(data) >= HDR + 16:
            texture_type = struct.unpack_from("<I", data, HDR)[0]
            if texture_type == OTEX_TYPE_RGBA16:
                palette_names.add(name)
        if kind != "ODLT":
            continue
        room = re.sub(r"(DL|Set)_?[0-9A-Fa-f]{4,}.*$", "", name)
        for texture_hash, palette_hash in display_list_texture_uses(data):
            texture = hash_to_name.get(texture_hash)
            if texture is None:
                continue
            if palette_hash is not None:
                palette = hash_to_name.get(palette_hash)
                if palette is not None:
                    direct[texture][palette] += 1
                    last_palette_by_room[room] = palette
            elif room in last_palette_by_room:
                carried[texture][last_palette_by_room[room]] += 1
    return direct, carried, names, palette_names


def decode_rgba16_palette(data: bytes, expected_entries: int | None = None) -> np.ndarray:
    if resource_type(data) != "OTEX" or len(data) < HDR + 16:
        raise ValueError("palette resource is not a complete OTEX")
    texture_type, width, height, data_size = struct.unpack_from("<4I", data, HDR)
    if texture_type != OTEX_TYPE_RGBA16:
        raise ValueError(f"palette resource has OTEX type {texture_type}, expected RGBA16")
    entries = width * height
    if expected_entries is not None and entries != expected_entries:
        raise ValueError(f"palette has {entries} entries, index metadata expects {expected_entries}")
    raw = data[HDR + 16:HDR + 16 + data_size]
    if data_size != entries * 2 or len(raw) != data_size:
        raise ValueError(f"palette payload size {data_size} does not match {entries} RGBA16 entries")
    words = np.frombuffer(raw, dtype=">u2").astype(np.uint16)
    rgba = np.empty((entries, 4), dtype=np.uint8)
    rgba[:, 0] = (((words >> 11) & 31) * 255 // 31).astype(np.uint8)
    rgba[:, 1] = (((words >> 6) & 31) * 255 // 31).astype(np.uint8)
    rgba[:, 2] = (((words >> 1) & 31) * 255 // 31).astype(np.uint8)
    rgba[:, 3] = ((words & 1) * 255).astype(np.uint8)
    return rgba


def choose_palette(entry, direct, carried, source_names, palette_names):
    resource = entry["resource"]
    candidates = [item["resource"] for item in entry.get("paletteResources", [])]
    if not candidates:
        # Recover code-loaded or otherwise unindexed associations from the
        # actual display-list use first; only then fall back to a unique local TLUT.
        observed = direct.get(resource, collections.Counter())
        if not observed:
            observed = carried.get(resource, collections.Counter())
        candidates = [name for name in observed if name in palette_names]
        if not candidates:
            folder = resource.rsplit("/", 1)[0]
            local = []
            for name in palette_names:
                if name.rsplit("/", 1)[0] != folder:
                    continue
                data_type = name.rsplit("/", 1)[-1].upper()
                if "TLUT" in data_type or "PAL" in data_type:
                    local.append(name)
            candidates = sorted(local)

    counts = direct.get(resource, collections.Counter())
    if not counts:
        counts = carried.get(resource, collections.Counter())
    ranked = [(name, counts.get(name, 0)) for name in candidates]
    ranked = [(name, count) for name, count in ranked if count > 0]
    if ranked:
        ranked.sort(key=lambda pair: (-pair[1], candidates.index(pair[0])))
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            raise ValueError(f"{resource}: tied original palette usage: {ranked}")
        return ranked[0][0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(f"{resource}: no original palette association or unique local TLUT")
    raise ValueError(f"{resource}: cannot resolve among original palettes {candidates}")


def decode_indexed_pixels(data: bytes, width: int, height: int, n64_format: str) -> np.ndarray:
    if n64_format not in CI_BITS:
        raise ValueError(f"unsupported indexed format {n64_format}")
    count = width * height
    if n64_format == "ci8":
        if len(data) != count:
            raise ValueError(f"CI8 payload has {len(data)} bytes, expected {count}")
        return np.frombuffer(data, dtype=np.uint8).reshape(height, width).copy()
    if len(data) != (count + 1) // 2:
        raise ValueError(f"CI4 payload has {len(data)} bytes, expected {(count + 1) // 2}")
    packed = np.frombuffer(data, dtype=np.uint8)
    indices = np.empty(len(packed) * 2, dtype=np.uint8)
    indices[0::2] = packed >> 4
    indices[1::2] = packed & 0x0F
    return indices[:count].reshape(height, width)


def colorize_index_png(
    png_path: Path,
    palette: np.ndarray,
    n64_format: str,
    source_indices: np.ndarray | None = None,
) -> None:
    if n64_format not in CI_BITS:
        raise ValueError(f"{png_path}: unsupported indexed format {n64_format}")
    with Image.open(png_path) as image:
        image.load()
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    # RGB values beneath fully transparent pixels are undefined in PNGs and
    # may contain arbitrary data. Validate only pixels that contribute to the
    # visible image; their hidden RGB is discarded when the palette is applied.
    visible = rgba[..., 3] > 0
    if not (
        np.array_equal(rgba[..., 0][visible], rgba[..., 1][visible])
        and np.array_equal(rgba[..., 0][visible], rgba[..., 2][visible])
    ):
        raise ValueError(f"{png_path}: visible pixels are not a greyscale palette-index map")
    if source_indices is not None:
        scales = (1, 16, 17) if n64_format == "ci4" else (1,)
        matches_source = False
        for scale in scales:
            expected = Image.fromarray((source_indices.astype(np.uint16) * scale).astype(np.uint8), "L")
            expected = np.asarray(expected.resize((rgba.shape[1], rgba.shape[0]), Image.Resampling.NEAREST))
            if np.array_equal(rgba[..., 0][visible], expected[visible]):
                matches_source = True
                break
        if not matches_source:
            raise ValueError(f"{png_path}: visible greyscale values do not match original indexed texels")
        indices = Image.fromarray(source_indices.astype(np.uint8), "L").resize(
            (rgba.shape[1], rgba.shape[0]), Image.Resampling.NEAREST
        )
        indices = np.asarray(indices, dtype=np.int64)
    else:
        values = rgba[..., 0].astype(np.uint16)
        indices = (values >> 4).astype(np.int64) if n64_format == "ci4" else values.astype(np.int64)
    # CI8 resources can use banked 16-entry TLUTs; repeat the native palette
    # exactly as the clean-room decoder does (index i -> entry i modulo N).
    indices %= len(palette)
    output = palette[indices].copy()
    output[..., 3] = ((output[..., 3].astype(np.uint16) * rgba[..., 3].astype(np.uint16) + 127) // 255).astype(np.uint8)
    Image.fromarray(output, "RGBA").save(png_path)


def colorize_from_coarse_grid(png_path: Path, grid_data: dict, style_mode: str) -> None:
    """Transfer original palette colour from a coarse grid, preserving PackBuild detail."""
    grid = np.asarray(grid_data.get("grid"), dtype=np.float32)
    if grid.ndim != 2 or grid.shape[1] != 4 or not len(grid):
        raise ValueError(f"{png_path}: invalid original colour grid")
    side = int(round(len(grid) ** 0.5))
    if side * side != len(grid):
        raise ValueError(f"{png_path}: colour grid is not square")
    grid = grid.reshape(side, side, 4)
    with Image.open(png_path) as image:
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    colour = np.asarray(
        Image.fromarray(np.clip(grid[..., :3], 0, 255).astype(np.uint8), "RGB").resize(
            (rgba.shape[1], rgba.shape[0]), Image.Resampling.BILINEAR
        ), dtype=np.float32
    )
    art = rgba[..., :3].astype(np.float32)
    # YCbCr transfer: keep the PackBuild luminance (linework, highlights,
    # texture and shading) and transfer only the original palette's chroma.
    def to_ycc(rgb):
        y = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        cb = (rgb[..., 2] - y) / 1.772
        cr = (rgb[..., 0] - y) / 1.402
        return y, cb, cr
    ay, acb, acr = to_ycc(art)
    _gy, gcb, gcr = to_ycc(colour)
    strength = {"material-blend": 1.0, "subtle-preserve": 0.35}.get(style_mode)
    if strength is None:
        raise ValueError(f"{png_path}: unknown style mode {style_mode!r}")
    # Retain a little of the artwork's own colour to avoid flattening intentional accents.
    cb = acb * (1.0 - strength) + gcb * strength
    cr = acr * (1.0 - strength) + gcr * strength
    red = ay + 1.402 * cr
    blue = ay + 1.772 * cb
    green = (ay - 0.299 * red - 0.114 * blue) / 0.587
    output = np.stack([red, green, blue], axis=-1)
    output = np.clip(np.rint(output), 0, 255).astype(np.uint8)
    result = np.dstack([output, rgba[..., 3]])
    Image.fromarray(result, "RGBA").save(png_path)


def repair_indexed_textures(source_o2r: str | Path, packbuild_root: str | Path, index_path: str | Path, texture_spec_path: str | Path):
    with open(index_path, "r", encoding="utf-8") as stream:
        index = json.load(stream)
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{index_path}: missing entries array")

    with open(texture_spec_path, "r", encoding="utf-8") as stream:
        texture_spec = json.load(stream).get("textures", {})
    stats = collections.Counter()
    with zipfile.ZipFile(source_o2r, "r") as archive:
        direct, carried, source_names, palette_names = collect_palette_usage(archive)
        source_name_set = set(source_names)
        for entry in entries:
            resource = entry["resource"]
            fmt = entry.get("n64Format")
            if fmt not in CI_BITS:
                stats["skipped-non-indexed"] += 1
                continue
            png_path = Path(packbuild_root) / entry["asset"]
            if not png_path.is_file():
                raise FileNotFoundError(f"{resource}: missing PackBuild PNG {png_path}")
            if resource not in source_name_set:
                raise ValueError(f"{resource}: missing source texture resource")
            source = archive.read(resource)
            if resource_type(source) != "OTEX" or len(source) < HDR + 16:
                raise ValueError(f"{resource}: source is not an OTEX texture")
            texture_type, width, height, data_size = struct.unpack_from("<4I", source, HDR)

            # The checked-in O2R is an RGBA32-converted template, even when
            # the repair index records the original N64 CI4/CI8 format.
            # Only decode source indices when the archive actually retains
            # an indexed OTEX payload; otherwise use the greyscale PackBuild
            # index map directly and still colour it with the original TLUT.
            source_indices = None
            if texture_type in (OTEX_TYPE_CI4, OTEX_TYPE_CI8):
                if entry.get("originalSize") != [width, height]:
                    raise ValueError(
                        f"{resource}: index originalSize {entry.get('originalSize')} "
                        f"does not match native indexed source dimensions {[width, height]}"
                    )
                expected_type = OTEX_TYPE_CI4 if fmt == "ci4" else OTEX_TYPE_CI8
                if texture_type != expected_type:
                    raise ValueError(
                        f"{resource}: source OTEX indexed type {texture_type} does not match {fmt}"
                    )
                expected_bytes = (width * height + (1 if fmt == "ci4" else 0)) // (2 if fmt == "ci4" else 1)
                if data_size != expected_bytes:
                    raise ValueError(
                        f"{resource}: source indexed payload has {data_size} bytes, expected {expected_bytes}"
                    )
                source_pixels = source[HDR + 16:HDR + 16 + data_size]
                source_indices = decode_indexed_pixels(source_pixels, width, height, fmt)
            elif texture_type == OTEX_TYPE_RGBA32:
                if entry.get("overrideSize") != [width, height]:
                    raise ValueError(
                        f"{resource}: index overrideSize {entry.get('overrideSize')} "
                        f"does not match converted RGBA32 template dimensions {[width, height]}"
                    )
                if data_size != width * height * 4:
                    raise ValueError(
                        f"{resource}: source RGBA32 payload has {data_size} bytes, expected {width * height * 4}"
                    )
            else:
                raise ValueError(
                    f"{resource}: unsupported source OTEX type {texture_type} for indexed {fmt} repair"
                )

            palette_resources = entry.get("paletteResources", [])
            # Shared scene textures have more than one legitimate in-game TLUT.
            # A single baked RGBA32 replacement cannot represent both contexts.
            # Validate every referenced native palette, then leave the user's
            # PackBuild artwork untouched rather than arbitrarily recolouring it.
            if len(palette_resources) > 1:
                for palette_ref in palette_resources:
                    palette_name = palette_ref["resource"]
                    if palette_name not in source_name_set or palette_name not in palette_names:
                        raise ValueError(f"{resource}: referenced palette {palette_name} is absent or not RGBA16 in source O2R")
                    decode_rgba16_palette(archive.read(palette_name), palette_ref.get("entries"))
                with Image.open(png_path) as image:
                    if list(image.size) != entry.get("overrideSize"):
                        raise ValueError(
                            f"{resource}: PackBuild dimensions {list(image.size)} "
                            f"do not match index overrideSize {entry.get('overrideSize')}"
                        )
                stats["preserved-multiple-palette-artwork"] += 1
                print(f"PRESERVED PACKBUILD ARTWORK (multiple TLUT contexts): {resource}")
                continue

            try:
                palette_name = choose_palette(entry, direct, carried, source_names, palette_names)
            except ValueError as exc:
                stats["unresolved-palette"] += 1
                print(f"UNRESOLVED PALETTE: {exc}")
                continue
            if palette_name not in source_name_set:
                raise ValueError(f"{resource}: selected palette {palette_name} is absent from source O2R")
            expected_entries = next(
                (item.get("entries") for item in palette_resources if item["resource"] == palette_name),
                None,
            )
            # Decode and validate the actual native RGBA16 TLUT even when a
            # spatial grid is unavailable. Never invent a spatial index map
            # from a palette strip: it contains colours, not texel positions.
            decode_rgba16_palette(archive.read(palette_name), expected_entries)
            with Image.open(png_path) as image:
                if list(image.size) != entry.get("overrideSize"):
                    raise ValueError(
                        f"{resource}: PackBuild dimensions {list(image.size)} "
                        f"do not match index overrideSize {entry.get('overrideSize')}"
                    )
            # PackBuild is authored RGBA artwork, not a recoverable CI index map.
            # Only apply chroma transfer when the pinned source has a spatial
            # palette-decoded grid for this exact texture.
            grid_data = texture_spec.get(resource)
            if grid_data is None:
                stats["preserved-no-grid-artwork"] += 1
                print(f"PRESERVED PACKBUILD ARTWORK (no spatial colour grid): {resource}")
                continue
            if [grid_data.get("w"), grid_data.get("h")] != entry.get("originalSize"):
                stats["preserved-invalid-grid-artwork"] += 1
                print(f"PRESERVED PACKBUILD ARTWORK (grid dimensions do not match): {resource}")
                continue
            try:
                colorize_from_coarse_grid(png_path, grid_data, entry.get("styleMode", "material-blend"))
            except ValueError as exc:
                stats["unresolved-colour-grid"] += 1
                print(f"UNRESOLVED COLOUR GRID: {exc}")
                continue
            stats["repaired"] += 1
            stats["ci4" if fmt == "ci4" else "ci8"] += 1
            stats["single-palette"] += 1

    stats["indexed-candidates"] = len(entries)
    stats["preserved-artwork"] = (
        stats["preserved-multiple-palette-artwork"]
        + stats["preserved-no-grid-artwork"]
        + stats["preserved-invalid-grid-artwork"]
    )
    stats["accounted-for"] = stats["repaired"] + stats["preserved-artwork"]
    stats["unrepaired"] = stats["indexed-candidates"] - stats["accounted-for"]
    if stats["unrepaired"]:
        print(f"ERROR: {stats['unrepaired']} indexed textures remain unaccounted for")
    return stats


def main():
    root = Path(__file__).resolve().parents[1]
    pack_root = root / "MajorasMask_UnrealInspired_FullReskin"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-o2r", default=str(pack_root / "MajorasMask_UnrealInspired_Environment.o2r"))
    parser.add_argument("--packbuild", default=str(pack_root / "PackBuild"))
    parser.add_argument("--index", default=str(root / "OriginalGameData/indexed_texture_repair_index.json"))
    parser.add_argument("--texture-spec", default=str(root / "OriginalGameData/indexed_texture_colour_grids.json"))
    args = parser.parse_args()
    stats = repair_indexed_textures(args.source_o2r, args.packbuild, args.index, args.texture_spec)
    print("Original indexed-palette repair:")
    for key in sorted(stats):
        print(f"  {key}: {stats[key]}")


if __name__ == "__main__":
    main()
