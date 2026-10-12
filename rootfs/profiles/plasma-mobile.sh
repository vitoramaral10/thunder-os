# Perfil "plasma-mobile": o Plasma Mobile do KDE.
# Plano: base do console (logind, systemd --user, udev do Arch) + startplasma com o boot
# por systemd --user (o padrao do Plasma) + greetd.
PROFILE_ORDER=40
PROFILE_TITLE="Plasma Mobile"
PROFILE_DESCRIPTION="Interface de celular do KDE, com o teclado do Plasma e apps Kirigami. A mais pesada das opcoes."
PROFILE_STATUS=planned
PROFILE_BLOCKERS="Alem da base do console: validar o startplasma sob o systemd --user no kernel 4.19, GPU (panfrost) ligada na imagem, e medir se cabe em 4 GB de RAM."
PROFILE_PACKAGES=(plasma-mobile plasma-keyboard greetd qmlkonsole)
PROFILE_SERVICES=()
PROFILE_LOCAL_PACKAGES=()
