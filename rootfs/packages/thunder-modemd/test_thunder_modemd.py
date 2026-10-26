"""Testes do thunder-modemd que rodam no PC, sem o aparelho nem o eccci.

    cd rootfs/packages/thunder-modemd && python3 -m unittest -v
"""
import calendar
import errno
import io
import os
import shutil
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thunder_modemd as t  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
_quiet = mock.patch.object(t, "log")


def setUpModule():
    _quiet.start()          # sem log no terminal; cada teste que confere log tem o seu


def tearDownModule():
    _quiet.stop()


def wpath(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\0\0"


def u32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def i32(v: int) -> bytes:
    return struct.pack("<i", v)


def request(op: int, blocks: list[bytes], reserved: int = 0, mtu: int = t.CCCI_MTU) -> list[bytes]:
    """Pacotes como o MD manda (canal 14, fragmentos com bit31)."""
    return t.fragment(t.encode_message(op, blocks), reserved, t.FS_RX_CH, mtu)


def join_reply(packets: list[bytes]) -> tuple[int, int, list[bytes]]:
    """Remonta a resposta: (op_field, reserved, blocos), conferindo cada pacote."""
    assert packets, "no reply"
    op_field = packets[0][16:20]
    body = b""
    for i, p in enumerate(packets):
        d0, length, ch, reserved = struct.unpack_from("<IIII", p)
        assert len(p) <= t.FS_PACKET_MAX, len(p)
        assert length == len(p)
        assert ch & 0xFFFF == t.FS_TX_CH
        assert p[16:20] == op_field
        assert bool(d0 & t.FS_MORE) == (i < len(packets) - 1)
        body += p[20:]
    (count,) = struct.unpack_from("<I", body)
    msg = op_field + body
    return (struct.unpack("<I", op_field)[0], reserved,
            t.parse_blocks(msg, 8, count))


def code(blocks: list[bytes], i: int = 0) -> int:
    return struct.unpack("<i", blocks[i][:4])[0]


class FsFixture(unittest.TestCase):
    """Copia de NVRAM de mentira num diretorio temporario."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="modemd-test-")
        self.nv = os.path.join(self.tmp, "nv")
        self.ro = os.path.join(self.tmp, "ro")
        for d in ("nvdata/md/NVRAM/NVD_DATA", "nvdata/md/NVRAM/CALIBRAT", "nvdata/md_cmn",
                  "protect1/md", "protect2/md", "nvcfg/mdota", "mdlpm", "vendor_de_md"):
            os.makedirs(os.path.join(self.nv, d))
        os.makedirs(os.path.join(self.ro, "etc_md/cacerts/tls"))
        self.write("nv/nvdata/md/NVRAM/NVD_DATA/CK00_000", b"0123456789")
        self.write("ro/etc_md/cacerts/tls/root.pem", b"PEM")
        self.drives = t.drive_table(self.nv, self.ro)
        self.srv = t.FsServer(self.drives)
        patcher = mock.patch.object(t, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, data: bytes) -> str:
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def read(self, rel: str) -> bytes:
        with open(os.path.join(self.tmp, rel), "rb") as f:
            return f.read()

    def call(self, op: int, blocks: list[bytes], reserved: int = 0) -> list[bytes]:
        out = []
        for p in request(op, blocks, reserved):
            out += self.srv.process_packet(p)
        op_field, res, rblocks = join_reply(out)
        self.assertEqual(op_field, op | t.RESP_ID)
        self.assertEqual(res, reserved)
        return rblocks

    def open(self, path: str, flags: int = 0) -> int:
        return code(self.call(0x1001, [wpath(path), u32(flags)]))


# --- moldura -----------------------------------------------------------------------------
class FramingTest(FsFixture):
    def test_message_round_trip(self):
        pkts = request(0x1001, [wpath("Z:\\a"), u32(0x100)], reserved=3)
        self.assertEqual(len(pkts), 1)
        req = t.parse_message(pkts[0])
        self.assertEqual((req.op, req.reserved), (0x1001, 3))
        self.assertEqual(req.blocks, [wpath("Z:\\a"), u32(0x100)])
        d0, length, ch, _ = struct.unpack_from("<IIII", pkts[0])
        self.assertEqual((d0, length, ch), (0, len(pkts[0]), t.FS_RX_CH))

    def test_blocks_are_padded_to_4(self):
        enc = t.encode_blocks([b"abc", b"de"])
        self.assertEqual(enc, u32(3) + b"abc\0" + u32(2) + b"de\0\0")

    def test_fragmentation_sizes(self):
        msg = t.encode_message(0x1003 | t.RESP_ID, [bytes(10000)])
        pkts = t.fragment(msg, 4)
        self.assertEqual([len(p) for p in pkts][:-1], [t.FS_PACKET_MAX] * (len(pkts) - 1))
        self.assertEqual(sum(len(p) - 20 for p in pkts), len(msg) - 4)
        for p in pkts:
            self.assertEqual(struct.unpack_from("<I", p, 12)[0], 4)        # reserved ecoado
            self.assertEqual(p[16:20], u32(0x1003 | t.RESP_ID))

    def test_reply_fragmented(self):
        data = bytes(range(256)) * 40                                       # 10240 B
        self.write("nv/nvdata/md/BIG", data)
        h = self.open("Z:/BIG", 0x100)
        out = []
        for p in request(0x1003, [i32(h), u32(len(data))], reserved=2):
            out += self.srv.process_packet(p)
        self.assertGreater(len(out), 1)
        self.assertTrue(all(len(p) <= 3476 for p in out))
        _op, res, blocks = join_reply(out)
        self.assertEqual(res, 2)
        self.assertEqual((code(blocks), struct.unpack("<I", blocks[1])[0]), (0, len(data)))
        self.assertEqual(blocks[2], data)

    def test_request_fragmented_and_wakeup_bit_masked(self):
        h = self.open("Z:/NEW", t.FS_CREATE)
        payload = os.urandom(5000)
        pkts = request(0x1004, [i32(h), payload, u32(len(payload))], reserved=1)
        self.assertEqual(len(pkts), 2)
        first = bytearray(pkts[0])
        struct.pack_into("<I", first, 0, t.FS_MORE | t.FS_AP_WAKEUP)
        self.assertEqual(self.srv.process_packet(bytes(first)), [])
        _op, _res, blocks = join_reply(self.srv.process_packet(pkts[1]))
        self.assertEqual((code(blocks), code(blocks, 1)), (0, 5000))
        self.call(0x1005, [i32(h)])
        self.assertEqual(self.read("nv/nvdata/md/NEW"), payload)

    def test_interleaved_slots(self):
        ha = self.open("Z:/A", t.FS_CREATE)
        hb = self.open("Z:/B", t.FS_CREATE)
        pa = request(0x1004, [i32(ha), b"a" * 4000, u32(4000)], reserved=0)
        pb = request(0x1004, [i32(hb), b"b" * 4000, u32(4000)], reserved=1)
        self.assertEqual(self.srv.process_packet(pa[0]), [])
        self.assertEqual(self.srv.process_packet(pb[0]), [])
        _, ra, ba = join_reply(self.srv.process_packet(pa[1]))
        _, rb, bb = join_reply(self.srv.process_packet(pb[1]))
        self.assertEqual((ra, code(ba, 1), rb, code(bb, 1)), (0, 4000, 1, 4000))
        self.call(0x1006, [])
        self.assertEqual(self.read("nv/nvdata/md/A"), b"a" * 4000)
        self.assertEqual(self.read("nv/nvdata/md/B"), b"b" * 4000)

    def test_truncated_request_answered(self):
        pkt = bytearray(request(0x1001, [wpath("Z:/x"), u32(0)])[0])
        struct.pack_into("<I", pkt, 24, 9999)                    # bloco maior que o pacote
        _op, _r, blocks = join_reply(self.srv.process_packet(bytes(pkt)))
        self.assertEqual(code(blocks), t.FS_PARAM_ERROR)

    def test_short_packet_dropped(self):
        self.assertEqual(self.srv.process_packet(b"\0" * 12), [])

    def test_unknown_ops_answered(self):
        for op in (0x1026, 0x2000, 0x1000):
            with self.subTest(op=hex(op)):
                self.assertEqual(code(self.call(op, [])), -2)

    def test_all_37_ops_answered(self):
        for op in range(0x1001, 0x1026):
            with self.subTest(op=t.FS_OPS[op]):
                out = []
                for p in request(op, []):
                    out += self.srv.process_packet(p)
                _op, _r, blocks = join_reply(out)
                if op == 0x1017:
                    self.assertEqual(blocks, [])

    def test_shutdown_zero_blocks_and_closes(self):
        h = self.open("Z:/S", t.FS_CREATE)
        self.assertEqual(self.call(0x1017, []), [])
        self.assertEqual(code(self.call(0x1005, [i32(h)])), t.FS_INVALID_FILE_HANDLE)


# --- operacoes -----------------------------------------------------------------------------
class OpsTest(FsFixture):
    def test_open_write_seek_read_size_close(self):
        h = self.open("Z:\\NVRAM\\NVD_DATA\\NEW_001", t.FS_CREATE)
        self.assertGreaterEqual(h, 1)
        b = self.call(0x1004, [i32(h), b"hello world", u32(11)])
        self.assertEqual((code(b), code(b, 1)), (0, 11))
        self.assertEqual(code(self.call(0x1002, [i32(h), i32(6), u32(0)])), 6)
        b = self.call(0x1003, [i32(h), u32(100)])
        self.assertEqual((code(b), struct.unpack("<I", b[1])[0], b[2]), (0, 5, b"world"))
        b = self.call(0x1009, [i32(h)])
        self.assertEqual((code(b), struct.unpack("<I", b[1])[0]), (0, 11))
        self.assertEqual(code(self.call(0x1005, [i32(h)])), 0)
        self.assertEqual(self.read("nv/nvdata/md/NVRAM/NVD_DATA/NEW_001"), b"hello world")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(
            self.nv, "nvdata/md/NVRAM/NVD_DATA/NEW_001")).st_mode) & 0o600, 0o600)

    def test_open_existing_keeps_content(self):
        h = self.open("Z:/NVRAM/NVD_DATA/CK00_000", t.FS_CREATE)
        b = self.call(0x1003, [i32(h), u32(4)])
        self.assertEqual(b[2], b"0123")

    def test_open_absent_without_create(self):
        self.assertEqual(self.open("Z:/NVRAM/NOPE", 0), t.FS_FILE_NOT_FOUND)
        self.assertEqual(self.open("Z:/NVRAM/NOPE", 0x100), t.FS_FILE_NOT_FOUND)

    def test_create_always_truncates(self):
        h = self.open("Z:/NVRAM/NVD_DATA/CK00_000", t.FS_CREATE_ALWAYS)
        self.call(0x1005, [i32(h)])
        self.assertEqual(self.read("nv/nvdata/md/NVRAM/NVD_DATA/CK00_000"), b"")

    def test_write_on_read_only_handle(self):
        h = self.open("Z:/NVRAM/NVD_DATA/CK00_000", 0x100)
        b = self.call(0x1004, [i32(h), b"x", u32(1)])
        self.assertEqual((code(b), code(b, 1)), (t.FS_GENERAL_FAILURE, 0))

    def test_invalid_handles(self):
        for op, blocks in ((0x1002, [i32(77), i32(0), u32(0)]), (0x1003, [i32(77), u32(1)]),
                           (0x1004, [i32(77), b"x", u32(1)]), (0x1005, [i32(77)]),
                           (0x1009, [i32(77)]), (0x1013, [i32(77), u32(9)])):
            with self.subTest(op=t.FS_OPS[op]):
                self.assertEqual(code(self.call(op, blocks)), t.FS_INVALID_FILE_HANDLE)
        h = self.open("Z:/NVRAM/NVD_DATA/CK00_000", 0x100)
        self.assertEqual(code(self.call(0x1002, [i32(h), i32(0), u32(3)])),
                         t.FS_INVALID_FILE_HANDLE)

    def test_error_shapes(self):
        b = self.call(0x1003, [i32(77), u32(1)])
        self.assertEqual([len(x) for x in b], [4, 4, 0])
        b = self.call(0x1013, [i32(77), u32(9)])
        self.assertEqual([len(x) for x in b], [4, 52, 0])
        b = self.call(0x1025, [wpath("Z:/nope")])
        self.assertEqual((code(b), len(b[1])), (t.FS_FILE_NOT_FOUND, 24))

    def test_handle_table_limit(self):
        hs = [self.open("Z:/NVRAM/NVD_DATA/CK00_000", 0x100) for _ in range(128)]
        self.assertEqual(sorted(hs), list(range(1, 129)))
        self.assertEqual(self.open("Z:/NVRAM/NVD_DATA/CK00_000", 0x100), t.FS_TOO_MANY_FILES)
        self.assertEqual(code(self.call(0x1006, [])), 0)
        self.assertEqual(self.open("Z:/NVRAM/NVD_DATA/CK00_000", 0x100), 1)

    def test_dirs(self):
        self.assertEqual(code(self.call(0x1007, [wpath("S:\\mdota\\sub")])), 0)
        self.assertTrue(os.path.isdir(os.path.join(self.nv, "nvcfg/mdota/sub")))
        self.assertEqual(code(self.call(0x1007, [wpath("S:\\mdota")])), t.FS_GENERAL_FAILURE)
        self.assertEqual(code(self.call(0x1008, [wpath("S:/mdota/sub")])), 0)
        self.assertEqual(code(self.call(0x1008, [wpath("S:/mdota/sub")])), t.FS_FILE_NOT_FOUND)
        self.assertEqual(code(self.call(0x1008, [wpath("Z:/NVRAM")])), t.FS_GENERAL_FAILURE)

    def test_delete_rename(self):
        self.write("nv/protect1/md/LD0B_001", b"x")
        self.assertEqual(code(self.call(0x100B, [wpath("X:/LD0B_001"), wpath("X:/LD0B_002")])), 0)
        self.assertTrue(os.path.exists(os.path.join(self.nv, "protect1/md/LD0B_002")))
        self.assertEqual(code(self.call(0x100F, [wpath("X:/LD0B_002")])), 0)
        self.assertEqual(code(self.call(0x100F, [wpath("X:/LD0B_002")])), t.FS_FILE_NOT_FOUND)
        self.assertEqual(code(self.call(0x100F, [wpath("Z:/NVRAM")])), t.FS_GENERAL_FAILURE)

    def test_move_copies_and_keeps_source(self):
        self.write("nv/protect1/md/MT00A001", b"A")
        b = self.call(0x100C, [wpath("X:\\MT00A001"), wpath("Y:\\MT00A001"), u32(0x10001)])
        self.assertEqual(code(b), 0)
        self.assertEqual(self.read("nv/protect2/md/MT00A001"), b"A")
        self.assertEqual(self.read("nv/protect1/md/MT00A001"), b"A")
        self.write("nv/protect1/md/MT00A001", b"B")
        self.assertEqual(code(self.call(0x100C, [wpath("X:/MT00A001"), wpath("Y:/MT00A001"),
                                                 u32(0x1)])), t.FS_GENERAL_FAILURE)
        self.assertEqual(self.read("nv/protect2/md/MT00A001"), b"A")
        self.assertEqual(code(self.call(0x100C, [wpath("X:/NOPE"), wpath("Y:/NOPE"),
                                                 u32(0x10001)])), t.FS_FILE_NOT_FOUND)
        self.assertEqual(code(self.call(0x100C, [wpath("X:/MT00A001"), wpath("Y:/K"),
                                                 u32(t.FS_MOVE_KILL)])), 0)
        self.assertFalse(os.path.exists(os.path.join(self.nv, "protect1/md/MT00A001")))
        self.assertEqual(self.read("nv/protect2/md/K"), b"B")

    def test_write_refused_below_free_space_reserve(self):
        h = self.open("Z:/NVRAM/NVD_DATA/NEW_002", t.FS_CREATE)
        with mock.patch.object(t, "FS_MIN_FREE", 1 << 62):
            b = self.call(0x1004, [i32(h), b"hello", u32(5)])
        self.assertEqual(code(b), t.FS_DISK_FULL)
        self.assertEqual(code(self.call(0x1005, [i32(h)])), 0)
        self.assertEqual(self.read("nv/nvdata/md/NVRAM/NVD_DATA/NEW_002"), b"")

    def test_sparse_seek_counts_the_hole(self):
        v = os.statvfs(self.nv)
        free = v.f_bavail * v.f_frsize
        h = self.open("Z:/NVRAM/NVD_DATA/SPARSE", t.FS_CREATE)
        # Reserva = livre - 1 MiB: um seek de 2 MiB seguido de 1 byte nao cabe.
        with mock.patch.object(t, "FS_MIN_FREE", free - (1 << 20)):
            self.assertEqual(code(self.call(0x1002, [i32(h), i32(2 << 20), u32(0)])), 2 << 20)
            self.assertEqual(code(self.call(0x1004, [i32(h), b"x", u32(1)])), t.FS_DISK_FULL)
        self.call(0x1005, [i32(h)])

    def test_failed_directory_copy_leaves_nothing(self):
        self.write("nv/protect1/md/D/A", b"A")

        def boom(src, dst, symlinks=False):
            os.makedirs(os.path.join(dst, "parcial"))
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(t.shutil, "copytree", boom):
            b = self.call(0x100C, [wpath("X:/D"), wpath("X:/E"), u32(t.FS_MOVE_COPY)])
        self.assertNotEqual(code(b), 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.nv, "protect1/md"))), ["D"])

    def test_count_folder_size_attributes_detail(self):
        self.write("nv/nvdata/md/NVRAM/CALIBRAT/A", b"12345")
        self.write("nv/nvdata/md/NVRAM/CALIBRAT/B", b"123")
        self.assertEqual(code(self.call(0x100D, [wpath("Z:/NVRAM/CALIBRAT"), u32(0)])), 2)
        self.assertEqual(code(self.call(0x100A, [wpath("Z:/NVRAM/CALIBRAT"), u32(0)])), 8)
        self.assertEqual(code(self.call(0x1010, [wpath("Z:/NVRAM")])), t.FS_ATTR_DIR)
        ro = self.write("nv/nvdata/md/RO", b"r")
        os.chmod(ro, 0o444)
        self.assertEqual(code(self.call(0x1010, [wpath("Z:/RO")])), t.FS_ATTR_READ_ONLY)
        self.assertEqual(code(self.call(0x1010, [wpath("Z:/NVRAM/CALIBRAT/A")])), 0)
        self.assertEqual(code(self.call(0x1010, [wpath("Z:/NONE")])), t.FS_FILE_NOT_FOUND)
        os.utime(ro, (1_700_000_000, 1_600_000_000))
        b = self.call(0x1025, [wpath("Z:/RO")])
        mt, at, ct = struct.unpack("<QQQ", b[1])
        self.assertEqual((code(b), mt, at), (0, 1_600_000_000, 1_700_000_000))
        self.assertGreater(ct, 0)

    def test_disk_info_and_cluster_size(self):
        st = os.statvfs(self.drives["Z"].root)
        b = self.call(0x100E, [wpath("Z:\\"), u32(3)])
        self.assertEqual((code(b), len(b[1])), (0, 84))
        f = struct.unpack_from("<IIII", b[1], 48)
        self.assertEqual(f, (512, st.f_bsize >> 9, st.f_blocks & 0xFFFFFFFF, 0))
        self.assertEqual(struct.unpack_from("<I", b[1], 64)[0], 512)
        self.assertEqual(b[1][:48], bytes(48))
        self.assertEqual(code(self.call(0x101B, [u32(ord("Z"))])), st.f_bsize)
        self.assertEqual(code(self.call(0x101B, [u32(ord("C"))])), t.FS_DRIVE_NOT_FOUND)

    def test_fixed_answers(self):
        self.assertEqual(code(self.call(0x101A, [u32(4), u32(2), u32(0xC)])), 0x5A)
        self.assertEqual(code(self.call(0x101A, [u32(2), u32(1), u32(0)])), -2)
        for op, want in ((0x1015, 0), (0x1016, 1), (0x1019, 0), (0x101C, 0), (0x1023, -2),
                         (0x101D, -2), (0x1020, -2)):
            with self.subTest(op=t.FS_OPS[op]):
                self.assertEqual(code(self.call(op, [u32(0)])), want)
        self.assertEqual([len(x) for x in self.call(0x101E, [])], [4, 0])
        self.assertEqual([len(x) for x in self.call(0x101F, [])], [4, 4])
        self.assertEqual(code(self.call(0x1021, [wpath("Z:/NVRAM/X")])), t.FS_ACCESS_DENIED)
        self.assertEqual(code(self.call(0x1018, [wpath("Z:/NVRAM"), u32(0)])),
                         t.FS_ACCESS_DENIED)
        self.assertTrue(os.path.isdir(os.path.join(self.nv, "nvdata/md/NVRAM")))

    def test_open_hint(self):
        hint = b"HINTHINT"
        b = self.call(0x1011, [wpath("Z:/NVRAM/NVD_DATA/CK00_000"), u32(0x100), hint])
        self.assertGreaterEqual(code(b), 1)
        self.assertEqual(b[1], hint)
        b = self.call(0x1011, [wpath("Z:/NOPE"), u32(0x100), hint])
        self.assertEqual((code(b), b[1]), (t.FS_FILE_NOT_FOUND, hint))

    def test_read_only_drives(self):
        h = self.open("R:\\cacerts\\tls\\root.pem", 0x100)
        self.assertGreaterEqual(h, 1)
        self.assertEqual(self.call(0x1003, [i32(h), u32(10)])[2], b"PEM")
        self.assertEqual(self.open("R:/cacerts/tls/root.pem", 0), t.FS_GENERAL_FAILURE)
        self.assertEqual(self.open("R:/cacerts/tls/new", t.FS_CREATE), t.FS_GENERAL_FAILURE)
        self.assertEqual(self.open("R:/cacerts/tls/absent", 0), t.FS_FILE_NOT_FOUND)
        self.assertEqual(code(self.call(0x100F, [wpath("R:/cacerts/tls/root.pem")])),
                         t.FS_GENERAL_FAILURE)
        self.assertEqual(code(self.call(0x1007, [wpath("R:/x")])), t.FS_GENERAL_FAILURE)
        self.assertTrue(os.path.exists(os.path.join(self.ro, "etc_md/cacerts/tls/root.pem")))
        # W: e T: sem raiz (padrao): PATH_NOT_FOUND
        self.assertEqual(self.open("W:/fw.bin", 0x100), t.FS_PATH_NOT_FOUND)
        self.assertEqual(self.open("T:/MP2.mcfota", 0x100), t.FS_PATH_NOT_FOUND)

    def test_fsync_on_close_only_on_durable_drives(self):
        real = os.fsync
        calls = []

        def spy(fd):
            calls.append(fd)
            return real(fd)
        with mock.patch.object(t.os, "fsync", side_effect=spy):
            h = self.open("Z:/NVRAM/NVD_DATA/D1", t.FS_CREATE)
            self.call(0x1004, [i32(h), b"x", u32(1)])
            self.call(0x1005, [i32(h)])
            n_durable = len(calls)
            h = self.open("U:/lpm", t.FS_CREATE)
            self.call(0x1004, [i32(h), b"x", u32(1)])
            self.call(0x1005, [i32(h)])
            h = self.open("Z:/NVRAM/NVD_DATA/CK00_000", 0x100)
            self.call(0x1005, [i32(h)])
        self.assertGreaterEqual(n_durable, 1)
        self.assertEqual(len(calls), n_durable)

    def test_stats_and_debug_log(self):
        self.open("Z:/NVRAM/NOPE", 0)
        self.open("Z:/NVRAM/NVD_DATA/CK00_000", 0x100)
        st = self.srv.stats
        self.assertEqual((st.requests, st.absent, st.errors), (2, 1, 0))
        self.assertIn("Open 2", st.summary())
        lines = [c.args for c in self.log.call_args_list if c.args[0] == t.LOG_DEBUG]
        self.assertTrue(any("path=Z:/NVRAM/NOPE" in m and "FILE_NOT_FOUND" in m
                            and "flags=0x0" in m and "slot=0" in m for _p, m in lines))


# --- caminhos --------------------------------------------------------------------------
class PathTest(FsFixture):
    def test_decode(self):
        self.assertEqual(t.decode_md_path(wpath("Z:\\NVRAM\\A") + b"junk"), "Z:/NVRAM/A")
        self.assertEqual(t.decode_md_path(b"Z\0:\0\\\0"), "Z:/")
        self.assertEqual(t.decode_md_path(b"Z\0:"), "Z")       # byte impar ignorado

    def test_resolve_ok(self):
        drive, host = self.srv.resolve("z:/NVRAM/./NVD_DATA//CK00_000")
        self.assertEqual(drive.letter, "Z")
        self.assertEqual(host, os.path.realpath(
            os.path.join(self.nv, "nvdata/md/NVRAM/NVD_DATA/CK00_000")))
        self.assertEqual(self.srv.resolve("Z:")[1], os.path.realpath(self.drives["Z"].root))

    def assertRefused(self, path, want=t.FS_PATH_NOT_FOUND):
        with self.assertRaises(t.FsError) as cm:
            self.srv.resolve(path)
        self.assertEqual(cm.exception.code, want, path)
        self.assertEqual(self.open(path, 0x100), want)

    def test_escapes_refused(self):
        self.assertRefused("Z:/../protect1/md")
        self.assertRefused("Z:/NVRAM/../../../../etc/passwd")
        self.assertRefused("/etc/passwd")
        self.assertRefused("\\etc\\passwd")
        self.assertRefused("NVRAM/X")
        self.assertRefused("Z:NVRAM")                      # relativo ao drive
        self.assertRefused("Z:/a\x01b")
        self.assertRefused("C:/x", t.FS_DRIVE_NOT_FOUND)
        self.assertRefused("Z:/" + "a" * 4100, t.FS_PATH_OVER_LEN)

    def test_symlinks_out_refused(self):
        os.symlink("/etc/passwd", os.path.join(self.nv, "nvdata/md/LINK"))
        os.symlink("/etc", os.path.join(self.nv, "nvdata/md/DIRLINK"))
        os.symlink(os.path.join(self.nv, "protect1/md"), os.path.join(self.nv, "nvdata/md/SIB"))
        self.assertRefused("Z:/LINK")
        self.assertRefused("Z:/DIRLINK/passwd")
        self.assertRefused("Z:/SIB/x")                     # outro drive tambem e fuga
        self.assertEqual(code(self.call(0x1007, [wpath("Z:/DIRLINK/new")])), t.FS_PATH_NOT_FOUND)
        self.assertEqual(code(self.call(0x100B, [wpath("Z:/NVRAM/NVD_DATA/CK00_000"),
                                                 wpath("Z:/DIRLINK/stolen")])),
                         t.FS_PATH_NOT_FOUND)
        self.assertTrue(os.path.exists(os.path.join(self.nv, "nvdata/md/NVRAM/NVD_DATA/CK00_000")))

    def test_symlink_inside_allowed(self):
        os.symlink("NVRAM/NVD_DATA/CK00_000", os.path.join(self.nv, "nvdata/md/ALIAS"))
        h = self.open("Z:/ALIAS", 0x100)
        self.assertEqual(self.call(0x1003, [i32(h), u32(3)])[2], b"012")

    def test_drive_without_root(self):
        shutil.rmtree(os.path.join(self.nv, "vendor_de_md"))
        self.assertRefused("Q:/cacerts/x")
        self.assertEqual(code(self.call(0x1012, [wpath("W:/*.*"), b"\0", b"\0", u32(9)])),
                         t.FS_PATH_NOT_FOUND)


class ErrnoTest(unittest.TestCase):
    def test_official_map(self):
        cases = {errno.ENOENT: -9, errno.EACCES: -16, errno.EMFILE: -5, errno.EINVAL: -18,
                 errno.ENOSPC: -18, errno.EEXIST: -18, errno.EISDIR: -18, errno.EROFS: -18,
                 errno.ENOTDIR: -18, errno.EBADF: -18}
        for e, want in cases.items():
            with self.subTest(errno=errno.errorcode[e]):
                self.assertEqual(t.fs_errno(OSError(e, "x")), want)
        self.assertNotEqual(t.fs_errno(OSError(errno.EINVAL, "x")), -errno.EINVAL)
        self.assertEqual(t.fs_errno(OSError("no errno")), -18)


# --- CMPT_Read / CMPT_Write ------------------------------------------------------------
def cmpt_read_desc(steps: int, off: int, length: int) -> bytes:
    return struct.pack("<10I", steps, 0, 0, 0x700, 0x1000, off, 0, 0x2000, length, 0x3000)


def cmpt_write_desc(flags: int, mode: int, off: int, whence: int, length: int,
                    handle: int) -> bytes:
    d = bytearray(48)
    struct.pack_into("<I", d, 0, flags)
    struct.pack_into("<I", d, 12, mode)
    struct.pack_into("<i", d, 16, off)
    struct.pack_into("<I", d, 20, whence)
    struct.pack_into("<I", d, 24, 0x90001234)      # ponteiro do MD, ignorado
    struct.pack_into("<I", d, 28, length)
    struct.pack_into("<i", d, 32, handle)
    return bytes(d)


class CmptTest(FsFixture):
    PATH = "Z:\\NVRAM\\NVD_DATA\\CK00_000"

    def test_read_layout(self):
        b = self.call(0x1022, [wpath(self.PATH), cmpt_read_desc(0x1D, 4, 4)])
        self.assertEqual(len(b), 4)
        self.assertEqual(struct.unpack("<Ii", b[0]), (0x1D, 0))
        self.assertEqual(struct.unpack("<I", b[1])[0], 0)
        self.assertEqual(struct.unpack("<I", b[2])[0], 4)
        self.assertEqual(b[3], b"4567")

    def test_read_from_start_and_size_step(self):
        b = self.call(0x1022, [wpath(self.PATH), cmpt_read_desc(0x1B, 0, 100)])
        self.assertEqual(struct.unpack("<Ii", b[0]), (0x1B, 0))
        self.assertEqual(struct.unpack("<I", b[1])[0], 10)     # bit1 pedido: tamanho
        self.assertEqual((struct.unpack("<I", b[2])[0], b[3]), (10, b"0123456789"))

    def test_read_steps_0x1f_seen_on_hardware(self):
        # open|GETFILESIZE|seek|read|close: o bloco 1 tem de levar o tamanho real
        b = self.call(0x1022, [wpath(self.PATH), cmpt_read_desc(0x1F, 0, 10)])
        self.assertEqual(struct.unpack("<Ii", b[0]), (0x1F, 0))
        self.assertEqual(struct.unpack("<I", b[1])[0], 10)
        self.assertEqual((struct.unpack("<I", b[2])[0], b[3]), (10, b"0123456789"))

    def test_read_absent_and_unmapped(self):
        b = self.call(0x1022, [wpath("Z:/NVRAM/NONE"), cmpt_read_desc(0x19, 0, 16)])
        self.assertEqual(struct.unpack("<Ii", b[0]), (0, t.FS_FILE_NOT_FOUND))
        self.assertEqual((b[1], b[2], b[3]), (u32(0), u32(0), b""))
        b = self.call(0x1022, [wpath("W:/x"), cmpt_read_desc(0x19, 0, 16)])
        self.assertEqual(struct.unpack("<Ii", b[0]), (0, t.FS_PATH_NOT_FOUND))

    def test_write_chained_chunks(self):
        path = "Z:\\NVRAM\\NVD_DATA\\NR06_009"
        b = self.call(0x1024, [wpath(path), cmpt_write_desc(0x21, t.FS_CREATE, 0, 0, 6, -1),
                               b"AAAAAA"])
        done, status = struct.unpack("<Ii", b[0])
        h = code(b, 1)
        self.assertEqual((done, status, struct.unpack("<I", b[2])[0]), (0x21, 0, 6))
        self.assertGreaterEqual(h, 1)
        b = self.call(0x1024, [wpath(path), cmpt_write_desc(0x31, t.FS_CREATE, 0, 0, 4, h),
                               b"BBBB"])
        self.assertEqual(struct.unpack("<Ii", b[0]), (0x30, 0))
        self.assertEqual((code(b, 1), struct.unpack("<I", b[2])[0]), (h, 4))
        self.assertNotIn(h, self.srv.handles)
        self.assertEqual(self.read("nv/nvdata/md/NVRAM/NVD_DATA/NR06_009"), b"AAAAAABBBB")

    def test_write_with_seek_single_shot(self):
        b = self.call(0x1024, [wpath(self.PATH), cmpt_write_desc(0x35, 0, 2, 0, 3, -1), b"xyz"])
        self.assertEqual(struct.unpack("<Ii", b[0]), (0x35, 0))
        self.assertEqual(self.read("nv/nvdata/md/NVRAM/NVD_DATA/CK00_000"), b"01xyz56789")
        self.assertEqual(self.srv.handles, {})

    def test_write_length_caps_payload(self):
        self.call(0x1024, [wpath("Z:/C"), cmpt_write_desc(0x31, t.FS_CREATE, 0, 0, 2, -1),
                           b"abcd"])
        self.assertEqual(self.read("nv/nvdata/md/C"), b"ab")

    def test_write_errors(self):
        b = self.call(0x1024, [wpath("Z:/NONE"), cmpt_write_desc(0x31, 0, 0, 0, 1, -1), b"x"])
        self.assertEqual(struct.unpack("<Ii", b[0]), (0, t.FS_FILE_NOT_FOUND))
        b = self.call(0x1024, [wpath("Z:/C"), cmpt_write_desc(0x30, 0, 0, 0, 1, 55), b"x"])
        self.assertEqual(struct.unpack("<Ii", b[0]), (0, t.FS_INVALID_FILE_HANDLE))
        b = self.call(0x1024, [wpath("R:/cacerts/tls/root.pem"),
                               cmpt_write_desc(0x31, 0, 0, 0, 1, -1), b"x"])
        self.assertEqual(struct.unpack("<Ii", b[0]), (0, t.FS_GENERAL_FAILURE))
        self.assertEqual(self.read("ro/etc_md/cacerts/tls/root.pem"), b"PEM")


# --- FindFirst / FindNext ---------------------------------------------------------------
class FindTest(FsFixture):
    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.nv, "nvdata/md/NVRAM/CALIBRAT")
        self.write("nv/nvdata/md/NVRAM/CALIBRAT/ML1A_000", b"x" * 300)
        self.write("nv/nvdata/md/NVRAM/CALIBRAT/NOTE.TXT", b"y")
        os.mkdir(os.path.join(self.dir, "SUB"))
        self.stamp = calendar.timegm((2026, 10, 15, 12, 34, 56, 0, 0, 0))
        os.utime(os.path.join(self.dir, "ML1A_000"), (self.stamp, self.stamp))

    def find_all(self, pattern, want=0, forbid=0, cap=9):
        b = self.call(0x1012, [wpath(pattern), bytes([want]), bytes([forbid]), u32(cap)])
        h = code(b)
        if h < 0:
            return h, []
        got = [b]
        while True:
            n = self.call(0x1013, [i32(h), u32(cap)])
            if code(n) != 0:
                self.assertEqual(code(n), t.FS_NO_MORE_FILES)
                self.assertEqual(n[1], got[-1][1])          # ultima entrada fica no buffer
                self.assertEqual(n[2], b"")
                break
            got.append(n)
        return h, got

    @staticmethod
    def name_of(b):
        return b[2].decode("utf-16-le").rstrip("\0")

    def test_lists_dot_entries_then_readdir_order(self):
        h, got = self.find_all("Z:\\NVRAM\\CALIBRAT\\*.*")
        self.assertGreaterEqual(h, 1)
        names = [self.name_of(b) for b in got]
        self.assertEqual(names[:2], [".", ".."])
        self.assertEqual(names[2:], os.listdir(self.dir))
        # fim da lista libera o handle
        self.assertEqual(code(self.call(0x1013, [i32(h), u32(9)])), t.FS_INVALID_FILE_HANDLE)

    def test_entry_encoding(self):
        _h, got = self.find_all("Z:/NVRAM/CALIBRAT/ML1A*")
        self.assertEqual(len(got), 1)
        e = got[0][1]
        self.assertEqual(len(e), 52)
        self.assertEqual(e[:11], bytes(11))
        self.assertEqual((e[11], e[12]), (0, 1))
        dos = ((2026 - 1980) << 25) | (10 << 21) | (15 << 16) | (12 << 11) | (34 << 5) | 28
        self.assertEqual(struct.unpack_from("<I", e, 14)[0], dos)
        self.assertEqual(struct.unpack_from("<I", e, 22)[0], dos)
        self.assertEqual(struct.unpack_from("<I", e, 28)[0], 300)
        self.assertEqual(got[0][2], "ML1A_000".encode("utf-16-le") + b"\0\0")
        self.assertEqual(t.dos_datetime(self.stamp), dos)

    def test_name_truncated_to_capacity(self):
        _h, got = self.find_all("Z:/NVRAM/CALIBRAT/ML1A_000", cap=5)
        self.assertEqual(got[0][2], "ML1A".encode("utf-16-le") + b"\0\0")
        self.assertEqual(got[0][1][12], 0)

    def test_attribute_filter(self):
        _h, got = self.find_all("Z:/NVRAM/CALIBRAT/*", forbid=t.FS_ATTR_DIR)
        self.assertEqual(sorted(self.name_of(b) for b in got), ["ML1A_000", "NOTE.TXT"])
        _h, got = self.find_all("Z:/NVRAM/CALIBRAT/*", want=t.FS_ATTR_DIR)
        self.assertEqual(sorted(self.name_of(b) for b in got), [".", "..", "SUB"])
        dirs = [b for b in got if self.name_of(b) == "SUB"]
        self.assertEqual(dirs[0][1][11], t.FS_ATTR_DIR)
        self.assertEqual(struct.unpack_from("<I", dirs[0][1], 28)[0], 0)

    def test_no_match_and_absent_dir(self):
        for pattern in ("Z:/NVRAM/CALIBRAT/ZZ*", "Z:/NVRAM/NODIR/*.*"):
            with self.subTest(pattern=pattern):
                b = self.call(0x1012, [wpath(pattern), b"\0", b"\0", u32(9)])
                self.assertEqual(code(b), t.FS_NO_MORE_FILES)
                self.assertEqual((b[1], b[2]), (bytes(52), b""))
        self.assertEqual(code(self.call(0x1012, [wpath("Z:/../*"), b"\0", b"\0", u32(9)])),
                         t.FS_PATH_NOT_FOUND)

    def test_find_close(self):
        b = self.call(0x1012, [wpath("Z:/NVRAM/CALIBRAT/*.*"), b"\0", b"\0", u32(9)])
        h = code(b)
        self.assertEqual(code(self.call(0x1014, [i32(h)])), 0)
        self.assertNotIn(h, self.srv.handles)
        self.assertEqual(code(self.call(0x1014, [i32(99)])), 0)

    def test_dos_match(self):
        for name, pat, want in (("CK00_000", "*.*", True), ("A.TXT", "*.TXT", True),
                                ("a.txt", "*.TXT", True), ("CK00_000", "CK*.*", True),
                                ("CK00_000", "CK??_000", True), ("CK00_000", "LD*", False),
                                ("A[1]", "A[1]", True), ("A1", "A[1]", False)):
            with self.subTest(name=name, pat=pat):
                self.assertEqual(t.dos_match(name, pat), want)


# --- dados de boot, CCB, mensagens --------------------------------------------------------
class BootDataTest(unittest.TestCase):
    def test_bytes(self):
        data = t.boot_data("N/Lf/Lt/W/G")
        self.assertEqual(len(data), 64)
        self.assertEqual(data[:20], u32(0) + u32(0) + u32(0xFFFFFFFF) + u32(0) + u32(0))
        self.assertEqual(data[20:44], b"N/Lf/Lt/W/G".ljust(24, b"\0"))
        self.assertEqual(data[44:], bytes(20))                 # wm_idx 0 e o resto
        self.assertEqual(data.hex(),
                         "00000000" "00000000" "ffffffff" "00000000" "00000000"
                         + b"N/Lf/Lt/W/G".hex() + "00" * 13      # RAT_STR0..5, 24 B
                         + "00000000"                           # WM_IDX
                         + "00" * 16)

    def test_rat_limit(self):
        t.boot_data("A" * 23)
        with self.assertRaises(ValueError):
            t.boot_data("A" * 24)

    def test_ioctl_numbers_match_spec(self):
        self.assertEqual(t.CCCI_IOC_GET_MD_STATE, 0x80044301)
        self.assertEqual(t.CCCI_IOC_GET_MD_INFO, 0x80044308)
        self.assertEqual(t.CCCI_IOC_DO_STOP_MD, 0x430C)
        self.assertEqual(t.CCCI_IOC_DO_START_MD, 0x430D)
        self.assertEqual(t.CCCI_IOC_SEND_BATTERY_INFO, 0x4315)
        self.assertEqual(t.CCCI_IOC_GET_MD_TYPE, 0x8004431F)
        self.assertEqual(t.CCCI_IOC_SET_BOOT_DATA, 0x4040432F)
        self.assertEqual(t.CCCI_IOC_GET_CCB_CONFIG_LENGTH, 0x8004433F)
        self.assertEqual(t.CCCI_IOC_GET_CCB_CONFIG, 0xC0184340)
        self.assertEqual(t.CCCI_IOC_CCB_CTRL_INFO, 0xC0104347)
        self.assertEqual((t.CCCI_IPC_UPDATE_TIME, t.CCCI_IPC_UPDATE_TIMEZONE), (0x5004, 0x5006))
        self.assertEqual(t.CCCI_IOC_UPDATE_SIM_SLOT_CFG, 0x40044326)
        self.assertEqual(t.CCB_CONFIG.size, 24)
        self.assertEqual(t.STA_EVENT.size, 56)


class CcbTest(unittest.TestCase):
    def test_header_bytes(self):
        h = t.ccb_header(dl_page=0x1000, ul_page=0x800, dl_buff=0x10000, ul_buff=0x8000)
        self.assertEqual(len(h), 0x40)
        want = bytearray(0x40)
        struct.pack_into("<III", want, 0x14, 0x1000, 0x10000, 0xEEFF0011)
        struct.pack_into("<III", want, 0x34, 0x800, 0x8000, 0xEEFF0011)
        self.assertEqual(h, bytes(want))
        self.assertEqual(h[0x1C:0x20], bytes.fromhex("1100ffee"))

    def test_ccb_init_writes_each_entry(self):
        dev = FakeDev()
        m = make_modemd(dev)
        n = m.ccb_init()
        self.assertEqual(n, 3)
        mm = dev.mm
        self.assertTrue(mm.closed)
        self.assertEqual(mm.length, 4096)
        for i in range(3):
            self.assertEqual(bytes(mm.buf[i * 0x40:(i + 1) * 0x40]),
                             t.ccb_header(4096 * (i + 1), 2048, 65536, 32768))
        self.assertEqual(bytes(mm.buf[0xC0:0x100]), bytes(0x40))
        users = [struct.unpack_from("<I", c[3])[0] for c in dev.calls
                 if c[0] == "ioctl" and c[2] == t.CCCI_IOC_CCB_CTRL_INFO]
        self.assertEqual(users, [0, 1, 2])
        self.assertEqual(dev.closed_paths(), [t.DEV_CCB])

    def test_ccb_region_too_small(self):
        dev = FakeDev()
        dev.ccb_ctrl_len = 0x80
        with self.assertRaises(t.Failure) as cm:
            make_modemd(dev).ccb_init()
        self.assertEqual(cm.exception.code, t.EX_PROTOCOL)


class MessagesTest(unittest.TestCase):
    def test_monitor(self):
        m = t.parse_monitor(struct.pack("<IIII", 0xFFFFFFFF, 0xFAF50005, 0xF0000000, 7))
        self.assertEqual((m.name, m.param, m.magic, m.channel),
                         ("RESET_REQUEST", 7, 0xFFFFFFFF, 0xF0000000))
        self.assertEqual(t.parse_monitor(struct.pack("<IIII", 0xFFFFFFFF, 0xFAF50006, 0, 0)).name,
                         "EXCEPTION")
        self.assertEqual(t.parse_monitor(struct.pack("<IIII", 0xFFFFFFFF, 0x1234, 0, 0)).name,
                         "0x00001234")
        self.assertIsNone(t.parse_monitor(b"\0" * 15))

    def test_md1_sta(self):
        raw = struct.pack("<qqii32s", 1_760_000_000, 123456, 0, 10, b"ccci_mdinit\0junk")
        self.assertEqual(len(raw), 56)
        ev = t.parse_sta_event(raw)
        self.assertEqual((ev.sec, ev.usec, ev.md_id, ev.event, ev.name, ev.reason),
                         (1_760_000_000, 123456, 0, 10, "READY", "ccci_mdinit"))
        self.assertEqual(t.parse_sta_event(struct.pack("<qqii32s", 0, 0, 0, 12, b"----")).name,
                         "STOP")
        self.assertIsNone(t.parse_sta_event(raw[:55]))

    def test_sys_boot(self):
        self.assertEqual(t.parse_sys_boot("md1:4"), 4)
        self.assertEqual(t.parse_sys_boot("md1:1\n"), 1)
        self.assertIsNone(t.parse_sys_boot(""))
        self.assertIsNone(t.parse_sys_boot(None))
        self.assertIsNone(t.parse_sys_boot("garbage"))


class RpcTest(unittest.TestCase):
    def setUp(self):
        self.srv = t.RpcServer(t.builtin_properties(t.DEFAULT_RAT))
        patcher = mock.patch.object(t, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def req(self, op, blocks, d0=0x1234, reserved=1):
        msg = t.encode_message(op, blocks)
        return struct.pack("<IIII", d0, 16 + len(msg), t.RPC_RX_CH, reserved) + msg

    def reply(self, pkt):
        out = self.srv.process_packet(pkt)
        self.assertEqual(len(out), 1)
        d0, length, ch, reserved = struct.unpack_from("<IIII", out[0])
        op, count = struct.unpack_from("<II", out[0], 16)
        self.assertEqual((length, ch), (len(out[0]), t.RPC_TX_CH))
        return d0, reserved, op, t.parse_blocks(out[0], 24, count)

    def test_property(self):
        d0, res, op, b = self.reply(self.req(0x400F, [b"ro.vendor.mtk_ps1_rat\0"]))
        self.assertEqual((d0, res, op), (0x1234, 1, 0xFFFF400F))
        self.assertEqual((code(b), b[1]), (11, b"N/Lf/Lt/W/G\0"))
        _d0, _r, _op, b = self.reply(self.req(0x400F, [b"persist.unknown\0"]))
        self.assertEqual((code(b), b[1]), (0, b"\0"))

    def test_crypto_state_default(self):
        _d0, _r, _op, b = self.reply(self.req(0x400F, [b"ro.crypto.state\0"]))
        self.assertEqual((code(b), b[1]), (11, b"unencrypted\0"))

    def test_sar(self):
        _d0, _r, op, b = self.reply(self.req(0x4010, [u32(0)]))
        self.assertEqual((op, code(b), struct.unpack("<I", b[1])[0]), (0xFFFF4010, 0, 0xFFFF))

    def test_other_ops_ignored(self):
        self.assertEqual(self.srv.process_packet(self.req(0x4001, [])), [])
        self.assertEqual(self.srv.process_packet(b"\0" * 10), [])

    def test_properties_follow_rat(self):
        p = t.builtin_properties("Lf/Lt/W/G")
        self.assertEqual(p["ro.vendor.mtk_protocol1_rat_config"], "Lf/Lt/W/G")
        self.assertNotIn("ro.vendor.mtk_md1_support", p)


class PropertiesTest(unittest.TestCase):
    def test_overrides(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(t, "log"):
            pf = os.path.join(d, "props.conf")
            with open(pf, "w") as f:
                f.write("# x\nro.crypto.state=encrypted\npersist.a=1\nbad line\n"
                        "ro.x=\x01\n")
            values, warnings = t.parse_config(
                f"MODEM_PROPS_FILE={pf}\nMODEM_PROP_persist.a=2\nMODEM_PROP_ro.b='v w'\n")
            self.assertEqual(warnings, [])
            cfg = t.config_from_values(values)
            props = t.load_properties(cfg)
        self.assertEqual(props["ro.crypto.state"], "encrypted")
        self.assertEqual(props["persist.a"], "2")              # modem.conf vence o arquivo
        self.assertEqual(props["ro.b"], "v w")
        self.assertNotIn("ro.x", props)
        self.assertEqual(props["ro.vendor.mtk_ps1_rat"], "N/Lf/Lt/W/G")

    def test_missing_file_and_bad_value(self):
        with mock.patch.object(t, "log"):
            props = t.load_properties(t.Config(props_file="/nonexistent/props.conf"))
        self.assertEqual(props["ro.crypto.state"], "unencrypted")
        with self.assertRaises(t.ConfigError):
            t.config_from_values({"MODEM_PROP_ro.a": "x" * 92})


class CaptureTest(unittest.TestCase):
    def test_records_and_limit(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cap.bin")
            with mock.patch.dict(os.environ, {"THUNDER_MODEMD_CAPTURE": path,
                                              "THUNDER_MODEMD_CAPTURE_MAX": "100"}), \
                    mock.patch.object(t, "log"):
                cap = t.Capture.from_env()
                cap.record(t.Capture.FS_RX, b"abc")
                cap.record(t.Capture.FS_TX, b"x" * 200)        # passa do limite: para
                cap.record(t.Capture.FS_RX, b"d")
                cap.close()
            with open(path, "rb") as f:
                data = f.read()
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            magic, kind, _ts, n = t.Capture.RECORD.unpack_from(data)
            self.assertEqual((magic, kind, n, data[t.Capture.RECORD.size:]),
                             (b"TMDC", 0, 3, b"abc"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(t.Capture.from_env())


# --- configuracao ---------------------------------------------------------------------------
class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = t.config_from_values({})
        self.assertEqual((c.nv_root, c.ro_root, c.rat, c.boot_timeout),
                         ("/var/lib/thunder-modem/nv", "/usr/share/thunder-modem/vendor",
                          "N/Lf/Lt/W/G", 40))

    def test_parse(self):
        values, warnings = t.parse_config(
            "# comentario\n"
            "MODEM_NV_ROOT='/data/nv/'   # fim de linha\n"
            "MODEM_RAT=\"Lf/Lt/W/G\"\n"
            "MODEM_BOOT_TIMEOUT=60\n"
            "garbage line\n"
            "lower=1\n"
            "MODEM_RO_ROOT=a b\n"
            "MODEM_X=1\n"
            "MODEM_RAT2='open\n")
        self.assertEqual(values, {"MODEM_NV_ROOT": "/data/nv/", "MODEM_RAT": "Lf/Lt/W/G",
                                  "MODEM_BOOT_TIMEOUT": "60"})
        self.assertEqual(len(warnings), 5)
        c = t.config_from_values(values)
        self.assertEqual((c.nv_root, c.rat, c.boot_timeout), ("/data/nv", "Lf/Lt/W/G", 60))

    def test_rejects(self):
        for values in ({"MODEM_NV_ROOT": "relative"}, {"MODEM_NV_ROOT": "/var/../etc"},
                       {"MODEM_NV_ROOT": "/"}, {"MODEM_NV_ROOT": "/a b"},
                       {"MODEM_RO_ROOT": "/var/lib/thunder-modem/nv/ro"},
                       {"MODEM_RAT": "N/C/Lf/Lt/W/G/Lf/Lt/W/G/X"}, {"MODEM_RAT": "N;reboot"},
                       {"MODEM_BOOT_TIMEOUT": "x"}, {"MODEM_BOOT_TIMEOUT": "5"},
                       {"MODEM_BOOT_TIMEOUT": "1000"}):
            with self.subTest(values=values), self.assertRaises(t.ConfigError):
                t.config_from_values(values)

    def test_load_missing_and_invalid(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(t, "log"):
            self.assertEqual(t.load_config(os.path.join(d, "none.conf")), t.Config())
            p = os.path.join(d, "modem.conf")
            with open(p, "w") as f:
                f.write("MODEM_BOOT_TIMEOUT=2\n")
            with self.assertRaises(t.Failure) as cm:
                t.load_config(p)
            self.assertEqual(cm.exception.code, t.EX_CONFIG)


# --- comm da thread de partida ----------------------------------------------------------
class CommTest(unittest.TestCase):
    def test_start_from_thread_with_comm(self):
        order = []
        state = {"comm": "python3"}

        def prctl(option, name, *rest):
            order.append(("prctl", threading.current_thread().name, option, name.value))
            state["comm"] = name.value.decode()
            return 0

        dev = mock.Mock()
        dev.ioctl.side_effect = lambda fd, req, arg=0: order.append(
            ("ioctl", threading.current_thread().name, req, state["comm"]))
        t.start_md(dev, 9, prctl=prctl, comm=lambda: state["comm"])
        self.assertEqual(order[0], ("prctl", "ccci_mdinit", t.PR_SET_NAME, b"ccci_mdinit"))
        self.assertEqual(order[1], ("ioctl", "ccci_mdinit", t.CCCI_IOC_DO_START_MD, "ccci_mdinit"))
        self.assertNotEqual(threading.current_thread().name, "ccci_mdinit")
        dev.ioctl.assert_called_once_with(9, t.CCCI_IOC_DO_START_MD, 0)

    def test_wrong_comm_never_starts(self):
        dev = mock.Mock()
        with self.assertRaises(t.Failure) as cm:
            t.start_md(dev, 9, prctl=lambda *a: 0, comm=lambda: "python3")
        self.assertEqual(cm.exception.code, t.EX_SOFTWARE)
        dev.ioctl.assert_not_called()

    def test_prctl_failure_never_starts(self):
        def prctl(*a):
            raise OSError(errno.EINVAL, "Invalid argument")
        dev = mock.Mock()
        with self.assertRaises(t.Failure):
            t.start_md(dev, 9, prctl=prctl, comm=lambda: "ccci_mdinit")
        dev.ioctl.assert_not_called()

    def test_ioctl_error(self):
        dev = mock.Mock()
        dev.ioctl.side_effect = OSError(errno.EBUSY, "Device or resource busy")
        with self.assertRaises(t.Failure) as cm:
            t.start_md(dev, 9, prctl=lambda *a: 0, comm=lambda: "ccci_mdinit")
        self.assertEqual(cm.exception.code, t.EX_PROTOCOL)

    @unittest.skipUnless(sys.platform.startswith("linux"), "prctl is Linux only")
    def test_real_prctl_is_per_thread(self):
        before = t.thread_comm()
        seen = {}

        def body():
            t.set_thread_comm("ccci_mdinit")
            seen["comm"] = t.thread_comm()
        th = threading.Thread(target=body, name="x")
        th.start()
        th.join()
        self.assertEqual(seen["comm"], "ccci_mdinit")
        self.assertEqual(t.thread_comm(), before)


# --- servico com um kernel de mentira -----------------------------------------------------
class FakeMmap:
    def __init__(self, length):
        self.length = length
        self.buf = bytearray(length)
        self.closed = False

    def __setitem__(self, key, value):
        self.buf[key] = value

    def close(self):
        self.closed = True


class FakeDev(t.Dev):
    """eccci simulado: guarda as chamadas e muda de estado com DO_START/DO_STOP."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.fd = 100
        self.paths = {}
        self.sys_boot = "md1:1"
        self.state = 0
        self.started = False
        self.boot_ok = True
        self.polls = 0
        self.ready_after = 2
        self.mm = None
        self.ccb_n = 3
        self.ccb_ctrl_len = 0x1000
        self.busy = set()

    def record(self, *c):
        with self.lock:
            self.calls.append(c)

    def open(self, path, flags):
        if path in self.busy:
            raise OSError(errno.EBUSY, "Device or resource busy")
        self.fd += 1
        self.paths[self.fd] = path
        self.record("open", path)
        return self.fd

    def close(self, fd):
        self.record("close", self.paths[fd])

    def readable(self, fd, timeout):
        return False

    def read_text(self, path):
        return self.sys_boot if path == t.SYS_BOOT else None

    def mmap(self, fd, length):
        self.mm = FakeMmap(length)
        return self.mm

    def ioctl(self, fd, req, arg=0):
        path = self.paths[fd]
        self.record("ioctl", path, req, bytes(arg) if isinstance(arg, bytearray) else arg)
        if req == t.CCCI_IOC_GET_MD_STATE:
            if self.started and self.boot_ok:
                self.polls += 1
                if self.polls >= self.ready_after:
                    self.state, self.sys_boot = 2, "md1:4"
            struct.pack_into("<I", arg, 0, self.state)
        elif req == t.CCCI_IOC_DO_START_MD:
            self.started, self.state, self.sys_boot, self.polls = True, 1, "md1:2", 0
            self.record("start-thread", threading.current_thread().name)
        elif req == t.CCCI_IOC_DO_STOP_MD:
            self.started, self.state, self.sys_boot = False, 0, "md1:1"
        elif req == t.CCCI_IOC_GET_CCB_CONFIG_LENGTH:
            struct.pack_into("<I", arg, 0, self.ccb_n)
        elif req == t.CCCI_IOC_GET_CCB_CONFIG:
            i = struct.unpack_from("<I", arg)[0]
            arg[:] = t.CCB_CONFIG.pack(i, 0, 4096 * (i + 1), 2048, 65536, 32768)
        elif req == t.CCCI_IOC_CCB_CTRL_INFO:
            u = struct.unpack_from("<I", arg)[0]
            arg[:] = t.CCB_CTRL_INFO.pack(u, u * 0x40, 0x12340000, self.ccb_ctrl_len)
        elif req in (t.CCCI_IOC_GET_MD_TYPE, t.CCCI_IOC_GET_MD_INFO, t.CCCI_IOC_GET_MD_EX_TYPE):
            struct.pack_into("<I", arg, 0, 22)
        return 0

    def index(self, *prefix):
        for i, c in enumerate(self.calls):
            if c[:len(prefix)] == prefix:
                return i
        raise AssertionError(f"{prefix} not called; calls={self.calls}")

    def ioctls(self, req):
        return [c for c in self.calls if c[0] == "ioctl" and c[2] == req]

    def closed_paths(self):
        return [c[1] for c in self.calls if c[0] == "close"]


def make_modemd(dev, **kw):
    notes = []
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("THUNDER_MODEMD_CAPTURE", None)
        m = t.Modemd(t.Config(**kw), dev=dev, notify=notes.append,
                     prctl=lambda *a: 0, comm=lambda: "ccci_mdinit")
    m.notes = notes
    return m


class ModemdTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(t, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)
        self.dev = FakeDev()
        self.m = make_modemd(self.dev)

    def test_boot_sequence_order(self):
        self.m.open_devices()
        self.m.boot()
        d = self.dev
        order = [d.index("open", t.DEV_FS), d.index("open", t.DEV_RPC),
                 d.index("open", t.DEV_MONITOR), d.index("open", t.DEV_MD1_STA),
                 d.index("open", t.DEV_CCB),
                 d.index("ioctl", t.DEV_MONITOR, t.CCCI_IOC_SET_BOOT_DATA),
                 d.index("ioctl", t.DEV_MONITOR, t.CCCI_IOC_UPDATE_SIM_SLOT_CFG),
                 d.index("ioctl", t.DEV_MONITOR, t.CCCI_IOC_DO_START_MD),
                 d.index("ioctl", t.DEV_IPC_TIME, t.CCCI_IPC_UPDATE_TIMEZONE),
                 d.index("ioctl", t.DEV_IPC_TIME, t.CCCI_IPC_UPDATE_TIME)]
        self.assertEqual(order, sorted(order))
        self.assertLess(d.index("close", t.DEV_CCB),
                        d.index("ioctl", t.DEV_MONITOR, t.CCCI_IOC_DO_START_MD))
        self.assertEqual(d.ioctls(t.CCCI_IOC_SET_BOOT_DATA)[0][3], t.boot_data("N/Lf/Lt/W/G"))
        self.assertEqual(d.ioctls(t.CCCI_IPC_UPDATE_TIME)[0][3], 0)
        self.assertEqual(d.ioctls(t.CCCI_IOC_UPDATE_SIM_SLOT_CFG)[0][3],
                         struct.pack("<4I", 1, 0, 0, 0))
        self.assertIn(("start-thread", "ccci_mdinit"), d.calls)
        self.assertNotIn("/dev/ttyC0", [c[1] for c in d.calls if c[0] == "open"])
        self.assertIn("READY=1\nSTATUS=modem ready", self.m.notes)
        self.assertTrue(self.m.md_ready and self.m.md_running)
        self.assertTrue(any("modem ready" == c.args[1] for c in self.log.call_args_list))

    def test_ready_notified_only_after_ready(self):
        self.m.open_devices()
        self.dev.ready_after = 4
        self.m.boot()
        i_ready = next(i for i, n in enumerate(self.m.notes) if n.startswith("READY=1"))
        self.assertEqual(i_ready, len(self.m.notes) - 1)
        self.assertGreaterEqual(self.dev.polls, 4)

    def test_shutdown_stops_then_closes_monitor_last(self):
        self.m.open_devices()
        self.m.boot()
        n_before = len(self.dev.calls)
        self.m.shutdown()
        tail = self.dev.calls[n_before:]
        stop = [i for i, c in enumerate(tail) if c[0] == "ioctl" and c[2] == t.CCCI_IOC_DO_STOP_MD]
        closes = [i for i, c in enumerate(tail) if c[0] == "close"]
        self.assertEqual(len(stop), 1)
        self.assertEqual(tail[stop[0]][3], bytes(4))           # &0: desligar, nao flight
        self.assertLess(stop[0], closes[0])
        self.assertEqual([tail[i][1] for i in closes],
                         [t.DEV_IPC_TIME, t.DEV_RPC, t.DEV_MD1_STA, t.DEV_FS, t.DEV_MONITOR])
        self.assertEqual(self.m.fds, {})
        self.assertTrue(self.m.threads_stop.is_set())

    def test_shutdown_without_start_does_not_stop(self):
        self.m.open_devices()
        self.m.shutdown()
        self.assertEqual(self.dev.ioctls(t.CCCI_IOC_DO_STOP_MD), [])
        self.assertEqual(self.dev.closed_paths()[-1], t.DEV_MONITOR)

    def test_boot_timeout_stops_modem(self):
        m = make_modemd(self.dev, boot_timeout=1)
        m.open_devices()
        self.dev.boot_ok = False
        with self.assertRaises(t.Failure) as cm:
            m.boot()
        self.assertEqual(cm.exception.code, t.EX_UNAVAILABLE)
        self.assertIn("timeout", str(cm.exception))
        self.assertGreater(self.dev.index("ioctl", t.DEV_MONITOR, t.CCCI_IOC_DO_STOP_MD),
                           self.dev.index("ioctl", t.DEV_MONITOR, t.CCCI_IOC_DO_START_MD))
        self.assertNotIn("READY=1\nSTATUS=modem ready", m.notes)
        self.assertFalse(m.md_running)

    def test_exception_message_during_boot(self):
        self.m.open_devices()
        self.dev.boot_ok = False
        self.m.events.put(("mon", t.MonitorMsg(t.MON_EXCEPTION, 0, 0xFFFFFFFF, 0xF0000000)))
        start = time.monotonic()
        with self.assertRaises(t.Failure) as cm:
            self.m.boot()
        self.assertIn("exception", str(cm.exception))
        self.assertLess(time.monotonic() - start, 5)

    def test_battery_message_during_boot_is_served(self):
        self.m.open_devices()
        self.m.events.put(("mon", t.MonitorMsg(t.MON_SEND_BATTERY_INFO, 0, 0xFFFFFFFF, 0)))
        self.dev.ready_after = 3
        self.m.boot()
        self.assertEqual(len(self.dev.ioctls(t.CCCI_IOC_SEND_BATTERY_INFO)), 1)

    def test_stop_requested_during_wait(self):
        self.m.open_devices()
        self.dev.boot_ok = False
        threading.Timer(0.3, lambda: setattr(self.m, "stop_requested", True)).start()
        with self.assertRaises(t.StopRequested):
            self.m.boot()
        self.m.shutdown()
        self.assertEqual(len(self.dev.ioctls(t.CCCI_IOC_DO_STOP_MD)), 1)

    def test_reset_request_restarts(self):
        self.m.open_devices()
        self.m.boot()
        self.m.dispatch(("mon", t.MonitorMsg(t.MON_RESET_REQUEST, 0, 0xFFFFFFFF, 0)))
        starts = [i for i, c in enumerate(self.dev.calls)
                  if c[0] == "ioctl" and c[2] == t.CCCI_IOC_DO_START_MD]
        stops = [i for i, c in enumerate(self.dev.calls)
                 if c[0] == "ioctl" and c[2] == t.CCCI_IOC_DO_STOP_MD]
        self.assertEqual(len(starts), 2)
        self.assertTrue(starts[0] < stops[0] < starts[1])
        self.assertEqual(self.m.notes.count("READY=1\nSTATUS=modem ready"), 1)
        self.assertTrue(self.m.md_ready)

    def test_flight_stop_and_start(self):
        self.m.open_devices()
        self.m.boot()
        self.m.dispatch(("mon", t.MonitorMsg(t.MON_FLIGHT_STOP_REQUEST, 0, 0xFFFFFFFF, 0)))
        self.assertEqual(self.dev.ioctls(t.CCCI_IOC_DO_STOP_MD)[-1][3], u32(1))
        self.assertFalse(self.m.md_running)
        self.m.dispatch(("mon", t.MonitorMsg(t.MON_FLIGHT_START_REQUEST, 0, 0xFFFFFFFF, 0)))
        self.assertEqual(len(self.dev.ioctls(t.CCCI_IOC_DO_START_MD)), 2)

    def test_running_modem_is_stopped_before_start(self):
        self.dev.sys_boot, self.dev.state = "md1:4", 2
        self.m.open_devices()
        self.m.boot()
        self.assertLess(self.dev.index("ioctl", t.DEV_MONITOR, t.CCCI_IOC_DO_STOP_MD),
                        self.dev.index("ioctl", t.DEV_MONITOR, t.CCCI_IOC_DO_START_MD))

    def test_disabled_modem(self):
        self.dev.sys_boot = ""
        self.m.open_devices()
        with self.assertRaises(t.Failure) as cm:
            self.m.boot()
        self.assertEqual(cm.exception.code, t.EX_UNAVAILABLE)
        self.assertEqual(self.dev.ioctls(t.CCCI_IOC_DO_START_MD), [])

    def test_monitor_busy(self):
        self.dev.busy.add(t.DEV_MONITOR)
        with self.assertRaises(t.Failure) as cm:
            self.m.open_devices()
        self.assertEqual(cm.exception.code, t.EX_UNAVAILABLE)
        self.assertIn("another owner", str(cm.exception))

    def test_fresh_boot_invalid_state_is_startable(self):
        # Visto no aparelho: logo depois do boot o sysfs mostra md1:0 (INVALID) e a
        # partida vale; antes o servico esperava GATED 20 s e falhava.
        self.dev.sys_boot = "md1:0"
        self.m.open_devices()
        start = time.monotonic()
        self.m.ensure_gated()
        self.assertLess(time.monotonic() - start, 1.0)

    def test_stop_waits_for_gated_event_without_sysfs(self):
        self.m.open_devices()
        self.m.boot()
        self.dev.read_text = lambda path: None             # sem /sys/kernel/ccci/boot

        def stop_ioctl(orig=self.dev.ioctl):
            def f(fd, req, arg=0):
                r = orig(fd, req, arg)
                if req == t.CCCI_IOC_DO_STOP_MD:
                    self.dev.state = 2                          # ainda nao parou
                    threading.Timer(0.3, self.m._record_sta, args=(
                        t.StaEvent(0, 0, 0, t.EV_STOP, "----"),)).start()
                return r
            return f
        self.dev.ioctl = stop_ioctl()
        start = time.monotonic()
        self.assertTrue(self.m.stop_md())
        self.assertGreater(time.monotonic() - start, 0.2)

    def test_signal_handler_only_sets_flag(self):
        self.m._on_signal(15, None)
        self.assertTrue(self.m.stop_requested)
        self.log.assert_not_called()

    def test_stale_ready_event_is_not_ready(self):
        self.m.open_devices()
        self.m._record_sta(t.StaEvent(0, 0, 0, t.EV_READY, "stale"))
        self.dev.boot_ok = False
        m = self.m
        m.cfg = t.Config(boot_timeout=1)
        with self.assertRaises(t.Failure):
            m.boot()


class NvSeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="modemd-seed-")
        self.src = os.path.join(self.tmp, "src")
        for rel, data, mode in (("nvdata/md/NVRAM/NVD_IMEI/LD0B_001", b"imei", 0o640),
                                ("nvdata/md/NVRAM/CALIBRAT/ML1A_000", b"cal", 0o660),
                                ("nvdata/APCFG/APRDEB/WIFI", b"wifi", 0o660),
                                ("protect1/md/MT00A001", b"p1", 0o600),
                                ("protect2/md/MT00B001", b"p2", 0o600),
                                ("nvcfg/mdota/x", b"ota", 0o644),
                                ("nvcfg/sensor/acc", b"s", 0o644)):
            p = os.path.join(self.src, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            os.chmod(p, mode)
        os.symlink("/etc/passwd", os.path.join(self.src, "nvdata/md/LINK"))
        self.cfg = t.Config(nv_root=os.path.join(self.tmp, "state/nv"))
        patcher = mock.patch.object(t, "log")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def seed(self, nvcfg=True):
        s = self.src
        return t.nv_seed(self.cfg, f"{s}/nvdata", f"{s}/protect1", f"{s}/protect2",
                         f"{s}/nvcfg" if nvcfg else None)

    def test_seed_layout_and_modes(self):
        self.assertEqual(self.seed(), t.EX_OK)
        nv = self.cfg.nv_root
        self.assertEqual(sorted(os.listdir(nv)),
                         ["mdlpm", "nvcfg", "nvdata", "protect1", "protect2", "vendor_de_md"])
        with open(f"{nv}/nvdata/md/NVRAM/NVD_IMEI/LD0B_001", "rb") as f:
            self.assertEqual(f.read(), b"imei")
        self.assertEqual(stat.S_IMODE(os.stat(f"{nv}/nvdata/md/NVRAM/NVD_IMEI/LD0B_001").st_mode),
                         0o640)
        self.assertEqual(stat.S_IMODE(os.stat(f"{nv}/protect1/md/MT00A001").st_mode), 0o600)
        self.assertTrue(os.path.isfile(f"{nv}/protect2/md/MT00B001"))
        self.assertTrue(os.path.isfile(f"{nv}/nvcfg/mdota/x"))
        self.assertFalse(os.path.exists(f"{nv}/nvcfg/sensor"))     # so o que e do modem
        self.assertFalse(os.path.exists(f"{nv}/nvdata/APCFG"))
        self.assertTrue(os.path.isdir(f"{nv}/nvdata/md_cmn"))
        self.assertFalse(os.path.lexists(f"{nv}/nvdata/md/LINK"))
        self.assertEqual(stat.S_IMODE(os.stat(nv).st_mode), 0o700)
        t.check_nv_storage(self.cfg)

    def test_refuses_non_empty_destination(self):
        self.seed()
        with self.assertRaises(t.Failure) as cm:
            self.seed()
        self.assertEqual(cm.exception.code, t.EX_CANTCREAT)

    def test_empty_destination_is_fine(self):
        os.makedirs(self.cfg.nv_root)
        self.assertEqual(self.seed(nvcfg=False), t.EX_OK)
        self.assertTrue(os.path.isdir(f"{self.cfg.nv_root}/nvcfg"))

    def test_missing_source(self):
        shutil.rmtree(os.path.join(self.src, "protect2/md"))
        with self.assertRaises(t.Failure) as cm:
            self.seed()
        self.assertEqual(cm.exception.code, t.EX_NOINPUT)
        self.assertFalse(os.path.exists(self.cfg.nv_root))

    def test_check_storage_requires_seed(self):
        with self.assertRaises(t.Failure) as cm:
            t.check_nv_storage(self.cfg)
        self.assertEqual(cm.exception.code, t.EX_NOINPUT)


class StateCommandTest(unittest.TestCase):
    def test_prints_both(self):
        dev = FakeDev()
        dev.sys_boot, dev.state = "md1:4", 2
        dev.busy.add(t.DEV_IOCTL[0])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = t.cmd_state(t.Config(), None, dev=dev)
        self.assertEqual(rc, t.EX_OK)
        text = out.getvalue()
        self.assertIn("md1:4 (READY)", text)
        self.assertIn("via /dev/ccci_ioctl1: 2 (READY)", text)
        self.assertEqual(dev.ioctls(t.CCCI_IOC_DO_START_MD), [])


class UnitFileTest(unittest.TestCase):
    def test_settings(self):
        with open(os.path.join(HERE, "thunder-modemd.service")) as f:
            lines = {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
        for want in ("Type=notify", "ExecStart=/usr/lib/thunder/modem/thunder-modemd run",
                     "TimeoutStartSec=90", "TimeoutStopSec=40", "Restart=on-failure",
                     "RestartSec=10", "StartLimitBurst=3", "StartLimitIntervalSec=300",
                     "ConditionPathExists=/dev/ccci_monitor", "KillMode=mixed",
                     "WantedBy=multi-user.target"):
            self.assertIn(want, lines)
        self.assertTrue(any(ln.startswith("ReadWritePaths=") and "/var/lib/thunder-modem" in ln
                            for ln in lines))
        self.assertTrue(any(ln.startswith("After=") and "thunder-usb-network.service" in ln
                            and "sshd.service" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
