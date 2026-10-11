import 'package:flutter/material.dart';

import 'backend.dart';
import 'theme.dart';
import 'wizard.dart';

void main() {
  runApp(ThunderInstallerApp(backend: Backend.locate()));
}

class ThunderInstallerApp extends StatelessWidget {
  const ThunderInstallerApp({super.key, required this.backend});

  final Backend? backend;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Instalador do Thunder OS',
      debugShowCheckedModeBanner: false,
      theme: buildTheme(Brightness.light),
      darkTheme: buildTheme(Brightness.dark),
      home: backend == null ? const _NoRepo() : Wizard(backend: backend!),
    );
  }
}

class _NoRepo extends StatelessWidget {
  const _NoRepo();

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return Scaffold(
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 520),
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(
                  Icons.folder_off_outlined,
                  size: 48,
                  color: Theme.of(context).colorScheme.error,
                ),
                const SizedBox(height: 16),
                Text(
                  'Repositorio do Thunder OS nao encontrado',
                  style: t.titleLarge,
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 8),
                Text(
                  'Abra o instalador de dentro do repositorio, ou defina '
                  'THUNDER_OS_REPO com o caminho dele.',
                  style: t.bodyMedium,
                  textAlign: TextAlign.center,
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
