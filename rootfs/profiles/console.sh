# Perfil "console": terminal e teclado na tela, sem ambiente de desktop.
# phoc + squeekboard + foot numa sessao do logind no tty1 (thunder-console@<usuario>,
# em console/overlay; o systemd --user da o D-Bus de sessao). cage nao serve: nao tem
# layer-shell nem input-method-v2. O logind entrega DRM e entrada pelo libseat; o seatd
# nao roda. Plano: build/research/gui/PLAN.md.
# experimental: nada disto foi validado no aparelho (passos 4-13 do plano).
PROFILE_ORDER=20
PROFILE_TITLE="Console na tela"
PROFILE_DESCRIPTION="Terminal em tela cheia com teclado virtual (phoc, squeekboard, foot), para usar o aparelho sem o cabo."
PROFILE_STATUS=experimental
PROFILE_BLOCKERS=""
PROFILE_PACKAGES=(phoc squeekboard foot mesa libbsd)
# Units do systemd a habilitar (systemctl enable); {user} vira o usuario da imagem.
PROFILE_SERVICES=('thunder-console@{user}.service')
# Sem grupos extras: o logind da card0 e a entrada a sessao ativa (TakeDevice, uaccess).
PROFILE_GROUPS=()
# bubblewrap sem assume_kernel: o glycin (icones do GTK) precisa dele no 4.19.
PROFILE_LOCAL_PACKAGES=(bubblewrap-thunder)
PROFILE_BUILD_DEPENDS=(bash-completion docbook-xsl git libxslt meson)
