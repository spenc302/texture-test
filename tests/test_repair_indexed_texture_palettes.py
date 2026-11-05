import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

from tools.repair_indexed_texture_palettes import (
    HDR,
    OTEX_MAGIC,
    choose_palette,
    collect_palette_usage,
    colorize_index_png,
    colorize_from_coarse_grid,
    decode_indexed_pixels,
    decode_rgba16_palette,
    display_list_texture_uses,
    resource_crc64,
)


def make_texture(texture_type, width, height, pixels):
    header = bytearray(HDR)
    struct.pack_into("<I", header, 4, OTEX_MAGIC)
    body = struct.pack("<4I", texture_type, width, height, len(pixels))
    return bytes(header) + body + pixels


def make_display_list(palette_name, texture_name):
    header = bytearray(HDR)
    struct.pack_into("<I", header, 4, int.from_bytes(b"ODLT", "big"))
    body = bytearray(8)  # ucode plus alignment padding

    def set_image(name):
        value = resource_crc64(name)
        return struct.pack("<II", 0x20000000, 0) + struct.pack("<II", value >> 32, value & 0xFFFFFFFF)

    body += set_image(palette_name)
    body += struct.pack("<II", 0xF0000000, 0)  # G_LOADTLUT
    body += set_image(texture_name)
    body += struct.pack("<II", 0xF3000000, 0)  # G_LOADBLOCK
    body += struct.pack("<II", 0xDF000000, 0)  # G_ENDDL
    return bytes(header) + bytes(body)


class IndexedPaletteRepairTests(unittest.TestCase):
    def test_crc64_matches_libultraship_reference_algorithm(self):
        table = []
        for index in range(256):
            value = index << 56
            for _ in range(8):
                value = ((value << 1) ^ 0x42F0E1EBA9EA3693) if value & (1 << 63) else value << 1
                value &= 0xFFFFFFFFFFFFFFFF
            table.append(value)
        crc = 0xFFFFFFFFFFFFFFFF
        for byte in b"objects/object_link_child/gLinkHumanSkinTex":
            crc = table[((crc >> 56) ^ byte) & 0xFF] ^ ((crc << 8) & 0xFFFFFFFFFFFFFFFF)
        self.assertEqual(resource_crc64("objects/object_link_child/gLinkHumanSkinTex"), crc)

    def test_display_list_reports_texture_and_loaded_palette(self):
        palette = "objects/test/gTestTLUT"
        texture = "objects/test/gTestTex"
        uses = display_list_texture_uses(make_display_list(palette, texture))
        self.assertEqual(uses, [(resource_crc64(texture), resource_crc64(palette))])

    def test_archive_usage_maps_multiple_palette_candidates_to_actual_display_list_use(self):
        palette_a = "objects/test/gTestTLUTA"
        palette_b = "objects/test/gTestTLUTB"
        texture = "objects/test/gTestTex"
        dl_a = "objects/test/test_DL_0000"
        dl_b = "objects/test/test_DL_0001"
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "source.o2r"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(palette_a, make_texture(2, 16, 1, bytes(32)))
                archive.writestr(palette_b, make_texture(2, 16, 1, bytes(32)))
                archive.writestr(texture, make_texture(4, 2, 1, b"\x00\x01"))
                archive.writestr(dl_a, make_display_list(palette_a, texture))
                archive.writestr(dl_b, make_display_list(palette_b, texture))
                archive.writestr("objects/test/test_DL_0002", make_display_list(palette_a, texture))
            with zipfile.ZipFile(archive_path) as archive:
                direct, carried, names, palette_names = collect_palette_usage(archive)
            self.assertEqual(direct[texture][palette_a], 2)
            self.assertEqual(direct[texture][palette_b], 1)
            entry = {
                "resource": texture,
                "paletteResources": [
                    {"resource": palette_a, "entries": 16},
                    {"resource": palette_b, "entries": 16},
                ],
            }
            self.assertEqual(choose_palette(entry, direct, carried, names, palette_names), palette_a)

    def test_rgba16_palette_decodes_big_endian_rgb5a1(self):
        # Red opaque and green transparent.
        raw = struct.pack(">HH", 0xF801, 0x07C0)
        palette = decode_rgba16_palette(make_texture(2, 2, 1, raw), 2)
        self.assertEqual(palette.tolist(), [[255, 0, 0, 255], [0, 255, 0, 0]])

    def test_ci8_greyscale_index_png_is_colorized_and_alpha_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "indexed.png"
            Image.fromarray(np.array([[[0, 0, 0, 255], [1, 1, 1, 128]]], dtype=np.uint8), "RGBA").save(path)
            palette = np.array([[255, 0, 0, 255], [0, 255, 0, 255]], dtype=np.uint8)
            colorize_index_png(path, palette, "ci8")
            with Image.open(path) as image:
                self.assertEqual(list(image.getdata()), [(255, 0, 0, 255), (0, 255, 0, 128)])

    def test_transparent_pixels_may_contain_non_greyscale_hidden_rgb(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "transparent-hidden-rgb.png"
            pixels = np.array(
                [[[0, 0, 0, 255], [240, 10, 90, 0]]],
                dtype=np.uint8,
            )
            Image.fromarray(pixels, "RGBA").save(path)
            palette = np.array([[255, 0, 0, 255]], dtype=np.uint8)
            colorize_index_png(path, palette, "ci8")
            with Image.open(path) as image:
                self.assertEqual(list(image.getdata()), [(255, 0, 0, 255), (255, 0, 0, 0)])

    def test_coarse_palette_grid_transfers_colour_and_preserves_artwork_luminance_and_alpha(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "artwork.png"
            # Neutral artwork with two luminance levels; original grid is green.
            pixels = np.array(
                [[[80, 80, 80, 255], [180, 180, 180, 96]]], dtype=np.uint8
            )
            Image.fromarray(pixels, "RGBA").save(path)
            grid = {"w": 1, "h": 1, "grid": [[80, 160, 100, 255]]}
            colorize_from_coarse_grid(path, grid, "material-blend")
            with Image.open(path) as image:
                out = np.asarray(image.convert("RGBA"))
            self.assertEqual(out[..., 3].tolist(), [[255, 96]])
            # Luminance remains within rounding tolerance while chroma becomes green.
            before_y = 0.299 * pixels[..., 0] + 0.587 * pixels[..., 1] + 0.114 * pixels[..., 2]
            after_y = 0.299 * out[..., 0] + 0.587 * out[..., 1] + 0.114 * out[..., 2]
            self.assertTrue(np.all(np.abs(before_y - after_y) <= 1.5))
            self.assertGreater(int(out[0, 0, 1]), int(out[0, 0, 0]))

    def test_subtle_preserve_keeps_more_original_artwork_chroma(self):
        with tempfile.TemporaryDirectory() as temp:
            full = Path(temp) / "full.png"
            subtle = Path(temp) / "subtle.png"
            pixels = np.array([[[180, 80, 80, 255]]], dtype=np.uint8)
            Image.fromarray(pixels, "RGBA").save(full)
            Image.fromarray(pixels, "RGBA").save(subtle)
            grid = {"w": 1, "h": 1, "grid": [[80, 160, 100, 255]]}
            colorize_from_coarse_grid(full, grid, "material-blend")
            colorize_from_coarse_grid(subtle, grid, "subtle-preserve")
            with Image.open(full) as im:
                full_pixel = np.asarray(im)[0, 0, :3].astype(int)
            with Image.open(subtle) as im:
                subtle_pixel = np.asarray(im)[0, 0, :3].astype(int)
            self.assertGreater(abs(int(subtle_pixel[0]) - int(subtle_pixel[1])),
                               abs(int(full_pixel[0]) - int(full_pixel[1])))

    def test_original_ci8_texels_must_match_packbuild_index_map(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "indexed.png"
            Image.fromarray(np.array([[[0, 0, 0, 255], [1, 1, 1, 255]]], dtype=np.uint8), "RGBA").save(path)
            indices = decode_indexed_pixels(bytes([0, 1]), 2, 1, "ci8")
            palette = np.array([[255, 0, 0, 255], [0, 255, 0, 255]], dtype=np.uint8)
            colorize_index_png(path, palette, "ci8", indices)
            with Image.open(path) as image:
                self.assertEqual(list(image.getdata()), [(255, 0, 0, 255), (0, 255, 0, 255)])

    def test_ci4_original_texels_unpack_high_nibble_first(self):
        indices = decode_indexed_pixels(bytes([0x1F]), 2, 1, "ci4")
        self.assertEqual(indices.tolist(), [[1, 15]])

    def test_ci4_index_uses_high_nibble(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "indexed4.png"
            Image.fromarray(np.array([[[0, 0, 0, 255], [255, 255, 255, 255]]], dtype=np.uint8), "RGBA").save(path)
            palette = np.zeros((16, 4), dtype=np.uint8)
            palette[0] = [10, 20, 30, 255]
            palette[15] = [200, 210, 220, 255]
            colorize_index_png(path, palette, "ci4")
            with Image.open(path) as image:
                self.assertEqual(list(image.getdata()), [(10, 20, 30, 255), (200, 210, 220, 255)])


if __name__ == "__main__":
    unittest.main()
