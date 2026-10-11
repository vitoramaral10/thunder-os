# Perfil "plasma-mobile": o Plasma Mobile do KDE.
# Plano (pesquisa de 2026-10-10): base do console + elogind real (o KWin so fala
# login1) + startkderc com systemdBoot=false (boot classico) + greetd.
PROFILE_ORDER=40
PROFILE_TITLE="Plasma Mobile"
PROFILE_DESCRIPTION="Interface de celular do KDE, com o teclado do Plasma e apps Kirigami. A mais pesada das opcoes."
PROFILE_STATUS=planned
PROFILE_BLOCKERS="Alem da base do console: elogind empacotado para o OpenRC, GPU (panfrost) ligada na imagem, e medir se cabe em 4 GB de RAM."
PROFILE_PACKAGES=(plasma-mobile plasma-keyboard greetd qmlkonsole)
PROFILE_SERVICES=()
PROFILE_LOCAL_PACKAGES=()
