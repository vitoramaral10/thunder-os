/*
 * Reinicia o thunder num modo do bootloader: "thunder-reboot bootloader" cai no fastboot.
 * O reboot do busybox e o do openrc-shutdown descartam o argumento; o LK so recebe o
 * modo pela forma de quatro argumentos da syscall (LINUX_REBOOT_CMD_RESTART2), que
 * nenhuma libc expoe.
 */
#include <linux/reboot.h>
#include <stdio.h>
#include <sys/syscall.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	const char *mode = argc > 1 ? argv[1] : "bootloader";

	sync();
	if (syscall(SYS_reboot, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
		    LINUX_REBOOT_CMD_RESTART2, mode) < 0) {
		perror("reboot");
		return 1;
	}
	return 0;
}
