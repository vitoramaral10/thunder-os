# Xiaomi Redmi 10 5G / POCO M4 5G (thunder), MediaTek MT6833.
# Lido pelos scripts de tools/ (bash). Caminhos relativos sao relativos a esta pasta.
# Referencia de cada variavel: docs/porting.md.

DEVICE_NAME="Redmi 10 5G / POCO M4 5G"
DEVICE_VENDOR=Xiaomi
DEVICE_CODENAME=thunder
DEVICE_SOC="MediaTek MT6833 (Dimensity 700)"
DEVICE_MAINTAINER="vitoramaral10"
# bring-up | boots | daily
DEVICE_STATUS=boots

# Kernel: 4.19 da Xiaomi no commit fixado, com o enxerto de conectividade e os patches.
KERNEL_URL=https://github.com/MiCode/Xiaomi_Kernel_OpenSource/archive/a40ceab8943703f33e935cb0b86d0f69aa2044dd.tar.gz
KERNEL_SHA512=d0aa0cd26632a4ce272eea248c3db0003ba61c525814bc1d7aa83f14860f55a7b7e6cb15153154be6319936bcb4171aaed3de913e09d577623a1c5c55893a1df
KERNEL_VERSION=4.19.325-perf
KERNEL_CONFIG=kernel-config
# O LK deste aparelho so aceita kernel comprimido.
KERNEL_IMAGE=Image.gz
KERNEL_DTB=mediatek/mt6833.dtb
# Codigo de vendor nao compila com -Werror em gcc moderno.
KERNEL_STRIP_WERROR=1
# Entradas do kernel alem do KERNEL_CONFIG (carimbo de reaproveitamento da build).
KERNEL_INPUTS=(patches connectivity)

# boot.img: offsets do boot.img de fabrica (header v2, pagina de 2048).
BOOTIMG_ARGS=(
	--header_version 2 --pagesize 2048 --base 0
	--kernel_offset 0x40080000 --ramdisk_offset 0x51100000
	--second_offset 0 --tags_offset 0x47c80000 --dtb_offset 0x47c80000
)
CMDLINE="bootopt=64S3,32N2,64N2 buildvariant=user"
ROOT_PARTITION=userdata
# O driver do toque e built-in e pede o firmware no probe, antes do switch_root.
INITRAMFS_FIRMWARE=(nt36672c_tm_01_ts_fw.bin nt36672c_tm_01_ts_mp.bin)

# Fastboot: A/B, vbmeta com a verificacao desligada. So o slot a foi testado.
FASTBOOT_PRODUCTS=(thunder)
# ro.product.device no Android de fabrica, para reconhecer o aparelho pelo adb.
ANDROID_DEVICES=(thunder light)
FASTBOOT_AB=1
FASTBOOT_SLOT=a
FASTBOOT_DISABLE_VBMETA=1
# O bootloader liga a protecao de escrita "power-on" do UFS na particao boot: de dentro
# do Linux a escrita volta DATA PROTECT (sense key 0x7). Kernel novo so pelo fastboot
# (tools/update-kernel.sh faz isso pelo cabo, sem botao).
BOOT_DEPLOY=fastboot

# Firmware proprietario: copiado da ROM de fabrica do usuario (tools/extract-firmware.sh),
# nunca do repositorio. Todos ficam em /firmware do vendor.
FIRMWARE_ROM_PARTITION=vendor
FIRMWARE_ROM_DIRS=(/firmware /etc/firmware)
FIRMWARE_FILES=(
	nt36672c_tm_01_ts_fw.bin nt36672c_tm_01_ts_mp.bin
	WIFI_RAM_CODE_soc2_2_1_1.bin WIFI_RAM_CODE_soc2_2_1a_1.bin
	soc2_2_ram_mcu_1_1_hdr.bin soc2_2_ram_mcu_1a_1_hdr.bin
	soc2_2_ram_wifi_1_1_hdr.bin soc2_2_ram_wifi_1a_1_hdr.bin
	soc2_2_ram_bt_1_1_hdr.bin soc2_2_ram_bt_1a_1_hdr.bin
	WMT_SOC.cfg WMT_SOC_L19_GL.cfg WMT_SOC_L19_IN.cfg WMT_SOC_L19N.cfg
	wifi.cfg txpowerctrl.cfg BT_FW.cfg
)

# Rootfs.
# Obrigatorios: sem eles a imagem nao sobe o Wi-Fi. O WMT_SOC.cfg o driver carrega sozinho
# por nome fixo nos chips SoC (wmt_conf.c:605-610), sem ioctl. Os ROM patches 1_1 sao os
# de WMT_ROM_PATCHES; o WIFI_RAM_CODE 1_1 e o firmware do radio que o gen4m pede.
FIRMWARE_REQUIRED=(
	nt36672c_tm_01_ts_fw.bin
	WMT_SOC.cfg
	soc2_2_ram_mcu_1_1_hdr.bin soc2_2_ram_wifi_1_1_hdr.bin soc2_2_ram_bt_1_1_hdr.bin
	WIFI_RAM_CODE_soc2_2_1_1.bin
)
# Conectividade (Wi-Fi e Bluetooth dividem o CONNSYS): o pacote thunder-mediatek-wmt
# (rootfs/packages) faz o papel do wmt_loader, do wmt_launcher e do wlan_assistant do
# Android. thunder-wmt-connsys inicializa o driver e fica residente; thunder-wmt-wifi
# liga o radio. O toque e uma regra do udev (overlay/usr/lib/udev/rules.d).
DEVICE_LOCAL_PACKAGES=(thunder-mediatek-wmt)
# Audio: o UCM do cartao (overlay/usr/share/alsa/ucm2/conf.d/mt6833-mt6359) e as regras
# do WirePlumber (overlay/usr/share/wireplumber). alsa-utils: a regra do udev roda o
# alsactl no boot, que aplica a FixedBootSequence do UCM (amplificador desligado, ganhos
# de fabrica; o padrao do driver e +8 dB no lineout). overlay/etc/systemd/system/
# alsa-restore.service -> /dev/null: sem asound.state gravado no desligamento, o restore
# do boot cai no init do UCM em vez de devolver um estado antigo (ganho alto, amplificador
# ligado).
# bluez: o bluetoothd do driver HCI btmtk_stp (thunder-wmt-bt). bluez-utils: bluetoothctl,
# unico jeito de parear no perfil console (sem tela de configuracoes), e btmon.
# iw: WoWLAN e diagnostico do Wi-Fi na suspensao (docs/suspend.md).
DEVICE_PACKAGES=(alsa-utils bluez bluez-utils iw)
DEVICE_SERVICES=(thunder-wmt-connsys.service thunder-wmt-wifi.service
	thunder-wmt-bt.service bluetooth.service xiaomi-thunder-suspend-guard.service)
# Modulos do nucleo WMT, antes do DO_MODULE_INIT.
WMT_CORE_MODULES=(wmt_drv connfem)
# Modulos do Wi-Fi, depois do CONNSYS pronto (ordem do init.wlan_drv.rc do Android).
WMT_WIFI_MODULES=(wmt_chrdev_wifi wlan_drv_gen4m)
# Driver HCI do Bluetooth (BlueZ) sobre o STP, depois do CONNSYS pronto; substitui o
# /dev/stpbt (bt_drv_6833) e a ponte em espaco de usuario (docs/bluetooth-bringup.md).
WMT_BT_MODULES=(btmtk_stp)
# Endereco BT de fabrica: <partlabel>:<arquivo no ext4>, 6 bytes na ordem de exibicao,
# lido sem montar. Vazio = o controlador fica sem endereco (unconfigured no BlueZ).
WMT_BT_ADDR=nvdata:/APCFG/APRDEB/BT_Addr
# ROM patches que o launcher registra; tipo e endereco vem do cabecalho de cada .bin.
# So a variante 1_1 foi validada neste aparelho (as 1a_1 tambem estao no firmware).
WMT_ROM_PATCHES=(soc2_2_ram_mcu_1_1_hdr.bin soc2_2_ram_wifi_1_1_hdr.bin soc2_2_ram_bt_1_1_hdr.bin)
# HIF do CONNSYS: STP_BTIF_FULL (0x03) com WMT_FM_COMM (2 << 4).
WMT_HIF=0x23
# NVRAM de fabrica do Wi-Fi (MAC e calibracao de RF): <partlabel>:<arquivo no ext4>, lida
# sem montar e entregue antes do 1o power-on. Vazio = sem NVRAM (status quo: MAC gerado).
# Ligada: no aparelho (2026-10-11) o 1o probe aplica a NVRAM ("NVRAM verdict: applied") e
# a rede associa, no boot e depois de desligar/religar o radio. Se um dia o veredito for
# "not applied" ou "unknown", o padrao volta para vazio (docs/wifi-bringup.md, T3).
WMT_WIFI_NVRAM=nvdata:/APCFG/APRDEB/WIFI
# NetworkManager so gerencia esta interface; extras do driver ficam de fora.
# Interface de estacao: o gen4m cria wlan0, wlan1, ap0 e p2p0, mas so a wlan0 e
# estacao de verdade.
WIFI_INTERFACE=wlan0
# 3,5 GiB de RAM: zram (o padrao, metade da RAM) em lz4, que o kernel tem (zstd nao), e
# 4 GiB de swap em disco atras dele, como a "extensao de memoria" do MIUI.
ZRAM_ALGORITHM=lz4
SWAPFILE_SIZE=4G
# overlay/etc/systemd/system-generators/systemd-gpt-auto-generator -> /dev/null: o disco da raiz e o UFS do Android, com ~60 particoes de vendor; a descoberta automatica nunca monta nem faz swapon delas.

# Enxerto da conectividade MediaTek (wmt_drv, wmt_chrdev_wifi, wlan_drv_gen4m, connfem,
# bt_drv) antes dos patches. common/ tem de vir do mesmo drop: o struct
# wmt_platform_bridge muda de layout entre geracoes, e a mistura erra em silencio em
# runtime. O Makefile do fonte da Xiaomi fixa o chip errado (CONNAC/6765).
device_kernel_prepare() {
	local src=$1 conn drop c=mtk-connectivity-gen4m-mt6833-2
	conn=$src/drivers/misc/mediatek/connectivity
	drop=$(mktemp -d)
	bsdtar -xf "$DEVICE_DIR/connectivity/$c.tar.gz" -C "$drop"
	for d in wmt_drv wmt_chrdev_wifi wlan_drv_gen4m connfem bt_drv; do
		rm -rf "${conn:?}/$d"
		cp -a "$drop/$c/$d" "$conn/$d"
	done
	cp -f "$drop/$c"/wmt_drv/{connectivity_build_in_adapter,wmt_build_in_adapter}.{c,h} "$conn/common/"
	cp -f "$DEVICE_DIR/connectivity/Makefile-mt6833" "$conn/Makefile"
	patch -p1 -d "$conn" < "$DEVICE_DIR/connectivity/gen4m-mt6833-kernel419.diff"
	rm -rf "$drop"
}
