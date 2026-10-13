#!/bin/bash
# Ajustes do perfil phosh que dependem dos pacotes ja instalados. Roda no chroot, no fim
# do rootfs/configure.sh (THUNDER_USER no ambiente).
set -euo pipefail
# O tty1 e da sessao Phosh (thunder-phosh@<usuario>.service, habilitada pelo
# PROFILE_SERVICES): o getty@tty1 do Arch sai, para os dois nao disputarem o VT no boot.
systemctl disable getty@tty1.service
