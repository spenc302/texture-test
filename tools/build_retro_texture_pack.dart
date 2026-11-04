import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:archive/archive.dart';
import 'package:archive/archive_io.dart';
import 'package:image/image.dart' as img;
import 'package:path/path.dart' as p;
import 'package:retro/features/create/create_finish/create_finish_viewmodel.dart'
    show buildTextureFromImage;
import 'package:retro/models/texture_manifest_entry.dart';
import 'package:retro/otr/types/texture.dart';

const int _otexMagic = 0x4F544558;
const List<int> _resourceId = <int>[
  0xEF, 0xBE, 0xAD, 0xDE, 0xEF, 0xBE, 0xAD, 0xDE,
];

Map<String, String> _parseArgs(List<String> args) {
  final result = <String, String>{};
  for (var i = 0; i < args.length; i++) {
    final key = args[i];
    if (!key.startsWith('--') || i + 1 >= args.length) {
      throw ArgumentError('Invalid argument near ' + key);
    }
    result[key.substring(2)] = args[++i];
  }
  return result;
}

void _validateTexture(Uint8List data, String resource) {
  if (data.length < 0x50) {
    throw FormatException(resource + ': truncated OTEX resource');
  }
  final view = ByteData.sublistView(data);
  if (view.getUint32(4, Endian.little) != _otexMagic) {
    throw FormatException(resource + ': invalid OTEX FourCC');
  }
  for (var i = 0; i < _resourceId.length; i++) {
    if (data[0x0C + i] != _resourceId[i]) {
      throw FormatException(resource + ': invalid 64-bit resource identifier');
    }
  }
  if (view.getUint32(0x14, Endian.little) != 0 || data[0x18] != 1) {
    throw FormatException(resource + ': invalid Retro common resource header');
  }
  if (data[0] != 0 || data[1] != 0 || data[2] != 0 || data[3] != 0) {
    throw FormatException(resource + ': invalid endianness/header prefix');
  }

  final gameVersion = view.getUint32(8, Endian.little);
  final textureTypeValue = view.getUint32(0x40, Endian.little);
  final width = view.getUint32(0x44, Endian.little);
  final height = view.getUint32(0x48, Endian.little);
  final textureType = TextureType.values.firstWhere(
    (type) => type.value == textureTypeValue,
    orElse: () => TextureType.Error,
  );
  if (textureType == TextureType.Error || width == 0 || height == 0) {
    throw FormatException(resource + ': invalid texture type or dimensions');
  }

  final expectedSize =
      (width * height * textureType.pixelMultiplier).toInt();
  late final int pixelOffset;
  late final int dataSize;
  if (gameVersion == 0) {
    pixelOffset = 0x50;
    dataSize = view.getUint32(0x4C, Endian.little);
  } else if (gameVersion == 1) {
    pixelOffset = 0x5C;
    final flags = view.getUint32(0x4C, Endian.little);
    final hScale = view.getFloat32(0x50, Endian.little);
    final vScale = view.getFloat32(0x54, Endian.little);
    dataSize = view.getUint32(0x58, Endian.little);
    if ((flags & 1) == 0 ||
        !hScale.isFinite ||
        !vScale.isFinite ||
        hScale <= 0 ||
        vScale <= 0) {
      throw FormatException(resource + ': invalid raw texture flags or scale');
    }
  } else {
    throw FormatException(resource + ': unsupported texture resource version ' + gameVersion.toString());
  }

  if (dataSize != expectedSize || data.length != pixelOffset + dataSize) {
    throw FormatException(resource + ': texture payload length does not match its format');
  }
}

Future<void> main(List<String> args) async {
  final options = _parseArgs(args);
  final sourceRoot = Directory(options['source-root'] ?? '');
  final metadataFile = File(options['metadata'] ?? '');
  final outputPath = options['output'] ?? '';
  final limit = int.tryParse(options['limit'] ?? '');

  if (!sourceRoot.existsSync() ||
      !File(p.join(sourceRoot.path, 'Retro_Source_Manifest.json')).existsSync() ||
      !metadataFile.existsSync() ||
      outputPath.isEmpty) {
    throw ArgumentError(
      'Required: --source-root <pack folder> --metadata <Retro manifest> --output <o2r path>',
    );
  }

  final overrides = jsonDecode(
    await File(p.join(sourceRoot.path, 'Retro_Source_Manifest.json')).readAsString(),
  ) as Map<String, dynamic>;
  final entries = (overrides['entries'] as List).cast<Map<String, dynamic>>();
  final metadata = jsonDecode(await metadataFile.readAsString())
      as Map<String, dynamic>;

  if (entries.isEmpty || metadata.isEmpty) {
    throw FormatException('The source or Retro texture manifest is empty');
  }
  if (entries.length != metadata.length) {
    throw FormatException(
      'Filtered source manifest and Retro metadata counts differ: ' +
      entries.length.toString() + ' vs ' + metadata.length.toString(),
    );
  }

  final outputFile = File(outputPath);
  await outputFile.parent.create(recursive: true);
  if (outputFile.existsSync()) {
    await outputFile.delete();
  }

  final encoder = ZipFileEncoder()..create(outputPath);
  final builtResources = <String>{};
  var builtV0 = 0;
  var builtV1 = 0;
  var built = 0;

  try {
    for (final entry in entries) {
      final resource = entry['resource'] as String;
      final rawMeta = metadata[resource];
      if (rawMeta == null) {
        throw FormatException(resource + ': missing canonical Retro metadata');
      }
      if (limit != null && built >= limit) {
        break;
      }
      if (!builtResources.add(resource)) {
        throw FormatException('Duplicate resource key: ' + resource);
      }

      final meta = rawMeta as Map<String, dynamic>;
      final originalSize = (entry['originalSize'] as List).cast<int>();
      final overrideSize = (entry['overrideSize'] as List).cast<int>();
      final textureWidth = meta['textureWidth'] as int;
      final textureHeight = meta['textureHeight'] as int;
      if (originalSize[0] != textureWidth ||
          originalSize[1] != textureHeight) {
        throw FormatException(resource + ': canonical original dimensions disagree with the source manifest');
      }

      final assetPath = p.join(
        sourceRoot.path,
        'PackBuild',
        entry['asset'] as String,
      );
      final assetFile = File(assetPath);
      if (!assetFile.existsSync()) {
        throw FileSystemException('Missing source PNG', assetPath);
      }

      final image = img.decodePng(await assetFile.readAsBytes());
      if (image == null) {
        throw FormatException(resource + ': PNG could not be decoded');
      }
      if (image.width != overrideSize[0] ||
          image.height != overrideSize[1]) {
        throw FormatException(
          resource + ': PNG dimensions ' + image.width.toString() + 'x' +
          image.height.toString() + ' do not match ' +
          overrideSize[0].toString() + 'x' + overrideSize[1].toString(),
        );
      }

      final textureTypeValue = meta['textureType'] as int;
      final textureType = TextureType.values.firstWhere(
        (type) => type.value == textureTypeValue,
        orElse: () => TextureType.Error,
      );
      if (textureType == TextureType.Error) {
        throw FormatException(resource + ': unsupported original texture type');
      }

      // Use Retro's own PNG-to-OTEX conversion path. Native-size images keep
      // the original format; scaled replacements use Retro's V1 raw-upload
      // rules, including its scale and palette handling.
      final manifestEntry = TextureManifestEntry(
        '',
        textureType,
        textureWidth,
        textureHeight,
      );
      final data = buildTextureFromImage(image, manifestEntry, resource);
      if (data == null) {
        throw FormatException(resource + ': Retro rejected the replacement image');
      }
      _validateTexture(data, resource);

      final gameVersion = ByteData.sublistView(data).getUint32(8, Endian.little);
      if (gameVersion == 0) {
        builtV0++;
      } else {
        builtV1++;
      }

      encoder.addArchiveFile(
        ArchiveFile.noCompress(resource, data.length, data),
      );
      built++;
      if (built % 250 == 0) {
        stdout.writeln('Packed ' + built.toString() + ' textures...');
      }
    }
    encoder.close();
  } catch (_) {
    encoder.close();
    rethrow;
  }

  if (limit == null && built != metadata.length) {
    throw StateError(
      'Built ' + built.toString() + ' textures, but the canonical manifest contains ' +
      metadata.length.toString(),
    );
  }

  stdout.writeln('Retro-compatible O2R complete.');
  stdout.writeln('Built resources: ' + built.toString());
  stdout.writeln('V0 native-format resources: ' + builtV0.toString());
  stdout.writeln('V1 raw-upload resources: ' + builtV1.toString());
  stdout.writeln('Eligible source entries: ' + entries.length.toString());
  stdout.writeln('Archive bytes: ' + outputFile.lengthSync().toString());
}
