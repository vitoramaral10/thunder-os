#!/bin/bash
# Grava o Arch no thunder pelo fastboot. Sem --sim, so mostra o plano.
#
#   tools/flash.sh            mostra o que seria gravado
#   tools/flash.sh --sim      grava vbmeta (verificacao desligada), boot e userdata
#   tools/flash.sh --sim --so-boot   grava so o boot.img (troca de kernel/initramfs)
#
# O userdata e APAGADO. O bootloader precisa estar desbloqueado; nada aqui o trava.
set -euo pipefail
. "$(dirname "$0")/common.sh"

out=$BUILD/out
sim=0
so_boot=0
for a in "$@"; do
	case $a in
	--sim) sim=1 ;;
	--so-boot) so_boot=1 ;;
	*) echo "argumento desconhecido: $a" >&2; exit 2 ;;
	esac
done

for f in boot.img userdata.img; do
	[ "$so_boot" = 1 ] && [ "$f" = userdata.img ] && continue
	[ -s "$out/$f" ] || { echo "falta $out/$f" >&2; exit 1; }
done

# Logo depois de o aparelho entrar no fastboot, o getvar as vezes fica preso em
# "< waiting for any device >": timeout em cada chamada e espera ate o LK responder.
var() { timeout 10 fastboot getvar "$1" 2>&1 | sed -n "s/^$1: *//p" | head -n 1; }
wait_fastboot() {
	local i
	for i in $(seq 1 30); do
		[ -n "$(var product)" ] && return 0
		sleep 1
	done
	return 1
}

echo "Plano:"
[ "$so_boot" = 1 ] || echo "  vbmeta    <- vbmeta vazio com a verificacao desligada (flags 2)"
echo "  boot      <- $out/boot.img ($(stat -c %s "$out/boot.img") B)"
[ "$so_boot" = 1 ] || echo "  userdata  <- $out/userdata.img ($(stat -c %s "$out/userdata.img") B)  APAGA O USERDATA"
[ "$sim" = 1 ] || { echo "Nada gravado. Rode com --sim para gravar."; exit 0; }

wait_fastboot || { echo "nenhum aparelho respondendo no fastboot" >&2; exit 1; }
[ "$(var product)" = thunder ] || { echo "produto nao e thunder: $(var product)" >&2; exit 1; }
[ "$(var unlocked)" = yes ] || { echo "bootloader travado" >&2; exit 1; }
slot=$(var current-slot)
[ "$slot" = a ] || { echo "slot ativo e '$slot', esperado 'a'" >&2; exit 1; }

if [ "$so_boot" != 1 ]; then
	avbtool make_vbmeta_image --flags 2 --padding_size 2048 --output "$out/vbmeta-disabled.img"
	# A imagem ja traz a flag 2. As opcoes --disable-verity/--disable-verification do
	# fastboot 37 recusam esta imagem ("Failed to find AVB_MAGIC at offset: 0").
	fastboot flash "vbmeta_$slot" "$out/vbmeta-disabled.img"
fi
fastboot flash "boot_$slot" "$out/boot.img"
[ "$so_boot" = 1 ] || fastboot flash userdata "$out/userdata.img"
fastboot reboot
echo "Gravado. Rede pelo cabo: 172.16.42.1 (ssh, ou telnet com login)."
