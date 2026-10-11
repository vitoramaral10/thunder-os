import 'package:flutter_test/flutter_test.dart';
import 'package:thunder_installer/backend.dart';

void main() {
  test('sinal vira "interrompida de fora", erro comum vira codigo', () {
    expect(exitMessage(-15), contains('sinal TERM'));
    expect(exitMessage(143), contains('sinal TERM'));
    expect(exitMessage(-9), contains('sinal KILL'));
    expect(exitMessage(1), contains('codigo 1'));
    expect(exitMessage(1), isNot(contains('sinal')));
    expect(exitMessage(75), contains('Outra instalacao'));
  });
}
