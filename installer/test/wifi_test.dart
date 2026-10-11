import 'package:flutter_test/flutter_test.dart';
import 'package:thunder_installer/backend.dart';
import 'package:thunder_installer/config.dart';

DeviceInfo _device({String wifi = 'wlan0'}) => DeviceInfo.fromJson({
  'id': 'xiaomi-thunder',
  'name': 'Redmi 10 5G',
  'vendor': 'Xiaomi',
  'codename': 'thunder',
  'wifi_interface': wifi,
});

void main() {
  test('scan: une faixas pelo sinal mais forte e ordena', () {
    const out =
        'SINAL  FAIXA   REDE\n'
        '-48    5GHz    Casa 5G\n'
        '-60    2.4GHz  Casa\n'
        '-70    5GHz    Casa\n'
        '-90    2.4GHz  Vizinho com espaco\n';
    final nets = parseWifiScan(out);
    expect(nets.map((n) => n.ssid), ['Casa 5G', 'Casa', 'Vizinho com espaco']);
    expect(nets[1].signal, -60);
    expect(nets[1].band, '2.4GHz');
    expect(nets.first.bars, 4);
    expect(nets.last.bars, 0);
  });

  test('status: le estado, rede e IP', () {
    final s = parseWifiStatus(
      'wpa_state=COMPLETED\nssid=Casa 5G\nfreq=5180\nip_address=192.168.0.7\n',
    );
    expect(s.connected, isTrue);
    expect(s.ssid, 'Casa 5G');
    expect(s.ip, '192.168.0.7');
    expect(parseWifiStatus('wpa_state=DISCONNECTED\n').connected, isFalse);
  });

  test('shQuote protege aspas e espacos para o shell remoto', () {
    expect(shQuote('Casa'), "'Casa'");
    expect(shQuote("Bar d'Ana \$HOME"), "'Bar d'\\''Ana \$HOME'");
  });

  test('validacao e ambiente do Wi-Fi', () {
    final c = InstallConfig()..wifiSsid = '';
    expect(c.wifiError(), isNull);
    c
      ..wifiSsid = 'Casa'
      ..wifiPassword = '1234567';
    expect(c.wifiError(), isNotNull);
    c.wifiPassword = '';
    expect(c.wifiError(), isNull, reason: 'rede aberta');
    c.wifiPassword = '12345678';
    expect(c.wifiError(), isNull);
    final env = c.environment(_device());
    expect(env['THUNDER_WIFI_SSID'], 'Casa');
    expect(env['THUNDER_WIFI_PASSWORD'], '12345678');
    // Aparelho sem Wi-Fi: nada vai para a imagem.
    final none = c.environment(_device(wifi: ''));
    expect(none['THUNDER_WIFI_SSID'], '');
    expect(none['THUNDER_WIFI_PASSWORD'], '');
  });
}
