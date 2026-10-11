import 'dart:io';

import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';

import '../wizard.dart';
import 'common.dart';

class RomStep extends StatelessWidget {
  const RomStep({super.key, required this.ctx});

  final StepContext ctx;

  Future<void> _pickFile() async {
    final f = await openFile(
      acceptedTypeGroups: const [
        XTypeGroup(
          label: 'ROM de fabrica',
          extensions: ['tgz', 'gz', 'tar', 'img'],
        ),
      ],
    );
    if (f != null) ctx.config.update(() => ctx.config.romPath = f.path);
  }

  Future<void> _pickDir() async {
    final d = await getDirectoryPath(confirmButtonText: 'Usar esta pasta');
    if (d != null) ctx.config.update(() => ctx.config.romPath = d);
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    final s = Theme.of(context).colorScheme;
    final path = ctx.config.romPath;
    final device = ctx.device!;
    final exists =
        path != null &&
        (FileSystemEntity.typeSync(path) != FileSystemEntityType.notFound);
    final size = exists && FileSystemEntity.isFileSync(path)
        ? '${(File(path).lengthSync() / (1 << 30)).toStringAsFixed(1)} GB'
        : 'pasta';
    final name = path?.split('/').last.toLowerCase() ?? '';
    final names = {device.codename, ...device.androidDevices};
    final looksRight = names.any(name.contains);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        StepHeader(
          title: 'ROM de fabrica',
          subtitle:
              'O firmware do toque, do Wi-Fi e do Bluetooth e proprietario e '
              'nao vem com o Thunder OS. Ele e copiado da ROM oficial do '
              '${device.vendor} ${device.name}, a mesma que voce usaria para voltar ao '
              'Android.',
        ),
        Card.outlined(
          child: Padding(
            padding: const EdgeInsets.all(20),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(
                      path == null ? Icons.attach_file : Icons.inventory_2,
                      color: s.primary,
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            path == null
                                ? 'Nenhuma ROM anexada'
                                : path.split('/').last,
                            style: t.titleMedium,
                          ),
                          if (path != null)
                            Text(
                              '$path · $size',
                              style: t.bodySmall?.copyWith(
                                color: s.onSurfaceVariant,
                              ),
                            ),
                        ],
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 16),
                Wrap(
                  spacing: 12,
                  runSpacing: 8,
                  children: [
                    FilledButton.tonalIcon(
                      onPressed: _pickFile,
                      icon: const Icon(Icons.upload_file),
                      label: const Text('Anexar pacote (.tgz)'),
                    ),
                    OutlinedButton.icon(
                      onPressed: _pickDir,
                      icon: const Icon(Icons.folder_open),
                      label: const Text('Escolher pasta extraida'),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
        if (path != null && !exists)
          const Notice(
            error: true,
            text: 'O arquivo escolhido nao existe mais.',
          ),
        if (path != null && exists && !looksRight)
          Notice(
            text:
                'O nome nao cita ${names.join(' nem ')}. Confira se a ROM e deste '
                'modelo: a instalacao para se nao achar o firmware.',
          ),
        const SizedBox(height: 24),
        Text('O que acontece com ela', style: t.titleSmall),
        const SizedBox(height: 8),
        Text(
          '• Aceita o pacote de fastboot como baixado (.tgz), a pasta extraida ou o '
          'super.img.\n'
          '• So a particao vendor e lida, direto do pacote, sem extrair os gigabytes do '
          'resto.\n'
          '• Nada e enviado para fora deste computador, e o pacote nao e alterado.',
          style: t.bodyMedium,
        ),
      ],
    );
  }
}
