// Porcentagem e tempo restante da instalacao. As etapas tem duracoes muito
// diferentes (o kernel leva minutos, o boot.img segundos), entao o progresso e
// ponderado pelo tempo esperado de cada uma. O tempo esperado vem do que a ultima
// instalacao mediu neste computador, ou de um padrao.
import 'dart:convert';
import 'dart:io';
import 'dart:math';

/// Medidos num notebook de 8 nucleos (2026-10-10), primeira instalacao.
const defaultEstimates = {
  'cross': Duration(seconds: 90),
  'kernel': Duration(minutes: 18),
  'firmware': Duration(seconds: 40),
  'rootfs': Duration(minutes: 15),
  'boot': Duration(seconds: 15),
  'userdata': Duration(seconds: 60),
  'flash': Duration(minutes: 3),
};

class ProgressEstimator {
  ProgressEstimator(this.steps, Map<String, Duration> estimates)
    : estimates = {...defaultEstimates, ...estimates};

  /// Ids das etapas, na ordem.
  final List<String> steps;
  final Map<String, Duration> estimates;

  /// Quanto cada etapa terminada levou (zero se foi pulada).
  final Map<String, Duration> took = {};
  final Set<String> skipped = {};
  String? current;
  DateTime? _currentStart;

  DateTime? get startedAt => _currentStart;
  double _best = 0;

  Duration _est(String id) => estimates[id] ?? const Duration(minutes: 1);

  void start(String id, DateTime now) {
    finish(now);
    current = id;
    _currentStart = now;
  }

  void skip() {
    if (current != null) skipped.add(current!);
  }

  void finish(DateTime now) {
    final c = current;
    if (c == null) return;
    took[c] = skipped.contains(c)
        ? Duration.zero
        : now.difference(_currentStart!);
    current = null;
    _currentStart = null;
  }

  Duration _elapsedCurrent(DateTime now) =>
      _currentStart == null ? Duration.zero : now.difference(_currentStart!);

  /// Tempo que falta: o resto da etapa atual mais as que ainda nao comecaram.
  /// Etapa que passou do esperado conta um resto minimo, para o numero nao zerar
  /// com trabalho ainda rodando.
  Duration remaining(DateTime now) {
    var left = Duration.zero;
    for (final id in steps) {
      if (took.containsKey(id)) continue;
      if (id == current) {
        final est = _est(id);
        final rest = est - _elapsedCurrent(now);
        final floor = Duration(seconds: max(5, est.inSeconds ~/ 20));
        left += rest > floor ? rest : floor;
      } else {
        left += _est(id);
      }
    }
    return left;
  }

  /// Fracao de 0 a 1. Nunca anda para tras.
  double fraction(DateTime now) {
    var done = Duration.zero;
    for (final d in took.values) {
      done += d;
    }
    done += _elapsedCurrent(now);
    final left = remaining(now);
    final total = done + left;
    final f = total == Duration.zero
        ? 0.0
        : done.inMilliseconds / total.inMilliseconds;
    _best = max(_best, min(f, 0.99));
    return _best;
  }

  /// Duracoes reais das etapas que rodaram de verdade, para a proxima estimativa.
  Map<String, Duration> measured() => {
    for (final e in took.entries)
      if (!skipped.contains(e.key) && e.value > Duration.zero) e.key: e.value,
  };
}

/// "cerca de 12 min", "menos de 1 min", "cerca de 1 h 05 min".
String formatRemaining(Duration d) {
  if (d < const Duration(minutes: 1)) return 'menos de 1 min';
  final mins = (d.inSeconds / 60).ceil();
  if (mins < 60) return 'cerca de $mins min';
  return 'cerca de ${mins ~/ 60} h ${(mins % 60).toString().padLeft(2, '0')} min';
}

String formatElapsed(Duration d) {
  final h = d.inHours, m = d.inMinutes % 60, s = d.inSeconds % 60;
  final mm = m.toString().padLeft(2, '0'), ss = s.toString().padLeft(2, '0');
  return h > 0 ? '$h:$mm:$ss' : '$mm:$ss';
}

/// Tempos guardados em `build/<aparelho>/install-times.json`.
Map<String, Duration> loadEstimates(File f) {
  try {
    final j = jsonDecode(f.readAsStringSync()) as Map<String, dynamic>;
    return {
      for (final e in j.entries)
        if (e.value is num) e.key: Duration(seconds: (e.value as num).round()),
    };
  } on Object {
    return const {};
  }
}

void saveEstimates(
  File f,
  Map<String, Duration> previous,
  Map<String, Duration> measured,
) {
  final merged = {...previous, ...measured};
  try {
    f.writeAsStringSync(
      jsonEncode({for (final e in merged.entries) e.key: e.value.inSeconds}),
    );
  } on FileSystemException {
    // Sem gravar, a proxima estimativa usa o padrao. Nao vale parar por isso.
  }
}
