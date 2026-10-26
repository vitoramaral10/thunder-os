// fastboot e adb: os do PATH (no Linux, android-tools) ou, no Windows, o
// platform-tools da Google baixado na primeira vez e conferido pelo SHA-256.
// Nao vai junto com o app: a licenca do Android SDK nao deixa redistribuir.
import 'dart:io';

import 'package:crypto/crypto.dart' as crypto;

class PlatformTools {
  PlatformTools._();

  static const version = '37.0.1';
  static const url =
      'https://dl.google.com/android/repository/platform-tools_r$version-win.zip';
  static const size = 8044989;

  /// Conferido em 2026-10-25: o SHA-1 bate com o repository2-3.xml da Google.
  static const sha256 =
      '45f4d63113e895ebde0c90f194099a4676b6ac653bd28d54314a9e022bbc1a99';

  /// Pasta do app no Windows (%LOCALAPPDATA%\ThunderOS); null fora dele.
  static String? get dataDir {
    final base = Platform.environment['LOCALAPPDATA'];
    return Platform.isWindows && base != null ? '$base\\ThunderOS' : null;
  }

  static String? get _dir =>
      dataDir == null ? null : '$dataDir\\platform-tools-$version';

  /// Caminho do executavel: o baixado, se existir, senao o nome puro (PATH).
  static String exe(String name) {
    final dir = _dir;
    if (dir != null) {
      final f = File('$dir\\platform-tools\\$name.exe');
      if (f.existsSync()) return f.path;
    }
    return name;
  }

  /// Ja da para chamar o fastboot e o adb (baixados ou no PATH)?
  static Future<bool> available() async {
    try {
      for (final tool in ['fastboot', 'adb']) {
        final r = await Process.run(exe(tool), const ['--version']);
        if (r.exitCode != 0) return false;
      }
      return true;
    } on ProcessException {
      return false;
    }
  }

  /// Baixa e extrai no Windows. [progress] recebe de 0 a 1.
  static Future<void> download({void Function(double)? progress}) async {
    final dir = _dir;
    if (dir == null) throw UnsupportedError('so no Windows');
    await Directory(dir).create(recursive: true);
    final zip = File('$dir\\platform-tools.zip');
    final client = HttpClient();
    try {
      final res = await (await client.getUrl(Uri.parse(url))).close();
      if (res.statusCode != 200) {
        throw HttpException('HTTP ${res.statusCode}', uri: Uri.parse(url));
      }
      final sink = zip.openWrite();
      var got = 0;
      try {
        await sink.addStream(
          res.map((chunk) {
            got += chunk.length;
            progress?.call(got / size);
            return chunk;
          }),
        );
      } finally {
        await sink.close();
      }
    } catch (_) {
      // Download pela metade: o arquivo sai, a proxima tentativa comeca do zero.
      if (zip.existsSync()) await zip.delete();
      rethrow;
    } finally {
      client.close();
    }
    if (!await verify(zip)) {
      await zip.delete();
      throw const FileSystemException(
        'o platform-tools baixado nao confere com o SHA-256 esperado',
      );
    }
    // tar.exe (bsdtar) vem no Windows 10 1803+ e abre zip.
    final r = await Process.run('tar', ['-xf', zip.path, '-C', dir]);
    await zip.delete();
    if (r.exitCode != 0) {
      throw ProcessException('tar', ['-xf'], '${r.stderr}'.trim(), r.exitCode);
    }
  }

  static Future<bool> verify(File f) async =>
      (await f.length()) == size &&
      '${await crypto.sha256.bind(f.openRead()).first}' == sha256;
}
