#!/usr/bin/env python3
"""GNSS MediaTek sem Android: o papel do HAL de GNSS, do init e do logd para o mnld.

O mnld (blob da ROM, Bionic) roda num chroot Android minimo (o rootfs do servico
thunder-gnss-mnld). Ele fala com o "HAL" por dois sockets AF_UNIX abstratos de
datagrama: recebe comandos em mtk_hal2mnl e manda NMEA e eventos para mtk_mnl2hal.
Este programa:

  props  <arquivo> [nome=valor | @arquivo]...
         grava a area de propriedades da Bionic (formato de arquivo unico) que o chroot
         monta em /dev/__properties__; @arquivo le linhas nome=valor (# comenta).
  bridge <dir>
         faz o papel do HAL e do logd: liga o GNSS e publica o NMEA em <dir>/nmea (link
         para um pty: gpsd, cat) e em <dir>/nmea.sock (socket de fluxo: o nmea-socket do
         GeoClue). Recebe o log do mnld em <dir>/socket/logdw e repassa os erros ao journal.

Protocolo (hal_mnl_interface_common.h e data_coder.c do HAL da MediaTek): inteiros
little-endian de 32 bits, long de 64, string = byte 1 + int tamanho + bytes com o NUL.
Toda mensagem comeca por versao (1) e comando.
"""
import grp
import os
import pty
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty

HAL_MNL_VERSION = 1
MTK_HAL2MNL = "mtk_hal2mnl"
MTK_MNL2HAL = "mtk_mnl2hal"

HAL2MNL_HAL_REBOOT = 0
HAL2MNL_GPS_INIT = 101
HAL2MNL_GPS_START = 102
HAL2MNL_GPS_STOP = 103
HAL2MNL_GPS_CLEANUP = 104
HAL2MNL_GPS_INJECT_TIME = 105

MNL2HAL_MNLD_REBOOT = 1
MNL2HAL_NMEA = 104
MNL2HAL_REQUEST_UTC_TIME = 301
# Incerteza da hora injetada, em ms: o relogio do sistema vem do NTP.
TIME_UNCERTAINTY_MS = 1000
# Log do mnld no journal: so E e acima, e cada texto no maximo uma vez nesta janela (s).
LOG_REPEAT_S = 600
# Leitores do nmea.sock: o grupo do GeoClue, e quantos ao mesmo tempo.
NMEA_GROUP = "geoclue"
NMEA_MAX_CLIENTS = 8
HAL_MNL_BUFF_SIZE = 16 * 1024

# Bionic: bionic/libc/system_properties/include/system_properties/prop_area.h e prop_info.h.
PROP_AREA_MAGIC = 0x504F5250
PROP_AREA_VERSION = 0xFC6ED0AB
PROP_AREA_HEADER = 128          # bytes_used, serial, magic, version, reserved[28]
PROP_AREA_SIZE = 128 * 1024     # PA_SIZE
PROP_VALUE_MAX = 92
PROP_BT = struct.Struct("<5I")  # namelen, prop, left, right, children; depois o nome

LOG_PRIO = "??VDIWEFS"


def abstract(name: str) -> bytes:
    # O socket_bind_udp/safe_sendto da MediaTek passa sizeof(sockaddr_un): o nome abstrato
    # inclui os NUL de preenchimento ate o fim do sun_path (108 bytes).
    return ("\0" + name).encode().ljust(108, b"\0")


# ---------------------------------------------------------------- propriedades


class _Node:
    def __init__(self, name: bytes):
        self.name = name
        self.value: bytes | None = None
        self.full = b""
        self.left = self.right = self.children = None
        self.off = 0


def _cmp(a: bytes, b: bytes) -> int:
    # cmp_prop_name: primeiro o tamanho, depois os bytes.
    if len(a) != len(b):
        return -1 if len(a) < len(b) else 1
    return (a > b) - (a < b)


def build_prop_area(props: dict[str, str]) -> bytes:
    """Area de propriedades so leitura, montada como o init da Bionic montaria."""
    root = _Node(b"")
    order: list[_Node] = []           # ordem de alocacao (offsets crescentes)

    def find(parent: _Node, seg: bytes) -> _Node:
        if parent.children is None:
            parent.children = _Node(seg)
            order.append(parent.children)
            return parent.children
        cur = parent.children
        while True:
            c = _cmp(seg, cur.name)
            if c == 0:
                return cur
            side = "left" if c < 0 else "right"
            nxt = getattr(cur, side)
            if nxt is None:
                nxt = _Node(seg)
                setattr(cur, side, nxt)
                order.append(nxt)
                return nxt
            cur = nxt

    infos: list[_Node] = []
    for name, value in props.items():
        v = value.encode()
        if len(v) >= PROP_VALUE_MAX:
            raise ValueError(f"valor longo demais para {name}: {len(v)} bytes")
        cur = root
        for seg in name.encode().split(b"."):
            if not seg:
                raise ValueError(f"nome invalido: {name!r}")
            cur = find(cur, seg)
        cur.value, cur.full = v, name.encode()
        infos.append(cur)

    def align(n: int) -> int:
        return (n + 3) & ~3

    # Offsets relativos a data_: a raiz fica no 0; cada no e cada prop_info em seguida.
    used = align(PROP_BT.size + 1)
    for n in order:
        n.off = used
        used += align(PROP_BT.size + len(n.name) + 1)
    info_off = {}
    for n in infos:
        info_off[id(n)] = used
        used += align(4 + PROP_VALUE_MAX + len(n.full) + 1)
    if PROP_AREA_HEADER + used > PROP_AREA_SIZE:
        raise ValueError("propriedades demais para a area")

    data = bytearray(PROP_AREA_SIZE - PROP_AREA_HEADER)

    def put_bt(off: int, n: _Node) -> None:
        off_of = lambda x: x.off if x is not None else 0
        PROP_BT.pack_into(data, off, len(n.name), info_off.get(id(n), 0),
                          off_of(n.left), off_of(n.right), off_of(n.children))
        data[off + PROP_BT.size:off + PROP_BT.size + len(n.name)] = n.name

    put_bt(0, root)
    for n in order:
        put_bt(n.off, n)
    for n in infos:
        off = info_off[id(n)]
        struct.pack_into("<I", data, off, len(n.value) << 24)   # serial: tamanho, limpo
        data[off + 4:off + 4 + len(n.value)] = n.value
        data[off + 4 + PROP_VALUE_MAX:off + 4 + PROP_VALUE_MAX + len(n.full)] = n.full
    header = struct.pack("<4I", used, 0, PROP_AREA_MAGIC, PROP_AREA_VERSION).ljust(PROP_AREA_HEADER, b"\0")
    return header + bytes(data)


def read_prop_area(blob: bytes, name: str) -> str | None:
    """Leitura como a da Bionic (find_property), para o teste."""
    data = blob[PROP_AREA_HEADER:]

    def bt(off):
        namelen, prop, left, right, children = PROP_BT.unpack_from(data, off)
        return data[off + PROP_BT.size:off + PROP_BT.size + namelen], prop, left, right, children

    cur = 0
    for seg in name.encode().split(b"."):
        child = bt(cur)[4]
        if not child:
            return None
        cur = child
        while True:
            nm, _, left, right, _ = bt(cur)
            c = _cmp(seg, nm)
            if c == 0:
                break
            cur = left if c < 0 else right
            if not cur:
                return None
    prop = bt(cur)[1]
    if not prop:
        return None
    serial = struct.unpack_from("<I", data, prop)[0]
    return data[prop + 4:prop + 4 + (serial >> 24)].decode()


def cmd_props(path: str, pairs: list[str]) -> int:
    props = {}
    lines = []
    for p in pairs:
        if p.startswith("@"):
            with open(p[1:]) as f:
                lines += [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
        else:
            lines.append(p)
    for p in lines:
        k, sep, v = p.partition("=")
        if not sep:
            print(f"esperava nome=valor: {p!r}", file=sys.stderr)
            return 2
        props[k] = v
    blob = build_prop_area(props)
    tmp = path + ".tmp"
    # A Bionic recusa a area se ela nao for do root ou se o grupo/outros puderem escrever.
    with open(tmp, "wb") as f:
        f.write(blob)
    os.chmod(tmp, 0o444)
    os.replace(tmp, path)
    return 0


# ---------------------------------------------------------------- ponte


def put_int(v: int) -> bytes:
    return struct.pack("<i", v)


def hal2mnl(cmd: int, payload: bytes = b"") -> bytes:
    return put_int(HAL_MNL_VERSION) + put_int(cmd) + payload


def inject_time(now_ms: int, boot_ms: int) -> bytes:
    # hal2mnl_gps_inject_time: hora UTC, elapsedRealtime da leitura e incerteza.
    return hal2mnl(HAL2MNL_GPS_INJECT_TIME, struct.pack("<qqi", now_ms, boot_ms, TIME_UNCERTAINTY_MS))


def parse_mnl2hal(buf: bytes) -> tuple[int, bytes | None]:
    """(comando, sentenca NMEA ou None). Lanca ValueError se a mensagem for curta."""
    if len(buf) < 8:
        raise ValueError("mensagem curta")
    _ver, cmd = struct.unpack_from("<ii", buf, 0)
    if cmd != MNL2HAL_NMEA:
        return cmd, None
    off = 8 + 8                                  # timestamp (long)
    if len(buf) < off + 5 or buf[off] == 0:
        return cmd, None
    n = struct.unpack_from("<i", buf, off + 1)[0]
    s = buf[off + 5:off + 5 + max(0, n)]
    return cmd, s.split(b"\0", 1)[0]


def journal(prio: int, msg: str) -> None:
    # Prefixo <N> do sd-daemon: o journald le a prioridade da linha. Quebra de linha no texto
    # (vinda do log do mnld) viraria outra linha com prioridade propria: escapada.
    msg = msg.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    print(f"<{prio}>{msg}", flush=True)


def recv_from_root(sock: socket.socket, n: int) -> bytes | None:
    """Datagrama so se veio do uid 0 (o mnld). Com SO_PASSCRED o kernel anexa as credenciais
    do remetente; sem elas, ou de outro uid, descarta."""
    data, anc, flags, _ = sock.recvmsg(n, socket.CMSG_SPACE(12))
    if flags & socket.MSG_CTRUNC:
        return None
    for level, kind, val in anc:
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(val) >= 12:
            return data if struct.unpack_from("=iII", val)[1] == 0 else None   # struct ucred
    return None


def parse_logdw(d: bytes) -> tuple[int, str, str] | None:
    # android_log_header_t: id u8, tid u16, sec u32, nsec u32; depois prio, tag\0, msg\0.
    if len(d) < 13 or d[0] == 2:                # 2 = events, binario
        return None
    body = d[11:]
    tag, _, msg = body[1:].partition(b"\0")
    return body[0], tag.decode(errors="replace"), msg.rstrip(b"\0\n").decode(errors="replace")


_NO_DIGITS = str.maketrans("", "", "0123456789")


class Dedup:
    """O mnld repete o mesmo erro a cada segundo (AGPS ausente, NMEA proprietario): cada
    texto passa no maximo uma vez por janela."""

    def __init__(self, window: float):
        self.window, self.seen = window, {}

    def fresh(self, msg: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        # Chave sem os numeros (contadores, fd, horas) e so a primeira linha.
        msg = msg.split("\n", 1)[0].translate(_NO_DIGITS)
        if now - self.seen.get(msg, -self.window) < self.window:
            return False
        if len(self.seen) > 1000:
            self.seen.clear()
        self.seen[msg] = now
        return True


class Bridge:
    def __init__(self, rundir: str):
        os.makedirs(os.path.join(rundir, "socket"), exist_ok=True)
        logdw = os.path.join(rundir, "socket", "logdw")
        self.link = link = os.path.join(rundir, "nmea")
        self.rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.rx.bind(abstract(MTK_MNL2HAL))
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self.tx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            os.unlink(logdw)
        except FileNotFoundError:
            pass
        self.log = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.log.bind(logdw)
        os.chmod(logdw, 0o600)                   # so o mnld (root) escreve
        # O gpsd le do lado escravo; sem eco nem traducao de fim de linha.
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        self.slave = slave                       # aberto: o pty nao some sem leitor
        os.set_blocking(self.master, False)
        tmp = link + ".tmp"
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        os.symlink(os.ttyname(slave), tmp)
        os.replace(tmp, link)
        sock = os.path.join(rundir, "nmea.sock")
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(sock)
        try:
            # So o GeoClue le a posicao crua; os apps passam pela autorizacao dele.
            os.chown(sock, 0, grp.getgrnam(NMEA_GROUP).gr_gid)
            os.chmod(sock, 0o660)
        except KeyError:
            os.chmod(sock, 0o600)                # sem GeoClue: so root (gpsd usa o pty)
        self.srv.listen(4)
        self.clients: list[socket.socket] = []
        self.quiet = Dedup(LOG_REPEAT_S)
        self.started = False

    def send(self, cmd: int, msg: bytes | None = None) -> None:
        try:
            self.tx.sendto(msg or hal2mnl(cmd), abstract(MTK_HAL2MNL))
        except OSError as e:                     # mnld ainda nao subiu: tenta de novo depois
            journal(6, f"hal2mnl {cmd}: {e.strerror}")
            return
        if cmd == HAL2MNL_GPS_START:
            self.started = True

    def start(self) -> None:
        self.send(HAL2MNL_GPS_INIT)
        self.send(HAL2MNL_GPS_START)

    def stop(self) -> None:
        if self.started:
            self.send(HAL2MNL_GPS_STOP)
            self.send(HAL2MNL_GPS_CLEANUP)

    def on_mnl(self, buf: bytes) -> None:
        try:
            cmd, nmea = parse_mnl2hal(buf)
        except ValueError:
            return
        if cmd == MNL2HAL_MNLD_REBOOT:
            journal(5, "mnld reiniciou: religando o GNSS")
            self.start()
        elif cmd == MNL2HAL_REQUEST_UTC_TIME:
            self.send(HAL2MNL_GPS_INJECT_TIME, inject_time(
                time.time_ns() // 1_000_000, time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000))
        elif nmea:
            line = nmea if nmea.endswith(b"\n") else nmea + b"\r\n"
            try:
                os.write(self.master, line)
            except BlockingIOError:              # ninguem lendo: descarta, o NMEA e ao vivo
                pass
            for c in self.clients[:]:
                try:
                    c.send(line, socket.MSG_DONTWAIT)
                except BlockingIOError:          # cliente lento perde a sentenca
                    pass
                except OSError:
                    self.clients.remove(c)
                    c.close()

    def run(self) -> int:
        stop = []
        signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
        signal.signal(signal.SIGINT, lambda *_: stop.append(1))
        self.start()
        journal(6, f"NMEA em {self.link} -> {os.ttyname(self.slave)}")
        last_retry = 0.0
        while not stop:
            try:
                r, _, _ = select.select([self.rx, self.log, self.master, self.srv, *self.clients], [], [], 5)
            except InterruptedError:
                continue
            if self.rx in r:
                buf = recv_from_root(self.rx, HAL_MNL_BUFF_SIZE)
                if buf is not None:
                    self.on_mnl(buf)
            if self.log in r:
                rec = parse_logdw(self.log.recv(65536))
                if rec and rec[0] >= 6 and self.quiet.fresh(f"{rec[1]}: {rec[2]}"):
                    journal(3, f"{rec[1]}: {rec[2]}")
            if self.srv in r:
                try:
                    c = self.srv.accept()[0]
                except OSError:                  # EMFILE e afins: recusa, nao derruba o servico
                    c = None
                if c is not None and len(self.clients) >= NMEA_MAX_CLIENTS:
                    c.close()
                elif c is not None:
                    self.clients.append(c)
            for c in [c for c in self.clients if c in r]:
                try:
                    alive = bool(c.recv(4096))   # vazio = cliente fechou
                except OSError:
                    alive = False
                if not alive:
                    self.clients.remove(c)
                    c.close()
            if self.master in r:
                try:
                    os.read(self.master, 4096)   # o gpsd as vezes escreve (sondagem)
                except OSError:
                    pass
            now = time.monotonic()
            if not self.started and now - last_retry > 5:
                last_retry = now
                self.start()
        self.stop()
        return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "props":
        return cmd_props(argv[1], argv[2:])
    if len(argv) == 2 and argv[0] == "bridge":
        return Bridge(argv[1]).run()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
