# Perfil "phosh": a interface de celular do GNOME.
# Plano (pesquisa de 2026-10-10): base do console + elogind real + gnome-session 48
# compilado contra o elogind (o 51 do Arch exige systemd --user; o pmOS/Alpine esta
# preso no 48 pelo mesmo motivo) + greetd.
PROFILE_ORDER=30
PROFILE_TITLE="Phosh"
PROFILE_DESCRIPTION="Interface de celular do GNOME: tela inicial, gestos, notificacoes e apps GNOME."
PROFILE_STATUS=planned
PROFILE_BLOCKERS="Alem da base do console: elogind empacotado para o OpenRC, gnome-session 48 recompilado (o 51 do Arch exige systemd), GPU (panfrost) ligada na imagem."
PROFILE_PACKAGES=(phosh phoc squeekboard greetd gnome-console)
PROFILE_SERVICES=()
PROFILE_LOCAL_PACKAGES=()
