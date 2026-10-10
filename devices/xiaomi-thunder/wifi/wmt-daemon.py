#!/usr/bin/env python3
"""Servico WMT: atende os pedidos do driver durante o power-on.

Equivalente minimo do launcher do Android. O driver publica um comando em texto
(ex.: "srh_rom_patch"), o userspace le' com read(), responde a informacao pedida
por ioctl, e sinaliza com write("ok"). Sem isso o power-on trava em
    wmt_ctrl_get_rom_patch_info: wmt_ctrl_ul_cmd fail(-2)
e o buffer fica ocupado ("cmd buf is occupied by (srh_rom_patch)").

Protocolo lido de wmt_dev.c: WMT_read:801 (publica comando),
WMT_write:769 (resposta "ok" -> wmt_lib_trigger_cmd_signal(0)),
WMT_poll:833 (avisa quando ha comando).
"""
import fcntl, os, select, struct, sys, time

MAGIC = 0xa0
def _IOW(nr, size): return (1 << 30) | (size << 16) | (MAGIC << 8) | nr
PTR = 8
SET_PATCH_NAME     = _IOW(4, PTR)
SET_PATCH_NUM      = _IOW(14, 4)
SET_ROM_PATCH_INFO = _IOW(31, PTR)
SET_STP_MODE       = _IOW(5, 4)

# hifconf: bits 0-3 = tipo de HIF, bits 4-7 = modo de FM.
# STP_BTIF_FULL = 0x03 (wmt_dev.h:21) -- e' o transporte do CONSYS do MT6833.
# WMT_FM_COMM   = 2    (wmt_core.h:115)
# Sem esta chamada o power-on morre em wmt_core_stp_init(800): "no hif info!"
HIFCONF = 0x03 | (2 << 4)   # 0x23

FW = "/lib/firmware/"

# Nao ha' tabela fixa de tipos: cada .bin declara o proprio tipo e o proprio
# endereco no cabecalho (struct wmt_rom_patch, wmt_core.h:441).  Campos u32 do
# cabecalho estao em big-endian.
#   bytes 24..27 = u4PatchAddr, lido como u32 little-endian: 0xF0000011 (mcu),
#                  0xF0170011 (bt), 0xF02A0011 (wifi).  O 0xF0 alto e' a base da
#                  EMI vista pelo CONSYS e o 0x11 baixo e' um flag -- NENHUM dos
#                  dois entra no offset.  A verdade de origem esta' no proprio
#                  driver, em consys_emi_entry_address (mt6833.c:1020):
#                      0x1800_2504 = 0xF017_0000   (BT)
#                      0x1800_2508 = 0xF02A_0000   (WIFI)
#                  Logo os offsets sao 0x000000 (mcu), 0x170000 (bt) e 0x2A0000
#                  (wifi).  O driver monta o offset com
#                  (addRess[2]<<16)|(addRess[1]<<8)|addRess[0] (wmt_ic_soc.c:3599)
#                  e ignora addRess[3], entao mandamos addRess[0] = 0.
#                  Deslocar o patch do MCU em 0x11 bytes ja' bastou para ele nao
#                  dar boot: o PC anda em passos uniformes varrendo EMI vazia.
#   bytes 28..31 = u4PatchType  -> ENUM_WMTDRV_TYPE: 0=BT, 3=WIFI, 4=WMT/MCU.
# Mandar tipo errado (ou endereco zerado) faz os tres patches caírem em
# EmiOffset=0x0, um por cima do outro: o MCU do CONSYS acorda com lixo, sai
# executando EMI vazia e o power-on morre em consys_polling_goto_idle com
# -WMT_ERRCODE_POLL_NOT_GOTO_IDLE (-7).
ARQUIVOS = [
    "soc2_2_ram_mcu_1_1_hdr.bin",
    "soc2_2_ram_wifi_1_1_hdr.bin",
    "soc2_2_ram_bt_1_1_hdr.bin",
]

def le_cabecalho(nome):
    try:
        with open(FW + nome, "rb") as f:
            hdr = f.read(32)
    except OSError as e:
        print(f"[daemon] {nome}: {e}", flush=True)
        return None
    if len(hdr) < 32:
        print(f"[daemon] {nome}: cabecalho curto ({len(hdr)}B)", flush=True)
        return None
    # zera o byte de flag; so' os bytes 1 e 2 formam o offset
    addr = bytes([0x00, hdr[25], hdr[26], hdr[27]])
    tipo = struct.unpack(">I", hdr[28:32])[0]
    return tipo, addr

def info(tipo, addr, nome):
    return bytearray(struct.pack("<I4s256s", tipo, addr, nome.encode()[:255]))

def main():
    dev = "/dev/stpwmt"
    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    print(f"[daemon] aberto {dev}, aguardando pedidos", flush=True)

    # pre-registra tudo que sabemos, antes de qualquer pedido
    def tenta(cmd, arg, rotulo):
        # cada chamada e' independente: uma falha nao pode abortar as outras
        try:
            fcntl.ioctl(fd, cmd, arg)
            print(f"[daemon] {rotulo}: ok", flush=True)
            return True
        except OSError as e:
            print(f"[daemon] {rotulo}: {e}", flush=True)
            return False

    # HIF PRIMEIRO: sem isso o power-on morre em
    # wmt_core_stp_init(800) "no hif info!" mesmo com os patches certos.
    tenta(SET_STP_MODE, HIFCONF, f"hif 0x{HIFCONF:02x} (BTIF_FULL+FM_COMM)")

    patches = []
    for nome in ARQUIVOS:
        c = le_cabecalho(nome)
        if c is not None:
            patches.append((c[0], c[1], nome))

    # SET_PATCH_NUM so' aceita uma chamada por carga do modulo (wmt_dev.c:1138
    # recusa com -1/EPERM se pAtchNum ja' e' > 0).  E' o caminho legado, nao
    # afeta o rom patch; a falha aqui nao impede nada.
    tenta(SET_PATCH_NUM, len(patches), f"patch_num={len(patches)}")

    # Idem: wmt_lib_set_rom_patch_info so' grava a primeira vez por tipo.  Se um
    # tipo ja' foi registrado errado, e' preciso recarregar o wmt_drv.
    for tipo, addr, nome in patches:
        off = addr[0] | (addr[1] << 8) | (addr[2] << 16)
        tenta(SET_ROM_PATCH_INFO, info(tipo, addr, nome),
              f"patch tipo {tipo} off 0x{off:06x} {nome}")

    tenta(SET_PATCH_NAME, bytearray(FW.encode() + b"\0" * 250), "patch_name")

    poller = select.poll()
    poller.register(fd, select.POLLIN)
    fim = time.time() + float(sys.argv[1]) if len(sys.argv) > 1 else time.time() + 60

    while time.time() < fim:
        for _fd, ev in poller.poll(1000):
            try:
                cmd = os.read(fd, 256).decode(errors="replace").strip("\0").strip()
            except OSError as e:
                print(f"[daemon] read falhou: {e}", flush=True)
                continue
            if not cmd:
                continue
            print(f"[daemon] pedido: {cmd!r}", flush=True)
            # responde: a informacao ja' foi registrada por ioctl acima,
            # so' falta confirmar para o driver seguir.
            try:
                os.write(fd, b"ok")
                print("[daemon]   respondido ok", flush=True)
            except OSError as e:
                print(f"[daemon]   write falhou: {e}", flush=True)
    os.close(fd)
    print("[daemon] fim", flush=True)
    return 0

sys.exit(main())
