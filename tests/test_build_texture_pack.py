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
    make_common_header,
    original_format_for,
)


def write_decomp(root: Path) -> Path:
    xml_root = root / "assets" / "xml"
    code_xml = xml_root / "code"
    code_xml.mkdir(parents=True)
    (code_xml / "circle_tex.xml").write_text(
        '<Root><File Name="code" OutName="circle_tex">'
        '<Texture Name="sCircleTex" Format="i4" Width="64" Height="64" '
        'Offset="0x138DD0"/></File></Root>',
        encoding="utf-8",
    )
    source = root / "src" / "code"
    source.mkdir(parents=True)
    (source / "z_circle_tex.c").write_text(
        'const u64 gCircleTex[] = {\n'
        '#include "assets/code/circle_tex/circle.i4.inc.c"\n'
        '};\n',
        encoding="utf-8",
    )
    return xml_root


class FreshPackBuildTests(unittest.TestCase):
    def test_common_header_matches_otr_exporter_layout(self):
        header = make_common_header(1)
        self.assertEqual(len(header), 0x40)
        self.assertEqual(header[:4], b"\\x00\\x00\\x00\\x00")
        self.assertEqual(struct.unpack_from("<I", header, 4)[0], OTEX_MAGIC)
        self.assertEqual(struct.unpack_from("<I", header, 8)[0], 1)
        self.assertEqual(struct.unpack_from("<Q", header, 0x0C)[0], 0x00000000DEADBEEF)
        self.assertEqual(header[0x14:0x20], bytes(12))

    def test_overflow_resource_keeps_its_own_format(self):
        formats = {
            "objects/example/exampleTex": "i4",
            "objects/example/exampleTex_Overflow": "i8",
        }
        self.assertEqual(
            original_format_for("objects/example/exampleTex_Overflow", formats), "i8"
        )

    def test_builds_rgba32_raw_and_reconstructs_rgba16_palette(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packbuild = root / "PackBuild"
            image_path = packbuild / "code/fbdemo_circle/gCircleTex.png"
            image_path.parent.mkdir(parents=True)
            rgba = bytes([255, 0, 0, 255] * (64 * 64))
            Image.frombytes("RGBA", (64, 64), rgba).save(image_path)

            palette_path = packbuild / "objects/test/palette.rgb5a1.png"
            palette_path.parent.mkdir(parents=True)
            palette = Image.new("RGBA", (2, 1))
            palette.putdata([(255, 128, 0, 255), (0, 0, 255, 0)])
            palette.save(palette_path)

            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "entries": [
                    {
                        "resource": "code/fbdemo_circle/gCircleTex",
                        "asset": "code/fbdemo_circle/gCircleTex.png",
                        "originalSize": [64, 64],
                        "overrideSize": [64, 64],
                        "styleMode": "material-blend",
                    },
                    {
                        "resource": "objects/test/palette",
                        "asset": "objects/test/palette.rgb5a1.png",
                        "originalSize": [2, 1],
                        "overrideSize": [2, 1],
                        "styleMode": "native-palette-preserve",
                    },
                ]
            }), encoding="utf-8")
            decomp_xml = write_decomp(root / "decomp")
            output = root / "fresh.o2r"

            stats = build_archive(output, packbuild, manifest, decomp_xml)
            self.assertEqual(stats["built-v1-rgba32-from-png"], 1)
            self.assertEqual(stats["built-v0-rgba16-palette-from-png"], 1)

            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), [
                    "portVersion",
                    "code/fbdemo_circle/gCircleTex",
                    "objects/test/palette",
                ])
                self.assertEqual(archive.read("portVersion"), PORT_VERSION)

                rgba_resource = archive.read("code/fbdemo_circle/gCircleTex")
                self.assertEqual(rgba_resource[0], 0)
                self.assertEqual(rgba_resource[1], 1)
                self.assertEqual(struct.unpack_from("<I", rgba_resource, 4)[0], OTEX_MAGIC)
                self.assertEqual(struct.unpack_from("<I", rgba_resource, 8)[0], 1)
                tex_type, width, height, flags = struct.unpack_from("<IIII", rgba_resource, 0x40)
                h_scale, v_scale = struct.unpack_from("<ff", rgba_resource, 0x50)
                data_size = struct.unpack_from("<I", rgba_resource, 0x58)[0]
                self.assertEqual((tex_type, width, height), (1, 64, 64))
                self.assertEqual(flags, TEX_FLAG_LOAD_AS_RAW)
                self.assertAlmostEqual(h_scale, 8.0)  # i4 source row -> RGBA32 row
                self.assertAlmostEqual(v_scale, 1.0)
                self.assertEqual(data_size, 64 * 64 * 4)
                self.assertEqual(rgba_resource[0x5C:], rgba)

                palette_resource = archive.read("objects/test/palette")
                self.assertEqual(struct.unpack_from("<I", palette_resource, 8)[0], 0)
                self.assertEqual(struct.unpack_from("<IIII", palette_resource, 0x40), (2, 2, 1, 4))
                # RGBA5551 is high-byte first: FC01, then 0030.
                self.assertEqual(palette_resource[0x50:], bytes.fromhex("FC01003E"))

    def test_fails_when_a_png_has_no_original_format_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packbuild = root / "PackBuild"
            asset = packbuild / "unknown/texture.png"
            asset.parent.mkdir(parents=True)
            Image.new("RGBA", (2, 2), (1, 2, 3, 255)).save(asset)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"entries": [{
                "resource": "unknown/texture",
                "asset": "unknown/texture.png",
                "originalSize": [2, 2],
                "overrideSize": [2, 2],
            }]}), encoding="utf-8")
            xml_root = root / "decomp" / "assets" / "xml"
            xml_root.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "Missing original N64 formats"):
                build_archive(root / "out.o2r", packbuild, manifest, xml_root)


if __name__ == "__main__":
    unittest.main()
