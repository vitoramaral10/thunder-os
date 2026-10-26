import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';

import '../backend.dart';
import '../progress.dart';
import '../wizard.dart';
import 'common.dart';

enum _State { pending, running, done, failed }

/// Etapas do tools/install.sh, na ordem. O titulo real vem do marcador ::step.
const _plan = [
  ('cross', 'Toolchain de compilacao'),
  ('kernel', 'Kernel do aparelho'),
  ('firmware', 'Firmware da ROM de fabrica'),
  ('rootfs', 'Sistema (Arch Linux ARM)'),
  ('boot', 'Imagem de boot'),
  ('userdata', 'Imagem da particao do sistema'),
  ('flash', 'Gravacao no aparelho'),
];

class InstallStep extends StatefulWidget {
  const InstallStep({super.key, required this.ctx});

  final StepContext ctx;

  @override
  State<InstallStep> createState() => _InstallStepState();
}

class _InstallStepState extends State<InstallStep> {
  // No Windows o app so grava (Backend.install): as etapas de montagem nao aparecem.
  late final List<(String, String)> plan = [
    for (final p in _plan)
      if (Platform.isWindows
          ? p.$1 == 'flash'
          : p.$1 != 'flash' || widget.ctx.config.flash)
        p,
  ];
  final states = <String, _State>{};
  final titles = <String, String>{};
  final log = <String>[];
  final scroll = ScrollController();
  InstallRun? run;
  StreamSubscription<InstallEvent>? sub;
  String? logPath;
  String? failure;
  bool finished = false;
  bool showLog = true;
  final started = DateTime.now();
  DateTime? ended;
  late final ProgressEstimator progress;
  late final File timesFile;
  Map<String, Duration> previousTimes = const {};
  // Redesenha a cada segundo: porcentagem, tempo decorrido e restante andam sozinhos.
  Timer? ticker;

  @override
  void initState() {
    super.initState();
    final dir = '${widget.ctx.backend.repo}/build/${widget.ctx.device!.id}';
    Directory(dir).createSync(recursive: true);
    timesFile = File('$dir/install-times.json');
    previousTimes = loadEstimates(timesFile);
    progress = ProgressEstimator([for (final p in plan) p.$1], previousTimes);
    ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted && ended == null) setState(() {});
    });
    _start();
  }

  @override
  void dispose() {
    ticker?.cancel();
    sub?.cancel();
    if (activeInstall.value == run) activeInstall.value = null;
    scroll.dispose();
    super.dispose();
  }

  Future<void> _start() async {
    final ctx = widget.ctx;
    final d = ctx.device!;
    final stamp = started
        .toIso8601String()
        .replaceAll(':', '')
        .split('.')
        .first;
    final dir = Directory('${ctx.backend.repo}/build/${d.id}')
      ..createSync(recursive: true);
    logPath = '${dir.path}/install-$stamp.log';
    try {
      run = await ctx.backend.install(d, ctx.config.environment(d), logPath!);
      activeInstall.value = run;
    } catch (e) {
      setState(() => failure = 'Nao consegui iniciar o tools/install.sh: $e');
      return;
    }
    sub = run!.events.listen(_onEvent);
  }

  void _onEvent(InstallEvent e) {
    setState(() {
      switch (e) {
        case StepStarted(:final id, :final title):
          for (final k in states.keys) {
            if (states[k] == _State.running) states[k] = _State.done;
          }
          progress.start(id, DateTime.now());
          states[id] = _State.running;
          titles[id] = title;
          _append('== $title');
        case StepSkipped():
          progress.skip();
        case InstallDone():
          for (final k in states.keys) {
            if (states[k] == _State.running) states[k] = _State.done;
          }
          finished = true;
          _end();
        case InstallFailed(:final message):
          if (failure != null || finished) return;
          for (final k in states.keys) {
            if (states[k] == _State.running) states[k] = _State.failed;
          }
          failure = message;
          _end();
        case LogLine(:final text):
          _append(text);
      }
    });
  }

  // Para o relogio e guarda quanto levou cada etapa que terminou, para a proxima
  // estimativa. A etapa que falhou nao entra: o tempo dela nao diz nada.
  void _end() {
    if (ended != null) return;
    ended = DateTime.now();
    if (activeInstall.value == run) activeInstall.value = null;
    if (failure == null) progress.finish(ended!);
    saveEstimates(timesFile, previousTimes, progress.measured());
  }

  // Ultimas linhas com texto do log, para mostrar junto do erro.
  List<String> _tail() {
    final lines = log.where((l) => l.trim().isNotEmpty).toList();
    return lines.sublist(lines.length > 6 ? lines.length - 6 : 0);
  }

  void _append(String line) {
    log.add(line);
    // A tela guarda as ultimas linhas; o arquivo guarda tudo.
    if (log.length > 4000) log.removeRange(0, 1000);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (scroll.hasClients) scroll.jumpTo(scroll.position.maxScrollExtent);
    });
  }

  Future<void> _cancel() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Parar a instalacao?'),
        content: const Text(
          'O que ja foi montado fica em build/ e e reaproveitado na proxima vez. '
          'Se a gravacao ja tiver comecado, o aparelho pode ficar sem sistema ate '
          'voce gravar de novo.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Continuar instalando'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Parar'),
          ),
        ],
      ),
    );
    if (ok == true) run?.cancel();
  }

  // Ao lado de cada etapa: quanto levou, "pulada" ou, na atual, quanto esta levando.
  Widget? _stepTime(String id, TextTheme t, ColorScheme s) {
    final style = t.bodySmall?.copyWith(
      color: s.onSurfaceVariant,
      fontFeatures: const [FontFeature.tabularFigures()],
    );
    if (progress.skipped.contains(id) && progress.took.containsKey(id)) {
      return Text('ja feita', style: style);
    }
    final took = progress.took[id];
    if (took != null) return Text(formatElapsed(took), style: style);
    if (id == progress.current && states[id] == _State.running) {
      return Text(
        formatElapsed(DateTime.now().difference(progress.startedAt!)),
        style: style,
      );
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    final s = Theme.of(context).colorScheme;
    final cfg = widget.ctx.config;
    final running = failure == null && !finished;
    final now = ended ?? DateTime.now();
    final fraction = finished ? 1.0 : progress.fraction(now);
    final elapsed = now.difference(started);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        StepHeader(
          title: finished
              ? (cfg.flash ? 'Thunder OS instalado' : 'Imagens prontas')
              : failure != null
              ? 'A instalacao parou'
              : 'Instalando',
          subtitle: running
              ? 'Pode deixar rodando. O aparelho so e tocado na ultima etapa.'
              : 'Log completo em ${logPath ?? 'build/'}',
        ),
        Row(
          children: [
            Text(
              '${(fraction * 100).floor()}%',
              style: t.headlineSmall?.copyWith(
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
            ),
            const Spacer(),
            Text(
              [
                'Decorrido ${formatElapsed(elapsed)}',
                if (running && progress.current != null)
                  'falta ${formatRemaining(progress.remaining(now))}',
              ].join(' · '),
              style: t.bodyMedium?.copyWith(
                color: s.onSurfaceVariant,
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        LinearProgressIndicator(
          value: fraction,
          semanticsLabel: 'Progresso da instalacao',
          semanticsValue: '${(fraction * 100).floor()}%',
        ),
        const SizedBox(height: 16),
        Card.outlined(
          child: Column(
            children: [
              for (final (id, fallback) in plan)
                ListTile(
                  dense: true,
                  leading: switch (states[id] ?? _State.pending) {
                    _State.pending => Icon(
                      Icons.radio_button_unchecked,
                      color: s.outline,
                    ),
                    _State.running => const SizedBox(
                      width: 24,
                      height: 24,
                      child: CircularProgressIndicator(strokeWidth: 3),
                    ),
                    _State.done => Icon(Icons.check_circle, color: s.primary),
                    _State.failed => Icon(Icons.cancel, color: s.error),
                  },
                  title: Text(titles[id] ?? fallback),
                  trailing: _stepTime(id, t, s),
                ),
            ],
          ),
        ),
        if (failure != null) Notice(error: true, text: failure!),
        if (failure != null && _tail().isNotEmpty)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(top: 8),
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: s.surfaceContainerHighest,
              borderRadius: BorderRadius.circular(12),
            ),
            child: SelectableText(
              _tail().join('\n'),
              style: t.bodySmall?.copyWith(
                fontFamily: 'monospace',
                color: s.onSurfaceVariant,
              ),
            ),
          ),
        if (!running) ...[
          const SizedBox(height: 16),
          Wrap(
            spacing: 12,
            runSpacing: 8,
            children: [
              if (failure != null)
                FilledButton.icon(
                  onPressed: widget.ctx.restart,
                  icon: const Icon(Icons.refresh),
                  label: const Text('Tentar de novo'),
                ),
              OutlinedButton.icon(
                onPressed: widget.ctx.goBack,
                icon: const Icon(Icons.arrow_back),
                label: Text(
                  failure != null ? 'Voltar ao resumo' : 'Instalar de novo',
                ),
              ),
            ],
          ),
        ],
        if (finished && cfg.flash)
          Notice(
            icon: Icons.celebration_outlined,
            text:
                'O aparelho esta reiniciando no Thunder OS. Com o cabo ligado: '
                'ssh ${Platform.isWindows ? '<usuario da imagem>' : cfg.user}@172.16.42.1 (o primeiro boot demora mais, porque o '
                'sistema cresce ate ocupar a particao).',
          ),
        if (finished && !cfg.flash)
          Notice(
            icon: Icons.inventory_outlined,
            text:
                'Imagens em build/${widget.ctx.device!.id}/out/. Para gravar depois: '
                'tools/flash.sh --sim',
          ),
        const SizedBox(height: 16),
        Row(
          children: [
            TextButton.icon(
              onPressed: () => setState(() => showLog = !showLog),
              icon: Icon(showLog ? Icons.expand_less : Icons.expand_more),
              label: Text(showLog ? 'Esconder detalhes' : 'Mostrar detalhes'),
            ),
            const Spacer(),
            if (running)
              OutlinedButton.icon(
                onPressed: run == null ? null : _cancel,
                icon: const Icon(Icons.stop_circle_outlined),
                label: const Text('Parar'),
              ),
          ],
        ),
        if (showLog)
          Container(
            height: 320,
            margin: const EdgeInsets.only(top: 8),
            decoration: BoxDecoration(
              color: s.surfaceContainerHighest,
              borderRadius: BorderRadius.circular(12),
            ),
            child: SelectionArea(
              child: ListView.builder(
                controller: scroll,
                padding: const EdgeInsets.all(12),
                itemCount: log.length,
                itemBuilder: (context, i) => Text(
                  log[i],
                  style: t.bodySmall?.copyWith(
                    fontFamily: 'monospace',
                    color: s.onSurfaceVariant,
                  ),
                ),
              ),
            ),
          ),
      ],
    );
  }
}
