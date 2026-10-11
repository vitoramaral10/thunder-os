import 'package:flutter/material.dart';

import '../backend.dart';
import '../wizard.dart';
import 'common.dart';

const _icons = {
  'none': Icons.terminal,
  'console': Icons.keyboard_alt_outlined,
  'phosh': Icons.phone_iphone,
  'plasma-mobile': Icons.auto_awesome_mosaic_outlined,
};

class ProfileStep extends StatefulWidget {
  const ProfileStep({super.key, required this.ctx});

  final StepContext ctx;

  @override
  State<ProfileStep> createState() => _ProfileStepState();
}

class _ProfileStepState extends State<ProfileStep> {
  late final packages = TextEditingController(
    text: widget.ctx.config.extraPackages,
  );

  @override
  void dispose() {
    packages.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final ctx = widget.ctx;
    final cfg = ctx.config;
    final t = Theme.of(context).textTheme;
    final allowed = ctx.device?.profiles ?? const <String>[];
    final profiles = ctx.catalogue.profiles
        .where((p) => allowed.contains(p.id))
        .toList();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const StepHeader(
          title: 'O que instalar',
          subtitle:
              'Escolha a interface que vai aparecer na tela. O sistema base, o SSH '
              'pelo cabo e o firmware do aparelho entram em todas.',
        ),
        for (final p in profiles)
          _ProfileCard(
            profile: p,
            selected: cfg.profile == p.id,
            onTap: p.planned
                ? null
                : () => cfg.update(() => cfg.profile = p.id),
          ),
        const SizedBox(height: 24),
        Text('Pacotes extras', style: t.titleMedium),
        const SizedBox(height: 12),
        TextField(
          controller: packages,
          decoration: InputDecoration(
            labelText: 'Pacotes do Arch Linux ARM (opcional)',
            hintText: 'htop vim git',
            helperText:
                'Separados por espaco. Instalados com o pacman durante a montagem.',
            errorText: cfg.packagesError(),
          ),
          onChanged: (v) => cfg.update(() => cfg.extraPackages = v),
        ),
      ],
    );
  }
}

class _ProfileCard extends StatelessWidget {
  const _ProfileCard({
    required this.profile,
    required this.selected,
    required this.onTap,
  });

  final ProfileInfo profile;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final s = Theme.of(context).colorScheme;
    final t = Theme.of(context).textTheme;
    final disabled = onTap == null;
    final fg = disabled ? s.onSurface.withValues(alpha: 0.38) : s.onSurface;

    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Semantics(
        button: true,
        selected: selected,
        enabled: !disabled,
        child: Card(
          // Selecionado: container de maior enfase e borda primaria; dois sinais.
          elevation: 0,
          color: selected ? s.secondaryContainer : s.surfaceContainerLow,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
            side: BorderSide(
              color: selected ? s.primary : s.outlineVariant,
              width: selected ? 2 : 1,
            ),
          ),
          clipBehavior: Clip.antiAlias,
          child: InkWell(
            onTap: onTap,
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(
                    _icons[profile.id] ?? Icons.widgets_outlined,
                    size: 32,
                    color: disabled ? fg : s.primary,
                  ),
                  const SizedBox(width: 16),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          children: [
                            Flexible(
                              child: Text(
                                profile.title,
                                style: t.titleMedium?.copyWith(color: fg),
                              ),
                            ),
                            const SizedBox(width: 12),
                            StatusChip(
                              tested: profile.tested,
                              label: profile.planned ? 'Em preparacao' : null,
                            ),
                          ],
                        ),
                        const SizedBox(height: 4),
                        Text(
                          profile.description,
                          style: t.bodyMedium?.copyWith(color: fg),
                        ),
                        if (profile.planned && profile.blockers.isNotEmpty) ...[
                          const SizedBox(height: 8),
                          Text(
                            'Falta: ${profile.blockers}',
                            style: t.bodySmall?.copyWith(
                              color: s.onSurfaceVariant,
                            ),
                          ),
                        ],
                      ],
                    ),
                  ),
                  const SizedBox(width: 8),
                  Icon(
                    selected
                        ? Icons.radio_button_checked
                        : Icons.radio_button_off,
                    color: selected ? s.primary : fg,
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
