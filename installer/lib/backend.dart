// Ponte com os scripts de tools/: o app nao sabe montar imagem nem falar com o
// aparelho, so chama tools/installer-info.sh, detect-device.py, to-fastboot.sh e
// install.sh e le o que eles imprimem.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

class DeviceInfo {
  DeviceInfo.fromJson(Map<String, dynamic> j)
    : id = j['id'] as String,
      name = j['name'] as String,
      vendor = j['vendor'] as String,
      codename = j['codename'] as String,
      soc = j['soc'] as String? ?? '',
      status = j['status'] as String? ?? 'bring-up',
      rootPartition = j['root_partition'] as String? ?? 'userdata',
      needsRom = j['needs_rom'] as bool? ?? false,
      kernelBuilt = j['kernel_built'] as bool? ?? false,
      profiles = List<String>.from(j['profiles'] as List? ?? const []),
      androidDevices = List<String>.from(
        j['android_devices'] as List? ?? const [],
      ),
      wifiInterface = j['wifi_interface'] as String? ?? '';

  final String id, name, vendor, codename, soc, status, rootPartition;
  final bool needsRom, kernelBuilt;
  final List<String> profiles, androidDevices;

  /// Interface Wi-Fi de estacao; vazia = aparelho sem Wi-Fi na imagem.
  final String wifiInterface;

  bool get hasWifi => wifiInterface.isNotEmpty;

  String get label => '$vendor $name';
}

class ProfileInfo {
  ProfileInfo.fromJson(Map<String, dynamic> j)
    : id = j['id'] as String,
      order = (j['order'] as num?)?.toInt() ?? 50,
      title = j['title'] as String,
      description = j['description'] as String,
      status = j['status'] as String? ?? 'experimental',
      blockers = j['blockers'] as String? ?? '',
      packages = List<String>.from(j['packages'] as List? ?? const []);

  final String id, title, description, status, blockers;
  final int order;
  final List<String> packages;

  bool get tested => status == 'tested';

  /// Ainda falta base: aparece, mas nao da para escolher.
  bool get planned => status == 'planned';
}

/// Uma rede vista pelo `thunder-wifi scan` no aparelho.
class WifiNetwork {
  const WifiNetwork(this.ssid, this.signal, this.band);

  final String ssid;

  /// dBm (mais perto de zero = mais forte).
  final int signal;
  final String band;

  /// 0 a 4 barras.
  int get bars => signal >= -55
      ? 4
      : signal >= -65
      ? 3
      : signal >= -75
      ? 2
      : signal >= -85
      ? 1
      : 0;
}

/// `thunder-wifi status` do aparelho.
class WifiStatus {
  const WifiStatus({this.state = '', this.ssid = '', this.ip = ''});

  final String state, ssid, ip;

  bool get connected => state == 'COMPLETED';
}

/// Aspas simples para o shell do outro lado do SSH (o ssh junta os argumentos numa
/// linha de comando).
String shQuote(String s) => "'${s.replaceAll("'", r"'\''")}'";

List<WifiNetwork> parseWifiScan(String out) {
  final nets = <String, WifiNetwork>{};
  for (final line in out.split('\n').skip(1)) {
    final m = RegExp(r'^(-?\d+)\s+(\S+)\s+(.+)$').firstMatch(line.trimRight());
    if (m == null) continue;
    final n = WifiNetwork(m.group(3)!, int.parse(m.group(1)!), m.group(2)!);
    // A mesma rede em duas faixas ou dois APs: fica a de sinal mais forte.
    final old = nets[n.ssid];
    if (old == null || n.signal > old.signal) nets[n.ssid] = n;
  }
  return nets.values.toList()..sort((a, b) => b.signal.compareTo(a.signal));
}

WifiStatus parseWifiStatus(String out) {
  String v(String k) =>
      RegExp('^$k=(.*)\$', multiLine: true).firstMatch(out)?.group(1)?.trim() ??
      '';
  return WifiStatus(
    state: v('wpa_state'),
    ssid: v('ssid'),
    ip: v('ip_address'),
  );
}

class Catalogue {
  Catalogue(this.devices, this.profiles);
  final List<DeviceInfo> devices;
  final List<ProfileInfo> profiles;

  DeviceInfo? device(String? id) =>
      devices.where((d) => d.id == id).firstOrNull;
  ProfileInfo? profile(String? id) =>
      profiles.where((p) => p.id == id).firstOrNull;
}

/// O que tools/detect-device.py encontrou no cabo.
class Detection {
  Detection.fromJson(Map<String, dynamic> j)
    : mode = j['mode'] as String? ?? 'none',
      serial = j['serial'] as String? ?? '',
      product = j['product'] as String? ?? '',
      model = j['model'] as String? ?? '',
      vendor = j['vendor'] as String? ?? '',
      slot = j['slot'] as String? ?? '',
      detail = j['detail'] as String? ?? '',
      unlocked = j['unlocked'] as bool?,
      device = j['device'] as String?;

  const Detection.none()
    : mode = 'none',
      serial = '',
      product = '',
      model = '',
      vendor = '',
      slot = '',
      detail = '',
      unlocked = null,
      device = null;

  /// fastboot, adb, thunder (Thunder OS rodando) ou none.
  final String mode;
  final String serial, product, model, vendor, slot, detail;
  final bool? unlocked;

  /// Pasta em devices/ que casou, ou null.
  final String? device;

  bool get connected => mode != 'none';
}

/// Instalacao rodando agora, para a janela perguntar antes de fechar.
final activeInstall = ValueNotifier<InstallRun?>(null);

/// Uma linha da instalacao: marcador de etapa, fim, erro ou log comum.
sealed class InstallEvent {}

class StepStarted extends InstallEvent {
  StepStarted(this.id, this.title);
  final String id, title;
}

class InstallDone extends InstallEvent {}

/// A etapa atual foi pulada (ja estava feita).
class StepSkipped extends InstallEvent {}

class InstallFailed extends InstallEvent {
  InstallFailed(this.message);
  final String message;
}

class LogLine extends InstallEvent {
  LogLine(this.text);
  final String text;
}

/// Codigo de saida do install.sh em frase. Negativo (Dart) ou 128+N (shell) e sinal:
/// alguem parou o processo de fora, nao foi uma etapa que falhou.
String exitMessage(int code) {
  final signal = code < 0 ? -code : (code > 128 ? code - 128 : 0);
  const names = {1: 'HUP', 2: 'INT', 9: 'KILL', 15: 'TERM'};
  // tools/common.sh (take_build_lock): outra montagem segurando build/.
  if (code == 75) {
    return 'Outra instalacao do Thunder OS esta rodando neste computador. '
        'Espere ela terminar e tente de novo.';
  }
  if (signal > 0) {
    return 'A instalacao foi interrompida de fora do app '
        '(sinal ${names[signal] ?? signal}).';
  }
  return 'A instalacao terminou com erro (codigo $code). Veja as ultimas linhas do log.';
}

class InstallRun {
  InstallRun._(this._process, this.events);

  final Process _process;
  final Stream<InstallEvent> events;

  /// Para a instalacao inteira: o script roda num grupo de processos proprio
  /// (setsid), e o sinal vai para o grupo, nao so para o bash.
  void cancel() {
    Process.runSync('kill', ['-TERM', '-${_process.pid}']);
  }
}

class Backend {
  Backend(this.repo);

  final String repo;

  /// Raiz do repositorio: THUNDER_OS_REPO, ou subindo a partir do executavel e do
  /// diretorio atual ate achar tools/install.sh e devices/.
  static Backend? locate() {
    final env = Platform.environment['THUNDER_OS_REPO'];
    final starts = [
      if (env != null && env.isNotEmpty) env,
      Directory.current.path,
      File(Platform.resolvedExecutable).parent.path,
    ];
    for (final start in starts) {
      var dir = Directory(start).absolute;
      for (var i = 0; i < 8; i++) {
        if (File('${dir.path}/tools/install.sh').existsSync() &&
            Directory('${dir.path}/devices').existsSync()) {
          return Backend(dir.path);
        }
        final parent = dir.parent;
        if (parent.path == dir.path) break;
        dir = parent;
      }
    }
    return null;
  }

  String tool(String name) => '$repo/tools/$name';

  Future<Map<String, dynamic>> _json(String exe, List<String> args) async {
    final r = await Process.run(exe, args, workingDirectory: repo);
    if (r.exitCode != 0) {
      throw ProcessException(exe, args, '${r.stderr}'.trim(), r.exitCode);
    }
    return jsonDecode(r.stdout as String) as Map<String, dynamic>;
  }

  Future<Catalogue> catalogue() async {
    final j = await _json(tool('installer-info.sh'), const []);
    return Catalogue(
      [for (final d in j['devices'] as List) DeviceInfo.fromJson(d)],
      [for (final p in j['profiles'] as List) ProfileInfo.fromJson(p)]
        ..sort((a, b) => a.order.compareTo(b.order)),
    );
  }

  Future<Detection> detect() async =>
      Detection.fromJson(await _json(tool('detect-device.py'), const []));

  /// Reinicia o aparelho no fastboot pelo adb ou pelo SSH. Devolve o log.
  Future<String> toFastboot() async {
    final r = await Process.run(
      tool('to-fastboot.sh'),
      const [],
      workingDirectory: repo,
    );
    final out = '${r.stdout}${r.stderr}'.trim();
    if (r.exitCode != 0) throw ProcessException('to-fastboot.sh', [], out);
    return out;
  }

  static const thunderAddr = '172.16.42.1';

  static const _sshOpts = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5'];

  Future<ProcessResult> _ssh(String command) => Process.run('ssh', [
    ..._sshOpts,
    'root@$thunderAddr',
    command,
  ]).timeout(const Duration(seconds: 30));

  Future<WifiStatus> wifiStatus() async =>
      parseWifiStatus('${(await _ssh('thunder-wifi status')).stdout}');

  Future<List<WifiNetwork>> wifiScan() async {
    final r = await _ssh('thunder-wifi scan');
    if (r.exitCode != 0) {
      throw ProcessException(
        'thunder-wifi',
        ['scan'],
        '${r.stdout}${r.stderr}'.trim(),
        r.exitCode,
      );
    }
    return parseWifiScan('${r.stdout}');
  }

  /// Conecta o aparelho a uma rede. A senha vai pela entrada padrao do ssh, nunca na
  /// linha de comando (apareceria no ps dos dois lados).
  Future<void> wifiConnect(String ssid, String password) async {
    final p = await Process.start('ssh', [
      ..._sshOpts,
      'root@$thunderAddr',
      'thunder-wifi connect ${shQuote(ssid)}',
    ]);
    p.stdin.writeln(password);
    await p.stdin.close();
    final out = StringBuffer();
    final pipes = Future.wait([
      p.stdout.transform(utf8.decoder).forEach(out.write),
      p.stderr.transform(utf8.decoder).forEach(out.write),
    ]);
    final code = await p.exitCode.timeout(const Duration(seconds: 60));
    await pipes;
    if (code != 0) {
      throw ProcessException(
        'thunder-wifi',
        ['connect'],
        out.toString().trim(),
        code,
      );
    }
  }

  /// Fusos do host (tzdata), para o seletor.
  Future<List<String>> timezones() async {
    final f = File('/usr/share/zoneinfo/zone1970.tab');
    if (!f.existsSync()) return const ['UTC'];
    final zones = <String>{'UTC'};
    for (final line in await f.readAsLines()) {
      if (line.startsWith('#')) continue;
      final cols = line.split('\t');
      if (cols.length >= 3) zones.add(cols[2]);
    }
    return zones.toList()..sort();
  }

  /// Locales UTF-8 que o glibc sabe gerar (a imagem gera o escolhido).
  Future<List<String>> locales() async {
    final f = File('/usr/share/i18n/SUPPORTED');
    if (!f.existsSync()) return const ['en_US.UTF-8', 'pt_BR.UTF-8'];
    final out = <String>[];
    for (final line in await f.readAsLines()) {
      final parts = line.split(' ');
      if (parts.length == 2 &&
          parts[1] == 'UTF-8' &&
          parts[0].endsWith('.UTF-8')) {
        out.add(parts[0]);
      }
    }
    return out..sort();
  }

  static String hostLocale() {
    final lang = Platform.environment['LANG'] ?? '';
    final m = RegExp(r'^([a-z]{2,3}_[A-Z]{2})').firstMatch(lang);
    return m == null ? 'en_US.UTF-8' : '${m.group(1)}.UTF-8';
  }

  static String hostTimezone() {
    try {
      final target = Link('/etc/localtime').targetSync();
      final i = target.indexOf('zoneinfo/');
      if (i >= 0) return target.substring(i + 'zoneinfo/'.length);
    } on FileSystemException {
      // Sem link: fica o padrao.
    }
    return 'UTC';
  }

  static String? defaultSshKey() {
    final home = Platform.environment['HOME'];
    if (home == null) return null;
    for (final name in ['id_ed25519.pub', 'id_ecdsa.pub', 'id_rsa.pub']) {
      final f = File('$home/.ssh/$name');
      if (f.existsSync()) return f.path;
    }
    return null;
  }

  /// Roda tools/install.sh com as opcoes no ambiente. Senhas vao pelo ambiente do
  /// processo, nunca para um arquivo.
  ///
  /// A saida passa por `tee -p` antes de chegar ao app: o log em [logPath] e
  /// gravado pelo tee, e se o app morrer (o cano para ele fecha), o tee segue
  /// gravando e a instalacao nao morre de SIGPIPE no meio de uma etapa.
  Future<InstallRun> install(Map<String, String> env, String logPath) async {
    final p = await Process.start(
      'setsid',
      [
        'bash',
        '-c',
        'set -o pipefail; "\$1" 2>&1 | tee -p -a "\$2"',
        'install',
        tool('install.sh'),
        logPath,
      ],
      workingDirectory: repo,
      environment: env,
    );
    final controller = StreamController<InstallEvent>();
    var open = 2;
    void onLine(String line) {
      if (line.startsWith('::step ')) {
        final rest = line.substring(7);
        final sp = rest.indexOf(' ');
        controller.add(
          sp < 0
              ? StepStarted(rest, rest)
              : StepStarted(rest.substring(0, sp), rest.substring(sp + 1)),
        );
      } else if (line == '::skip') {
        controller.add(StepSkipped());
      } else if (line == '::done') {
        controller.add(InstallDone());
      } else if (line.startsWith('::error ')) {
        controller.add(InstallFailed(line.substring(8)));
      } else {
        controller.add(LogLine(line));
      }
    }

    Future<void> pipe(Stream<List<int>> s) async {
      await s
          .transform(const Utf8Decoder(allowMalformed: true))
          .transform(const LineSplitter())
          .forEach(onLine);
      if (--open == 0) {
        final code = await p.exitCode;
        if (code != 0) controller.add(InstallFailed(exitMessage(code)));
        await controller.close();
      }
    }

    pipe(p.stdout);
    pipe(p.stderr);
    return InstallRun._(p, controller.stream.asBroadcastStream());
  }
}
