#!/usr/bin/env python3
"""Le registradores fisicos via /dev/mem (mmap alinhado a pagina)."""
import mmap, os, struct, sys

def ler(base, n=8):
    pagina = 0x1000
    ini = base & ~(pagina - 1)
    off = base - ini
    fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    try:
        m = mmap.mmap(fd, pagina, mmap.MAP_SHARED, mmap.PROT_READ, offset=ini)
    except OSError as e:
        os.close(fd)
        print(f"ERRO mmap em 0x{ini:08x}: {e}")
        print("(EPERM/EINVAL aqui = CONFIG_STRICT_DEVMEM bloqueando)")
        return
    try:
        for i in range(n):
            a = off + i * 4
            v = struct.unpack("<I", m[a:a+4])[0]
            print(f"  0x{base + i*4:08x} = 0x{v:08x}")
    finally:
        m.close(); os.close(fd)

# TX DMA do BTIF: segundo "reg" do no btif@1100c000 no device tree
BASE = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0x10217d80
N = int(sys.argv[2]) if len(sys.argv) > 2 else 8
print(f"=== dump de 0x{BASE:08x} ({N} palavras) ===")
ler(BASE, N)
