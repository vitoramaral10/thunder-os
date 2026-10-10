#!/usr/bin/env python3
"""Inicializador do WMT (equivalente minimo do wmt_loader do Android).

Sem isto, wmt_lib_init() nunca roda: so' o chardev de deteccao sobe, as threads
wmtd_thread/wmtd_worker_thread nao existem, e "echo 1 > /dev/wmtWifi" falha com
"WMT turn on WIFI fail!".

Interface lida de wmt_drv/common_detect/wmt_detect.h:81-92.
"""
import fcntl, os, struct, sys

MAGIC = ord('w')
def _IOR(nr): return (2 << 30) | (4 << 16) | (MAGIC << 8) | nr
def _IOW(nr): return (1 << 30) | (4 << 16) | (MAGIC << 8) | nr

CMD = {
    "GET_CHIP_ID":        _IOR(0),
    "SET_CHIP_ID":        _IOW(1),
    "EXT_CHIP_DETECT":    _IOR(2),
    "GET_SOC_CHIP_ID":    _IOR(3),
    "DO_MODULE_INIT":     _IOR(4),
    "MODULE_CLEANUP":     _IOR(5),
    "GET_ADIE_CHIP_ID":   _IOR(9),
    "CONNSYS_SOC_HW_INIT":_IOR(10),
}

def chamar(fd, nome, arg=0):
    cmd = CMD[nome]
    try:
        r = fcntl.ioctl(fd, cmd, arg)
        print(f"  {nome:<20} cmd=0x{cmd:08x} -> {r} (0x{r & 0xffffffff:x})")
        return r
    except OSError as e:
        print(f"  {nome:<20} cmd=0x{cmd:08x} -> ERRO {e}")
        return None

def main():
    dev = "/dev/wmtdetect"
    if not os.path.exists(dev):
        print(f"ERRO: {dev} nao existe -- o wmt_drv esta carregado?")
        return 1
    fd = os.open(dev, os.O_RDWR)
    print(f"aberto {dev}")
    try:
        # ordem do wmt_loader do Android: preparar o hardware do CONSYS,
        # identificar o chip, registrar o id, e so' entao inicializar o driver.
        chamar(fd, "CONNSYS_SOC_HW_INIT")
        soc = chamar(fd, "GET_SOC_CHIP_ID")
        chamar(fd, "GET_ADIE_CHIP_ID")
        if soc is not None and soc > 0:
            chamar(fd, "SET_CHIP_ID", soc)
        print("  --- inicializando o driver principal ---")
        chamar(fd, "DO_MODULE_INIT")
    finally:
        os.close(fd)
    print("fim")
    return 0

sys.exit(main())
