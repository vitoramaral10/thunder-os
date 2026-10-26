#!/bin/bash
# Catalogo para o instalador, em JSON no stdout: aparelhos (devices/*/device.sh) e
# perfis de interface (rootfs/profiles/*.sh). Nao precisa de DEVICE.
set -euo pipefail
. "$(dirname "$0")/common.sh"

js() {
	local s=$1
	s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\t'/\\t}
	printf '"%s"' "$s"
}
jarr() {
	local first=1 x
	printf '['
	for x in "$@"; do
		[ "$first" = 1 ] || printf ','
		first=0
		js "$x"
	done
	printf ']'
}

printf '{"devices":['
first=1
for d in "$repo"/devices/*/device.sh; do
	name=$(basename "$(dirname "$d")")
	# Cada aparelho num subshell: os device.sh definem as mesmas variaveis.
	(
		DEVICE=$name
		require_device >/dev/null
		[ "$first" = 1 ] || printf ','
		printf '{"id":%s,"name":%s,"vendor":%s,"codename":%s,"soc":%s,"status":%s,' \
			"$(js "$DEVICE")" "$(js "$DEVICE_NAME")" "$(js "$DEVICE_VENDOR")" \
			"$(js "$DEVICE_CODENAME")" "$(js "${DEVICE_SOC:-}")" "$(js "${DEVICE_STATUS:-bring-up}")"
		printf '"fastboot_products":%s,"android_devices":%s,"root_partition":%s,' \
			"$(jarr "${FASTBOOT_PRODUCTS[@]}")" "$(jarr "${ANDROID_DEVICES[@]}")" "$(js "$ROOT_PARTITION")"
		printf '"wifi_interface":%s,' "$(js "$WIFI_INTERFACE")"
		# Para a gravacao feita pelo proprio app (Windows): o mesmo que o flash.sh le.
		printf '"fastboot_ab":%s,"fastboot_slot":%s,"disable_vbmeta":%s,' \
			"$([ "$FASTBOOT_AB" = 1 ] && echo true || echo false)" "$(js "$FASTBOOT_SLOT")" \
			"$([ "$FASTBOOT_DISABLE_VBMETA" = 1 ] && echo true || echo false)"
		printf '"needs_rom":%s,"kernel_built":%s,"profiles":%s}' \
			"$([ ${#FIRMWARE_FILES[@]} -gt 0 ] && echo true || echo false)" \
			"$([ -d "$KERNEL_OUT/lib/modules/$KVER" ] && echo true || echo false)" \
			"$(jarr "${DEVICE_PROFILES[@]}")"
	)
	first=0
done
printf '],"profiles":['
first=1
for p in "$repo"/rootfs/profiles/*.sh; do
	(
		PROFILE_STATUS=experimental PROFILE_BLOCKERS= PROFILE_PACKAGES=() PROFILE_SERVICES=()
		. "$p"
		[ "$first" = 1 ] || printf ','
		printf '{"id":%s,"order":%s,"title":%s,"description":%s,"status":%s,"blockers":%s,"packages":%s}' \
			"$(js "$(basename "$p" .sh)")" "${PROFILE_ORDER:-50}" "$(js "$PROFILE_TITLE")" "$(js "$PROFILE_DESCRIPTION")" \
			"$(js "$PROFILE_STATUS")" "$(js "${PROFILE_BLOCKERS:-}")" "$(jarr "${PROFILE_PACKAGES[@]}")"
	)
	first=0
done
printf ']}\n'
