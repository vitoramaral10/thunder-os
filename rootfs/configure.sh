#!/bin/bash
# Configura o rootfs do Arch para o aparelho. Roda DENTRO do chroot (tools/achroot.sh),
# com os overlays, os pacotes, os modulos, o firmware e config ja copiados para
# /root/thunder-stage por tools/build-rootfs.sh. Idempotente.
set -euo pipefail
S=/root/thunder-stage
pac() { pacman --noconfirm --needed --disable-sandbox "$@"; }
# DEVICE*, KVER, THUNDER_{USER,PASSWORD,LOCALE,TIMEZONE,HOSTNAME}, DEVICE_{PACKAGES,SERVICES}
. "$S/config"

# OpenRC no lugar do systemd como PID 1 (substitui o systemd-sysvcompat; o pacote
# systemd fica instalado e inerte porque o pacman depende dele).
pacman -U --noconfirm --ask 4 --disable-sandbox "$S"/packages/openrc-*.pkg.tar.*

# Pacotes: busybox estatico (udhcpd, telnetd, initramfs), sudo, inetutils (hostname,
# que o servico hostname do OpenRC chama).
pac -S busybox sudo inetutils
[ ${#DEVICE_PACKAGES[@]} -eq 0 ] || pac -S "${DEVICE_PACKAGES[@]}"

# pacman no aparelho: o 4.19 nao tem Landlock.
sed -i 's/^#DisableSandboxFilesystem/DisableSandboxFilesystem/' /etc/pacman.conf

# Overlay do nucleo (servicos, gadget USB, scripts de boot, fstab) e o do aparelho por
# cima.
cp -a "$S/overlay/." /
[ ! -d "$S/device-overlay" ] || cp -a "$S/device-overlay/." /

# Identidade do aparelho, lida pelo initramfs e pelo usb-gadget (busybox sh).
mkdir -p /etc/thunder
grep -E '^DEVICE(|_NAME|_VENDOR|_CODENAME)=' "$S/config" > /etc/thunder/device
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
printf 'root:%s\n%s:%s\n' "$THUNDER_PASSWORD" "$THUNDER_USER" "$THUNDER_PASSWORD" | chpasswd
echo '%wheel ALL=(ALL:ALL) ALL' > /etc/sudoers.d/wheel
chmod 440 /etc/sudoers.d/wheel
for h in /root "/home/$THUNDER_USER"; do
	install -d -m 700 "$h/.ssh"
	install -m 600 "$S/authorized_keys" "$h/.ssh/authorized_keys"
done
chown -R "$THUNDER_USER:$THUNDER_USER" "/home/$THUNDER_USER/.ssh"

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
for s in usb-network telnet-usb sshd local "${DEVICE_SERVICES[@]}"; do rc-update add "$s" default; done

rc-update show -v | grep -v '^ *[a-z-]* *|$' || true
