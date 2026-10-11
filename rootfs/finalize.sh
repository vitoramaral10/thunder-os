#!/bin/bash
# Limpeza antes de gerar a imagem. Roda DENTRO do chroot (tools/achroot.sh).
set -euo pipefail

rm -rf /root/thunder-stage
# Com o cache do host montado (tools/achroot.sh), os pacotes ficaram nele e o diretorio
# da imagem esta vazio por baixo; limpar aqui apagaria o cache compartilhado.
mountpoint -q /var/cache/pacman/pkg || pacman -Scc --noconfirm >/dev/null
# As chaves de host nascem no primeiro boot (servico sshd).
rm -f /etc/ssh/ssh_host_*
# O resolv.conf do PC (127.0.0.53) nao serve no aparelho.
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/resolv.conf
rm -f /var/log/pacman.log
du -sh / --exclude=/proc --exclude=/sys --exclude=/dev 2>/dev/null | tail -1
