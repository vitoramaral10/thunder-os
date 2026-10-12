"""Testes do thunder-wmt que rodam no PC, sem o aparelho nem o driver.

    python3 -m unittest discover -s rootfs/packages/thunder-mediatek-wmt -v
"""
import errno
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thunder_wmt as t  # noqa: E402


def header(addr: bytes, ptype: int, plat: bytes = b"ALPS") -> bytes:
    """Cabecalho de 32 bytes como o dos .bin do MT6833 (wifi-bringup.md, tabela)."""
    return b"20231215181632\0\0" + plat + b"\x8a\x00\x8a\x00" + addr + struct.pack(">I", ptype)


MCU = header(bytes.fromhex("110000f0"), 4)
WIFI = header(bytes.fromhex("11002af0"), 3)
BT = header(bytes.fromhex("110017f0"), 0)


class ConfigTest(unittest.TestCase):
    def test_parse(self):
        m = t.parse_config(
            "# comentario\n"
            "WMT_CORE_MODULES=wmt_drv connfem\n"
            "WMT_ROM_PATCHES='a_hdr.bin' \"b_hdr.bin\"  # resto e comentario\n"
            "\n"
            "WMT_WIFI_NVRAM=\n")
        self.assertEqual(m["WMT_CORE_MODULES"], ["wmt_drv", "connfem"])
        self.assertEqual(m["WMT_ROM_PATCHES"], ["a_hdr.bin", "b_hdr.bin"])
        self.assertEqual(m["WMT_WIFI_NVRAM"], [])

    def test_parse_rejects(self):
        for bad in ("WMT_X", "wmt_x=1", "WMT_X =1", "WMT_X=a;b", "WMT_X='open",
                    "WMT_X=$(reboot)", "WMT_X=a\\ b"):
            with self.subTest(bad=bad), self.assertRaises(t.ConfigError):
                t.parse_config(bad)

    def test_mapping_defaults(self):
        c = t.config_from_mapping({})
        self.assertEqual(c.hif, 0x23)
        self.assertEqual(c.firmware_dir, "/usr/lib/firmware")
        self.assertIsNone(c.wifi_nvram)
        self.assertEqual(c.core_modules, ())

    def test_mapping_values(self):
        c = t.config_from_mapping(t.parse_config(
            "WMT_HIF=0x03\nWMT_FIRMWARE_DIR=/lib/firmware/\nWMT_WIFI_INTERFACE=wlan0\n"
            "WMT_WIFI_NVRAM=nvdata:/APCFG/APRDEB/WIFI\n"))
        self.assertEqual(c.hif, 3)
        self.assertEqual(c.firmware_dir, "/lib/firmware")
        self.assertEqual(c.wifi_interface, "wlan0")
        self.assertEqual(c.wifi_nvram, ("nvdata", "/APCFG/APRDEB/WIFI"))

    def test_mapping_rejects(self):
        for m in ({"WMT_HIF": ["0x100"]}, {"WMT_HIF": ["x"]}, {"WMT_HIF": ["1", "2"]},
                  {"WMT_FIRMWARE_DIR": ["lib"]}, {"WMT_WIFI_INTERFACE": ["a" * 16]},
                  {"WMT_WIFI_NVRAM": ["nvdata"]}, {"WMT_WIFI_NVRAM": ["nvdata:APCFG"]},
                  {"WMT_CORE_MODULES": ["../x"]}):
            with self.subTest(m=m), self.assertRaises(t.ConfigError):
                t.config_from_mapping(m)


class RomPatchTest(unittest.TestCase):
    def test_offsets_match_driver(self):
        # EmiOffset do dmesg do aparelho: mcu 0x0, wifi 0x2a0000, bt 0x170000.
        cases = ((MCU, 4, 0x0), (WIFI, 3, 0x2A0000), (BT, 0, 0x170000))
        for hdr, ptype, off in cases:
            p = t.parse_rom_patch_header("x", hdr)
            self.assertEqual((p.type, p.emi_offset), (ptype, off))
            self.assertEqual(p.addr[0], 0, "o byte de flag 0x11 nunca vai para o driver")

    def test_bad_headers(self):
        for hdr in (MCU[:31], header(bytes(4), 4, b"XXXX"), header(bytes(4), 5)):
            with self.assertRaises(ValueError):
                t.parse_rom_patch_header("x", hdr)

    def test_pack_layout(self):
        p = t.parse_rom_patch_header("soc2_2_ram_wifi_1_1_hdr.bin", WIFI)
        raw = t.pack_rom_patch_info(p)
        self.assertEqual(len(raw), 264)                  # struct wmt_rom_patch_info
        self.assertEqual(struct.unpack_from("<I", raw)[0], 3)
        self.assertEqual(bytes(raw[4:8]), bytes.fromhex("00002af0"))
        self.assertEqual(bytes(raw[8:36]), b"soc2_2_ram_wifi_1_1_hdr.bin\0")
        with self.assertRaises(ValueError):
            t.pack_rom_patch_info(t.RomPatch("a" * 256, 3, bytes(4)))

    def test_set(self):
        ps = [t.parse_rom_patch_header(n, h) for n, h in (("m", MCU), ("w", WIFI), ("b", BT))]
        t.check_rom_patch_set(ps)
        with self.assertRaises(ValueError):
            t.check_rom_patch_set(ps + [t.parse_rom_patch_header("w2", WIFI)])
        with self.assertRaises(ValueError):
            t.check_rom_patch_set(ps[1:])

    def test_ioctl_numbers_unchanged(self):
        # Os mesmos numeros do loader e do daemon antigos, validados no aparelho.
        def old_ior(nr): return (2 << 30) | (4 << 16) | (ord("w") << 8) | nr
        def old_iow(nr, size): return (1 << 30) | (size << 16) | (0xA0 << 8) | nr
        self.assertEqual(t.COMBO_IOCTL_CONNSYS_SOC_HW_INIT, old_ior(10))
        self.assertEqual(t.COMBO_IOCTL_GET_SOC_CHIP_ID, old_ior(3))
        self.assertEqual(t.COMBO_IOCTL_GET_ADIE_CHIP_ID, old_ior(9))
        self.assertEqual(t.COMBO_IOCTL_DO_MODULE_INIT, old_ior(4))
        self.assertEqual(t.COMBO_IOCTL_SET_CHIP_ID, (1 << 30) | (4 << 16) | (ord("w") << 8) | 1)
        self.assertEqual(t.WMT_IOCTL_SET_STP_MODE, old_iow(5, 4))
        self.assertEqual(t.WMT_IOCTL_SET_ROM_PATCH_INFO, old_iow(31, 8))


class NvramTest(unittest.TestCase):
    def blob(self, size=2050, mac=bytes.fromhex("021122334455")):
        b = bytearray(size)
        b[4:10] = mac
        return bytes(b)

    def test_validate(self):
        t.validate_nvram(self.blob())
        for size in (0, 9, 2049, 2051, 8194):
            with self.subTest(size=size), self.assertRaises(t.NvramError):
                t.validate_nvram(bytes(size))

    def test_write_buffer(self):
        data = self.blob()
        buf = t.nvram_write_buffer(data)
        self.assertEqual(buf[:12], b"WR-BUF:NVRAM")
        self.assertEqual(buf[12:], data)
        self.assertEqual(t.nvram_mac(data), bytes.fromhex("021122334455"))

    def test_mac_kind(self):
        self.assertEqual(t.mac_kind(bytes(6)), "zero")
        self.assertEqual(t.mac_kind(bytes.fromhex("010000000000")), "multicast")
        self.assertEqual(t.mac_kind(bytes.fromhex("020000000001")), "locally administered")
        self.assertEqual(t.mac_kind(bytes.fromhex("001122334455")), "globally unique")

    def test_outcome(self):
        factory = bytes.fromhex("001122334455")
        self.assertEqual(t.nvram_outcome(factory, factory)[0], t.LOG_INFO)
        self.assertEqual(t.nvram_outcome(factory, bytes.fromhex("000822abcdfb"))[0], t.LOG_WARNING)
        self.assertEqual(t.nvram_outcome(factory, bytes(6))[0], t.LOG_NOTICE)
        self.assertEqual(t.nvram_outcome(factory, b"")[0], t.LOG_NOTICE)
        self.assertEqual(t.nvram_outcome(factory, bytes.fromhex("021122334455"))[0], t.LOG_WARNING)
        for prio, msg in (t.nvram_outcome(factory, factory),
                          t.nvram_outcome(factory, bytes.fromhex("000822abcdfb")),
                          t.nvram_outcome(factory, bytes.fromhex("021122334455"))):
            self.assertNotIn("11:22", msg, "endereco nunca vai para o log")
            self.assertTrue(msg.startswith(t.VERDICT), "uma linha de veredito, sempre igual")

    def test_verdict_words(self):
        factory = bytes.fromhex("001122334455")
        self.assertIn("applied", t.nvram_outcome(factory, factory)[1])
        self.assertNotIn("not applied", t.nvram_outcome(factory, factory)[1])
        self.assertIn("not applied", t.nvram_outcome(factory, bytes.fromhex("000822abcdfb"))[1])

    def test_find_partition(self):
        with tempfile.TemporaryDirectory() as d:
            for name, uevent in (("sdc6", "DEVNAME=sdc6\nPARTNAME=nvcfg\n"),
                                 ("sdc7", "DEVNAME=sdc7\nPARTNAME=nvdata\n"),
                                 ("sdc", "DEVNAME=sdc\nDEVTYPE=disk\n")):
                os.makedirs(os.path.join(d, name))
                with open(os.path.join(d, name, "uevent"), "w") as f:
                    f.write(uevent)
            os.makedirs(os.path.join(d, "noevent"))
            self.assertEqual(t.find_partition("nvdata", d, "/dev"), "/dev/sdc7")
            with self.assertRaises(t.NvramError):
                t.find_partition("missing", d)
            os.makedirs(os.path.join(d, "sdd1"))
            with open(os.path.join(d, "sdd1", "uevent"), "w") as f:
                f.write("PARTNAME=nvdata\n")
            with self.assertRaises(t.NvramError):     # ambiguo
                t.find_partition("nvdata", d)

    @unittest.skipUnless(shutil.which("mke2fs") and shutil.which("debugfs"), "e2fsprogs")
    def test_read_nvram_from_ext4_image_without_mounting(self):
        data = bytes(range(256)) * 8 + b"\xff\x00"          # 2050 bytes, binario
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "root", "APCFG", "APRDEB")
            os.makedirs(root)
            with open(os.path.join(root, "WIFI"), "wb") as f:
                f.write(data)
            img = os.path.join(d, "nvdata.img")
            subprocess.run(["mke2fs", "-q", "-t", "ext4", "-d", os.path.join(d, "root"),
                            img, "4M"], check=True)
            before = os.stat(img).st_mtime_ns
            sysblk = os.path.join(d, "sys-class-block", "sdc7")
            os.makedirs(sysblk)
            with open(os.path.join(sysblk, "uevent"), "w") as f:
                f.write("DEVNAME=sdc7\nPARTNAME=nvdata\n")
            devdir = os.path.join(d, "dev")
            os.makedirs(devdir)
            os.symlink(img, os.path.join(devdir, "sdc7"))
            sysroot = os.path.join(d, "sys-class-block")
            self.assertEqual(t.read_nvram(("nvdata", "/APCFG/APRDEB/WIFI"), sysroot, devdir), data)
            with self.assertRaises(t.NvramError):
                t.read_nvram(("nvdata", "/APCFG/APRDEB/BT_Addr"), sysroot, devdir)
            self.assertEqual(os.stat(img).st_mtime_ns, before, "a imagem nao pode ser escrita")


class CliTest(unittest.TestCase):
    def test_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "none.conf")
            self.assertEqual(t.main(["connsys", "--config", missing]), t.EX_CONFIG)
            bad = os.path.join(d, "bad.conf")
            with open(bad, "w") as f:
                f.write("WMT_HIF=banana\n")
            self.assertEqual(t.main(["wifi-on", "--config", bad]), t.EX_CONFIG)
            empty = os.path.join(d, "empty.conf")
            open(empty, "w").close()
            self.assertEqual(t.main(["connsys", "--config", empty]), t.EX_CONFIG)
            self.assertEqual(t.main(["wifi-on", "--config", empty]), t.EX_CONFIG)
            self.assertEqual(t.main(["nvram-check", "--config", empty]), t.EX_OK)
            # wifi-off e o ExecStop: nao pode depender de uma configuracao valida.
            with mock.patch.object(t, "DEV_WIFI", os.path.join(d, "no-such-node")):
                self.assertEqual(t.main(["wifi-off", "--config", missing]), t.EX_OK)
                self.assertEqual(t.main(["wifi-off", "--config", bad]), t.EX_OK)
        with open(os.devnull, "w") as null:
            err, sys.stderr = sys.stderr, null
            try:
                self.assertEqual(t.main(["reboot"]), t.EX_USAGE)
            finally:
                sys.stderr = err

    def test_rom_patch_errors_before_touching_the_driver(self):
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "wmt.conf")
            with open(conf, "w") as f:
                f.write(f"WMT_CORE_MODULES=wmt_drv\nWMT_ROM_PATCHES=nope.bin\n"
                        f"WMT_FIRMWARE_DIR={d}\n")
            self.assertEqual(t.main(["connsys", "--config", conf]), t.EX_OSFILE)


class WifiPowerTest(unittest.TestCase):
    def setUp(self):
        self.node = tempfile.NamedTemporaryFile()
        self.addCleanup(self.node.close)
        patch = mock.patch.object(t, "DEV_WIFI", self.node.name)
        patch.start()
        self.addCleanup(patch.stop)

    def test_eio_is_retried_once(self):
        eio = OSError(errno.EIO, "Input/output error")
        with mock.patch.object(t.os, "write", side_effect=[eio, 1]) as w, \
                mock.patch.object(t.time, "sleep") as sleep:
            t.wifi_power(True, retry_delay=3.0)
        self.assertEqual(w.call_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_second_eio_fails(self):
        eio = OSError(errno.EIO, "Input/output error")
        with mock.patch.object(t.os, "write", side_effect=[eio, eio]) as w, \
                mock.patch.object(t.time, "sleep"), self.assertRaises(t.Failure) as cm:
            t.wifi_power(True)
        self.assertEqual((cm.exception.code, w.call_count), (t.EX_IOERR, 2))

    def test_other_errors_and_power_off_are_not_retried(self):
        for on, err in ((True, errno.EPERM), (False, errno.EIO)):
            with self.subTest(on=on), \
                    mock.patch.object(t.os, "write", side_effect=OSError(err, "x")) as w, \
                    mock.patch.object(t.time, "sleep") as sleep, self.assertRaises(t.Failure):
                t.wifi_power(on)
            self.assertEqual(w.call_count, 1)
            sleep.assert_not_called()


class WifiOnTest(unittest.TestCase):
    def cfg(self, nvram=None):
        return t.Config(core_modules=("wmt_drv",), rom_patches=(), hif=0x23, firmware_dir="/x",
                        wifi_modules=("wmt_chrdev_wifi",), wifi_interface="lo", wifi_nvram=nvram)

    def test_registered_interface_still_gets_push_and_verdict(self):
        # "lo" sempre existe em /sys/class/net: simula o netdev que o gen4m mantem entre
        # power cycles. Mesmo assim a NVRAM vai e o veredito sai.
        with tempfile.NamedTemporaryFile() as node, \
                mock.patch.object(t, "DEV_WIFI", node.name), \
                mock.patch.object(t, "READY_MARK", node.name), \
                mock.patch.object(t, "modprobe"), \
                mock.patch.object(t, "read_nvram", return_value=b"x" * 2050) as read, \
                mock.patch.object(t, "push_nvram") as push, \
                mock.patch.object(t, "nvram_mac", return_value=b"\0" * 6), \
                mock.patch.object(t, "verify_nvram") as verify, \
                mock.patch.object(t, "wifi_power") as power, \
                mock.patch.object(t, "log") as log:
            rc = t.cmd_wifi_on(self.cfg(("nvdata", "/APCFG/APRDEB/WIFI")))
        self.assertEqual(rc, t.EX_OK)
        read.assert_called_once()
        push.assert_called_once()
        verify.assert_called_once()
        power.assert_called_once_with(True)
        self.assertTrue(any("already registered" in c.args[1] for c in log.call_args_list))

    def test_ready_mark_is_written_before_ready_notification(self):
        with tempfile.TemporaryDirectory() as d:
            mark = os.path.join(d, "connsys-ready")
            seen = []
            with mock.patch.object(t, "READY_MARK", mark), \
                    mock.patch.object(t, "sd_notify",
                                      side_effect=lambda m: seen.append((m, os.path.exists(mark)))):
                t.announce_ready("launcher ready")
            self.assertEqual(len(seen), 1)
            self.assertTrue(seen[0][0].startswith("READY=1"))
            self.assertTrue(seen[0][1], "READY=1 sent before the mark existed")

    def test_ready_mark_failure_fails_connsys_without_notifying(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(t, "READY_MARK", os.path.join(d, "no-dir", "connsys-ready")), \
                mock.patch.object(t, "sd_notify") as notify, \
                self.assertRaises(t.Failure) as cm:
            t.announce_ready("launcher ready")
        self.assertEqual(cm.exception.code, t.EX_OSERR)
        notify.assert_not_called()

    def test_refuses_without_connsys_ready_mark(self):
        # /dev/stpwmt pode existir (DO_MODULE_INIT) com o launcher caido: sem o marcador
        # o wifi-on recusa com 69 e nao mexe no radio.
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(t, "READY_MARK", os.path.join(d, "connsys-ready")), \
                mock.patch.object(t, "modprobe") as modprobe, \
                mock.patch.object(t, "wifi_power") as power, \
                self.assertRaises(t.Failure) as cm:
            t.cmd_wifi_on(self.cfg())
        self.assertEqual(cm.exception.code, t.EX_UNAVAILABLE)
        modprobe.assert_not_called()
        power.assert_not_called()

    def test_requires_connsys(self):
        with mock.patch.object(t, "DEV_STPWMT", "/nonexistent/stpwmt"), \
                self.assertRaises(t.Failure) as cm:
            t.cmd_wifi_on(self.cfg())
        self.assertEqual(cm.exception.code, t.EX_UNAVAILABLE)


@unittest.skipUnless(sys.platform.startswith("linux"), "ethtool ioctl")
class EthtoolTest(unittest.TestCase):
    def test_loopback_has_zero_permanent_address(self):
        try:
            perm = t.permanent_address("lo")
        except OSError as e:     # ex.: qemu-user sem traducao do SIOCETHTOOL (check())
            self.skipTest(f"SIOCETHTOOL unavailable: {e}")
        self.assertEqual(perm, bytes(6))


if __name__ == "__main__":
    unittest.main()
