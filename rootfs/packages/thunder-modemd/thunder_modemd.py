#!/usr/bin/env python3
"""thunder-modemd: dono do MD1 (modem) do MT6833 sobre o eccci do kernel 4.19 da Xiaomi.

Faz o papel de dois servicos do Android:

  ccci_mdinit   partida e supervisao do MD pelo /dev/ccci_monitor: cabecalhos CCB,
                SET_BOOT_DATA, DO_START_MD (de uma thread com comm "ccci_mdinit"), espera
                o READY, hora para o MD; depois atende as mensagens do monitor (reset,
                stop/start, exception) e os eventos do /dev/ccci_md1_sta. Neste ROM o
                ccci_mdinit tambem embute o servidor FS ("ccci_fsd") do /dev/ccci_fs: o
                MD guarda a NVRAM dele (IMEI, calibracao de RF, LIDs) por esse protocolo.
                Servimos uma COPIA gravavel em MODEM_NV_ROOT, nunca as particoes.
  ccci_rpcd     os dois opcodes RPC que o kernel manda ao userspace (/dev/ccci_rpc):
                propriedade do sistema e indice da tabela de SAR

Nao abre o /dev/ttyC0 (canal AT): quem fala AT e o ModemManager ou um teste, depois do
READY. Com o ttyC0 aberto na partida o kernel segura o boot ate 30 s (SPEC 1.4).

Comandos:

  thunder-modemd run      o servico (Type=notify; SIGTERM para o MD e sai)
  thunder-modemd nv-seed SRC_NVDATA SRC_PROTECT1 SRC_PROTECT2 [SRC_NVCFG]
                          copia as pastas do modem (md/, md_cmn/, mdota/) para
                          MODEM_NV_ROOT; recusa se o destino nao estiver vazio
  thunder-modemd state    mostra /sys/kernel/ccci/boot e GET_MD_STATE (nao inicia nada)

Configuracao: /etc/thunder/modem.conf (KEY=valor; arquivo ausente = padroes):
MODEM_NV_ROOT, MODEM_RO_ROOT, MODEM_RAT, MODEM_BOOT_TIMEOUT, MODEM_PROPS_FILE (nome=valor
por linha, padrao /etc/thunder/modem-props.conf) e MODEM_PROP_<propriedade>=valor, que
respondem o RPC 0x400F por cima da tabela embutida. Log no journal (stderr com prefixo de
prioridade <N>). Codigos de saida do sysexits.h.

Captura opcional: THUNDER_MODEMD_CAPTURE=/caminho grava os pacotes crus de FS e RPC.
ATENCAO: a captura contem IMEI e calibracao de RF do aparelho; fica local, nunca publicar.
Limite por THUNDER_MODEMD_CAPTURE_MAX (bytes, padrao 32 MiB). Sob a unit
(ProtectSystem=strict) so /var/lib/thunder-modem e gravavel. Registro: b"TMDC", u8 tipo
(0 fs rx, 1 fs tx, 2 rpc rx, 3 rpc tx), 3 bytes 0, f64 time.time(), u32 tamanho, dados.

Fonte dos fatos: build/xiaomi-thunder/modem-research/SPEC.md (kernel e ROM citados la por
arquivo:linha e endereco). Partes adaptadas de github.com/MT6895-Mainline/mtk-ccci-userspace
(mdinit.py e qqc_rpc_sar.py, GPL-2.0-or-later), marcadas "adaptado do MT6895".
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import fnmatch
import mmap
import os
import queue
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field

# --- codigos de saida (sysexits.h) ------------------------------------------------
EX_OK = 0
EX_USAGE = 64        # linha de comando
EX_NOINPUT = 66      # copia da NVRAM ausente (run) ou origem ausente (nv-seed)
EX_UNAVAILABLE = 69  # no do eccci ausente/ocupado, MD1 desligado, o MD nao chegou a READY
EX_SOFTWARE = 70     # bug nosso (comm da thread de partida errado, thread presa)
EX_CANTCREAT = 73    # nv-seed: destino nao vazio ou copia falhou
EX_TEMPFAIL = 75     # o MD nao voltou a GATED a tempo de uma nova partida
EX_PROTOCOL = 76     # o kernel recusou um ioctl da partida (CCB, SET_BOOT_DATA)
EX_CONFIG = 78       # /etc/thunder/modem.conf invalido

CONFIG_PATH = "/etc/thunder/modem.conf"
DEFAULT_PROPS_FILE = "/etc/thunder/modem-props.conf"
DEFAULT_NV_ROOT = "/var/lib/thunder-modem/nv"
DEFAULT_RO_ROOT = "/usr/share/thunder-modem/vendor"
# RSC do thunder global (V:etc/rsc/thundergl/ro.prop): mtk_protocol1_rat_config. O
# build.prop generico traz N/C/Lf/Lt/W/G (variante CN); a imagem e NLWCG (SPEC 2.1).
DEFAULT_RAT = "N/Lf/Lt/W/G"
DEFAULT_BOOT_TIMEOUT = 40   # o kernel desiste em 30 s sem HS2 e nao avisa por evento
STOP_TIMEOUT = 20.0
START_THREAD_TIMEOUT = 10.0  # DO_START_MD so enfileira o comando: volta na hora

# --- nos do eccci (SPEC 1.1) --------------------------------------------------------
DEV_FS = "/dev/ccci_fs"
DEV_RPC = "/dev/ccci_rpc"
DEV_MONITOR = "/dev/ccci_monitor"
DEV_MD1_STA = "/dev/ccci_md1_sta"
DEV_CCB = "/dev/ccci_ccb_ctrl"
DEV_IPC_TIME = "/dev/ccci_ipc_5"
DEV_IOCTL = tuple(f"/dev/ccci_ioctl{i}" for i in range(5))
SYS_BOOT = "/sys/kernel/ccci/boot"
SYS_KCFG = "/sys/kernel/ccci/kcfg_setting"
SYS_MDEE = "/sys/kernel/md/mdee"


# --- ioctls (magic 'C', K:eccci/inc/ccci_core.h; SPEC 1.2) --------------------------
def _ioc(direction: int, magic: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (magic << 8) | nr


_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2
_C = ord("C")
CCCI_IOC_GET_MD_STATE = _ioc(_IOC_READ, _C, 1, 4)
CCCI_IOC_GET_MD_INFO = _ioc(_IOC_READ, _C, 8, 4)
CCCI_IOC_GET_MD_EX_TYPE = _ioc(_IOC_READ, _C, 9, 4)
CCCI_IOC_DO_STOP_MD = _ioc(_IOC_NONE, _C, 12, 0)     # _IO, mas o kernel le *(u32 *)arg
CCCI_IOC_DO_START_MD = _ioc(_IOC_NONE, _C, 13, 0)
CCCI_IOC_SEND_BATTERY_INFO = _ioc(_IOC_NONE, _C, 21, 0)
CCCI_IOC_GET_MD_TYPE = _ioc(_IOC_READ, _C, 31, 4)
# UPDATE_SIM_SLOT_CFG: u32[4] {need_update, sim_mode, slot1, slot2} -> CFG_UPDATE ao FSM
# (fsm_md_data_ioctl). O MT6895 manda {1,0,0,0} antes do start; no aparelho, com ele, o
# MD chegou a HS1 em ~0,2 s e READY em ~3,3 s. Inofensivo: so repassa os modos atuais.
CCCI_IOC_UPDATE_SIM_SLOT_CFG = _ioc(_IOC_WRITE, _C, 38, 4)
SIM_SLOT_CFG = (1, 0, 0, 0)
CCCI_IOC_SET_BOOT_DATA = _ioc(_IOC_WRITE, _C, 47, 64)
CCCI_IOC_GET_CCB_CONFIG_LENGTH = _ioc(_IOC_READ, _C, 63, 4)
CCCI_IOC_GET_CCB_CONFIG = _ioc(_IOC_READ | _IOC_WRITE, _C, 64, 24)
CCCI_IOC_CCB_CTRL_INFO = _ioc(_IOC_READ | _IOC_WRITE, _C, 71, 16)
_P = ord("P")
CCCI_IPC_UPDATE_TIME = _ioc(_IOC_NONE, _P, 4, 0)       # arg = fuso (inteiro, nao ponteiro)
CCCI_IPC_UPDATE_TIMEZONE = _ioc(_IOC_NONE, _P, 6, 0)
MD_TIME_TZ = 0                                         # [INF] SPEC 2.3: UTC

# GET_MD_STATE: estado "de usuario" (ccci_fsm.c:945-970). 0 inclui GATED e WAITING_TO_STOP.
MD_USER_INVALID, MD_USER_BOOTING, MD_USER_READY, MD_USER_EXCEPTION = 0, 1, 2, 3
USER_STATE_NAMES = {0: "INVALID/GATED", 1: "BOOTING", 2: "READY", 3: "EXCEPTION"}
# /sys/kernel/ccci/boot: "md1:<enum MD_STATE>" (mtk_ccci_common.h:451-460), mais fino.
SYS_STATE_NAMES = {0: "INVALID", 1: "GATED", 2: "BOOT_WAITING_FOR_HS1",
                   3: "BOOT_WAITING_FOR_HS2", 4: "READY", 5: "EXCEPTION", 6: "RESET",
                   7: "WAITING_TO_STOP"}
SYS_INVALID, SYS_GATED, SYS_READY, SYS_EXCEPTION = 0, 1, 4, 5

# --- mensagens do monitor (ccci_fsm_internal.h:87-99; SPEC 1.6) ---------------------
MON_FORCE_STOP_REQUEST = 0xFAF50001
MON_FLIGHT_STOP_REQUEST = 0xFAF50002
MON_FORCE_START_REQUEST = 0xFAF50003
MON_FLIGHT_START_REQUEST = 0xFAF50004
MON_RESET_REQUEST = 0xFAF50005
MON_EXCEPTION = 0xFAF50006
MON_SEND_BATTERY_INFO = 0xFAF50007
MON_STORE_NVRAM_MD_TYPE = 0xFAF50008
MON_CFG_UPDATE = 0xFAF50009
MON_RANDOM_PATTERN = 0xFAF5000A
MONITOR_NAMES = {
    MON_FORCE_STOP_REQUEST: "FORCE_STOP_REQUEST", MON_FLIGHT_STOP_REQUEST: "FLIGHT_STOP_REQUEST",
    MON_FORCE_START_REQUEST: "FORCE_START_REQUEST",
    MON_FLIGHT_START_REQUEST: "FLIGHT_START_REQUEST", MON_RESET_REQUEST: "RESET_REQUEST",
    MON_EXCEPTION: "EXCEPTION", MON_SEND_BATTERY_INFO: "SEND_BATTERY_INFO",
    MON_STORE_NVRAM_MD_TYPE: "STORE_NVRAM_MD_TYPE", MON_CFG_UPDATE: "CFG_UPDATE",
    MON_RANDOM_PATTERN: "RANDOM_PATTERN",
}
CCCI_MAGIC_NUM = 0xFFFFFFFF
CCCI_MONITOR_CH_ID = 0xF0000000
MONITOR_MSG = struct.Struct("<IIII")       # struct ccci_header: data[0], msg, canal, param

# --- eventos do /dev/ccci_md1_sta (ccci_core.h:585-600; SPEC 1.5) -------------------
EV_HS1, EV_READY, EV_EXCEPTION, EV_STOP = 9, 10, 11, 12
EVENT_NAMES = {1: "RESET_REQUEST", 2: "F_ASSERT_REQUEST", 3: "STOP_REQUEST",
               4: "START_REQUEST", 5: "ENTER_FLIGHT", 6: "LEAVE_FLIGHT",
               7: "ENTER_FLIGHT_E", 8: "LEAVE_FLIGHT_E", 9: "HS1", 10: "READY",
               11: "EXCEPTION", 12: "STOP", 13: "RILD_POWEROFF_START"}
# struct md_status_event no arm64: struct timeval (16), int md_id, int event_type,
# char reason[32] (ccci_util_broadcast.c:29-34). O kernel copia 56 bytes sem olhar o
# tamanho do buffer (caminho nao compat): ler sempre com folga.
STA_EVENT = struct.Struct("<qqii32s")
STA_READ_SIZE = 64

# --- SET_BOOT_DATA (u32[16], enum MD_CFG_* de K:eccci/inc/ccci_modem.h; SPEC 2.1) ---
MD_CFG_MDLOG_MODE, MD_CFG_SBP_CODE, MD_CFG_DUMP_FLAG, MD_CFG_SBP_SUB_ID = 0, 1, 2, 3
MD_CFG_RAT_CHK_FLAG, MD_CFG_RAT_STR0, MD_CFG_WM_IDX = 4, 5, 11
MD_DBG_DUMP_INVALID = 0xFFFFFFFF
RAT_STR_BYTES = 24              # RAT_STR0..5; o kernel forca o ultimo byte a 0

# --- CCB (/dev/ccci_ccb_ctrl; SPEC 2.2, libccci_util ccci_ccb_init_users) -----------
CCB_CONFIG = struct.Struct("<IB3xIIII")    # user_id, core_id, dl_page, ul_page, dl_buff, ul_buff
CCB_CTRL_INFO = struct.Struct("<IIII")     # user_id, ctrl_offset, ctrl_addr, ctrl_length
CCB_HEADER_SIZE = 0x40
CCB_GUARD_BAND = 0xEEFF0011
CCB_CTRL_USERS = (0, 1, 2)
CCB_MAX_CONFIGS = 64            # o kernel tem 19; acima disso o valor e lixo

# --- prctl (comm da thread que chama DO_START_MD; fsm_ioctl.c:507-519) --------------
PR_SET_NAME = 15
MDINIT_COMM = "ccci_mdinit"


class Failure(Exception):
    """Erro com codigo de saida e mensagem para o journal."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConfigError(ValueError):
    pass


class StopRequested(Exception):
    """SIGTERM/SIGINT durante uma espera: sair pelo caminho de parada limpa."""


# --- log ----------------------------------------------------------------------------
LOG_ERR, LOG_WARNING, LOG_NOTICE, LOG_INFO, LOG_DEBUG = 3, 4, 5, 6, 7
_log_lock = threading.Lock()


def log(prio: int, message: str) -> None:
    # Com stderr no journal (JOURNAL_STREAM), o prefixo <N> vira a prioridade da linha.
    # Varias threads logam: o lock evita linhas misturadas. Nunca chamar de signal handler.
    prefix = f"<{prio}>" if "JOURNAL_STREAM" in os.environ else ""
    with _log_lock:
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
CONFIG_KEYS = ("MODEM_NV_ROOT", "MODEM_RO_ROOT", "MODEM_RAT", "MODEM_BOOT_TIMEOUT",
               "MODEM_PROPS_FILE")
# MODEM_PROP_<propriedade>=valor: resposta do RPC 0x400F para essa propriedade.
PROP_PREFIX = "MODEM_PROP_"
_PROP_NAME = re.compile(r"[A-Za-z0-9_.-]{1,91}\Z")
PROP_VALUE_MAX = 91             # PROP_VALUE_MAX 92 do Android, com o NUL
_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_RAT = re.compile(r"[A-Za-z]+(?:/[A-Za-z]+)*\Z")
_SAFE_PATH = re.compile(r"/[A-Za-z0-9_.+/-]+\Z")


@dataclass(frozen=True)
class Config:
    nv_root: str = DEFAULT_NV_ROOT
    ro_root: str = DEFAULT_RO_ROOT
    rat: str = DEFAULT_RAT
    boot_timeout: int = DEFAULT_BOOT_TIMEOUT
    props_file: str = DEFAULT_PROPS_FILE
    props: tuple[tuple[str, str], ...] = ()


def parse_config(text: str) -> tuple[dict[str, str], list[str]]:
    """KEY=valor por linha, '#' comenta, aspas de shell. Defensivo: linha malformada ou
    chave desconhecida vira aviso e e ignorada (nada de eval de shell)."""
    values: dict[str, str] = {}
    warnings: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        is_prop = key.startswith(PROP_PREFIX) and bool(_PROP_NAME.match(key[len(PROP_PREFIX):]))
        if not sep or not (_KEY.match(key) or is_prop):
            warnings.append(f"line {n}: ignored (expected KEY=value)")
            continue
        try:
            tokens = shlex.split(value, comments=True)
        except ValueError as e:
            warnings.append(f"line {n}: ignored ({e})")
            continue
        if len(tokens) > 1:
            warnings.append(f"line {n}: ignored ({key} takes one value, got {len(tokens)})")
            continue
        if key not in CONFIG_KEYS and not is_prop:
            warnings.append(f"line {n}: unknown key {key} ignored")
            continue
        values[key] = tokens[0] if tokens else ""
    return values, warnings


def _abs_dir(key: str, value: str) -> str:
    v = value.rstrip("/")
    if (not _SAFE_PATH.match(value) or not v or "//" in value
            or os.path.normpath(v) != v):
        raise ConfigError(f"{key}: expected a normalised absolute path, got {value!r}")
    return v


def config_from_values(values: dict[str, str]) -> Config:
    """Valor invalido de chave conhecida e erro (EX_CONFIG): servir a NVRAM de um lugar
    que ninguem pediu e pior do que nao subir."""
    def get(key: str, default: str) -> str:
        return values.get(key) or default

    nv = _abs_dir("MODEM_NV_ROOT", get("MODEM_NV_ROOT", DEFAULT_NV_ROOT))
    ro = _abs_dir("MODEM_RO_ROOT", get("MODEM_RO_ROOT", DEFAULT_RO_ROOT))
    if os.path.commonpath((nv, ro)) in (nv, ro):
        raise ConfigError("MODEM_NV_ROOT and MODEM_RO_ROOT must not contain each other")
    rat = get("MODEM_RAT", DEFAULT_RAT)
    if not _RAT.match(rat) or len(rat) > RAT_STR_BYTES - 1:
        raise ConfigError(f"MODEM_RAT: expected letters separated by '/', up to "
                          f"{RAT_STR_BYTES - 1} characters, got {rat!r}")
    raw_t = get("MODEM_BOOT_TIMEOUT", str(DEFAULT_BOOT_TIMEOUT))
    try:
        timeout = int(raw_t, 10)
    except ValueError:
        raise ConfigError(f"MODEM_BOOT_TIMEOUT: not an integer: {raw_t!r}") from None
    if not 10 <= timeout <= 300:
        raise ConfigError("MODEM_BOOT_TIMEOUT: out of range 10..300 s")
    props_file = _abs_dir("MODEM_PROPS_FILE", get("MODEM_PROPS_FILE", DEFAULT_PROPS_FILE))
    props = []
    for key, value in values.items():
        if key.startswith(PROP_PREFIX):
            props.append((key[len(PROP_PREFIX):], _prop_value(key, value)))
    return Config(nv_root=nv, ro_root=ro, rat=rat, boot_timeout=timeout,
                  props_file=props_file, props=tuple(props))


def _prop_value(where: str, value: str) -> str:
    if len(value.encode()) > PROP_VALUE_MAX or any(ord(c) < 0x20 or ord(c) > 0x7E
                                                   for c in value):
        raise ConfigError(f"{where}: property value must be printable ASCII, up to "
                          f"{PROP_VALUE_MAX} bytes")
    return value


def parse_props_file(text: str) -> tuple[dict[str, str], list[str]]:
    """nome=valor por linha (como o build.prop), '#' comenta; linha invalida e ignorada."""
    props: dict[str, str] = {}
    warnings: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not _PROP_NAME.match(name):
            warnings.append(f"line {n}: ignored (expected name=value)")
            continue
        try:
            props[name] = _prop_value(f"line {n}", value)
        except ConfigError as e:
            warnings.append(f"{e}; ignored")
    return props, warnings


def load_config(path: str) -> Config:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        log(LOG_INFO, f"{path} absent; using the defaults")
        return Config()
    except OSError as e:
        raise Failure(EX_CONFIG, f"cannot read {path}: {e.strerror}") from None
    values, warnings = parse_config(text)
    for w in warnings:
        log(LOG_WARNING, f"{path}: {w}")
    try:
        return config_from_values(values)
    except ConfigError as e:
        raise Failure(EX_CONFIG, f"{path}: {e}") from None


# --- dados de boot e CCB (funcoes puras) --------------------------------------------
def boot_data(rat: str) -> bytes:
    """u32[16] do SET_BOOT_DATA (SPEC 2.1): {0, 0, 0xFFFFFFFF, 0, 0, rat em 24 bytes,
    wm_idx 0, 0...}. wm_idx 0 e invalido para o kernel, que entao usa a string."""
    raw = rat.encode("ascii")
    if len(raw) > RAT_STR_BYTES - 1:
        raise ValueError(f"RAT string {rat!r} longer than {RAT_STR_BYTES - 1} bytes")
    words = [0] * 16
    words[MD_CFG_MDLOG_MODE] = 0         # MODE_IDLE, sem espera (build user sem mdlogger)
    words[MD_CFG_SBP_CODE] = 0           # [INF] 0 = default do MD
    words[MD_CFG_DUMP_FLAG] = MD_DBG_DUMP_INVALID
    words[MD_CFG_SBP_SUB_ID] = 0
    words[MD_CFG_RAT_CHK_FLAG] = 0       # persist.vendor.md_c2k_cap_dep_check=0
    words[MD_CFG_RAT_STR0:MD_CFG_RAT_STR0 + 6] = struct.unpack(
        "<6I", raw.ljust(RAT_STR_BYTES, b"\0"))
    words[MD_CFG_WM_IDX] = 0
    return struct.pack("<16I", *words)


def ccb_header(dl_page: int, ul_page: int, dl_buff: int, ul_buff: int) -> bytes:
    """Cabecalho de controle de 0x40 bytes de uma entrada do ccb_configs[] (SPEC 2.2).
    Adaptado do MT6895 (mdinit.py ccb_init): la o MD assertava em ccci_shm_bm.c:602 sem
    isto. O MD sobrescreve +0x00 e +0x20 com o guard band dele."""
    hdr = bytearray(CCB_HEADER_SIZE)
    struct.pack_into("<III", hdr, 0x14, dl_page, dl_buff, CCB_GUARD_BAND)
    struct.pack_into("<III", hdr, 0x34, ul_page, ul_buff, CCB_GUARD_BAND)
    return bytes(hdr)


def ccb_headers(configs: list[tuple[int, int, int, int, int, int]]) -> bytes:
    return b"".join(ccb_header(dl_page, ul_page, dl_buff, ul_buff)
                    for _uid, _core, dl_page, ul_page, dl_buff, ul_buff in configs)


# --- mensagens do kernel (funcoes puras) --------------------------------------------
@dataclass(frozen=True)
class MonitorMsg:
    msg: int
    param: int
    magic: int
    channel: int

    @property
    def name(self) -> str:
        return MONITOR_NAMES.get(self.msg, f"0x{self.msg:08X}")


def parse_monitor(data: bytes) -> MonitorMsg | None:
    if len(data) < MONITOR_MSG.size:
        return None
    magic, msg, channel, param = MONITOR_MSG.unpack_from(data)
    return MonitorMsg(msg=msg, param=param, magic=magic, channel=channel)


@dataclass(frozen=True)
class StaEvent:
    sec: int
    usec: int
    md_id: int
    event: int
    reason: str

    @property
    def name(self) -> str:
        return EVENT_NAMES.get(self.event, str(self.event))


def parse_sta_event(data: bytes) -> StaEvent | None:
    if len(data) < STA_EVENT.size:
        return None
    sec, usec, md_id, event, reason = STA_EVENT.unpack_from(data)
    return StaEvent(sec=sec, usec=usec, md_id=md_id, event=event,
                    reason=reason.split(b"\0", 1)[0].decode("ascii", "replace"))


def parse_sys_boot(text: str | None) -> int | None:
    """'md1:4' -> 4. None se ausente, vazio (MD1 desligado no kernel) ou ilegivel."""
    if not text:
        return None
    m = re.search(r"md1:(-?\d+)", text)
    return int(m.group(1)) if m else None


# --- protocolo FS (SPEC 3) ----------------------------------------------------------
CCCI_MTU = 3456                       # 3584 - 128 (ccci_common_config.h)
FS_HEADER = 16 + 4                    # ccci_header + op_id
FS_PACKET_MAX = CCCI_MTU + FS_HEADER  # 3476: acima disso o write() da -ENOMEM
FS_RX_CH, FS_TX_CH = 14, 15
FS_MORE = 0x80000000                  # CCCI_FS_REQ_SEND_AGAIN: ha mais fragmentos
FS_AP_WAKEUP = 0x40000000             # CCCI_FS_AP_CCCI_WAKEUP: marcado pelo HIF
RESP_ID = 0xFFFF0000                  # FS_API_RESP_ID / RPC_API_RESP_ID
FS_SLOTS = 5                          # FS_REQ_BUFFER_MUN do MD
FS_MAX_BLOCKS = 64
FS_MAX_MESSAGE = 1 << 20
FS_MAX_HANDLES = 128                  # tabela de 128 entradas do ROM (cmp w3,#0x80)
FS_MAX_IO = 1 << 20
FS_PATH_MAX = 4096
CCCI_HEADER = struct.Struct("<IIII")  # data[0], data[1], canal | seq<<16, reserved

FS_OK = 0
FS_PARAM_ERROR = -2
FS_DRIVE_NOT_FOUND = -4
FS_TOO_MANY_FILES = -5
FS_NO_MORE_FILES = -6
FS_FILE_NOT_FOUND = -9
FS_INVALID_FILE_HANDLE = -10
FS_ACCESS_DENIED = -16
FS_GENERAL_FAILURE = -18
FS_PATH_NOT_FOUND = -19
FS_DISK_FULL = -22
# Espaco que o modem nunca consome no sistema de arquivos da raiz: no Android os drives sao
# particoes proprias e fixas; aqui a copia do NVRAM divide o ext4 com o resto do sistema.
# f_bavail ja exclui a reserva do root, que o daemon (root) tambem nao deve gastar.
FS_MIN_FREE = 256 << 20
FS_FILE_EXISTS = -36
FS_READ_ONLY = -45
FS_PATH_OVER_LEN = -49
FS_CODE_NAMES = {
    0: "OK", -1: "FAIL", -2: "PARAM_ERROR", -4: "DRIVE_NOT_FOUND", -5: "TOO_MANY_FILES",
    -6: "NO_MORE_FILES", -9: "FILE_NOT_FOUND", -10: "INVALID_FILE_HANDLE",
    -16: "ACCESS_DENIED", -18: "GENERAL_FAILURE", -19: "PATH_NOT_FOUND", -22: "DISK_FULL",
    -36: "FILE_EXISTS", -45: "READ_ONLY", -49: "PATH_OVER_LEN",
}
# Ausencia esperada: nao conta como erro no resumo (o MD sonda LIDs que nao existem).
FS_BENIGN = frozenset({FS_NO_MORE_FILES, FS_FILE_NOT_FOUND})

# Flags do Open (ROM 0x1aa70-0x1aac8, conferido com llvm-objdump): base O_RDWR;
# 0x100 tira o bit de escrita; 0x10000 vira O_RDWR|O_CREAT; 0x20000 vira
# O_RDWR|O_CREAT|O_TRUNC (vence); bit 28 vira 0x800 (O_NONBLOCK do arm64). Modo 0660.
FS_READ_ONLY_FLAG = 0x100
FS_CREATE = 0x10000
FS_CREATE_ALWAYS = 0x20000
FS_NONBLOCK_MODE = 0x10000000
FS_CREATE_MODE = 0o660
FS_DIR_MODE = 0o770

FS_ATTR_READ_ONLY = 0x01
FS_ATTR_DIR = 0x10
DOS_ENTRY_SIZE = 52
DISK_INFO_SIZE = 84
FILE_DETAIL_SIZE = 24
# FS_Move (KAL do MD): 0x10001 visto no fio pelo MT6895 = COPY | OVERWRITE.
FS_MOVE_COPY = 0x1
FS_MOVE_KILL = 0x2
FS_MOVE_OVERWRITE = 0x10000
GET_DRIVE_SYSTEM = 4
GET_DRIVE_ANSWER = 0x5A        # ROM 0x22714: mov w22,#0x5a para FS_DRIVE_I_SYSTEM

FS_OPS = {
    0x1001: "Open", 0x1002: "Seek", 0x1003: "Read", 0x1004: "Write", 0x1005: "Close",
    0x1006: "CloseAll", 0x1007: "CreateDir", 0x1008: "RemoveDir", 0x1009: "GetFileSize",
    0x100A: "GetFolderSize", 0x100B: "Rename", 0x100C: "Move", 0x100D: "Count",
    0x100E: "GetDiskInfo", 0x100F: "Delete", 0x1010: "GetAttributes", 0x1011: "OpenHint",
    0x1012: "FindFirst", 0x1013: "FindNext", 0x1014: "FindClose", 0x1015: "LockFAT",
    0x1016: "UnlockAll", 0x1017: "ShutDown", 0x1018: "XDelete", 0x1019: "ClearDiskFlag",
    0x101A: "GetDrive", 0x101B: "GetClusterSize", 0x101C: "SetDiskFlag",
    0x101D: "OTPWrite", 0x101E: "OTPRead", 0x101F: "OTPQueryLength", 0x1020: "OTPLock",
    0x1021: "Restore", 0x1022: "CMPT_Read", 0x1023: "Bin_Region_Access",
    0x1024: "CMPT_Write", 0x1025: "GetFileDetail",
}


class FrameError(ValueError):
    def __init__(self, message: str, op: int | None = None, reserved: int = 0) -> None:
        super().__init__(message)
        self.op = op
        self.reserved = reserved


class FsError(Exception):
    """Falha de uma operacao FS com o codigo que vai ao MD."""

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(reason or FS_CODE_NAMES.get(code, str(code)))
        self.code = code
        self.reason = reason


def fs_errno(e: OSError) -> int:
    """errno -> codigo FS do servidor oficial (MT6895 0x4060b8, SPEC 3.3): so ENOENT,
    EACCES e EMFILE tem codigo proprio; o resto e -18. Nunca devolver errno cru: -22
    (EINVAL) seria lido como FS_DISK_FULL. Adaptado do MT6895 (fs_error_from_errno)."""
    return {errno.ENOENT: FS_FILE_NOT_FOUND, errno.EACCES: FS_ACCESS_DENIED,
            errno.EMFILE: FS_TOO_MANY_FILES}.get(getattr(e, "errno", None),
                                                 FS_GENERAL_FAILURE)


def encode_blocks(blocks: list[bytes]) -> bytes:
    out = bytearray()
    for b in blocks:
        out += struct.pack("<I", len(b)) + b + b"\0" * (-len(b) & 3)
    return bytes(out)


def encode_message(op_field: int, blocks: list[bytes]) -> bytes:
    """op_id | n_blocos | blocos (sem o ccci_header)."""
    return struct.pack("<II", op_field & 0xFFFFFFFF, len(blocks)) + encode_blocks(blocks)


def fragment(message: bytes, reserved: int, channel: int = FS_TX_CH,
             mtu: int = CCCI_MTU) -> list[bytes]:
    """Mensagem logica -> pacotes de ate 16 + 4 + mtu bytes. Todo fragmento repete
    [ccci_header][op_id]; todos menos o ultimo levam data[0] |= FS_MORE; `reserved`
    (slot do MD) ecoado em todos. seq_num/assert_bit vao 0: o HIF renumera no envio
    (ccci_md_inc_tx_seq_num, hif/ccci_hif_internal.h:155-166). Adaptado do MT6895
    (send_reply)."""
    op_field, body = message[:4], message[4:]
    chunks = [body[i:i + mtu] for i in range(0, len(body), mtu)] or [b""]
    packets = []
    for i, chunk in enumerate(chunks):
        d0 = FS_MORE if i < len(chunks) - 1 else 0
        packets.append(CCCI_HEADER.pack(d0, FS_HEADER + len(chunk), channel & 0xFFFF,
                                        reserved & 0xFFFFFFFF) + op_field + chunk)
    return packets


def parse_blocks(data: bytes, offset: int, count: int) -> list[bytes]:
    if count > FS_MAX_BLOCKS:
        raise ValueError(f"{count} blocks")
    blocks = []
    for _ in range(count):
        if offset + 4 > len(data):
            raise ValueError("truncated block list")
        (n,) = struct.unpack_from("<I", data, offset)
        offset += 4
        if n > len(data) - offset:
            raise ValueError(f"block of {n} bytes runs past the message")
        blocks.append(bytes(data[offset:offset + n]))
        offset += (n + 3) & ~3
    return blocks


@dataclass
class FsRequest:
    op: int
    reserved: int
    blocks: list[bytes]
    size: int
    packets: int = 1


def parse_message(data: bytes, packets: int = 1) -> FsRequest:
    """Mensagem logica remontada (header + op + n + blocos) -> FsRequest."""
    if len(data) < FS_HEADER:
        raise FrameError(f"short FS packet ({len(data)} bytes)")
    reserved = struct.unpack_from("<I", data, 12)[0]
    op = struct.unpack_from("<I", data, 16)[0]
    if len(data) < FS_HEADER + 4:
        return FsRequest(op=op, reserved=reserved, blocks=[], size=len(data), packets=packets)
    count = struct.unpack_from("<I", data, 20)[0]
    try:
        blocks = parse_blocks(data, 24, count)
    except ValueError as e:
        raise FrameError(str(e), op=op, reserved=reserved) from None
    return FsRequest(op=op, reserved=reserved, blocks=blocks, size=len(data), packets=packets)


class Reassembler:
    """Junta os fragmentos de uma requisicao do MD (data[0] bit31 = mais fragmentos;
    o bit30 vem do HIF e e mascarado). Um pendente por slot (`reserved`): fragmentos de
    slots diferentes nao se misturam. Cada continuacao repete [header 16][op 4]; so o
    corpo depois disso entra. Adaptado do MT6895 (join_fs_fragment), que viu o MD mandar
    3476 + 660 bytes para uma mensagem de 4116."""

    def __init__(self) -> None:
        self.pending: dict[int, tuple[bytearray, int]] = {}

    def reset(self) -> None:
        self.pending.clear()

    def feed(self, pkt: bytes) -> FsRequest | None:
        if len(pkt) < FS_HEADER:
            raise FrameError(f"short FS packet ({len(pkt)} bytes)")
        d0, _length, _ch, reserved = CCCI_HEADER.unpack_from(pkt)
        op = struct.unpack_from("<I", pkt, 16)[0]
        more = bool(d0 & FS_MORE)
        cur = self.pending.pop(reserved, None)
        if cur is not None and struct.unpack_from("<I", cur[0], 16)[0] != op:
            log(LOG_WARNING, f"fs: slot {reserved}: fragment of op 0x{op:04x} while "
                             "another op was pending; dropping the partial request")
            cur = None
        if cur is None:
            buf = bytearray(pkt)
            struct.pack_into("<I", buf, 0, 0)
            count = 1
        else:
            buf, count = cur
            buf += pkt[FS_HEADER:]
            count += 1
        if len(buf) > FS_MAX_MESSAGE:
            raise FrameError(f"request over {FS_MAX_MESSAGE} bytes", op=op, reserved=reserved)
        if more:
            if reserved not in self.pending and len(self.pending) >= FS_SLOTS:
                log(LOG_WARNING, f"fs: more than {FS_SLOTS} partial requests pending")
            self.pending[reserved] = (buf, count)
            return None
        return parse_message(bytes(buf), count)


def decode_md_path(raw: bytes) -> str:
    """UTF-16LE terminado em 0 -> str com '/' no lugar de '\\' (ROM 0x1a628)."""
    text = raw[:len(raw) & ~1].decode("utf-16-le", errors="replace")
    return text.split("\0", 1)[0].replace("\\", "/")


def dos_match(name: str, pattern: str) -> bool:
    """Coringa de FAT, sem diferenciar caixa: '*.*' casa tudo, inclusive nome sem ponto
    (os LIDs do NVRAM nao tem extensao); 'X*.*' casa 'XYZ'."""
    n, p = name.upper(), pattern.upper().replace("[", "[[]")
    if p in ("*", "*.*"):
        return True
    if fnmatch.fnmatchcase(n, p):
        return True
    return p.endswith(".*") and "." not in n and fnmatch.fnmatchcase(n, p[:-2])


def attributes(st: os.stat_result) -> int:
    """READ_ONLY se legivel e sem bit de escrita/execucao; DIR (ROM 0x20098, como o
    MT6895 decodificou: 0x23d3c..0x23d58)."""
    mode = st.st_mode
    a = FS_ATTR_READ_ONLY if (mode & 0o444) and not (mode & 0o333) else 0
    return a | (FS_ATTR_DIR if stat.S_ISDIR(mode) else 0)


def dos_datetime(t: float) -> int:
    """Data/hora DOS (FAT) em UTC: ano-1980 << 25 | mes << 21 | dia << 16 | h << 11 |
    min << 5 | seg/2."""
    tm = time.gmtime(max(t, 0))
    year = min(max(tm.tm_year, 1980), 2107)
    return (((year - 1980) << 25) | (tm.tm_mon << 21) | (tm.tm_mday << 16)
            | (tm.tm_hour << 11) | (tm.tm_min << 5) | (tm.tm_sec // 2))


def dir_entry(st: os.stat_result, name: str, cap_chars: int) -> bytes:
    """FS_DOSDirEntry de 52 bytes como o MT6895 preenche (mdinit.py _entry52): atributos
    @11, @12 = 1 se o nome cabe, data/hora DOS @14 e @22, tamanho @28. FileName/Extension
    8.3 ficam zerados (o nome vai no bloco UTF-16). Diretorio com tamanho 0, como no FAT."""
    b = bytearray(DOS_ENTRY_SIZE)
    b[11] = attributes(st)
    b[12] = 1 if len(name) < cap_chars else 0
    stamp = dos_datetime(st.st_mtime)
    struct.pack_into("<I", b, 14, stamp)
    struct.pack_into("<I", b, 22, stamp)
    size = 0 if stat.S_ISDIR(st.st_mode) else st.st_size
    struct.pack_into("<I", b, 28, size & 0xFFFFFFFF)
    return bytes(b)


def name_block(name: str, cap_chars: int) -> bytes:
    """Nome em UTF-16LE com NUL, cortado em cap-1 caracteres (MT6895 _name_blk)."""
    return name[:max(cap_chars - 1, 0)].encode("utf-16-le", "replace") + b"\0\0"


def _i32(v: int) -> bytes:
    return struct.pack("<i", max(-0x80000000, min(v, 0x7FFFFFFF)))


def _u32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def _apparent_size(path: str) -> int:
    """Tamanho aparente (com buracos) de um arquivo, ou a soma dos arquivos de uma arvore."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    return sum(os.lstat(os.path.join(d, f)).st_size for d, _, fs in os.walk(path) for f in fs)


@dataclass(frozen=True)
class Drive:
    letter: str
    root: str | None
    writable: bool
    durable: bool = False     # fsync no close e nas mudancas de diretorio


DRIVE_ORDER = ("Z", "X", "Y", "W", "V", "U", "T", "S", "R", "Q")   # ROM 0x2d910, SPEC 3.4


def drive_table(nv_root: str, ro_root: str) -> dict[str, Drive]:
    """Tabela de drives do ROM MT6833 (SPEC 3.4) sobre a copia gravavel e as raizes so
    leitura. Raiz ausente = PATH_NOT_FOUND (-19) para tudo naquele drive."""
    nv, ro = nv_root.rstrip("/"), ro_root.rstrip("/")
    return {
        "Z": Drive("Z", f"{nv}/nvdata/md", True, True),      # /mnt/vendor/nvdata/md
        "X": Drive("X", f"{nv}/protect1/md", True, True),    # /mnt/vendor/protect_f/md
        "Y": Drive("Y", f"{nv}/protect2/md", True, True),    # /mnt/vendor/protect_s/md
        "W": Drive("W", f"{ro}/firmware", False),            # /vendor/firmware
        "V": Drive("V", f"{nv}/nvdata/md_cmn", True, True),  # /mnt/vendor/nvdata/md_cmn
        "U": Drive("U", f"{nv}/mdlpm", True),                # /data/vendor/mdlpm
        "T": Drive("T", f"{ro}/mdota", False),               # /vendor/etc/mdota
        "S": Drive("S", f"{nv}/nvcfg", True, True),          # /mnt/vendor/nvcfg
        "R": Drive("R", f"{ro}/etc_md", False),              # /vendor/etc/md
        "Q": Drive("Q", f"{nv}/vendor_de_md", True),         # /data/vendor_de/md
    }


@dataclass
class FileHandle:
    fd: int
    path: str
    drive: Drive
    created: bool = False
    dirty: bool = False


@dataclass
class FindHandle:
    directory: str
    drive: Drive
    names: list[str]
    idx: int = 0
    last_entry: bytes = bytes(DOS_ENTRY_SIZE)


@dataclass
class _Ctx:
    """O que vai para a linha de debug de cada requisicao."""
    path: str = ""
    flags: int | None = None
    result: int = 0
    nbytes: int = 0
    note: str = ""


@dataclass
class FsStats:
    requests: int = 0
    errors: int = 0
    absent: int = 0
    read_bytes: int = 0
    written_bytes: int = 0
    per_op: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        top = sorted(self.per_op.items(), key=lambda kv: (-kv[1], kv[0]))
        ops = ", ".join(f"{k} {v}" for k, v in top) or "none"
        return (f"fs: {self.requests} requests ({ops}); {self.errors} failed, "
                f"{self.absent} not found; {self.read_bytes} B read, "
                f"{self.written_bytes} B written")


class Capture:
    """Pacotes crus de FS/RPC em arquivo limitado (ver docstring do modulo: contem IMEI
    e calibracao; local, nunca publicar)."""

    MAGIC = b"TMDC"
    RECORD = struct.Struct("<4sB3xdI")
    FS_RX, FS_TX, RPC_RX, RPC_TX = 0, 1, 2, 3

    def __init__(self, path: str, limit: int) -> None:
        self.path, self.limit = path, limit
        self.lock = threading.Lock()
        self.fd: int | None = None
        self.size = 0
        self.full = False

    @classmethod
    def from_env(cls) -> "Capture | None":
        path = os.environ.get("THUNDER_MODEMD_CAPTURE")
        if not path:
            return None
        try:
            limit = int(os.environ.get("THUNDER_MODEMD_CAPTURE_MAX", str(32 << 20)), 10)
        except ValueError:
            limit = 32 << 20
        log(LOG_WARNING, f"capturing raw FS/RPC packets to {path} (up to {limit} B): the "
                         "file holds the IMEI and RF calibration, keep it local")
        return cls(path, limit)

    def record(self, kind: int, data: bytes) -> None:
        with self.lock:
            if self.full:
                return
            rec = self.RECORD.pack(self.MAGIC, kind, time.time(), len(data)) + data
            if self.size + len(rec) > self.limit:
                self.full = True
                log(LOG_NOTICE, f"capture {self.path} reached {self.limit} B; stopped")
                return
            try:
                if self.fd is None:
                    self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                                      | os.O_CLOEXEC, 0o600)
                    self.size = os.fstat(self.fd).st_size
                os.write(self.fd, rec)
                self.size += len(rec)
            except OSError as e:
                self.full = True
                log(LOG_WARNING, f"capture {self.path}: {e.strerror}; stopped")

    def close(self) -> None:
        with self.lock:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def open_flags(fs_flags: int) -> int:
    flags = os.O_RDONLY if fs_flags & FS_READ_ONLY_FLAG else os.O_RDWR
    if fs_flags & FS_CREATE:
        flags = os.O_RDWR | os.O_CREAT
    if fs_flags & FS_CREATE_ALWAYS:
        flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC
    if fs_flags & FS_NONBLOCK_MODE:
        flags |= os.O_NONBLOCK
    return flags


class FsServer:
    """Servidor CCCI FS. process_packet() recebe um pacote do MD e devolve os pacotes da
    resposta (vazio enquanto a requisicao chega em fragmentos). Uma thread so atende;
    o lock serve para o close_all() da thread principal na parada/reset do MD."""

    def __init__(self, drives: dict[str, Drive], capture: Capture | None = None) -> None:
        self.drives = drives
        self.capture = capture
        self.lock = threading.Lock()
        self.reasm = Reassembler()
        self.handles: dict[int, FileHandle | FindHandle] = {}
        self.stats = FsStats()
        self.seq = 0

    # --- transporte ---
    def process_packet(self, pkt: bytes) -> list[bytes]:
        with self.lock:
            if self.capture:
                self.capture.record(Capture.FS_RX, pkt)
            try:
                req = self.reasm.feed(pkt)
            except FrameError as e:
                log(LOG_WARNING, f"fs: malformed request ({e}): {pkt[:48].hex()}")
                if e.op is None:
                    return []
                fake = FsRequest(op=e.op, reserved=e.reserved, blocks=[], size=len(pkt))
                blocks = self.error_blocks(fake, FS_PARAM_ERROR)
                return self._sent(fragment(encode_message(e.op | RESP_ID, blocks), e.reserved))
            if req is None:
                return []
            blocks = self.handle(req)
            return self._sent(fragment(encode_message(req.op | RESP_ID, blocks), req.reserved))

    def _sent(self, packets: list[bytes]) -> list[bytes]:
        if self.capture:
            for p in packets:
                self.capture.record(Capture.FS_TX, p)
        return packets

    def handle(self, req: FsRequest) -> list[bytes]:
        """Toda requisicao tem resposta: o cliente FS do MD e sincrono e trava sem ela."""
        self.seq += 1
        name = FS_OPS.get(req.op, f"0x{req.op:04x}")
        method = getattr(self, f"op_{name.lower()}", None) if req.op in FS_OPS else None
        ctx = _Ctx()
        try:
            if method is None:
                raise FsError(FS_PARAM_ERROR, "unknown op")
            blocks = method(req.blocks, ctx)
        except FsError as e:
            ctx.result, ctx.note = e.code, e.reason or ctx.note
            blocks = self.error_blocks(req, e.code)
        except OSError as e:
            ctx.result = fs_errno(e)
            ctx.note = f"{errno.errorcode.get(e.errno or 0, e.errno)} {e.strerror or e}"
            blocks = self.error_blocks(req, ctx.result)
        except Exception as e:     # nunca deixar o MD sem resposta por bug nosso
            log(LOG_ERR, f"fs: op {name} raised {e!r}\n{traceback.format_exc()}")
            ctx.result, ctx.note = FS_PARAM_ERROR, "internal error"
            blocks = self.error_blocks(req, FS_PARAM_ERROR)
        self._account(name, req, ctx, blocks)
        return blocks

    def _account(self, name: str, req: FsRequest, ctx: _Ctx, blocks: list[bytes]) -> None:
        st = self.stats
        st.requests += 1
        st.per_op[name] = st.per_op.get(name, 0) + 1
        if ctx.result < 0:
            if ctx.result in FS_BENIGN:
                st.absent += 1
            else:
                st.errors += 1
        res = FS_CODE_NAMES.get(ctx.result, str(ctx.result)) if ctx.result < 0 else str(ctx.result)
        parts = [f"fs #{self.seq} slot={req.reserved} {name}"]
        if ctx.path:
            parts.append(f"path={ctx.path}")
        if ctx.flags is not None:
            parts.append(f"flags=0x{ctx.flags:x}")
        parts.append(f"-> {res}")
        if ctx.nbytes:
            parts.append(f"bytes={ctx.nbytes}")
        out = len(encode_blocks(blocks)) + 8
        parts.append(f"(in {req.size} B/{req.packets} pkt, out {out} B)")
        if ctx.note:
            parts.append(ctx.note)
        log(LOG_DEBUG, " ".join(parts))

    @staticmethod
    def error_blocks(req: FsRequest, code: int) -> list[bytes]:
        """Forma da resposta de erro de cada op (SPEC 3.3): mesmo numero de blocos da
        resposta de sucesso, para o MD nao ler bloco que nao veio."""
        op = req.op
        if op == 0x1003:                       # Read: status, lidos, dados
            return [_i32(code), _u32(0), b""]
        if op in (0x1004, 0x1009, 0x101F):     # Write, GetFileSize, OTPQueryLength
            return [_i32(code), _u32(0)]
        if op == 0x100E:
            return [_i32(code), bytes(DISK_INFO_SIZE)]
        if op == 0x1011:                       # OpenHint ecoa o hint
            return [_i32(code), req.blocks[2] if len(req.blocks) > 2 else b""]
        if op in (0x1012, 0x1013):
            return [_i32(code), bytes(DOS_ENTRY_SIZE), b""]
        if op == 0x1017:                       # ShutDown: 0 blocos (ROM 0x193dc)
            return []
        if op == 0x101E:
            return [_i32(code), b""]
        if op == 0x1022:
            return [struct.pack("<Ii", 0, code), _u32(0), _u32(0), b""]
        if op == 0x1024:
            return [struct.pack("<Ii", 0, code), _i32(-1), _u32(0)]
        if op == 0x1025:
            return [_i32(code), bytes(FILE_DETAIL_SIZE)]
        return [_i32(code)]

    # --- argumentos ---
    @staticmethod
    def _arg_u32(blocks: list[bytes], i: int) -> int:
        if i >= len(blocks):
            raise FsError(FS_PARAM_ERROR, f"missing block {i}")
        return struct.unpack_from("<I", blocks[i].ljust(4, b"\0"))[0]

    @staticmethod
    def _arg_i32(blocks: list[bytes], i: int) -> int:
        if i >= len(blocks):
            raise FsError(FS_PARAM_ERROR, f"missing block {i}")
        return struct.unpack_from("<i", blocks[i].ljust(4, b"\0"))[0]

    @staticmethod
    def _arg_u8(blocks: list[bytes], i: int) -> int:
        if i >= len(blocks):
            raise FsError(FS_PARAM_ERROR, f"missing block {i}")
        return blocks[i][0] if blocks[i] else 0

    def _arg_path(self, blocks: list[bytes], i: int, ctx: _Ctx) -> str:
        if i >= len(blocks):
            raise FsError(FS_PARAM_ERROR, f"missing block {i}")
        p = decode_md_path(blocks[i])
        ctx.path = f"{ctx.path} -> {p}" if ctx.path else p
        return p

    # --- caminhos ---
    def resolve(self, md_path: str) -> tuple[Drive, str]:
        """Caminho do MD ('Z:/NVRAM/X') -> (drive, caminho real no host). Recusa caminho
        relativo (ROM 0x1a1b8), drive sem raiz, '..', e qualquer coisa que, resolvida
        com realpath (symlinks inclusive), saia da raiz do drive."""
        if len(md_path) > FS_PATH_MAX:
            raise FsError(FS_PATH_OVER_LEN)
        if len(md_path) < 2 or md_path[1] != ":":
            raise FsError(FS_PATH_NOT_FOUND, "relative path refused")
        drive = self.drives.get(md_path[0].upper())
        if drive is None:
            raise FsError(FS_DRIVE_NOT_FOUND, f"no drive {md_path[0]!r}")
        if drive.root is None or not os.path.isdir(drive.root):
            raise FsError(FS_PATH_NOT_FOUND, f"drive {drive.letter}: has no root")
        rest = md_path[2:]
        if rest and not rest.startswith("/"):
            raise FsError(FS_PATH_NOT_FOUND, "drive-relative path refused")
        parts = [p for p in rest.split("/") if p and p != "."]
        for p in parts:
            if p == ".." or any(ord(c) < 0x20 for c in p):
                raise FsError(FS_PATH_NOT_FOUND, "invalid path component")
        host = "/".join([drive.root, *parts])
        if len(os.fsencode(host)) >= FS_PATH_MAX:
            raise FsError(FS_PATH_OVER_LEN)
        root = os.path.realpath(drive.root)
        real = os.path.realpath(host)
        if real != root and not real.startswith(root + "/"):
            raise FsError(FS_PATH_NOT_FOUND, "path escapes its drive")
        return drive, real

    @staticmethod
    def _need_writable(drive: Drive, host: str, creating: bool = False) -> None:
        """Drive so leitura responde como o Android num /vendor montado ro: ENOENT se o
        alvo nao existe (e nao e criacao), senao EROFS (-> -18 pelo mapa de errno)."""
        if drive.writable:
            return
        if not creating and not os.path.lexists(host):
            raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))
        raise OSError(errno.EROFS, os.strerror(errno.EROFS))

    def _durable_dir(self, drive: Drive, host: str) -> None:
        if drive.durable:
            _fsync_dir(os.path.dirname(host))

    # --- handles ---
    def _alloc(self, entry: FileHandle | FindHandle) -> int:
        for h in range(1, FS_MAX_HANDLES + 1):
            if h not in self.handles:
                self.handles[h] = entry
                return h
        raise FsError(FS_TOO_MANY_FILES, "handle table full")

    def _free_slot(self) -> bool:
        return len(self.handles) < FS_MAX_HANDLES

    def _file(self, h: int) -> FileHandle:
        e = self.handles.get(h)
        if not isinstance(e, FileHandle):
            raise FsError(FS_INVALID_FILE_HANDLE)
        return e

    def _open(self, md_path: str, fs_flags: int) -> int:
        drive, host = self.resolve(md_path)
        flags = open_flags(fs_flags)
        if (flags & os.O_ACCMODE) != os.O_RDONLY or flags & (os.O_CREAT | os.O_TRUNC):
            self._need_writable(drive, host, creating=bool(flags & os.O_CREAT))
        if not self._free_slot():      # o ROM confere a tabela antes do open()
            raise FsError(FS_TOO_MANY_FILES, "handle table full")
        existed = os.path.lexists(host)
        fd = os.open(host, flags | os.O_CLOEXEC, FS_CREATE_MODE)
        created = not existed and os.path.exists(host)
        return self._alloc(FileHandle(fd=fd, path=host, drive=drive, created=created))

    def _close(self, h: int) -> None:
        e = self.handles.pop(h, None)
        if isinstance(e, FindHandle):
            return
        if e is None:
            raise FsError(FS_INVALID_FILE_HANDLE)
        try:
            if e.drive.durable and (e.dirty or e.created):
                os.fsync(e.fd)
        finally:
            os.close(e.fd)
        if e.created:
            self._durable_dir(e.drive, e.path)

    def close_all(self) -> int:
        """Fecha tudo (com fsync). Chamado no CloseAll/ShutDown e quando o MD para: os
        handles de uma vida do MD nao valem na proxima."""
        n = 0
        for h in sorted(self.handles):
            try:
                self._close(h)
                n += 1
            except (OSError, FsError) as e:
                log(LOG_WARNING, f"fs: closing handle {h}: {e}")
        return n

    def reset(self) -> None:
        with self.lock:
            n = self.close_all()
            self.reasm.reset()
        if n:
            log(LOG_INFO, f"fs: closed {n} handle(s) left open by the modem")

    # --- operacoes (0x1001..0x1025) ---
    def op_open(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        path = self._arg_path(b, 0, ctx)
        ctx.flags = self._arg_u32(b, 1)
        h = self._open(path, ctx.flags)
        ctx.result = h
        return [_i32(h)]

    def op_seek(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        h, off, whence = self._arg_i32(b, 0), self._arg_i32(b, 1), self._arg_u32(b, 2)
        e = self._file(h)
        ctx.path = e.path
        if whence > 2:                   # ROM: whence fora de 0..2 -> -10 (MT6895 0x1b848)
            raise FsError(FS_INVALID_FILE_HANDLE, f"whence {whence}")
        pos = os.lseek(e.fd, off, whence)
        ctx.result = pos
        return [_i32(pos)]

    def op_read(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        h, n = self._arg_i32(b, 0), self._arg_u32(b, 1)
        e = self._file(h)
        ctx.path = e.path
        data = bytearray()
        want = min(n, FS_MAX_IO)
        while len(data) < want:
            chunk = os.read(e.fd, want - len(data))
            if not chunk:
                break
            data += chunk
        ctx.nbytes = len(data)
        self.stats.read_bytes += len(data)
        return [_i32(FS_OK), _u32(len(data)), bytes(data)]

    def op_write(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        h = self._arg_i32(b, 0)
        if len(b) < 3:
            raise FsError(FS_PARAM_ERROR, "missing blocks")
        data, n = b[1], self._arg_i32(b, 2)
        e = self._file(h)
        ctx.path = e.path
        if n < 0:
            raise FsError(FS_PARAM_ERROR, f"negative length {n}")
        written = self._write_all(e, data[:n])
        ctx.nbytes = written
        return [_i32(FS_OK), _i32(written)]

    @staticmethod
    def _reserve(path: str, extra: int) -> None:
        """FS_DISK_FULL se crescer `extra` bytes deixaria menos que FS_MIN_FREE livres."""
        if extra <= 0:
            return
        v = os.statvfs(path)
        if v.f_bavail * v.f_frsize - extra < FS_MIN_FREE:
            raise FsError(FS_DISK_FULL, f"free-space reserve ({extra} bytes asked)")

    def _write_all(self, e: FileHandle, data: bytes) -> int:
        # O que a escrita acrescenta ao arquivo, inclusive o buraco de um seek alem do fim.
        self._reserve(os.path.dirname(e.path),
                      os.lseek(e.fd, 0, os.SEEK_CUR) + len(data) - os.fstat(e.fd).st_size)
        view, done = memoryview(data), 0
        while done < len(data):
            w = os.write(e.fd, view[done:])
            if w <= 0:
                break
            done += w
        e.dirty = e.dirty or done > 0
        self.stats.written_bytes += done
        return done

    def op_close(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        h = self._arg_i32(b, 0)
        e = self.handles.get(h)
        ctx.path = e.path if isinstance(e, FileHandle) else ""
        if not isinstance(e, FileHandle):
            raise FsError(FS_INVALID_FILE_HANDLE)
        self._close(h)
        return [_i32(FS_OK)]

    def op_closeall(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        ctx.note = f"closed {self.close_all()}"
        return [_i32(FS_OK)]

    def op_createdir(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        drive, host = self.resolve(self._arg_path(b, 0, ctx))
        self._need_writable(drive, host, creating=True)
        os.mkdir(host, FS_DIR_MODE)      # ja existe -> EEXIST -> -18 (como o ROM)
        self._durable_dir(drive, host)
        return [_i32(FS_OK)]

    def op_removedir(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        drive, host = self.resolve(self._arg_path(b, 0, ctx))
        self._need_writable(drive, host)
        os.rmdir(host)
        self._durable_dir(drive, host)
        return [_i32(FS_OK)]

    def op_getfilesize(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        e = self._file(self._arg_i32(b, 0))
        ctx.path = e.path
        size = os.fstat(e.fd).st_size
        ctx.nbytes = size
        return [_i32(FS_OK), _u32(size)]

    def op_getfoldersize(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        _drive, host = self.resolve(self._arg_path(b, 0, ctx))
        st = os.stat(host)
        total = st.st_size
        if stat.S_ISDIR(st.st_mode):
            total = 0
            for dirpath, _dirs, files in os.walk(host):
                for f in files:
                    try:
                        s = os.lstat(os.path.join(dirpath, f))
                    except OSError:
                        continue
                    if stat.S_ISREG(s.st_mode):
                        total += s.st_size
        ctx.result = min(total, 0x7FFFFFFF)
        return [_i32(ctx.result)]

    def op_rename(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        sdrive, src = self.resolve(self._arg_path(b, 0, ctx))
        ddrive, dst = self.resolve(self._arg_path(b, 1, ctx))
        self._need_writable(sdrive, src)
        self._need_writable(ddrive, dst, creating=True)
        os.rename(src, dst)
        self._durable_dir(sdrive, src)
        self._durable_dir(ddrive, dst)
        return [_i32(FS_OK)]

    def op_move(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        """Move com FS_MOVE_COPY (o 0x10001 que o MT6895 viu) copia e mantem a origem:
        protect1/protect2 sao o par A/B do NVRAM e apagar a origem os desencontraria
        (adaptado do MT6895 do_move). Sem COPY e com KILL vira rename."""
        sdrive, src = self.resolve(self._arg_path(b, 0, ctx))
        ddrive, dst = self.resolve(self._arg_path(b, 1, ctx))
        flags = ctx.flags = self._arg_u32(b, 2) if len(b) > 2 else FS_MOVE_COPY
        st = os.stat(src)                         # origem ausente -> ENOENT -> -9
        self._need_writable(ddrive, dst, creating=True)
        if flags & FS_MOVE_COPY or not flags & FS_MOVE_KILL:
            # A copia escreve os buracos de um arquivo esparso: conta o tamanho aparente.
            self._reserve(os.path.dirname(dst), _apparent_size(src))
        if os.path.lexists(dst) and not flags & FS_MOVE_OVERWRITE:
            raise OSError(errno.EEXIST, os.strerror(errno.EEXIST))
        if flags & FS_MOVE_KILL and not flags & FS_MOVE_COPY:
            self._need_writable(sdrive, src)
            os.replace(src, dst)
            self._durable_dir(sdrive, src)
        elif stat.S_ISDIR(st.st_mode):
            if os.path.lexists(dst):
                raise OSError(errno.EEXIST, os.strerror(errno.EEXIST))
            # Atomica como _copy_file: uma copia que falha no meio nao deixa nada para tras.
            tmp = f"{dst}.thunder-modemd-tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                shutil.copytree(src, tmp, symlinks=True)
                os.rename(tmp, dst)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        else:
            self._copy_file(src, dst)
            ctx.nbytes = st.st_size
        self._durable_dir(ddrive, dst)
        return [_i32(FS_OK)]

    @staticmethod
    def _copy_file(src: str, dst: str) -> None:
        """Copia atomica: temporario no mesmo diretorio, fsync, rename por cima."""
        tmp = f"{dst}.thunder-modemd-tmp"
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                shutil.copyfileobj(fi, fo, 1 << 20)
                fo.flush()
                os.fsync(fo.fileno())
            shutil.copymode(src, tmp)
            os.replace(tmp, dst)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def op_count(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        _drive, host = self.resolve(self._arg_path(b, 0, ctx))
        ctx.flags = self._arg_u32(b, 1) if len(b) > 1 else 0
        st = os.stat(host)
        n = len(os.listdir(host)) if stat.S_ISDIR(st.st_mode) else 1
        ctx.result = n
        return [_i32(n)]

    def op_getdiskinfo(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        """ROM 0x19200 -> 0x1f7a4 (conferido com llvm-objdump): ignora o caminho, faz
        statfs da raiz do Z: e preenche 4 campos do struct de 84 bytes: +48 = 512,
        +52 = f_bsize >> 9, +56 = f_blocks (32 bits), +64 = 512. statfs falhou -> -1.
        O resto o ROM deixa como estava; aqui vai zerado."""
        if b:
            self._arg_path(b, 0, ctx)
        info = bytearray(DISK_INFO_SIZE)
        root = self.drives["Z"].root
        try:
            st = os.statvfs(root or "")
        except OSError as e:
            ctx.note = f"statfs: {e.strerror}"
            ctx.result = -1
            return [_i32(-1), bytes(info)]
        struct.pack_into("<II", info, 48, 512, (st.f_bsize >> 9) & 0xFFFFFFFF)
        struct.pack_into("<I", info, 56, st.f_blocks & 0xFFFFFFFF)
        struct.pack_into("<I", info, 64, 512)
        return [_i32(FS_OK), bytes(info)]

    def op_delete(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        drive, host = self.resolve(self._arg_path(b, 0, ctx))
        self._need_writable(drive, host)
        os.unlink(host)                  # diretorio -> EISDIR -> -18
        self._durable_dir(drive, host)
        return [_i32(FS_OK)]

    def op_getattributes(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        _drive, host = self.resolve(self._arg_path(b, 0, ctx))
        ctx.result = attributes(os.stat(host))
        return [_i32(ctx.result)]

    def op_openhint(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        """ROM: o handler chama o mesmo helper do Open (0x1a628) e ecoa o hint do MD
        (MT6895 0x19380). Resposta [handle][hint]."""
        path = self._arg_path(b, 0, ctx)
        ctx.flags = self._arg_u32(b, 1)
        hint = b[2] if len(b) > 2 else b""
        h = self._open(path, ctx.flags)
        ctx.result = h
        return [_i32(h), hint]

    def op_findfirst(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        """[padrao][u8 attr][u8 attr_mask][u32 cap] -> [handle][52 B][nome UTF-16].
        Ordem de readdir com '.' e '..' (como o servidor do ROM, MT6895 v865); filtro de
        atributos do MT6895: attr exigidos, attr_mask proibidos."""
        pattern = self._arg_path(b, 0, ctx)
        want, forbid, cap = self._arg_u8(b, 1), self._arg_u8(b, 2), self._arg_u32(b, 3)
        ctx.flags = (want << 8) | forbid
        if "/" in pattern:
            md_dir, _, pat = pattern.rpartition("/")
        else:
            md_dir, pat = pattern[:2], pattern[2:]
        if not pat:
            raise FsError(FS_PARAM_ERROR, "empty pattern")
        drive, host_dir = self.resolve(md_dir)
        try:
            listing = os.listdir(host_dir)
            dir_st = os.stat(host_dir)
        except (FileNotFoundError, NotADirectoryError):
            # [INF] pasta ausente = "nada encontrado" (MT6895 respondia -6 aqui)
            return self._find_end(ctx, bytes(DOS_ENTRY_SIZE))
        names = []
        for name in (".", "..", *listing):
            if not dos_match(name, pat):
                continue
            st = dir_st if name in (".", "..") else self._entry_stat(host_dir, name)
            if st is None:
                continue
            a = attributes(st)
            if want & ~a or forbid & a:
                continue
            names.append(name)
        if not names:
            return self._find_end(ctx, bytes(DOS_ENTRY_SIZE))
        h = self._alloc(FindHandle(directory=host_dir, drive=drive, names=names))
        ctx.result = h
        ctx.note = f"{len(names)} match(es)"
        st_code, entry, nb = self._find_advance(h, cap)
        if st_code != FS_OK:
            self.handles.pop(h, None)
            return self._find_end(ctx, entry)
        return [_i32(h), entry, nb]

    @staticmethod
    def _entry_stat(directory: str, name: str) -> os.stat_result | None:
        try:
            st = os.lstat(os.path.join(directory, name))
        except OSError:
            return None
        return None if stat.S_ISLNK(st.st_mode) else st

    @staticmethod
    def _find_end(ctx: _Ctx, entry: bytes) -> list[bytes]:
        ctx.result = FS_NO_MORE_FILES
        return [_i32(FS_NO_MORE_FILES), entry, b""]

    def _find_advance(self, h: int, cap: int) -> tuple[int, bytes, bytes]:
        f = self.handles[h]
        assert isinstance(f, FindHandle)
        while f.idx < len(f.names):
            name = f.names[f.idx]
            f.idx += 1
            path = f.directory if name in (".", "..") else os.path.join(f.directory, name)
            try:
                st = os.stat(path)
            except OSError:
                continue                 # sumiu entre o FindFirst e agora
            f.last_entry = dir_entry(st, name, cap)
            return FS_OK, f.last_entry, name_block(name, cap)
        return FS_NO_MORE_FILES, f.last_entry, b""

    def op_findnext(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        h, cap = self._arg_i32(b, 0), self._arg_u32(b, 1)
        f = self.handles.get(h)
        if not isinstance(f, FindHandle):
            raise FsError(FS_INVALID_FILE_HANDLE)
        ctx.path = f.directory
        code, entry, nb = self._find_advance(h, cap)
        if code != FS_OK:
            # Fim da lista libera o handle (o MD nem sempre manda FindClose; o ROM deixa
            # a ultima entrada no buffer de saida, MT6895 _advance).
            self.handles.pop(h, None)
            return self._find_end(ctx, entry)
        return [_i32(FS_OK), entry, nb]

    def op_findclose(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        h = self._arg_i32(b, 0) if b else 0
        if isinstance(self.handles.get(h), FindHandle):
            self.handles.pop(h)
        return [_i32(FS_OK)]

    def op_lockfat(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        return [_i32(FS_OK)]

    def op_unlockall(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        ctx.result = 1
        return [_i32(1)]                 # ROM 0x1939c: mov w20,#1

    def op_shutdown(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        ctx.note = f"closed {self.close_all()}"
        return []                        # ROM 0x193dc: 0 blocos

    def op_xdelete(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        # Apagar arvores inteiras da NVRAM a pedido do MD fica recusado ate ser visto num
        # boot real (MT6895 fazia o mesmo). -16 e codigo que o mapa oficial produz.
        self._arg_path(b, 0, ctx)
        log(LOG_WARNING, f"fs: modem asked XDelete {ctx.path}; refused (ACCESS_DENIED)")
        raise FsError(FS_ACCESS_DENIED, "XDelete refused")

    def op_cleardiskflag(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        return [_i32(FS_OK)]

    def op_getdrive(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        dtype = self._arg_u32(b, 0) if b else 0
        ctx.flags = dtype
        if dtype != GET_DRIVE_SYSTEM:
            raise FsError(FS_PARAM_ERROR, f"drive type {dtype}")
        ctx.result = GET_DRIVE_ANSWER
        return [_i32(GET_DRIVE_ANSWER)]

    def op_getclustersize(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        """ROM 0x1949c -> 0x227a8: o argumento e a letra do drive ('Q'..'Z'); fora disso
        -4; statfs da raiz e devolve f_bsize; statfs falhou -> -18."""
        letter = self._arg_u32(b, 0)
        drive = self.drives.get(chr(letter).upper()) if 0x41 <= letter <= 0x7A else None
        if drive is None:
            raise FsError(FS_DRIVE_NOT_FOUND, f"drive {letter:#x}")
        ctx.path = f"{drive.letter}:"
        try:
            size = os.statvfs(drive.root or "").f_bsize
        except OSError as e:
            raise FsError(FS_GENERAL_FAILURE, f"statfs: {e.strerror}") from None
        ctx.result = size
        return [_i32(size)]

    def op_setdiskflag(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        return [_i32(FS_OK)]

    # OTP: o ROM usa /dev/otp, que este sistema nao tem. Formas do MT6895.
    def op_otpwrite(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        raise FsError(FS_PARAM_ERROR, "no OTP")

    def op_otpread(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        raise FsError(FS_PARAM_ERROR, "no OTP")

    def op_otpquerylength(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        raise FsError(FS_PARAM_ERROR, "no OTP")

    def op_otplock(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        raise FsError(FS_PARAM_ERROR, "no OTP")

    def op_restore(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        # ROM: NVM_RestoreFromBinRegion_OneFile (libnvram, particao nvram crua). Nao ha
        # BinRegion aqui; recusar faz o MD seguir com o default do LID.
        if b:
            self._arg_path(b, 0, ctx)
        log(LOG_NOTICE, f"fs: modem asked Restore {ctx.path}; no BinRegion here")
        raise FsError(FS_ACCESS_DENIED, "Restore unsupported")

    def op_cmpt_read(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        """Leitura composta (open/seek/read/close). Descritor <10I> do MT6895 (v425.1,
        tirado do codigo do MD): +0 steps, +20 offset, +32 tamanho. Resposta:
        [u32 ops feitos (= steps se ok), i32 status][u32 tamanho do arquivo se o passo
        GETFILESIZE (bit1) foi pedido][u32 lidos][dados]. O MD confere ops == steps e
        lidos == pedido; arquivo ausente = status -9 com 0 ops (MT6895 E2')."""
        path = self._arg_path(b, 0, ctx)
        if len(b) < 2:
            raise FsError(FS_PARAM_ERROR, "missing descriptor")
        steps, _a, _b, _cap, _d1, off, _s, _d2, length, _d3 = struct.unpack_from(
            "<10I", b[1].ljust(40, b"\0"))
        ctx.flags = steps
        data, size = b"", 0
        _drive, host = self.resolve(path)
        fd = os.open(host, os.O_RDONLY | os.O_CLOEXEC)   # o ROM abre com 0x700 (RO)
        try:
            size = os.fstat(fd).st_size
            if steps & 0x4 or off:
                os.lseek(fd, off, os.SEEK_SET)
            want = min(length, FS_MAX_IO)
            buf = bytearray()
            while len(buf) < want:
                chunk = os.read(fd, want - len(buf))
                if not chunk:
                    break
                buf += chunk
            data = bytes(buf)
        finally:
            os.close(fd)
        ctx.nbytes = len(data)
        ctx.note = f"off={off} len={length} size={size}"
        self.stats.read_bytes += len(data)
        return [struct.pack("<Ii", steps, FS_OK), _u32(size if steps & 0x2 else 0),
                _u32(len(data)), data]

    def op_bin_region_access(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        raise FsError(FS_PARAM_ERROR, "bin_region unsupported")   # o ROM tambem -2

    def op_cmpt_write(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        """Escrita composta. Descritor do MT6895 (v450, FS_CMPT_Write_Wrap do MD): +0
        flags (bit0 open, bit2 seek, bit4 close, bit5 write), +12 modo do open, +16
        offset (s32), +20 whence, +28 tamanho, +32 handle (-1 no 1o pedaco). O MD
        reenvia os pedacos seguintes com o handle devolvido e sem seek: a posicao segue
        dentro do mesmo open. Resposta [u32 ops feitos, i32 status][i32 handle]
        [u32 escritos]. O open usa as mesmas flags do Open (o ROM chama 0x1a628)."""
        path = self._arg_path(b, 0, ctx)
        if len(b) < 3:
            raise FsError(FS_PARAM_ERROR, "missing blocks")
        d = b[1].ljust(48, b"\0")
        flags = struct.unpack_from("<I", d, 0)[0]
        mode = struct.unpack_from("<I", d, 12)[0]
        seek_off = struct.unpack_from("<i", d, 16)[0]
        whence = struct.unpack_from("<I", d, 20)[0]
        length = struct.unpack_from("<I", d, 28)[0]
        handle = struct.unpack_from("<i", d, 32)[0]
        payload = b[2][:length] if length else b[2]
        ctx.flags = flags
        status, done, written = FS_OK, 0, 0
        notes = [f"mode=0x{mode:x}"]
        try:
            if isinstance(self.handles.get(handle), FileHandle):
                notes.append(f"reuse h={handle}")
            elif flags & 0x1:
                handle = self._open(path, mode)
                done |= 0x1
                notes.append(f"open h={handle}")
            else:
                raise FsError(FS_INVALID_FILE_HANDLE, "no handle and no open step")
            e = self._file(handle)
            if flags & 0x4:
                if whence > 2:
                    raise FsError(FS_INVALID_FILE_HANDLE, f"whence {whence}")
                os.lseek(e.fd, seek_off, whence)
                done |= 0x4
                notes.append(f"seek {seek_off}/{whence}")
            if flags & 0x20:
                written = self._write_all(e, payload)
                done |= 0x20
        except FsError as e:
            status = e.code
            notes.append(e.reason or FS_CODE_NAMES.get(e.code, ""))
        except OSError as e:
            status = fs_errno(e)
            notes.append(e.strerror or str(e))
        if flags & 0x10 and isinstance(self.handles.get(handle), FileHandle):
            try:
                self._close(handle)
                done |= 0x10
            except OSError as e:
                status = status or fs_errno(e)
                notes.append(f"close: {e.strerror}")
        ctx.result = status
        ctx.nbytes = written
        ctx.note = " ".join(notes)
        return [struct.pack("<Ii", done, status), _i32(handle), _u32(written)]

    def op_getfiledetail(self, b: list[bytes], ctx: _Ctx) -> list[bytes]:
        """ROM 0x19008 / MT6895 0x22bb0: [status][mtime, atime, ctime em u64]."""
        _drive, host = self.resolve(self._arg_path(b, 0, ctx))
        st = os.stat(host)
        return [_i32(FS_OK), struct.pack("<QQQ", int(st.st_mtime), int(st.st_atime),
                                         int(st.st_ctime))]


# --- RPC (SPEC 4.1; adaptado do MT6895 qqc_rpc_sar.py) ------------------------------
RPC_RX_CH, RPC_TX_CH = 32, 33
RPC_QUERY_AP_SYS_PROPERTY = 0x400F
RPC_SAR_TABLE_IDX_QUERY = 0x4010
SAR_NO_TABLE = 0xFFFF               # sem tabela: o MD usa a default


def builtin_properties(rat: str) -> dict[str, str]:
    """Propriedades que o MD pede pelo 0x400F. ro.crypto.state: visto no aparelho, pedido
    pelo nvram_handler_startup.c do MD logo depois do READY; vazio e o suspeito de um
    assert reproduzivel ("nvram_handler_startup.c line 230", 2,3 s depois do READY).
    "unencrypted": este sistema nao cifra a NVRAM. [INF] O resto vem do RSC do thunder
    global (V:etc/rsc/thundergl/ro.prop, SPEC 2.1). Ausente = "" (como property_get de
    prop nao definida). Sobrescreve: MODEM_PROPS_FILE e MODEM_PROP_<nome> (modem.conf)."""
    props = {"ro.crypto.state": "unencrypted",
             "ro.vendor.mtk_protocol1_rat_config": rat, "ro.vendor.mtk_ps1_rat": rat,
             "ro.vendor.mtk_c2k_support": "0"}
    if rat == DEFAULT_RAT:
        props["ro.vendor.mtk_md1_support"] = "22"     # md_rat_map[22] = unlwg
    return props


def load_properties(cfg: Config) -> dict[str, str]:
    """Tabela do RPC 0x400F: embutida < MODEM_PROPS_FILE < MODEM_PROP_* do modem.conf."""
    props = builtin_properties(cfg.rat)
    try:
        with open(cfg.props_file, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        text = None
    except OSError as e:
        log(LOG_WARNING, f"cannot read {cfg.props_file}: {e.strerror}; ignored")
        text = None
    if text is not None:
        extra, warnings = parse_props_file(text)
        for w in warnings:
            log(LOG_WARNING, f"{cfg.props_file}: {w}")
        props.update(extra)
        log(LOG_INFO, f"{cfg.props_file}: {len(extra)} modem properties")
    props.update(dict(cfg.props))
    return props


class RpcServer:
    def __init__(self, properties: dict[str, str], capture: Capture | None = None) -> None:
        self.properties = properties
        self.capture = capture

    def process_packet(self, pkt: bytes) -> list[bytes]:
        """Resposta reaproveita o cabecalho do pedido como o kernel faz (port_rpc.c
        1280-1320): data[0] e reserved ecoados, canal 33, op | 0xFFFF0000."""
        if self.capture:
            self.capture.record(Capture.RPC_RX, pkt)
        if len(pkt) < 24:
            log(LOG_WARNING, f"rpc: short packet {pkt.hex()}")
            return []
        d0, _length, _ch, reserved = CCCI_HEADER.unpack_from(pkt)
        op, count = struct.unpack_from("<II", pkt, 16)
        try:
            blocks = parse_blocks(pkt, 24, count)
        except ValueError as e:
            log(LOG_WARNING, f"rpc: op 0x{op:x} malformed ({e}): {pkt.hex()}")
            blocks = []
        if op == RPC_QUERY_AP_SYS_PROPERTY:
            name = blocks[0].split(b"\0", 1)[0].decode("ascii", "replace") if blocks else ""
            known = name in self.properties
            value = self.properties.get(name, "")
            raw = value.encode()
            out = [_i32(len(raw)), raw + b"\0"]
            if known:
                log(LOG_INFO, f"rpc: modem asked property {name!r} -> {value!r}")
            else:
                log(LOG_NOTICE, f"rpc: modem asked property {name!r}, not in the table; "
                                "answered empty (set it with MODEM_PROP_<name>)")
        elif op == RPC_SAR_TABLE_IDX_QUERY:
            out = [_i32(0), _u32(SAR_NO_TABLE)]
            log(LOG_INFO, "rpc: SAR table index query -> 0xffff (default table)")
        else:
            log(LOG_WARNING, f"rpc: op 0x{op:x} not handled (the kernel forwards only "
                             f"0x400f/0x4010): {pkt[:64].hex()}")
            return []
        msg = encode_message(op | RESP_ID, out)
        reply = CCCI_HEADER.pack(d0, 16 + len(msg), RPC_TX_CH, reserved) + msg
        if self.capture:
            self.capture.record(Capture.RPC_TX, reply)
        return [reply]


# --- acesso ao kernel (trocado nos testes) ------------------------------------------
class Dev:
    def open(self, path: str, flags: int) -> int:
        return os.open(path, flags | os.O_CLOEXEC)

    def close(self, fd: int) -> None:
        os.close(fd)

    def read(self, fd: int, n: int) -> bytes:
        return os.read(fd, n)

    def write(self, fd: int, data: bytes) -> int:
        return os.write(fd, data)

    def ioctl(self, fd: int, request: int, arg: int | bytearray = 0) -> int:
        if isinstance(arg, bytearray):
            return fcntl.ioctl(fd, request, arg, True)
        return fcntl.ioctl(fd, request, arg)

    def readable(self, fd: int, timeout: float) -> bool:
        p = select.poll()
        p.register(fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        return bool(p.poll(int(timeout * 1000)))

    def read_text(self, path: str) -> str | None:
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                return f.read().strip()
        except OSError:
            return None

    def mmap(self, fd: int, length: int):
        return mmap.mmap(fd, length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)


def ioctl_u32(dev: Dev, fd: int, request: int) -> int:
    buf = bytearray(4)
    dev.ioctl(fd, request, buf)
    return struct.unpack("<I", buf)[0]


# --- comm da thread de partida -------------------------------------------------------
_libc = None


def libc_prctl(option: int, arg2, arg3: int = 0, arg4: int = 0, arg5: int = 0) -> int:
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    fn = _libc.prctl
    fn.restype = ctypes.c_int
    rc = fn(ctypes.c_int(option), arg2, ctypes.c_ulong(arg3), ctypes.c_ulong(arg4),
            ctypes.c_ulong(arg5))
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return rc


def set_thread_comm(name: str, prctl=libc_prctl) -> None:
    """comm e por thread (task_struct): PR_SET_NAME so muda a thread que chama."""
    prctl(PR_SET_NAME, ctypes.c_char_p(name.encode()[:15]), 0, 0, 0)


def thread_comm() -> str:
    for path in ("/proc/thread-self/comm",
                 f"/proc/self/task/{threading.get_native_id()}/comm"):
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                return f.read().strip()
        except OSError:
            continue
    return ""


def start_md(dev: Dev, mon_fd: int, prctl=libc_prctl, comm=thread_comm,
             timeout: float = START_THREAD_TIMEOUT) -> None:
    """DO_START_MD de uma thread nova com comm "ccci_mdinit". O kernel so enfileira o
    start se current->comm comeca com "ccci_mdinit" e, se nao, loga "drop invalid user"
    e devolve 0 (fsm_ioctl.c:507-519): por isso o comm e conferido antes do ioctl."""
    result: dict[str, BaseException | bool] = {}

    def body() -> None:
        try:
            set_thread_comm(MDINIT_COMM, prctl)
            got = comm()
            if not got.startswith(MDINIT_COMM):
                raise Failure(EX_SOFTWARE, f"start thread comm is {got!r}, not "
                                           f"{MDINIT_COMM!r}: the kernel would drop the start")
            dev.ioctl(mon_fd, CCCI_IOC_DO_START_MD, 0)
            result["ok"] = True
        except BaseException as e:   # repassado para a thread principal
            result["error"] = e

    t = threading.Thread(target=body, name=MDINIT_COMM, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise Failure(EX_SOFTWARE, "DO_START_MD did not return")
    err = result.get("error")
    if isinstance(err, Failure):
        raise err
    if isinstance(err, OSError):
        raise Failure(EX_PROTOCOL, f"DO_START_MD: {err.strerror or err}")
    if err is not None:
        raise Failure(EX_SOFTWARE, f"start thread: {err!r}")


# --- NVRAM: copia gravavel -----------------------------------------------------------
NV_REQUIRED = ("nvdata/md", "protect1/md", "protect2/md")
NV_OPTIONAL = ("nvdata/md_cmn", "nvcfg", "mdlpm", "vendor_de_md")


def check_nv_storage(cfg: Config) -> None:
    """A copia tem de existir (nv-seed): sem ela o MD recriaria IMEI e calibracao com
    default na copia. As pastas que nascem vazias no Android sao criadas aqui."""
    for rel in NV_REQUIRED:
        path = f"{cfg.nv_root}/{rel}"
        if not os.path.isdir(path):
            raise Failure(EX_NOINPUT, f"{path} missing: seed the NV copy first "
                                      "(thunder-modemd nv-seed ...)")
    if not os.listdir(f"{cfg.nv_root}/nvdata/md"):
        raise Failure(EX_NOINPUT, f"{cfg.nv_root}/nvdata/md is empty: seed the NV copy first")
    for rel in NV_OPTIONAL:
        try:
            os.makedirs(f"{cfg.nv_root}/{rel}", FS_DIR_MODE, exist_ok=True)
        except OSError as e:
            raise Failure(EX_NOINPUT, f"cannot create {cfg.nv_root}/{rel}: {e.strerror}") from None
    mode = os.stat(cfg.nv_root).st_mode
    if mode & 0o077:
        log(LOG_WARNING, f"{cfg.nv_root} is accessible to other users (mode "
                         f"{stat.S_IMODE(mode):o}); it holds the IMEI")


def copy_tree(src: str, dst: str) -> tuple[int, list[str]]:
    """Copia arquivos e pastas com modo e datas; symlink e arquivo especial ficam de
    fora (o servidor recusaria de todo jeito)."""
    os.mkdir(dst, 0o700)
    files, skipped = 0, []
    with os.scandir(src) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        target = os.path.join(dst, entry.name)
        if entry.is_symlink():
            skipped.append(entry.path)
        elif entry.is_dir(follow_symlinks=False):
            n, s = copy_tree(entry.path, target)
            files += n
            skipped += s
        elif entry.is_file(follow_symlinks=False):
            shutil.copy2(entry.path, target, follow_symlinks=False)
            files += 1
        else:
            skipped.append(entry.path)
    shutil.copystat(src, dst, follow_symlinks=False)
    return files, skipped


def nv_seed(cfg: Config, nvdata: str, protect1: str, protect2: str,
            nvcfg: str | None = None) -> int:
    """Copia md/ e md_cmn/ do nvdata, md/ do protect1 e do protect2 e mdota/ do nvcfg
    (o resto do nvcfg e calibracao de sensor/camera do AP, nao e do modem). As origens
    sao as raizes das particoes montadas ro ou de um backup extraido."""
    plan = [(f"{nvdata}/md", "nvdata/md", True), (f"{nvdata}/md_cmn", "nvdata/md_cmn", False),
            (f"{protect1}/md", "protect1/md", True), (f"{protect2}/md", "protect2/md", True)]
    if nvcfg is not None:
        plan.append((f"{nvcfg}/mdota", "nvcfg/mdota", False))
    root = cfg.nv_root
    for src, _rel, required in plan:
        if required and not os.path.isdir(src):
            raise Failure(EX_NOINPUT, f"{src} is not a directory (pass the partition "
                                      "roots, the ones that contain md/)")
    for s in [nvdata, protect1, protect2] + ([nvcfg] if nvcfg else []):
        if not os.path.isdir(s):
            raise Failure(EX_NOINPUT, f"{s} is not a directory")
        rs, rr = os.path.realpath(s), os.path.realpath(root)
        if os.path.commonpath((rs, rr)) in (rs, rr):
            raise Failure(EX_USAGE, f"{s} and {root} overlap")
    if os.path.lexists(root):
        if not os.path.isdir(root) or os.path.islink(root):
            raise Failure(EX_CANTCREAT, f"{root} exists and is not a directory")
        if os.listdir(root):
            raise Failure(EX_CANTCREAT, f"{root} is not empty; refusing to overwrite the "
                                        "modem's NV copy (move it away first)")
    os.makedirs(root, 0o700, exist_ok=True)
    os.chmod(root, 0o700)
    stage = os.path.join(root, ".seed-incomplete")
    os.mkdir(stage, 0o700)
    total = 0
    try:
        for top in ("nvdata", "protect1", "protect2", "nvcfg"):
            os.mkdir(os.path.join(stage, top), 0o700)
        for src, rel, _required in plan:
            dst = os.path.join(stage, rel)
            if not os.path.isdir(src):
                os.mkdir(dst, FS_DIR_MODE)
                log(LOG_INFO, f"{src} absent; {rel} starts empty")
                continue
            n, skipped = copy_tree(src, dst)
            total += n
            for s in skipped:
                log(LOG_WARNING, f"skipped {s} (symlink or special file)")
            log(LOG_INFO, f"{rel}: {n} files from {src}")
        for rel in ("mdlpm", "vendor_de_md"):
            os.mkdir(os.path.join(stage, rel), FS_DIR_MODE)
        for name in os.listdir(stage):
            os.rename(os.path.join(stage, name), os.path.join(root, name))
        os.rmdir(stage)
    except OSError as e:
        raise Failure(EX_CANTCREAT, f"seeding {root} failed: {e} (remove {root} and "
                                    "try again)") from None
    os.sync()
    log(LOG_INFO, f"NV copy seeded in {root}: {total} files (holds the IMEI and RF "
                  "calibration: never publish it)")
    return EX_OK


# --- servico ---------------------------------------------------------------------------
class Modemd:
    """Partida e supervisao do MD. A thread principal faz tudo que mexe no estado do MD;
    as threads de leitura so atendem FS/RPC e enfileiram mensagens do monitor e eventos
    do md1_sta."""

    def __init__(self, cfg: Config, dev: Dev | None = None, notify=sd_notify,
                 prctl=libc_prctl, comm=thread_comm) -> None:
        self.cfg = cfg
        self.dev = dev or Dev()
        self.notify = notify
        self.prctl, self.comm = prctl, comm
        self.capture = Capture.from_env()
        self.drives = drive_table(cfg.nv_root, cfg.ro_root)
        self.fs = FsServer(self.drives, self.capture)
        self.rpc = RpcServer(load_properties(cfg), self.capture)
        self.fds: dict[str, int] = {}
        self.events: queue.Queue = queue.Queue()
        self.deferred: list[tuple] = []
        self.stop_requested = False
        self.threads_stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.sta_lock = threading.Lock()
        self.sta_seq = 0
        self.sta_events: list[tuple[int, StaEvent]] = []
        self.md_running = False
        self.md_ready = False
        self.ready_announced = False
        self.time_synced = False
        self.clock_offset = 0.0
        self.last_periodic = 0.0
        self.last_summary = 0.0
        self.last_summary_count = -1

    # --- sinais ---
    def _on_signal(self, _signum: int, _frame: object) -> None:
        self.stop_requested = True       # so a flag: nada de log nem lock aqui

    def _check_stop(self) -> None:
        if self.stop_requested:
            raise StopRequested()

    def _status(self, text: str) -> None:
        self.notify(f"STATUS={text}")

    # --- abertura dos nos e threads ---
    def open_devices(self) -> None:
        """Ordem da SPEC 5.2: FS (e a thread dele) antes de tudo, entao RPC, monitor e
        md1_sta. Todo no do eccci tem um dono so: EBUSY = outro dono (ccci_mdinit da
        ROM, outra instancia)."""
        for name, path, flags, required in (
                ("fs", DEV_FS, os.O_RDWR, True), ("rpc", DEV_RPC, os.O_RDWR, True),
                ("monitor", DEV_MONITOR, os.O_RDWR, True),
                ("sta", DEV_MD1_STA, os.O_RDONLY, False)):
            try:
                self.fds[name] = self.dev.open(path, flags)
            except OSError as e:
                if required:
                    raise Failure(EX_UNAVAILABLE, f"open {path}: {e.strerror}"
                                  + (" (another owner?)" if e.errno == errno.EBUSY else "")) from None
                log(LOG_WARNING, f"open {path}: {e.strerror}; relying on GET_MD_STATE")
            log(LOG_DEBUG, f"opened {path}")

    def start_threads(self) -> None:
        targets = [("fs", self._serve_loop, ("fs", self.fs)),
                   ("rpc", self._serve_loop, ("rpc", self.rpc)),
                   ("monitor", self._monitor_loop, ()), ("sta", self._sta_loop, ())]
        for name, target, args in targets:
            if name not in self.fds:
                continue
            t = threading.Thread(target=target, args=args, name=f"modemd-{name}", daemon=True)
            t.start()
            self.threads.append(t)

    def _serve_loop(self, name: str, server: FsServer | RpcServer) -> None:
        """Leitura bloqueante com poll de 0,5 s (para notar a parada). O FS tem de seguir
        respondendo em BOOT_WAITING e em EXCEPTION (SPEC 1.6)."""
        fd = self.fds[name]
        while not self.threads_stop.is_set():
            try:
                if not self.dev.readable(fd, 0.5):
                    continue
                pkt = self.dev.read(fd, 65536)
            except OSError as e:
                if e.errno == errno.EBADF or self.threads_stop.is_set():
                    return
                log(LOG_WARNING, f"{name}: read failed: {e.strerror}")
                time.sleep(0.5)
                continue
            if not pkt:
                time.sleep(0.05)
                continue
            for out in server.process_packet(pkt):
                try:
                    n = self.dev.write(fd, out)
                    if n != len(out):
                        log(LOG_WARNING, f"{name}: short write {n}/{len(out)}")
                except OSError as e:
                    log(LOG_ERR, f"{name}: reply of {len(out)} B not sent: {e.strerror}")

    def _monitor_loop(self) -> None:
        # O monitor nao tem poll (fops sem .poll): read bloqueante; o processo sai com a
        # thread presa no read, e o fd so e solto no fim do processo.
        fd = self.fds["monitor"]
        while not self.threads_stop.is_set():
            try:
                data = self.dev.read(fd, 4096)
            except OSError as e:
                if e.errno == errno.EBADF or self.threads_stop.is_set():
                    return
                log(LOG_WARNING, f"monitor: read failed: {e.strerror}")
                time.sleep(0.5)
                continue
            msg = parse_monitor(data)
            if msg is None:
                if data:
                    log(LOG_WARNING, f"monitor: short message {data.hex()}")
                continue
            log(LOG_NOTICE, f"monitor: {msg.name} param=0x{msg.param:x}")
            self.events.put(("mon", msg))

    def _sta_loop(self) -> None:
        fd = self.fds["sta"]
        while not self.threads_stop.is_set():
            try:
                if not self.dev.readable(fd, 0.5):
                    continue
                data = self.dev.read(fd, STA_READ_SIZE)
            except OSError as e:
                if e.errno == errno.EBADF or self.threads_stop.is_set():
                    return
                if e.errno == errno.EAGAIN:
                    continue
                log(LOG_WARNING, f"md1_sta: read failed: {e.strerror}")
                time.sleep(0.5)
                continue
            ev = parse_sta_event(data)
            if ev is None:
                continue
            self._record_sta(ev)

    def _record_sta(self, ev: StaEvent) -> None:
        with self.sta_lock:
            self.sta_seq += 1
            self.sta_events.append((self.sta_seq, ev))
            del self.sta_events[:-32]
            seq = self.sta_seq
        log(LOG_INFO, f"md1_sta: event {ev.name} ({ev.event}) reason={ev.reason!r} "
                      f"at {ev.sec}.{ev.usec:06d}")
        self.events.put(("sta", seq, ev))

    def _sta_marker(self) -> int:
        with self.sta_lock:
            return self.sta_seq

    def _sta_since(self, marker: int, kinds: set[int]) -> bool:
        with self.sta_lock:
            return any(seq > marker and ev.event in kinds for seq, ev in self.sta_events)

    def _next_event(self, timeout: float):
        try:
            return self.events.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    # --- estado do MD ---
    def user_state(self) -> int | None:
        try:
            return ioctl_u32(self.dev, self.fds["monitor"], CCCI_IOC_GET_MD_STATE)
        except OSError:
            return None

    def sys_state(self) -> int | None:
        return parse_sys_boot(self.dev.read_text(SYS_BOOT))

    def log_kernel_info(self) -> None:
        kcfg = self.dev.read_text(SYS_KCFG)
        if kcfg:
            keep = [ln.strip() for ln in kcfg.splitlines()
                    if ln.strip().startswith(("[modem en]", "[ccci_drv_ver]", "[MTK_MD_CAP]"))]
            log(LOG_INFO, "kcfg_setting: " + " ".join(keep or [kcfg.splitlines()[0]]))
        try:
            ver = ioctl_u32(self.dev, self.fds["monitor"], CCCI_IOC_GET_MD_INFO)
            log(LOG_INFO, f"modem image: {({1: 'debug', 2: 'release'}).get(ver, ver)} build")
        except OSError as e:
            log(LOG_DEBUG, f"GET_MD_INFO: {e.strerror}")

    def ensure_gated(self) -> None:
        """DO_START_MD so vale com o FSM em GATED (ccci_fsm.c:346-352). Logo depois do boot
        o FSM ja esta em GATED, mas o md_state do sysfs fica em INVALID (md1:0) ate a
        primeira partida (visto no aparelho: a partida de md1:0 vai direto a HS1); os dois
        valem."""
        text = self.dev.read_text(SYS_BOOT)
        if text == "":
            raise Failure(EX_UNAVAILABLE, f"{SYS_BOOT} is empty: MD1 is disabled in this kernel")
        deadline = time.monotonic() + STOP_TIMEOUT
        stopped = False
        while True:
            s = self.sys_state()
            u = self.user_state()
            if s in (SYS_GATED, SYS_INVALID) or (s is None and u == MD_USER_INVALID):
                return
            if not stopped and (s in (SYS_READY, SYS_EXCEPTION)
                                or u in (MD_USER_READY, MD_USER_EXCEPTION)):
                log(LOG_WARNING, f"modem already {SYS_STATE_NAMES.get(s, s)}; stopping it first")
                stopped = True
                if not self.stop_md():
                    raise Failure(EX_TEMPFAIL, "modem did not stop; cannot start it again")
                continue
            if time.monotonic() >= deadline:
                raise Failure(EX_TEMPFAIL, f"modem not gated after {STOP_TIMEOUT:g} s (state "
                                           f"{SYS_STATE_NAMES.get(s, s)}, user {u})")
            self._check_stop()
            time.sleep(0.25)

    def ccb_init(self) -> int:
        """SPEC 2.2: um cabecalho de 0x40 B por entrada do ccb_configs[], antes do start."""
        fd = self.dev.open(DEV_CCB, os.O_RDWR)
        try:
            n = ioctl_u32(self.dev, fd, CCCI_IOC_GET_CCB_CONFIG_LENGTH)
            if not 0 < n <= CCB_MAX_CONFIGS:
                raise Failure(EX_PROTOCOL, f"CCB: {n} configs")
            configs = []
            for i in range(n):
                buf = bytearray(CCB_CONFIG.pack(i, 0, 0, 0, 0, 0))
                self.dev.ioctl(fd, CCCI_IOC_GET_CCB_CONFIG, buf)
                configs.append(CCB_CONFIG.unpack(buf))
                log(LOG_DEBUG, "CCB[%d] user=%d core=%d dl_page=%d ul_page=%d dl_buff=%d "
                               "ul_buff=%d" % (i, *configs[-1]))
            ctrl_len = 0
            for user in CCB_CTRL_USERS:
                buf = bytearray(CCB_CTRL_INFO.pack(user, 0, 0, 0))
                try:
                    self.dev.ioctl(fd, CCCI_IOC_CCB_CTRL_INFO, buf)
                except OSError as e:
                    log(LOG_WARNING, f"CCB_CTRL_INFO user {user}: {e.strerror}")
                    continue
                _u, off, _addr, length = CCB_CTRL_INFO.unpack(buf)
                log(LOG_DEBUG, f"CCB ctrl user {user}: offset={off} length=0x{length:x}")
                ctrl_len = max(ctrl_len, length)
            headers = ccb_headers(configs)
            if ctrl_len < len(headers):
                raise Failure(EX_PROTOCOL, f"CCB control region of {ctrl_len} B, need "
                                           f"{len(headers)}")
            # O mmap arredonda para pagina e o kernel recusa mais que o tamanho da regiao.
            page = mmap.PAGESIZE
            maplen = min(ctrl_len, (len(headers) + page - 1) // page * page)
            mm = self.dev.mmap(fd, maplen)
            try:
                # Regiao nao cacheada (Device): escrita em blocos de 64 B alinhados, como o
                # MT6895 fez no hardware dele.
                for i in range(0, len(headers), CCB_HEADER_SIZE):
                    mm[i:i + CCB_HEADER_SIZE] = headers[i:i + CCB_HEADER_SIZE]
            finally:
                mm.close()
            return n
        finally:
            self.dev.close(fd)

    def _ccb_init_retry(self) -> None:
        last = None
        for attempt in range(1, 4):
            try:
                n = self.ccb_init()
                log(LOG_INFO, f"CCB: {n} control headers written")
                return
            except (OSError, Failure) as e:
                last = e
                log(LOG_WARNING, f"CCB init attempt {attempt}: {e}")
                self._check_stop()
                time.sleep(1)
        raise Failure(EX_PROTOCOL, f"CCB init failed: {last}")

    def _probe_md_type(self) -> None:
        """GET_MD_TYPE espera ate 300 s se o rt-rat nao foi setado (fsm_ioctl.c:291-309):
        so diagnostico, numa thread a parte para nunca segurar a partida."""
        def body() -> None:
            try:
                t = ioctl_u32(self.dev, self.fds["monitor"], CCCI_IOC_GET_MD_TYPE)
                log(LOG_INFO, f"GET_MD_TYPE = {t} (22 = N/Lf/Lt/W/G)")
            except (OSError, KeyError) as e:
                log(LOG_DEBUG, f"GET_MD_TYPE: {e}")
        threading.Thread(target=body, name="modemd-mdtype", daemon=True).start()

    def boot(self) -> None:
        """SPEC 5.2 passos 5-10. Falha = MD parado e Failure (o systemd reinicia)."""
        self._status("waiting for the modem to be gated")
        self.ensure_gated()
        self._status("initialising CCB headers")
        self._ccb_init_retry()
        data = boot_data(self.cfg.rat)
        log(LOG_DEBUG, "boot data: " + " ".join(f"{w:08x}" for w in struct.unpack("<16I", data)))
        try:
            self.dev.ioctl(self.fds["monitor"], CCCI_IOC_SET_BOOT_DATA, bytearray(data))
        except OSError as e:
            raise Failure(EX_PROTOCOL, f"SET_BOOT_DATA refused: {e.strerror} (see "
                                       "'Current setting has mistake' in dmesg)") from None
        try:
            self.dev.ioctl(self.fds["monitor"], CCCI_IOC_UPDATE_SIM_SLOT_CFG,
                           bytearray(struct.pack("<4I", *SIM_SLOT_CFG)))
        except OSError as e:
            log(LOG_WARNING, f"UPDATE_SIM_SLOT_CFG: {e.strerror}; continuing")
        self._probe_md_type()
        marker = self._sta_marker()
        self._status("starting the modem")
        self.md_running = True
        self.md_ready = False
        start_md(self.dev, self.fds["monitor"], self.prctl, self.comm)
        log(LOG_INFO, f"DO_START_MD issued (RAT {self.cfg.rat}); waiting up to "
                      f"{self.cfg.boot_timeout} s for READY")
        result = self.wait_ready(self.cfg.boot_timeout, marker)
        if result != "ready":
            log(LOG_ERR, f"modem did not become ready: {result}")
            self.log_exception_info()
            self.stop_md()
            raise Failure(EX_UNAVAILABLE, f"modem did not become ready ({result})")
        self.on_ready()

    def wait_ready(self, timeout: float, marker: int) -> str:
        """READY = GET_MD_STATE 2 (o evento 10 so acorda a espera). Sem evento no timeout
        de HS1/HS2: o kernel manda EXCEPTION so ao monitor (SPEC 1.4.5)."""
        deadline = time.monotonic() + timeout
        last = None
        while True:
            self._check_stop()
            u = self.user_state()
            if u != last:
                log(LOG_DEBUG, f"GET_MD_STATE = {USER_STATE_NAMES.get(u, u)}")
                last = u
            if u == MD_USER_READY or (u is None and self._sta_since(marker, {EV_READY})):
                return "ready"
            if u == MD_USER_EXCEPTION:
                return "exception (GET_MD_STATE 3)"
            left = deadline - time.monotonic()
            if left <= 0:
                return f"timeout after {timeout:g} s (state {USER_STATE_NAMES.get(u, u)})"
            item = self._next_event(min(0.25, left))
            if item and item[0] == "mon":
                msg: MonitorMsg = item[1]
                if msg.msg == MON_EXCEPTION:
                    return "exception reported by the kernel (boot handshake failed?)"
                if msg.msg in (MON_RESET_REQUEST, MON_FORCE_STOP_REQUEST,
                               MON_FLIGHT_STOP_REQUEST):
                    return f"interrupted by {msg.name}"
                if not self.handle_minor(msg):
                    self.deferred.append(item)

    def stop_md(self, flight: bool = False) -> bool:
        """DO_STOP_MD(&0) (ou &1 = flight) e espera GATED: evento 12 depois do pedido ou
        /sys/kernel/ccci/boot = md1:1; sem sysfs, GET_MD_STATE 0. Ate 20 s. O FS segue
        respondendo durante a parada (outra thread)."""
        mon = self.fds.get("monitor")
        if mon is None:
            return False
        self.md_running = False
        self.md_ready = False
        marker = self._sta_marker()
        self._status("stopping the modem" + (" (flight mode)" if flight else ""))
        try:
            self.dev.ioctl(mon, CCCI_IOC_DO_STOP_MD, bytearray(struct.pack("<I", int(flight))))
        except OSError as e:
            log(LOG_ERR, f"DO_STOP_MD: {e.strerror}")
            return False
        deadline = time.monotonic() + STOP_TIMEOUT
        while True:
            s = self.sys_state()
            if s == SYS_GATED or self._sta_since(marker, {EV_STOP}) or (
                    s is None and self.user_state() == MD_USER_INVALID):
                break
            left = deadline - time.monotonic()
            if left <= 0:
                log(LOG_ERR, f"modem not stopped after {STOP_TIMEOUT:g} s (state "
                             f"{SYS_STATE_NAMES.get(s, s)})")
                return False
            item = self._next_event(min(0.25, left))
            if item and item[0] == "mon":
                self.deferred.append(item)
        self.fs.reset()
        log(LOG_INFO, "modem stopped" + (" (flight mode)" if flight else ""))
        self._status("modem stopped" + (" (flight mode)" if flight else ""))
        return True

    def on_ready(self) -> None:
        self.md_ready = True
        self.time_sync()
        log(LOG_INFO, "modem ready")
        log(LOG_INFO, self.fs.stats.summary())
        if not self.ready_announced:
            self.ready_announced = True
            self.notify("READY=1\nSTATUS=modem ready")
        else:
            self._status("modem ready")

    def time_sync(self) -> None:
        """UPDATE_TIMEZONE + UPDATE_TIME no ccci_ipc_5 (SPEC 2.3): o kernel manda a hora
        pelo port ccci_0_202, que so funciona com o MD READY."""
        try:
            if "ipc5" not in self.fds:
                self.fds["ipc5"] = self.dev.open(DEV_IPC_TIME, os.O_RDWR)
            self.dev.ioctl(self.fds["ipc5"], CCCI_IPC_UPDATE_TIMEZONE, MD_TIME_TZ)
            self.dev.ioctl(self.fds["ipc5"], CCCI_IPC_UPDATE_TIME, MD_TIME_TZ)
        except OSError as e:
            self.time_synced = False
            log(LOG_WARNING, f"time to the modem not sent: {e.strerror}; retrying later")
            return
        self.time_synced = True
        self.clock_offset = time.time() - time.monotonic()
        log(LOG_INFO, "time sent to the modem (UTC)")

    def log_exception_info(self) -> None:
        try:
            ex = ioctl_u32(self.dev, self.fds["monitor"], CCCI_IOC_GET_MD_EX_TYPE)
            log(LOG_ERR, f"modem exception type {ex}")
        except (OSError, KeyError):
            pass
        text = self.dev.read_text(SYS_MDEE)
        if text:
            log(LOG_ERR, "mdee: " + " | ".join(ln.strip() for ln in text.splitlines()[:20]
                                               if ln.strip()))

    # --- mensagens do monitor ---
    def handle_minor(self, msg: MonitorMsg) -> bool:
        """Mensagens que nao mexem no estado do MD. True se tratada."""
        if msg.msg == MON_SEND_BATTERY_INFO:
            try:
                self.dev.ioctl(self.fds["monitor"], CCCI_IOC_SEND_BATTERY_INFO, 0)
            except OSError as e:
                log(LOG_WARNING, f"SEND_BATTERY_INFO: {e.strerror}")
            return True
        if msg.msg in (MON_STORE_NVRAM_MD_TYPE, MON_CFG_UPDATE, MON_RANDOM_PATTERN):
            return True          # ROM: sysenv/"dummy"; nada a fazer aqui
        return False

    def dispatch(self, item: tuple) -> None:
        if item[0] == "sta":
            ev: StaEvent = item[2]
            if ev.event == EV_EXCEPTION and self.md_running:
                self._status("modem exception")
            return
        msg: MonitorMsg = item[1]
        if self.handle_minor(msg):
            return
        if msg.msg == MON_RESET_REQUEST:
            log(LOG_WARNING, "kernel asked for a modem reset; stopping and starting it")
            if self.md_running:
                self.stop_md()
            self.boot()
        elif msg.msg == MON_FORCE_STOP_REQUEST:
            self.stop_md()
        elif msg.msg == MON_FLIGHT_STOP_REQUEST:
            self.stop_md(flight=True)
        elif msg.msg in (MON_FORCE_START_REQUEST, MON_FLIGHT_START_REQUEST):
            if self.md_running:
                log(LOG_INFO, f"{msg.name} with the modem already started; ignored")
            else:
                self.boot()
        elif msg.msg == MON_EXCEPTION:
            # O MD fica em EXCEPTION ate alguem parar/reiniciar; nao reiniciamos sozinhos
            # (ver o EE primeiro). O caminho de WDT do kernel manda RESET_REQUEST.
            log(LOG_ERR, "modem exception")
            self.md_ready = False
            self.log_exception_info()
            self._status("modem exception (see the journal and dmesg)")
        else:
            log(LOG_WARNING, f"monitor message {msg.name} not handled")

    def periodic(self) -> None:
        now = time.monotonic()
        if now - self.last_periodic < 60:
            return
        self.last_periodic = now
        if self.md_ready:
            offset = time.time() - now
            if not self.time_synced or abs(offset - self.clock_offset) > 5:
                if self.time_synced:
                    log(LOG_INFO, "wall clock jumped; sending the time to the modem again")
                self.time_sync()
        if (now - self.last_summary >= 300
                and self.fs.stats.requests != self.last_summary_count):
            self.last_summary = now
            self.last_summary_count = self.fs.stats.requests
            log(LOG_INFO, self.fs.stats.summary())

    def loop(self) -> None:
        self.last_periodic = self.last_summary = time.monotonic()
        while not self.stop_requested:
            item = self.deferred.pop(0) if self.deferred else self._next_event(1.0)
            if item:
                self.dispatch(item)
            self.periodic()

    def shutdown(self) -> None:
        """Parada limpa: DO_STOP_MD e esperar GATED antes de fechar; o monitor por ultimo
        (fecha-lo dispara force_md_stop no kernel). Nunca deixar o MD ligado num reboot a
        quente: o MT6895 viu o handshake seguinte travar e o watchdog reiniciar."""
        if self.md_running and "monitor" in self.fds:
            self.stop_md()
        self.threads_stop.set()
        with self.fs.lock:
            n = self.fs.close_all()
        if n:
            log(LOG_INFO, f"fs: closed {n} handle(s) on exit")
        if self.fs.stats.requests:
            log(LOG_INFO, self.fs.stats.summary())
        for name in ("ipc5", "rpc", "sta", "fs", "monitor"):
            fd = self.fds.pop(name, None)
            if fd is None:
                continue
            try:
                self.dev.close(fd)
            except OSError as e:
                log(LOG_WARNING, f"close {name}: {e.strerror}")
        if self.capture:
            self.capture.close()

    def run(self) -> int:
        check_nv_storage(self.cfg)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        try:
            self.open_devices()
            self.start_threads()
            self.log_kernel_info()
            self.boot()
            self.loop()
        except StopRequested:
            pass
        finally:
            self.shutdown()
        log(LOG_INFO, "thunder-modemd stopped")
        return EX_OK


# --- comandos ----------------------------------------------------------------------------
def cmd_run(cfg: Config, _args: argparse.Namespace) -> int:
    return Modemd(cfg).run()


def cmd_nv_seed(cfg: Config, args: argparse.Namespace) -> int:
    return nv_seed(cfg, args.nvdata, args.protect1, args.protect2, args.nvcfg)


def cmd_state(_cfg: Config, _args: argparse.Namespace, dev: Dev | None = None) -> int:
    dev = dev or Dev()
    text = dev.read_text(SYS_BOOT)
    s = parse_sys_boot(text)
    if text is None:
        print(f"{SYS_BOOT}: absent")
    else:
        print(f"{SYS_BOOT}: {text or '(empty: MD1 disabled)'}"
              + (f" ({SYS_STATE_NAMES.get(s, '?')})" if s is not None else ""))
    for path in DEV_IOCTL:
        try:
            fd = dev.open(path, os.O_RDWR)
        except OSError as e:
            if e.errno in (errno.EBUSY, errno.ENOENT):
                continue
            print(f"{path}: {e.strerror}")
            return EX_UNAVAILABLE
        try:
            u = ioctl_u32(dev, fd, CCCI_IOC_GET_MD_STATE)
        except OSError as e:
            print(f"GET_MD_STATE via {path}: {e.strerror}")
            return EX_UNAVAILABLE
        finally:
            dev.close(fd)
        print(f"GET_MD_STATE via {path}: {u} ({USER_STATE_NAMES.get(u, '?')})")
        return EX_OK
    print("GET_MD_STATE: no free /dev/ccci_ioctl0..4")
    return EX_UNAVAILABLE


COMMANDS = {"run": cmd_run, "nv-seed": cmd_nv_seed, "state": cmd_state}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="thunder-modemd", description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=os.environ.get("THUNDER_MODEMD_CONF", CONFIG_PATH))
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="the service")
    seed = sub.add_parser("nv-seed", help="copy the modem NV dirs into MODEM_NV_ROOT")
    seed.add_argument("nvdata")
    seed.add_argument("protect1")
    seed.add_argument("protect2")
    seed.add_argument("nvcfg", nargs="?")
    sub.add_parser("state", help="print the modem state without starting it")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return EX_USAGE if e.code else EX_OK
    try:
        cfg = load_config(args.config)
        return COMMANDS[args.command](cfg, args)
    except Failure as e:
        log(LOG_ERR, str(e))
        return e.code


if __name__ == "__main__":
    sys.exit(main())
