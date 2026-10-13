#!/bin/bash
# Configura o rootfs do Arch para o aparelho. Roda DENTRO do chroot (tools/achroot.sh),
# com os overlays, os pacotes, os modulos, o firmware e config ja copiados para
# /root/thunder-stage por tools/build-rootfs.sh. Idempotente.
set -euo pipefail
S=/root/thunder-stage
# Um timeout de espelho nao pode derrubar a instalacao: ate 3 tentativas.
pac() {
	local i
	for i in 1 2 3; do
		pacman --noconfirm --needed --disable-sandbox "$@" && return 0
		[ "$i" = 3 ] || { echo "pacman falhou; tentando de novo em 10 s ($i/3)" >&2; sleep 10; }
	done
	return 1
}
# DEVICE*, KVER, THUNDER_{USER,PASSWORD,ROOT_PASSWORD,LOCALE,TIMEZONE,HOSTNAME,PROFILE},
# DEVICE_{PACKAGES,SERVICES}, PROFILE_{PACKAGES,SERVICES,GROUPS}, EXTRA_PACKAGES, LOCAL_PACKAGES,
# WMT_*
. "$S/config"

# Pacotes nossos (os do perfil, ex.: bubblewrap-thunder), na ordem. --ask 4: aceita
# trocar o pacote do Arch que eles substituem.
for p in ${LOCAL_PACKAGES[@]+"${LOCAL_PACKAGES[@]}"}; do
	pacman -U --noconfirm --ask 4 --disable-sandbox "$S/packages/$p"/*.pkg.tar.*
done

# Pacotes: systemd-sysvcompat (/usr/bin/init -> systemd: o PID 1 e o systemd do Arch),
# busybox estatico (udhcpd e telnetd do cabo USB, initramfs), sudo, inetutils (hostname;
# o telnet.socket dele fica desligado: o telnet do cabo e o thunder-telnet-usb).
# android-tools e python: o mkbootimg do boot-deploy (kernel atualizado pelo pacman).
pac -S systemd-sysvcompat busybox sudo inetutils android-tools python
# Do aparelho, do perfil de interface e os extras escolhidos no instalador; com Wi-Fi,
# NetworkManager (backend wpa_supplicant; o iwd precisa de crypto do kernel que falta).
extra=("${DEVICE_PACKAGES[@]}" "${PROFILE_PACKAGES[@]}" "${EXTRA_PACKAGES[@]}")
[ -z "$WIFI_INTERFACE" ] || extra+=(networkmanager wpa_supplicant)
[ ${#extra[@]} -eq 0 ] || pac -S "${extra[@]}"

# pacman no aparelho: o 4.19 nao tem Landlock.
sed -i 's/^#DisableSandboxFilesystem/DisableSandboxFilesystem/' /etc/pacman.conf

# Overlays: o do nucleo (units, drop-ins, gadget USB, fstab), o do perfil e o do
# aparelho por cima.
cp -a "$S/overlay/." /
[ ! -d "$S/profile-overlay" ] || cp -a "$S/profile-overlay/." /
[ ! -d "$S/device-overlay" ] || cp -a "$S/device-overlay/." /

# Identidade do aparelho, lida pelo initramfs, pelo usb-gadget (busybox sh) e pelo
# thunder-wifi (WIFI_INTERFACE).
mkdir -p /etc/thunder
grep -E '^(DEVICE(|_NAME|_VENDOR|_CODENAME)|WIFI_INTERFACE)=' "$S/config" > /etc/thunder/device
grep -E '^THUNDER_PROFILE=' "$S/config" >> /etc/thunder/device

# Swap: zram na frente (prioridade 100) e o /swapfile atras (10), criado no 1o boot pelo
# thunder-swapfile.service, depois do growfs. O config pode vir de um build-rootfs.sh
# antigo, sem as variaveis.
zramconf=/usr/lib/systemd/zram-generator.conf
if [ -n "${ZRAM_SIZE-}" ]; then
	pac -S zram-generator
	{
		echo "# Gerado pelo rootfs/configure.sh (ZRAM_* do device.sh)."
		echo "[zram0]"
		echo "zram-size = $ZRAM_SIZE"
		[ -z "${ZRAM_ALGORITHM-}" ] || echo "compression-algorithm = $ZRAM_ALGORITHM"
		echo "swap-priority = 100"
	} > "$zramconf"
else
	rm -f "$zramconf"
fi
sed -i '\|^/swapfile[[:space:]]|d' /etc/fstab
if [ -n "${SWAPFILE_SIZE-}" ]; then
	echo "SWAPFILE_SIZE='$SWAPFILE_SIZE'" >> /etc/thunder/device
	printf '/swapfile\tnone\tswap\tdefaults,pri=10,nofail\t0\t0\n' >> /etc/fstab
fi

# Conectividade MediaTek WMT (pacote thunder-mediatek-wmt): o que o device.sh diz do chip
# vira /etc/thunder/wmt.conf, lido pelo thunder-wmt. Aparelho sem WMT fica sem o arquivo
# e as units do pacote ficam inertes (ConditionPathExists=). O config pode vir de um
# build-rootfs.sh antigo, sem as WMT_*.
wmtconf=/etc/thunder/wmt.conf
wmt_core=(${WMT_CORE_MODULES[@]+"${WMT_CORE_MODULES[@]}"})
if [ ${#wmt_core[@]} -gt 0 ]; then
	{
		echo "# Gerado pelo rootfs/configure.sh (WMT_* do device.sh). Lido pelo thunder-wmt."
		echo "WMT_CORE_MODULES=${wmt_core[*]}"
		echo "WMT_WIFI_MODULES=${WMT_WIFI_MODULES[*]-}"
		echo "WMT_ROM_PATCHES=${WMT_ROM_PATCHES[*]-}"
		echo "WMT_HIF=${WMT_HIF-}"
		echo "WMT_WIFI_INTERFACE=$WIFI_INTERFACE"
		echo "WMT_WIFI_NVRAM=${WMT_WIFI_NVRAM-}"
		echo "WMT_BT_MODULES=${WMT_BT_MODULES[*]-}"
		echo "WMT_BT_ADDR=${WMT_BT_ADDR-}"
	} > "$wmtconf"
else
	rm -f "$wmtconf"
fi
echo "$THUNDER_HOSTNAME" > /etc/hostname

# Firmware do aparelho, e o kernel como pacote (linux-<aparelho>): modulos, kernel e
# layout do boot.img. Com THUNDER_IMAGE_BUILD=1 o hook do pacote so roda o depmod; o
# boot.img da imagem e montado no PC (tools/build-boot.sh).
mkdir -p /usr/lib/firmware
cp -a "$S/firmware/." /usr/lib/firmware/
THUNDER_IMAGE_BUILD=1 pacman -U --noconfirm --disable-sandbox "$S"/packages/kernel/*.pkg.tar.*

# Usuarios: root e $THUNDER_USER com a senha provisoria (trocar com passwd), chave SSH.
if id alarm >/dev/null 2>&1; then userdel -r alarm; fi
id "$THUNDER_USER" >/dev/null 2>&1 || useradd -m -G wheel -s /bin/bash "$THUNDER_USER"
printf 'root:%s\n%s:%s\n' "$THUNDER_ROOT_PASSWORD" "$THUNDER_USER" "$THUNDER_PASSWORD" | chpasswd
# Grupos extras do perfil (video, input...): os que faltam sao criados como
# grupo de sistema; usermod -aG e idempotente.
for g in ${PROFILE_GROUPS[@]+"${PROFILE_GROUPS[@]}"}; do
	getent group "$g" >/dev/null || groupadd -r "$g"
	usermod -aG "$g" "$THUNDER_USER"
done
echo '%wheel ALL=(ALL:ALL) ALL' > /etc/sudoers.d/wheel
chmod 440 /etc/sudoers.d/wheel
# Chave SSH opcional: sem ela, o login e por senha.
if [ -s "$S/authorized_keys" ]; then
	for h in /root "/home/$THUNDER_USER"; do
		install -d -m 700 "$h/.ssh"
		install -m 600 "$S/authorized_keys" "$h/.ssh/authorized_keys"
	done
	chown -R "$THUNDER_USER:$THUNDER_USER" "/home/$THUNDER_USER/.ssh"
fi

# Locale, fuso, machine-id.
for l in en_US.UTF-8 "$THUNDER_LOCALE"; do
	grep -q "^#\?$l " /etc/locale.gen || { echo "locale desconhecido: $l" >&2; exit 1; }
	sed -i "s/^#\($l \)/\1/" /etc/locale.gen
done
locale-gen
echo "LANG=$THUNDER_LOCALE" > /etc/locale.conf
[ -f "/usr/share/zoneinfo/$THUNDER_TIMEZONE" ] || { echo "fuso desconhecido: $THUNDER_TIMEZONE" >&2; exit 1; }
ln -sf "/usr/share/zoneinfo/$THUNDER_TIMEZONE" /etc/localtime
# machine-id gerado aqui de proposito: vazio, o PID 1 trataria o 1o boot como "first
# boot" e aplicaria o 90-systemd.preset inteiro (homed, sysext, networkd...).
[ -s /etc/machine-id ] || dbus-uuidgen > /etc/machine-id

# Kernel de vendor antigo: o systemd 262 precisa de recursos que ele nao tem
# (/usr/lib/thunder/systemd-compat). Credenciais e o MemoryPressureWatch= de unit com
# PrivateNetwork= pedem a mount API nova (5.2); ProtectProc=/ProcSubset= pedem procfs por
# montagem (5.8). Os drop-ins entram em todo servico so quando o KVER do aparelho e
# anterior; kernel mainline nao recebe nada. O systemd --user falha igual nos dois
# primeiros (243/CREDENTIALS, 226/NAMESPACE) e recebe os mesmos arquivos; ProtectProc= e
# ProcSubset= ele ignora sem erro, entao esse fica so no sistema.
kver_before() { [ "$(printf '%s\n' "$1" "${KVER%%-*}" | sort -V | head -n 1)" != "$1" ]; }
compat=/usr/lib/thunder/systemd-compat
# versao-minima:gerente:arquivo
for c in 5.2:system:zz-thunder-no-credentials.conf 5.2:system:zz-thunder-no-memory-pressure.conf \
	5.8:system:zz-thunder-no-protectproc.conf \
	5.2:user:zz-thunder-no-credentials.conf 5.2:user:zz-thunder-no-memory-pressure.conf; do
	IFS=: read -r min mgr f <<<"$c"
	d=/usr/lib/systemd/$mgr/service.d
	if kver_before "$min"; then
		install -D -m 644 "$compat/$f" "$d/$f"
	else
		rm -f "${d:?}/${f:?}"
	fi
done

# Rede: o tarball do Arch Linux ARM liga o systemd-networkd (DHCP em en*/eth*) e o
# systemd-resolved; aqui saem. O cabo USB e do thunder-usb-network e o Wi-Fi e do
# NetworkManager, que escreve o /etc/resolv.conf. Como num Arch de PC com NM.
for u in systemd-networkd.service systemd-networkd.socket systemd-networkd-varlink.socket \
	systemd-networkd-varlink-metrics.socket systemd-networkd-resolve-hook.socket \
	systemd-networkd-wait-online.service systemd-network-generator.service \
	systemd-resolved.service systemd-resolved-monitor.socket systemd-resolved-varlink.socket; do
	[ ! -e "/usr/lib/systemd/system/$u" ] || systemctl disable --quiet "$u"
done
rm -f /etc/systemd/network/en.network /etc/systemd/network/eth.network
for u in systemd-networkd.service systemd-resolved.service; do
	if systemctl is-enabled --quiet "$u" 2>/dev/null; then
		echo "$u continua habilitado" >&2
		exit 1
	fi
done

# Units do nucleo, do aparelho (DEVICE_SERVICES) e do perfil (PROFILE_SERVICES): nomes
# de unit com sufixo; "{user}" vira o THUNDER_USER (ex.: thunder-console@{user}.service).
# PROFILE_SERVICES pode vir sem definicao se o config for de um build-rootfs.sh antigo.
units=(thunder-usb-network.service thunder-telnet-usb.service thunder-sshd-rescue.service
	sshd.service systemd-timesyncd.service thunder-rtc-sync.path systemd-pstore.service)
for s in ${DEVICE_SERVICES[@]+"${DEVICE_SERVICES[@]}"} ${PROFILE_SERVICES[@]+"${PROFILE_SERVICES[@]}"}; do
	case $s in
	*:*) echo "\"$s\": a forma servico:runlevel era do OpenRC; use o nome da unit" >&2; exit 1 ;;
	*.service | *.socket | *.timer | *.path | *.target) units+=("${s//\{user\}/$THUNDER_USER}") ;;
	*) echo "nome de unit sem sufixo: \"$s\" (ex.: $s.service)" >&2; exit 1 ;;
	esac
done
systemctl enable "${units[@]}"

# Wi-Fi: o NetworkManager so gerencia a interface de estacao do aparelho (o resto, como
# o cabo USB e as interfaces extras do driver, fica de fora) e, se o instalador mandou,
# a rede ja vem salva pelo thunder-wifi save: o perfil (modo 600) guarda a senha, que o
# WPA3 precisa, e leva PMF opcional; a senha nao passa pela linha de comando.
nmwifi=/usr/lib/NetworkManager/conf.d/51-thunder-wifi.conf
if [ -n "$WIFI_INTERFACE" ]; then
	printf '%s\n' "# Gerado pelo rootfs/configure.sh (WIFI_INTERFACE do device.sh)." \
		'[keyfile]' "unmanaged-devices=except:interface-name:$WIFI_INTERFACE" > "$nmwifi"
	systemctl enable NetworkManager.service
	if [ -n "$THUNDER_WIFI_SSID" ]; then
		rm -f /etc/NetworkManager/system-connections/thunder-*.nmconnection
		printf '%s\n' "$THUNDER_WIFI_PASSWORD" |
			THUNDER_WIFI_IFACE=$WIFI_INTERFACE /usr/local/bin/thunder-wifi save "$THUNDER_WIFI_SSID"
	fi
else
	rm -f "$nmwifi"
fi

# Audio por Bluetooth: e o WirePlumber que registra os codecs A2DP no BlueZ, mas o
# PipeWire do Arch so sobe por socket, quando algum programa toca. No perfil console
# nada toca no login, e um fone que conecta sozinho ficaria sem audio. Com bluez e
# WirePlumber na imagem, a sessao do usuario sobe o WirePlumber (que puxa o PipeWire).
# O perfil none nao tem servidor de audio, e o add-wants falha sem a unit.
if pacman -Q bluez >/dev/null 2>&1 && [ -e /usr/lib/systemd/user/wireplumber.service ]; then
	systemctl --global add-wants default.target wireplumber.service
fi

# Schemas do GLib: overrides de overlay (ex.: 90-thunder-console.gschema.override) so valem
# depois de compilar.
if [ -d /usr/share/glib-2.0/schemas ]; then
	glib-compile-schemas /usr/share/glib-2.0/schemas
fi

# Ajustes do perfil de interface que dependem do usuario ou dos pacotes ja instalados.
if [ -f "$S/profile-configure.sh" ]; then
	THUNDER_USER=$THUNDER_USER bash "$S/profile-configure.sh"
fi

systemctl list-unit-files --state=enabled --no-legend --no-pager || true
