# Perfil "none": sem interface na tela. Acesso por SSH e telnet no cabo USB.
PROFILE_ORDER=10
PROFILE_TITLE="Sem interface (SSH)"
PROFILE_DESCRIPTION="Sistema base, sem nada na tela. Acesso pelo cabo USB com SSH (172.16.42.1)."
# tested: validado num aparelho real. experimental: monta, mas nao foi validado.
# planned: ainda falta base; o build recusa (PROFILE_BLOCKERS diz o que falta).
PROFILE_STATUS=tested
PROFILE_PACKAGES=()
PROFILE_SERVICES=()
# Pacotes nossos (rootfs/packages/<nome>/PKGBUILD) compilados no buildroot e instalados.
PROFILE_LOCAL_PACKAGES=()
