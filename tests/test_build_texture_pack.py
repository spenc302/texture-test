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
    load_port_texture_formats,
    resolve_original_formats,
)


def make_v0(texture_type: int, width: int, height: int, pixels: bytes, padding: bytes = b"") -> bytes:
    header = bytearray(0x40)
    struct.pack_into("<I", header, 4, OTEX_MAGIC)
    struct.pack_into("<I", header, 8, 0)
    data_size = len(pixels)
    body = struct.pack("<IIII", texture_type, width, height, data_size)
    return bytes(header) + body + pixels + padding


def make_decomp_xml(root: Path, fmt: str = "rgba16") -> Path:
    xml = root / "objects"
    xml.mkdir(parents=True, exist_ok=True)
    (xml / "test.xml").write_text(
        f'<Root><File Name="test"><Texture Name="sample" Format="{fmt}" Offset="0x10" /></File></Root>',
        encoding="utf-8",
    )
    return root


class DirectPackBuildTests(unittest.TestCase):
    def test_2ship_metadata_maps_all_zapd_texture_types_and_checks_dimensions(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata_path = Path(temp) / "2ship-metadata.json"
            type_formats = {
                1: "rgba32", 2: "rgba16", 3: "ci4", 4: "ci8", 5: "i4",
                6: "i8", 7: "ia4", 8: "ia8", 9: "ia16",
            }
            metadata = {
                f"test/resource_{texture_type}": {
                    "textureType": texture_type,
                    "textureWidth": 32,
                    "textureHeight": 16,
                }
                for texture_type in type_formats
            }
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            resources = list(metadata)
            formats = load_port_texture_formats(
                metadata_path,
                resources,
                {resource: (32, 16) for resource in resources},
            )
            self.assertEqual(
                formats,
                {resource: type_formats[index + 1] for index, resource in enumerate(resources)},
            )
            with self.assertRaisesRegex(ValueError, "do not match Overrides_Manifest originalSize"):
                load_port_texture_formats(
                    metadata_path, [resources[0]], {resources[0]: (64, 16)}
                )

    def test_builds_v1_from_png_and_preserves_palette_exactly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packbuild = root / "PackBuild"
            packbuild.mkdir()

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

            decomp = make_decomp_xml(root / "decomp")
            stats = build_archive(source, output, packbuild, manifest, decomp)
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
                self.assertEqual(flags, 0x01)
                self.assertAlmostEqual(h_scale, 4.0)  # 2x width scale * RGBA16->RGBA32 byte scale
                self.assertAlmostEqual(v_scale, 1.0)
                self.assertEqual(data_size, 32)
                self.assertEqual(result[0x5C:], rgba)


    def test_overlay_xml_uses_full_overlays_resource_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "decomp" / "assets" / "xml"
            overlays = root / "overlays"
            overlays.mkdir(parents=True)
            (overlays / "ovl_Arrow_Fire.xml").write_text(
                '<Root><File Name="ovl_Arrow_Fire">'
                '<Texture Name="gFireArrowTex" Format="i8" Offset="0xAB0"/>'
                '</File></Root>',
                encoding="utf-8",
            )
            formats = load_original_formats(root)
            self.assertEqual(formats["overlays/ovl_Arrow_Fire/gFireArrowTex"], "i8")

    def test_overflow_resource_resolves_to_base_xml_texture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "decomp" / "assets" / "xml"
            objects = root / "objects"
            objects.mkdir(parents=True)
            (objects / "test.xml").write_text(
                '<Root><File Name="test"><Texture Name="gTexture" Format="rgba16"/></File></Root>',
                encoding="utf-8",
            )
            resolved, from_c, from_port = resolve_original_formats(
                root, ["objects/test/gTexture_Overflow"]
            )
            self.assertEqual(resolved["objects/test/gTexture_Overflow"], "rgba16")
            self.assertEqual(from_c, 0)
            self.assertEqual(from_port, 0)

    def test_c_display_list_format_is_used_when_xml_has_no_texture_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            decomp = Path(temp) / "decomp"
            xml = decomp / "assets" / "xml"
            (xml / "objects").mkdir(parents=True)
            (xml / "objects" / "empty.xml").write_text(
                '<Root><File Name="empty"></File></Root>', encoding="utf-8"
            )
            source = decomp / "src" / "code"
            source.mkdir(parents=True)
            (source / "test.c").write_text(
                'gsDPLoadTextureBlock(gSceneTexture, G_IM_FMT_IA, G_IM_SIZ_8b, '
                '32, 32, 0, G_TX_WRAP, G_TX_WRAP, 5, 5, G_TX_NOLOD, G_TX_NOLOD);',
                encoding="utf-8",
            )
            resolved, from_c = resolve_original_formats(
                xml, ["scenes/nonmq/TEST/gSceneTexture"]
            )
            self.assertEqual(resolved["scenes/nonmq/TEST/gSceneTexture"], "ia8")
            self.assertEqual(from_c, 1)
            self.assertEqual(from_port, 0)

    def test_rejects_manifest_archive_dimension_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "PackBuild" / "objects/test").mkdir(parents=True)
            Image.new("RGBA", (4, 2)).save(root / "PackBuild/objects/test/sample.rgba32.png")
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
            decomp = make_decomp_xml(root / "decomp")
            with self.assertRaisesRegex(ValueError, "template dimensions"):
                build_archive(source, root / "output.o2r", root / "PackBuild", manifest, decomp)


if __name__ == "__main__":
    unittest.main()
