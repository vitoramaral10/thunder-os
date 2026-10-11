# Perfil "console": terminal e teclado na tela, sem ambiente de desktop.
# phoc + squeekboard + foot sob seatd, numa sessao dbus-run-session (servico thunder-console).
# cage nao serve: nao tem layer-shell nem input-method-v2. O udev e o systemd-udevd do Arch
# como servico do OpenRC; o kernel ganha tres backports (statx mnt_id, waitid P_PIDFD,
# close_range) para ele rodar no 4.19. Plano: build/research/gui/PLAN.md.
# experimental: nada disto foi validado no aparelho (passos 4-13 do plano).
PROFILE_ORDER=20
PROFILE_TITLE="Console na tela"
PROFILE_DESCRIPTION="Terminal em tela cheia com teclado virtual (phoc, squeekboard, foot), para usar o aparelho sem o cabo."
PROFILE_STATUS=experimental
PROFILE_BLOCKERS=""
PROFILE_PACKAGES=(phoc squeekboard foot seatd mesa libbsd)
# udev e udev-trigger vao no sysinit (forma servico:runlevel); o resto, no default.
PROFILE_SERVICES=(udev:sysinit udev-trigger:sysinit seatd thunder-console)
# O usuario precisa de seat (seatd.sock), video/render (DRM) e input (libinput).
PROFILE_GROUPS=(seat video input render)
# bubblewrap sem assume_kernel: o glycin (icones do GTK) precisa dele no 4.19.
PROFILE_LOCAL_PACKAGES=(bubblewrap-thunder)
PROFILE_BUILD_DEPENDS=(bash-completion docbook-xsl git libxslt meson)
