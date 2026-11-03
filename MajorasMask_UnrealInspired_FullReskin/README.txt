Majora's Mask VR - Unreal-inspired full visual reskin
====================================================

Installable pack
----------------
MajorasMask_UnrealInspired_Environment.o2r is the ready-to-enable pack for this project's Majora's Mask VR native port. It contains overrides for all 13,542 exported texture resource keys, covering scenes, props, characters, item/UI art, text glyphs, and internal TLUT palette resources. Palette entries retain their original format and pixel data for compatibility. The pack does not include a ROM or the source O2R archive.

Windows: put the .o2r in a `texturepacks` folder beside 2ship.exe. Open System > Mod library, refresh packs, enable this pack under Texture Packs, and restart the game.

Quest: put the .o2r in `MMVR/texturepacks` in shared storage. In the headset, open System > Mod library, connect the MMVR shared folder, refresh, enable the pack, and restart the game.

Look and limitations
--------------------
This is a stylized game-wide reskin inspired by Unreal Engine materials, not a literal Unreal Engine renderer. The native port's texture override format loads color maps; it does not apply the included normal, roughness, height, or ambient-occlusion maps as PBR shader inputs. Those maps are included as editable source material artwork for a future renderer or other 3D tools.

The pack covers all 13,542 of 13,542 texture resources in the supplied export. Color-indexed textures are converted to RGBA replacements, with source palettes decoded where available; 835 indexed textures use an index-based fallback and may need visual refinement. Original alpha/intensity masks are retained. The 855 TLUT palette entries are included as native-format, byte-for-byte copies so they remain compatible with indexed-texture rendering. No in-game smoke test has been performed; validate it in your own installation and keep a normal save before enabling or changing packs.

Source files
------------
- `SourceMaterials`: eight original procedural tileable PBR source sets: mossy stone, forest grass, aged oak, packed earth, moonlit snow, deep water, weathered sandstone, and dark slate. Each set has BaseColor, DirectX Normal, Roughness, Height, and AmbientOcclusion maps.
- `PackBuild`: the generated RGBA32 color-map replacements and resource paths used to build the .o2r.
- `Overrides_Manifest.json`: lists each override and its source material.
- `Preview_Materials.png`: color preview of the material library.
- `Preview_Reskin_Progress.png`: progress overview of the original procedural material sources and coverage.

The material source maps are original procedural artwork. The O2R and mapping target this repository's 2Ship2Harkinian 5.0.1 resource format.


Build pipeline
--------------
The final Quest O2R is built directly from the generated PackBuild PNGs by
`tools/build_texture_pack.py`. It does not run the old archive-wide V0-to-V1
conversion pass. For each RGBA32 replacement, the builder reads the PNG pixels,
uses the matching V0 archive entry only as the OTEX header/resource template,
looks up the original N64 texture format in the pinned zeldaret/mm XML assets,
and writes the final OTEX V1 RAW-import header and pixels in one build step.
The HByteScale and VPixelScale values are calculated from the original N64
format and the original/replacement dimensions.

The 855 entries marked `native-palette-preserve` are not regenerated from their
RGB5A1 PNG inspection assets. Their original archive resources, including any
alignment padding, are copied byte-for-byte. Resource names/order and the
portVersion marker are verified after writing. The workflow runs the direct
builder regression tests before packaging the artifact.
