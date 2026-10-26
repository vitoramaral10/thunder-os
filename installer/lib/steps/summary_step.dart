import 'dart:io';

import 'package:flutter/material.dart';

import '../wizard.dart';
import 'common.dart';

class SummaryStep extends StatelessWidget {
  const SummaryStep({super.key, required this.ctx});

  final StepContext ctx;

  Future<void> _start(BuildContext context) async {
    final cfg = ctx.config;
    final d = ctx.device!;
    if (cfg.flash) {
      // Diz a consequencia, nao "tem certeza?".
      final ok = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          icon: const Icon(Icons.warning_amber_rounded),
          title: Text('Apagar o ${d.name} e instalar?'),
          content: Text(
            'Tudo na particao ${d.rootPartition} do aparelho sera apagado (fotos, '
            'apps, contas). A verificacao de boot sera desligada para o Thunder OS '
            'iniciar. O bootloader nao e travado nem destravado.\n\n'
            'Para voltar ao Android, use o script de gravacao da ROM de fabrica.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancelar'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Apagar e instalar'),
            ),
          ],
        ),
      );
      if (ok != true) return;
    }
    ctx.goNext();
  }

  @override
  Widget build(BuildContext context) {
    final cfg = ctx.config;
    final d = ctx.device!;
    final t = Theme.of(context).textTheme;
    final profile = ctx.catalogue.profile(cfg.profile);
    final rows = <(IconData, String, String)>[
      (Icons.smartphone_outlined, 'Aparelho', '${d.label} (${d.codename})'),
      if (d.needsRom)
        (
          Icons.inventory_2_outlined,
          'ROM de fabrica',
          cfg.romPath?.split('/').last ?? '-',
        ),
      (
        Icons.dashboard_customize_outlined,
        'Interface',
        profile?.title ?? cfg.profile,
      ),
      if (cfg.extraPackages.trim().isNotEmpty)
        (Icons.add_box_outlined, 'Pacotes extras', cfg.extraPackages.trim()),
      (Icons.person_outline, 'Usuario', '${cfg.user} (sudo)'),
      (
        Icons.admin_panel_settings_outlined,
        'Root',
        cfg.rootSameAsUser ? 'mesma senha do usuario' : 'senha propria',
      ),
      (Icons.lan_outlined, 'Hostname', cfg.effectiveHostname(d)),
      (
        Icons.key,
        'Chave SSH',
        cfg.sshKey?.split('/').last ?? 'nenhuma (login por senha)',
      ),
      if (d.hasWifi)
        (
          Icons.wifi,
          'Wi-Fi',
          cfg.wifiSsid.isEmpty ? 'sem rede salva' : '"${cfg.wifiSsid}"',
        ),
      (Icons.translate, 'Idioma', cfg.locale),
      (Icons.schedule, 'Fuso horario', cfg.timezone),
    ];

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const StepHeader(
          title: 'Resumo',
          subtitle:
              'Confira antes de comecar. A montagem roda neste computador, sem '
              'root, e pode levar de 20 minutos a mais de uma hora na primeira vez.',
        ),
        Card.outlined(
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Column(
              children: [
                for (final (icon, label, value) in rows)
                  ListTile(
                    dense: true,
                    leading: Icon(icon),
                    title: Text(label),
                    trailing: ConstrainedBox(
                      constraints: const BoxConstraints(maxWidth: 380),
                      child: Text(
                        value,
                        textAlign: TextAlign.end,
                        overflow: TextOverflow.ellipsis,
                        style: t.bodyMedium,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 24),
        Text('Tamanho inicial da imagem', style: t.titleSmall),
        const SizedBox(height: 8),
        SegmentedButton<String>(
          segments: const [
            ButtonSegment(value: '4G', label: Text('4 GB')),
            ButtonSegment(value: '8G', label: Text('8 GB')),
            ButtonSegment(value: '16G', label: Text('16 GB')),
          ],
          selected: {cfg.size},
          onSelectionChanged: (v) => cfg.update(() => cfg.size = v.first),
        ),
        const SizedBox(height: 4),
        Text(
          'No primeiro boot o sistema cresce ate ocupar a particao inteira.',
          style: t.bodySmall,
        ),
        const SizedBox(height: 16),
        // No Windows o app ainda nao monta imagens, so grava: nao ha o que desligar.
        if (!Platform.isWindows)
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Gravar no aparelho ao terminar'),
            subtitle: Text(
              cfg.flash
                  ? 'Leva o aparelho ao fastboot e grava boot e ${d.rootPartition}.'
                  : 'So monta as imagens em build/${d.id}/out/, sem tocar no aparelho.',
            ),
            value: cfg.flash,
            onChanged: (v) => cfg.update(() => cfg.flash = v),
          ),
        if (cfg.flash)
          Notice(
            error: true,
            icon: Icons.delete_forever_outlined,
            text:
                'A gravacao apaga todos os dados da particao ${d.rootPartition} do '
                'aparelho.',
          ),
        const SizedBox(height: 24),
        Align(
          alignment: Alignment.centerRight,
          child: FilledButton.icon(
            onPressed: () => _start(context),
            icon: Icon(
              cfg.flash ? Icons.system_update_alt : Icons.build_outlined,
            ),
            label: Text(cfg.flash ? 'Instalar' : 'Montar imagens'),
          ),
        ),
      ],
    );
  }
}
