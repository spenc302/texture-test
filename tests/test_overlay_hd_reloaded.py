import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tools.overlay_hd_reloaded import overlay
from tools.build_texture_pack import PORT_VERSION, OTEX_MAGIC, OTEX_HEADER_SIZE, V0_PIXEL_OFFSET, V1_PIXEL_OFFSET


def resource_v0(texture_type, width, height, pixels):
    header = bytearray(OTEX_HEADER_SIZE)
    struct.pack_into("<I", header, 4, OTEX_MAGIC)
    struct.pack_into("<I", header, 8, 0)
    return bytes(header) + struct.pack("<IIII", texture_type, width, height, len(pixels)) + pixels


def resource_v1(texture_type, width, height, pixels):
    header = bytearray(OTEX_HEADER_SIZE)
    struct.pack_into("<I", header, 4, OTEX_MAGIC)
    struct.pack_into("<I", header, 8, 1)
    fields = struct.pack("<IIIIffI", texture_type, width, height, 1, 2.0, 1.0, len(pixels))
    return bytes(header) + fields + pixels


class OverlayHdReloadedTests(unittest.TestCase):
    def test_preserves_upstream_extras_and_overlays_v1_artwork(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packbuild = root / "PackBuild"
            packbuild.mkdir()
            Image.new("RGBA", (2, 2), (240, 20, 10, 255)).save(packbuild / "art.png")

            manifest = {
                "entries": [{
                    "resource": "scene/test",
                    "asset": "art.png",
                    "originalSize": [2, 2],
                    "overrideSize": [2, 2]
                }]
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            base_path, fallback_path, output_path = root / "base.o2r", root / "fallback.o2r", root / "out.o2r"

            with zipfile.ZipFile(base_path, "w") as z:
                z.writestr("alt/scene/test", resource_v1(1, 2, 2, bytes(16)))
                z.writestr("alt/upstream/extra", b"keep-this-resource")
            with zipfile.ZipFile(fallback_path, "w") as z:
                z.writestr("portVersion", PORT_VERSION)
                z.writestr("scene/test", resource_v0(1, 2, 2, bytes(16)))

            with patch("tools.overlay_hd_reloaded.resolve_original_formats",
                       return_value=({"scene/test": "rgba32"}, 0, 1)):
                stats = overlay(str(base_path), str(fallback_path), str(output_path),
                                str(packbuild), str(manifest_path), str(root), str(root / "unused.json"))

            with zipfile.ZipFile(output_path) as z:
                self.assertEqual(z.namelist(), ["portVersion", "alt/scene/test", "alt/upstream/extra"])
                self.assertEqual(z.read("alt/upstream/extra"), b"keep-this-resource")
                data = z.read("alt/scene/test")
                self.assertEqual(struct.unpack_from("<I", data, 8)[0], 1)
                self.assertEqual(struct.unpack_from("<IIII", data, 64)[:3], (1, 2, 2))
                self.assertEqual(data[V1_PIXEL_OFFSET:], bytes((240, 20, 10, 255)) * 4)
            self.assertEqual(stats["overlaid-packbuild-artwork"], 1)
            self.assertEqual(stats["preserved-upstream-resource"], 1)

    def test_overlays_rgba32_artwork_on_native_i4_hd_resource(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packbuild = root / "PackBuild"
            packbuild.mkdir()
            Image.new("RGBA", (2, 2), (30, 80, 190, 255)).save(packbuild / "art.png")

            manifest = {"entries": [{
                "resource": "message_texture_static/gMessageXLeftTex",
                "asset": "art.png",
                "originalSize": [2, 2],
                "overrideSize": [2, 2]
            }]}
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            base_path, fallback_path, output_path = root / "base.o2r", root / "fallback.o2r", root / "out.o2r"
            # Model the actual upstream failure: a valid OTEX V1 I4 resource.
            with zipfile.ZipFile(base_path, "w") as z:
                z.writestr("alt/message_texture_static/gMessageXLeftTex",
                           resource_v1(5, 2, 2, bytes(4)))
            with zipfile.ZipFile(fallback_path, "w") as z:
                z.writestr("portVersion", PORT_VERSION)
                z.writestr("message_texture_static/gMessageXLeftTex",
                           resource_v0(5, 2, 2, bytes(4)))

            with patch("tools.overlay_hd_reloaded.resolve_original_formats",
                       return_value=({"message_texture_static/gMessageXLeftTex": "i4"}, 0, 1)):
                stats = overlay(str(base_path), str(fallback_path), str(output_path),
                                str(packbuild), str(manifest_path), str(root), str(root / "unused.json"))

            with zipfile.ZipFile(output_path) as z:
                data = z.read("alt/message_texture_static/gMessageXLeftTex")
                self.assertEqual(struct.unpack_from("<I", data, 8)[0], 1)
                self.assertEqual(struct.unpack_from("<IIII", data, 64)[:4], (1, 2, 2, 1))
                self.assertEqual(data[V1_PIXEL_OFFSET:], bytes((30, 80, 190, 255)) * 4)
            self.assertEqual(stats["overlaid-packbuild-artwork"], 1)

    def test_adds_missing_manifest_palette_from_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packbuild = root / "PackBuild"
            packbuild.mkdir()
            Image.new("RGBA", (1, 1), (1, 2, 3, 255)).save(packbuild / "art.png")
            manifest = {"entries": [
                {"resource": "scene/test", "asset": "art.png", "originalSize": [1, 1], "overrideSize": [1, 1]},
                {"resource": "scene/palette", "asset": "palette.png", "originalSize": [1, 1], "overrideSize": [1, 1],
                 "styleMode": "native-palette-preserve"}
            ]}
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            base_path, fallback_path, output_path = root / "base.o2r", root / "fallback.o2r", root / "out.o2r"
            palette = resource_v0(2, 1, 1, b"\\x12\\x34")
            with zipfile.ZipFile(base_path, "w") as z:
                z.writestr("alt/scene/test", resource_v0(1, 1, 1, bytes(4)))
            with zipfile.ZipFile(fallback_path, "w") as z:
                z.writestr("portVersion", PORT_VERSION)
                z.writestr("scene/test", resource_v0(1, 1, 1, bytes(4)))
                z.writestr("scene/palette", palette)

            with patch("tools.overlay_hd_reloaded.resolve_original_formats",
                       return_value=({"scene/test": "rgba32"}, 0, 1)):
                stats = overlay(str(base_path), str(fallback_path), str(output_path),
                                str(packbuild), str(manifest_path), str(root), str(root / "unused.json"))

            with zipfile.ZipFile(output_path) as z:
                self.assertEqual(z.read("alt/scene/palette"), palette)
                self.assertEqual(z.namelist(), ["portVersion", "alt/scene/test", "alt/scene/palette"])
            self.assertEqual(stats["added-fallback-native-palette"], 1)


if __name__ == "__main__":
    unittest.main()
