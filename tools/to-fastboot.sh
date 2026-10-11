#!/bin/bash
# Leva o aparelho do cabo para o fastboot, sem botao: pelo adb (Android de fabrica, com
# depuracao USB) ou pelo SSH (Thunder OS rodando, thunder-reboot). Usado pelo instalador.
set -euo pipefail

if command -v adb >/dev/null && adb get-state 2>/dev/null | grep -qx device; then
	echo "adb: reboot bootloader"
	adb reboot bootloader
elif ping -c 1 -W 1 172.16.42.1 >/dev/null 2>&1; then
	echo "ssh: thunder-reboot bootloader"
	# O aparelho cai antes de responder: limite de tempo, senao o ssh espera minutos.
	timeout 15 ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=2 \
		-o ServerAliveCountMax=3 root@172.16.42.1 thunder-reboot bootloader </dev/null || true
else
	echo "nenhum aparelho no adb nem no SSH: entre no fastboot pelos botoes" >&2
	exit 1
fi
