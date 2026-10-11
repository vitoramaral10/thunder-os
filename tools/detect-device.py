#!/usr/bin/env python3
"""Detecta o aparelho no cabo USB e imprime um JSON no stdout.

Procura, nesta ordem:
  fastboot  bootloader: product, unlocked, current-slot
  adb       Android de fabrica: ro.product.device, modelo, fabricante
  thunder   Thunder OS rodando: /etc/thunder/device por SSH em 172.16.42.1
e casa o resultado com os aparelhos de devices/*/device.sh (FASTBOOT_PRODUCTS para o
fastboot, ANDROID_DEVICES para o adb, DEVICE para o Thunder OS).

Saida: {"mode": "fastboot"|"adb"|"thunder"|"none", "serial", "product", "model",
        "vendor", "unlocked", "slot", "device": "<pasta em devices/>"|null, "detail"}
Nunca escreve nada no aparelho.
"""

import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THUNDER_ADDR = "172.16.42.1"


def run(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def catalogue():
    rc, out = run([os.path.join(REPO, "tools", "installer-info.sh")], timeout=30)
    try:
        return json.loads(out)["devices"] if rc == 0 else []
    except (ValueError, KeyError):
        return []


def fastboot():
    if not shutil.which("fastboot"):
        return None
    rc, out = run(["fastboot", "devices"])
    serials = [l.split()[0] for l in out.splitlines() if l.strip().endswith("fastboot")]
    if rc != 0 or not serials:
        return None
    serial = serials[0]

    def var(name):
        # getvar responde no stderr ("product: thunder").
        _, o = run(["fastboot", "-s", serial, "getvar", name])
        for line in o.splitlines():
            if line.startswith(name + ":"):
                return line.split(":", 1)[1].strip()
        return ""

    return {"mode": "fastboot", "serial": serial, "product": var("product"),
            "unlocked": var("unlocked") == "yes", "slot": var("current-slot")}


def adb():
    if not shutil.which("adb"):
        return None
    rc, out = run(["adb", "devices"])
    rows = [l.split() for l in out.splitlines()[1:] if l.strip()]
    if rc != 0 or not rows:
        return None
    serial, state = rows[0][0], rows[0][1]
    if state != "device":
        # unauthorized/offline: a depuracao USB precisa ser aceita no aparelho.
        return {"mode": "adb", "serial": serial, "detail": state}

    def prop(name):
        _, o = run(["adb", "-s", serial, "shell", "getprop", name])
        return o.strip()

    return {"mode": "adb", "serial": serial, "product": prop("ro.product.device"),
            "model": prop("ro.product.model"), "vendor": prop("ro.product.manufacturer")}


def thunder():
    if not shutil.which("ssh"):
        return None
    rc, _ = run(["ping", "-c", "1", "-W", "1", THUNDER_ADDR], timeout=3)
    if rc != 0:
        return None
    rc, out = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                   "-o", "StrictHostKeyChecking=accept-new",
                   f"root@{THUNDER_ADDR}", "cat /etc/thunder/device"], timeout=8)
    info = {"mode": "thunder", "serial": THUNDER_ADDR}
    if rc != 0:
        info["detail"] = "ssh sem acesso (chave?)"
        return info
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v.strip().strip("'")
    info["product"] = info.get("DEVICE_CODENAME", "")
    info["model"] = info.get("DEVICE_NAME", "")
    info["vendor"] = info.get("DEVICE_VENDOR", "")
    return info


def match(found, devices):
    for d in devices:
        if found["mode"] == "fastboot" and found.get("product") in d["fastboot_products"]:
            return d["id"]
        if found["mode"] == "adb" and found.get("product") in d["android_devices"]:
            return d["id"]
        if found["mode"] == "thunder" and found.get("DEVICE") == d["id"]:
            return d["id"]
    return None


def main():
    found = fastboot() or adb() or thunder() or {"mode": "none"}
    found["device"] = match(found, catalogue()) if found["mode"] != "none" else None
    for k in ("serial", "product", "model", "vendor", "slot", "detail"):
        found.setdefault(k, "")
    found.setdefault("unlocked", None)
    for k in [k for k in found if k.startswith(("DEVICE", "THUNDER_"))]:
        del found[k]
    json.dump(found, sys.stdout)
    print()


if __name__ == "__main__":
    main()
