#!/usr/bin/env python3
"""Empurra a NVRAM de Wi-Fi para o driver, antes de ligar o radio.

Sem isso o gen4m nao acha a NVRAM e sorteia um MAC local a cada boot -- o
endereco muda, o DHCP entrega um IP diferente toda vez, e a calibracao de RF
de fabrica nao e' aplicada.

O driver le' a NVRAM de "/data/nvram/APCFG/APRDEB/WIFI" (platform.c:93), um
caminho do Android que nao existe aqui.  Mas essa leitura esta' desativada
(`#if 0` em kalCfgDataRead): na pratica o buffer `g_aucNvram` e' preenchido
pelo userspace, escrevendo em /dev/wmtWifi com o prefixo de 12 bytes
"WR-BUF:NVRAM" seguido do conteudo cru (wmt_cdev_wifi.c:483-505).  O handler
e' registrado em wlanCreateWirelessDevice, entao o wlan_drv_gen4m precisa
estar carregado; o driver espera ate' 2 s por ele.

Tem de ser UMA unica chamada de write(): o driver fatia o mesmo buffer de
usuario em prefixo e payload.

A origem do arquivo e' a particao nvdata (ext4), em /APCFG/APRDEB/WIFI.
Montar somente leitura -- ela guarda a calibracao de fabrica:
    mount -o ro /dev/disk/by-partlabel/nvdata /mnt && cp /mnt/APCFG/APRDEB/WIFI ...
"""
import os, sys

NVRAM = sys.argv[1] if len(sys.argv) > 1 else "/lib/firmware/wifi_nvram.bin"
NO = "/dev/wmtWifi"
PREFIXO = b"WR-BUF:NVRAM"

def main():
    try:
        with open(NVRAM, "rb") as f:
            dados = f.read()
    except OSError as e:
        print(f"[nvram] nao li {NVRAM}: {e}", file=sys.stderr)
        return 1
    if not dados:
        print(f"[nvram] {NVRAM} esta' vazio", file=sys.stderr)
        return 1

    # O MAC fica no offset 4, logo apos o cabecalho de 4 bytes.
    if len(dados) >= 10:
        mac = ":".join(f"{b:02x}" for b in dados[4:10])
        local = "local/aleatorio" if dados[4] & 0x02 else "global/de fabrica"
        print(f"[nvram] {len(dados)} bytes, MAC {mac} ({local})")

    carga = PREFIXO + dados
    try:
        fd = os.open(NO, os.O_WRONLY)
    except OSError as e:
        print(f"[nvram] nao abri {NO}: {e}", file=sys.stderr)
        return 1
    try:
        n = os.write(fd, carga)          # precisa ser um write() so'
    except OSError as e:
        print(f"[nvram] write falhou: {e}", file=sys.stderr)
        return 1
    finally:
        os.close(fd)

    if n != len(carga):
        print(f"[nvram] write parcial: {n} de {len(carga)}", file=sys.stderr)
        return 1
    print(f"[nvram] enviados {len(dados)} bytes de NVRAM ao driver")
    return 0

sys.exit(main())
