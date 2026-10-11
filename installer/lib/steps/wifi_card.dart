// Wi-Fi do aparelho que ja roda o Thunder OS, pelo cabo: estado, busca de redes e
// conexao. Tudo por SSH com o thunder-wifi; a senha vai pela entrada padrao.
import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';

import '../backend.dart';

const _barIcons = [
  Icons.signal_wifi_0_bar,
  Icons.network_wifi_1_bar,
  Icons.network_wifi_2_bar,
  Icons.network_wifi_3_bar,
  Icons.signal_wifi_4_bar,
];

class WifiCard extends StatefulWidget {
  const WifiCard({super.key, required this.backend});

  final Backend backend;

  @override
  State<WifiCard> createState() => _WifiCardState();
}

class _WifiCardState extends State<WifiCard> {
  WifiStatus? status;
  List<WifiNetwork>? networks;
  String? selected;
  bool scanning = false;
  bool connecting = false;
  bool showPassword = false;
  String? error;
  final password = TextEditingController();
  Timer? timer;

  @override
  void initState() {
    super.initState();
    _refresh();
    // O IP chega pelo DHCP alguns segundos depois de associar.
    timer = Timer.periodic(const Duration(seconds: 8), (_) => _refresh());
  }

  @override
  void dispose() {
    timer?.cancel();
    password.dispose();
    super.dispose();
  }

  Future<void> _refresh() async {
    try {
      final s = await widget.backend.wifiStatus();
      if (mounted) setState(() => status = s);
    } catch (_) {
      // Sem SSH agora (reiniciando, cabo solto): mantem o ultimo estado.
    }
  }

  Future<void> _scan() async {
    setState(() {
      scanning = true;
      error = null;
    });
    try {
      final n = await widget.backend.wifiScan();
      if (!mounted) return;
      setState(() => networks = n);
    } on ProcessException catch (e) {
      if (mounted) setState(() => error = 'A busca falhou: ${_why(e)}');
    } on TimeoutException {
      if (mounted) setState(() => error = 'A busca nao respondeu em 30 s.');
    } finally {
      if (mounted) setState(() => scanning = false);
    }
  }

  // A ultima linha do thunder-wifi diz o motivo; sem saida, o codigo.
  String _why(ProcessException e) {
    final lines = e.message
        .trim()
        .split('\n')
        .where((l) => l.trim().isNotEmpty);
    return lines.isEmpty ? 'codigo ${e.errorCode}' : lines.last.trim();
  }

  String? _passwordError() {
    final n = password.text.length;
    return n > 0 && (n < 8 || n > 63) ? 'De 8 a 63 caracteres' : null;
  }

  Future<void> _connect() async {
    final ssid = selected;
    if (ssid == null || _passwordError() != null) return;
    setState(() {
      connecting = true;
      error = null;
    });
    final messenger = ScaffoldMessenger.of(context);
    try {
      await widget.backend.wifiConnect(ssid, password.text);
      password.clear();
      messenger.showSnackBar(
        SnackBar(
          content: Text('Conectado a "$ssid". O IP chega em instantes.'),
        ),
      );
      await _refresh();
    } on ProcessException catch (e) {
      if (mounted) setState(() => error = 'Nao conectou: ${_why(e)}');
    } on TimeoutException {
      if (mounted) setState(() => error = 'O aparelho nao respondeu em 60 s.');
    } finally {
      if (mounted) setState(() => connecting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    final s = Theme.of(context).colorScheme;
    final st = status;
    final summary = st == null
        ? 'Lendo o estado...'
        : st.connected
        ? 'Conectado a "${st.ssid}"${st.ip.isEmpty ? ' · aguardando IP' : ' · ${st.ip}'}'
        : st.state.isEmpty
        ? 'Wi-Fi desligado no aparelho'
        : 'Desconectado';

    return Card.outlined(
      margin: const EdgeInsets.only(top: 16),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(
                  st?.connected == true ? Icons.wifi : Icons.wifi_off,
                  color: st?.connected == true ? s.primary : s.onSurfaceVariant,
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('Wi-Fi do aparelho', style: t.titleMedium),
                      Text(
                        summary,
                        style: t.bodyMedium?.copyWith(
                          color: s.onSurfaceVariant,
                        ),
                      ),
                    ],
                  ),
                ),
                FilledButton.tonalIcon(
                  onPressed: scanning || connecting ? null : _scan,
                  icon: scanning
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.wifi_find),
                  label: Text(
                    networks == null ? 'Procurar redes' : 'Procurar de novo',
                  ),
                ),
              ],
            ),
            if (networks != null && networks!.isEmpty)
              const Padding(
                padding: EdgeInsets.only(top: 12),
                child: Text('Nenhuma rede encontrada.'),
              ),
            if (networks != null && networks!.isNotEmpty) ...[
              const SizedBox(height: 12),
              ConstrainedBox(
                constraints: const BoxConstraints(maxHeight: 260),
                child: ListView(
                  shrinkWrap: true,
                  children: [
                    for (final n in networks!)
                      ListTile(
                        dense: true,
                        selected: selected == n.ssid,
                        selectedTileColor: s.secondaryContainer,
                        selectedColor: s.onSecondaryContainer,
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(12),
                        ),
                        leading: Icon(
                          _barIcons[n.bars],
                          semanticLabel: 'Sinal ${n.bars} de 4',
                        ),
                        title: Text(n.ssid),
                        subtitle: Text('${n.band} · ${n.signal} dBm'),
                        trailing: st?.connected == true && st?.ssid == n.ssid
                            ? const Text('conectado')
                            : null,
                        onTap: connecting
                            ? null
                            : () => setState(() {
                                selected = n.ssid;
                                password.clear();
                              }),
                      ),
                  ],
                ),
              ),
            ],
            if (selected != null) ...[
              const SizedBox(height: 16),
              TextField(
                controller: password,
                obscureText: !showPassword,
                enabled: !connecting,
                autofocus: true,
                decoration: InputDecoration(
                  labelText: 'Senha de "$selected"',
                  helperText:
                      'Vazio para rede aberta. Fica salva no aparelho so como chave.',
                  errorText: _passwordError(),
                  suffixIcon: IconButton(
                    tooltip: showPassword ? 'Esconder senha' : 'Mostrar senha',
                    icon: Icon(
                      showPassword ? Icons.visibility_off : Icons.visibility,
                    ),
                    onPressed: () =>
                        setState(() => showPassword = !showPassword),
                  ),
                ),
                onChanged: (_) => setState(() {}),
                onSubmitted: (_) => _connect(),
              ),
              const SizedBox(height: 12),
              Align(
                alignment: Alignment.centerRight,
                child: FilledButton.icon(
                  onPressed: connecting || _passwordError() != null
                      ? null
                      : _connect,
                  icon: connecting
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.wifi),
                  label: Text(connecting ? 'Conectando...' : 'Conectar'),
                ),
              ),
            ],
            if (error != null)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Text(
                  error!,
                  style: t.bodyMedium?.copyWith(color: s.error),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
