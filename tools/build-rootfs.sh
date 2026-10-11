#!/bin/bash
# Monta o rootfs do Arch Linux ARM para o aparelho em $WORK/rootfs, sem root no host:
# user namespace + binfmt_misc proprio do namespace + qemu-aarch64 estatico.
#
# Entradas: $WORK/kernel (tools/build-kernel.sh), firmware proprietario em $FIRMWARE
#           (tools/extract-firmware.sh), chave SSH publica em $AUTHORIZED_KEYS (opcional),
#           perfil de interface em rootfs/profiles/$THUNDER_PROFILE.sh.
# O corpo vai num bloco: o bash le o bloco inteiro antes de executar, entao editar
# este arquivo com ele rodando nao muda a execucao em curso.
{
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device
take_build_lock

# Chave de assinatura do Arch Linux ARM Build System <builder@archlinuxarm.org>.
ALARM_KEY=68B3537F39A313B3E574D06777193F152BDBE6A6
TARBALL=ArchLinuxARM-aarch64-latest.tar.gz
# Chave SSH: AUTHORIZED_KEYS= (vazio) monta sem chave, so com login por senha.
if [ -z "${AUTHORIZED_KEYS+x}" ]; then
	AUTHORIZED_KEYS=$HOME/.ssh/id_ed25519.pub
	[ -s "$AUTHORIZED_KEYS" ] || AUTHORIZED_KEYS=
fi
THUNDER_USER=${THUNDER_USER:-$(id -un)}
# Senha provisoria do usuario (e de root, se THUNDER_ROOT_PASSWORD nao vier): troque no
# aparelho.
THUNDER_PASSWORD=${THUNDER_PASSWORD:-147147}
THUNDER_ROOT_PASSWORD=${THUNDER_ROOT_PASSWORD:-$THUNDER_PASSWORD}
THUNDER_PROFILE=${THUNDER_PROFILE:-none}
# Rede Wi-Fi ja configurada na imagem (opcional). A senha vira PSK no chroot.
THUNDER_WIFI_SSID=${THUNDER_WIFI_SSID:-}
THUNDER_WIFI_PASSWORD=${THUNDER_WIFI_PASSWORD:-}
# Pacotes extras do Arch, separados por espaco.
read -r -a EXTRA_PACKAGES <<< "${THUNDER_EXTRA_PACKAGES:-}"
THUNDER_LOCALE=${THUNDER_LOCALE:-en_US.UTF-8}
THUNDER_TIMEZONE=${THUNDER_TIMEZONE:-UTC}
THUNDER_HOSTNAME=${THUNDER_HOSTNAME:-$DEVICE_CODENAME}
pac="pacman --noconfirm --needed --disable-sandbox"

KERNEL_PKG=$OUT/linux-$DEVICE.pkg.tar.zst
[ -s "$KERNEL_PKG" ] || { echo "sem o pacote do kernel: rode tools/build-kernel.sh e tools/package-kernel.sh" >&2; exit 1; }
for f in "${FIRMWARE_REQUIRED[@]}" "${INITRAMFS_FIRMWARE[@]}"; do
	[ -s "$FIRMWARE/$f" ] || { echo "sem $f em $FIRMWARE (devices/$DEVICE/docs)" >&2; exit 1; }
done
[ -z "$AUTHORIZED_KEYS" ] || [ -s "$AUTHORIZED_KEYS" ] || { echo "sem chave SSH em $AUTHORIZED_KEYS" >&2; exit 1; }
[[ $THUNDER_USER =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] && [ "$THUNDER_USER" != root ] ||
	{ echo "usuario invalido: $THUNDER_USER" >&2; exit 1; }

profile_file=$repo/rootfs/profiles/$THUNDER_PROFILE.sh
[ -f "$profile_file" ] || { echo "perfil desconhecido: $THUNDER_PROFILE" >&2; exit 1; }
[[ " ${DEVICE_PROFILES[*]} " == *" $THUNDER_PROFILE "* ]] ||
	{ echo "$DEVICE nao aceita o perfil $THUNDER_PROFILE (${DEVICE_PROFILES[*]})" >&2; exit 1; }
PROFILE_PACKAGES=() PROFILE_SERVICES=() PROFILE_LOCAL_PACKAGES=() PROFILE_BUILD_DEPENDS=()
PROFILE_GROUPS=()
PROFILE_STATUS=experimental PROFILE_BLOCKERS=
. "$profile_file"
if [ "${PROFILE_STATUS:-}" = planned ] && [ "${THUNDER_ALLOW_PLANNED:-0}" != 1 ]; then
	echo "o perfil $THUNDER_PROFILE ainda nao instala: ${PROFILE_BLOCKERS:-falta base}" >&2
	echo "(THUNDER_ALLOW_PLANNED=1 monta assim mesmo, para quem esta portando)" >&2
	exit 1
fi
LOCAL_PACKAGES=(openrc "${PROFILE_LOCAL_PACKAGES[@]}")
echo "perfil: $THUNDER_PROFILE ($PROFILE_TITLE)"

echo "== qemu-aarch64 estatico"
if [ ! -x "$QEMU" ]; then
	f=$(fetch_host_pkg qemu-user-static)
	bsdtar -xOf "$f" usr/bin/qemu-aarch64-static > "$QEMU"
	chmod 755 "$QEMU"
fi

echo "== tarball do Arch Linux ARM, com assinatura"
# THUNDER_REFRESH_BASE=1 baixa o tarball de novo e refaz a base do zero.
if [ "${THUNDER_REFRESH_BASE:-0}" = 1 ]; then
	rm -f "$BUILD/dl/$TARBALL" "$BUILD/dl/$TARBALL.sig" "$BASE.key"
fi
for f in "$TARBALL" "$TARBALL.sig"; do
	[ -s "$BUILD/dl/$f" ] || curl -fL -o "$BUILD/dl/$f" "http://os.archlinuxarm.org/os/$f"
done
export GNUPGHOME=$BUILD/tools/gnupg
mkdir -p -m 700 "$GNUPGHOME"
gpg --batch --list-keys "$ALARM_KEY" >/dev/null 2>&1 ||
	gpg --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys "$ALARM_KEY"
gpg --batch --status-fd 1 --verify "$BUILD/dl/$TARBALL.sig" "$BUILD/dl/$TARBALL" 2>/dev/null |
	grep -q "^\[GNUPG:\] VALIDSIG $ALARM_KEY " || { echo "assinatura invalida" >&2; exit 1; }

export DEVICE
tools_achroot() { "$repo/tools/achroot.sh" "$@"; }
# Copia de diretorio do rootfs: no btrfs/xfs o reflink nao duplica nada no disco.
tree_copy() { userns cp -a --reflink=auto "$1" "$2"; }

# Base: o tarball extraido, com keyring, atualizado e sem o kernel generico, o
# mkinitcpio e o linux-firmware. Igual para todos os aparelhos e perfis, entao fica em
# cache ($BASE) e so e refeita quando o tarball muda; nas outras vezes recebe so a
# atualizacao incremental do dia.
base_key=$(sha256sum "$BUILD/dl/$TARBALL" | cut -c1-16)
if [ -d "$BASE/usr" ] && [ "$(cat "$BASE.key" 2>/dev/null)" = "$base_key" ]; then
	echo "== base em cache ($(date -r "$BASE.key" '+%d/%m %H:%M')); atualizacao incremental"
	ROOTFS=$BASE tools_achroot /bin/bash -c "$pac -Syu" ||
		echo "aviso: a base nao atualizou (sem rede?); seguindo com a do cache" >&2
else
	echo "== base nova: extracao, keyring, atualizacao; fora o kernel generico"
	userns rm -rf "${BASE:?}" "${BUILDROOT:?}"
	rm -f "$BASE.key"
	mkdir -p "$BASE"
	userns bsdtar -xpf "$BUILD/dl/$TARBALL" -C "$BASE" --numeric-owner
	ROOTFS=$BASE tools_achroot /bin/bash -c "pacman-key --init && pacman-key --populate archlinuxarm && $pac -Syu"
	ROOTFS=$BASE tools_achroot /bin/bash -c "pacman -Rns --noconfirm --disable-sandbox linux-aarch64 mkinitcpio mkinitcpio-busybox \$(pacman -Qq | grep '^linux-firmware')"
	echo "$base_key" > "$BASE.key"
fi

echo "== rootfs do aparelho a partir da base"
userns rm -rf "${ROOTFS:?}"
tree_copy "$BASE" "$ROOTFS"
export ROOTFS

# Pacotes nossos e o thunder-reboot: guardados em $LOCAL_PKGS/<nome>/<hash da receita>.
# So o que mudou e compilado, num chroot de build ($BUILDROOT) que fica fora do rootfs.
recipe_hash() { (cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) | sha256sum | cut -c1-16; }
declare -A PKG_DIR
need_build=()
for p in "${LOCAL_PACKAGES[@]}"; do
	PKG_DIR[$p]=$LOCAL_PKGS/$p/$(recipe_hash "$repo/rootfs/packages/$p")
	compgen -G "${PKG_DIR[$p]}/*.pkg.tar.*" >/dev/null || need_build+=("$p")
done
REBOOT_DIR=$LOCAL_PKGS/thunder-reboot/$(sha256sum "$repo/rootfs/src/thunder-reboot.c" | cut -c1-16)
[ -x "$REBOOT_DIR/thunder-reboot" ] || need_build+=(thunder-reboot)

if [ ${#need_build[@]} -eq 0 ]; then
	echo "== pacotes nossos em cache: ${LOCAL_PACKAGES[*]} thunder-reboot"
else
	echo "== chroot de build para: ${need_build[*]}"
	if [ ! -d "$BUILDROOT/usr" ]; then
		tree_copy "$BASE" "$BUILDROOT"
	fi
	ROOTFS=$BUILDROOT tools_achroot /bin/bash -c "$pac -Syu base-devel meson psmisc ${PROFILE_BUILD_DEPENDS[*]} && (id builder >/dev/null 2>&1 || useradd -m builder)"
	# Valores entram nos shells do namespace pelo ambiente, nunca colados no codigo: um
	# caminho ou uma senha com apostrofo quebraria a sintaxe.
	export repo BUILDROOT
	UIDGID=$(userns awk -F: '$1=="builder"{print $3":"$4}' "$BUILDROOT/etc/passwd")
	export UIDGID
	# Na ordem: cada pacote entra no buildroot depois de pronto (ou do cache), para o
	# seguinte poder depender dele.
	for p in "${LOCAL_PACKAGES[@]}"; do
		export p dest=${PKG_DIR[$p]}
		if [[ " ${need_build[*]} " == *" $p "* ]]; then
			echo "== pacote local: $p"
			userns bash -c 'set -e
				w=$BUILDROOT/home/builder/work
				rm -rf "${w:?}/$p"; mkdir -p "$w"
				cp -a "$repo/rootfs/packages/$p" "$w/$p"
				chown -R "$UIDGID" "$w/$p"'
			ROOTFS=$BUILDROOT tools_achroot /bin/bash -c "cd /home/builder/work/$p &&
				su builder -c 'MAKEFLAGS=-j\$(nproc) makepkg -f --noconfirm'"
			userns bash -c 'set -e
				rm -rf "${dest:?}"; mkdir -p "$dest"
				cp "$BUILDROOT/home/builder/work/$p"/*.pkg.tar.* "$dest/"
				chown -R 0:0 "$dest"'
		fi
		userns bash -c 'set -e; rm -rf "$BUILDROOT/root/pkg"; mkdir -p "$BUILDROOT/root/pkg"; cp "$dest"/*.pkg.tar.* "$BUILDROOT/root/pkg/"'
		ROOTFS=$BUILDROOT tools_achroot /bin/bash -c "pacman -U --noconfirm --ask 4 --disable-sandbox /root/pkg/*.pkg.tar.*"
	done
	if [[ " ${need_build[*]} " == *" thunder-reboot "* ]]; then
		echo "== thunder-reboot (estatico)"
		userns cp "$repo/rootfs/src/thunder-reboot.c" "$BUILDROOT/root/thunder-reboot.c"
		ROOTFS=$BUILDROOT tools_achroot /bin/bash -c 'cd /root &&
			gcc -static -O2 -Wall -Wextra -o thunder-reboot thunder-reboot.c && strip thunder-reboot'
		export REBOOT_DIR
		userns bash -c 'set -e; rm -rf "${REBOOT_DIR:?}"; mkdir -p "$REBOOT_DIR"
			install -m755 "$BUILDROOT/root/thunder-reboot" "$REBOOT_DIR/thunder-reboot"'
	fi
fi
# O cache de pacotes do pacman nao para de crescer: fora o que nao foi usado em 60 dias.
find "$PACMAN_CACHE" -maxdepth 1 -type f -mtime +60 -delete 2>/dev/null || true

echo "== stage: overlays, pacotes, modulos, firmware, chave"
S=$ROOTFS/root/thunder-stage
export S
# Config lida pelo configure.sh (bash) e, em /etc/thunder/device, pelo initramfs e pelo
# usb-gadget (busybox sh): so atribuicoes com aspas simples.
q() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
stage_config=$(
	for v in DEVICE DEVICE_NAME DEVICE_VENDOR DEVICE_CODENAME KVER THUNDER_USER \
		THUNDER_PASSWORD THUNDER_ROOT_PASSWORD THUNDER_LOCALE THUNDER_TIMEZONE \
		THUNDER_HOSTNAME THUNDER_PROFILE WIFI_INTERFACE THUNDER_WIFI_SSID \
		THUNDER_WIFI_PASSWORD; do
		echo "$v=$(q "${!v}")"
	done
	qa() { local x; for x in "$@"; do printf '%s ' "$(q "$x")"; done; }
	echo "DEVICE_PACKAGES=($(qa "${DEVICE_PACKAGES[@]}"))"
	echo "DEVICE_SERVICES=($(qa "${DEVICE_SERVICES[@]}"))"
	echo "PROFILE_PACKAGES=($(qa "${PROFILE_PACKAGES[@]}"))"
	echo "PROFILE_SERVICES=($(qa "${PROFILE_SERVICES[@]}"))"
	echo "PROFILE_GROUPS=($(qa "${PROFILE_GROUPS[@]}"))"
	echo "EXTRA_PACKAGES=($(qa "${EXTRA_PACKAGES[@]}"))"
	echo "LOCAL_PACKAGES=($(qa "${LOCAL_PACKAGES[@]}"))"
)
LOCAL_DIRS=$(for p in "${LOCAL_PACKAGES[@]}"; do printf '%s=%s\n' "$p" "${PKG_DIR[$p]}"; done)
export stage_config THUNDER_PROFILE LOCAL_DIRS REBOOT_DIR repo KVER KERNEL_PKG FIRMWARE AUTHORIZED_KEYS DEVICE_DIR
userns bash -c 'set -e
	rm -rf "${S:?}"; mkdir -p "$S/packages/kernel" "$S/firmware"
	cp -a "$repo/rootfs/overlay" "$S/overlay"
	if [ -d "$DEVICE_DIR/overlay" ]; then cp -a "$DEVICE_DIR/overlay" "$S/device-overlay"; fi
	pd=$repo/rootfs/profiles/$THUNDER_PROFILE
	if [ -d "$pd/overlay" ]; then cp -a "$pd/overlay" "$S/profile-overlay"; fi
	if [ -f "$pd/configure.sh" ]; then cp "$pd/configure.sh" "$S/profile-configure.sh"; fi
	install -Dm755 "$REBOOT_DIR/thunder-reboot" "$S/overlay/usr/local/bin/thunder-reboot"
	# A receita do boot.img vai para dentro da imagem: o aparelho remonta o boot.img
	# sozinho quando o pacote do kernel atualiza (boot-deploy).
	install -Dm755 "$repo/initramfs/init" "$S/overlay/usr/lib/thunder/initramfs/init"
	install -Dm755 "$repo/initramfs/thunder-mkboot" "$S/overlay/usr/lib/thunder/thunder-mkboot"
	cp "$(readlink -f "$KERNEL_PKG")" "$S/packages/kernel/"
	while IFS="=" read -r p dir; do
		[ -n "$p" ] || continue
		mkdir -p "$S/packages/$p"; cp "$dir"/*.pkg.tar.* "$S/packages/$p/"
	done <<< "$LOCAL_DIRS"
	if [ -d "$FIRMWARE" ]; then cp -a "$FIRMWARE/." "$S/firmware/"; fi
	if [ -n "$AUTHORIZED_KEYS" ]; then cp "$AUTHORIZED_KEYS" "$S/authorized_keys"; fi
	printf "%s\n" "$stage_config" > "$S/config"
	cp "$repo/rootfs/configure.sh" "$repo/rootfs/finalize.sh" "$S/"
	chown -R 0:0 "$S"'

echo "== configuracao"
tools_achroot /bin/bash /root/thunder-stage/configure.sh
tools_achroot /bin/bash /root/thunder-stage/finalize.sh
exit
}
