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
KERNEL_VERSION=4.19.191-perf
KERNEL_CONFIG=kernel-config
# O LK deste aparelho so aceita kernel comprimido.
KERNEL_IMAGE=Image.gz
KERNEL_DTB=mediatek/mt6833.dtb
# Codigo de vendor nao compila com -Werror em gcc moderno.
KERNEL_STRIP_WERROR=1

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
FIRMWARE_REQUIRED=(nt36672c_tm_01_ts_fw.bin)
# python: o bring-up do Wi-Fi (wmt-loader, wmt-daemon) e em Python.
DEVICE_PACKAGES=(python)
DEVICE_SERVICES=(touchscreen-wake wifi-thunder)
# Interface de estacao: o gen4m cria wlan0, wlan1, ap0 e p2p0, mas so a wlan0 e
# estacao de verdade.
WIFI_INTERFACE=wlan0

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
