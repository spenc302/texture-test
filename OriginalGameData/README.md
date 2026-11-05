# Original-game texture data and Quest repair notes

This directory contains **metadata only** gathered from the original Majora's Mask resource pipeline. It does not contain Nintendo texture pixels, palette colour tables, ROM data, or extracted binary assets.

## Pinned source data

- [andrewnakas/mm-cleanroom](https://github.com/andrewnakas/mm-cleanroom), commit `fd57f77fa713a70c8d59a0e554e30ff34f842e1f`
  - `games/mm/spec/textures.json`: 13,542 resource records with N64 texture type, native dimensions, a coarse colour grid, optional alpha outline, and palette references where resolved. The colour grids are generated summaries, not original texture pixels.
  - `games/mm/spec/palettes.json`: palette-resource to texture-user relationships.
  - `games/mm/extract_spec.py`: the palette-resolution order used by that source: display-list references, XML `TlutOffset`, name pairing, carried room TLUT, then object-level fallback.
- [2Ship2Harkinian VR source](https://github.com/spenc302/Majoras-Mask-VR/tree/quest-restart-feature), commit `f8bba158c45646519172b2691eeda2304c53357c`
  - `TextureFactory.cpp` has both OTEX V0 and V1 readers. V1 reads texture type, width, height, flags, HByteScale, VPixelScale, and data size.
  - `Texture.h` defines `TEX_FLAG_LOAD_AS_RAW`.
  - `ArchiveManager.cpp` opens `.o2r` and `.zip` as O2R ZIP archives. `.otr` is routed to the separate OTR archive reader when MPQ support is compiled in.
- [zeldaret/mm](https://github.com/zeldaret/mm), pinned in the pack workflow at `710e1a7d50a1f987f5fc55abca37f7fdcc8ee43d`, supplies original XML texture declarations and C display-list format definitions.
- The existing pack workflow also pins the 2Ship texture metadata source at `6750fb02b3ee22a9d3b0ec617cd24fd639191d26`.

## Verified indexed-texture repair scope

The clean-room texture spec and the current `Overrides_Manifest.json` were compared by exact resource path:

| Finding | Count |
|---|---:|
| Texture resources in the pack manifest | 13,542 |
| Original-game texture resources in clean-room spec | 13,542 |
| Original CI4/CI8 resources | 3,067 |
| CI4/CI8 resources marked `paletteDecoded: false` in the pack manifest | **835** |
| Those 835 with a source palette association | **832** |
| Single-palette candidates | 794 |
| Multiple-palette candidates | 38 |
| No palette association found yet | 3 |

The three unresolved resources are:

- `objects/object_owl/gOwlEyeClosedTex`
- `objects/object_owl/gOwlEyeHalfTex`
- `objects/object_owl/gOwlEyeOpenTex`

The 832 mapped candidates and their exact source resource paths, native formats, dimensions, replacement assets, and palette associations are recorded in `indexed_texture_repair_index.json`.

### Repair implementation

The PackBuild PNGs are full-colour RGBA reskin artwork, not greyscale CI index maps. Their RGB values cannot be interpreted as original CI indices. The repair therefore transfers the original grid's spatial chroma into each PackBuild image using a YCbCr colour transfer: PackBuild luminance and alpha remain intact, preserving its linework, shading, and texture detail. `material-blend` uses the original grid chroma fully; `subtle-preserve` blends it at 35% to retain more of the artwork's existing colour. The repair validates each grid's native dimensions against the indexed-texture metadata and validates the associated palette resource in the source O2R before applying it.

### Why the 38 multiple-palette cases need special handling

Some CI textures are reused with different TLUTs in different game contexts. Replacing one such resource with a single RGBA32 image discards that context-dependent palette choice. Do not arbitrarily bake the first palette into those 38 resources. They need per-use review or must retain their indexed/palette-aware runtime behaviour.

For the 794 single-palette candidates, the clean-room `textures.json` palette-aware coarse colour grids are now checked into `indexed_texture_colour_grids.json` and consumed by `tools/repair_indexed_texture_palettes.py`. The grid is intentionally lossy; it restores broad original colour character, not exact original texels. The 38 multi-palette assets remain unchanged, and the three owl-eye resources still need a display-list/XML/object-level palette investigation.

## OTEX and archive compatibility

The current texture builder emits OTEX V1 RGBA32 resources using `TEX_FLAG_LOAD_AS_RAW` and the original-format-derived HByteScale/VPixelScale. The VR source branch contains the V1 texture reader, so V1 itself is supported by that code.

**Keep the final pack extension as `.o2r`. Do not rename it to `.otr`.** In the VR source, `.o2r` is the O2R ZIP archive path. `.otr` can select a different archive reader when MPQ support is enabled. A filename extension change does not convert an archive's internal format.

## Next texture repair pass

1. Use `indexed_texture_repair_index.json` to isolate the 835 known CI fallbacks.
2. Transfer the 794 single-palette clean-room colour grids onto the existing PackBuild artwork, preserving its luminance detail, alpha, and current output dimensions.
3. Leave the 38 multi-palette assets unchanged until their context-specific use is resolved.
4. Investigate the three owl-eye resources using the original display lists and object-local TLUT rules.
5. Rebuild the final V1 O2R and validate its ZIP structure, `portVersion`, resource names/order, OTEX V1 headers, and palette preservation before enabling it in VR.

No GitHub Actions workflow was changed or triggered as part of adding this source metadata.
