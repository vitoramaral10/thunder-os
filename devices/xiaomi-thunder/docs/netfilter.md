# Netfilter rB: acceptance and test plan

What rB is and why: `kernel.md`, section "rB". Design: `build/research/nft/DESIGN.md`.
U = unprivileged user, R = root. Results on the device: section 3.

## 0. Rules

- `fastboot boot` does **not** work on this bootloader (tested 2026-10-12: it reboots into
  `boot_a`). There is no boot in RAM. The only way to try the kernel is to install it with
  `tools/update-kernel.sh --reboot`, so the rollback material has to exist **before** that.
- Before installing: save on the host (a) the current good boot image, as a dump of `boot_a` checked by
  sha256 against what runs (not `/var/lib/thunder/boot-previous.img`: with `BOOT_DEPLOY=fastboot` the
  `boot-deploy` hook exits before writing it, so an old file with that name can mislead), and (b) the current
  `linux-xiaomi-thunder-*.pkg.tar.zst` from the pacman cache (modules of the old build). Rollback:
  `fastboot flash boot_a <good image>` (reach it with `ssh root@172.16.42.1 thunder-reboot bootloader`;
  if the new kernel does not come up, there is no SSH: enter fastboot with the buttons, Volume Down +
  Power from power-off) and `pacman -U --overwrite '*' <old package>`.
- Build and test one at a time, `-j 2`, under `nice`/`ionice` (and `systemd-run` limits on the homelab).

## 1. Build acceptance (`tools/build-kernel.sh`, `tools/package-kernel.sh`)

1. The log shows the 9 new `patch:` lines with no `FAILED` and no `fuzz`; `offset` is expected for the gate.
2. `out/.config` has `CONFIG_NF_TABLES=y`, `# CONFIG_STATIC_USERMODEHELPER is not set`,
   `# CONFIG_NF_CT_PROTO_DCCP is not set`, `# CONFIG_NET_ACT_IPT is not set`,
   `# CONFIG_NF_CONNTRACK_TIMEOUT is not set`, and has **no** `CONFIG_NF_NAT_PROTO_DCCP` and no
   `CONFIG_STATIC_USERMODEHELPER_PATH`.
3. No new warnings in `net/netfilter`, `net/bridge`, `net/ipv6/netfilter`, `net/sched` against the previous build.
4. `nm vmlinux | grep -E 'nf_net_admin_capable|sysctl_nf_userns_restrict|hw_protection_shutdown'` finds all three.
5. `out/modules.order` gains **10** `nft_*` modules: `nft_redir`, `nft_redir_ipv4`, `nft_redir_ipv6`,
   `nft_hash`, `nft_numgen`, `nft_quota`, `nft_objref`, `nft_connlimit`, `nft_queue`, `nft_reject_bridge`.
   No `act_ipt.ko`.
6. **The kernel package carries the core_pattern override:**
   `bsdtar -tf build/xiaomi-thunder/out/linux-xiaomi-thunder.pkg.tar.zst | grep usr/lib/sysctl.d/51-thunder-coredump-no-pidfd.conf`
   must match (the script already fails without it for kernels < 6.16), and the file must read
   `kernel.core_pattern=|/usr/lib/systemd/systemd-coredump %P %u %g %s %t %c %h %d` (no `%F`, no `%I`).
7. `Image.gz` (13,693,374 B before) and the `boot.img` fit `boot_a` (`blockdev --getsize64` on the device).
8. Slice invariant (DESIGN 1.2): the patch recipe, run again, gives the same body sha256 as the header.

## 2. Device tests (after `update-kernel.sh --reboot`)

1. `uname -a`; `sysctl net.netfilter.nf_userns_restrict` is `= 1`; `dmesg | grep -i 'nf_tables\|netfilter'` has no
   "cannot register nf_userns_restrict".
2. **Gate, netlink.** U: `unshare -Urn nft list ruleset` fails with "Operation not permitted". U:
   `unshare -Urn python3 -c 'import socket; s=socket.socket(16,3,12); s.bind((0,1))'` raises `PermissionError`.
3. **Gate, sockopts.** U: `XTABLES_LOCKFILE=/tmp/xt-$USER.lock unshare -Urn iptables-legacy -L` fails with
   "can't initialize iptables table `filter': Permission denied" (without `XTABLES_LOCKFILE` it stops earlier, on
   the root-owned `/run/xtables.lock`, and proves nothing). **Positive, R:** `iptables-legacy -L` works
   and lists the chains (the sockopt path after the gate), and so does `ip6tables-legacy -L`.
4. **The knob.**
   - R: `sysctl -w net.netfilter.nf_userns_restrict=0`; now U: `unshare -Urn nft list ruleset` works (empty
     output). R sets it back to `1`.
   - U (no capability): `echo 0 > /proc/sys/net/netfilter/nf_userns_restrict` fails.
   - **Non-root with only `CAP_NET_ADMIN`:** as R,
     `setpriv --reuid=1000 --regid=1000 --clear-groups --inh-caps=+net_admin --ambient-caps=+net_admin sh -c 'grep CapEff /proc/self/status; echo 0 > /proc/sys/net/netfilter/nf_userns_restrict'`
     must show a non-zero `CapEff` and then fail with `Operation not permitted` (EPERM from the handler;
     `net_ctl_permissions()` lets the open succeed). `sysctl net.netfilter.nf_userns_restrict` stays `1`.
   - U: `unshare -Urn ls /proc/sys/net/netfilter/nf_userns_restrict` is ENOENT (init_net only).
5. **act_ipt is gone.** `grep -c act_ipt /lib/modules/*/modules.order` is 0, and the tc actions registered in
   the running kernel (`grep -oE " tcf_[a-z]+_init$" /proc/kallsyms`) are only `mirred` and `police`: no
   `tcf_ipt_init`. That is the proof. A tc-level test does not reach the kernel here: this kernel has no `u32`
   classifier nor `gact` action (use `fw` + `police` as the positive control:
   `tc filter add dev lo parent ffff: protocol ip prio 1 handle 1 fw action police rate 1mbit burst 10k drop`
   works inside `unshare -Urn`), and Arch's `tc` does not build an `xt` action request at all (it prints the
   generic action help).
5b. **cgroup v1 `release_agent` (CVE-2022-0492).** R: `mkdir -p /mnt/t && mount -t cgroup -o none,name=t cgroup /mnt/t`.
   U: `unshare -UrmC sh -c 'mkdir -p /tmp/c && mount -t cgroup -o none,name=t cgroup /tmp/c && echo /tmp/x > /tmp/c/release_agent'`
   must fail with `Operation not permitted`. R: `umount /mnt/t`.
6. **Firewall as R.** Load with `nft -f` a ruleset written one statement per line (on a single line nft
   needs `;` before every `}`): a `table inet f` with an input chain
   (`type filter hook input priority 0; policy accept;`, then `ct state established,related counter accept` and
   `ct state invalid counter drop`), and `table ip nat` / `table ip6 nat` with a postrouting chain
   (`type nat hook postrouting priority 100;`, then `oifname "wlan0" masquerade`). SSH over the cable stays up and the counters grow.
   `iptables -t nat -A POSTROUTING -o wlan0 -j MASQUERADE && iptables-save` and `iptables -L` work (nft
   backend). Then `nft flush ruleset`.
7. **Reject IPv6.** `ip6tables -A OUTPUT -p tcp --dport 9 -j REJECT --reject-with tcp-reset; nc -6 ::1 9`
   refuses without a panic. The `ccmni` path needs mobile data and stays untested.
8. **Coredump.**
   - `sysctl kernel.core_pattern` shows the override from the package file (it ends in `%h %d`) after a
     reboot or `sysctl --system`.
   - U: `sleep 100 & kill -SEGV $!`; `coredumpctl list` shows it and `coredumpctl info` has the right PID, UID
     and signal 11.
   - Control: with the stock pattern (`... %d %F %I`) the entry is lost or the fields are shifted; this
     justifies the override.
9. **Autoload.** `cat /proc/sys/kernel/modprobe` is `/sbin/modprobe`.
   `modprobe -r tcp_htcp; sysctl -w net.ipv4.tcp_congestion_control=htcp` loads it alone.
   `nft add table ip t; nft add chain ip t c; nft add rule ip t c meta mark set numgen inc mod 2` loads `nft_numgen`;
   then `nft delete table ip t`.
10. **bubblewrap intact.** U: `bwrap --unshare-all --ro-bind / / true; echo $?` is 0.
11. **Wi-Fi up** (modules of the same build), and **NM hotspot** (`nmcli dev wifi hotspot ...`): `nm-shared-*`
    shows in `nft list ruleset`, a client gets DHCP and NAT. Note CVE-2024-26921 (kernel.md): this is a
    stateful IPv6 firewall, so do it only knowing that risk.
12. **Clean poweroff.** `systemctl poweroff`; on the next boot `journalctl -b -1 | tail` shows an orderly
    shutdown. The kernel path (`orderly_poweroff` from thermal or battery) has no safe trigger and stays
    untested; see the risk in `kernel.md`.
13. **Regression.** `systemctl --failed`, boot time and `journalctl -b -p err` against rA.

The public PoC of CVE-2024-1086 is not run: the gate stops it for a user, and as root it risks the kernel.
The slice invariant already shows `nf_tables_api.c` equal to `v4.19.325`.

## 3. Results on xiaomi-thunder (2026-10-12, kernel #1 Mon Oct 12 08:42:49)

- Build: the series applied with no reject; `out/.config` as in section 1.2; `Image` 13,812,711 B.
- 1: gate sysctl at 1, registered. 2: `nft` and a raw `NETLINK_NETFILTER` bind from a user namespace get
  EPERM. 3: `iptables-legacy` from a user namespace is refused by the kernel; as root it works, IPv4 and IPv6.
- 4: with the knob at 0 the user namespace gets in; the user cannot write it; a non-root process with
  `CAP_NET_ADMIN` gets EPERM; a child netns has no such sysctl.
- 5: no `act_ipt` in the kernel (registered actions: mirred, police). 5b: `release_agent` write from
  `unshare -UrmC` gets EPERM.
- 6: inet filter with `ct state` counters growing, ip and ip6 NAT tables loaded, `iptables` on the nf_tables
  backend works, SSH over the cable unaffected. 7: IPv6 `REJECT --reject-with tcp-reset` refuses, no oops.
- 8: `core_pattern` comes from the package file; a SIGSEGV of a user process shows in `coredumpctl` with the
  right PID, UID and signal, core stored.
- 9: `tcp_htcp` and `nft_numgen` load on demand. 10: bubblewrap works. 11: Wi-Fi associates (the NVRAM still
  applies). Hotspot not tested (it would take the user's Wi-Fi down; AP mode on gen4m is untested).
- 12: `systemctl reboot` shuts down cleanly through `systemd-shutdown`; a real power-off needs someone to
  power the phone on again and was not run. 13: `running`, no failed unit, boot 8.2 s.
- Rebuilt with the missing-battery poweroff fallback (kernel #1 Mon Oct 12 09:12:14): the 9 patches apply
  with no reject, no new build warning, `hw_protection_shutdown` in `/proc/kallsyms`; 1, 2, 6, 8, 10 and 11
  pass again, screen blank/unblank logs no GPIO warning, `running`, boot 8.2 s. The fallback itself has
  no safe trigger (it needs a missing battery) and is untested.

