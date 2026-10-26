#!/bin/bash
# Instalacao de ponta a ponta, usada pelo instalador grafico (e utilizavel a mao):
# toolchain, kernel, firmware da ROM, rootfs, boot.img, imagem da raiz e gravacao.
# Opcoes pelo ambiente (as mesmas dos scripts de tools/):
#   DEVICE, THUNDER_ROM, THUNDER_PROFILE, THUNDER_USER, THUNDER_PASSWORD,
#   THUNDER_ROOT_PASSWORD, THUNDER_HOSTNAME, THUNDER_LOCALE, THUNDER_TIMEZONE,
#   THUNDER_EXTRA_PACKAGES, AUTHORIZED_KEYS, SIZE
#   THUNDER_FLASH=1   grava no aparelho no fim (sem isso, so monta as imagens)
#
# Para o app, cada etapa abre com uma linha "::step <id> <titulo>" e o fim e
# "::done" ou "::error <mensagem>". "::skip" diz que a etapa atual foi pulada (ja feita),
# para o app nao contar o tempo dela na estimativa. O resto da saida e o log dos scripts.
# O corpo vai num bloco: o bash le o bloco inteiro antes de executar, entao editar
# este arquivo com ele rodando nao muda a execucao em curso.
{
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device
take_build_lock

t=$repo/tools
current=
step() { current=$1; shift; echo "::step $current $*"; }
trap 'rc=$?; [ $rc = 0 ] || echo "::error etapa $current falhou (codigo $rc)"' EXIT

step cross "Toolchain de compilacao"
if [ -x "$BUILD/cross/usr/bin/aarch64-linux-gnu-gcc" ] || command -v aarch64-linux-gnu-gcc >/dev/null; then
	echo "::skip"; echo "ja existe"
else
	"$t/fetch-cross.sh"
fi

step kernel "Kernel do aparelho"
stamp=$KERNEL_OUT/.thunder-inputs
want=$(kernel_inputs)  # tools/common.sh
if [ -d "$KERNEL_OUT/lib/modules/$KVER" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$want" ]; then
	echo "::skip"; echo "ja compilado com estas entradas"
else
	"$t/build-kernel.sh"
	echo "$want" > "$stamp"
fi
# O pacote do kernel (linux-<aparelho>) e rapido de refazer e e o que a imagem instala.
"$t/package-kernel.sh"

step firmware "Firmware do aparelho"
if [ ${#FIRMWARE_FILES[@]} -eq 0 ]; then
	echo "::skip"; echo "o aparelho nao precisa"
elif [ -n "${THUNDER_ROM:-}" ]; then
	# Mesma ROM (caminho, tamanho, data) e mesma lista de arquivos: nao extrai de novo.
	rom_stamp="$(realpath "$THUNDER_ROM") $(stat -c '%s %Y' "$THUNDER_ROM") ${FIRMWARE_FILES[*]}"
	have_all=1
	for f in "${FIRMWARE_FILES[@]}"; do [ -s "$FIRMWARE/$f" ] || have_all=0; done
	if [ "$have_all" = 1 ] && [ "$(cat "$WORK/firmware.stamp" 2>/dev/null)" = "$rom_stamp" ]; then
		echo "::skip"; echo "ja extraido desta ROM"
	else
		"$t/extract-firmware.sh" "$THUNDER_ROM"
		echo "$rom_stamp" > "$WORK/firmware.stamp"
	fi
elif [ -n "$(ls -A "$FIRMWARE" 2>/dev/null)" ]; then
	echo "usando o que ja esta em $FIRMWARE"
else
	echo "::skip"; echo "sem ROM: o aparelho copia o firmware da particao super no 1o boot"
fi

step rootfs "Sistema (Arch Linux ARM)"
"$t/build-rootfs.sh"

step boot "Imagem de boot"
"$t/build-boot.sh"

step userdata "Imagem da particao $ROOT_PARTITION"
"$t/make-userdata.sh"

if [ "${THUNDER_FLASH:-0}" = 1 ]; then
	step flash "Gravacao no aparelho"
	if ! fastboot devices 2>/dev/null | grep -q fastboot; then
		"$t/to-fastboot.sh" || echo "entre no fastboot pelos botoes do aparelho; esperando..."
	fi
	FASTBOOT_WAIT=${FASTBOOT_WAIT:-180} "$t/flash.sh" --sim
fi

echo "::done"
exit
}
