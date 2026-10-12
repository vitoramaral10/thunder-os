#!/bin/bash
# Limpeza antes de gerar a imagem. Roda DENTRO do chroot (tools/achroot.sh).
set -euo pipefail

rm -rf /root/thunder-stage
# Com o cache do host montado (tools/achroot.sh), os pacotes ficaram nele e o diretorio
# da imagem esta vazio por baixo; limpar aqui apagaria o cache compartilhado.
mountpoint -q /var/cache/pacman/pkg || pacman -Scc --noconfirm >/dev/null
# As chaves de host nascem no primeiro boot (sshdgenkeys.service, que o sshd.service puxa).
rm -f /etc/ssh/ssh_host_*
# O resolv.conf do PC (127.0.0.53) nao serve no aparelho. Valor inicial: com Wi-Fi, o
# NetworkManager reescreve o arquivo quando conecta.
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/resolv.conf
rm -f /var/log/pacman.log
# Os hooks do pacman ja rodaram o ldconfig e o hwdb no chroot: o 1o boot nao refaz
# (ConditionNeedsUpdate= do ldconfig.service e do systemd-hwdb-update.service).
touch /etc/.updated /var/.updated
# Tamanho do que vai para a imagem: o cache do pacman e montado do host (pacman-cache).
du -sh / --exclude=/proc --exclude=/sys --exclude=/dev --exclude=/var/cache/pacman/pkg 2>/dev/null | tail -1
