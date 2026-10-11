# Perfil "console": terminal e teclado na tela, sem ambiente de desktop.
# Plano (pesquisa de 2026-10-10): phoc + squeekboard + foot sob seatd, numa sessao
# dbus-run-session. cage nao serve: nao tem layer-shell nem input-method-v2.
PROFILE_ORDER=20
PROFILE_TITLE="Console na tela"
PROFILE_DESCRIPTION="Terminal em tela cheia com teclado virtual, para usar o aparelho sem o cabo."
PROFILE_STATUS=planned
PROFILE_BLOCKERS="Falta udev (eudev empacotado para o OpenRC), para o libinput enxergar o toque, e o seatd sob o OpenRC. Falta confirmar no aparelho que a libsystemd 262 do Arch carrega no kernel 4.19."
PROFILE_PACKAGES=(phoc squeekboard foot seatd)
PROFILE_SERVICES=()
PROFILE_LOCAL_PACKAGES=()
