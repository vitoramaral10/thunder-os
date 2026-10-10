#!/bin/sh
# Sequencia completa de bring-up do Wi-Fi.
exec >/tmp/sobe-wifi.log 2>&1
echo "=== $(date) ==="

NVRAM=/lib/firmware/wifi_nvram.bin

# A NVRAM vem da particao nvdata (ext4), em /APCFG/APRDEB/WIFI.  Guardamos uma
# copia em /lib/firmware; se sumir, recupera da particao.  Montagem SEMPRE
# somente leitura: nvdata guarda a calibracao de RF de fabrica.
if [ ! -s "$NVRAM" ]; then
    echo "--- extraindo nvram da particao nvdata ---"
    mkdir -p /mnt/nvdata-ro
    if mount -o ro /dev/disk/by-partlabel/nvdata /mnt/nvdata-ro 2>&1; then
        cp /mnt/nvdata-ro/APCFG/APRDEB/WIFI "$NVRAM" && chmod 644 "$NVRAM"
        umount /mnt/nvdata-ro
        echo "nvram extraida: $(wc -c < "$NVRAM") bytes"
    else
        echo "AVISO: nao montei nvdata; o MAC vai sair aleatorio"
    fi
fi

for m in wmt_drv connfem wmt_chrdev_wifi wlan_drv_gen4m; do modprobe $m; done
echo "modulos: $(cut -d' ' -f1 /proc/modules | sort | tr '\n' ' ')"
python3 /usr/local/bin/wmt-loader.py
sleep 2
setsid python3 /usr/local/bin/wmt-daemon.py 120 </dev/null >/tmp/wmtd.log 2>&1 &
sleep 6
echo "--- daemon ---"; head -10 /tmp/wmtd.log

# Ligar o radio.  Com a NVRAM habilitada e' preciso um ciclo descartavel
# primeiro: o MAC de fabrica so' pega no SEGUNDO probe (medido varias vezes).
# Sem NVRAM, um power-on simples -- o ciclo duplo deixa o radio num estado em
# que o supplicant nao associa ("Wi-Fi network could not be found"), mesmo
# enxergando a rede no scan.
if [ -e /etc/thunder-wifi-nvram ]; then
    echo "--- 1o power-on (descartavel) ---"
    timeout 90 sh -c 'echo 1 > /dev/wmtWifi' ; echo "rc=$?"
    sleep 4
    echo "--- desligando para empurrar a nvram ---"
    echo 0 > /dev/wmtWifi 2>&1
    sleep 3
    echo "--- nvram ---"
    python3 /usr/local/bin/wifi-nvram-push.py "$NVRAM"
    echo "--- ligando radio (definitivo) ---"
else
    echo "--- ligando radio (nvram desabilitada; MAC sai aleatorio) ---"
fi
timeout 90 sh -c 'echo 1 > /dev/wmtWifi' ; echo "rc=$?"
sleep 5
echo "--- MAC ---"
for i in wlan0 wlan1; do
    [ -e "/sys/class/net/$i/address" ] && echo "$i $(cat /sys/class/net/$i/address)"
done
echo "--- interfaces ---"
ls /sys/class/net/ | grep -viE 'ccmni|ifb|ip6|ip_vti|sit|tunl|dummy|^lo$' | tr '\n' ' '
echo
echo "--- MAC ---"
for i in wlan0 wlan1; do
    [ -e "/sys/class/net/$i/address" ] && echo "$i $(cat /sys/class/net/$i/address)"
done
echo "--- dmesg ---"
dmesg | grep -iE 'hif|stp_init|pwr_on|wlan|patch|nvram' | tail -12
echo "=== FIM ==="
