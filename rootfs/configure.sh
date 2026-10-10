#!/bin/bash
# Configura o rootfs do Arch para o thunder. Roda DENTRO do chroot (tools/achroot.sh),
# com o overlay, os pacotes, os modulos, o firmware e config ja copiados para
# /root/thunder-stage por tools/build-rootfs.sh. Idempotente.
set -euo pipefail
S=/root/thunder-stage
KVER=4.19.191-perf
pac() { pacman --noconfirm --needed --disable-sandbox "$@"; }
. "$S/config"   # THUNDER_USER, THUNDER_PASSWORD

# OpenRC no lugar do systemd como PID 1 (substitui o systemd-sysvcompat; o pacote
# systemd fica instalado e inerte porque o pacman depende dele).
pacman -U --noconfirm --ask 4 --disable-sandbox "$S"/packages/openrc-*.pkg.tar.*

# Pacotes: busybox estatico (udhcpd, telnetd, initramfs), sudo, inetutils (hostname,
# que o servico hostname do OpenRC chama).
pac -S busybox sudo inetutils

# pacman no aparelho: o 4.19 nao tem Landlock.
sed -i 's/^#DisableSandboxFilesystem/DisableSandboxFilesystem/' /etc/pacman.conf

# Overlay (servicos, gadget USB, scripts de boot, fstab, hostname).
cp -a "$S/overlay/." /

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
sed -i 's/^#\(pt_BR.UTF-8 UTF-8\)/\1/; s/^#\(en_US.UTF-8 UTF-8\)/\1/' /etc/locale.gen
locale-gen
echo 'LANG=pt_BR.UTF-8' > /etc/locale.conf
ln -sf /usr/share/zoneinfo/America/Sao_Paulo /etc/localtime
[ -s /etc/machine-id ] || dbus-uuidgen > /etc/machine-id

# OpenRC: log do boot em /var/log/rc.log; runlevels.
sed -i 's/^#\?rc_logger=.*/rc_logger="YES"/' /etc/rc.conf
# O servico dmesg baixa o console_loglevel para 1, e isso vale para o console do pstore:
# um travamento depois do sysinit nao deixaria log. Fora do sysinit.
rc-update del dmesg sysinit 2>/dev/null || true
for s in binfmt network staticroute; do rc-update del "$s" boot 2>/dev/null || true; done
rc-update del netmount default 2>/dev/null || true
for s in usb-network telnet-usb sshd touchscreen-wake local; do rc-update add "$s" default; done

rc-update show -v | grep -v '^ *[a-z-]* *|$' || true
