import 'package:flutter/material.dart';

class StepHeader extends StatelessWidget {
  const StepHeader({super.key, required this.title, required this.subtitle});

  final String title, subtitle;

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.only(bottom: 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title, style: t.headlineSmall),
          const SizedBox(height: 8),
          Text(
            subtitle,
            style: t.bodyMedium?.copyWith(
              color: Theme.of(context).colorScheme.onSurfaceVariant,
            ),
          ),
        ],
      ),
    );
  }
}

/// Faixa de aviso: erro bloqueia, aviso so informa. Icone e texto, nunca so cor.
class Notice extends StatelessWidget {
  const Notice({super.key, required this.text, this.error = false, this.icon});

  final String text;
  final bool error;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    final s = Theme.of(context).colorScheme;
    final bg = error ? s.errorContainer : s.secondaryContainer;
    final fg = error ? s.onErrorContainer : s.onSecondaryContainer;
    return Container(
      margin: const EdgeInsets.only(top: 12),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            icon ?? (error ? Icons.error_outline : Icons.info_outline),
            color: fg,
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              text,
              style: Theme.of(
                context,
              ).textTheme.bodyMedium?.copyWith(color: fg),
            ),
          ),
        ],
      ),
    );
  }
}

/// Selo de status (testado/experimental), com icone para nao depender de cor.
class StatusChip extends StatelessWidget {
  const StatusChip({super.key, required this.tested, this.label});

  final bool tested;
  final String? label;

  @override
  Widget build(BuildContext context) {
    final s = Theme.of(context).colorScheme;
    return Chip(
      visualDensity: VisualDensity.compact,
      avatar: Icon(
        tested ? Icons.verified_outlined : Icons.science_outlined,
        size: 18,
        color: tested ? s.primary : s.tertiary,
      ),
      label: Text(label ?? (tested ? 'Testado' : 'Experimental')),
    );
  }
}
