#!/usr/bin/env python3
"""thunder-speechd: audio de chamada (voz CS) do MD1 do MT6833, no lugar do HAL MediaTek.

Faz tres coisas que o audio HAL do Android faz numa chamada:

  /dev/ccci_aud   dono unico do canal de speech do MD (CCCI_PCM_TX 5 / RX 4): le as
                  mensagens do MD (mailbox e payload), responde as que esperam ack
                  (MD Alive 0xAFA0 -> 0x2FA0, EPOF 0xAF78 -> 0x2F78, codec 0xAF90 ->
                  0x2F90...) e manda SPH_ON (0x2F20, sph_info_t) / SPH_OFF (0x2F21)
  hw:0,N          a FE hostless "Hostless_Speech" (N achado em /proc/asound, em geral 23)
                  aberta nas duas direcoes so com ioctls de controle (HW_PARAMS,
                  SW_PARAMS, PREPARE, START, DROP): liga o clock da interface PCM 2 (MD1)
                  ao ADDA. NUNCA read/write/mmap de dados nem STATUS/DELAY/HWSYNC/avail:
                  a FE nao tem buffer DMA e o pointer() indexa o memif fora do vetor
                  (oops do kernel; VOICE-AUDIO 3.3)
  controlC0       as 4 chaves de interconexao MD1 <-> ADDA (ADDA_DL_CHx PCM_2_CAP_CH1,
                  PCM_2_PB_CHx ADDA_UL_CHx), ligadas antes do hw:0,N e desligadas depois.
                  A saida analogica (receptor/viva-voz) fica com o verbo UCM "Voice Call"

Gatilho: a propriedade State dos objetos org.freedesktop.ModemManager1.Call (busctl
monitor no barramento do sistema), ou a mao pelo socket de controle.

Comandos:

  thunder-speechd run [--ack-timeout S] [--no-modemmanager] [--early-media] [--no-inhibit]
                          o servico (Type=notify; SIGTERM desliga tudo e sai)
  thunder-speechd on      liga a voz (rotas, hw:0,N, SPH_ON) no daemon que esta rodando
  thunder-speechd off     desliga
  thunder-speechd status  estado do daemon
  thunder-speechd mute on|off
                          mudo do microfone para a rede (0x2F02)
  thunder-speechd gain DL_DB UL_DB
                          ganho do MD (0x2F00 em dB, so atenuacao; 0x2F01 em passos de
                          0,25 dB); vale na hora se a voz esta ligada, senao na proxima

O socket de controle e /run/thunder-speechd/ctl (so root). Log no journal (stderr com
prefixo de prioridade <N>); cada mensagem do ccci_aud, nos dois sentidos, sai em debug com
id, nome, parametros e hex. Codigos de saida do sysexits.h.

Fonte dos fatos: build/xiaomi-thunder/modem-research/VOICE-AUDIO.md (kernel e HAL citados
la por arquivo:linha e endereco). [INF] marca o que la tambem e inferencia.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import os
import queue
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

# --- codigos de saida (sysexits.h) ------------------------------------------------
EX_OK = 0
EX_USAGE = 64        # linha de comando
EX_UNAVAILABLE = 69  # sem /dev/ccci_aud, sem o PCM Hostless_Speech, sem as chaves; daemon fora
EX_SOFTWARE = 70     # bug nosso; ABI que nao e LP64
EX_TEMPFAIL = 75     # outra instancia (flock) ou outro dono do ccci_aud (EBUSY)
EX_PROTOCOL = 76     # o daemon respondeu algo que o cliente nao entende
EX_NOPERM = 77       # run sem root

RUN_DIR = "/run/thunder-speechd"
CTL_NAME = "ctl"
LOCK_NAME = "lock"
# Presente enquanto a voz esta ligada. Se existe na partida, a instancia anterior morreu
# com o MD em speech on: mandamos SPH_OFF antes de tudo (o kernel ja fechou os PCMs dela).
# A unit usa RuntimeDirectoryPreserve=restart para ele sobreviver ao Restart=.
MARK_NAME = "speech-on"

DEV_CCCI_AUD = "/dev/ccci_aud"
DEV_SND = "/dev/snd"
PROC_ASOUND = "/proc/asound"
DEFAULT_CARD = 0
DEFAULT_ACK_TIMEOUT = 5.0     # o HAL espera ate 60 s (alock_wait_ms(60000), HAL@0x10d1bc)
OFF_ACK_TIMEOUT = 2.0         # acks no caminho de desligar: nao segurar a parada
CTL_REPLY_MARGIN = 10.0


class Failure(Exception):
    """Erro com codigo de saida e mensagem para o journal."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class StopRequested(Exception):
    """SIGTERM/SIGINT durante uma espera: sair pelo caminho de parada limpa."""


class SpeechError(Exception):
    """Falha de uma operacao de voz (vira resposta de erro no socket e log)."""


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


def hexdump(data: bytes, limit: int = 512) -> str:
    h = data[:limit].hex()
    return h + (f"...(+{len(data) - limit} B)" if len(data) > limit else "")


# --- ioctls ----------------------------------------------------------------------------
def _ioc(direction: int, magic: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (magic << 8) | nr


_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2
_IOC_RW = _IOC_READ | _IOC_WRITE

# eccci: GET_MD_STATE no proprio fd do ccci_aud, como o HAL (HAL@0x10e6bc)
CCCI_IOC_GET_MD_STATE = _ioc(_IOC_READ, ord("C"), 1, 4)
MD_USER_READY = 2
MD_STATE_NAMES = {0: "INVALID/GATED", 1: "BOOTING", 2: "READY", 3: "EXCEPTION"}

# --- protocolo de speech no /dev/ccci_aud (VOICE-AUDIO 2.2) ---------------------------
# Tudo little-endian. O usuario le e grava o cabecalho CCCI de 16 B (PORT_F_USER_HEADER).
# Ao gravar, o kernel reescreve (port_proxy.c:535-546): escrita de 16 B -> data[0] =
# 0xFFFFFFFF; escrita maior -> data[1] = tamanho; nos dois casos channel = 5.
CCCI_MAGIC = 0xFFFFFFFF
CH_PCM_TX = 5                 # AP -> MD
CH_PCM_RX = 4                 # MD -> AP
CH_ASSERT_BIT = 0x80000000
HEADER = struct.Struct("<IIII")
MAILBOX = struct.Struct("<IHHII")            # magic, param16, msg_id, channel, param32
TX_PAYLOAD = struct.Struct("<IIIHHHHH")      # data0, data1, channel, len, id, sync, type, size
RX_PAYLOAD = struct.Struct("<IIIHHHHHHH")    # ... + data_idx, data_total_idx
TX_SYNC = 0xA2A2
RX_SYNC = 0x1234
TX_DATA_MAX = 3434
RX_DATA_MAX = 3430
# Uma leitura = um skb inteiro se o buffer couber (port_dev_read). O maior MD->AP e
# 16+10+3430 = 3456 B (o HAL le com 3456); 4096 deixa folga e nunca parte uma mensagem.
READ_SIZE = 4096

# ids (VOICE-AUDIO 2.5); A2M = 0x2Fxx, M2A = 0xAFxx; o ack do MD e id | 0x8000
MSG_SET_DL_GAIN = 0x2F00
MSG_SET_UL_GAIN = 0x2F01
MSG_SET_UL_MUTE = 0x2F02
MSG_SET_DL_MUTE = 0x2F03
MSG_SPH_ON = 0x2F20
MSG_SPH_OFF = 0x2F21
MSG_SET_SPH_ENH = 0x2F23
MSG_SET_SPEECH_MODE = 0x2F2B
MSG_SET_ENH_MASK = 0x2F2C
MSG_M2A_BGS_DATA_REQUEST = 0xAF51
MSG_M2A_RAT_INFO = 0xAF75
MSG_M2A_EPOF = 0xAF78
MSG_M2A_NETWORK_CODEC_INFO = 0xAF90
MSG_M2A_MD_ALIVE = 0xAFA0

ACK_BIT = 0x8000

# Tipo de sincronia por id (getSyncType, HAL@0x146b84; VOICE-AUDIO 2.4)
A2M_NO_ACK = frozenset({0x2F00, 0x2F01, 0x2F05, 0x2F09, 0x2F13, 0x2F23, 0x2F2C, 0x2F42,
                        0x2F45, 0x2F46, 0x2F47, 0x2F4A, 0x2FB2, 0x2FB3})          # 0x00
A2M_NEED_ACK = frozenset({0x2F02, 0x2F03, 0x2F08, 0x2F0A, 0x2F20, 0x2F21, 0x2F2B, 0x2F30,
                          0x2F31, *range(0x2F3A, 0x2F42), 0x2F43, 0x2F44, 0x2F48, 0x2F49,
                          0x2F79, 0x2F7B, 0x2F80, 0x2FB0, 0x2FB1, 0x2FB4, 0x2FB6,
                          0x2FB7})                                                 # 0x01
M2A_ACKS = frozenset(i | ACK_BIT for i in A2M_NEED_ACK)                            # 0x81
# 0xA0: o MD pede dados (BGS, gravacao, PCM2way, VoIP) e o AP responde com DADOS (0x20).
# So chegam com o recurso ligado, e nao ligamos nenhum: so log, nunca resposta vazia.
M2A_DATA_REQUEST = frozenset({0xAF50, 0xAF51, 0xAF57, 0xAF58, 0xAF59})
# 0xA1: o MD avisa e espera ack (0x21). O HAL responde; tambem so com recurso ligado.
# [INF] os parametros do ack do HAL nao estao na spec: mandamos (0, 0) como nos 0x91.
M2A_NOTIFY_NEED_ACK_DATA = frozenset({0xAF60, 0xAF62, 0xAF64, 0xAF66, 0xAF67, 0xAFB8})
M2A_NOTIFY = frozenset({0xAF70, 0xAF75, 0xAFE4})                                  # 0x90
# 0x91: o MD avisa e espera ack; o ack do AP (0x11) e uma mailbox id & ~0x8000.
# 0x2FA0 vai com (0, 0) (HAL@0x101b30-0x101b54); [INF] (0, 0) tambem nos outros.
M2A_NOTIFY_NEED_ACK = frozenset({0xAF78, 0xAF90, 0xAFA0, 0xAFB5})

MSG_NAMES = {
    MSG_SET_DL_GAIN: "SET_DL_GAIN", MSG_SET_UL_GAIN: "SET_UL_GAIN",
    MSG_SET_UL_MUTE: "SET_UL_MUTE", MSG_SET_DL_MUTE: "SET_DL_MUTE",
    MSG_SPH_ON: "SPH_ON", MSG_SPH_OFF: "SPH_OFF", MSG_SET_SPH_ENH: "SET_SPH_ENH",
    MSG_SET_SPEECH_MODE: "SET_SPEECH_MODE", MSG_SET_ENH_MASK: "SET_ENH_MASK",
    MSG_M2A_BGS_DATA_REQUEST: "BGS_DATA_REQUEST", MSG_M2A_RAT_INFO: "RAT_INFO",
    MSG_M2A_EPOF: "EPOF", MSG_M2A_NETWORK_CODEC_INFO: "NETWORK_CODEC_INFO",
    MSG_M2A_MD_ALIVE: "MD_ALIVE",
    0x2F78: "EPOF_ACK", 0x2F90: "NETWORK_CODEC_INFO_ACK", 0x2FA0: "MD_ALIVE_ACK",
    0x2FB5: "ACK(0xAFB5)",
}
DATA_TYPE_SPH_INFO = 0x19
DATA_TYPE_MD_ALIVE = 0x1A
DATA_TYPE_NETWORK_CODEC = 0x14
DATA_TYPE_NAMES = {DATA_TYPE_SPH_INFO: "sph_info", DATA_TYPE_MD_ALIVE: "md_alive",
                   DATA_TYPE_NETWORK_CODEC: "network_codec_info"}
MD_ALIVE_DATA_SIZE = 32       # processModemAlive exige type 0x1A e 32 B (HAL@0x101a68)


def msg_name(msg_id: int) -> str:
    if msg_id in MSG_NAMES:
        return MSG_NAMES[msg_id]
    if msg_id in M2A_ACKS:
        req = msg_id & ~ACK_BIT
        return f"ACK({MSG_NAMES.get(req, f'0x{req:04X}')})"
    return f"0x{msg_id:04X}"


def ack_for(request_id: int) -> int | None:
    """Id do ack que o MD manda para um pedido nosso, ou None se o pedido nao tem ack
    (isApNeedAck, HAL@0x146cc8; isAckMessageInPairByID: ack = id | 0x8000)."""
    return request_id | ACK_BIT if request_id in A2M_NEED_ACK else None


def reply_for(msg_id: int) -> int | None:
    """Mailbox que o AP manda em resposta a uma mensagem do MD, ou None (so log)."""
    if msg_id in M2A_NOTIFY_NEED_ACK or msg_id in M2A_NOTIFY_NEED_ACK_DATA:
        return msg_id & ~ACK_BIT
    return None


@dataclass
class Message:
    kind: str                 # "mailbox" ou "payload"
    msg_id: int
    p16: int = 0
    p32: int = 0
    data_type: int = 0
    data: bytes = b""
    idx: int = 0
    total: int = 0
    channel: int = 0
    data0: int = 0
    raw: bytes = b""

    def describe(self) -> str:
        if self.kind == "mailbox":
            return (f"mailbox 0x{self.msg_id:04X} {msg_name(self.msg_id)} "
                    f"p16=0x{self.p16:04X} p32=0x{self.p32:08X}")
        t = DATA_TYPE_NAMES.get(self.data_type, "?")
        frag = f" idx={self.idx}/{self.total}" if self.total else ""
        return (f"payload 0x{self.msg_id:04X} {msg_name(self.msg_id)} "
                f"type=0x{self.data_type:02X}({t}) size={len(self.data)}{frag}")


class FrameError(ValueError):
    pass


def encode_mailbox(msg_id: int, p16: int = 0, p32: int = 0) -> bytes:
    """Mailbox AP->MD, 16 B, sem dados (speechMessageToCcciMessage HAL@0x10f0e8)."""
    return MAILBOX.pack(CCCI_MAGIC, p16 & 0xFFFF, msg_id, CH_PCM_TX, p32 & 0xFFFFFFFF)


def encode_payload(msg_id: int, data_type: int, data: bytes) -> bytes:
    """Payload AP->MD: cabecalho CCCI + {sync 0xA2A2, type, size} + dados.
    O HAL poe data1 = N+6; o kernel reescreve com o tamanho da escrita (16+6+N), que e o
    que gravamos aqui para o hex do log bater com o que vai ao MD."""
    n = len(data)
    if n > TX_DATA_MAX:
        raise FrameError(f"payload too large ({n} > {TX_DATA_MAX})")
    return TX_PAYLOAD.pack(0, TX_PAYLOAD.size + n, CH_PCM_TX, n + 6, msg_id, TX_SYNC,
                           data_type, n) + bytes(data)


def encode_md_mailbox(msg_id: int, p16: int = 0, p32: int = 0) -> bytes:
    """Mailbox MD->AP (para testes e simulacao)."""
    return MAILBOX.pack(CCCI_MAGIC, p16 & 0xFFFF, msg_id, CH_PCM_RX, p32 & 0xFFFFFFFF)


def encode_md_payload(msg_id: int, data_type: int, data: bytes, data0: int = 0,
                      idx: int = 1, total: int = 1) -> bytes:
    """Payload MD->AP (para testes e simulacao): cabecalho de 10 B com sync 0x1234."""
    n = len(data)
    return RX_PAYLOAD.pack(data0, RX_PAYLOAD.size + n, CH_PCM_RX | CH_ASSERT_BIT, 10 + n,
                           msg_id, RX_SYNC, data_type, n, idx, total) + bytes(data)


def parse_rx(buf: bytes) -> Message:
    """Mensagem MD->AP como ccciMessageToSpeechMessage (HAL@0x10f26c-0x10f550)."""
    if len(buf) < HEADER.size:
        raise FrameError(f"short read ({len(buf)} B)")
    d0, d1, ch, res = HEADER.unpack_from(buf)
    if d0 == CCCI_MAGIC:
        if len(buf) != HEADER.size:                     # assert em HAL@0x10f29c
            raise FrameError(f"mailbox with {len(buf)} B (must be 16)")
        return Message("mailbox", d1 >> 16, p16=d1 & 0xFFFF, p32=res, channel=ch,
                       data0=d0, raw=bytes(buf))
    if len(buf) < RX_PAYLOAD.size:
        raise FrameError(f"payload shorter than its header ({len(buf)} B)")
    (_d0, size, _ch, _ln, msg_id, sync, dtype, dsize, idx,
     total) = RX_PAYLOAD.unpack_from(buf)
    if sync != RX_SYNC:
        raise FrameError(f"payload 0x{msg_id:04X}: sync 0x{sync:04X} (expected 0x1234)")
    if dsize > RX_DATA_MAX:
        raise FrameError(f"payload 0x{msg_id:04X}: data_size {dsize} > {RX_DATA_MAX}")
    if not size == len(buf) == RX_PAYLOAD.size + dsize:   # HAL@0x10f354-0x10f3d4
        raise FrameError(f"payload 0x{msg_id:04X}: message_size {size}, read {len(buf)}, "
                         f"data_size {dsize}")
    if idx != total:                                       # o HAL nao remonta fragmentos
        raise FrameError(f"payload 0x{msg_id:04X}: fragment {idx}/{total}")
    return Message("payload", msg_id, data_type=dtype,
                   data=bytes(buf[RX_PAYLOAD.size:RX_PAYLOAD.size + dsize]), idx=idx,
                   total=total, channel=ch, data0=d0, raw=bytes(buf))


def parse_tx(buf: bytes) -> Message:
    """Mensagem AP->MD (o que nos mesmos gravamos), para log e testes."""
    if len(buf) < HEADER.size:
        raise FrameError(f"short frame ({len(buf)} B)")
    if len(buf) == HEADER.size:
        _m, p16, msg_id, ch, p32 = MAILBOX.unpack(buf)
        return Message("mailbox", msg_id, p16=p16, p32=p32, channel=ch, raw=bytes(buf))
    if len(buf) < TX_PAYLOAD.size:
        raise FrameError(f"payload shorter than its header ({len(buf)} B)")
    d0, _d1, ch, ln, msg_id, sync, dtype, dsize = TX_PAYLOAD.unpack_from(buf)
    if sync != TX_SYNC or ln != dsize + 6 or len(buf) != TX_PAYLOAD.size + dsize:
        raise FrameError(f"bad AP->MD payload 0x{msg_id:04X}")
    return Message("payload", msg_id, data_type=dtype, data=bytes(buf[TX_PAYLOAD.size:]),
                   channel=ch, data0=d0, raw=bytes(buf))


def parse_md_alive(msg: Message) -> tuple[int, int] | None:
    """(headerMdVersion, mdVersion) do MD Alive (HAL@0x101a90-0x101ad0), ou None."""
    if (msg.kind != "payload" or msg.data_type != DATA_TYPE_MD_ALIVE
            or len(msg.data) != MD_ALIVE_DATA_SIZE):
        return None
    return struct.unpack_from("<HH", msg.data)


# --- sph_info_t (128 B, data_type 0x19; VOICE-AUDIO 2.7) ------------------------------
SPH_INFO_SIZE = 128
# 8k=0, 16k=1, 32k=2, 48k=3 (sph_sample_rate_value_to_enum HAL@0x1468c8). O HAL usa a
# mesma taxa no PCM 2 e no sph_info; MTK_PHONE_CALL_FIXED_48K_SAMPLERATE=yes nesta ROM.
RATE_ENUM = {8000: 0, 16000: 1, 32000: 2, 48000: 3}
CALL_RATE = 48000
EXT_DEV_DEFAULT = 0           # receptor/viva-voz; 6 = fone com fio (troca por 0x2F2B)
ENH_DYNAMIC_CTRL = 2          # 2 | (m&1) | ((m>>3)&4) com m=0 (HAL@0x109178) [INF]
SPEECH_COMMON_PARA = (6, 0xDABD, 0x7918, 0x2A00, 0x8001, 0, 0, 0, 0, 0, 0, 0)
SMARTPA_CONFIG = 1


def sph_info(rate: int = CALL_RATE, ext_dev: int = EXT_DEV_DEFAULT) -> bytes:
    """application 0 (normal), sem BT, param_emi_valid = 0: o MD usa os parametros
    padrao dele (o fallback do proprio HAL, "md use default parameter", 2.6). mute_info
    fica 0: o mudo vai depois pela 0x2F02 [INF: semantica do campo nao conhecida]."""
    if rate not in RATE_ENUM:
        raise ValueError(f"rate {rate} not in {sorted(RATE_ENUM)}")
    b = bytearray(SPH_INFO_SIZE)
    b[0] = 0                                  # application: normal
    b[1] = 0                                  # bt_info
    b[2] = RATE_ENUM[rate]                    # sample_rate_enum
    b[3] = 0                                  # opendsp
    b[4] = 0                                  # param_path
    b[5] = 0                                  # param_emi_valid
    struct.pack_into("<HHH", b, 6, 0, 0, ext_dev)   # param_size, param_index, ext_dev_info
    # 12 loopback_flag, 13 loopback_delay, 14 echo_ref_delay_ms, 16 mic_delay_ms,
    # 17 mute_info: todos 0
    struct.pack_into("<H", b, 18, ENH_DYNAMIC_CTRL)
    struct.pack_into("<II", b, 20, 0, 0)            # usip_param_size, usip_param_index
    struct.pack_into("<12H", b, 28, *SPEECH_COMMON_PARA)
    # 52..83 debug_info: 0 (evita o MD empurrar log EPL) [INF]
    b[84] = SMARTPA_CONFIG
    return bytes(b)


def s16(value: int) -> int:
    """Inteiro com sinal no campo u16 da mailbox."""
    if not -0x8000 <= value <= 0x7FFF:
        raise ValueError(f"{value} does not fit in s16")
    return value & 0xFFFF


# Faixas aceitas para o ganho do MD. [INF] unidade do DL em dB (ApplyMdDlGain passa
# -degradeDb, HAL@0x117d50): so atenuacao, nunca ganho no ouvido. UL em passos de
# 0,25 dB (ApplyMdUlGain: lsl #2, HAL@0x117dd4); a faixa e escolha nossa.
DL_GAIN_RANGE = (-60, 0)
UL_GAIN_RANGE = (-24.0, 24.0)


def gain_params(dl_db: int, ul_db: float) -> tuple[int, int]:
    if not DL_GAIN_RANGE[0] <= dl_db <= DL_GAIN_RANGE[1]:
        raise ValueError(f"downlink gain {dl_db} dB outside {DL_GAIN_RANGE}")
    if not UL_GAIN_RANGE[0] <= ul_db <= UL_GAIN_RANGE[1]:
        raise ValueError(f"uplink gain {ul_db} dB outside {UL_GAIN_RANGE}")
    return s16(int(dl_db)), s16(round(ul_db * 4))


# --- ALSA: ABI do arm64 (LP64) ------------------------------------------------------
# Tamanhos e offsets conferidos com offsetof/sizeof sobre o <sound/asound.h> (LP64; igual
# no x86_64 e no arm64) e com include/uapi/sound/asound.h do kernel 4.19.
_A, _U = ord("A"), ord("U")
HW_PARAMS_SIZE = 608
SW_PARAMS_SIZE = 136
PCM_INFO_SIZE = 288
SNDRV_PCM_IOCTL_INFO = _ioc(_IOC_READ, _A, 0x01, PCM_INFO_SIZE)          # 0x81204101
SNDRV_PCM_IOCTL_HW_PARAMS = _ioc(_IOC_RW, _A, 0x11, HW_PARAMS_SIZE)       # 0xC2604111
SNDRV_PCM_IOCTL_SW_PARAMS = _ioc(_IOC_RW, _A, 0x13, SW_PARAMS_SIZE)       # 0xC0884113
SNDRV_PCM_IOCTL_PREPARE = _ioc(_IOC_NONE, _A, 0x40, 0)                    # 0x4140
SNDRV_PCM_IOCTL_START = _ioc(_IOC_NONE, _A, 0x42, 0)                      # 0x4142
SNDRV_PCM_IOCTL_DROP = _ioc(_IOC_NONE, _A, 0x43, 0)                       # 0x4143
# Os unicos ioctls que este programa faz num PCM. STATUS, DELAY, HWSYNC, SYNC_PTR e
# qualquer read/write/mmap/poll chamam o pointer()/copy_user da FE sem memif: oops.
PCM_ALLOWED_IOCTLS = frozenset({SNDRV_PCM_IOCTL_INFO, SNDRV_PCM_IOCTL_HW_PARAMS,
                                SNDRV_PCM_IOCTL_SW_PARAMS, SNDRV_PCM_IOCTL_PREPARE,
                                SNDRV_PCM_IOCTL_START, SNDRV_PCM_IOCTL_DROP})

# snd_pcm_hw_params: flags u32 @0; masks[3] @4 (8 x u32); mres[5] @100; intervals[12] @260
# ({min, max, bits} de 12 B; bits: openmin 1, openmax 2, integer 4, empty 8); ires[9]
# @404; rmask @512, cmask @516, info @520, msbits @524, rate_num @528, rate_den @532,
# fifo_size u64 @536; reserved[64] @544.
HW_MASK_OFF, HW_MASK_WORDS = 4, 8
HW_INTERVAL_OFF, HW_INTERVAL = 260, struct.Struct("<III")
HW_RMASK_OFF, HW_CMASK_OFF, HW_INFO_OFF = 512, 516, 520
INTERVAL_INTEGER = 4
P_ACCESS, P_FORMAT, P_SUBFORMAT = 0, 1, 2
P_SAMPLE_BITS, P_FRAME_BITS, P_CHANNELS, P_RATE = 8, 9, 10, 11
P_PERIOD_TIME, P_PERIOD_SIZE, P_PERIOD_BYTES, P_PERIODS = 12, 13, 14, 15
P_BUFFER_TIME, P_BUFFER_SIZE, P_BUFFER_BYTES, P_TICK_TIME = 16, 17, 18, 19
ACCESS_RW_INTERLEAVED = 3
FORMAT_S16_LE = 2
SUBFORMAT_STD = 0
U32_MAX = 0xFFFFFFFF
ULONG_MAX = 0xFFFFFFFFFFFFFFFF

# snd_pcm_sw_params: tstamp_mode i32 @0, period_step @4, sleep_min @8, avail_min u64 @16,
# xfer_align @24, start_threshold @32, stop_threshold @40, silence_threshold @48,
# silence_size @56, boundary @64 (saida), proto @72, tstamp_type @76, reserved[56] @80.
SW_PARAMS = struct.Struct("<iII4xQQQQQQQII56x")
SW_BOUNDARY_OFF = 64
SW_STOP_OFF = 40

# snd_pcm_info: device, subdevice, stream, card, id[64], name[80], subname[32],
# dev_class, dev_subclass, subdevices_count, subdevices_avail, sync[16], reserved[64]
PCM_INFO = struct.Struct("<IIii64s80s32siiII16s64s")
STREAM_PLAYBACK, STREAM_CAPTURE = 0, 1

# Parametros do HAL (pcm_config HAL@0x138b44-0x138ba4): 2 canais, S16_LE, periodo 1024,
# 2 periodos, 48 kHz.
PCM_CHANNELS = 2
PCM_PERIOD_SIZE = 1024
PCM_PERIODS = 2

# Controles (snd_ctl_*): elem_id 64 B {numid, iface, device, subdevice, name[44], index};
# elem_list 80 B {offset, space, used, count, pids*, reserved[50]} (+6 de alinhamento);
# elem_info 272 B {id, type @64, access @68, count @72, owner @76, value @80 (union de
# 128 B), dimen @208, reserved @216}; elem_value 1224 B {id, indirect @64, value @72
# (long[128] = 1024 B), tstamp @1096, reserved @1112}.
ELEM_ID = struct.Struct("<IiII44sI")
ELEM_LIST = struct.Struct("<IIIIQ50s6x")
ELEM_INFO_SIZE = 272
ELEM_VALUE_SIZE = 1224
ELEM_VALUE_OFF = 72
SNDRV_CTL_IOCTL_ELEM_LIST = _ioc(_IOC_RW, _U, 0x10, ELEM_LIST.size)       # 0xC0505510
SNDRV_CTL_IOCTL_ELEM_INFO = _ioc(_IOC_RW, _U, 0x11, ELEM_INFO_SIZE)       # 0xC1105511
SNDRV_CTL_IOCTL_ELEM_READ = _ioc(_IOC_RW, _U, 0x12, ELEM_VALUE_SIZE)      # 0xC4C85512
SNDRV_CTL_IOCTL_ELEM_WRITE = _ioc(_IOC_RW, _U, 0x13, ELEM_VALUE_SIZE)     # 0xC4C85513
IFACE_MIXER = 2
ELEM_TYPE_BOOLEAN, ELEM_TYPE_INTEGER = 1, 2
ELEM_VALUES_MAX = 128

# As 4 chaves de interconexao do MD1 (audio_device.xml MD1_TO_ADDA_DL / ADDA_UL_TO_MD1).
# Ficam com o daemon, nao com o UCM: se o PipeWire trocar de verbo no meio da chamada,
# as rotas nao somem debaixo do PCM aberto (VOICE-AUDIO 3.1).
ROUTE_SWITCHES = ("ADDA_DL_CH1 PCM_2_CAP_CH1", "ADDA_DL_CH2 PCM_2_CAP_CH1",
                  "PCM_2_PB_CH1 ADDA_UL_CH1", "PCM_2_PB_CH2 ADDA_UL_CH2")
HOSTLESS_SPEECH = "Hostless_Speech"


def hw_params(rate: int = CALL_RATE, channels: int = PCM_CHANNELS,
              period_size: int = PCM_PERIOD_SIZE, periods: int = PCM_PERIODS) -> bytearray:
    """snd_pcm_hw_params como o pcm_open do tinyalsa: param_init (mascaras cheias,
    intervalos [0, ~0], rmask e info ~0) e depois os valores fixos; o periodo vai como
    minimo (param_set_min), o resto exato com integer."""
    b = bytearray(HW_PARAMS_SIZE)
    for m in range(3):
        for w in range(HW_MASK_WORDS):
            struct.pack_into("<I", b, HW_MASK_OFF + (m * HW_MASK_WORDS + w) * 4, U32_MAX)
    for i in range(12):
        HW_INTERVAL.pack_into(b, HW_INTERVAL_OFF + i * HW_INTERVAL.size, 0, U32_MAX, 0)
    struct.pack_into("<I", b, HW_RMASK_OFF, U32_MAX)
    struct.pack_into("<I", b, HW_CMASK_OFF, 0)
    struct.pack_into("<I", b, HW_INFO_OFF, U32_MAX)

    def set_mask(param: int, bit: int) -> None:
        base = HW_MASK_OFF + param * HW_MASK_WORDS * 4
        for w in range(HW_MASK_WORDS):
            struct.pack_into("<I", b, base + w * 4, 0)
        struct.pack_into("<I", b, base + (bit >> 5) * 4, 1 << (bit & 31))

    def set_int(param: int, value: int) -> None:
        HW_INTERVAL.pack_into(b, interval_off(param), value, value, INTERVAL_INTEGER)

    def set_min(param: int, value: int) -> None:
        off = interval_off(param)
        _mn, mx, bits = HW_INTERVAL.unpack_from(b, off)
        HW_INTERVAL.pack_into(b, off, value, mx, bits)

    set_mask(P_ACCESS, ACCESS_RW_INTERLEAVED)
    set_mask(P_FORMAT, FORMAT_S16_LE)
    set_mask(P_SUBFORMAT, SUBFORMAT_STD)
    set_min(P_PERIOD_SIZE, period_size)
    set_int(P_SAMPLE_BITS, 16)
    set_int(P_FRAME_BITS, 16 * channels)
    set_int(P_CHANNELS, channels)
    set_int(P_PERIODS, periods)
    set_int(P_RATE, rate)
    return b


def interval_off(param: int) -> int:
    return HW_INTERVAL_OFF + (param - P_SAMPLE_BITS) * HW_INTERVAL.size


def hw_interval(b: bytes | bytearray, param: int) -> tuple[int, int]:
    mn, mx, _bits = HW_INTERVAL.unpack_from(b, interval_off(param))
    return mn, mx


def sw_params(buffer_size: int, period_size: int) -> bytearray:
    """stop_threshold = ULONG_MAX: >= boundary, entao o START do playback com o buffer
    vazio passa (snd_pcm_playback_data, pcm.h:861-868) e nada para por xrun. O HAL de 32
    bits consegue o mesmo com 0xFFFFFFFF porque o compat recalcula o boundary (1.3).
    start_threshold = buffer: nunca auto-start (o START e explicito). silence 0: com
    silence_size > 0 o kernel encheria de silencio o dma_area NULL no START."""
    return bytearray(SW_PARAMS.pack(0, 1, 0, period_size, 0, buffer_size, ULONG_MAX,
                                    0, 0, 0, 0, 0))


def parse_pcm_info(b: bytes | bytearray) -> dict:
    (device, subdevice, stream, card, pid, name, subname, _cls, _sub, _cnt, _avail,
     _sync, _res) = PCM_INFO.unpack(bytes(b))
    return {"device": device, "subdevice": subdevice, "stream": stream, "card": card,
            "id": _cstr(pid), "name": _cstr(name), "subname": _cstr(subname)}


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def is_hostless_speech(pcm_id: str, pcm_name: str = "") -> bool:
    """O ASoC nomeia a FE dinamica "<stream_name> (*)" (soc_new_pcm, soc-pcm.c:3188).
    Confere a primeira palavra exata: "Hostless_Sph_Echo_Ref" e outros nao casam."""
    for s in (pcm_id, pcm_name):
        words = s.split()
        if words and words[0] == HOSTLESS_SPEECH:
            return True
    return False


# --- acesso ao sistema (trocado nos testes) -----------------------------------------
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
        # So no fd do ccci_aud. Nunca poll() num PCM (o poll chama o pointer()).
        p = select.poll()
        p.register(fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        return bool(p.poll(int(timeout * 1000)))

    def read_text(self, path: str) -> str | None:
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                return f.read()
        except OSError:
            return None

    def listdir(self, path: str) -> list[str]:
        try:
            return os.listdir(path)
        except OSError:
            return []


def ioctl_u32(dev: Dev, fd: int, request: int) -> int:
    buf = bytearray(4)
    dev.ioctl(fd, request, buf)
    return struct.unpack("<I", buf)[0]


# --- descoberta do PCM Hostless_Speech ----------------------------------------------
@dataclass(frozen=True)
class PcmDevice:
    card: int
    device: int
    pcm_id: str

    def path(self, capture: bool) -> str:
        return f"{DEV_SND}/pcmC{self.card}D{self.device}{'c' if capture else 'p'}"


def parse_proc_info(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip()
    return out


def find_hostless_pcm(card: int = DEFAULT_CARD, dev: Dev | None = None,
                      proc: str = PROC_ASOUND) -> PcmDevice:
    """Acha N por /proc/asound/cardC/pcmN{p,c}/info (id/name com Hostless_Speech), sem
    numero fixo. So le o "info" (snd_pcm_info: nao chama pointer()); nunca sub*/status,
    que com o stream rodando chama o pointer() da FE (oops)."""
    dev = dev or Dev()
    base = f"{proc}/card{card}"
    found: dict[int, dict[str, dict[str, str]]] = {}
    for entry in sorted(dev.listdir(base)):
        m = re.fullmatch(r"pcm(\d+)([pc])", entry)
        if not m:
            continue
        text = dev.read_text(f"{base}/{entry}/info")
        if text is None:
            continue
        info = parse_proc_info(text)
        if is_hostless_speech(info.get("id", ""), info.get("name", "")):
            found.setdefault(int(m.group(1)), {})[m.group(2)] = info
    if not found:
        raise Failure(EX_UNAVAILABLE, f"no {HOSTLESS_SPEECH} PCM in {base}/pcm*/info; "
                                      "refusing to touch any PCM")
    if len(found) > 1:
        raise Failure(EX_UNAVAILABLE, f"{HOSTLESS_SPEECH} matches several PCMs "
                                      f"({sorted(found)}); refusing to guess")
    (device, dirs), = found.items()
    if set(dirs) != {"p", "c"}:
        raise Failure(EX_UNAVAILABLE, f"{HOSTLESS_SPEECH} pcm{device} lacks "
                                      f"{'playback' if 'p' not in dirs else 'capture'}")
    for d, info in dirs.items():
        if info.get("device") not in (None, str(device)) or info.get("card") not in (None, str(card)):
            raise Failure(EX_UNAVAILABLE, f"{base}/pcm{device}{d}/info disagrees with its "
                                          f"path: {info}")
    return PcmDevice(card, device, dirs["p"].get("id", HOSTLESS_SPEECH))


class PcmStream:
    """Um lado do hw:C,N. So os ioctls de PCM_ALLOWED_IOCTLS; nunca dados."""

    def __init__(self, dev: Dev, pcm: PcmDevice, capture: bool) -> None:
        self.dev, self.pcm, self.capture = dev, pcm, capture
        self.name = f"hw:{pcm.card},{pcm.device}{'c' if capture else 'p'}"
        self.fd: int | None = None
        self.configured = False       # HW_PARAMS tentado: so entao o DROP faz sentido
        self.running = False

    def _ioctl(self, request: int, arg: int | bytearray = 0) -> None:
        assert request in PCM_ALLOWED_IOCTLS, hex(request)
        assert self.fd is not None
        self.dev.ioctl(self.fd, request, arg)

    def open(self) -> None:
        # O_NONBLOCK: com outro dono o open volta EBUSY em vez de esperar. Nao muda nada
        # nos ioctls que fazemos (nenhum deles dorme).
        path = self.pcm.path(self.capture)
        self.fd = self.dev.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            buf = bytearray(PCM_INFO_SIZE)
            self._ioctl(SNDRV_PCM_IOCTL_INFO, buf)
            info = parse_pcm_info(buf)
            want = STREAM_CAPTURE if self.capture else STREAM_PLAYBACK
            if (not is_hostless_speech(info["id"], info["name"])
                    or info["device"] != self.pcm.device or info["card"] != self.pcm.card
                    or info["stream"] != want):
                raise SpeechError(f"{path} is not {HOSTLESS_SPEECH} "
                                  f"(SNDRV_PCM_IOCTL_INFO: {info}); not touching it")
        except BaseException:
            self.close()
            raise
        log(LOG_DEBUG, f"{self.name}: opened {path} ({info['id']})")

    def configure(self, rate: int = CALL_RATE) -> None:
        hp = hw_params(rate)
        self.configured = True
        self._ioctl(SNDRV_PCM_IOCTL_HW_PARAMS, hp)
        period = hw_interval(hp, P_PERIOD_SIZE)[0]
        buffer = hw_interval(hp, P_BUFFER_SIZE)[0]
        got_rate = hw_interval(hp, P_RATE)[0]
        if not period or not buffer:
            raise SpeechError(f"{self.name}: HW_PARAMS returned period {period}, "
                              f"buffer {buffer}")
        sp = sw_params(buffer, period)
        self._ioctl(SNDRV_PCM_IOCTL_SW_PARAMS, sp)
        boundary = struct.unpack_from("<Q", sp, SW_BOUNDARY_OFF)[0]
        stop = struct.unpack_from("<Q", sp, SW_STOP_OFF)[0]
        if stop < boundary:
            raise SpeechError(f"{self.name}: stop_threshold {stop} < boundary {boundary}; "
                              "START would fail with EPIPE")
        log(LOG_DEBUG, f"{self.name}: rate {got_rate}, period {period}, buffer {buffer}, "
                       f"boundary {boundary}")

    def prepare(self) -> None:
        # So com o stream parado: PREPARE com ele rodando faz o RESET chamar o pointer().
        assert not self.running
        self._ioctl(SNDRV_PCM_IOCTL_PREPARE)

    def start(self) -> None:
        self._ioctl(SNDRV_PCM_IOCTL_START)
        self.running = True

    def close(self) -> None:
        """DROP (para o stream sem tocar no buffer) e close. Seguro em qualquer estado;
        um PCM que nao passou na conferencia do INFO so e fechado, sem DROP."""
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if self.configured:
                self.dev.ioctl(fd, SNDRV_PCM_IOCTL_DROP, 0)
        except OSError as e:
            if e.errno != errno.EBADFD:        # DROP antes de um HW_PARAMS que falhou
                log(LOG_WARNING, f"{self.name}: DROP: {e.strerror}")
        finally:
            self.configured = False
            self.running = False
            try:
                self.dev.close(fd)
            except OSError as e:
                log(LOG_WARNING, f"{self.name}: close: {e.strerror}")
        log(LOG_DEBUG, f"{self.name}: closed")


# --- controles do mixer -------------------------------------------------------------
@dataclass(frozen=True)
class ElemId:
    numid: int
    iface: int
    device: int
    subdevice: int
    name: str
    index: int

    def pack(self) -> bytes:
        return ELEM_ID.pack(self.numid, self.iface, self.device, self.subdevice,
                            self.name.encode()[:43], self.index)

    @classmethod
    def unpack(cls, raw: bytes) -> ElemId:
        numid, iface, device, sub, name, index = ELEM_ID.unpack(raw[:ELEM_ID.size])
        return cls(numid, iface, device, sub, _cstr(name), index)


class Mixer:
    """Controles por nome em /dev/snd/controlC<card> com os ioctls crus."""

    def __init__(self, dev: Dev, card: int = DEFAULT_CARD) -> None:
        self.dev, self.card = dev, card
        self.path = f"{DEV_SND}/controlC{card}"
        self.fd: int | None = None
        self.ids: list[ElemId] = []

    def open(self) -> None:
        self.fd = self.dev.open(self.path, os.O_RDWR)
        self.ids = self.list_ids()

    def close(self) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            self.dev.close(fd)

    def list_ids(self) -> list[ElemId]:
        assert self.fd is not None
        req = bytearray(ELEM_LIST.pack(0, 0, 0, 0, 0, b""))
        self.dev.ioctl(self.fd, SNDRV_CTL_IOCTL_ELEM_LIST, req)
        count = ELEM_LIST.unpack(req)[3]
        if not count:
            return []
        space = ctypes.create_string_buffer(ELEM_ID.size * count)
        req = bytearray(ELEM_LIST.pack(0, count, 0, 0, ctypes.addressof(space), b""))
        self.dev.ioctl(self.fd, SNDRV_CTL_IOCTL_ELEM_LIST, req)
        used = min(ELEM_LIST.unpack(req)[2], count)
        raw = space.raw
        return [ElemId.unpack(raw[i * ELEM_ID.size:(i + 1) * ELEM_ID.size])
                for i in range(used)]

    def find(self, name: str) -> ElemId:
        hits = [e for e in self.ids if e.name == name and e.iface == IFACE_MIXER]
        if not hits:
            raise SpeechError(f"control '{name}' not found on card {self.card}")
        if len(hits) > 1:
            raise SpeechError(f"control '{name}' is ambiguous ({len(hits)} elements)")
        return hits[0]

    def info(self, eid: ElemId) -> tuple[int, int, int, int]:
        """(type, count, min, max); min/max so para INTEGER (BOOLEAN = 0..1)."""
        assert self.fd is not None
        buf = bytearray(ELEM_INFO_SIZE)
        buf[:ELEM_ID.size] = eid.pack()
        self.dev.ioctl(self.fd, SNDRV_CTL_IOCTL_ELEM_INFO, buf)
        etype, _access, count = struct.unpack_from("<iII", buf, 64)
        mn, mx = struct.unpack_from("<qq", buf, 80)
        if etype == ELEM_TYPE_BOOLEAN:
            mn, mx = 0, 1
        return etype, count, mn, mx

    def read_ints(self, name: str) -> list[int]:
        assert self.fd is not None
        eid = self.find(name)
        etype, count, _mn, _mx = self.info(eid)
        if etype not in (ELEM_TYPE_BOOLEAN, ELEM_TYPE_INTEGER):
            raise SpeechError(f"control '{name}' has type {etype}, not boolean/integer")
        buf = bytearray(ELEM_VALUE_SIZE)
        buf[:ELEM_ID.size] = eid.pack()
        self.dev.ioctl(self.fd, SNDRV_CTL_IOCTL_ELEM_READ, buf)
        count = min(count, ELEM_VALUES_MAX)
        return list(struct.unpack_from(f"<{count}q", buf, ELEM_VALUE_OFF))

    def set_switch(self, name: str, on: bool) -> None:
        """Escreve 0/1 em todos os canais de um controle BOOLEAN (ou INTEGER 0..1)."""
        assert self.fd is not None
        eid = self.find(name)
        etype, count, mn, mx = self.info(eid)
        if etype not in (ELEM_TYPE_BOOLEAN, ELEM_TYPE_INTEGER) or mn > 0 or mx < 1:
            raise SpeechError(f"control '{name}' is not a switch (type {etype}, "
                              f"{mn}..{mx})")
        count = max(1, min(count, ELEM_VALUES_MAX))
        buf = bytearray(ELEM_VALUE_SIZE)
        buf[:ELEM_ID.size] = eid.pack()
        struct.pack_into(f"<{count}q", buf, ELEM_VALUE_OFF, *([1 if on else 0] * count))
        self.dev.ioctl(self.fd, SNDRV_CTL_IOCTL_ELEM_WRITE, buf)
        log(LOG_DEBUG, f"mixer: '{name}' = {int(on)}")

    def set_routes(self, on: bool, names: tuple[str, ...] = ROUTE_SWITCHES,
                   best_effort: bool = False) -> list[str]:
        errors = []
        for name in names:
            try:
                self.set_switch(name, on)
            except (OSError, SpeechError) as e:
                if not best_effort:
                    raise
                errors.append(f"'{name}': {getattr(e, 'strerror', None) or e}")
        return errors


# --- /dev/ccci_aud ---------------------------------------------------------------------
class AckWaiter:
    def __init__(self, ack_id: int) -> None:
        self.ack_id = ack_id
        self.event = threading.Event()
        self.message: Message | None = None


@dataclass
class LinkStats:
    rx: int = 0
    tx: int = 0
    malformed: int = 0
    unknown: int = 0
    md_alive: int = 0
    md_version: tuple[int, int] | None = None
    last_md_alive: float = 0.0
    ids: dict[int, int] = field(default_factory=dict)


class SpeechLink:
    """Dono do /dev/ccci_aud: thread de leitura, respostas automaticas e espera de acks.
    on_event recebe ("md_alive", Message) e ("epof", Message) para a thread principal."""

    def __init__(self, dev: Dev, path: str = DEV_CCCI_AUD, on_event=None) -> None:
        self.dev, self.path = dev, path
        self.on_event = on_event or (lambda *_: None)
        self.fd: int | None = None
        self.send_lock = threading.Lock()
        self.wait_lock = threading.Lock()
        self.waiters: dict[int, AckWaiter] = {}
        self.stats = LinkStats()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def open(self) -> None:
        try:
            self.fd = self.dev.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise Failure(EX_TEMPFAIL, f"open {self.path}: busy (another owner: the "
                                           "ROM audio HAL, a cat, another instance?)") from None
            raise Failure(EX_UNAVAILABLE, f"open {self.path}: {e.strerror}") from None
        log(LOG_DEBUG, f"opened {self.path}")

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(2.0)
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                self.dev.close(fd)
            except OSError as e:
                log(LOG_WARNING, f"close {self.path}: {e.strerror}")

    def md_state(self) -> int | None:
        if self.fd is None:
            return None
        try:
            return ioctl_u32(self.dev, self.fd, CCCI_IOC_GET_MD_STATE)
        except OSError as e:
            log(LOG_WARNING, f"GET_MD_STATE on {self.path}: {e.strerror}")
            return None

    # --- envio ---
    def send(self, frame: bytes) -> None:
        if self.fd is None:
            raise SpeechError(f"{self.path} is not open")
        try:
            msg = parse_tx(frame)
            desc = msg.describe()
        except FrameError as e:          # bug nosso: nunca mandar quadro que nao parseia
            raise SpeechError(f"refusing to send a malformed frame ({e})") from None
        with self.send_lock:
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    n = self.dev.write(self.fd, frame)
                    break
                except OSError as e:
                    if e.errno == errno.EAGAIN and time.monotonic() < deadline:
                        time.sleep(0.01)
                        continue
                    raise SpeechError(f"tx {desc}: {e.strerror}") from None
        self.stats.tx += 1
        log(LOG_DEBUG, f"tx {desc} [{hexdump(frame)}]")
        if n != len(frame):
            raise SpeechError(f"tx {desc}: short write {n}/{len(frame)}")

    def request(self, frame: bytes, timeout: float, abort=None,
                on_sent=None) -> Message | None:
        """Manda e espera o ack (id | 0x8000). None se o tempo acabar. O waiter entra
        antes do envio: o ack pode chegar antes do write voltar. on_sent() roda logo
        depois de o quadro sair (antes da espera)."""
        msg_id = parse_tx(frame).msg_id
        ack_id = ack_for(msg_id)
        if ack_id is None:
            self.send(frame)
            if on_sent is not None:
                on_sent()
            return None
        waiter = AckWaiter(ack_id)
        with self.wait_lock:
            self.waiters[ack_id] = waiter
        try:
            self.send(frame)
            if on_sent is not None:
                on_sent()
            deadline = time.monotonic() + timeout
            while not waiter.event.wait(min(0.2, max(0.0, deadline - time.monotonic()))):
                if abort is not None and abort():
                    raise StopRequested()
                if time.monotonic() >= deadline:
                    log(LOG_WARNING, f"no {msg_name(ack_id)} within {timeout:g} s")
                    return None
            return waiter.message
        finally:
            with self.wait_lock:
                if self.waiters.get(ack_id) is waiter:
                    del self.waiters[ack_id]

    # --- recepcao ---
    def start_reader(self) -> None:
        self.thread = threading.Thread(target=self._reader, name="speechd-ccci", daemon=True)
        self.thread.start()

    def _reader(self) -> None:
        while not self.stop.is_set():
            fd = self.fd
            if fd is None:
                return
            try:
                if not self.dev.readable(fd, 0.5):
                    continue
                buf = self.dev.read(fd, READ_SIZE)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    continue
                if e.errno == errno.EBADF or self.stop.is_set():
                    return
                log(LOG_WARNING, f"{self.path}: read failed: {e.strerror}")
                time.sleep(0.5)
                continue
            if not buf:
                time.sleep(0.05)
                continue
            try:
                self.handle_rx(buf)
            except Exception as e:  # noqa: BLE001 - a thread de leitura nao pode morrer
                log(LOG_ERR, f"{self.path}: handling {hexdump(buf, 64)}: {e!r}")

    def handle_rx(self, buf: bytes) -> None:
        self.stats.rx += 1
        try:
            msg = parse_rx(buf)
        except FrameError as e:
            self.stats.malformed += 1
            log(LOG_WARNING, f"rx malformed ({e}): [{hexdump(buf)}]")
            return
        self.stats.ids[msg.msg_id] = self.stats.ids.get(msg.msg_id, 0) + 1
        log(LOG_DEBUG, f"rx {msg.describe()} [{hexdump(buf)}]")
        mid = msg.msg_id
        if mid in M2A_ACKS:
            with self.wait_lock:
                waiter = self.waiters.get(mid)
            if waiter is None:
                log(LOG_NOTICE, f"rx {msg_name(mid)} with nobody waiting (late?)")
            else:
                waiter.message = msg
                waiter.event.set()
            return
        reply = reply_for(mid)
        if mid == MSG_M2A_MD_ALIVE:
            self._md_alive(msg)
        elif mid == MSG_M2A_EPOF:
            log(LOG_NOTICE, "modem says EPOF")
        elif mid == MSG_M2A_NETWORK_CODEC_INFO:
            log(LOG_INFO, f"network codec info (type 0x{msg.data_type:02X}, "
                          f"{len(msg.data)} B)")
        elif mid in M2A_NOTIFY_NEED_ACK:
            log(LOG_INFO, f"rx {msg.describe()}")
        elif mid in M2A_NOTIFY_NEED_ACK_DATA:
            log(LOG_NOTICE, f"rx {msg.describe()}: data notify for a feature we never "
                            f"turn on; acking with (0, 0) [{hexdump(buf, 64)}]")
        elif mid in M2A_DATA_REQUEST:
            log(LOG_NOTICE, f"rx {msg.describe()}: the modem asks for data we do not "
                            f"provide; not answered [{hexdump(buf, 64)}]")
        elif mid in M2A_NOTIFY:
            log(LOG_INFO, f"rx {msg.describe()}")
        else:
            self.stats.unknown += 1
            log(LOG_NOTICE, f"rx unknown {msg.describe()}; not answered [{hexdump(buf)}]")
        if reply is not None:
            try:
                self.send(encode_mailbox(reply, 0, 0))
            except SpeechError as e:
                log(LOG_ERR, f"cannot answer {msg_name(mid)}: {e}")
        if mid == MSG_M2A_MD_ALIVE:
            self.on_event("md_alive", msg)
        elif mid == MSG_M2A_EPOF:
            self.on_event("epof", msg)

    def _md_alive(self, msg: Message) -> None:
        self.stats.md_alive += 1
        self.stats.last_md_alive = time.time()
        ver = parse_md_alive(msg)
        if ver is None:
            # O HAL exige payload type 0x1A de 32 B. Respondemos mesmo assim: o MD
            # esperando um ack que nao vem e pior que um ack para um MD Alive estranho.
            log(LOG_WARNING, f"MD Alive with unexpected shape ({msg.describe()}); acking "
                             "anyway")
        else:
            self.stats.md_version = ver
            log(LOG_INFO, f"MD Alive: headerMdVersion 0x{ver[0]:04X}, mdVersion "
                          f"0x{ver[1]:04X}; answering 0x2FA0")


# --- inibidor de suspensao durante a chamada -----------------------------------------
class Inhibitor:
    """Um inibidor "sleep" do logind (systemd-inhibit ... sleep infinity) enquanto a
    voz esta ligada: suspender no meio da chamada derruba o PCM 2. [INF]"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.proc: subprocess.Popen | None = None

    def acquire(self) -> None:
        if not self.enabled or (self.proc and self.proc.poll() is None):
            return
        exe = shutil.which("systemd-inhibit")
        if not exe:
            log(LOG_WARNING, "systemd-inhibit not found; the phone may suspend mid-call")
            return
        try:
            self.proc = subprocess.Popen(
                [exe, "--what=sleep", "--who=thunder-speechd", "--why=voice call",
                 "--mode=block", "sleep", "infinity"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, close_fds=True)
        except OSError as e:
            log(LOG_WARNING, f"systemd-inhibit: {e.strerror}")

    def release(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# --- controle da voz ----------------------------------------------------------------
class Speech:
    """Liga e desliga a voz do MD na ordem do HAL. Tudo roda na thread principal."""

    def __init__(self, link: SpeechLink, mixer: Mixer, pcm: PcmDevice, dev: Dev,
                 ack_timeout: float = DEFAULT_ACK_TIMEOUT, inhibitor: Inhibitor | None = None,
                 mark_path: str | None = None, abort=None) -> None:
        self.link, self.mixer, self.pcm, self.dev = link, mixer, pcm, dev
        self.ack_timeout = ack_timeout
        self.inhibitor = inhibitor or Inhibitor(False)
        self.mark_path = mark_path
        self.abort = abort
        self.on = False
        self.streams: list[PcmStream] = []
        self.routes_on = False
        self.ul_mute = False
        self.dl_gain = 0
        self.ul_gain = 0.0
        self.last_error = ""
        self.sph_sent = False         # SPH_ON saiu (com ou sem ack) e nao houve SPH_OFF

    # --- partes ---
    def _check_md(self) -> None:
        st = self.link.md_state()
        if st is not None and st != MD_USER_READY:
            raise SpeechError(f"modem is {MD_STATE_NAMES.get(st, st)}, not READY")

    def _open_pcm_path(self) -> None:
        """Rotas AFE antes do hw:0,N (sem BE ligado o hw_params da FE DPCM da EINVAL),
        depois open + HW/SW_PARAMS + PREPARE da saida e da entrada, depois START das duas
        (modemPcmOpen / modemPcmStart, HAL@0x138be8-0x1393ec)."""
        self.routes_on = True             # antes: um erro no meio ainda desliga as que foram
        self.mixer.set_routes(True)
        for capture in (False, True):
            s = PcmStream(self.dev, self.pcm, capture)
            s.open()
            self.streams.append(s)
            s.configure(CALL_RATE)
            s.prepare()
        for s in self.streams:
            s.start()
        log(LOG_INFO, f"PCM path up: routes on, hw:{self.pcm.card},{self.pcm.device} "
                      f"playback+capture started at {CALL_RATE} Hz")

    def _close_pcm_path(self) -> list[str]:
        errors = []
        for s in self.streams:
            try:
                s.close()
            except OSError as e:
                errors.append(f"{s.name}: {e.strerror}")
        self.streams = []
        if self.routes_on:
            errors += self.mixer.set_routes(False, best_effort=True)
            self.routes_on = False
        return errors

    def _md_on(self) -> None:
        """SPH_ON e espera o 0xAF20; depois mudo e ganhos (VOICE-AUDIO 2.8 passo 3)."""
        def sent() -> None:
            self.sph_sent = True

        ack = self.link.request(encode_payload(MSG_SPH_ON, DATA_TYPE_SPH_INFO,
                                               sph_info(CALL_RATE)),
                                self.ack_timeout, self.abort, on_sent=sent)
        if ack is None:
            raise SpeechError(f"SPH_ON not acknowledged within {self.ack_timeout:g} s")
        self._mutes(self.ul_mute)
        self._gains()

    def _mutes(self, ul_mute: bool) -> None:
        for mid, value in ((MSG_SET_UL_MUTE, ul_mute), (MSG_SET_DL_MUTE, False)):
            if self.link.request(encode_mailbox(mid, int(value)), self.ack_timeout,
                                 self.abort) is None:
                # Nao desfaz a chamada: a voz esta ligada, so o mudo ficou incerto.
                log(LOG_WARNING, f"{msg_name(mid)} {int(value)} not acknowledged")

    def _gains(self) -> None:
        dl, ul = gain_params(self.dl_gain, self.ul_gain)
        self.link.send(encode_mailbox(MSG_SET_DL_GAIN, dl))
        self.link.send(encode_mailbox(MSG_SET_UL_GAIN, ul))

    def _sph_off(self, timeout: float) -> str | None:
        try:
            ack = self.link.request(encode_mailbox(MSG_SPH_OFF), timeout)
        except SpeechError as e:
            return f"SPH_OFF: {e}"
        self.sph_sent = False
        return None if ack is not None else f"SPH_OFF not acknowledged within {timeout:g} s"

    def _mark(self, on: bool) -> None:
        if not self.mark_path:
            return
        try:
            if on:
                os.close(os.open(self.mark_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                                 | os.O_CLOEXEC, 0o600))
            elif os.path.lexists(self.mark_path):
                os.unlink(self.mark_path)
        except OSError as e:
            log(LOG_WARNING, f"{self.mark_path}: {e.strerror}")

    # --- API ---
    def prepare(self) -> None:
        """Chamada tocando ou discando: so confere que da para ligar (MD READY); nao
        muda nada no hardware."""
        self._check_md()
        log(LOG_INFO, "call coming up; speech starts when it is ACTIVE")

    def speech_on(self) -> None:
        if self.on:
            log(LOG_INFO, "speech already on")
            return
        log(LOG_NOTICE, "speech on")
        try:
            self._check_md()
            # Ordem do HAL (open(): modemPcmOpen ... modemPcmStart ... SpeechOn): o PCM 2
            # tem de estar com clock antes do SPH_ON [INF: o MD sem amostras pode dar
            # assert], entao caminho PCM primeiro e SPH_ON depois.
            self._open_pcm_path()
            self._md_on()
        except BaseException as e:
            self.last_error = str(e) or type(e).__name__
            log(LOG_ERR, f"speech on failed: {self.last_error}; rolling back")
            self._rollback()
            if isinstance(e, OSError):
                raise SpeechError(f"speech on: {e.strerror or e}") from None
            raise
        self.on = True
        self.last_error = ""
        self._mark(True)
        self.inhibitor.acquire()
        log(LOG_NOTICE, "speech is on")

    def _rollback(self) -> None:
        """SPH_ON sem ack (ou falha no meio): SPH_OFF primeiro (so se o SPH_ON chegou a
        sair), para o MD largar o PCM 2 antes de o clock sumir; depois fechar o PCM e
        desligar as rotas."""
        errors = []
        if self.sph_sent:
            err = self._sph_off(OFF_ACK_TIMEOUT)
            if err:
                errors.append(err)
        errors += self._close_pcm_path()
        for e in errors:
            log(LOG_WARNING, f"rollback: {e}")

    def speech_off(self) -> list[str]:
        """Ordem do close() do HAL: mudo do UL, DROP/close dos PCMs, rotas, SPH_OFF.
        Cada passo segue mesmo se o anterior falhar."""
        if not self.on and not self.streams and not self.routes_on:
            return []
        log(LOG_NOTICE, "speech off")
        errors = []
        was_on = self.on or self.sph_sent
        if was_on:
            try:
                if self.link.request(encode_mailbox(MSG_SET_UL_MUTE, 1),
                                     OFF_ACK_TIMEOUT) is None:
                    errors.append("SET_UL_MUTE 1 not acknowledged")
            except SpeechError as e:
                errors.append(str(e))
        errors += self._close_pcm_path()
        if was_on:
            err = self._sph_off(max(OFF_ACK_TIMEOUT, min(self.ack_timeout, 10.0)))
            if err:
                errors.append(err)
        self.on = False
        self._mark(False)
        self.inhibitor.release()
        for e in errors:
            log(LOG_WARNING, f"speech off: {e}")
        log(LOG_NOTICE, "speech is off" + (f" ({len(errors)} error(s))" if errors else ""))
        return errors

    def mute_ul(self, mute: bool) -> None:
        self.ul_mute = mute
        if self.on and self.link.request(encode_mailbox(MSG_SET_UL_MUTE, int(mute)),
                                         self.ack_timeout, self.abort) is None:
            raise SpeechError(f"SET_UL_MUTE {int(mute)} not acknowledged")
        log(LOG_INFO, f"uplink mute {'on' if mute else 'off'}"
                      + ("" if self.on else " (applies at the next speech on)"))

    def set_gains(self, dl_db: int, ul_db: float) -> None:
        gain_params(dl_db, ul_db)               # valida antes de guardar
        self.dl_gain, self.ul_gain = dl_db, ul_db
        if self.on:
            self._gains()
        log(LOG_INFO, f"modem gains: downlink {dl_db} dB, uplink {ul_db:g} dB"
                      + ("" if self.on else " (applies at the next speech on)"))

    def md_restarted(self) -> None:
        """MD Alive com a voz ligada: o MD reiniciou e perdeu o estado (2.8 passo 6).
        O caminho PCM do AP continua de pe; refaz so o lado do MD. [INF]"""
        if not self.on:
            return
        log(LOG_WARNING, "modem restarted during a call; sending SPH_ON again")
        try:
            self._md_on()
        except SpeechError as e:
            self.last_error = str(e)
            log(LOG_ERR, f"speech on after modem restart failed: {e}; turning off")
            self.speech_off()

    def cleanup_stale(self) -> None:
        """Na partida: chaves de rota ligadas ou a marca de voz ligada = instancia
        anterior morreu no meio de uma chamada."""
        stale = []
        for name in ROUTE_SWITCHES:
            try:
                if any(self.mixer.read_ints(name)):
                    stale.append(name)
            except (OSError, SpeechError) as e:
                log(LOG_WARNING, f"reading '{name}': {getattr(e, 'strerror', None) or e}")
        if stale:
            log(LOG_NOTICE, f"routes left on by a previous run: {stale}; turning off")
            for e in self.mixer.set_routes(False, best_effort=True):
                log(LOG_WARNING, f"stale routes: {e}")
        if self.mark_path and os.path.lexists(self.mark_path):
            log(LOG_NOTICE, "a previous run died with speech on; sending SPH_OFF")
            err = self._sph_off(OFF_ACK_TIMEOUT)
            if err:
                log(LOG_WARNING, err)
            self._mark(False)


# --- estado das chamadas (ModemManager) ---------------------------------------------
MM_NAME = "org.freedesktop.ModemManager1"
MM_PATH = "/org/freedesktop/ModemManager1"
MM_VOICE_IFACE = "org.freedesktop.ModemManager1.Modem.Voice"
MM_CALL_IFACE = "org.freedesktop.ModemManager1.Call"
# MMCallState
CALL_UNKNOWN, CALL_DIALING, CALL_RINGING_OUT, CALL_RINGING_IN = 0, 1, 2, 3
CALL_ACTIVE, CALL_HELD, CALL_WAITING, CALL_TERMINATED = 4, 5, 6, 7
CALL_STATE_NAMES = {0: "UNKNOWN", 1: "DIALING", 2: "RINGING_OUT", 3: "RINGING_IN",
                    4: "ACTIVE", 5: "HELD", 6: "WAITING", 7: "TERMINATED"}
WANT_OFF, WANT_PREPARE, WANT_ON = "off", "prepare", "on"


def desired_speech(states: list[int], early_media: bool = False) -> str:
    """ACTIVE (ou HELD, a chamada continua no MD) -> on; RINGING_IN/DIALING/RINGING_OUT/
    WAITING -> prepare; nenhuma chamada ou so TERMINATED -> off. Com early_media,
    DIALING/RINGING_OUT ja ligam (o ringback da rede vem pelo canal de voz, 3.1)."""
    s = set(states)
    if s & {CALL_ACTIVE, CALL_HELD}:
        return WANT_ON
    if early_media and s & {CALL_DIALING, CALL_RINGING_OUT}:
        return WANT_ON
    if s & {CALL_DIALING, CALL_RINGING_OUT, CALL_RINGING_IN, CALL_WAITING}:
        return WANT_PREPARE
    return WANT_OFF


class CallTracker:
    """Transforma o estado agregado das chamadas em acoes, so nas mudancas (borda): um
    "on" manual sem chamada nao e desfeito ate o ModemManager mudar de estado."""

    def __init__(self, early_media: bool = False) -> None:
        self.early_media = early_media
        self.want = WANT_OFF
        self.states: list[int] = []

    def update(self, states: list[int] | None) -> list[str]:
        if states is None:                   # consulta falhou: manter o que esta
            return []
        self.states = list(states)
        want = desired_speech(states, self.early_media)
        old, self.want = self.want, want
        if want == old:
            return []
        if want == WANT_ON:
            return ["on"]
        if want == WANT_PREPARE:
            return ["off", "prepare"] if old == WANT_ON else ["prepare"]
        return ["off"] if old == WANT_ON else []


def _unwrap(v):
    """Valor de variante do busctl --json: {"type": sig, "data": valor}."""
    while isinstance(v, dict) and set(v) == {"type", "data"}:
        v = v["data"]
    return v


class Busctl:
    """Consultas ao ModemManager pelo busctl (o systemd esta na imagem). --auto-start=no:
    nunca ativar o ModemManager por D-Bus so por perguntar."""

    BASE = ("busctl", "--system", "--json=short", "--auto-start=no", "--timeout=5")

    def __init__(self, run=subprocess.run) -> None:
        self.run = run

    def _json(self, *args: str):
        try:
            r = self.run([*self.BASE, *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(LOG_WARNING, f"busctl {args[0]}: {e}")
            return None
        if r.returncode != 0:
            log(LOG_DEBUG, f"busctl {' '.join(args)}: {r.stderr.strip()}")
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            log(LOG_WARNING, f"busctl {args[0]}: unparsable output {r.stdout[:200]!r}")
            return None

    def call_states(self) -> list[int] | None:
        """Estados de todas as chamadas; [] sem ModemManager no barramento; None se a
        consulta falhou (quem chama mantem o estado atual)."""
        has = self._json("call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                         "org.freedesktop.DBus", "NameHasOwner", "s", MM_NAME)
        if has is None:
            return None
        data = _unwrap(has)
        if isinstance(data, list):
            data = data[0] if data else False
        if not data:
            return []
        objs = self._json("call", MM_NAME, MM_PATH, "org.freedesktop.DBus.ObjectManager",
                          "GetManagedObjects")
        if objs is None:
            return None
        tree = _unwrap(objs)
        if isinstance(tree, list):
            tree = tree[0] if tree else {}
        if not isinstance(tree, dict):
            return None
        calls = []
        for _path, ifaces in sorted(tree.items()):
            voice = ifaces.get(MM_VOICE_IFACE) if isinstance(ifaces, dict) else None
            if isinstance(voice, dict):
                calls += [c for c in (_unwrap(voice.get("Calls")) or []) if isinstance(c, str)]
        states = []
        for path in calls:
            st = self._json("get-property", MM_NAME, path, MM_CALL_IFACE, "State")
            if st is None:
                return None
            v = _unwrap(st)
            if isinstance(v, list):
                v = v[0] if v else None
            if not isinstance(v, int):
                return None
            states.append(v)
        return states

    MONITOR_MATCHES = (
        (f"type='signal',sender='{MM_NAME}',interface='org.freedesktop.DBus.Properties',"
         f"member='PropertiesChanged',arg0='{MM_CALL_IFACE}'"),
        f"type='signal',sender='{MM_NAME}',interface='{MM_CALL_IFACE}'",
        f"type='signal',sender='{MM_NAME}',interface='{MM_VOICE_IFACE}'",
        (f"type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',"
         f"arg0='{MM_NAME}'"),
    )

    def monitor_argv(self) -> list[str]:
        return ["busctl", "--system", "--json=short", "monitor",
                *(f"--match={m}" for m in self.MONITOR_MATCHES)]


class CallWatcher:
    """Thread com o busctl monitor: cada sinal relevante vira ("mm",) na fila; a thread
    principal reconsulta tudo (Busctl.call_states), entao um sinal perdido nao trava o
    estado. Morreu o busctl: espera 5 s e sobe outro."""

    def __init__(self, events: queue.Queue, busctl: Busctl) -> None:
        self.events, self.busctl = events, busctl
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> bool:
        if not shutil.which("busctl"):
            log(LOG_WARNING, "busctl not found; ModemManager call tracking disabled")
            return False
        self.thread = threading.Thread(target=self._loop, name="speechd-mm", daemon=True)
        self.thread.start()
        return True

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.proc = subprocess.Popen(self.busctl.monitor_argv(), stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             text=True, close_fds=True)
            except OSError as e:
                log(LOG_WARNING, f"busctl monitor: {e.strerror}")
                self.stop.wait(5)
                continue
            self.events.put(("mm",))
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                if self.stop.is_set():
                    break
                if line.strip():
                    self.events.put(("mm",))
            rc = self.proc.wait()
            if not self.stop.is_set():
                log(LOG_WARNING, f"busctl monitor exited ({rc}); restarting in 5 s")
                self.stop.wait(5)

    def close(self) -> None:
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.thread:
            self.thread.join(3)


# --- socket de controle --------------------------------------------------------------
CTL_COMMANDS = ("status", "on", "off", "mute", "gain")


def parse_command(line: str) -> tuple[str, tuple]:
    """Linha do cliente -> (comando, argumentos). ValueError se invalida."""
    words = line.strip().split()
    if not words or words[0] not in CTL_COMMANDS:
        raise ValueError(f"unknown command {line.strip()!r} (known: {', '.join(CTL_COMMANDS)})")
    cmd, args = words[0], words[1:]
    if cmd in ("status", "on", "off"):
        if args:
            raise ValueError(f"{cmd} takes no arguments")
        return cmd, ()
    if cmd == "mute":
        if len(args) != 1 or args[0] not in ("on", "off", "1", "0"):
            raise ValueError("usage: mute on|off")
        return cmd, (args[0] in ("on", "1"),)
    if len(args) != 2:
        raise ValueError("usage: gain DL_DB UL_DB")
    dl, ul = int(args[0]), float(args[1])
    gain_params(dl, ul)
    return cmd, (dl, ul)


class ControlServer:
    """AF_UNIX em RUN_DIR/ctl (0600, pasta 0700) e SO_PEERCRED uid 0. Um pedido por
    conexao: uma linha; resposta "ok"/"error: ..." e linhas de detalhe."""

    def __init__(self, path: str, events: queue.Queue, reply_timeout: float,
                 allow_uid: int = 0) -> None:
        self.path, self.events, self.reply_timeout = path, events, reply_timeout
        self.allow_uid = allow_uid
        self.sock: socket.socket | None = None
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if os.path.lexists(self.path):
            os.unlink(self.path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        old = os.umask(0o177)
        try:
            s.bind(self.path)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        s.listen(4)
        s.settimeout(0.5)
        self.sock = s
        self.thread = threading.Thread(target=self._loop, name="speechd-ctl", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        assert self.sock is not None
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self.stop.is_set():
                    return
                time.sleep(0.2)
                continue
            with conn:
                try:
                    self._serve(conn)
                except OSError as e:
                    log(LOG_DEBUG, f"control: {e}")

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        # Le o pedido antes de qualquer resposta: fechar com dado nao lido manda RST e o
        # cliente perde a mensagem de erro.
        data = b""
        while b"\n" not in data and len(data) < 512:
            chunk = conn.recv(512)
            if not chunk:
                break
            data += chunk
        if uid != self.allow_uid:
            conn.sendall(b"error: root only\n")
            return
        try:
            cmd, args = parse_command(data.decode("utf-8", "replace"))
        except ValueError as e:
            conn.sendall(f"error: {e}\n".encode())
            return
        reply: queue.Queue = queue.Queue(1)
        self.events.put(("ctl", cmd, args, reply))
        try:
            text = reply.get(timeout=self.reply_timeout)
        except queue.Empty:
            text = "error: daemon busy (no reply in time)"
        conn.sendall(text.encode() + b"\n")

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(2)
        if self.sock:
            self.sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def ctl_request(path: str, line: str, timeout: float = 300.0) -> tuple[bool, str]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(line.encode() + b"\n")
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    text = data.decode("utf-8", "replace").rstrip("\n")
    first, _, rest = text.partition("\n")
    if first == "ok":
        return True, rest
    if first.startswith("error"):
        return False, text
    raise ValueError(f"unexpected reply {text[:200]!r}")


# --- servico ---------------------------------------------------------------------------
@dataclass
class Options:
    card: int = DEFAULT_CARD
    ack_timeout: float = DEFAULT_ACK_TIMEOUT
    modemmanager: bool = True
    early_media: bool = False
    inhibit: bool = True
    run_dir: str = RUN_DIR
    ctl_uid: int = 0              # so os testes mudam


class Speechd:
    """Thread principal = unica que mexe na voz. As outras (ccci, controle, ModemManager)
    so enfileiram eventos."""

    def __init__(self, opts: Options, dev: Dev | None = None, notify=sd_notify,
                 busctl: Busctl | None = None) -> None:
        self.opts = opts
        self.dev = dev or Dev()
        self.notify = notify
        self.busctl = busctl or Busctl()
        self.events: queue.Queue = queue.Queue()
        self.stop_requested = False
        self.lock_fd: int | None = None
        self.link = SpeechLink(self.dev, DEV_CCCI_AUD, self._link_event)
        self.mixer = Mixer(self.dev, opts.card)
        self.speech: Speech | None = None
        self.tracker = CallTracker(opts.early_media)
        self.watcher: CallWatcher | None = None
        self.server: ControlServer | None = None
        self.pcm: PcmDevice | None = None
        self.last_resync = 0.0
        self.mm_dirty = False

    def _on_signal(self, _signum: int, _frame: object) -> None:
        self.stop_requested = True       # so a flag: nada de log, lock nem fila aqui

    def _abort(self) -> bool:
        return self.stop_requested

    def _link_event(self, kind: str, msg: Message) -> None:
        self.events.put(("link", kind, msg))

    def _status(self, text: str) -> None:
        self.notify(f"STATUS={text}")

    # --- partida ---
    def take_lock(self) -> None:
        d = self.opts.run_dir
        os.makedirs(d, 0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise Failure(EX_SOFTWARE, f"{d} is not our directory")
        if st.st_mode & 0o077:
            os.chmod(d, 0o700)
        fd = os.open(f"{d}/{LOCK_NAME}", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise Failure(EX_TEMPFAIL, "another thunder-speechd is running") from None
        self.lock_fd = fd

    def setup(self) -> None:
        if struct.calcsize("P") != 8:
            raise Failure(EX_SOFTWARE, "the ALSA ioctl layouts here are LP64 (arm64) only")
        self.take_lock()
        self.pcm = find_hostless_pcm(self.opts.card, self.dev)
        log(LOG_INFO, f"{HOSTLESS_SPEECH} is hw:{self.pcm.card},{self.pcm.device} "
                      f"({self.pcm.pcm_id})")
        try:
            self.mixer.open()
            for name in ROUTE_SWITCHES:
                self.mixer.find(name)
        except OSError as e:
            raise Failure(EX_UNAVAILABLE, f"{self.mixer.path}: {e.strerror}") from None
        except SpeechError as e:
            raise Failure(EX_UNAVAILABLE, str(e)) from None
        self.link.open()
        self.link.start_reader()
        self.speech = Speech(self.link, self.mixer, self.pcm, self.dev, self.opts.ack_timeout,
                             Inhibitor(self.opts.inhibit), f"{self.opts.run_dir}/{MARK_NAME}",
                             self._abort)
        self.speech.cleanup_stale()
        self.server = ControlServer(f"{self.opts.run_dir}/{CTL_NAME}", self.events,
                                    4 * self.opts.ack_timeout + CTL_REPLY_MARGIN,
                                    self.opts.ctl_uid)
        self.server.start()
        if self.opts.modemmanager:
            self.watcher = CallWatcher(self.events, self.busctl)
            if not self.watcher.start():
                self.watcher = None
        st = self.link.md_state()
        log(LOG_INFO, f"ready: modem {MD_STATE_NAMES.get(st, st) if st is not None else '?'}, "
                      f"ModemManager tracking {'on' if self.watcher else 'off'}")
        self.notify("READY=1")
        self._status("speech off")

    # --- laco ---
    def loop(self) -> None:
        while not self.stop_requested:
            try:
                item = self.events.get(timeout=0.5)
            except queue.Empty:
                item = None
            # Esvazia a fila antes do periodic(): uma rajada de sinais do ModemManager
            # vira uma consulta so.
            while item is not None and not self.stop_requested:
                self.dispatch(item)
                try:
                    item = self.events.get_nowait()
                except queue.Empty:
                    item = None
            self.periodic()

    def dispatch(self, item: tuple) -> None:
        kind = item[0]
        if kind == "mm":
            self.mm_dirty = True
        elif kind == "link":
            if item[1] == "md_alive" and self.speech:
                self.speech.md_restarted()
                self._update_status()
        elif kind == "ctl":
            _, cmd, args, reply = item
            reply.put(self.handle_command(cmd, args))

    def periodic(self) -> None:
        now = time.monotonic()
        # Sinais em rajada: uma consulta por volta do laco. Com chamada em curso, uma
        # consulta a cada 15 s pega um sinal perdido.
        busy = self.tracker.want != WANT_OFF
        if self.watcher and (self.mm_dirty or (busy and now - self.last_resync > 15)):
            self.mm_dirty = False
            self.last_resync = now
            self.resync_calls()

    def resync_calls(self) -> None:
        states = self.busctl.call_states()
        if states is not None and states != self.tracker.states:
            log(LOG_INFO, "calls: " + (", ".join(CALL_STATE_NAMES.get(s, str(s))
                                                 for s in states) or "none"))
        for action in self.tracker.update(states):
            self.run_action(action)

    def run_action(self, action: str) -> None:
        assert self.speech is not None
        try:
            if action == "on":
                self.speech.speech_on()
            elif action == "off":
                self.speech.speech_off()
            elif action == "prepare":
                self.speech.prepare()
        except SpeechError as e:
            log(LOG_ERR, f"{action}: {e}")
        self._update_status()

    def _update_status(self) -> None:
        if self.speech:
            self._status("speech on" if self.speech.on else "speech off")

    def handle_command(self, cmd: str, args: tuple) -> str:
        assert self.speech is not None
        try:
            if cmd == "on":
                self.speech.speech_on()
            elif cmd == "off":
                errors = self.speech.speech_off()
                if errors:
                    self._update_status()
                    return "error: " + "; ".join(errors)
            elif cmd == "mute":
                self.speech.mute_ul(args[0])
            elif cmd == "gain":
                self.speech.set_gains(*args)
        except (SpeechError, ValueError) as e:
            self._update_status()
            return f"error: {e}"
        self._update_status()
        return "ok\n" + self.status_text()

    def status_text(self) -> str:
        sp, st = self.speech, self.link.stats
        assert sp is not None and self.pcm is not None
        md = self.link.md_state()
        lines = [
            f"speech: {'on' if sp.on else 'off'}",
            (f"pcm: hw:{self.pcm.card},{self.pcm.device} ({self.pcm.pcm_id}), "
             f"{'running' if sp.streams else 'closed'}"),
            f"routes: {'on' if sp.routes_on else 'off'}",
            f"uplink mute: {'on' if sp.ul_mute else 'off'}",
            f"modem gains: downlink {sp.dl_gain} dB, uplink {sp.ul_gain:g} dB",
            f"modem: {MD_STATE_NAMES.get(md, md) if md is not None else '?'}",
            f"md alive: {st.md_alive}"
            + (f" (version 0x{st.md_version[0]:04X}/0x{st.md_version[1]:04X})"
               if st.md_version else ""),
            f"messages: rx {st.rx}, tx {st.tx}, malformed {st.malformed}, unknown {st.unknown}",
            f"ack timeout: {sp.ack_timeout:g} s",
        ]
        if self.watcher:
            calls = ", ".join(CALL_STATE_NAMES.get(s, str(s)) for s in self.tracker.states)
            lines.append(f"calls (ModemManager): {calls or 'none'} -> {self.tracker.want}")
        else:
            lines.append("calls (ModemManager): not tracked")
        if sp.last_error:
            lines.append(f"last error: {sp.last_error}")
        return "\n".join(lines)

    # --- parada ---
    def shutdown(self) -> None:
        if self.speech:
            try:
                self.speech.speech_off()
            except Exception as e:  # noqa: BLE001 - a parada tem de chegar ao fim
                log(LOG_ERR, f"speech off on exit: {e!r}")
        if self.watcher:
            self.watcher.close()
        if self.server:
            self.server.close()
        # Respostas pendentes no socket nao ficam penduradas ate o timeout.
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if item[0] == "ctl":
                item[3].put("error: thunder-speechd is stopping")
        self.link.close()
        self.mixer.close()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        try:
            self.setup()
            self.loop()
        except StopRequested:
            pass
        finally:
            self.notify("STOPPING=1")
            self.shutdown()
        log(LOG_INFO, "thunder-speechd stopped")
        return EX_OK


# --- comandos ----------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise Failure(EX_NOPERM, "run needs root (/dev/ccci_aud, /dev/snd, /run)")
    if args.ack_timeout <= 0 or args.ack_timeout > 60:
        raise Failure(EX_USAGE, "--ack-timeout must be in (0, 60] seconds")
    opts = Options(card=args.card, ack_timeout=args.ack_timeout,
                   modemmanager=not args.no_modemmanager, early_media=args.early_media,
                   inhibit=not args.no_inhibit)
    return Speechd(opts).run()


def cmd_client(args: argparse.Namespace) -> int:
    line = args.command
    if args.command == "mute":
        line += f" {args.state}"
    elif args.command == "gain":
        line += f" {args.dl} {args.ul}"
    path = f"{RUN_DIR}/{CTL_NAME}"
    try:
        ok, text = ctl_request(path, line)
    except FileNotFoundError:
        raise Failure(EX_UNAVAILABLE, "thunder-speechd is not running (no control "
                                      "socket)") from None
    except PermissionError:
        raise Failure(EX_NOPERM, f"{path}: root only") from None
    except OSError as e:          # recusada, timeout, ...
        raise Failure(EX_UNAVAILABLE, f"{path}: {e}") from None
    except ValueError as e:
        raise Failure(EX_PROTOCOL, str(e)) from None
    if text:
        print(text)
    return EX_OK if ok else EX_UNAVAILABLE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="thunder-speechd", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="the service")
    run.add_argument("--card", type=int, default=DEFAULT_CARD)
    run.add_argument("--ack-timeout", type=float, default=DEFAULT_ACK_TIMEOUT,
                     help="seconds to wait for the modem's acks (default 5)")
    run.add_argument("--no-modemmanager", action="store_true",
                     help="do not follow ModemManager calls (CLI only)")
    run.add_argument("--early-media", action="store_true",
                     help="speech on already in DIALING/RINGING_OUT (network ringback)")
    run.add_argument("--no-inhibit", action="store_true",
                     help="no logind sleep inhibitor while speech is on")
    sub.add_parser("on", help="speech on in the running daemon")
    sub.add_parser("off", help="speech off")
    sub.add_parser("status", help="daemon state")
    mute = sub.add_parser("mute", help="uplink (microphone) mute")
    mute.add_argument("state", choices=("on", "off"))
    gain = sub.add_parser("gain", help="modem gains")
    gain.add_argument("dl", type=int, help="downlink dB (-60..0)")
    gain.add_argument("ul", type=float, help="uplink dB (-24..24, 0.25 steps)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return EX_USAGE if e.code else EX_OK
    try:
        if args.command == "run":
            return cmd_run(args)
        return cmd_client(args)
    except Failure as e:
        log(LOG_ERR, str(e))
        return e.code


if __name__ == "__main__":
    sys.exit(main())
