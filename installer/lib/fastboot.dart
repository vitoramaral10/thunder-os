// Conversa com o aparelho pelo fastboot e adb sem passar por shell, para rodar
// igual no Linux e no Windows. A gravacao repete o tools/flash.sh --sim: quem
// muda um, muda o outro.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'platform_tools.dart';

/// Roda um comando com limite de tempo; codigo 1 e saida vazia se nao der para
/// rodar ou se estourar o tempo.
Future<(int, String)> runTool(
  String exe,
  List<String> args, {
  Duration timeout = const Duration(seconds: 6),
}) async {
  try {
    final p = await Process.start(exe, args);
    final out = StringBuffer();
    final pipes = Future.wait([
      p.stdout.transform(utf8.decoder).forEach(out.write),
      p.stderr.transform(utf8.decoder).forEach(out.write),
    ]);
    final code = await p.exitCode.timeout(
      timeout,
      onTimeout: () {
        p.kill();
        return -1;
      },
    );
    if (code == -1) return (1, '');
    await pipes;
    return (code, out.toString());
  } on ProcessException {
    return (1, '');
  }
}

/// Seriais em `fastboot devices`.
List<String> parseFastbootDevices(String out) => [
  for (final l in const LineSplitter().convert(out))
    if (l.trim().endsWith('fastboot')) l.trim().split(RegExp(r'\s+')).first,
];

/// Valor de `fastboot getvar` ("product: thunder", que vem no stderr).
String parseGetvar(String out, String name) {
  for (final l in const LineSplitter().convert(out)) {
    if (l.startsWith('$name:')) return l.substring(name.length + 1).trim();
  }
  return '';
}

/// Primeiro aparelho USB de `adb devices`: (serial, estado), ou null. Os de rede
/// (ip:porta, adb sem fio por mDNS) ficam de fora: nao estao no cabo, e reinicia-los
/// no bootloader seria mexer em outro celular.
(String, String)? parseAdbDevices(String out) {
  for (final l in const LineSplitter().convert(out).skip(1)) {
    final cols = l.trim().split(RegExp(r'\s+'));
    if (cols.length < 2) continue;
    if (cols[0].contains(':') || cols[0].contains('._adb-tls-')) continue;
    return (cols[0], cols[1]);
  }
  return null;
}

Future<String> getvar(String name, {String? serial}) async {
  final (_, out) = await runTool(PlatformTools.exe('fastboot'), [
    if (serial != null) ...['-s', serial],
    'getvar',
    name,
  ], timeout: const Duration(seconds: 10));
  return parseGetvar(out, name);
}

/// vbmeta vazio com a verificacao desligada (flags 2), byte a byte igual ao
/// `avbtool make_vbmeta_image --flags 2 --padding_size 2048` (avbtool 1.4.0).
Uint8List disabledVbmeta() {
  final b = Uint8List(2048);
  final v = ByteData.sublistView(b);
  b.setAll(0, ascii.encode('AVB0'));
  v.setUint32(4, 1); // required_libavb_version_major
  v.setUint32(120, 2); // flags: AVB_VBMETA_IMAGE_FLAGS_VERIFICATION_DISABLED
  b.setAll(128, ascii.encode('avbtool 1.4.0'));
  return b;
}

/// O que a gravacao precisa saber do aparelho (vem do catalogo).
class FlashTarget {
  const FlashTarget({
    required this.products,
    required this.rootPartition,
    required this.ab,
    required this.slot,
    required this.disableVbmeta,
  });

  final List<String> products;
  final String rootPartition, slot;
  final bool ab, disableVbmeta;
}

/// Grava boot.img e userdata.img de [outDir]. [log] recebe cada linha. Erro vira
/// excecao com a frase para o usuario. [cancelled] e consultado entre os passos.
Future<void> flash(
  FlashTarget t,
  String outDir,
  void Function(String) log, {
  bool Function()? cancelled,
  Duration wait = const Duration(seconds: 180),
}) async {
  final fb = PlatformTools.exe('fastboot');
  final boot = File('$outDir/boot.img');
  final root = File('$outDir/userdata.img');
  for (final f in [boot, root]) {
    if (!f.existsSync() || f.lengthSync() == 0) throw 'falta ${f.path}';
  }

  // Um aparelho so no fastboot, e todo comando vai para ele (-s): com dois no cabo,
  // nunca gravar no errado.
  final deadline = DateTime.now().add(wait);
  var serials = <String>[];
  String product = '';
  while (true) {
    if (cancelled?.call() ?? false) throw 'cancelado';
    serials = parseFastbootDevices((await runTool(fb, ['devices'])).$2);
    if (serials.length > 1) {
      throw 'mais de um aparelho no fastboot (${serials.join(', ')}): deixe so '
          'o que vai receber o Thunder OS no cabo';
    }
    if (serials.length == 1) {
      product = await getvar('product', serial: serials.first);
      if (product.isNotEmpty) break;
    }
    if (DateTime.now().isAfter(deadline)) {
      throw 'nenhum aparelho respondendo no fastboot';
    }
    await Future<void>.delayed(const Duration(seconds: 1));
  }
  final serial = serials.first;
  if (!t.products.contains(product)) {
    throw "produto '$product' nao e ${t.products.join(' ')}";
  }
  if (await getvar('unlocked', serial: serial) != 'yes') {
    throw 'bootloader travado';
  }
  var suffix = '';
  if (t.ab) {
    final slot = await getvar('current-slot', serial: serial);
    if (slot.isEmpty) throw 'o aparelho nao informa current-slot';
    if (t.slot.isNotEmpty && slot != t.slot) {
      throw "slot ativo e '$slot', esperado '${t.slot}'";
    }
    suffix = '_$slot';
  }

  Future<void> step(List<String> args) async {
    if (cancelled?.call() ?? false) throw 'cancelado';
    log('fastboot ${args.join(' ')}');
    final p = await Process.start(fb, ['-s', serial, ...args]);
    await Future.wait([
      for (final s in [p.stdout, p.stderr])
        s
            .transform(const Utf8Decoder(allowMalformed: true))
            .transform(const LineSplitter())
            .forEach(log),
    ]);
    final code = await p.exitCode;
    if (code != 0) throw 'fastboot ${args.first} falhou (codigo $code)';
  }

  if (t.disableVbmeta) {
    final vb = File('$outDir/vbmeta-disabled.img');
    await vb.writeAsBytes(disabledVbmeta());
    await step(['flash', 'vbmeta$suffix', vb.path]);
  }
  await step(['flash', 'boot$suffix', boot.path]);
  await step(['flash', t.rootPartition, root.path]);
  await step(['reboot']);
  log('Gravado. Rede pelo cabo: 172.16.42.1.');
}
