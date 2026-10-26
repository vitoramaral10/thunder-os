#!/bin/bash
# Leva o aparelho do cabo para o fastboot, sem botao: pelo adb (Android de fabrica, com
# depuracao USB) ou pelo SSH (Thunder OS rodando, thunder-reboot). Usado pelo install.sh;
# o instalador grafico faz o mesmo em installer/lib/backend.dart.
set -euo pipefail

# Primeiro aparelho USB pronto no adb. Os de rede (ip:porta, adb sem fio por mDNS) ficam
# de fora: nao estao no cabo, e reinicia-los no bootloader seria mexer em outro celular.
adb_serial=
if command -v adb >/dev/null; then
	adb_serial=$(adb devices 2>/dev/null | awk 'NR > 1 && $2 == "device" && $1 !~ /:|\._adb-tls-/ { print $1; exit }')
fi

if [ -n "$adb_serial" ]; then
	echo "adb: reboot bootloader ($adb_serial)"
	adb -s "$adb_serial" reboot bootloader
elif ping -c 1 -W 1 172.16.42.1 >/dev/null 2>&1; then
	echo "ssh: thunder-reboot bootloader"
	# O aparelho cai antes de responder: limite de tempo, senao o ssh espera minutos.
	timeout 15 ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=2 \
		-o ServerAliveCountMax=3 root@172.16.42.1 thunder-reboot bootloader </dev/null || true
else
	echo "nenhum aparelho no adb nem no SSH: entre no fastboot pelos botoes" >&2
	exit 1
fi
