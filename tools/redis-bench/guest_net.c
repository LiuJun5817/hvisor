/* Minimal IPv4 setup for the repository's guest image, which has no iproute2. */
#define _DEFAULT_SOURCE
#include <arpa/inet.h>
#include <net/if.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

static void check(int result, const char *operation) {
    if (result < 0) { perror(operation); exit(1); }
}

static void configure(int fd, const char *name, const char *address, const char *mask) {
    struct ifreq request = {0};
    snprintf(request.ifr_name, IFNAMSIZ, "%s", name);
    struct sockaddr_in *addr = (struct sockaddr_in *)&request.ifr_addr;
    addr->sin_family = AF_INET;
    if (inet_pton(AF_INET, address, &addr->sin_addr) != 1) exit(1);
    check(ioctl(fd, SIOCSIFADDR, &request), "SIOCSIFADDR");
    if (inet_pton(AF_INET, mask, &addr->sin_addr) != 1) exit(1);
    check(ioctl(fd, SIOCSIFNETMASK, &request), "SIOCSIFNETMASK");
    check(ioctl(fd, SIOCGIFFLAGS, &request), "SIOCGIFFLAGS");
    request.ifr_flags |= IFF_UP;
    check(ioctl(fd, SIOCSIFFLAGS, &request), "SIOCSIFFLAGS");
    printf("network_ready interface=%s address=%s mask=%s\n", name, address, mask);
}

int main(int argc, char **argv) {
    if (argc != 2 || strlen(argv[1]) >= IFNAMSIZ) {
        fprintf(stderr, "usage: %s INTERFACE\n", argv[0]); return 1;
    }
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    check(fd, "socket");
    configure(fd, "lo", "127.0.0.1", "255.0.0.0");
    configure(fd, argv[1], "10.0.2.15", "255.255.255.0");
    close(fd);
    return 0;
}
