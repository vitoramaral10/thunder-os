#!/usr/bin/env python3
"""Extrai uma particao logica do super.img de uma ROM de fabrica, num passe so.

Le o super.img direto de dentro do pacote de fastboot (.tgz/.tar.gz/.tar), de uma pasta
que contenha images/super.img, ou de um super.img solto, esparso (formato sparse do
Android) ou cru. Grava so a particao pedida: nada de desesparsar os ~9 GB do super em
disco nem de precisar de simg2img/lpunpack.

Formatos (AOSP): sparse em system/core/libsparse/sparse_format.h; metadados de
particao dinamica em system/core/fs_mgr/liblp/include/liblp/metadata_format.h.

Uso: rom-extract-partition.py <rom> <particao> <saida>
     <particao> sem sufixo (vendor) tenta vendor_a, vendor_b e vendor, nessa ordem.
Saida: imprime "particao <nome> <bytes>" no stdout; erros no stderr, codigo != 0.
"""

import os
import struct
import sys
import tarfile

SPARSE_MAGIC = 0xED26FF3A
CHUNK_RAW, CHUNK_FILL, CHUNK_DONT_CARE, CHUNK_CRC32 = 0xCAC1, 0xCAC2, 0xCAC3, 0xCAC4
LP_GEOMETRY_MAGIC = 0x616C4467
LP_HEADER_MAGIC = 0x414C5030
LP_RESERVED = 4096
LP_GEOMETRY_SIZE = 4096
LP_TARGET_LINEAR, LP_TARGET_ZERO = 0, 1
SECTOR = 512
# Os metadados (geometria + copias) ficam bem antes do primeiro dado, alinhado a 1 MiB.
HEAD = 1 << 20


class Fail(Exception):
    pass


def is_archive(path):
    """Tar (cru ou comprimido) pela assinatura. tarfile.is_tarfile nao serve: um super
    cru comeca com zeros, que e um tar vazio valido."""
    with open(path, "rb") as f:
        h = f.read(512)
    return (h[:2] == b"\x1f\x8b" or h[:3] == b"BZh" or h[:6] == b"\xfd7zXZ\0"
            or h[:4] == b"\x28\xb5\x2f\xfd" or h[257:262] == b"ustar")


def open_super(path):
    """Devolve um fluxo binario do super.img e um rotulo para o log."""
    if os.path.isdir(path):
        for cand in ("images/super.img", "super.img"):
            p = os.path.join(path, cand)
            if os.path.isfile(p):
                return open(p, "rb"), p
        raise Fail(f"{path}: sem images/super.img")
    if is_archive(path):
        # Modo de fluxo: nao exige seek, le o .tgz uma vez so.
        tar = tarfile.open(path, "r|*")
        for m in tar:
            if m.isfile() and (m.name == "super.img" or m.name.endswith("/super.img")):
                return tar.extractfile(m), f"{path}:{m.name}"
        raise Fail(f"{path}: o pacote nao tem super.img")
    return open(path, "rb"), path


def read_exact(f, n):
    buf = bytearray()
    while len(buf) < n:
        b = f.read(n - len(buf))
        if not b:
            raise Fail("super.img truncado")
        buf += b
    return bytes(buf)


def blocks(f):
    """Gera (offset, bytes) do conteudo do super, cru ou esparso, em ordem crescente.
    Regioes "don't care" sao puladas (leem como zero, que e o que a saida ja tem)."""
    first = read_exact(f, 28)
    magic, major, _minor, fhdr, chdr, blk, _total_blks, nchunks, _crc = struct.unpack("<IHHHHIIII", first)
    if magic != SPARSE_MAGIC:
        # Cru: o fluxo e o proprio super.
        yield 0, first
        off = len(first)
        while True:
            b = f.read(4 << 20)
            if not b:
                return
            yield off, b
            off += len(b)
    if major != 1:
        raise Fail(f"sparse versao {major} desconhecida")
    if fhdr > 28:
        read_exact(f, fhdr - 28)
    off = 0
    for _ in range(nchunks):
        ctype, _r, csz, total = struct.unpack("<HHII", read_exact(f, 12))
        if chdr > 12:
            read_exact(f, chdr - 12)
        size = csz * blk
        if ctype == CHUNK_RAW:
            left = size
            while left:
                n = min(left, 4 << 20)
                yield off + size - left, read_exact(f, n)
                left -= n
        elif ctype == CHUNK_FILL:
            pat = read_exact(f, 4)
            if pat != b"\0\0\0\0":
                step = 1 << 20
                for o in range(0, size, step):
                    n = min(step, size - o)
                    yield off + o, pat * (n // 4)
        elif ctype == CHUNK_DONT_CARE:
            pass
        elif ctype == CHUNK_CRC32:
            read_exact(f, 4)
        else:
            raise Fail(f"chunk sparse desconhecido 0x{ctype:x}")
        off += size


def parse_lp(head):
    g = head[LP_RESERVED:LP_RESERVED + LP_GEOMETRY_SIZE]
    gmagic, _gsize = struct.unpack_from("<II", g, 0)
    if gmagic != LP_GEOMETRY_MAGIC:
        raise Fail("super.img sem metadados de particao dinamica (magic da geometria)")
    max_size, _slots, _lbs = struct.unpack_from("<III", g, 40)
    # Metadados primarios: depois da geometria e da copia dela.
    mo = LP_RESERVED + 2 * LP_GEOMETRY_SIZE
    hmagic, _maj, _min, hsize = struct.unpack_from("<IHHI", head, mo)
    if hmagic != LP_HEADER_MAGIC:
        raise Fail("cabecalho dos metadados LP invalido")
    # header: magic, major, minor, header_size, checksum[32], tables_size, checksum[32],
    # e os descritores partitions, extents, groups, block_devices (offset, num, size).
    d = mo + 4 + 2 + 2 + 4 + 32 + 4 + 32
    parts_t = struct.unpack_from("<III", head, d)
    ext_t = struct.unpack_from("<III", head, d + 12)
    tables = mo + hsize
    if tables + max_size > len(head):
        raise Fail("metadados LP maiores que o esperado")
    exts = []
    for i in range(ext_t[1]):
        o = tables + ext_t[0] + i * ext_t[2]
        nsec, ttype, tdata, _src = struct.unpack_from("<QIQI", head, o)
        exts.append((nsec, ttype, tdata))
    parts = {}
    for i in range(parts_t[1]):
        o = tables + parts_t[0] + i * parts_t[2]
        name = head[o:o + 36].split(b"\0", 1)[0].decode()
        _attr, first, num, _grp = struct.unpack_from("<IIII", head, o + 36)
        parts[name] = exts[first:first + num]
    return parts


def main():
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    rom, want, out = sys.argv[1:]
    f, label = open_super(rom)
    print(f"lendo {label}", file=sys.stderr)

    gen = blocks(f)
    head = bytearray(HEAD)
    pending = []
    for off, data in gen:
        if off < HEAD:
            n = min(len(data), HEAD - off)
            head[off:off + n] = data[:n]
        if off + len(data) > HEAD:
            pending.append((off, data))
            break
    parts = parse_lp(bytes(head))

    names = [want] if want in parts else [want + "_a", want + "_b", want]
    name = next((n for n in names if n in parts and parts[n]), None)
    if not name:
        raise Fail(f"particao {want} nao existe ou esta vazia; ha: {', '.join(sorted(parts))}")

    # Mapa: regiao fisica do super -> offset na particao.
    spans, logical = [], 0
    for nsec, ttype, tdata in parts[name]:
        if ttype == LP_TARGET_LINEAR:
            spans.append((tdata * SECTOR, (tdata + nsec) * SECTOR, logical))
        elif ttype != LP_TARGET_ZERO:
            raise Fail(f"extent de tipo {ttype} nao suportado")
        logical += nsec * SECTOR
    spans.sort()
    end_phys = max(s[1] for s in spans)

    with open(out, "wb") as o:
        o.truncate(logical)

        def put(off, data):
            a, b = off, off + len(data)
            for s, e, lo in spans:
                if e <= a or s >= b:
                    continue
                x, y = max(a, s), min(b, e)
                o.seek(lo + x - s)
                o.write(data[x - a:y - a])

        put(0, bytes(head))
        for off, data in pending:
            put(off, data)
        for off, data in gen:
            if off >= end_phys:
                break
            put(off, data)
    print(f"particao {name} {logical}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        print(f"erro: {e}", file=sys.stderr)
        sys.exit(1)
