import 'package:flutter/material.dart';

import '../wizard.dart';
import 'common.dart';

class RegionStep extends StatefulWidget {
  const RegionStep({super.key, required this.ctx});

  final StepContext ctx;

  @override
  State<RegionStep> createState() => _RegionStepState();
}

class _RegionStepState extends State<RegionStep> {
  late final Future<(List<String>, List<String>)> lists = () async {
    final b = widget.ctx.backend;
    return (await b.locales(), await b.timezones());
  }();

  @override
  Widget build(BuildContext context) {
    final cfg = widget.ctx.config;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const StepHeader(
          title: 'Idioma e fuso horario',
          subtitle:
              'Vieram deste computador. O ingles (en_US) e gerado sempre, '
              'como reserva.',
        ),
        FutureBuilder(
          future: lists,
          builder: (context, snap) {
            if (!snap.hasData) return const LinearProgressIndicator();
            final (locales, zones) = snap.data!;
            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                DropdownMenu<String>(
                  width: 420,
                  menuHeight: 360,
                  enableFilter: true,
                  requestFocusOnTap: true,
                  initialSelection: locales.contains(cfg.locale)
                      ? cfg.locale
                      : null,
                  label: const Text('Idioma do sistema (locale)'),
                  leadingIcon: const Icon(Icons.translate),
                  onSelected: (v) {
                    if (v != null) cfg.update(() => cfg.locale = v);
                  },
                  dropdownMenuEntries: [
                    for (final l in locales)
                      DropdownMenuEntry(value: l, label: l),
                  ],
                ),
                const SizedBox(height: 24),
                DropdownMenu<String>(
                  width: 420,
                  menuHeight: 360,
                  enableFilter: true,
                  requestFocusOnTap: true,
                  initialSelection: zones.contains(cfg.timezone)
                      ? cfg.timezone
                      : null,
                  label: const Text('Fuso horario'),
                  leadingIcon: const Icon(Icons.schedule),
                  onSelected: (v) {
                    if (v != null) cfg.update(() => cfg.timezone = v);
                  },
                  dropdownMenuEntries: [
                    for (final z in zones)
                      DropdownMenuEntry(value: z, label: z),
                  ],
                ),
              ],
            );
          },
        ),
      ],
    );
  }
}
