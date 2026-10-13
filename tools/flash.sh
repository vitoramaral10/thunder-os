#!/bin/bash
# Grava o Thunder OS no aparelho pelo fastboot. Sem --sim, so mostra o plano.
#
#   tools/flash.sh            mostra o que seria gravado
#   tools/flash.sh --sim      grava vbmeta (verificacao desligada), boot e a raiz
#   tools/flash.sh --sim --so-boot   grava so o boot.img (troca de kernel/initramfs)
#
# A particao da raiz (ROOT_PARTITION, em geral userdata) e APAGADA. O bootloader precisa
# estar desbloqueado; nada aqui o trava. Confere o produto (FASTBOOT_PRODUCTS) antes.
# O corpo vai num bloco: o bash le o bloco inteiro antes de executar, entao editar
# este arquivo com ele rodando nao muda a execucao em curso.
{
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device
take_build_lock

out=$OUT
# BOOT_IMG grava outro boot.img (o update-kernel.sh usa o que o aparelho montou).
boot_img=${BOOT_IMG:-$out/boot.img}
sim=0
so_boot=0
for a in "$@"; do
	case $a in
	--sim) sim=1 ;;
	--so-boot) so_boot=1 ;;
	*) echo "argumento desconhecido: $a" >&2; exit 2 ;;
	esac
done

[ -s "$boot_img" ] || { echo "falta $boot_img" >&2; exit 1; }
[ "$so_boot" = 1 ] || [ -s "$out/userdata.img" ] || { echo "falta $out/userdata.img" >&2; exit 1; }

# Logo depois de o aparelho entrar no fastboot, o getvar as vezes fica preso em
# "< waiting for any device >": timeout em cada chamada e espera ate o LK responder.
var() { timeout 10 fastboot getvar "$1" 2>&1 | sed -n "s/^$1: *//p" | head -n 1; }
wait_fastboot() {
	local i
	for i in $(seq 1 "${FASTBOOT_WAIT:-30}"); do
		[ -n "$(var product)" ] && return 0
		sleep 1
	done
	return 1
}

echo "Aparelho: $DEVICE_VENDOR $DEVICE_NAME ($DEVICE)"
echo "Plano:"
[ "$so_boot" = 1 ] || [ "$FASTBOOT_DISABLE_VBMETA" != 1 ] ||
	echo "  vbmeta    <- vbmeta vazio com a verificacao desligada (flags 2)"
echo "  boot      <- $boot_img ($(stat -c %s "$boot_img") B)"
[ "$so_boot" = 1 ] || echo "  $ROOT_PARTITION  <- $out/userdata.img ($(stat -c %s "$out/userdata.img") B)  APAGA A PARTICAO"
[ "$sim" = 1 ] || { echo "Nada gravado. Rode com --sim para gravar."; exit 0; }

wait_fastboot || { echo "nenhum aparelho respondendo no fastboot" >&2; exit 1; }
product=$(var product)
ok=0
for p in "${FASTBOOT_PRODUCTS[@]}"; do [ "$product" = "$p" ] && ok=1; done
[ "$ok" = 1 ] || { echo "produto '$product' nao e $DEVICE (${FASTBOOT_PRODUCTS[*]})" >&2; exit 1; }
[ "$(var unlocked)" = yes ] || { echo "bootloader travado" >&2; exit 1; }
suffix=
if [ "$FASTBOOT_AB" = 1 ]; then
	slot=$(var current-slot)
	[ -n "$slot" ] || { echo "o aparelho nao informa current-slot; FASTBOOT_AB=0?" >&2; exit 1; }
	# FASTBOOT_SLOT fixa o slot testado; vazio aceita o ativo.
	[ -z "$FASTBOOT_SLOT" ] || [ "$slot" = "$FASTBOOT_SLOT" ] ||
		{ echo "slot ativo e '$slot', esperado '$FASTBOOT_SLOT'" >&2; exit 1; }
	suffix=_$slot
fi

if [ "$so_boot" != 1 ] && [ "$FASTBOOT_DISABLE_VBMETA" = 1 ]; then
	avbtool make_vbmeta_image --flags 2 --padding_size 2048 --output "$out/vbmeta-disabled.img"
	# A imagem ja traz a flag 2. As opcoes --disable-verity/--disable-verification do
	# fastboot 37 recusam esta imagem ("Failed to find AVB_MAGIC at offset: 0").
	fastboot flash "vbmeta$suffix" "$out/vbmeta-disabled.img"
fi
fastboot flash "boot$suffix" "$boot_img"
[ "$so_boot" = 1 ] || fastboot flash "$ROOT_PARTITION" "$out/userdata.img"
fastboot reboot
echo "Gravado. Rede pelo cabo: 172.16.42.1 (ssh, ou telnet com login)."
exit
}
