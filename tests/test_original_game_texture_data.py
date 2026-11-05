import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "MajorasMask_UnrealInspired_FullReskin" / "Overrides_Manifest.json"
REPAIR_INDEX = ROOT / "OriginalGameData" / "indexed_texture_repair_index.json"


class OriginalGameTextureDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.repair = json.loads(REPAIR_INDEX.read_text(encoding="utf-8"))
        cls.manifest_by_resource = {
            entry["resource"]: entry for entry in cls.manifest["entries"]
        }
        cls.repair_by_resource = {
            entry["resource"]: entry for entry in cls.repair["entries"]
        }

    def test_repair_index_covers_all_835_indexed_fallbacks(self):
        summary = self.repair["summary"]
        entries = self.repair["entries"]
        self.assertEqual(summary["indexedFallbackCount"], 835)
        self.assertEqual(len(entries), 835)
        self.assertEqual(len({entry["resource"] for entry in entries}), 835)
        self.assertEqual(summary["paletteResolved"], 835)
        self.assertEqual(summary["singlePalette"], 799)
        self.assertEqual(summary["multiplePalettes"], 36)
        self.assertEqual(summary["unresolved"], 0)

    def test_each_repair_record_matches_the_pack_manifest(self):
        for record in self.repair["entries"]:
            with self.subTest(resource=record["resource"]):
                source = self.manifest_by_resource[record["resource"]]
                self.assertIs(source.get("paletteDecoded"), False)
                self.assertEqual(record["asset"], source["asset"])
                self.assertEqual(record["sourceMaterial"], source["sourceMaterial"])
                self.assertEqual(record["styleMode"], source["styleMode"])
                self.assertEqual(record["overrideSize"], source["overrideSize"])
                self.assertEqual(record["originalSize"], source["originalSize"])
                self.assertIn(record["n64Format"], {"ci4", "ci8"})

                palettes = record["paletteResources"]
                if record["paletteStatus"] == "single":
                    self.assertEqual(len(palettes), 1)
                elif record["paletteStatus"] == "multiple":
                    self.assertGreater(len(palettes), 1)
                else:
                    self.fail(f"Unexpected unresolved palette: {record['resource']}")

    def test_five_ambiguous_object_palettes_are_resolved_from_xml_tlut_offsets(self):
        expected = {
            "objects/object_owl/gOwlEyeClosedTex": "objects/object_owl/gOwlFlyingEyeTLUT",
            "objects/object_owl/gOwlEyeHalfTex": "objects/object_owl/gOwlPerchingEyeTLUT",
            "objects/object_owl/gOwlEyeOpenTex": "objects/object_owl/gOwlPerchingEyeTLUT",
            "objects/object_tk/object_tk_Tex_00A350": "objects/object_tk/object_tk_TLUT_00A300",
            "objects/object_zl1/gChildZelda1Tex_81C8": "objects/object_zl1/gChildZelda1TLUT_70C8",
        }
        for texture, palette in expected.items():
            with self.subTest(texture=texture):
                record = self.repair_by_resource[texture]
                self.assertEqual(record["paletteStatus"], "single")
                self.assertEqual(
                    [item["resource"] for item in record["paletteResources"]],
                    [palette],
                )
                self.assertEqual(
                    record["paletteEvidence"]["method"],
                    "explicit Texture TlutOffset",
                )

    def test_all_36_remaining_multi_palette_resources_keep_every_source_palette(self):
        multi = [
            entry for entry in self.repair["entries"]
            if entry["paletteStatus"] == "multiple"
        ]
        self.assertEqual(len(multi), 36)
        self.assertTrue(all(len(entry["paletteResources"]) >= 2 for entry in multi))


if __name__ == "__main__":
    unittest.main()
