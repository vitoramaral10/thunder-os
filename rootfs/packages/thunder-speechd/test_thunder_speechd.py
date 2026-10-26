"""Testes do thunder-speechd que rodam no PC, sem o aparelho, o eccci nem o ALSA.

    cd rootfs/packages/thunder-speechd && python3 -m unittest -v
"""
import ctypes
import errno
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thunder_speechd as t

_quiet = mock.patch.object(t, "log")


def setUpModule():
    _quiet.start()          # sem log no terminal; cada teste que confere log tem o seu


def tearDownModule():
    _quiet.stop()


# A mensagem de 58 B que o MD manda no boot (VOICE-AUDIO 2.3).
OBSERVED_MD_ALIVE = bytes.fromhex(
    "345c03043a000000040000802a00a0af34121a0020000100010055aa0030"
    "00000000000000000000000000000000000000000000000000000000")

PCM_N = 23
PROC = "/proc/asound/card0"


def proc_tree(extra: dict | None = None, hostless_id: str = "Hostless_Speech (*)") -> dict:
    """Arquivos de /proc/asound/card0 como o 4.19 escreve (snd_pcm_proc_info_read)."""
    files = {}

    def add(n, d, pid):
        stream = "PLAYBACK" if d == "p" else "CAPTURE"
        files[f"{PROC}/pcm{n}{d}/info"] = (
            f"card: 0\ndevice: {n}\nsubdevice: 0\nstream: {stream}\nid: {pid}\n"
            f"name: \nsubname: subdevice #0\nclass: 0\nsubclass: 0\n"
            f"subdevices_count: 1\nsubdevices_avail: 1\n")

    for n, pid in ((0, "Playback_1 (*)"), (10, "Capture_1 (*)"), (22, "Hostless_FM (*)"),
                   (PCM_N, hostless_id), (24, "Hostless_Sph_Echo_Ref (*)")):
        for d in "pc":
            add(n, d, pid)
    files.update(extra or {})
    return files


class Elem:
    def __init__(self, numid, name, etype=t.ELEM_TYPE_BOOLEAN, count=1, mn=0, mx=1,
                 iface=t.IFACE_MIXER, device=0):
        self.numid, self.name, self.etype, self.count = numid, name, etype, count
        self.mn, self.mx, self.iface, self.device = mn, mx, iface, device
        self.values = [0] * count


class FakeDev:
    """ccci_aud + PCMs + controlC0 + /proc num objeto so. Registra a ordem de tudo em
    self.events e falha o teste em qualquer ioctl de PCM proibido ou leitura/escrita
    de dados num PCM."""

    CCCI_FD, PCM_P_FD, PCM_C_FD, CTL_FD = 10, 20, 21, 30

    def __init__(self, files=None, elems=None, pcm_id="Hostless_Speech (*)"):
        self.files = proc_tree() if files is None else files
        self.elems = elems if elems is not None else [
            Elem(i + 1, n) for i, n in enumerate(t.ROUTE_SWITCHES)] + [
            Elem(10, "Handset_PGA_GAIN", t.ELEM_TYPE_INTEGER + 1, 1, 0, 0)]
        self.pcm_id = pcm_id
        self.events: list[tuple] = []
        self.tx: list[bytes] = []
        self.open_fds: set[int] = set()
        self.md_state = t.MD_USER_READY
        self.auto_ack: set[int] = {t.MSG_SPH_ON, t.MSG_SPH_OFF, t.MSG_SET_UL_MUTE,
                                   t.MSG_SET_DL_MUTE}
        self.link = None             # SpeechLink que recebe os acks automaticos
        self.fail_ioctl: dict[int, int] = {}

    # --- arquivos ---
    def read_text(self, path):
        return self.files.get(path)

    def listdir(self, path):
        pre = path.rstrip("/") + "/"
        return sorted({p[len(pre):].split("/", 1)[0] for p in self.files if p.startswith(pre)})

    # --- nos ---
    def open(self, path, flags):
        fd = {t.DEV_CCCI_AUD: self.CCCI_FD,
              f"{t.DEV_SND}/pcmC0D{PCM_N}p": self.PCM_P_FD,
              f"{t.DEV_SND}/pcmC0D{PCM_N}c": self.PCM_C_FD,
              f"{t.DEV_SND}/controlC0": self.CTL_FD}.get(path)
        if fd is None:
            raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        if fd in self.open_fds:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), path)
        self.open_fds.add(fd)
        if fd in (self.PCM_P_FD, self.PCM_C_FD):
            self.events.append(("pcm-open", self._side(fd)))
        return fd

    def close(self, fd):
        self.open_fds.discard(fd)
        if fd in (self.PCM_P_FD, self.PCM_C_FD):
            self.events.append(("pcm-close", self._side(fd)))

    @staticmethod
    def _side(fd):
        return "p" if fd == FakeDev.PCM_P_FD else "c"

    def readable(self, fd, timeout):
        assert fd == self.CCCI_FD, "poll() on a PCM calls the FE pointer()"
        time.sleep(min(timeout, 0.01))
        return False

    def read(self, fd, n):
        assert fd == self.CCCI_FD, "read() on the hostless PCM: dma_area is NULL"
        raise OSError(errno.EAGAIN, "again")

    def write(self, fd, data):
        assert fd == self.CCCI_FD, "write() on the hostless PCM: dma_area is NULL"
        data = bytes(data)
        self.tx.append(data)
        msg = t.parse_tx(data)
        self.events.append(("tx", t.msg_name(msg.msg_id), msg.p16))
        if msg.msg_id in self.auto_ack and self.link is not None:
            self.link.handle_rx(t.encode_md_mailbox(msg.msg_id | t.ACK_BIT))
        return len(data)

    # --- ioctls ---
    def ioctl(self, fd, request, arg=0):
        if request in self.fail_ioctl:
            e = self.fail_ioctl[request]
            raise OSError(e, os.strerror(e))
        if fd == self.CCCI_FD:
            assert request == t.CCCI_IOC_GET_MD_STATE
            struct.pack_into("<I", arg, 0, self.md_state)
            return 0
        if fd in (self.PCM_P_FD, self.PCM_C_FD):
            return self._pcm_ioctl(fd, request, arg)
        assert fd == self.CTL_FD
        return self._ctl_ioctl(request, arg)

    def _pcm_ioctl(self, fd, request, arg):
        side = self._side(fd)
        assert request in t.PCM_ALLOWED_IOCTLS, f"forbidden PCM ioctl 0x{request:08x}"
        names = {t.SNDRV_PCM_IOCTL_INFO: "INFO", t.SNDRV_PCM_IOCTL_HW_PARAMS: "HW_PARAMS",
                 t.SNDRV_PCM_IOCTL_SW_PARAMS: "SW_PARAMS",
                 t.SNDRV_PCM_IOCTL_PREPARE: "PREPARE", t.SNDRV_PCM_IOCTL_START: "START",
                 t.SNDRV_PCM_IOCTL_DROP: "DROP"}
        self.events.append((names[request], side))
        if request == t.SNDRV_PCM_IOCTL_INFO:
            assert len(arg) == t.PCM_INFO_SIZE
            stream = t.STREAM_PLAYBACK if side == "p" else t.STREAM_CAPTURE
            arg[:] = t.PCM_INFO.pack(PCM_N, 0, stream, 0, self.pcm_id.encode(), b"",
                                     b"subdevice #0", 0, 0, 1, 0, b"", b"")
        elif request == t.SNDRV_PCM_IOCTL_HW_PARAMS:
            assert len(arg) == t.HW_PARAMS_SIZE
            self.last_hw = bytes(arg)
            period = t.hw_interval(arg, t.P_PERIOD_SIZE)[0]
            periods = t.hw_interval(arg, t.P_PERIODS)[0]
            for p, v in ((t.P_PERIOD_SIZE, period), (t.P_BUFFER_SIZE, period * periods)):
                t.HW_INTERVAL.pack_into(arg, t.interval_off(p), v, v, t.INTERVAL_INTEGER)
        elif request == t.SNDRV_PCM_IOCTL_SW_PARAMS:
            assert len(arg) == t.SW_PARAMS_SIZE
            self.last_sw = bytes(arg)
            struct.pack_into("<Q", arg, t.SW_BOUNDARY_OFF, 0x4000000000000000)
        return 0

    def _ctl_ioctl(self, request, arg):
        if request == t.SNDRV_CTL_IOCTL_ELEM_LIST:
            offset, space, _used, _count, pids, _r = t.ELEM_LIST.unpack(arg)
            n = min(space, len(self.elems) - offset)
            raw = b"".join(t.ElemId(e.numid, e.iface, e.device, 0, e.name, 0).pack()
                           for e in self.elems[offset:offset + n])
            if n:
                ctypes.memmove(pids, raw, len(raw))
            arg[:] = t.ELEM_LIST.pack(offset, space, n, len(self.elems), pids, b"")
            return 0
        eid = t.ElemId.unpack(bytes(arg[:t.ELEM_ID.size]))
        elem = next((e for e in self.elems if e.numid == eid.numid), None)
        if elem is None:
            raise OSError(errno.ENOENT, "no such element")
        if request == t.SNDRV_CTL_IOCTL_ELEM_INFO:
            struct.pack_into("<iII", arg, 64, elem.etype, 0, elem.count)
            struct.pack_into("<qq", arg, 80, elem.mn, elem.mx)
        elif request == t.SNDRV_CTL_IOCTL_ELEM_READ:
            struct.pack_into(f"<{elem.count}q", arg, t.ELEM_VALUE_OFF, *elem.values)
        elif request == t.SNDRV_CTL_IOCTL_ELEM_WRITE:
            elem.values = list(struct.unpack_from(f"<{elem.count}q", arg, t.ELEM_VALUE_OFF))
            self.events.append(("mixer", elem.name, elem.values[0]))
        else:
            raise AssertionError(f"unexpected control ioctl 0x{request:08x}")
        return 0


def make_speech(dev=None, ack_timeout=0.3, mark=None):
    dev = dev or FakeDev()
    link = t.SpeechLink(dev)
    dev.link = link
    link.open()
    mixer = t.Mixer(dev)
    mixer.open()
    pcm = t.find_hostless_pcm(0, dev, "/proc/asound")
    sp = t.Speech(link, mixer, pcm, dev, ack_timeout, mark_path=mark)
    return sp, dev


# --- protocolo ---------------------------------------------------------------------
class ObservedMessageTest(unittest.TestCase):
    def test_size(self):
        self.assertEqual(len(OBSERVED_MD_ALIVE), 58)

    def test_parse(self):
        m = t.parse_rx(OBSERVED_MD_ALIVE)
        self.assertEqual(m.kind, "payload")
        self.assertEqual(m.msg_id, t.MSG_M2A_MD_ALIVE)
        self.assertEqual(m.data0, 0x04035C34)
        self.assertEqual(m.channel & 0xFFFF, t.CH_PCM_RX)
        self.assertTrue(m.channel & t.CH_ASSERT_BIT)
        self.assertEqual((m.data_type, len(m.data), m.idx, m.total), (0x1A, 32, 1, 1))
        self.assertEqual(t.parse_md_alive(m), (0xAA55, 0x3000))
        self.assertIn("MD_ALIVE", m.describe())

    def test_reencode_identical(self):
        m = t.parse_rx(OBSERVED_MD_ALIVE)
        self.assertEqual(t.encode_md_payload(m.msg_id, m.data_type, m.data, m.data0),
                         OBSERVED_MD_ALIVE)

    def test_reply_is_2fa0_mailbox(self):
        self.assertEqual(t.reply_for(t.MSG_M2A_MD_ALIVE), 0x2FA0)
        self.assertEqual(t.encode_mailbox(0x2FA0),
                         bytes.fromhex("ffffffff0000a02f0500000000000000"))


class FramingTest(unittest.TestCase):
    def test_mailbox_round_trip(self):
        for mid, p16, p32 in ((0x2F02, 1, 0), (0x2F00, t.s16(-6), 0), (0x2F21, 0, 0xDEADBEEF)):
            raw = t.encode_mailbox(mid, p16, p32)
            self.assertEqual(len(raw), 16)
            m = t.parse_tx(raw)
            self.assertEqual((m.kind, m.msg_id, m.p16, m.p32, m.channel),
                             ("mailbox", mid, p16, p32, t.CH_PCM_TX))
            # O MD manda mailbox com o mesmo layout (canal 4)
            r = t.parse_rx(t.encode_md_mailbox(mid | t.ACK_BIT, p16, p32))
            self.assertEqual((r.kind, r.msg_id, r.p16, r.p32), ("mailbox", mid | 0x8000, p16, p32))

    def test_payload_round_trip(self):
        data = bytes(range(40))
        raw = t.encode_payload(0x2F2B, 0x19, data)
        self.assertEqual(len(raw), 22 + 40)
        d0, d1, ch, ln, mid, sync, dtype, dsize = t.TX_PAYLOAD.unpack_from(raw)
        self.assertEqual((d0, d1, ch, ln, mid, sync, dtype, dsize),
                         (0, 62, 5, 46, 0x2F2B, 0xA2A2, 0x19, 40))
        m = t.parse_tx(raw)
        self.assertEqual((m.kind, m.msg_id, m.data_type, m.data), ("payload", 0x2F2B, 0x19, data))
        r = t.parse_rx(t.encode_md_payload(0xAF90, 0x14, data))
        self.assertEqual((r.msg_id, r.data_type, r.data), (0xAF90, 0x14, data))

    def test_payload_too_large(self):
        with self.assertRaises(t.FrameError):
            t.encode_payload(0x2F20, 0x19, bytes(t.TX_DATA_MAX + 1))

    def test_malformed_rx(self):
        good = bytearray(OBSERVED_MD_ALIVE)
        cases = {
            "short": bytes(8),
            "mailbox not 16": t.encode_md_mailbox(0xAF20) + b"\0",
            "bad sync": bytes(good[:16]) + b"\x35\x12" + bytes(good[18:]),
            "size mismatch": bytes(good[:-1]),
            "fragment": t.encode_md_payload(0xAFA0, 0x1A, bytes(32), idx=1, total=2),
            "header only": bytes(good[:20]),
        }
        for name, raw in cases.items():
            with self.subTest(name), self.assertRaises(t.FrameError):
                t.parse_rx(raw)

    def test_gain_params(self):
        self.assertEqual(t.gain_params(0, 0), (0, 0))
        self.assertEqual(t.gain_params(-6, 1.5), (0xFFFA, 6))
        self.assertEqual(t.gain_params(-60, -24), (0xFFC4, 0xFFA0))
        for dl, ul in ((1, 0), (-61, 0), (0, 24.5)):
            with self.subTest(dl=dl, ul=ul), self.assertRaises(ValueError):
                t.gain_params(dl, ul)


class AckMappingTest(unittest.TestCase):
    def test_requests_with_ack(self):
        self.assertEqual(t.ack_for(t.MSG_SPH_ON), 0xAF20)
        self.assertEqual(t.ack_for(t.MSG_SPH_OFF), 0xAF21)
        self.assertEqual(t.ack_for(t.MSG_SET_UL_MUTE), 0xAF02)
        self.assertEqual(t.ack_for(t.MSG_SET_DL_MUTE), 0xAF03)
        self.assertEqual(t.ack_for(t.MSG_SET_SPEECH_MODE), 0xAF2B)
        for mid in t.A2M_NEED_ACK:
            self.assertEqual(t.ack_for(mid), mid | 0x8000)

    def test_requests_without_ack(self):
        for mid in (t.MSG_SET_DL_GAIN, t.MSG_SET_UL_GAIN, t.MSG_SET_SPH_ENH, 0x2FA0):
            self.assertIsNone(t.ack_for(mid))
        self.assertFalse(t.A2M_NO_ACK & t.A2M_NEED_ACK)
        self.assertIn(0x2F3A, t.A2M_NEED_ACK)
        self.assertIn(0x2F41, t.A2M_NEED_ACK)
        self.assertNotIn(0x2F42, t.A2M_NEED_ACK)

    def test_md_messages(self):
        self.assertEqual(t.reply_for(0xAF78), 0x2F78)
        self.assertEqual(t.reply_for(0xAF90), 0x2F90)
        self.assertEqual(t.reply_for(0xAFA0), 0x2FA0)
        self.assertEqual(t.reply_for(0xAFB5), 0x2FB5)
        self.assertEqual(t.reply_for(0xAF60), 0x2F60)       # 0xA1 -> 0x21
        for mid in (0xAF20, 0xAF51, 0xAF75, 0xAF70, 0xAF99):  # ack, pedido de dados, aviso, ?
            self.assertIsNone(t.reply_for(mid))

    def test_names(self):
        self.assertEqual(t.msg_name(0xAF20), "ACK(SPH_ON)")
        self.assertEqual(t.msg_name(0x2F20), "SPH_ON")
        self.assertEqual(t.msg_name(0xAF99), "0xAF99")


class SphInfoTest(unittest.TestCase):
    def test_layout(self):
        b = t.sph_info()
        self.assertEqual(len(b), 128)
        self.assertEqual(b[0:6], bytes([0, 0, 3, 0, 0, 0]))     # app, bt, 48k, dsp, path, emi
        self.assertEqual(struct.unpack_from("<HHH", b, 6), (0, 0, 0))
        self.assertEqual(b[12:18], bytes(6))
        self.assertEqual(struct.unpack_from("<H", b, 18), (2,))
        self.assertEqual(struct.unpack_from("<II", b, 20), (0, 0))
        self.assertEqual(struct.unpack_from("<12H", b, 28),
                         (6, 0xDABD, 0x7918, 0x2A00, 0x8001, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(b[52:84], bytes(32))
        self.assertEqual(b[84], 1)
        self.assertEqual(b[85:], bytes(43))

    def test_exact_bytes(self):
        expected = bytearray(128)
        expected[2] = 3
        expected[18] = 2
        expected[28:38] = bytes.fromhex("0600bdda1879002a0180")
        expected[84] = 1
        self.assertEqual(t.sph_info(), bytes(expected))

    def test_rates(self):
        self.assertEqual([t.sph_info(r)[2] for r in (8000, 16000, 32000, 48000)], [0, 1, 2, 3])
        with self.assertRaises(ValueError):
            t.sph_info(44100)

    def test_sph_on_frame(self):
        raw = t.encode_payload(t.MSG_SPH_ON, t.DATA_TYPE_SPH_INFO, t.sph_info())
        self.assertEqual(len(raw), 150)
        self.assertEqual(raw[:22].hex(), "00000000" "96000000" "05000000" "8600" "202f"
                                         "a2a2" "1900" "8000")


# --- ALSA ----------------------------------------------------------------------------
class AbiTest(unittest.TestCase):
    """Valores tirados de offsetof/sizeof sobre o <sound/asound.h> num LP64."""

    def test_ioctl_numbers(self):
        self.assertEqual(t.SNDRV_PCM_IOCTL_INFO, 0x81204101)
        self.assertEqual(t.SNDRV_PCM_IOCTL_HW_PARAMS, 0xC2604111)
        self.assertEqual(t.SNDRV_PCM_IOCTL_SW_PARAMS, 0xC0884113)
        self.assertEqual(t.SNDRV_PCM_IOCTL_PREPARE, 0x4140)
        self.assertEqual(t.SNDRV_PCM_IOCTL_START, 0x4142)
        self.assertEqual(t.SNDRV_PCM_IOCTL_DROP, 0x4143)
        self.assertEqual(t.SNDRV_CTL_IOCTL_ELEM_LIST, 0xC0505510)
        self.assertEqual(t.SNDRV_CTL_IOCTL_ELEM_INFO, 0xC1105511)
        self.assertEqual(t.SNDRV_CTL_IOCTL_ELEM_READ, 0xC4C85512)
        self.assertEqual(t.SNDRV_CTL_IOCTL_ELEM_WRITE, 0xC4C85513)
        self.assertEqual(t.CCCI_IOC_GET_MD_STATE, 0x80044301)

    def test_struct_sizes(self):
        self.assertEqual(len(t.hw_params()), 608)
        self.assertEqual(len(t.sw_params(2048, 1024)), 136)
        self.assertEqual(t.SW_PARAMS.size, 136)
        self.assertEqual(t.PCM_INFO.size, 288)
        self.assertEqual(t.ELEM_ID.size, 64)
        self.assertEqual(t.ELEM_LIST.size, 80)

    def test_hw_params_fields(self):
        b = t.hw_params()
        self.assertEqual(struct.unpack_from("<I", b, 0), (0,))             # flags
        def mask(p):
            return struct.unpack_from("<8I", b, 4 + p * 32)
        self.assertEqual(mask(t.P_ACCESS), (1 << 3, 0, 0, 0, 0, 0, 0, 0))   # RW_INTERLEAVED
        self.assertEqual(mask(t.P_FORMAT), (1 << 2, 0, 0, 0, 0, 0, 0, 0))   # S16_LE
        self.assertEqual(mask(t.P_SUBFORMAT), (1, 0, 0, 0, 0, 0, 0, 0))     # STD
        self.assertEqual(b[100:260], bytes(160))                             # mres
        exact = {t.P_SAMPLE_BITS: 16, t.P_FRAME_BITS: 32, t.P_CHANNELS: 2,
                 t.P_RATE: 48000, t.P_PERIODS: 2}
        for p, v in exact.items():
            self.assertEqual(struct.unpack_from("<III", b, 260 + (p - 8) * 12), (v, v, 4))
        self.assertEqual(struct.unpack_from("<III", b, 260 + 5 * 12), (1024, 0xFFFFFFFF, 0))
        self.assertEqual(struct.unpack_from("<III", b, 260 + 9 * 12), (0, 0xFFFFFFFF, 0))
        self.assertEqual(b[404:512], bytes(108))                             # ires
        self.assertEqual(struct.unpack_from("<III", b, 512), (0xFFFFFFFF, 0, 0xFFFFFFFF))
        self.assertEqual(b[524:], bytes(608 - 524))

    def test_sw_params_fields(self):
        b = t.sw_params(2048, 1024)
        self.assertEqual(struct.unpack_from("<iII", b, 0), (0, 1, 0))
        self.assertEqual(struct.unpack_from("<8Q", b, 16),
                         (1024, 0, 2048, 2**64 - 1, 0, 0, 0, 0))
        self.assertEqual(struct.unpack_from("<II", b, 72), (0, 0))

    def test_pcm_info_parse(self):
        raw = t.PCM_INFO.pack(23, 0, 1, 0, b"Hostless_Speech (*)", b"", b"subdevice #0",
                              0, 0, 1, 1, b"", b"")
        self.assertEqual(t.parse_pcm_info(raw),
                         {"device": 23, "subdevice": 0, "stream": 1, "card": 0,
                          "id": "Hostless_Speech (*)", "name": "", "subname": "subdevice #0"})

    def test_hostless_name_match(self):
        self.assertTrue(t.is_hostless_speech("Hostless_Speech (*)"))
        self.assertTrue(t.is_hostless_speech("", "Hostless_Speech"))
        for bad in ("Hostless_Sph_Echo_Ref (*)", "Hostless_SpeechX (*)", "Playback_1 (*)", ""):
            self.assertFalse(t.is_hostless_speech(bad), bad)


class FindPcmTest(unittest.TestCase):
    def find(self, files):
        return t.find_hostless_pcm(0, FakeDev(files=files), "/proc/asound")

    def test_found_not_hardcoded(self):
        p = self.find(proc_tree())
        self.assertEqual((p.card, p.device), (0, 23))
        self.assertEqual(p.path(False), "/dev/snd/pcmC0D23p")
        self.assertEqual(p.path(True), "/dev/snd/pcmC0D23c")
        # Outro numero no /proc: segue o /proc, nao o 23 da contagem
        files = {k.replace("pcm23", "pcm31").replace("device: 23", "device: 31"): v
                 .replace("device: 23", "device: 31") for k, v in proc_tree().items()}
        self.assertEqual(self.find(files).device, 31)

    def test_missing_refuses(self):
        files = {k: v for k, v in proc_tree().items() if "pcm23" not in k}
        with self.assertRaises(t.Failure) as cm:
            self.find(files)
        self.assertEqual(cm.exception.code, t.EX_UNAVAILABLE)

    def test_missing_capture_refuses(self):
        files = {k: v for k, v in proc_tree().items() if "pcm23c" not in k}
        with self.assertRaises(t.Failure):
            self.find(files)

    def test_duplicate_refuses(self):
        files = proc_tree()
        for d in "pc":
            files[f"{PROC}/pcm40{d}/info"] = "card: 0\ndevice: 40\nid: Hostless_Speech (*)\n"
        with self.assertRaises(t.Failure):
            self.find(files)

    def test_never_reads_status(self):
        dev = FakeDev()
        seen = []
        orig = dev.read_text
        dev.read_text = lambda p: (seen.append(p), orig(p))[1]
        t.find_hostless_pcm(0, dev, "/proc/asound")
        self.assertTrue(seen)
        self.assertTrue(all(p.endswith("/info") for p in seen), seen)


class PcmStreamTest(unittest.TestCase):
    def test_wrong_device_is_not_touched(self):
        dev = FakeDev(pcm_id="Hostless_FM (*)")
        pcm = t.find_hostless_pcm(0, dev, "/proc/asound")      # /proc diz Hostless_Speech
        s = t.PcmStream(dev, pcm, False)
        with self.assertRaises(t.SpeechError):
            s.open()
        self.assertEqual(dev.events, [("pcm-open", "p"), ("INFO", "p"), ("pcm-close", "p")])
        self.assertIsNone(s.fd)

    def test_configure_sequence(self):
        dev = FakeDev()
        pcm = t.find_hostless_pcm(0, dev, "/proc/asound")
        s = t.PcmStream(dev, pcm, True)
        s.open()
        s.configure()
        s.prepare()
        s.start()
        s.close()
        self.assertEqual([e[0] for e in dev.events],
                         ["pcm-open", "INFO", "HW_PARAMS", "SW_PARAMS", "PREPARE", "START",
                          "DROP", "pcm-close"])
        self.assertEqual(struct.unpack_from("<Q", dev.last_sw, 40)[0], 2**64 - 1)
        self.assertEqual(struct.unpack_from("<Q", dev.last_sw, 32)[0], 2048)   # buffer

    def test_stop_threshold_below_boundary_refused(self):
        dev = FakeDev()
        pcm = t.find_hostless_pcm(0, dev, "/proc/asound")
        s = t.PcmStream(dev, pcm, False)
        s.open()
        with mock.patch.object(t, "ULONG_MAX", 2**32 - 1), self.assertRaises(t.SpeechError):
            s.configure()


class MixerTest(unittest.TestCase):
    def mixer(self, elems=None):
        dev = FakeDev(elems=elems)
        m = t.Mixer(dev)
        m.open()
        return m, dev

    def test_lookup_by_name(self):
        m, _dev = self.mixer()
        self.assertEqual(len(m.ids), 5)
        e = m.find("PCM_2_PB_CH2 ADDA_UL_CH2")
        self.assertEqual((e.numid, e.iface, e.name), (4, t.IFACE_MIXER, "PCM_2_PB_CH2 ADDA_UL_CH2"))
        with self.assertRaises(t.SpeechError):
            m.find("PCM_2_PB_CH3 ADDA_UL_CH3")

    def test_ambiguous_and_wrong_iface(self):
        elems = [Elem(1, "Dup"), Elem(2, "Dup", device=1), Elem(3, "Card", iface=0)]
        m, _dev = self.mixer(elems)
        with self.assertRaises(t.SpeechError):
            m.find("Dup")
        with self.assertRaises(t.SpeechError):
            m.find("Card")

    def test_routes_on_off(self):
        m, dev = self.mixer()
        m.set_routes(True)
        self.assertEqual(dev.events, [("mixer", n, 1) for n in t.ROUTE_SWITCHES])
        self.assertEqual(m.read_ints(t.ROUTE_SWITCHES[0]), [1])
        dev.events.clear()
        m.set_routes(False)
        self.assertEqual(dev.events, [("mixer", n, 0) for n in t.ROUTE_SWITCHES])

    def test_stereo_switch_writes_all_channels(self):
        m, dev = self.mixer([Elem(1, "S", count=2)])
        m.set_switch("S", True)
        self.assertEqual(dev.elems[0].values, [1, 1])

    def test_not_a_switch(self):
        m, _dev = self.mixer()
        with self.assertRaises(t.SpeechError):
            m.set_switch("Handset_PGA_GAIN", True)          # enumerado

    def test_best_effort(self):
        elems = [Elem(1, t.ROUTE_SWITCHES[0]), Elem(2, t.ROUTE_SWITCHES[2])]
        m, dev = self.mixer(elems)
        errors = m.set_routes(False, best_effort=True)
        self.assertEqual(len(errors), 2)
        self.assertEqual([e[1] for e in dev.events], [t.ROUTE_SWITCHES[0], t.ROUTE_SWITCHES[2]])


# --- ccci_aud ---------------------------------------------------------------------------
class LinkTest(unittest.TestCase):
    def setUp(self):
        self.dev = FakeDev()
        self.events = []
        self.link = t.SpeechLink(self.dev, on_event=lambda k, m: self.events.append(k))
        self.link.open()

    def tx_ids(self):
        return [t.parse_tx(f).msg_id for f in self.dev.tx]

    def test_md_alive_answered(self):
        self.link.handle_rx(OBSERVED_MD_ALIVE)
        self.assertEqual(self.dev.tx, [t.encode_mailbox(0x2FA0, 0, 0)])
        self.assertEqual(self.events, ["md_alive"])
        self.assertEqual(self.link.stats.md_version, (0xAA55, 0x3000))

    def test_notify_acks(self):
        self.link.handle_rx(t.encode_md_mailbox(0xAF78))
        self.link.handle_rx(t.encode_md_payload(0xAF90, 0x14, bytes(0xB8)))
        self.link.handle_rx(t.encode_md_mailbox(0xAFB5))
        self.assertEqual(self.tx_ids(), [0x2F78, 0x2F90, 0x2FB5])
        self.assertEqual(self.events, ["epof"])

    def test_not_answered(self):
        with mock.patch.object(t, "log") as lg:
            self.link.handle_rx(t.encode_md_mailbox(0xAF99, 1, 2))           # desconhecida
            self.link.handle_rx(t.encode_md_mailbox(0xAF51))                 # pede dados
            self.link.handle_rx(t.encode_md_mailbox(0xAF75))                 # RAT INFO
            self.link.handle_rx(t.encode_md_mailbox(0xAF20))                 # ack sem espera
            self.link.handle_rx(b"\x01" * 20)                                # malformada
        self.assertEqual(self.dev.tx, [])
        self.assertEqual(self.link.stats.unknown, 1)
        self.assertEqual(self.link.stats.malformed, 1)
        prios = [c.args[0] for c in lg.call_args_list]
        self.assertIn(t.LOG_NOTICE, prios)
        notice = [c.args[1] for c in lg.call_args_list if c.args[0] == t.LOG_NOTICE]
        self.assertTrue(any("0xAF99" in s and "ffffffff" in s for s in notice), notice)

    def test_every_message_logged_at_debug(self):
        with mock.patch.object(t, "log") as lg:
            self.link.handle_rx(OBSERVED_MD_ALIVE)
        debug = [c.args[1] for c in lg.call_args_list if c.args[0] == t.LOG_DEBUG]
        self.assertTrue(any(s.startswith("rx payload 0xAFA0 MD_ALIVE type=0x1A") for s in debug))
        self.assertTrue(any(s.startswith("tx mailbox 0x2FA0") for s in debug))

    def test_request_gets_ack(self):
        self.dev.link = self.link
        ack = self.link.request(t.encode_mailbox(t.MSG_SPH_OFF), 1.0)
        self.assertIsNotNone(ack)
        self.assertEqual(ack.msg_id, 0xAF21)
        self.assertEqual(self.link.waiters, {})

    def test_request_ack_from_reader_thread(self):
        def later():
            time.sleep(0.05)
            self.link.handle_rx(t.encode_md_mailbox(0xAF02))
        th = threading.Thread(target=later)
        th.start()
        ack = self.link.request(t.encode_mailbox(t.MSG_SET_UL_MUTE, 0), 2.0)
        th.join()
        self.assertEqual(ack.msg_id, 0xAF02)

    def test_request_timeout(self):
        t0 = time.monotonic()
        self.assertIsNone(self.link.request(t.encode_mailbox(t.MSG_SPH_OFF), 0.2))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(self.link.waiters, {})

    def test_request_abort(self):
        with self.assertRaises(t.StopRequested):
            self.link.request(t.encode_mailbox(t.MSG_SPH_OFF), 5.0, abort=lambda: True)

    def test_no_ack_request_returns_none(self):
        self.assertIsNone(self.link.request(t.encode_mailbox(t.MSG_SET_DL_GAIN), 5.0))
        self.assertEqual(self.tx_ids(), [t.MSG_SET_DL_GAIN])

    def test_busy_node(self):
        self.dev.open_fds.add(FakeDev.CCCI_FD)
        other = t.SpeechLink(self.dev)
        with self.assertRaises(t.Failure) as cm:
            other.open()
        self.assertEqual(cm.exception.code, t.EX_TEMPFAIL)

    def test_reader_thread_and_close(self):
        self.link.start_reader()
        self.link.close()
        self.assertFalse(self.link.thread.is_alive())


# --- voz -------------------------------------------------------------------------------
ON_ORDER = [
    ("mixer", "ADDA_DL_CH1 PCM_2_CAP_CH1", 1), ("mixer", "ADDA_DL_CH2 PCM_2_CAP_CH1", 1),
    ("mixer", "PCM_2_PB_CH1 ADDA_UL_CH1", 1), ("mixer", "PCM_2_PB_CH2 ADDA_UL_CH2", 1),
    ("pcm-open", "p"), ("INFO", "p"), ("HW_PARAMS", "p"), ("SW_PARAMS", "p"), ("PREPARE", "p"),
    ("pcm-open", "c"), ("INFO", "c"), ("HW_PARAMS", "c"), ("SW_PARAMS", "c"), ("PREPARE", "c"),
    ("START", "p"), ("START", "c"),
    ("tx", "SPH_ON", 0), ("tx", "SET_UL_MUTE", 0), ("tx", "SET_DL_MUTE", 0),
    ("tx", "SET_DL_GAIN", 0), ("tx", "SET_UL_GAIN", 0),
]
OFF_ORDER = [
    ("tx", "SET_UL_MUTE", 1),
    ("DROP", "p"), ("pcm-close", "p"), ("DROP", "c"), ("pcm-close", "c"),
    ("mixer", "ADDA_DL_CH1 PCM_2_CAP_CH1", 0), ("mixer", "ADDA_DL_CH2 PCM_2_CAP_CH1", 0),
    ("mixer", "PCM_2_PB_CH1 ADDA_UL_CH1", 0), ("mixer", "PCM_2_PB_CH2 ADDA_UL_CH2", 0),
    ("tx", "SPH_OFF", 0),
]


class SpeechTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="speechd-test-")
        self.mark = os.path.join(self.tmp, "speech-on")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_on_order_pcm_before_sph_on(self):
        sp, dev = make_speech(mark=self.mark)
        sp.speech_on()
        self.assertTrue(sp.on)
        self.assertEqual(dev.events, ON_ORDER)
        self.assertTrue(os.path.exists(self.mark))
        sph = t.parse_tx(dev.tx[0])
        self.assertEqual((sph.msg_id, sph.data_type, sph.data), (0x2F20, 0x19, t.sph_info()))

    def test_off_order(self):
        sp, dev = make_speech(mark=self.mark)
        sp.speech_on()
        dev.events.clear()
        self.assertEqual(sp.speech_off(), [])
        self.assertEqual(dev.events, OFF_ORDER)
        self.assertFalse(sp.on)
        self.assertFalse(os.path.exists(self.mark))
        self.assertEqual(dev.open_fds, {FakeDev.CCCI_FD, FakeDev.CTL_FD})
        self.assertEqual(sp.speech_off(), [])               # de novo: nada

    def test_rollback_on_missing_sph_on_ack(self):
        sp, dev = make_speech(mark=self.mark)
        dev.auto_ack.discard(t.MSG_SPH_ON)
        with self.assertRaises(t.SpeechError):
            sp.speech_on()
        self.assertFalse(sp.on)
        tail = dev.events[ON_ORDER.index(("tx", "SPH_ON", 0)):]
        self.assertEqual(tail, [
            ("tx", "SPH_ON", 0),
            ("tx", "SPH_OFF", 0),
            ("DROP", "p"), ("pcm-close", "p"), ("DROP", "c"), ("pcm-close", "c"),
            ("mixer", "ADDA_DL_CH1 PCM_2_CAP_CH1", 0), ("mixer", "ADDA_DL_CH2 PCM_2_CAP_CH1", 0),
            ("mixer", "PCM_2_PB_CH1 ADDA_UL_CH1", 0), ("mixer", "PCM_2_PB_CH2 ADDA_UL_CH2", 0),
        ])
        self.assertEqual(dev.open_fds, {FakeDev.CCCI_FD, FakeDev.CTL_FD})
        self.assertFalse(os.path.exists(self.mark))
        self.assertIn("SPH_ON not acknowledged", sp.last_error)
        self.assertEqual(sp.speech_off(), [])               # nada sobrou para desligar

    def test_rollback_on_pcm_failure_without_sph(self):
        sp, dev = make_speech()
        dev.fail_ioctl[t.SNDRV_PCM_IOCTL_START] = errno.EPIPE
        with self.assertRaises(t.SpeechError):
            sp.speech_on()
        self.assertFalse(any(e[0] == "tx" for e in dev.events))      # nem SPH_ON nem SPH_OFF
        self.assertEqual(dev.events[-4:], [("mixer", n, 0) for n in t.ROUTE_SWITCHES])
        self.assertEqual(dev.open_fds, {FakeDev.CCCI_FD, FakeDev.CTL_FD})

    def test_rollback_when_route_missing(self):
        elems = [Elem(1, t.ROUTE_SWITCHES[0])]
        sp, dev = make_speech(FakeDev(elems=elems))
        with self.assertRaises(t.SpeechError):
            sp.speech_on()
        self.assertFalse(any(e[0] in ("pcm-open", "tx") for e in dev.events))
        self.assertEqual(dev.elems[0].values, [0])

    def test_rollback_on_stop_request(self):
        sp, dev = make_speech()
        dev.auto_ack.discard(t.MSG_SPH_ON)
        sp.abort = lambda: True
        sp.ack_timeout = 5.0
        with self.assertRaises(t.StopRequested):
            sp.speech_on()
        self.assertIn(("tx", "SPH_OFF", 0), dev.events)
        self.assertFalse(sp.on)

    def test_md_not_ready(self):
        sp, dev = make_speech()
        dev.md_state = 3
        with self.assertRaises(t.SpeechError):
            sp.speech_on()
        self.assertEqual(dev.events, [])

    def test_mute_and_gains(self):
        sp, dev = make_speech()
        sp.mute_ul(True)
        sp.set_gains(-6, 1.5)
        self.assertEqual(dev.events, [])                    # desligada: so guarda
        sp.speech_on()
        tail = dev.events[-4:]
        self.assertEqual(tail, [("tx", "SET_UL_MUTE", 1), ("tx", "SET_DL_MUTE", 0),
                                ("tx", "SET_DL_GAIN", 0xFFFA), ("tx", "SET_UL_GAIN", 6)])
        dev.events.clear()
        sp.mute_ul(False)
        self.assertEqual(dev.events, [("tx", "SET_UL_MUTE", 0)])
        with self.assertRaises(ValueError):
            sp.set_gains(3, 0)

    def test_md_restart_redoes_sph_on(self):
        sp, dev = make_speech()
        sp.speech_on()
        dev.events.clear()
        sp.md_restarted()
        self.assertEqual([e[1] for e in dev.events],
                         ["SPH_ON", "SET_UL_MUTE", "SET_DL_MUTE", "SET_DL_GAIN", "SET_UL_GAIN"])
        self.assertTrue(sp.on)

    def test_cleanup_stale(self):
        sp, dev = make_speech(mark=self.mark)
        dev.elems[1].values = [1]
        open(self.mark, "w").close()
        sp.cleanup_stale()
        self.assertEqual([e for e in dev.events if e[0] == "mixer"],
                         [("mixer", n, 0) for n in t.ROUTE_SWITCHES])
        self.assertEqual(dev.events[-1], ("tx", "SPH_OFF", 0))
        self.assertFalse(os.path.exists(self.mark))

    def test_cleanup_clean_start_sends_nothing(self):
        sp, dev = make_speech(mark=self.mark)
        sp.cleanup_stale()
        self.assertEqual(dev.events, [])


# --- chamadas ------------------------------------------------------------------------
class CallStateTest(unittest.TestCase):
    def test_desired(self):
        d = t.desired_speech
        self.assertEqual(d([]), "off")
        self.assertEqual(d([t.CALL_TERMINATED]), "off")
        for s in (t.CALL_RINGING_IN, t.CALL_DIALING, t.CALL_RINGING_OUT, t.CALL_WAITING):
            self.assertEqual(d([s]), "prepare")
        self.assertEqual(d([t.CALL_ACTIVE]), "on")
        self.assertEqual(d([t.CALL_HELD]), "on")
        self.assertEqual(d([t.CALL_WAITING, t.CALL_ACTIVE]), "on")
        self.assertEqual(d([t.CALL_DIALING], early_media=True), "on")
        self.assertEqual(d([t.CALL_RINGING_IN], early_media=True), "prepare")

    def test_tracker_incoming_call(self):
        tr = t.CallTracker()
        steps = [([t.CALL_RINGING_IN], ["prepare"]), ([t.CALL_RINGING_IN], []),
                 ([t.CALL_ACTIVE], ["on"]), (None, []), ([t.CALL_ACTIVE], []),
                 ([t.CALL_TERMINATED], ["off"]), ([], [])]
        for states, actions in steps:
            with self.subTest(states=states):
                self.assertEqual(tr.update(states), actions)

    def test_tracker_outgoing_and_waiting(self):
        tr = t.CallTracker()
        self.assertEqual(tr.update([t.CALL_DIALING]), ["prepare"])
        self.assertEqual(tr.update([t.CALL_RINGING_OUT]), [])
        self.assertEqual(tr.update([t.CALL_ACTIVE]), ["on"])
        self.assertEqual(tr.update([t.CALL_ACTIVE, t.CALL_WAITING]), [])
        self.assertEqual(tr.update([t.CALL_TERMINATED, t.CALL_RINGING_IN]), ["off", "prepare"])
        self.assertEqual(tr.update([t.CALL_RINGING_IN]), [])
        self.assertEqual(tr.update([]), [])                  # nunca atendida: nada a desligar

    def test_tracker_early_media(self):
        tr = t.CallTracker(early_media=True)
        self.assertEqual(tr.update([t.CALL_DIALING]), ["on"])
        self.assertEqual(tr.update([t.CALL_ACTIVE]), [])
        self.assertEqual(tr.update([]), ["off"])

    def test_tracker_actions_drive_speech(self):
        sp, dev = make_speech()
        tr = t.CallTracker()
        for states in ([t.CALL_RINGING_IN], [t.CALL_ACTIVE], [t.CALL_TERMINATED]):
            for a in tr.update(states):
                {"prepare": sp.prepare, "on": sp.speech_on, "off": sp.speech_off}[a]()
        self.assertEqual(dev.events, ON_ORDER + OFF_ORDER)


def fake_busctl(responses):
    """responses: lista de (argumentos depois do BASE, stdout ou None = falha)."""
    calls = []

    def run(argv, **_kw):
        args = argv[len(t.Busctl.BASE):]
        calls.append(args)
        for want, out in responses:
            if list(args[:len(want)]) == list(want):
                if out is None:
                    return subprocess.CompletedProcess(argv, 1, "", "Call failed")
                return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
        raise AssertionError(f"unexpected busctl {args}")
    return run, calls


HAS_OWNER = ["call", "org.freedesktop.DBus"]
GET_OBJECTS = ["call", t.MM_NAME, t.MM_PATH]
CALL1 = "/org/freedesktop/ModemManager1/Call/1"
CALL2 = "/org/freedesktop/ModemManager1/Call/2"
OBJECTS = {"type": "a{oa{sa{sv}}}", "data": [{
    "/org/freedesktop/ModemManager1/Modem/0": {
        "org.freedesktop.ModemManager1.Modem": {"State": {"type": "i", "data": 8}},
        t.MM_VOICE_IFACE: {"Calls": {"type": "ao", "data": [CALL1, CALL2]},
                           "EmergencyOnly": {"type": "b", "data": False}}}}]}


class BusctlTest(unittest.TestCase):
    def test_states(self):
        run, calls = fake_busctl([
            (HAS_OWNER, {"type": "b", "data": [True]}), (GET_OBJECTS, OBJECTS),
            (["get-property", t.MM_NAME, CALL1], {"type": "i", "data": 4}),
            (["get-property", t.MM_NAME, CALL2], {"type": "i", "data": 6})])
        self.assertEqual(t.Busctl(run).call_states(), [4, 6])
        self.assertTrue(all(c[0] in ("call", "get-property") for c in calls))

    def test_no_modemmanager(self):
        run, calls = fake_busctl([(HAS_OWNER, {"type": "b", "data": [False]})])
        self.assertEqual(t.Busctl(run).call_states(), [])
        self.assertEqual(len(calls), 1)

    def test_no_voice_interface(self):
        objs = {"type": "a{oa{sa{sv}}}", "data": [{"/m/0": {"x": {}}}]}
        run, _ = fake_busctl([(HAS_OWNER, {"type": "b", "data": [True]}), (GET_OBJECTS, objs)])
        self.assertEqual(t.Busctl(run).call_states(), [])

    def test_failure_keeps_state(self):
        run, _ = fake_busctl([(HAS_OWNER, {"type": "b", "data": [True]}), (GET_OBJECTS, OBJECTS),
                              (["get-property"], None)])
        self.assertIsNone(t.Busctl(run).call_states())
        run, _ = fake_busctl([(HAS_OWNER, None)])
        self.assertIsNone(t.Busctl(run).call_states())

    def test_never_autostarts(self):
        self.assertIn("--auto-start=no", t.Busctl.BASE)
        argv = t.Busctl().monitor_argv()
        self.assertEqual(argv[:4], ["busctl", "--system", "--json=short", "monitor"])
        self.assertTrue(any("arg0='org.freedesktop.ModemManager1.Call'" in a for a in argv))


# --- socket de controle e daemon -------------------------------------------------------
class CommandTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(t.parse_command("on\n"), ("on", ()))
        self.assertEqual(t.parse_command("status"), ("status", ()))
        self.assertEqual(t.parse_command("mute on"), ("mute", (True,)))
        self.assertEqual(t.parse_command("mute off"), ("mute", (False,)))
        self.assertEqual(t.parse_command("gain -6 1.5"), ("gain", (-6, 1.5)))
        for bad in ("", "reboot", "on now", "mute", "gain 1 0", "gain x 0", "mute maybe"):
            with self.subTest(bad), self.assertRaises(ValueError):
                t.parse_command(bad)


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="speechd-run-")
        self.dev = FakeDev()
        self.notes = []
        opts = t.Options(ack_timeout=0.3, modemmanager=False, inhibit=False,
                         run_dir=self.tmp, ctl_uid=os.getuid())
        self.d = t.Speechd(opts, self.dev, self.notes.append)
        self.dev.link = self.d.link

    def tearDown(self):
        self.d.shutdown()
        shutil.rmtree(self.tmp)

    def test_setup_and_commands(self):
        self.d.setup()
        self.assertIn("READY=1", self.notes)
        self.assertEqual(self.d.pcm.device, PCM_N)
        out = self.d.handle_command("on", ())
        self.assertTrue(out.startswith("ok\nspeech: on"), out)
        self.assertEqual(self.dev.events, ON_ORDER)
        self.assertIn("speech: on", self.d.handle_command("status", ()))
        out = self.d.handle_command("off", ())
        self.assertTrue(out.startswith("ok\nspeech: off"), out)
        self.dev.auto_ack.discard(t.MSG_SPH_ON)
        out = self.d.handle_command("on", ())
        self.assertTrue(out.startswith("error: SPH_ON not acknowledged"), out)
        self.assertIn("last error: SPH_ON not acknowledged", self.d.status_text())

    def test_socket_round_trip(self):
        self.d.setup()
        th = threading.Thread(target=self.d.loop)
        th.start()
        try:
            path = os.path.join(self.tmp, t.CTL_NAME)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            ok, text = t.ctl_request(path, "on", 10)
            self.assertTrue(ok, text)
            self.assertIn("speech: on", text)
            ok, text = t.ctl_request(path, "bogus", 10)
            self.assertFalse(ok)
            ok, text = t.ctl_request(path, "off", 10)
            self.assertTrue(ok, text)
        finally:
            self.d.stop_requested = True
            th.join(5)

    def test_wrong_uid_refused(self):
        self.d.opts.ctl_uid = os.getuid() + 1
        self.d.setup()
        ok, text = t.ctl_request(os.path.join(self.tmp, t.CTL_NAME), "on", 5)
        self.assertFalse(ok)
        self.assertIn("root only", text)
        self.assertFalse(any(e[0] == "tx" for e in self.dev.events))

    def test_single_instance(self):
        self.d.take_lock()
        other = t.Speechd(t.Options(run_dir=self.tmp), FakeDev(), lambda _m: None)
        with self.assertRaises(t.Failure) as cm:
            other.take_lock()
        self.assertEqual(cm.exception.code, t.EX_TEMPFAIL)

    def test_refuses_without_hostless_pcm(self):
        self.d.dev.files = {k: v for k, v in proc_tree().items() if "pcm23" not in k}
        with self.assertRaises(t.Failure) as cm:
            self.d.setup()
        self.assertEqual(cm.exception.code, t.EX_UNAVAILABLE)
        self.assertNotIn(FakeDev.CCCI_FD, self.dev.open_fds)

    def test_refuses_without_route_controls(self):
        self.d.dev.elems = [Elem(1, t.ROUTE_SWITCHES[0])]
        self.d.mixer = t.Mixer(self.d.dev)
        with self.assertRaises(t.Failure) as cm:
            self.d.setup()
        self.assertEqual(cm.exception.code, t.EX_UNAVAILABLE)

    def test_shutdown_turns_off(self):
        self.d.setup()
        self.d.handle_command("on", ())
        self.dev.events.clear()
        self.d.shutdown()
        self.assertEqual(self.dev.events, OFF_ORDER)
        self.assertEqual(self.dev.open_fds, set())

    def test_md_alive_during_call(self):
        self.d.setup()
        self.d.handle_command("on", ())
        self.dev.events.clear()
        self.d.link.handle_rx(OBSERVED_MD_ALIVE)          # thread de leitura: so ack + fila
        self.assertEqual(self.dev.events, [("tx", "MD_ALIVE_ACK", 0)])
        self.d.dispatch(self.d.events.get_nowait())       # thread principal: refaz o SPH_ON
        self.assertEqual(self.dev.events[1], ("tx", "SPH_ON", 0))

    def test_modemmanager_drives_speech(self):
        self.d.setup()
        seq = iter([[t.CALL_RINGING_IN], [t.CALL_ACTIVE], [t.CALL_TERMINATED]])
        self.d.busctl = mock.Mock(call_states=lambda: next(seq))
        self.d.watcher = mock.Mock()
        for _ in range(3):
            self.d.dispatch(("mm",))
            self.d.periodic()
        self.assertEqual(self.dev.events, ON_ORDER + OFF_ORDER)


class CliTest(unittest.TestCase):
    def test_usage(self):
        with mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            self.assertEqual(t.main([]), t.EX_USAGE)
            self.assertEqual(t.main(["gain", "x", "0"]), t.EX_USAGE)

    def test_client_without_daemon(self):
        with mock.patch.object(t, "RUN_DIR", "/nonexistent-speechd"):
            self.assertEqual(t.main(["status"]), t.EX_UNAVAILABLE)

    def test_run_needs_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        self.assertEqual(t.main(["run"]), t.EX_NOPERM)


if __name__ == "__main__":
    unittest.main()
