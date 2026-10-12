# Perfil "phosh": a interface de celular do GNOME (Phosh do Arch).
# Sessao: thunder-phosh@<usuario>.service (phosh/overlay), o phosh.service do upstream
# como template: PAM login no tty1, phosh-session sob capsh --noamb, gnome-session 51 no
# systemd --user. Sem greeter: o Phosh sobe bloqueado e a senha do usuario desbloqueia.
# Escala: a que o phoc calcula do tamanho fisico do painel. Desenho e testes:
# build/research/phosh/DESIGN.md.
# experimental: validado no xiaomi-thunder em 2026-10-11 com o pacman no sistema rodando
# (sobe no boot, desbloqueia, toque, teclado com acentos, Wi-Fi, apagar/acender a tela);
# falta instalar do zero pelo app para virar tested.
PROFILE_ORDER=30
PROFILE_TITLE="Phosh"
PROFILE_DESCRIPTION="Interface de celular do GNOME: tela inicial, gestos, notificacoes, teclado na tela e apps GNOME."
PROFILE_STATUS=experimental
PROFILE_BLOCKERS=""
# phosh puxa gnome-session, gnome-settings-daemon, polkit, upower, iio-sensor-proxy,
# feedbackd e os portais; o phoc vem do phoc-thunder (PROFILE_LOCAL_PACKAGES). squeekboard
# explicito: e o phosh-osk-provider (o stevia conflita com ele). libbsd: dep que o
# squeekboard do Arch ARM nao declara. mesa: driver panfrost. gnome-backgrounds: o override
# do Phosh (00_mobi.Phosh) aponta o fundo para ele.
PROFILE_PACKAGES=(phosh squeekboard gnome-console gnome-control-center
	phosh-mobile-settings gnome-keyring gnome-backgrounds mesa libbsd)
# Units do systemd a habilitar (systemctl enable); {user} vira o usuario da imagem.
PROFILE_SERVICES=('thunder-phosh@{user}.service')
# feedbackd: grupo que as regras do feedbackd dao aos LEDs (fbd-ledctrl -G), como no pmOS.
PROFILE_GROUPS=(feedbackd)
# bubblewrap sem assume_kernel: glycin e xdg-desktop-portal precisam dele no 4.19.
# phoc-thunder: o phoc com o wlroots embutido e o patch do upstream; o do Arch, contra o
# wlroots do sistema, derruba a superficie "phosh home" (rootfs/packages/phoc-thunder).
PROFILE_LOCAL_PACKAGES=(bubblewrap-thunder phoc-thunder)
# makepkg roda sem -s: depends e makedepends dos pacotes locais entram aqui.
PROFILE_BUILD_DEPENDS=(bash-completion docbook-xsl git libxslt meson
	cairo dconf glib2-devel gnome-desktop gsettings-desktop-schemas hicolor-icon-theme
	libgmobile libinput libxcb libxkbcommon mutter pango pixman wayland wayland-protocols
	xorg-xwayland lcms2 libdisplay-info libdrm libglvnd libliftoff seatd vulkan-icd-loader
	vulkan-headers xcb-util-errors xcb-util-renderutil xcb-util-wm glslang ninja
	python-docutils systemd)
