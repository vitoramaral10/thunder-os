import 'package:flutter/material.dart';

/// Semente do esquema: ambar, o "trovao" do nome. Unica cor escolhida a mao; o
/// resto sai do gerador do Material 3.
const seedColor = Color(0xFFF2A900);

ThemeData buildTheme(Brightness brightness) {
  final scheme = ColorScheme.fromSeed(
    seedColor: seedColor,
    brightness: brightness,
  );
  return ThemeData(
    colorScheme: scheme,
    visualDensity: VisualDensity.standard,
    inputDecorationTheme: const InputDecorationTheme(
      border: OutlineInputBorder(),
    ),
  );
}
