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
# DEVICE_{PACKAGES,SERVICES}, PROFILE_{PACKAGES,SERVICES}, EXTRA_PACKAGES, LOCAL_PACKAGES
. "$S/config"

# Pacotes nossos, na ordem. O primeiro e o OpenRC no lugar do systemd como PID 1
# (substitui o systemd-sysvcompat; o pacote systemd fica instalado e inerte porque o
# pacman depende dele).
for p in "${LOCAL_PACKAGES[@]}"; do
	pacman -U --noconfirm --ask 4 --disable-sandbox "$S/packages/$p"/*.pkg.tar.*
done

# Pacotes: busybox estatico (udhcpd, telnetd, initramfs), sudo, inetutils (hostname,
# que o servico hostname do OpenRC chama).
pac -S busybox sudo inetutils
# Do aparelho, do perfil de interface e os extras escolhidos no instalador; com Wi-Fi,
# wpa_supplicant e dhcpcd (sem udev, o NetworkManager nao gerencia a interface).
extra=("${DEVICE_PACKAGES[@]}" "${PROFILE_PACKAGES[@]}" "${EXTRA_PACKAGES[@]}")
[ -z "$WIFI_INTERFACE" ] || extra+=(wpa_supplicant dhcpcd)
[ ${#extra[@]} -eq 0 ] || pac -S "${extra[@]}"

# pacman no aparelho: o 4.19 nao tem Landlock.
sed -i 's/^#DisableSandboxFilesystem/DisableSandboxFilesystem/' /etc/pacman.conf

# Overlays: o do nucleo (servicos, gadget USB, scripts de boot, fstab), o do perfil e o
# do aparelho por cima.
cp -a "$S/overlay/." /
[ ! -d "$S/profile-overlay" ] || cp -a "$S/profile-overlay/." /
[ ! -d "$S/device-overlay" ] || cp -a "$S/device-overlay/." /

# Identidade do aparelho, lida pelo initramfs e pelo usb-gadget (busybox sh).
mkdir -p /etc/thunder
grep -E '^DEVICE(|_NAME|_VENDOR|_CODENAME)=' "$S/config" > /etc/thunder/device
grep -E '^THUNDER_PROFILE=' "$S/config" >> /etc/thunder/device
echo "$THUNDER_HOSTNAME" > /etc/hostname
echo "hostname=\"$THUNDER_HOSTNAME\"" > /etc/conf.d/hostname

# Modulos do kernel e firmware do aparelho.
rm -rf "/usr/lib/modules/$KVER"
mkdir -p /usr/lib/modules /usr/lib/firmware
cp -a "$S/modules/$KVER" /usr/lib/modules/
depmod "$KVER"
cp -a "$S/firmware/." /usr/lib/firmware/

# Usuarios: root e $THUNDER_USER com a senha provisoria (trocar com passwd), chave SSH.
if id alarm >/dev/null 2>&1; then userdel -r alarm; fi
id "$THUNDER_USER" >/dev/null 2>&1 || useradd -m -G wheel -s /bin/bash "$THUNDER_USER"
printf 'root:%s\n%s:%s\n' "$THUNDER_ROOT_PASSWORD" "$THUNDER_USER" "$THUNDER_PASSWORD" | chpasswd
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
[ -s /etc/machine-id ] || dbus-uuidgen > /etc/machine-id

# OpenRC: log do boot em /var/log/rc.log; runlevels.
sed -i 's/^#\?rc_logger=.*/rc_logger="YES"/' /etc/rc.conf
# O servico dmesg baixa o console_loglevel para 1, e isso vale para o console do pstore:
# um travamento depois do sysinit nao deixaria log. Fora do sysinit.
rc-update del dmesg sysinit 2>/dev/null || true
for s in binfmt network staticroute; do rc-update del "$s" boot 2>/dev/null || true; done
rc-update del netmount default 2>/dev/null || true
for s in usb-network telnet-usb sshd local "${DEVICE_SERVICES[@]}" "${PROFILE_SERVICES[@]}"; do
	rc-update add "$s" default
done

# Wi-Fi: servicos na interface do aparelho e, se o instalador mandou, a rede ja salva.
# Na config so vai a PSK (derivada pelo wpa_passphrase); a senha nao fica na imagem. O
# nome da rede vai em hexadecimal, como o thunder-wifi faz.
if [ -n "$WIFI_INTERFACE" ]; then
	echo "wpa_iface=\"$WIFI_INTERFACE\"" > /etc/conf.d/wpa-supplicant
	wconf=/etc/wpa_supplicant/wpa_supplicant-$WIFI_INTERFACE.conf
	mkdir -p /etc/wpa_supplicant
	printf 'ctrl_interface=DIR=/run/wpa_supplicant GROUP=wheel\nupdate_config=1\n' > "$wconf"
	if [ -n "$THUNDER_WIFI_SSID" ]; then
		ssid_hex=$(printf '%s' "$THUNDER_WIFI_SSID" | od -An -tx1 | tr -d ' \n')
		if [ -n "$THUNDER_WIFI_PASSWORD" ]; then
			psk=$(printf '%s\n' "$THUNDER_WIFI_PASSWORD" | wpa_passphrase "$THUNDER_WIFI_SSID" |
				sed -n 's/^[[:space:]]*psk=//p')
			[ -n "$psk" ] || { echo "wpa_passphrase recusou a senha do Wi-Fi" >&2; exit 1; }
			printf 'network={\n\tssid=%s\n\tpsk=%s\n}\n' "$ssid_hex" "$psk" >> "$wconf"
		else
			printf 'network={\n\tssid=%s\n\tkey_mgmt=NONE\n}\n' "$ssid_hex" >> "$wconf"
		fi
	fi
	chmod 600 "$wconf"
	rc-update add wpa-supplicant default
	rc-update add dhcpcd-wifi default
fi

# Ajustes do perfil de interface que dependem do usuario ou dos pacotes ja instalados.
if [ -f "$S/profile-configure.sh" ]; then
	THUNDER_USER=$THUNDER_USER bash "$S/profile-configure.sh"
fi

rc-update show -v | grep -v '^ *[a-z-]* *|$' || true
