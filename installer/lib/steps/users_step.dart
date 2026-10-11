import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';

import '../wizard.dart';
import 'common.dart';

class UsersStep extends StatefulWidget {
  const UsersStep({super.key, required this.ctx});

  final StepContext ctx;

  @override
  State<UsersStep> createState() => _UsersStepState();
}

class _UsersStepState extends State<UsersStep> {
  late final cfg = widget.ctx.config;
  late final user = TextEditingController(text: cfg.user);
  late final pass = TextEditingController(text: cfg.password);
  late final pass2 = TextEditingController(text: cfg.passwordConfirm);
  late final root = TextEditingController(text: cfg.rootPassword);
  late final root2 = TextEditingController(text: cfg.rootPasswordConfirm);
  late final host = TextEditingController(text: cfg.hostname);
  late final wifiSsid = TextEditingController(text: cfg.wifiSsid);
  late final wifiPass = TextEditingController(text: cfg.wifiPassword);
  bool showPass = false;
  // Erro so aparece depois que o campo foi mexido, nao na tela vazia.
  final touched = <String>{};

  @override
  void dispose() {
    for (final c in [
      user,
      pass,
      pass2,
      root,
      root2,
      host,
      wifiSsid,
      wifiPass,
    ]) {
      c.dispose();
    }
    super.dispose();
  }

  String? _err(String field, String? error) =>
      touched.contains(field) ? error : null;

  void _set(String field, VoidCallback change) {
    touched.add(field);
    cfg.update(change);
  }

  Future<void> _pickKey() async {
    final f = await openFile(
      acceptedTypeGroups: const [
        XTypeGroup(label: 'Chave publica SSH', extensions: ['pub']),
      ],
    );
    if (f != null) cfg.update(() => cfg.sshKey = f.path);
  }

  Widget _passwordField(
    String field,
    TextEditingController c,
    String label,
    void Function(String) onChanged,
    String? error,
  ) {
    return TextField(
      controller: c,
      obscureText: !showPass,
      decoration: InputDecoration(
        labelText: label,
        errorText: _err(field, error),
        suffixIcon: IconButton(
          tooltip: showPass ? 'Esconder senhas' : 'Mostrar senhas',
          icon: Icon(showPass ? Icons.visibility_off : Icons.visibility),
          onPressed: () => setState(() => showPass = !showPass),
        ),
      ),
      onChanged: (v) => _set(field, () => onChanged(v)),
    );
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    final s = Theme.of(context).colorScheme;
    final device = widget.ctx.device;
    const gap = SizedBox(height: 16);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const StepHeader(
          title: 'Usuarios',
          subtitle:
              'Sua conta no aparelho (com sudo) e a senha de root. As senhas vao '
              'direto para a imagem e nao ficam salvas neste computador.',
        ),
        Text('Sua conta', style: t.titleMedium),
        const SizedBox(height: 12),
        TextField(
          controller: user,
          decoration: InputDecoration(
            labelText: 'Nome de usuario',
            helperText: 'Minusculas, numeros, - e _',
            errorText: _err('user', cfg.userError()),
          ),
          onChanged: (v) => _set('user', () => cfg.user = v.trim()),
        ),
        gap,
        _passwordField(
          'pass',
          pass,
          'Senha',
          (v) => cfg.password = v,
          cfg.password.length < 6 ? 'Pelo menos 6 caracteres' : null,
        ),
        gap,
        _passwordField(
          'pass2',
          pass2,
          'Confirme a senha',
          (v) => cfg.passwordConfirm = v,
          cfg.passwordError(),
        ),
        const SizedBox(height: 32),
        Text('Root', style: t.titleMedium),
        SwitchListTile(
          contentPadding: EdgeInsets.zero,
          title: const Text('Usar a mesma senha para o root'),
          value: cfg.rootSameAsUser,
          onChanged: (v) => cfg.update(() => cfg.rootSameAsUser = v),
        ),
        if (!cfg.rootSameAsUser) ...[
          const SizedBox(height: 8),
          _passwordField(
            'root',
            root,
            'Senha de root',
            (v) => cfg.rootPassword = v,
            cfg.rootPassword.length < 6 ? 'Pelo menos 6 caracteres' : null,
          ),
          gap,
          _passwordField(
            'root2',
            root2,
            'Confirme a senha de root',
            (v) => cfg.rootPasswordConfirm = v,
            cfg.rootPasswordError(),
          ),
        ],
        const SizedBox(height: 32),
        Text('Rede e acesso', style: t.titleMedium),
        const SizedBox(height: 12),
        TextField(
          controller: host,
          decoration: InputDecoration(
            labelText: 'Nome do aparelho na rede (hostname)',
            hintText: device?.codename,
            helperText: 'Vazio usa "${cfg.effectiveHostname(device)}"',
            errorText: cfg.hostnameError(),
          ),
          onChanged: (v) => cfg.update(() => cfg.hostname = v.trim()),
        ),
        if (device?.hasWifi ?? false) ...[
          gap,
          TextField(
            controller: wifiSsid,
            decoration: const InputDecoration(
              labelText: 'Rede Wi-Fi (opcional)',
              helperText:
                  'O aparelho ja liga conectado. Da para trocar depois no app.',
              prefixIcon: Icon(Icons.wifi),
            ),
            onChanged: (v) => cfg.update(() => cfg.wifiSsid = v),
          ),
          if (cfg.wifiSsid.isNotEmpty) ...[
            gap,
            _passwordField(
              'wifi',
              wifiPass,
              'Senha do Wi-Fi',
              (v) => cfg.wifiPassword = v,
              cfg.wifiError(),
            ),
          ],
        ],
        gap,
        Card.outlined(
          child: ListTile(
            leading: Icon(Icons.key, color: s.primary),
            title: Text(
              cfg.sshKey == null
                  ? 'Sem chave SSH: login so por senha'
                  : cfg.sshKey!.split('/').last,
            ),
            subtitle: Text(
              cfg.sshKey ??
                  'Com uma chave publica, o SSH pelo cabo entra sem senha.',
            ),
            trailing: Wrap(
              spacing: 8,
              children: [
                TextButton(onPressed: _pickKey, child: const Text('Escolher')),
                if (cfg.sshKey != null)
                  TextButton(
                    onPressed: () => cfg.update(() => cfg.sshKey = null),
                    child: const Text('Remover'),
                  ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}
