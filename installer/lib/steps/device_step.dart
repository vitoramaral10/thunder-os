import 'dart:async';

import 'package:flutter/material.dart';

import '../backend.dart';
import '../wizard.dart';
import 'common.dart';
import 'wifi_card.dart';

const _modeText = {
  'fastboot': 'Bootloader (fastboot)',
  'adb': 'Android (adb)',
  'thunder': 'Thunder OS rodando',
  'none': 'Nenhum aparelho no cabo',
};

class DeviceStep extends StatefulWidget {
  const DeviceStep({super.key, required this.ctx});

  final StepContext ctx;

  @override
  State<DeviceStep> createState() => _DeviceStepState();
}

class _DeviceStepState extends State<DeviceStep> {
  Detection detection = const Detection.none();
  bool checking = true;
  bool rebooting = false;
  String? error;
  Timer? timer;

  @override
  void initState() {
    super.initState();
    _poll();
  }

  @override
  void dispose() {
    timer?.cancel();
    super.dispose();
  }

  // Detecta, e de novo 3 s depois de terminar (nunca duas deteccoes ao mesmo tempo).
  Future<void> _poll() async {
    try {
      final d = await widget.ctx.backend.detect();
      if (!mounted) return;
      setState(() {
        detection = d;
        error = null;
        checking = false;
      });
      final cfg = widget.ctx.config;
      if (d.device != null && cfg.device == null) {
        cfg.update(() => cfg.device = d.device);
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        error = '$e';
        checking = false;
      });
    }
    timer = Timer(const Duration(seconds: 3), _poll);
  }

  Future<void> _toFastboot() async {
    setState(() => rebooting = true);
    final messenger = ScaffoldMessenger.of(context);
    try {
      await widget.ctx.backend.toFastboot();
      messenger.showSnackBar(
        const SnackBar(
          content: Text('Reiniciando no fastboot. Aguarde o aparelho voltar.'),
        ),
      );
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('$e')));
    } finally {
      if (mounted) setState(() => rebooting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final ctx = widget.ctx;
    final cfg = ctx.config;
    final t = Theme.of(context).textTheme;
    final s = Theme.of(context).colorScheme;
    final matched = ctx.catalogue.device(detection.device);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const StepHeader(
          title: 'Conecte o aparelho',
          subtitle:
              'Ligue o celular ao computador pelo cabo USB. Ele pode estar no '
              'Android de fabrica (com depuracao USB ativa), no fastboot ou ja rodando '
              'o Thunder OS.',
        ),
        Card.outlined(
          child: Padding(
            padding: const EdgeInsets.all(20),
            child: Row(
              children: [
                CircleAvatar(
                  radius: 28,
                  backgroundColor: s.primaryContainer,
                  child: Icon(
                    detection.connected
                        ? Icons.phone_android
                        : Icons.usb_off_outlined,
                    color: s.onPrimaryContainer,
                  ),
                ),
                const SizedBox(width: 20),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        matched?.label ??
                            (detection.model.isNotEmpty
                                ? '${detection.vendor} ${detection.model}'
                                : _modeText[detection.mode] ?? detection.mode),
                        style: t.titleMedium,
                      ),
                      const SizedBox(height: 4),
                      Text(
                        [
                          _modeText[detection.mode] ?? detection.mode,
                          if (detection.product.isNotEmpty)
                            'codinome ${detection.product}',
                          if (detection.slot.isNotEmpty)
                            'slot ${detection.slot}',
                          if (detection.serial.isNotEmpty) detection.serial,
                        ].join(' · '),
                        style: t.bodyMedium?.copyWith(
                          color: s.onSurfaceVariant,
                        ),
                      ),
                    ],
                  ),
                ),
                if (checking)
                  const SizedBox(
                    width: 24,
                    height: 24,
                    child: CircularProgressIndicator(),
                  )
                else
                  Tooltip(
                    message: 'Procurando a cada 3 segundos',
                    child: Icon(Icons.autorenew, color: s.onSurfaceVariant),
                  ),
              ],
            ),
          ),
        ),
        if (error != null)
          Notice(text: 'Falha na deteccao: $error', error: true),
        if (detection.mode == 'adb' && detection.detail.isNotEmpty)
          Notice(
            text:
                'O aparelho respondeu "${detection.detail}" no adb. Desbloqueie a '
                'tela e aceite a depuracao USB neste computador.',
          ),
        if (detection.mode == 'thunder' && detection.detail.isNotEmpty)
          Notice(text: 'Thunder OS no cabo, mas ${detection.detail}.'),
        if (detection.mode == 'fastboot' && detection.unlocked == false)
          const Notice(
            error: true,
            text:
                'O bootloader esta travado. Desbloqueie pelo procedimento oficial do '
                'fabricante antes de instalar; o instalador nunca trava nem destrava o '
                'bootloader.',
          ),
        if (detection.connected && matched == null && detection.detail.isEmpty)
          Notice(
            text:
                'Este aparelho (${detection.product}) ainda nao tem port no Thunder OS. '
                'Veja docs/porting.md para adicionar.',
            icon: Icons.help_outline,
          ),
        // Thunder OS ja rodando: da para conectar o Wi-Fi dele daqui, sem reinstalar.
        if (detection.mode == 'thunder' &&
            detection.detail.isEmpty &&
            (matched?.hasWifi ?? false))
          WifiCard(backend: ctx.backend),
        if (detection.mode == 'adb' || detection.mode == 'thunder') ...[
          const SizedBox(height: 16),
          OutlinedButton.icon(
            onPressed: rebooting ? null : _toFastboot,
            icon: const Icon(Icons.restart_alt),
            label: const Text('Reiniciar no fastboot agora'),
          ),
          const SizedBox(height: 4),
          Text(
            'Opcional: o instalador faz isso sozinho antes de gravar.',
            style: t.bodySmall?.copyWith(color: s.onSurfaceVariant),
          ),
        ],
        const SizedBox(height: 32),
        Text('Aparelho a instalar', style: t.titleMedium),
        const SizedBox(height: 12),
        DropdownMenu<String>(
          width: 480,
          initialSelection: cfg.device,
          label: const Text('Modelo'),
          onSelected: (v) => cfg.update(() => cfg.device = v),
          dropdownMenuEntries: [
            for (final d in ctx.catalogue.devices)
              DropdownMenuEntry(
                value: d.id,
                label: d.label,
                trailingIcon: Text(d.codename),
              ),
          ],
        ),
        if (cfg.device != null &&
            detection.device != null &&
            cfg.device != detection.device)
          const Notice(
            error: true,
            text:
                'O modelo escolhido nao e o que esta no cabo. A gravacao confere o '
                'aparelho e vai recusar.',
          ),
        if (ctx.device != null) ...[
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              StatusChip(
                tested: ctx.device!.status != 'bring-up',
                label: switch (ctx.device!.status) {
                  'daily' => 'Uso diario',
                  'boots' => 'Inicia, acesso por SSH',
                  _ => 'Em desenvolvimento',
                },
              ),
              if (ctx.device!.soc.isNotEmpty)
                Chip(label: Text(ctx.device!.soc)),
            ],
          ),
        ],
      ],
    );
  }
}
