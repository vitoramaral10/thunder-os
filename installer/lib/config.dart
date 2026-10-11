// O que o usuario escolheu no instalador, e como isso vira o ambiente do
// tools/install.sh.
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'backend.dart';

final userPattern = RegExp(r'^[a-z_][a-z0-9_-]{0,31}$');
final hostPattern = RegExp(r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$');
final packagesPattern = RegExp(r'^[a-z0-9@._+\- ]*$');

class InstallConfig extends ChangeNotifier {
  InstallConfig()
    : user = _hostUser(),
      locale = Backend.hostLocale(),
      timezone = Backend.hostTimezone(),
      sshKey = Backend.defaultSshKey();

  static String _hostUser() {
    final u = Platform.environment['USER'] ?? '';
    return userPattern.hasMatch(u) && u != 'root' ? u : '';
  }

  String? device;
  String? romPath;
  String profile = 'none';
  String user;
  String password = '';
  String passwordConfirm = '';
  bool rootSameAsUser = false;
  String rootPassword = '';
  String rootPasswordConfirm = '';
  String hostname = '';
  String locale;
  String timezone;
  String? sshKey;
  String extraPackages = '';
  String wifiSsid = '';
  String wifiPassword = '';
  String size = '4G';
  bool flash = true;

  void update(VoidCallback change) {
    change();
    notifyListeners();
  }

  String effectiveHostname(DeviceInfo? d) =>
      hostname.isNotEmpty ? hostname : (d?.codename ?? 'thunder');

  String? userError() {
    if (user.isEmpty) return 'Escolha um nome de usuario';
    if (user == 'root') return 'root ja existe; escolha outro nome';
    if (!userPattern.hasMatch(user)) {
      return 'Use letras minusculas, numeros, - e _, comecando por letra';
    }
    return null;
  }

  String? passwordError() {
    if (password.length < 6) return 'Pelo menos 6 caracteres';
    if (password != passwordConfirm) return 'As senhas nao conferem';
    return null;
  }

  String? rootPasswordError() {
    if (rootSameAsUser) return null;
    if (rootPassword.length < 6) return 'Pelo menos 6 caracteres';
    if (rootPassword != rootPasswordConfirm) return 'As senhas nao conferem';
    return null;
  }

  String? hostnameError() {
    if (hostname.isEmpty) return null;
    return hostPattern.hasMatch(hostname)
        ? null
        : 'Letras, numeros e -, ate 63';
  }

  String? packagesError() => packagesPattern.hasMatch(extraPackages.trim())
      ? null
      : 'Nomes de pacote separados por espaco';

  /// Rede vazia = sem Wi-Fi pre-configurado. Senha vazia = rede aberta; WPA aceita
  /// de 8 a 63 caracteres.
  String? wifiError() {
    if (wifiSsid.isEmpty) return null;
    if (wifiSsid.length > 32) return 'Nome de rede tem ate 32 caracteres';
    final n = wifiPassword.length;
    if (n > 0 && (n < 8 || n > 63)) {
      return 'Senha de Wi-Fi tem de 8 a 63 caracteres';
    }
    return null;
  }

  bool get usersValid =>
      userError() == null &&
      passwordError() == null &&
      rootPasswordError() == null &&
      hostnameError() == null &&
      wifiError() == null;

  /// Ambiente do tools/install.sh. Vence o local.conf do repositorio.
  Map<String, String> environment(DeviceInfo d) => {
    'DEVICE': d.id,
    'THUNDER_PROFILE': profile,
    'THUNDER_USER': user,
    'THUNDER_PASSWORD': password,
    'THUNDER_ROOT_PASSWORD': rootSameAsUser ? password : rootPassword,
    'THUNDER_HOSTNAME': effectiveHostname(d),
    'THUNDER_LOCALE': locale,
    'THUNDER_TIMEZONE': timezone,
    'THUNDER_EXTRA_PACKAGES': extraPackages.trim(),
    // Vazio = sem chave: o login fica por senha.
    'AUTHORIZED_KEYS': sshKey ?? '',
    'SIZE': size,
    'THUNDER_FLASH': flash ? '1' : '0',
    'THUNDER_WIFI_SSID': d.hasWifi ? wifiSsid : '',
    'THUNDER_WIFI_PASSWORD': d.hasWifi && wifiSsid.isNotEmpty
        ? wifiPassword
        : '',
    'THUNDER_ROM': ?romPath,
  };
}
