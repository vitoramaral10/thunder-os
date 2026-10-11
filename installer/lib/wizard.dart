// Assistente em passos, no jeito dos instaladores de desktop: lista de etapas a
// esquerda, conteudo a direita, Voltar/Continuar embaixo.
import 'dart:ui' show AppExitResponse;

import 'package:flutter/material.dart';

import 'backend.dart';
import 'config.dart';
import 'steps/device_step.dart';
import 'steps/install_step.dart';
import 'steps/profile_step.dart';
import 'steps/region_step.dart';
import 'steps/rom_step.dart';
import 'steps/summary_step.dart';
import 'steps/users_step.dart';

/// Ultima etapa (posicao na ordem visivel) que a barra lateral deixa abrir: so se
/// pula para uma etapa com todas as anteriores validas, e nunca para a ultima (a
/// instalacao), que so abre pelo botao do resumo, onde fica a confirmacao de apagar
/// o aparelho.
int lastReachable(List<bool> validInOrder) {
  var i = 0;
  while (i < validInOrder.length - 2 && validInOrder[i]) {
    i++;
  }
  return i;
}

class WizardStep {
  const WizardStep({
    required this.title,
    required this.icon,
    required this.build,
    required this.valid,
    this.visible,
  });

  final String title;
  final IconData icon;
  final Widget Function(StepContext) build;
  final bool Function(StepContext) valid;
  final bool Function(StepContext)? visible;
}

/// O que cada tela recebe.
class StepContext {
  StepContext(
    this.backend,
    this.catalogue,
    this.config, {
    required this.goNext,
    required this.goBack,
    required this.restart,
  });

  final Backend backend;
  final Catalogue catalogue;
  final InstallConfig config;
  final VoidCallback goNext;
  final VoidCallback goBack;

  /// Monta a etapa atual de novo, do zero (a instalacao recomeca).
  final VoidCallback restart;

  DeviceInfo? get device => catalogue.device(config.device);
}

final steps = <WizardStep>[
  WizardStep(
    title: 'Aparelho',
    icon: Icons.smartphone_outlined,
    build: (c) => DeviceStep(ctx: c),
    valid: (c) => c.device != null,
  ),
  WizardStep(
    title: 'ROM de fabrica',
    icon: Icons.inventory_2_outlined,
    build: (c) => RomStep(ctx: c),
    valid: (c) => c.config.romPath != null,
    visible: (c) => c.device?.needsRom ?? true,
  ),
  WizardStep(
    title: 'Interface',
    icon: Icons.dashboard_customize_outlined,
    build: (c) => ProfileStep(ctx: c),
    valid: (c) =>
        c.catalogue.profile(c.config.profile) != null &&
        c.config.packagesError() == null,
  ),
  WizardStep(
    title: 'Usuarios',
    icon: Icons.person_outline,
    build: (c) => UsersStep(ctx: c),
    valid: (c) => c.config.usersValid,
  ),
  WizardStep(
    title: 'Regiao',
    icon: Icons.public,
    build: (c) => RegionStep(ctx: c),
    valid: (c) => true,
  ),
  WizardStep(
    title: 'Resumo',
    icon: Icons.fact_check_outlined,
    build: (c) => SummaryStep(ctx: c),
    valid: (c) => true,
  ),
  WizardStep(
    title: 'Instalacao',
    icon: Icons.downloading_outlined,
    build: (c) => InstallStep(ctx: c),
    valid: (c) => false,
  ),
];

class Wizard extends StatefulWidget {
  const Wizard({super.key, required this.backend});

  final Backend backend;

  @override
  State<Wizard> createState() => _WizardState();
}

class _WizardState extends State<Wizard> {
  final config = InstallConfig();
  late Future<Catalogue> catalogue = widget.backend.catalogue();
  int current = 0;
  // Muda a chave da etapa atual: "Tentar de novo" remonta a instalacao do zero.
  int attempt = 0;

  late final AppLifecycleListener lifecycle;

  @override
  void initState() {
    super.initState();
    config.addListener(_changed);
    lifecycle = AppLifecycleListener(onExitRequested: _onExitRequested);
  }

  // Fechar a janela no meio da instalacao pergunta antes. Parar mata o grupo de
  // processos inteiro; continuar mantem a janela aberta.
  Future<AppExitResponse> _onExitRequested() async {
    final run = activeInstall.value;
    if (run == null || !mounted) return AppExitResponse.exit;
    final stop = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        icon: const Icon(Icons.warning_amber_rounded),
        title: const Text('A instalacao ainda esta rodando'),
        content: const Text(
          'Fechar agora para a instalacao. O que ja foi montado fica em build/ e e '
          'reaproveitado na proxima vez. Se a gravacao ja tiver comecado, o aparelho '
          'pode ficar sem sistema ate voce gravar de novo.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Parar e fechar'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Continuar instalando'),
          ),
        ],
      ),
    );
    if (stop != true) return AppExitResponse.cancel;
    run.cancel();
    return AppExitResponse.exit;
  }

  @override
  void dispose() {
    lifecycle.dispose();
    config.removeListener(_changed);
    config.dispose();
    super.dispose();
  }

  void _changed() => setState(() {});

  List<int> _visible(StepContext c) => [
    for (var i = 0; i < steps.length; i++)
      if (steps[i].visible?.call(c) ?? true) i,
  ];

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<Catalogue>(
      future: catalogue,
      builder: (context, snap) {
        if (snap.hasError) {
          return _Failure(
            message: '${snap.error}',
            retry: () => setState(() => catalogue = widget.backend.catalogue()),
          );
        }
        if (!snap.hasData) {
          return const Scaffold(
            body: Center(child: CircularProgressIndicator()),
          );
        }
        late StepContext ctx;
        ctx = StepContext(
          widget.backend,
          snap.data!,
          config,
          goNext: () => _go(ctx, 1),
          goBack: () => _go(ctx, -1),
          restart: () => setState(() => attempt++),
        );
        return _layout(context, ctx);
      },
    );
  }

  void _go(StepContext ctx, int delta) {
    final order = _visible(ctx);
    final pos = order.indexOf(current);
    final next = pos + delta;
    if (next >= 0 && next < order.length) setState(() => current = order[next]);
  }

  Widget _layout(BuildContext context, StepContext ctx) {
    final scheme = Theme.of(context).colorScheme;
    final order = _visible(ctx);
    if (!order.contains(current)) current = order.first;
    final pos = order.indexOf(current);
    final step = steps[current];
    final installing = current == steps.length - 1;
    final reachable = lastReachable([
      for (final i in order) steps[i].valid(ctx),
    ]);

    return Scaffold(
      body: Row(
        children: [
          Container(
            width: 248,
            color: scheme.surfaceContainerLow,
            child: SafeArea(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Padding(
                    padding: const EdgeInsets.fromLTRB(24, 28, 24, 24),
                    child: Row(
                      children: [
                        Icon(Icons.bolt, color: scheme.primary, size: 32),
                        const SizedBox(width: 8),
                        Text(
                          'Thunder OS',
                          style: Theme.of(context).textTheme.titleLarge,
                        ),
                      ],
                    ),
                  ),
                  for (var i = 0; i < order.length; i++)
                    _StepTile(
                      index: i + 1,
                      step: steps[order[i]],
                      selected: order[i] == current,
                      done: i < pos,
                      onTap: !installing && i <= reachable
                          ? () => setState(() => current = order[i])
                          : null,
                    ),
                ],
              ),
            ),
          ),
          Expanded(
            child: Column(
              children: [
                Expanded(
                  child: SingleChildScrollView(
                    padding: const EdgeInsets.fromLTRB(40, 32, 40, 24),
                    child: Align(
                      alignment: Alignment.topLeft,
                      child: ConstrainedBox(
                        constraints: const BoxConstraints(maxWidth: 760),
                        child: KeyedSubtree(
                          key: ValueKey((current, attempt)),
                          child: step.build(ctx),
                        ),
                      ),
                    ),
                  ),
                ),
                if (!installing)
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 40,
                      vertical: 16,
                    ),
                    decoration: BoxDecoration(
                      border: Border(
                        top: BorderSide(color: scheme.outlineVariant),
                      ),
                    ),
                    child: Row(
                      children: [
                        if (pos > 0)
                          OutlinedButton(
                            onPressed: () => _go(ctx, -1),
                            child: const Text('Voltar'),
                          ),
                        const Spacer(),
                        if (current != steps.length - 2)
                          FilledButton(
                            onPressed: step.valid(ctx)
                                ? () => _go(ctx, 1)
                                : null,
                            child: const Text('Continuar'),
                          ),
                      ],
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _StepTile extends StatelessWidget {
  const _StepTile({
    required this.index,
    required this.step,
    required this.selected,
    required this.done,
    required this.onTap,
  });

  final int index;
  final WizardStep step;
  final bool selected, done;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 2),
      child: ListTile(
        selected: selected,
        selectedTileColor: scheme.secondaryContainer,
        selectedColor: scheme.onSecondaryContainer,
        shape: const StadiumBorder(),
        enabled: onTap != null || selected,
        leading: Icon(done ? Icons.check_circle : step.icon),
        title: Text(step.title),
        onTap: onTap,
      ),
    );
  }
}

class _Failure extends StatelessWidget {
  const _Failure({required this.message, required this.retry});

  final String message;
  final VoidCallback retry;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 560),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                'Nao consegui ler os aparelhos e perfis',
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 12),
              SelectableText(message, textAlign: TextAlign.center),
              const SizedBox(height: 16),
              FilledButton(
                onPressed: retry,
                child: const Text('Tentar de novo'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
