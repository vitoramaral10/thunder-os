#!/bin/bash
# Carimbo das entradas do kernel (tools/common.sh, kernel_inputs).
#
#   tools/kernel-stamp.sh          mostra o carimbo atual e o gravado na build
#   tools/kernel-stamp.sh --write  grava o atual em $KERNEL_OUT/.thunder-inputs, para o
#                                  install.sh reaproveitar uma build que voce sabe que
#                                  esta em dia (ex.: depois de mudar so a regra do carimbo)
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device

now=$(kernel_inputs)
stamp=$KERNEL_OUT/.thunder-inputs
if [ "${1:-}" = --write ]; then
	[ -d "$KERNEL_OUT/lib/modules/$KVER" ] || { echo "sem kernel em $KERNEL_OUT" >&2; exit 1; }
	echo "$now" > "$stamp"
	echo "gravado: $now"
else
	echo "atual:   $now"
	echo "gravado: $(cat "$stamp" 2>/dev/null || echo nenhum)"
fi
