import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:thunder_installer/progress.dart';

void main() {
  final t0 = DateTime(2026, 10, 10, 12);
  DateTime at(int s) => t0.add(Duration(seconds: s));

  ProgressEstimator est() => ProgressEstimator(
    ['a', 'b', 'c'],
    {
      'a': const Duration(seconds: 100),
      'b': const Duration(seconds: 300),
      'c': const Duration(seconds: 100),
    },
  );

  test('pondera pelo tempo esperado de cada etapa', () {
    final p = est()..start('a', at(0));
    expect(p.fraction(at(50)), closeTo(50 / 500, 0.001));
    expect(p.remaining(at(50)), const Duration(seconds: 450));
    p.start('b', at(100));
    expect(p.fraction(at(250)), closeTo(250 / 500, 0.001));
  });

  test('etapa pulada sai da conta e o restante cai', () {
    final p = est()..start('a', at(0));
    p.start('b', at(100));
    p.skip();
    p.start('c', at(101));
    expect(p.took['b'], Duration.zero);
    expect(p.remaining(at(101)), const Duration(seconds: 100));
    expect(p.measured().containsKey('b'), isFalse);
  });

  test(
    'etapa que passa do esperado nao zera o restante nem volta a porcentagem',
    () {
      final p = est()..start('a', at(0));
      final before = p.fraction(at(90));
      // 'a' esperava 100 s e ja leva 400 s.
      expect(p.remaining(at(400)), greaterThan(const Duration(seconds: 400)));
      expect(p.fraction(at(400)), greaterThanOrEqualTo(before));
      expect(p.fraction(at(400)), lessThan(1));
    },
  );

  test('guarda e le os tempos medidos', () {
    final dir = Directory.systemTemp.createTempSync('thunder-times');
    addTearDown(() => dir.deleteSync(recursive: true));
    final f = File('${dir.path}/install-times.json');
    final p = est()..start('a', at(0));
    p.start('b', at(120));
    p.finish(at(420));
    saveEstimates(f, const {'c': Duration(seconds: 7)}, p.measured());
    final read = loadEstimates(f);
    expect(read['a'], const Duration(seconds: 120));
    expect(read['b'], const Duration(seconds: 300));
    expect(read['c'], const Duration(seconds: 7));
    expect(loadEstimates(File('${dir.path}/nao-existe.json')), isEmpty);
  });

  test('formatos', () {
    expect(formatRemaining(const Duration(seconds: 20)), 'menos de 1 min');
    expect(formatRemaining(const Duration(seconds: 61)), 'cerca de 2 min');
    expect(formatRemaining(const Duration(minutes: 65)), 'cerca de 1 h 05 min');
    expect(formatElapsed(const Duration(seconds: 75)), '01:15');
    expect(formatElapsed(const Duration(hours: 1, seconds: 5)), '1:00:05');
  });
}
