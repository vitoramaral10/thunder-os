import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:thunder_installer/backend.dart';
import 'package:thunder_installer/fastboot.dart';
import 'package:thunder_installer/platform_tools.dart';

void main() {
  test('vbmeta desligado igual ao do avbtool 1.4.0', () {
    // sha256 de `avbtool make_vbmeta_image --flags 2 --padding_size 2048`.
    expect(
      '${sha256.convert(disabledVbmeta())}',
      '3ed263680e72c3af16ee7c4d2642957a1ac78681b4ee325e388c285c29055bbc',
    );
  });

  test('saidas do fastboot e do adb', () {
    expect(
      parseFastbootDevices('0123abcd\tfastboot\nlixo\n\nXYZ  fastboot\n'),
      ['0123abcd', 'XYZ'],
    );
    expect(parseFastbootDevices(''), isEmpty);
    expect(
      parseGetvar(
        'product: thunder\nFinished. Total time: 0.001s\n',
        'product',
      ),
      'thunder',
    );
    expect(parseGetvar('unlocked: yes\n', 'product'), '');
    expect(
      parseAdbDevices('List of devices attached\nabc123\tunauthorized\n\n'),
      ('abc123', 'unauthorized'),
    );
    expect(parseAdbDevices('List of devices attached\n\n'), isNull);
    // adb sem fio (outro celular na rede) nao conta como aparelho no cabo.
    expect(
      parseAdbDevices(
        'List of devices attached\n'
        'adb-RX1._adb-tls-connect._tcp\tdevice\n'
        '192.168.0.5:5555\tdevice\n'
        'USB1\tdevice\n',
      ),
      ('USB1', 'device'),
    );
  });

  test('/etc/thunder/device em mapa', () {
    expect(
      parseDeviceFile(
        "DEVICE=xiaomi-thunder\nDEVICE_NAME='Redmi 10 5G'\nlixo\n",
      ),
      {'DEVICE': 'xiaomi-thunder', 'DEVICE_NAME': 'Redmi 10 5G'},
    );
  });

  test('catalogo traz o que a gravacao precisa', () {
    final t = DeviceInfo.fromJson({
      'id': 'x',
      'name': 'X',
      'vendor': 'V',
      'codename': 'x',
      'fastboot_products': ['thunder'],
      'fastboot_ab': true,
      'fastboot_slot': 'a',
      'disable_vbmeta': false,
      'root_partition': 'userdata',
    }).flashTarget;
    expect(t.products, ['thunder']);
    expect((t.ab, t.slot, t.disableVbmeta), (true, 'a', false));
  });

  test(
    'platform-tools: so aceita o zip com o tamanho e o hash certos',
    () async {
      final dir = await Directory.systemTemp.createTemp('pt');
      addTearDown(() => dir.delete(recursive: true));
      final f = File('${dir.path}/pt.zip')
        ..writeAsBytesSync(List.filled(PlatformTools.size, 0));
      expect(await PlatformTools.verify(f), isFalse);
    },
  );
}
