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
| Those 835 with a source palette association | **835** |
| Single-palette candidates after XML review | 799 |
| Multiple-palette candidates requiring context handling | 36 |
| Palette associations still unresolved | 0 |

Five associations previously marked ambiguous are now resolved from explicit `TlutOffset` declarations in the pinned zeldaret XML:

- Owl closed eye → `gOwlFlyingEyeTLUT` at `0x74B8`.
- Owl half-open and open eyes → `gOwlPerchingEyeTLUT` at `0x72B8`. The XML comments that the two owl TLUTs are identical.
- Object TK texture `object_tk_Tex_00A350` → `object_tk_TLUT_00A300` at `0xA300`.
- Child Zelda texture `gChildZelda1Tex_81C8` → `gChildZelda1TLUT_70C8` at `0x70C8`.

The exact evidence is recorded per entry in `indexed_texture_repair_index.json`. These five palette associations are resolved, but their colour grids are not yet present in the checked-in coarse-grid file; they still need colour-data handling before they can be counted as repaired.

### Repair implementation

The PackBuild PNGs are reskin artwork, not recoverable original CI index maps. Their RGB values cannot be interpreted as original CI indices. The current repair transfers the original grid's spatial chroma into each PackBuild image using a YCbCr colour transfer: PackBuild luminance and alpha remain intact, preserving linework, shading, and texture detail. `material-blend` uses the original grid chroma fully; `subtle-preserve` blends it at 35% to retain more of the artwork's existing colour. The repair validates each grid's native dimensions against the indexed-texture metadata and validates the associated palette resource in the source O2R before applying it.

### Why the 36 multiple-palette cases need special handling

The remaining 36 resources are shared scene textures used with different TLUTs in different game contexts. A single baked RGBA32 image cannot preserve both palette outcomes. Do not arbitrarily bake the first palette into those resources. They need context-aware colour handling or a palette-aware runtime representation while retaining the PackBuild artwork.

The 799 single-palette candidates have clean-room palette-aware coarse colour grids checked into `indexed_texture_colour_grids.json` and consumed by `tools/repair_indexed_texture_palettes.py`. The grid is intentionally lossy; it restores broad original colour character, not exact original texels.

## OTEX and archive compatibility

The current texture builder emits OTEX V1 RGBA32 resources using `TEX_FLAG_LOAD_AS_RAW` and the original-format-derived HByteScale/VPixelScale. The VR source branch contains the V1 texture reader, so V1 itself is supported by that code.

**Keep the final pack extension as `.o2r`. Do not rename it to `.otr`.** In the VR source, `.o2r` is the O2R ZIP archive path. `.otr` can select a different archive reader when MPQ support is enabled. A filename extension change does not convert an archive's internal format.

## Next texture repair pass

1. Generate or otherwise establish the five missing colour grids using their now-explicit palette associations.
2. Resolve the 36 shared scene textures without collapsing their context-specific TLUT behaviour.
3. Confirm the colour-transfer strength preserves the PackBuild art and the user's intended colours.
4. Rebuild the final V1 O2R and validate its ZIP structure, `portVersion`, resource names/order, OTEX V1 headers, and palette preservation before enabling it in VR.

No GitHub Actions workflow was changed or triggered as part of recording these XML-backed palette resolutions.
