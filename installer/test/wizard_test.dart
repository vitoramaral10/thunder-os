import 'package:flutter_test/flutter_test.dart';
import 'package:thunder_installer/wizard.dart';

void main() {
  group('lastReachable', () {
    test('nunca deixa abrir a instalacao pela barra lateral', () {
      // Tudo valido (o resumo sempre e): o maximo e o resumo, nao a instalacao.
      final all = List.filled(7, true);
      expect(lastReachable(all), all.length - 2);
    });

    test('para na primeira etapa invalida', () {
      expect(lastReachable([true, false, true, true, true, true, false]), 1);
      expect(lastReachable([false, true, true, true, true, true, false]), 0);
    });

    test('a ordem real termina em resumo e instalacao', () {
      expect(steps.last.title, 'Instalacao');
      expect(steps[steps.length - 2].title, 'Resumo');
    });
  });
}
