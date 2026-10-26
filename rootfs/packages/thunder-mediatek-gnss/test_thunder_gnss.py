"""Testes do thunder-gnss que rodam no PC, sem o aparelho nem o mnld.

    python3 -m unittest discover -s rootfs/packages/thunder-mediatek-gnss -v
"""
import contextlib
import io
import os
import socket
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thunder_gnss as t  # noqa: E402


def mnl2hal_nmea(sentence: bytes) -> bytes:
    """mnl2hal_nmea do mnld: versao, comando, timestamp, string, tamanho."""
    s = sentence + b"\0"
    return struct.pack("<iiqbi", 1, t.MNL2HAL_NMEA, 123, 1, len(s)) + s + struct.pack("<i", len(sentence))


class PropAreaTest(unittest.TestCase):
    PROPS = {
        "persist.vendor.connsys.chipid": "0x6833",
        "vendor.connsys.driver.ready": "yes",
        "vendor.connsys.adie.chipid": "0x6631",
        "ro.build.type": "user",
        "ro.vendor.gps.chrdev": "gps_drv",
        "a": "1",
        "vendor.connsys": "pai e folha ao mesmo tempo",
    }

    def test_round_trip(self):
        blob = t.build_prop_area(self.PROPS)
        self.assertEqual(len(blob), t.PROP_AREA_SIZE)
        magic, version = struct.unpack_from("<II", blob, 8)
        self.assertEqual((magic, version), (t.PROP_AREA_MAGIC, t.PROP_AREA_VERSION))
        for k, v in self.PROPS.items():
            self.assertEqual(t.read_prop_area(blob, k), v, k)

    def test_absent(self):
        blob = t.build_prop_area(self.PROPS)
        for k in ("ro.nada", "vendor", "vendor.connsys.chip", "persist.vendor.connsys.chipid.x"):
            self.assertIsNone(t.read_prop_area(blob, k), k)

    def test_name_stored_after_value(self):
        # A Bionic devolve o nome lido do prop_info (__system_property_read_callback).
        blob = t.build_prop_area({"ro.x": "1"})
        self.assertIn(b"ro.x\0", blob)

    def test_value_too_long(self):
        with self.assertRaises(ValueError):
            t.build_prop_area({"ro.x": "v" * t.PROP_VALUE_MAX})

    def test_file_and_args(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "gnss.props")
            with open(src, "w") as f:
                f.write("# comentario\nro.a=1\n\nro.b=dois\n")
            out = os.path.join(d, "props")
            self.assertEqual(t.cmd_props(out, [f"@{src}", "ro.b=3"]), 0)
            with open(out, "rb") as f:
                blob = f.read()
            self.assertEqual(t.read_prop_area(blob, "ro.a"), "1")
            self.assertEqual(t.read_prop_area(blob, "ro.b"), "3")
            self.assertEqual(os.stat(out).st_mode & 0o777, 0o444)


class ProtocolTest(unittest.TestCase):
    def test_hal2mnl_start(self):
        self.assertEqual(t.hal2mnl(t.HAL2MNL_GPS_START), struct.pack("<ii", 1, 102))

    def test_inject_time(self):
        self.assertEqual(t.inject_time(5, 7), struct.pack("<iiqqi", 1, 105, 5, 7, t.TIME_UNCERTAINTY_MS))

    def test_parse_nmea(self):
        cmd, s = t.parse_mnl2hal(mnl2hal_nmea(b"$GPGGA,1*00\r\n"))
        self.assertEqual((cmd, s), (t.MNL2HAL_NMEA, b"$GPGGA,1*00\r\n"))

    def test_parse_other(self):
        self.assertEqual(t.parse_mnl2hal(struct.pack("<ii", 1, t.MNL2HAL_MNLD_REBOOT)),
                         (t.MNL2HAL_MNLD_REBOOT, None))
        with self.assertRaises(ValueError):
            t.parse_mnl2hal(b"\1\0\0")

    def test_abstract_name_padded(self):
        a = t.abstract("mtk_hal2mnl")
        self.assertEqual(len(a), 108)
        self.assertTrue(a.startswith(b"\0mtk_hal2mnl\0"))

    def test_logdw(self):
        d = bytes([0]) + struct.pack("<HII", 7, 0, 0) + b"\x06MNLD\0erro\n\0"
        self.assertEqual(t.parse_logdw(d), (6, "MNLD", "erro"))
        self.assertIsNone(t.parse_logdw(bytes([2]) + b"\0" * 20))


    def test_dedup(self):
        d = t.Dedup(10)
        self.assertTrue(d.fresh("a", 0))
        self.assertFalse(d.fresh("a", 5))
        self.assertTrue(d.fresh("b", 5))
        self.assertTrue(d.fresh("a", 10))
        self.assertTrue(d.fresh("retry:1", 0))
        self.assertFalse(d.fresh("retry:2\nresto", 1))


class HardeningTest(unittest.TestCase):
    def test_journal_escapes_newlines(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            t.journal(3, "x\n<0>forjado\r")
        self.assertEqual(out.getvalue(), "<3>x\\n<0>forjado\\r\n")

    @unittest.skipIf(os.geteuid() == 0, "o teste precisa de um remetente que nao seja root")
    def test_recv_from_root_drops_other_uids(self):
        name = b"\0thunder-gnss-test-%d" % os.getpid()
        rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        tx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            rx.bind(name)
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            tx.sendto(b"forjado", name)
            self.assertIsNone(t.recv_from_root(rx, 64))
        finally:
            rx.close()
            tx.close()


if __name__ == "__main__":
    unittest.main()
