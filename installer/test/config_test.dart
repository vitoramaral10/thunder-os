import 'package:flutter_test/flutter_test.dart';
import 'package:thunder_installer/backend.dart';
import 'package:thunder_installer/config.dart';

DeviceInfo _device() => DeviceInfo.fromJson({
  'id': 'xiaomi-thunder',
  'name': 'Redmi 10 5G',
  'vendor': 'Xiaomi',
  'codename': 'thunder',
  'root_partition': 'userdata',
  'needs_rom': true,
  'profiles': ['none'],
  'android_devices': ['thunder', 'light'],
});

void main() {
  group('usuario', () {
    test('aceita nome valido e recusa root, maiuscula e vazio', () {
      final c = InstallConfig();
      for (final ok in ['vitor', 'a_b-1', '_x']) {
        c.user = ok;
        expect(c.userError(), isNull, reason: ok);
      }
      for (final bad in ['', 'root', 'Vitor', '1abc', 'a b']) {
        c.user = bad;
        expect(c.userError(), isNotNull, reason: bad);
      }
    });
  });

  group('senhas', () {
    test('exige 6 caracteres e confirmacao igual', () {
      final c = InstallConfig()
        ..password = '12345'
        ..passwordConfirm = '12345';
      expect(c.passwordError(), isNotNull);
      c
        ..password = '123456'
        ..passwordConfirm = '123457';
      expect(c.passwordError(), isNotNull);
      c.passwordConfirm = '123456';
      expect(c.passwordError(), isNull);
    });

    test('root com a mesma senha dispensa os campos de root', () {
      final c = InstallConfig()..rootSameAsUser = true;
      expect(c.rootPasswordError(), isNull);
      c.rootSameAsUser = false;
      expect(c.rootPasswordError(), isNotNull);
    });
  });

  group('ambiente do install.sh', () {
    test('leva as escolhas e a senha certa para o root', () {
      final c = InstallConfig()
        ..user = 'vitor'
        ..password = 'segredo1'
        ..rootPassword = 'raiz1234'
        ..profile = 'none'
        ..romPath = '/rom.tgz'
        ..sshKey = null
        ..extraPackages = '  htop vim ';
      final env = c.environment(_device());
      expect(env['DEVICE'], 'xiaomi-thunder');
      expect(env['THUNDER_ROOT_PASSWORD'], 'raiz1234');
      expect(env['THUNDER_HOSTNAME'], 'thunder');
      expect(env['THUNDER_EXTRA_PACKAGES'], 'htop vim');
      expect(env['THUNDER_ROM'], '/rom.tgz');
      // Chave vazia (e nao ausente): o build-rootfs.sh entende "sem chave".
      expect(env['AUTHORIZED_KEYS'], '');
      c.rootSameAsUser = true;
      expect(c.environment(_device())['THUNDER_ROOT_PASSWORD'], 'segredo1');
    });

    test('sem ROM, nao manda THUNDER_ROM', () {
      final env = (InstallConfig()..romPath = null).environment(_device());
      expect(env.containsKey('THUNDER_ROM'), isFalse);
    });
  });

  test('pacotes extras so aceitam nomes', () {
    final c = InstallConfig()..extraPackages = 'htop; rm -rf /';
    expect(c.packagesError(), isNotNull);
    c.extraPackages = 'htop python-pip';
    expect(c.packagesError(), isNull);
  });
}
