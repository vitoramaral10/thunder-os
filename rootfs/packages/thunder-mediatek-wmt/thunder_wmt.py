#!/usr/bin/env python3
"""thunder-wmt: userspace da conectividade MediaTek WMT (SoC CONNAC 1.0, ex.: MT6833).

Faz o papel de tres servicos do Android, na ordem do init deles:

  wmt_loader      ioctls em /dev/wmtdetect que inicializam o driver (DO_MODULE_INIT)
  wmt_launcher    registra o HIF e os ROM patches em /dev/stpwmt e fica residente com o
                  no aberto, atendendo os pedidos de texto do driver
  wlan_assistant  entrega a NVRAM de fabrica do Wi-Fi (MAC e calibracao de RF) ao driver,
                  antes do primeiro power-on

e, no lugar da pilha de Bluetooth do Android, sobe o driver HCI do kernel (btmtk_stp,
BlueZ) e grava nele o endereco de fabrica do BT.

Comandos (um por unit do systemd):

  thunder-wmt connsys    loader + launcher residente (Type=notify; SIGTERM encerra)
  thunder-wmt wifi-on    modulos do Wi-Fi, NVRAM, power-on, espera a interface
  thunder-wmt wifi-off   power-off do Wi-Fi (nao le a configuracao: e o ExecStop)
  thunder-wmt bt-on      modulo HCI do BT, endereco de fabrica pelo mgmt do BlueZ
  thunder-wmt nvram-check  so le e valida a NVRAM, sem tocar no driver (diagnostico)

Configuracao: /etc/thunder/wmt.conf (gerado pelo rootfs/configure.sh a partir das WMT_*
do device.sh). Log no journal (stderr com prefixo de prioridade <N>). Codigos de saida
do sysexits.h, que o systemd mostra pelo nome (status=78/CONFIG).

Fonte dos fatos: wmt_drv/common_main e wlan_drv_gen4m do drop de conectividade do
aparelho (devices/xiaomi-thunder/connectivity/); referencias por arquivo:linha em
devices/xiaomi-thunder/docs/wifi-bringup.md.
"""
from __future__ import annotations

import argparse
import array
import errno
import fcntl
import os
import re
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass

# --- codigos de saida (sysexits.h) ------------------------------------------------
EX_OK = 0
EX_USAGE = 64        # linha de comando
EX_NOINPUT = 66      # NVRAM ilegivel (so nvram-check)
EX_UNAVAILABLE = 69  # o driver nao subiu: no de dispositivo ausente, DO_MODULE_INIT falhou
EX_SOFTWARE = 70     # bug nosso
EX_OSERR = 71        # modprobe falhou, ou o marcador de pronto nao foi gravado
EX_OSFILE = 72       # ROM patch ausente ou com cabecalho invalido
EX_IOERR = 74        # power-on/off do Wi-Fi falhou, a interface nao apareceu, ou o
                     # controlador BT recusou o endereco
EX_PROTOCOL = 76     # o driver recusou um ioctl do launcher
EX_CONFIG = 78       # /etc/thunder/wmt.conf ausente ou invalido

CONFIG_PATH = "/etc/thunder/wmt.conf"

# --- nos do driver ------------------------------------------------------------------
DEV_DETECT = "/dev/wmtdetect"   # wmt_drv, no load do modulo
DEV_STPWMT = "/dev/stpwmt"      # nasce no DO_MODULE_INIT
# Marcador do connsys pronto (RuntimeDirectory=thunder-wmt da unit; some quando ela para).
READY_MARK = "/run/thunder-wmt/connsys-ready"
DEV_WIFI = "/dev/wmtWifi"       # wmt_chrdev_wifi, no load do modulo

# Os nos nascem no devtmpfs dentro do proprio device_create() (sincrono), entao em geral
# ja existem quando o modprobe/ioctl volta; a espera e so uma rede de seguranca.
NODE_TIMEOUT = 5.0
# A interface nasce no probe do gen4m, dentro do write("1"); espera curta.
IFACE_TIMEOUT = 10.0
# Registro do hci_dev no load do btmtk_stp (o power-on do BT fica com o bluetoothd).
HCI_TIMEOUT = 10.0
SYS_BLUETOOTH = "/sys/class/bluetooth"
# Um chip reset inteiro em andamento faz o driver recusar operacoes com EIO
# (wmt_lib_put_act_op, wmt_lib.c:1623-1627). O driver nao expoe esse estado em sysfs/proc,
# entao nao ha sinal para consultar: uma unica nova tentativa depois de uma pausa curta.
# 3 s cobre o ciclo de recuperacao medido (~1,5 s de power-on com calibracao).
EIO_RETRY_DELAY = 3.0
POLL_STEP = 0.05

# --- ioctls (wmt_detect.h:81-92 e wmt_dev.c:94-129) ---------------------------------
def _ioc(direction: int, magic: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (magic << 8) | nr

_IOC_WRITE, _IOC_READ = 1, 2
_DETECT = ord("w")
COMBO_IOCTL_SET_CHIP_ID = _ioc(_IOC_WRITE, _DETECT, 1, 4)
COMBO_IOCTL_GET_SOC_CHIP_ID = _ioc(_IOC_READ, _DETECT, 3, 4)
COMBO_IOCTL_DO_MODULE_INIT = _ioc(_IOC_READ, _DETECT, 4, 4)
COMBO_IOCTL_GET_ADIE_CHIP_ID = _ioc(_IOC_READ, _DETECT, 9, 4)
COMBO_IOCTL_CONNSYS_SOC_HW_INIT = _ioc(_IOC_READ, _DETECT, 10, 4)

_WMT = 0xA0
WMT_IOCTL_SET_STP_MODE = _ioc(_IOC_WRITE, _WMT, 5, 4)
WMT_IOCTL_SET_ROM_PATCH_INFO = _ioc(_IOC_WRITE, _WMT, 31, 8)   # char*: 8 bytes no arm64

# --- ROM patches (struct wmt_rom_patch, wmt_core.h:441; wmt_rom_patch_info, wmt_lib.h:175)
ROM_HEADER_LEN = 32
ROM_PLATFORM = b"ALPS"                       # bytes 16..19 de todo patch deste drop
WMTDRV_TYPES = {0: "BT", 1: "FM", 2: "GPS", 3: "WIFI", 4: "WMT"}   # wmt_exp.h; < ANT (5)
ROM_PATCH_INFO = struct.Struct("<I4s256s")   # type, addRess[4], patchName[256]

# --- NVRAM do Wi-Fi (wmt_cdev_wifi.c WIFI_write; gl_init.c wlanNvramBufHandler) ----
NVRAM_PREFIX = b"WR-BUF:NVRAM"
NVRAM_MAX = 8192            # g_aucNvram[MAX_CFG_FILE_WIFI_REC_SIZE] (CFG_Wifi_File.h:569)
NVRAM_MAC_OFFSET = 4        # WIFI_CFG_PARAM_STRUCT.aucMacAddress
DRIVER_FALLBACK_OUI = b"\x00\x08\x22"   # wlanUpdateNetworkAddress: 00:08:22 + systick

# --- ethtool (MAC permanente: o gen4m copia o MAC do probe em perm_addr) ------------
SIOCETHTOOL = 0x8946
ETHTOOL_GPERMADDR = 0x20
SIOCGIFFLAGS = 0x8913
SIOCSIFFLAGS = 0x8914
IFF_UP = 0x1
IFREQ_FLAGS = struct.Struct("16sH22x")     # struct ifreq: ifr_name + ifr_flags (40 bytes)
SYS_NET = "/sys/class/net"

# mgmt do BlueZ (doc/mgmt-api.txt): socket HCI no canal de controle.
BTPROTO_HCI = 1
HCI_CHANNEL_CONTROL = 3
MGMT_INDEX_NONE = 0xFFFF
# ioctl HCIGETDEVINFO (hci_sock.h): struct hci_dev_info, flags em 16 (dev_id, name[8],
# bdaddr[6] antes). HCI_UP (bit 0) vem mascarado enquanto HCI_AUTO_OFF esta ativo
# (hci_get_dev_info); HCI_RUNNING (bit 2) nao: sobe no hci_dev_do_open e cai no close.
HCIGETDEVINFO = _ioc(_IOC_READ, ord("H"), 211, 4)
HCI_DEV_INFO_SIZE = 92
HCI_DEV_INFO_FLAGS = 16
HCI_UP = 1 << 0
HCI_RUNNING = 1 << 2
MGMT_OP_READ_INDEX_LIST = 0x0003
MGMT_OP_READ_INFO = 0x0004
MGMT_OP_SET_POWERED = 0x0005
MGMT_OP_SET_PUBLIC_ADDRESS = 0x0039
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002
MGMT_STATUS_REJECTED = 0x0b
MGMT_STATUS_INVALID_INDEX = 0x11
MGMT_HDR = struct.Struct("<HHH")       # opcode/evento, indice do controlador, tamanho
MGMT_TIMEOUT = 5.0
# Setup do controlador (power-on do BT pelo WMT, firmware, init do HCI) e a reconfiguracao
# depois do Set Public Address. Leva ~4 s; no pior caso a gravacao do endereco no driver
# esgota tres tentativas de tres comandos de 2 s (18 s) antes de o power-on terminar.
BT_SETUP_TIMEOUT = 40.0
# Tentativas de gravar o endereco: a do Set Public Address e mais duas por power-on.
BT_ADDR_ATTEMPTS = 3
# Endereco que o controlador da MediaTek traz quando ninguem gravou nada (igual em todo
# aparelho): na NVRAM ele quer dizer "nao gravado".
BT_PLACEHOLDER_ADDR = bytes.fromhex("000046676101")

_NAME = re.compile(r"[A-Za-z0-9_.+-]+\Z")
_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_VALUE = re.compile(r"[A-Za-z0-9_.:/+-]+\Z")


class Failure(Exception):
    """Erro com codigo de saida e mensagem para o journal."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConfigError(ValueError):
    pass


class NvramError(ValueError):
    pass


# --- log ----------------------------------------------------------------------------
LOG_ERR, LOG_WARNING, LOG_NOTICE, LOG_INFO, LOG_DEBUG = 3, 4, 5, 6, 7


def log(prio: int, message: str) -> None:
    # Com stderr no journal (JOURNAL_STREAM), o prefixo <N> vira a prioridade da linha.
    prefix = f"<{prio}>" if "JOURNAL_STREAM" in os.environ else ""
    print(f"{prefix}{message}", file=sys.stderr, flush=True)


def sd_notify(message: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.connect(addr)
            s.sendall(message.encode())
    except OSError as e:
        log(LOG_WARNING, f"sd_notify failed: {e}")


# --- configuracao (funcoes puras) ---------------------------------------------------
def parse_config(text: str) -> dict[str, list[str]]:
    """KEY=valor por linha, '#' comenta; valor com aspas de shell; listas por espaco."""
    out: dict[str, list[str]] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not _KEY.match(key):
            raise ConfigError(f"line {n}: expected KEY=value")
        try:
            tokens = shlex.split(value, comments=True)
        except ValueError as e:
            raise ConfigError(f"line {n}: {e}") from None
        for t in tokens:
            if not _VALUE.match(t):
                raise ConfigError(f"line {n}: invalid value {t!r}")
        out[key] = tokens
    return out


def parse_nvram_spec(spec: str, key: str = "WMT_WIFI_NVRAM") -> tuple[str, str]:
    """'<partlabel>:<caminho absoluto no ext4>' -> (partlabel, caminho)."""
    label, sep, path = spec.partition(":")
    if not sep or not _NAME.match(label) or not path.startswith("/") or "//" in path:
        raise ConfigError(f"{key}: expected <partlabel>:/path, got {spec!r}")
    return label, path


@dataclass(frozen=True)
class Config:
    core_modules: tuple[str, ...]
    rom_patches: tuple[str, ...]
    hif: int
    firmware_dir: str
    wifi_modules: tuple[str, ...]
    wifi_interface: str
    wifi_nvram: tuple[str, str] | None
    bt_modules: tuple[str, ...] = ()
    bt_addr: tuple[str, str] | None = None


def config_from_mapping(m: dict[str, list[str]]) -> Config:
    def scalar(key: str, default: str) -> str:
        v = m.get(key)
        if v is None or not v:
            return default
        if len(v) > 1:
            raise ConfigError(f"{key}: expected one value, got {len(v)}")
        return v[0]

    def names(key: str) -> tuple[str, ...]:
        v = tuple(m.get(key, ()))
        for x in v:
            if not _NAME.match(x):
                raise ConfigError(f"{key}: invalid name {x!r}")
        return v

    try:
        hif = int(scalar("WMT_HIF", "0x23"), 0)
    except ValueError:
        raise ConfigError("WMT_HIF: not an integer") from None
    if not 0 <= hif <= 0xFF:
        raise ConfigError("WMT_HIF: out of range 0..0xff")
    fw = scalar("WMT_FIRMWARE_DIR", "/usr/lib/firmware")
    if not fw.startswith("/"):
        raise ConfigError("WMT_FIRMWARE_DIR: must be an absolute path")
    iface = scalar("WMT_WIFI_INTERFACE", "")
    if iface and (not _NAME.match(iface) or len(iface) > 15):
        raise ConfigError(f"WMT_WIFI_INTERFACE: invalid interface name {iface!r}")
    nv = scalar("WMT_WIFI_NVRAM", "")
    bt_addr = scalar("WMT_BT_ADDR", "")
    return Config(
        core_modules=names("WMT_CORE_MODULES"),
        rom_patches=names("WMT_ROM_PATCHES"),
        hif=hif,
        firmware_dir=fw.rstrip("/") or "/",
        wifi_modules=names("WMT_WIFI_MODULES"),
        wifi_interface=iface,
        wifi_nvram=parse_nvram_spec(nv, "WMT_WIFI_NVRAM") if nv else None,
        bt_modules=names("WMT_BT_MODULES"),
        bt_addr=parse_nvram_spec(bt_addr, "WMT_BT_ADDR") if bt_addr else None,
    )


def load_config(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise Failure(EX_CONFIG, f"cannot read {path}: {e.strerror}") from None
    try:
        return config_from_mapping(parse_config(text))
    except ConfigError as e:
        raise Failure(EX_CONFIG, f"{path}: {e}") from None


# --- ROM patches (funcoes puras) ----------------------------------------------------
@dataclass(frozen=True)
class RomPatch:
    name: str
    type: int
    addr: bytes   # addRess[4] do ioctl

    @property
    def emi_offset(self) -> int:
        # wmt_ic_soc.c:3600: (addRess[2] << 16) | (addRess[1] << 8) | addRess[0]
        return self.addr[0] | (self.addr[1] << 8) | (self.addr[2] << 16)


def parse_rom_patch_header(name: str, hdr: bytes) -> RomPatch:
    """Tipo e endereco declarados no cabecalho do .bin (campos u32 em big-endian).

    u4PatchAddr (bytes 24..27) lido em little-endian da 0xF0xxxx11: o 0x11 baixo e
    flag e o 0xF0 alto e a base da EMI vista pelo CONNSYS; nenhum dos dois entra no
    offset. Mandar o byte 24 desloca o patch em 0x11 bytes e o MCU nao da boot.
    """
    if len(hdr) < ROM_HEADER_LEN:
        raise ValueError(f"{name}: header too short ({len(hdr)} bytes)")
    if hdr[16:20] != ROM_PLATFORM:
        raise ValueError(f"{name}: not a WMT ROM patch (no {ROM_PLATFORM.decode()} tag)")
    ptype = struct.unpack_from(">I", hdr, 28)[0]
    if ptype not in WMTDRV_TYPES:
        raise ValueError(f"{name}: unknown patch type {ptype}")
    return RomPatch(name=name, type=ptype, addr=bytes((0, hdr[25], hdr[26], hdr[27])))


def pack_rom_patch_info(p: RomPatch) -> bytearray:
    raw = p.name.encode()
    if len(raw) > 255:
        raise ValueError(f"{p.name}: name longer than 255 bytes")
    return bytearray(ROM_PATCH_INFO.pack(p.type, p.addr, raw))


def check_rom_patch_set(patches: list[RomPatch]) -> None:
    """O driver guarda um registro por tipo (o 2o e ignorado em silencio) e sem o WMT
    (tipo 4, o MCU) o CONNSYS nao sobe."""
    seen: dict[int, str] = {}
    for p in patches:
        if p.type in seen:
            raise ValueError(f"{p.name} and {seen[p.type]} are both type "
                             f"{WMTDRV_TYPES[p.type]}; the driver keeps only one per type")
        seen[p.type] = p.name
    if 4 not in seen:
        raise ValueError("no WMT (MCU, type 4) ROM patch: CONNSYS cannot boot without it")


# --- NVRAM (funcoes puras) ----------------------------------------------------------
def validate_nvram(data: bytes) -> None:
    # Regra do wlan_assistant: tamanho - 2 multiplo de 256 (o WIFI tem 2050 bytes).
    if len(data) < NVRAM_MAC_OFFSET + 6 or (len(data) - 2) % 256:
        raise NvramError(f"unexpected NVRAM size {len(data)} (want 256*n + 2 bytes)")
    if len(data) > NVRAM_MAX:
        raise NvramError(f"NVRAM of {len(data)} bytes exceeds the driver buffer ({NVRAM_MAX})")


def nvram_mac(data: bytes) -> bytes:
    return bytes(data[NVRAM_MAC_OFFSET:NVRAM_MAC_OFFSET + 6])


def nvram_write_buffer(data: bytes) -> bytes:
    """Um write() so: o driver fatia o mesmo buffer em prefixo e carga."""
    validate_nvram(data)
    return NVRAM_PREFIX + data


def mac_kind(mac: bytes) -> str:
    if not any(mac):
        return "zero"
    if mac[0] & 1:
        return "multicast"
    return "locally administered" if mac[0] & 2 else "globally unique"


VERDICT = "NVRAM verdict: "


def nvram_outcome(expected: bytes, permanent: bytes) -> tuple[int, str]:
    """Resultado do push, sem imprimir enderecos (journal pode ir para relatorio).
    A mensagem e a unica linha de veredito do servico (prefixo VERDICT)."""
    if len(permanent) != 6 or not any(permanent):
        return LOG_NOTICE, (f"{VERDICT}unknown (permanent address unavailable; cannot tell "
                            "whether the factory NVRAM took)")
    if permanent == expected:
        return LOG_INFO, f"{VERDICT}applied (the interface has the factory MAC)"
    if permanent[:3] == DRIVER_FALLBACK_OUI:
        return LOG_WARNING, (f"{VERDICT}not applied (pushed, but the driver generated a fallback "
                             "MAC with the 00:08:22 prefix and the factory RF calibration was "
                             "not sent to the firmware)")
    return LOG_WARNING, (f"{VERDICT}not applied (the interface MAC is neither the factory nor "
                         "the driver fallback MAC)")


# --- endereco do Bluetooth (funcoes puras) -----------------------------------------
def bt_addr_from_nvram(data: bytes) -> bytes:
    """BT_Addr da NVRAM (6 bytes na ordem de exibicao) -> bdaddr do HCI (LSB primeiro).
    A ordem foi medida no aparelho: gravado invertido, o Read_BD_ADDR devolve exatamente o
    que esta na NVRAM (docs/bluetooth-bringup.md)."""
    if len(data) < 6:
        raise NvramError(f"BT address of {len(data)} bytes (want 6)")
    addr = bytes(data[:6])
    if addr in (b"\x00" * 6, b"\xff" * 6, BT_PLACEHOLDER_ADDR):
        raise NvramError("BT address in NVRAM is unset (zero, ff or the MediaTek placeholder)")
    if addr[0] & 1:
        raise NvramError("BT address in NVRAM is a multicast address")
    return addr[::-1]


def mgmt_packet(opcode: int, index: int, params: bytes = b"") -> bytes:
    return MGMT_HDR.pack(opcode, index, len(params)) + params


def mgmt_set_public_address(index: int, bdaddr: bytes) -> bytes:
    """Comando mgmt Set Public Address (bdaddr LSB primeiro, como no HCI)."""
    if len(bdaddr) != 6:
        raise ValueError("bdaddr must be 6 bytes")
    return mgmt_packet(MGMT_OP_SET_PUBLIC_ADDRESS, index, bdaddr)


def mgmt_reply(packet: bytes, index: int, opcode: int) -> tuple[int, bytes] | None:
    """(status, parametros de retorno) da resposta a 'opcode' no controlador 'index';
    None se o pacote e outro evento (o canal de controle tambem entrega eventos que nao
    pedimos)."""
    if len(packet) < MGMT_HDR.size + 3:
        return None
    event, idx, plen = MGMT_HDR.unpack_from(packet)
    if event not in (MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS) or idx != index:
        return None
    op, status = struct.unpack_from("<HB", packet, MGMT_HDR.size)
    if op != opcode:
        return None
    end = MGMT_HDR.size + plen
    return status, bytes(packet[MGMT_HDR.size + 3:end])


def hci_dev_info_flags(info: bytes) -> int:
    """flags de um struct hci_dev_info devolvido pelo HCIGETDEVINFO."""
    if len(info) < HCI_DEV_INFO_FLAGS + 4:
        raise ValueError("short hci_dev_info")
    return struct.unpack_from("<I", info, HCI_DEV_INFO_FLAGS)[0]


def mgmt_index_list(params: bytes) -> list[int]:
    """Read Controller Index List: num (2 bytes) e os indices."""
    if len(params) < 2:
        return []
    (num,) = struct.unpack_from("<H", params)
    return list(struct.unpack_from(f"<{num}H", params, 2)) if len(params) >= 2 + 2 * num else []


def hci_index(sys_bluetooth: str = SYS_BLUETOOTH) -> int | None:
    """Indice do primeiro controlador hciN registrado, ou None."""
    try:
        names = os.listdir(sys_bluetooth)
    except OSError:
        return None
    idx = sorted(int(n[3:]) for n in names if re.fullmatch(r"hci[0-9]+", n))
    return idx[0] if idx else None


# --- utilitarios de sistema ---------------------------------------------------------
def wait_for_path(path: str, timeout: float, what: str) -> None:
    """Espera um no do devtmpfs/sysfs aparecer. Sysfs nao gera inotify, e os nos do
    devtmpfs nascem sincronos com o device_create(): checagem curta com prazo."""
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() >= deadline:
            raise Failure(EX_UNAVAILABLE, f"{what}: {path} did not appear within {timeout:g} s")
        time.sleep(POLL_STEP)


def announce_ready(status: str) -> None:
    """Marcador primeiro, READY=1 depois: o systemd pode iniciar o wifi-on logo apos o
    READY=1, e o wifi-on exige o marcador. Sem conseguir grava-lo, o connsys falha (71) e o
    Restart=on-failure tenta de novo, em vez de ficar ativo com o Wi-Fi recusando."""
    try:
        with open(READY_MARK, "w") as f:
            f.write(status + "\n")
    except OSError as e:
        raise Failure(EX_OSERR, f"cannot write {READY_MARK}: {e.strerror or e}") from None
    sd_notify(f"READY=1\nSTATUS={status}")


def modprobe(modules: tuple[str, ...]) -> None:
    if not modules:
        return
    r = subprocess.run(["modprobe", "-a", "--", *modules], capture_output=True, text=True)
    if r.returncode != 0:
        detail = (r.stderr.strip().splitlines() or ["no output"])[-1]
        raise Failure(EX_OSERR, f"modprobe {' '.join(modules)} failed: {detail}")
    log(LOG_DEBUG, f"modules loaded: {' '.join(modules)}")


def ioctl_int(fd: int, request: int, arg: int = 0) -> int:
    return fcntl.ioctl(fd, request, arg)


def permanent_address(iface: str) -> bytes:
    buf = array.array("B", struct.pack("=II", ETHTOOL_GPERMADDR, 32) + bytes(32))
    ptr, _ = buf.buffer_info()
    ifr = struct.pack("16sP", iface.encode(), ptr).ljust(40, b"\0")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        fcntl.ioctl(s.fileno(), SIOCETHTOOL, ifr)
    size = struct.unpack_from("=I", buf, 4)[0]
    return bytes(buf[8:8 + min(size, 32)])


# --- connsys: loader + launcher -----------------------------------------------------
def read_rom_patches(cfg: Config) -> list[RomPatch]:
    if not cfg.rom_patches:
        raise Failure(EX_CONFIG, "WMT_ROM_PATCHES is empty")
    patches = []
    for name in cfg.rom_patches:
        path = f"{cfg.firmware_dir}/{name}"
        try:
            with open(path, "rb") as f:
                patches.append(parse_rom_patch_header(name, f.read(ROM_HEADER_LEN)))
        except OSError as e:
            raise Failure(EX_OSFILE, f"ROM patch {path}: {e.strerror}") from None
        except ValueError as e:
            raise Failure(EX_OSFILE, f"ROM patch {path}: {e}") from None
    try:
        check_rom_patch_set(patches)
    except ValueError as e:
        raise Failure(EX_OSFILE, str(e)) from None
    return patches


def wmt_loader() -> None:
    """wmt_loader do Android. So roda uma vez por carga do wmt_drv: num restart do
    servico o /dev/stpwmt ja existe e o CONNSYS_SOC_HW_INIT nao e repetido com o
    CONNSYS possivelmente ligado."""
    if os.path.exists(DEV_STPWMT):
        log(LOG_INFO, "WMT driver already initialised (service restart); skipping the loader")
        return
    wait_for_path(DEV_DETECT, NODE_TIMEOUT, "wmt_drv")
    fd = os.open(DEV_DETECT, os.O_RDWR | os.O_CLOEXEC)
    try:
        try:
            ioctl_int(fd, COMBO_IOCTL_CONNSYS_SOC_HW_INIT)
        except OSError as e:
            # O fluxo validado no aparelho sempre ignorou este retorno.
            log(LOG_WARNING, f"CONNSYS_SOC_HW_INIT: {e.strerror}; continuing")
        try:
            soc = ioctl_int(fd, COMBO_IOCTL_GET_SOC_CHIP_ID)
        except OSError as e:
            raise Failure(EX_UNAVAILABLE, f"GET_SOC_CHIP_ID: {e.strerror}") from None
        if soc <= 0:
            raise Failure(EX_UNAVAILABLE, f"GET_SOC_CHIP_ID returned {soc}")
        try:
            adie = ioctl_int(fd, COMBO_IOCTL_GET_ADIE_CHIP_ID)
        except OSError:
            adie = -1
        try:
            ioctl_int(fd, COMBO_IOCTL_SET_CHIP_ID, soc)
        except OSError as e:
            raise Failure(EX_UNAVAILABLE, f"SET_CHIP_ID 0x{soc:x}: {e.strerror}") from None
        try:
            # Sincrono (modo .ko): volta depois do WMT_init; o retorno e a sinalizacao.
            ioctl_int(fd, COMBO_IOCTL_DO_MODULE_INIT)
        except OSError as e:
            raise Failure(EX_UNAVAILABLE, f"DO_MODULE_INIT failed: {e.strerror}") from None
        adie_s = f"0x{adie:x}" if adie >= 0 else "unknown"
        log(LOG_INFO, f"WMT driver initialised: SoC 0x{soc:x}, A-die {adie_s}")
    finally:
        os.close(fd)


class _Stop(Exception):
    pass


def _raise_stop(signum: int, _frame: object) -> None:
    raise _Stop(signum)


def register_rom_patches(fd: int, patches: list[RomPatch]) -> None:
    for p in patches:
        try:
            # O driver aceita um registro por tipo e devolve 0 tambem para o duplicado.
            fcntl.ioctl(fd, WMT_IOCTL_SET_ROM_PATCH_INFO, pack_rom_patch_info(p))
        except OSError as e:
            raise Failure(EX_PROTOCOL, f"SET_ROM_PATCH_INFO {p.name}: {e.strerror}") from None
        log(LOG_DEBUG, f"ROM patch {p.name}: type {WMTDRV_TYPES[p.type]}, "
                       f"EMI offset 0x{p.emi_offset:06x}")


def answer(fd: int, command: str, patches: list[RomPatch]) -> None:
    if command == "srh_rom_patch":
        # O driver so pergunta se o registro do tipo WMT sumiu: registra de novo.
        register_rom_patches(fd, patches)
        reply = b"ok"
        log(LOG_NOTICE, "driver asked for the ROM patches again; re-registered")
    else:
        # srh_patch (multi-patch legado, desligado neste chip), update_patch_version
        # (so com SET_ACTIVE_PATCH_VERSION) e open_stp/close_stp/baud_* (so UART): nada
        # a responder. "fail" libera o driver na hora em vez de 6 s de timeout.
        reply = b"fail"
        log(LOG_WARNING, f"driver request {command!r} is not supported; answered fail")
    os.write(fd, reply)


def cmd_connsys(cfg: Config) -> int:
    if not cfg.core_modules:
        raise Failure(EX_CONFIG, "WMT_CORE_MODULES is empty")
    patches = read_rom_patches(cfg)          # antes de tocar no driver: erro de arquivo cedo
    modprobe(cfg.core_modules)
    wmt_loader()
    wait_for_path(DEV_STPWMT, NODE_TIMEOUT, "WMT core")
    try:
        # WMT_open espera ate 2 s o fim do wmt_lib_init: e a barreira de prontidao.
        fd = os.open(DEV_STPWMT, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        raise Failure(EX_UNAVAILABLE, f"open {DEV_STPWMT}: {e.strerror}") from None
    signal.signal(signal.SIGTERM, _raise_stop)
    signal.signal(signal.SIGINT, _raise_stop)
    try:
        # ROM patches ANTES do HIF: o SET_STP_MODE pode agendar o power-on adiado pelo
        # desbloqueio da tela (g_late_pwr_on_for_blank, wmt_dev.c:915-921), e esse
        # power-on baixa os patches. O registro e um store direto.
        register_rom_patches(fd, patches)
        try:
            # NAO e sincrono: enfileira a operacao HIF_CONF (timeoutValue=0, wmt_dev.c:903)
            # e volta. A ordem em relacao aos power-ons seguintes e da fila FIFO de
            # operacoes do driver, nao deste retorno.
            fcntl.ioctl(fd, WMT_IOCTL_SET_STP_MODE, cfg.hif)
        except OSError as e:
            raise Failure(EX_PROTOCOL, f"SET_STP_MODE 0x{cfg.hif:02x}: {e.strerror}") from None
        # READY=1 = os ioctls voltaram ok; nao prova que o HIF ja foi aplicado.
        status = (f"launcher ready: HIF 0x{cfg.hif:02x} queued, "
                  f"{len(patches)} ROM patches ({', '.join(WMTDRV_TYPES[p.type] for p in patches)})")
        log(LOG_INFO, status)
        announce_ready(status)
        # Residente com o no aberto: com o ultimo fd fechado o driver poe gWmtClose=1
        # (wmt_dev.c:1538), e ai um timeout de RX do chip nunca falha (a thread mtk_wmtd
        # fica em laco, wmt_ctrl.c:310) e o assert de firmware e suprimido
        # (wmt_ctrl.c:1193). Aberto, o driver se recupera sozinho (chip reset).
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        empty = 0
        while True:
            for _fd, events in poller.poll():
                if not events & select.POLLIN:
                    continue
                raw = os.read(fd, 256)
                command = raw.decode(errors="replace").strip("\0").strip()
                if not command:
                    empty += 1
                    if empty >= 100:
                        raise Failure(EX_PROTOCOL, "driver keeps signalling a request but "
                                                   "returns nothing to read")
                    continue
                empty = 0
                answer(fd, command, patches)
    except _Stop:
        log(LOG_INFO, "launcher stopping; the WMT driver now treats the link as closing")
        return EX_OK
    finally:
        os.close(fd)


# --- Wi-Fi ----------------------------------------------------------------------------
def find_partition(label: str, sys_block: str = "/sys/class/block",
                   dev_dir: str = "/dev") -> str:
    """No de bloco da particao pelo PARTNAME do uevent no sysfs. O kernel cria o sysfs e o
    no do devtmpfs no probe do disco: nao depende do udev ter feito o link by-partlabel."""
    try:
        entries = sorted(os.listdir(sys_block))
    except OSError as e:
        raise NvramError(f"cannot list {sys_block}: {e.strerror}") from None
    found = []
    for entry in entries:
        try:
            with open(f"{sys_block}/{entry}/uevent", encoding="utf-8") as f:
                fields = dict(line.partition("=")[::2] for line in f.read().splitlines())
        except OSError:
            continue
        if fields.get("PARTNAME") == label:
            found.append(f"{dev_dir}/{fields.get('DEVNAME') or entry}")
    if not found:
        raise NvramError(f"no partition with PARTNAME={label} in {sys_block}")
    if len(found) > 1:
        raise NvramError(f"PARTNAME={label} is ambiguous ({len(found)} partitions)")
    return found[0]


def read_partition_file(spec: tuple[str, str], sys_block: str = "/sys/class/block",
                        dev_dir: str = "/dev") -> bytes:
    """Le o arquivo do ext4 da particao sem montar (debugfs abre so leitura sem -w, e
    aqui nunca se passa -w): nada de mount, de replay de journal nem de copia da
    calibracao em disco."""
    label, path = spec
    dev = find_partition(label, sys_block, dev_dir)
    try:
        wait_for_path(dev, NODE_TIMEOUT, f"partition {label}")
    except Failure as e:
        raise NvramError(str(e)) from None
    try:
        r = subprocess.run(["debugfs", "-R", f"cat {path}", dev],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NvramError(f"debugfs: {e}") from None
    if r.returncode != 0 or not r.stdout:
        err = r.stderr.decode(errors="replace").strip().splitlines()
        raise NvramError(f"cannot read {label}:{path} ({err[-1] if err else 'empty output'})")
    return r.stdout


def read_nvram(spec: tuple[str, str], sys_block: str = "/sys/class/block",
               dev_dir: str = "/dev") -> bytes:
    """NVRAM do Wi-Fi: o arquivo da particao, validado."""
    data = read_partition_file(spec, sys_block, dev_dir)
    validate_nvram(data)
    return data


def push_nvram(data: bytes) -> None:
    buf = nvram_write_buffer(data)
    try:
        fd = os.open(DEV_WIFI, os.O_WRONLY | os.O_CLOEXEC)
    except OSError as e:
        raise NvramError(f"open {DEV_WIFI}: {e.strerror or e}") from None
    # O driver espera o handler do gen4m ate 200 s segurando o mutex; o log diz onde parou.
    log(LOG_INFO, f"pushing the factory NVRAM ({len(data)} bytes) to the driver")
    try:
        n = os.write(fd, buf)
    except OSError as e:
        raise NvramError(f"driver refused the NVRAM: {e.strerror or e}") from None
    finally:
        os.close(fd)
    if n != len(buf):
        raise NvramError(f"short write ({n} of {len(buf)} bytes)")


def _write_wifi(value: bytes) -> None:
    fd = os.open(DEV_WIFI, os.O_WRONLY | os.O_CLOEXEC)
    try:
        # Bloqueia ate o fim do func_on/func_off (o probe do gen4m roda aqui dentro);
        # sucesso devolve o tamanho escrito, falha devolve EIO.
        os.write(fd, value)
    finally:
        os.close(fd)


def wifi_power(on: bool, retry_delay: float = EIO_RETRY_DELAY) -> None:
    word = "on" if on else "off"
    try:
        _write_wifi(b"1" if on else b"0")
        return
    except OSError as e:
        first = e
    if on and first.errno == errno.EIO:
        # Chip reset inteiro em andamento: o driver recusa operacoes ate terminar.
        log(LOG_WARNING, f"Wi-Fi power on refused (EIO: the driver returns it for any func_on failure, a chip reset in progress among them); "
                         f"retrying once in {retry_delay:g} s")
        time.sleep(retry_delay)
        try:
            _write_wifi(b"1")
            return
        except OSError as e:
            first = e
    raise Failure(EX_IOERR, f"Wi-Fi power {word} failed: {first.strerror} "
                            "(see the kernel log, [MTK-WIFI]/[WMT-*])")


def verify_nvram(iface: str, expected: bytes) -> None:
    try:
        perm = permanent_address(iface)
    except OSError as e:
        log(LOG_NOTICE, f"{VERDICT}unknown (cannot read the permanent address of {iface}: "
                        f"{e.strerror})")
        return
    prio, message = nvram_outcome(expected, perm)
    log(prio, message)


def cmd_wifi_on(cfg: Config) -> int:
    if not cfg.wifi_modules or not cfg.wifi_interface:
        raise Failure(EX_CONFIG, "WMT_WIFI_MODULES and WMT_WIFI_INTERFACE must be set")
    # O no /dev/stpwmt existe desde o DO_MODULE_INIT; o marcador so depois do launcher
    # registrar HIF e ROM patches. Sem ele o power-on morreria com um EIO generico.
    if not os.path.exists(READY_MARK):
        raise Failure(EX_UNAVAILABLE, "thunder-wmt-connsys is not ready "
                                      f"({READY_MARK} absent): see its journal")
    modprobe(cfg.wifi_modules)
    wait_for_path(DEV_WIFI, NODE_TIMEOUT, "wmt_chrdev_wifi")
    sysfs = f"/sys/class/net/{cfg.wifi_interface}"
    expected = None
    registered = os.path.exists(sysfs)
    if registered:
        # O gen4m registra o netdev no 1o probe e o mantem entre power-off/on (so fica
        # dormant): a interface existir nao quer dizer radio ligado. A NVRAM e enviada
        # sempre (fica no driver para o proximo probe) e o veredito sai sempre: ele le o
        # estado atual da interface.
        log(LOG_INFO, f"{cfg.wifi_interface} already registered (kept across power cycles)")
    if cfg.wifi_nvram is None:
        log(LOG_NOTICE, f"{VERDICT}not applied (disabled: WMT_WIFI_NVRAM is empty); generated "
                        "MAC, no factory RF calibration")
    else:
        try:
            data = read_nvram(cfg.wifi_nvram)
            # Depois do modprobe (o handler nasce no load do gen4m) e antes do 1o
            # power-on: a ordem do init.wlan_drv.rc + wlan_assistant do Android.
            push_nvram(data)
            expected = nvram_mac(data)
            log(LOG_INFO, f"factory NVRAM pushed: {len(data)} bytes, "
                          f"{mac_kind(expected)} MAC")
        except NvramError as e:
            log(LOG_WARNING, f"{VERDICT}not applied (not pushed: {e}); Wi-Fi comes up with a "
                             "generated MAC and without the factory RF calibration")
    start = time.monotonic()
    wifi_power(True)
    try:
        wait_for_path(sysfs, IFACE_TIMEOUT, "gen4m probe")
    except Failure as e:
        log(LOG_ERR, str(e))
        try:
            wifi_power(False)       # falha = radio desligado, nunca meio ligado
        except Failure as off:
            log(LOG_ERR, str(off))
        raise Failure(EX_IOERR, f"Wi-Fi powered on but {cfg.wifi_interface} never appeared") from None
    log(LOG_INFO, f"Wi-Fi on: {cfg.wifi_interface} present "
                  f"{(time.monotonic() - start) * 1000:.0f} ms after the power-on request")
    if registered:
        # Religamento: o wifi-off derrubou o link, e o NetworkManager nao levanta de novo
        # uma interface que outro derrubou. No 1o probe ela nasce e o NM a levanta.
        interface_up(cfg.wifi_interface)
    if expected is not None:
        verify_nvram(cfg.wifi_interface, expected)
    return EX_OK


def wireless_interfaces(sys_net: str = SYS_NET) -> list[str]:
    try:
        names = sorted(os.listdir(sys_net))
    except OSError:
        return []
    return [n for n in names if os.path.exists(os.path.join(sys_net, n, "wireless"))]


def _ifreq_flags(sock: socket.socket, iface: str, flags: int | None = None) -> int:
    req = IFREQ_FLAGS.pack(iface.encode(), flags or 0)
    out = fcntl.ioctl(sock.fileno(), SIOCGIFFLAGS if flags is None else SIOCSIFFLAGS, req)
    return IFREQ_FLAGS.unpack(out)[1]


def interfaces_down(ifaces: list[str]) -> None:
    # O gen4m guarda o netdev entre power cycles, mas o func_off libera o adaptador por
    # baixo dele: um "link down" depois disso (o NetworkManager, um ip link) entra no
    # wlanStop -> aisFsmStateAbort_SCAN com o adaptador solto e da oops (visto em
    # 2026-10-13, no pstore). Derrubar antes, com o radio ainda ligado, e o caminho que o
    # driver conhece. Sem ordem com o NetworkManager no desligamento, so isso cobre.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for iface in ifaces:
            try:
                flags = _ifreq_flags(sock, iface)
                if flags & IFF_UP:
                    _ifreq_flags(sock, iface, flags & ~IFF_UP)
                    log(LOG_INFO, f"{iface} down before the power-off")
            except OSError as e:
                log(LOG_WARNING, f"cannot bring {iface} down: {e.strerror}")


def interface_up(iface: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            flags = _ifreq_flags(sock, iface)
            if not flags & IFF_UP:
                _ifreq_flags(sock, iface, flags | IFF_UP)
                log(LOG_INFO, f"{iface} up again")
        except OSError as e:
            log(LOG_WARNING, f"cannot bring {iface} up: {e.strerror}")


def cmd_wifi_off(_cfg: Config | None) -> int:
    # ExecStop: tem de funcionar sem /etc/thunder/wmt.conf valido (main nao o carrega).
    if not os.path.exists(DEV_WIFI):
        log(LOG_INFO, f"{DEV_WIFI} absent; nothing to power off")
        return EX_OK
    interfaces_down(wireless_interfaces())
    wifi_power(False)
    # A interface fica (o gen4m registra o netdev uma vez e so o poe em dormant).
    log(LOG_INFO, "Wi-Fi off")
    return EX_OK


def cmd_nvram_check(cfg: Config) -> int:
    if cfg.wifi_nvram is None:
        log(LOG_NOTICE, "WMT_WIFI_NVRAM is empty: NVRAM push disabled")
        return EX_OK
    try:
        data = read_nvram(cfg.wifi_nvram)
    except NvramError as e:
        raise Failure(EX_NOINPUT, str(e)) from None
    log(LOG_INFO, f"NVRAM ok: {len(data)} bytes, {mac_kind(nvram_mac(data))} MAC")
    return EX_OK


# --- Bluetooth -------------------------------------------------------------------------
class Mgmt:
    """Canal de controle do BlueZ (mgmt) no kernel."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC,
                                  BTPROTO_HCI)
        try:
            self.sock.bind((MGMT_INDEX_NONE, HCI_CHANNEL_CONTROL))
        except OSError:
            self.sock.close()
            raise

    def close(self) -> None:
        self.sock.close()

    def command(self, opcode: int, index: int, params: bytes = b"",
                timeout: float = MGMT_TIMEOUT) -> tuple[int, bytes]:
        self.sock.send(mgmt_packet(opcode, index, params))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Failure(EX_IOERR, f"mgmt: no reply to opcode {opcode:#06x} "
                                        f"within {timeout:g} s")
            ready, _, _ = select.select([self.sock], [], [], left)
            if ready:
                reply = mgmt_reply(self.sock.recv(1024), index, opcode)
                if reply is not None:
                    return reply


def wait_controller(mgmt: Mgmt, index: int, deadline: float) -> None:
    """O hciN aparece no sysfs no hci_register_dev(), antes do setup; o mgmt so o lista
    (Index Added) depois do setup e da configuracao, e antes disso recusa os comandos."""
    while True:
        status, params = mgmt.command(MGMT_OP_READ_INDEX_LIST, MGMT_INDEX_NONE)
        if status == 0 and index in mgmt_index_list(params):
            return
        if time.monotonic() >= deadline:
            raise Failure(EX_UNAVAILABLE, f"hci{index} did not finish its setup within "
                                          f"{BT_SETUP_TIMEOUT:g} s (see the kernel log)")
        time.sleep(0.2)


def hci_is_running(index: int) -> bool:
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC, BTPROTO_HCI)
    try:
        buf = bytearray(HCI_DEV_INFO_SIZE)
        struct.pack_into("<H", buf, 0, index)
        fcntl.ioctl(s, HCIGETDEVINFO, buf)
    finally:
        s.close()
    return bool(hci_dev_info_flags(buf) & HCI_RUNNING)


def wait_down(index: int, deadline: float) -> None:
    """Depois do setup o kernel deixa o controlador ligado por 2 s (HCI_AUTO_OFF) e o mgmt
    ja o trata como desligado. Um Set Public Address nessa janela marca HCI_CONFIG, mas o
    hci_power_on() do 4.19 ve HCI_UP e so liga pelo mgmt, sem reabrir: a configuracao
    nunca roda e o indice some do mgmt ate o reboot. Espera o transporte fechar: o
    HCI_UP vem mascarado nessa janela, o HCI_RUNNING nao."""
    while hci_is_running(index):
        if time.monotonic() >= deadline:
            raise Failure(EX_UNAVAILABLE, f"hci{index} still up {BT_SETUP_TIMEOUT:g} s after "
                                          "its setup (something powered it on?)")
        time.sleep(0.2)


def wait_address(mgmt: Mgmt, index: int, bdaddr: bytes, deadline: float) -> bool:
    """Set Public Address responde antes de o kernel religar o controlador e gravar o
    endereco: confirma pelo Read Controller Information, que traz o Read_BD_ADDR. False se
    o controlador voltou ao mgmt com outro endereco (o driver nao derruba o power-on quando
    a gravacao falha); erro se ele nao voltou."""
    while True:
        status, params = mgmt.command(MGMT_OP_READ_INFO, index)
        if status == 0 and params[:6] == bdaddr:
            return True
        if status not in (0, MGMT_STATUS_INVALID_INDEX):
            raise Failure(EX_IOERR, f"hci{index}: Read Controller Information failed "
                                    f"(mgmt status {status:#04x})")
        if time.monotonic() >= deadline:
            if status == 0:
                return False
            raise Failure(EX_IOERR, f"hci{index} did not come back to mgmt within "
                                    f"{BT_SETUP_TIMEOUT:g} s after the address change "
                                    "(see the kernel log)")
        time.sleep(0.2)


def power_cycle_address(mgmt: Mgmt, index: int, bdaddr: bytes) -> bool:
    """Liga e desliga o controlador pelo mgmt: o setup() do driver roda a cada abertura e
    grava o endereco de novo, e um power-on em que a gravacao falha falha inteiro. True se,
    ligado, ele devolveu o endereco de fabrica."""
    status, _ = mgmt.command(MGMT_OP_SET_POWERED, index, b"\x01", timeout=BT_SETUP_TIMEOUT)
    if status:
        log(LOG_WARNING, f"hci{index}: power on failed (mgmt status {status:#04x})")
        return False
    try:
        status, params = mgmt.command(MGMT_OP_READ_INFO, index)
        return status == 0 and params[:6] == bdaddr
    finally:
        mgmt.command(MGMT_OP_SET_POWERED, index, b"\x00", timeout=BT_SETUP_TIMEOUT)


def cmd_bt_on(cfg: Config) -> int:
    if not cfg.bt_modules:
        raise Failure(EX_CONFIG, "WMT_BT_MODULES must be set")
    if not os.path.exists(READY_MARK):
        raise Failure(EX_UNAVAILABLE, "thunder-wmt-connsys is not ready "
                                      f"({READY_MARK} absent): see its journal")
    modprobe(cfg.bt_modules)
    deadline = time.monotonic() + HCI_TIMEOUT
    while (index := hci_index()) is None:
        if time.monotonic() >= deadline:
            raise Failure(EX_UNAVAILABLE, f"no hci device within {HCI_TIMEOUT:g} s "
                                          f"after loading {' '.join(cfg.bt_modules)}")
        time.sleep(POLL_STEP)
    if cfg.bt_addr is None:
        log(LOG_NOTICE, f"hci{index} registered; WMT_BT_ADDR is empty: it keeps the MediaTek "
                        "placeholder address, the same on every unit")
        return EX_OK
    # Sem o endereco de fabrica a unit falha, e o bluetooth.service, que depende dela, nao
    # sobe: o endereco provisorio e o mesmo em todo aparelho.
    try:
        bdaddr = bt_addr_from_nvram(read_partition_file(cfg.bt_addr))
    except NvramError as e:
        raise Failure(EX_NOINPUT, f"no factory BT address ({e}); Bluetooth stays off "
                                  "rather than use the MediaTek placeholder") from None
    mgmt = Mgmt()
    try:
        deadline = time.monotonic() + BT_SETUP_TIMEOUT
        wait_controller(mgmt, index, deadline)
        wait_down(index, deadline)
        status, _ = mgmt.command(MGMT_OP_SET_PUBLIC_ADDRESS, index, bdaddr)
        if status == MGMT_STATUS_REJECTED:
            raise Failure(EX_IOERR, f"hci{index} is powered: Set Public Address needs it off "
                                    "(this unit runs before bluetooth.service)")
        if status:
            raise Failure(EX_IOERR, f"hci{index}: Set Public Address refused (mgmt status "
                                    f"{status:#04x})")
        # O driver regrava o endereco a cada power-on (setup do HCI): vale para os
        # power-on do bluetoothd e para o reset do CONNSYS.
        ok = wait_address(mgmt, index, bdaddr, time.monotonic() + BT_SETUP_TIMEOUT)
        for attempt in range(2, BT_ADDR_ATTEMPTS + 1):
            if ok:
                break
            log(LOG_WARNING, f"hci{index}: factory address not taken; power cycle "
                             f"{attempt} of {BT_ADDR_ATTEMPTS}")
            wait_down(index, time.monotonic() + BT_SETUP_TIMEOUT)
            ok = power_cycle_address(mgmt, index, bdaddr)
        if not ok:
            raise Failure(EX_IOERR, f"hci{index}: the controller did not take the factory "
                                    f"address after {BT_ADDR_ATTEMPTS} attempts (vendor "
                                    "command 0xfc1a, see the kernel log)")
    finally:
        mgmt.close()
    # Sem o endereco no log: o journal pode ir para relatorio.
    log(LOG_INFO, f"hci{index}: factory address set and read back")
    return EX_OK


def radio_serialized(cmd):
    """wifi-on e wifi-off um de cada vez, tambem fora do systemd (que ja serializa os jobs
    da unit): um wifi-on que levante o link entre o link down e o func_off de um wifi-off
    poria um scan no adaptador que o func_off libera, o oops que o wifi-off evita. O lock
    e no proprio script, so leitura: funciona sob ProtectSystem=strict. O script e 0700
    (PKGBUILD): flock nao olha o modo de abertura, entao um arquivo legivel por outro uid
    deixaria esse uid segurar o lock e travar o radio. A espera tem prazo (RADIO_WAIT),
    abaixo do TimeoutStopSec da unit, para falhar com codigo claro em vez de ser morta."""
    def run(cfg):
        with open(RADIO_LOCK, "rb") as f:
            deadline = time.monotonic() + RADIO_WAIT
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        log(LOG_ERR, f"radio lock busy for {RADIO_WAIT:g} s; giving up")
                        return EX_UNAVAILABLE
                    time.sleep(0.2)
            return cmd(cfg)
    return run


RADIO_LOCK = os.path.realpath(__file__)
# Abaixo do TimeoutStopSec=30 da thunder-wmt-wifi.
RADIO_WAIT = 25.0

COMMANDS = {
    "connsys": cmd_connsys,
    "wifi-on": radio_serialized(cmd_wifi_on),
    "wifi-off": radio_serialized(cmd_wifi_off),
    "nvram-check": cmd_nvram_check,
    "bt-on": cmd_bt_on,
}


# Comandos que nao dependem da configuracao (wifi-off e o ExecStop da unit).
NO_CONFIG = frozenset({"wifi-off"})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="thunder-wmt", description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--config", default=os.environ.get("THUNDER_WMT_CONF", CONFIG_PATH))
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return EX_USAGE if e.code else EX_OK
    try:
        cfg = None if args.command in NO_CONFIG else load_config(args.config)
        return COMMANDS[args.command](cfg)
    except Failure as e:
        log(LOG_ERR, str(e))
        return e.code


if __name__ == "__main__":
    sys.exit(main())
