import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from tools.build_texture_pack import (
    OTEX_MAGIC,
    PORT_VERSION,
    TEX_FLAG_LOAD_AS_RAW,
    build_archive,
    load_original_formats,
)


def make_v0(texture_type: int, width: int, height: int, pixels: bytes, padding: bytes = b"") -> bytes:
    header = bytearray(0x40)
    struct.pack_into("<I", header, 4, OTEX_MAGIC)
    struct.pack_into("<I", header, 8, 0)
    data_size = len(pixels)
    body = struct.pack("<IIII", texture_type, width, height, data_size)
    return bytes(header) + body + pixels + padding


class DirectPackBuildTests(unittest.TestCase):
    def test_builds_v1_from_png_and_preserves_palette_exactly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packbuild = root / "PackBuild"
            packbuild.mkdir()
            xml_objects = root / "xml" / "objects"
            xml_objects.mkdir(parents=True)

            image_path = packbuild / "objects/test/sample.rgba32.png"
            image_path.parent.mkdir(parents=True)
            rgba = bytes([
                255, 0, 0, 255, 0, 255, 0, 255,
                0, 0, 255, 255, 255, 255, 255, 255,
                10, 20, 30, 255, 40, 50, 60, 255,
                70, 80, 90, 255, 100, 110, 120, 255,
            ])
            Image.frombytes("RGBA", (4, 2), rgba).save(image_path)

            palette_path = packbuild / "objects/test/palette.rgb5a1.png"
            Image.new("RGBA", (2, 2), (12, 34, 56, 255)).save(palette_path)

            (xml_objects / "test.xml").write_text(
                '<File Name="test"><Texture Name="sample" Format="ci8"/></File>',
                encoding="utf-8",
            )

            image_template = make_v0(1, 4, 2, bytes(4 * 2 * 4))
            # Deliberately include trailing native palette padding. It must
            # survive byte-for-byte rather than being regenerated from PNG.
            palette_template = make_v0(2, 2, 2, b"\x11" * 8, b"\xAA\xBB\xCC\xDD")
            source = root / "source.o2r"
            output = root / "output.o2r"
            with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("portVersion", PORT_VERSION)
                archive.writestr("objects/test/sample", image_template)
                archive.writestr("objects/test/palette", palette_template)

            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "entries": [
                    {
                        "resource": "objects/test/sample",
                        "asset": "objects/test/sample.rgba32.png",
                        "originalSize": [2, 2],
                        "overrideSize": [4, 2],
                        "styleMode": "material-blend",
                    },
                    {
                        "resource": "objects/test/palette",
                        "asset": "objects/test/palette.rgb5a1.png",
                        "originalSize": [2, 2],
                        "overrideSize": [2, 2],
                        "styleMode": "native-palette-preserve",
                    },
                ]
            }), encoding="utf-8")

            stats = build_archive(source, output, packbuild, manifest, root / "xml")
            self.assertEqual(stats["built-v1-rgba32-from-png"], 1)
            self.assertEqual(stats["preserved-native-palette"], 1)

            with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as built:
                self.assertEqual(built.namelist(), original.namelist())
                self.assertEqual(built.read("portVersion"), PORT_VERSION)
                self.assertEqual(built.read("objects/test/palette"), palette_template)

                result = built.read("objects/test/sample")
                self.assertEqual(struct.unpack_from("<I", result, 8)[0], 1)
                texture_type, width, height, flags = struct.unpack_from("<IIII", result, 0x40)
                h_scale, v_scale = struct.unpack_from("<ff", result, 0x50)
                data_size = struct.unpack_from("<I", result, 0x58)[0]
                self.assertEqual((texture_type, width, height), (1, 4, 2))
                self.assertEqual(flags, TEX_FLAG_LOAD_AS_RAW)
                self.assertAlmostEqual(h_scale, 8.0)
                self.assertAlmostEqual(v_scale, 1.0)
                self.assertEqual(data_size, 32)
                self.assertEqual(result[0x5C:], rgba)


    def test_indexes_overlay_group_and_resolves_overflow_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            overlays = root / "overlays"
            overlays.mkdir()
            (overlays / "ovl_Arrow_Fire.xml").write_text(
                '<File Name="ovl_Arrow_Fire">'
                '<Texture Name="gFireArrowTex" Format="i8" Offset="0xAB0"/>'
                '</File>',
                encoding="utf-8",
            )
            formats = load_original_formats(root)
            self.assertEqual(formats["overlays/ovl_Arrow_Fire/gFireArrowTex"], "i8")
            resource = "overlays/ovl_Arrow_Fire/gFireArrowTex_Overflow"
            base = resource[:-len("_Overflow")]
            self.assertEqual(formats[base], "i8")

    def test_rejects_manifest_archive_dimension_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "PackBuild" / "objects/test").mkdir(parents=True)
            (root / "xml" / "objects").mkdir(parents=True)
            Image.new("RGBA", (4, 2)).save(root / "PackBuild/objects/test/sample.rgba32.png")
            (root / "xml/objects/test.xml").write_text(
                '<File Name="test"><Texture Name="sample" Format="ci8"/></File>',
                encoding="utf-8",
            )
            source = root / "source.o2r"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("portVersion", PORT_VERSION)
                archive.writestr("objects/test/sample", make_v0(1, 8, 2, bytes(8 * 2 * 4)))
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"entries": [{
                "resource": "objects/test/sample",
                "asset": "objects/test/sample.rgba32.png",
                "originalSize": [2, 2],
                "overrideSize": [4, 2],
            }]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "template dimensions"):
                build_archive(source, root / "output.o2r", root / "PackBuild", manifest, root / "xml")


if __name__ == "__main__":
    unittest.main()
