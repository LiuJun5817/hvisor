#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <netinet/in.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* Linux guest launch-to-ready benchmark. Each sample owns exactly one child.
 * The main clock starts immediately before fork(), and ends after a valid PONG.
 * The child also records a diagnostic timestamp immediately before execv().
 * Configuration, argument construction, log-file setup and port checks precede
 * the main clock. Identity checking, SET/GET validation and teardown follow it.
 */
struct options {
    const char *redis;
    const char *config;
    unsigned repeats;
    unsigned port;
    unsigned timeout_ms;
    unsigned poll_us;
    int server_cpu;
    int observer_cpu;
};

struct shared_clock {
    _Atomic uint64_t exec_ns;
};

static volatile sig_atomic_t interrupted;

static void handle_signal(int signo) { interrupted = signo; }

static uint64_t monotonic_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        perror("clock_gettime");
        exit(2);
    }
    return (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
}

static void errorf(char *error, size_t size, const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vsnprintf(error, size, fmt, args);
    va_end(args);
}

static int set_cpu(int cpu) {
    if (cpu < 0)
        return 0;
    cpu_set_t cpus;
    CPU_ZERO(&cpus);
    CPU_SET(cpu, &cpus);
    return sched_setaffinity(0, sizeof(cpus), &cpus);
}

static void pause_ns(uint64_t ns) {
    struct timespec ts = {
        .tv_sec = (time_t)(ns / UINT64_C(1000000000)),
        .tv_nsec = (long)(ns % UINT64_C(1000000000)),
    };
    while (nanosleep(&ts, &ts) < 0 && errno == EINTR && !interrupted) {}
}

static int wait_socket(int fd, short events, uint64_t deadline) {
    for (;;) {
        if (interrupted) {
            errno = EINTR;
            return -1;
        }
        uint64_t now = monotonic_ns();
        if (now >= deadline) {
            errno = ETIMEDOUT;
            return -1;
        }
        uint64_t ms = (deadline - now + 999999) / 1000000;
        int timeout = (int)(ms > 50 ? 50 : ms);
        struct pollfd pfd = {.fd = fd, .events = events};
        int rc = poll(&pfd, 1, timeout);
        if (rc > 0)
            return 0;
        if (rc < 0 && errno != EINTR)
            return -1;
    }
}

static int send_all(int fd, const char *data, size_t size, uint64_t deadline) {
    while (size) {
        ssize_t n = send(fd, data, size, MSG_NOSIGNAL);
        if (n > 0) {
            data += n;
            size -= (size_t)n;
        } else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (wait_socket(fd, POLLOUT, deadline) < 0)
                return -1;
        } else if (n < 0 && errno == EINTR) {
            if (interrupted)
                return -1;
        } else {
            if (n == 0)
                errno = EPIPE;
            return -1;
        }
    }
    return 0;
}

static int receive_exact(int fd, char *data, size_t size, uint64_t deadline) {
    while (size) {
        ssize_t n = recv(fd, data, size, 0);
        if (n > 0) {
            data += n;
            size -= (size_t)n;
        } else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (wait_socket(fd, POLLIN, deadline) < 0)
                return -1;
        } else if (n < 0 && errno == EINTR) {
            if (interrupted)
                return -1;
        } else {
            if (n == 0)
                errno = ECONNRESET;
            return -1;
        }
    }
    return 0;
}

/* Used only outside the timed startup interval for RESP bulk replies. */
static int receive_line(int fd, char *data, size_t capacity, uint64_t deadline) {
    for (size_t n = 0; n + 1 < capacity; ++n) {
        if (receive_exact(fd, data + n, 1, deadline) < 0)
            return -1;
        if (n > 0 && data[n - 1] == '\r' && data[n] == '\n') {
            data[n - 1] = '\0';
            return 0;
        }
    }
    errno = EMSGSIZE;
    return -1;
}

static int receive_bulk(int fd, char *data, size_t capacity, uint64_t deadline) {
    char line[128];
    if (receive_line(fd, line, sizeof(line), deadline) < 0)
        return -1;
    if (line[0] != '$') {
        errno = EPROTO;
        return -1;
    }
    char *end = NULL;
    errno = 0;
    long length = strtol(line + 1, &end, 10);
    if (errno || end == line + 1 || *end || length < 0 ||
        (unsigned long)length >= capacity) {
        errno = EPROTO;
        return -1;
    }
    if (receive_exact(fd, data, (size_t)length, deadline) < 0)
        return -1;
    data[length] = '\0';
    char ending[2];
    if (receive_exact(fd, ending, sizeof(ending), deadline) < 0)
        return -1;
    if (memcmp(ending, "\r\n", 2)) {
        errno = EPROTO;
        return -1;
    }
    return (int)length;
}

static int connect_server(unsigned port, uint64_t deadline) {
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0)
        return -1;
    struct sockaddr_in address = {
        .sin_family = AF_INET,
        .sin_port = htons((uint16_t)port),
        .sin_addr.s_addr = htonl(INADDR_LOOPBACK),
    };
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        if (errno != EINPROGRESS || wait_socket(fd, POLLOUT, deadline) < 0)
            goto failed;
        int socket_error = 0;
        socklen_t size = sizeof(socket_error);
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &socket_error, &size) < 0)
            goto failed;
        if (socket_error) {
            errno = socket_error;
            goto failed;
        }
    }
    return fd;
failed: {
    int saved = errno;
    close(fd);
    errno = saved;
    return -1;
}
}

static int check_port(unsigned port) {
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    int reuse = 1;
    (void)setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    struct sockaddr_in address = {
        .sin_family = AF_INET,
        .sin_port = htons((uint16_t)port),
        .sin_addr.s_addr = htonl(INADDR_LOOPBACK),
    };
    int rc = bind(fd, (struct sockaddr *)&address, sizeof(address));
    int saved = errno;
    close(fd);
    errno = saved;
    return rc;
}

static int child_status(pid_t pid, bool *reaped, int *status) {
    if (*reaped)
        return 1;
    pid_t rc = waitpid(pid, status, WNOHANG);
    if (rc == pid) {
        *reaped = true;
        return 1;
    }
    return rc < 0 && errno != EINTR ? -1 : 0;
}

static int stop_child(pid_t pid, bool *reaped, int *status) {
    if (*reaped)
        return 0;
    if (kill(pid, SIGTERM) < 0 && errno != ESRCH)
        return -1;
    uint64_t deadline = monotonic_ns() + UINT64_C(2000000000);
    do {
        int rc = child_status(pid, reaped, status);
        if (rc != 0)
            return rc < 0 ? -1 : 0;
        pause_ns(UINT64_C(1000000));
    } while (monotonic_ns() < deadline);
    if (kill(pid, SIGKILL) < 0 && errno != ESRCH)
        return -1;
    pid_t rc;
    do {
        rc = waitpid(pid, status, 0);
    } while (rc < 0 && errno == EINTR);
    if (rc == pid) {
        *reaped = true;
        return 0;
    }
    return -1;
}

static int validate_server(int fd, pid_t pid, unsigned repeat, unsigned timeout_ms,
                           char *error, size_t error_size) {
    uint64_t deadline = monotonic_ns() + (uint64_t)timeout_ms * 1000000;
    static const char info[] = "*2\r\n$4\r\nINFO\r\n$6\r\nserver\r\n";
    char reply[16384];
    if (send_all(fd, info, sizeof(info) - 1, deadline) < 0 ||
        receive_bulk(fd, reply, sizeof(reply), deadline) < 0) {
        errorf(error, error_size, "INFO server failed: %s", strerror(errno));
        return -1;
    }
    char expected_pid[80];
    snprintf(expected_pid, sizeof(expected_pid), "\r\nprocess_id:%ld\r\n", (long)pid);
    if (!strstr(reply, expected_pid)) {
        errorf(error, error_size, "Redis process_id does not match owned child %ld", (long)pid);
        return -1;
    }
    char key[96], value[96], request[512];
    snprintf(key, sizeof(key), "__redis_bench_startup__:%ld:%u", (long)pid, repeat);
    snprintf(value, sizeof(value), "validated-%ld-%u", (long)pid, repeat);
    int length = snprintf(request, sizeof(request),
        "*3\r\n$3\r\nSET\r\n$%zu\r\n%s\r\n$%zu\r\n%s\r\n",
        strlen(key), key, strlen(value), value);
    if (send_all(fd, request, (size_t)length, deadline) < 0 ||
        receive_exact(fd, reply, 5, deadline) < 0) {
        errorf(error, error_size, "SET validation failed: %s", strerror(errno));
        return -1;
    }
    if (memcmp(reply, "+OK\r\n", 5)) {
        errorf(error, error_size, "SET validation returned an unexpected reply");
        return -1;
    }
    length = snprintf(request, sizeof(request), "*2\r\n$3\r\nGET\r\n$%zu\r\n%s\r\n",
                      strlen(key), key);
    if (send_all(fd, request, (size_t)length, deadline) < 0 ||
        receive_bulk(fd, reply, sizeof(reply), deadline) < 0) {
        errorf(error, error_size, "GET validation failed: %s", strerror(errno));
        return -1;
    }
    if (strcmp(reply, value)) {
        errorf(error, error_size, "GET validation returned the wrong value");
        return -1;
    }
    return 0;
}

static void json_string(const char *text) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
        if (*p == '"' || *p == '\\')
            printf("\\%c", *p);
        else if (*p < 32 || *p >= 127)
            printf("\\u%04x", (unsigned)*p);
        else
            putchar(*p);
    }
    putchar('"');
}

static int run_sample(const struct options *opts, unsigned repeat) {
    char error[512] = "";
    char port[16];
    snprintf(port, sizeof(port), "%u", opts->port);
    char data_dir[] = "/tmp/redis-startup-data-XXXXXX";
    bool data_dir_created = false;
    char *const args[] = {
        (char *)opts->redis, (char *)opts->config,
        "--port", port, "--bind", "127.0.0.1", "--protected-mode", "yes",
        "--daemonize", "no", "--supervised", "no", "--save", "",
        "--appendonly", "no", "--logfile", "", "--pidfile", "",
        "--dir", data_dir, NULL,
    };
    struct shared_clock *shared = MAP_FAILED;
    char log_name[] = "/tmp/redis-startup-XXXXXX";
    int log_fd = -1, fd = -1, exec_pipe[2] = {-1, -1};
    pid_t pid = -1;
    bool reaped = false;
    int status = 0;
    uint64_t spawn_ns = 0, ready_ns = 0, exec_ns = 0;
    unsigned probes = 0;
    if (check_port(opts->port) < 0) {
        errorf(error, sizeof(error), "port %u is unavailable: %s", opts->port, strerror(errno));
        goto cleanup;
    }
    if (!mkdtemp(data_dir)) {
        errorf(error, sizeof(error), "data directory setup failed: %s", strerror(errno));
        goto cleanup;
    }
    data_dir_created = true;
    shared = mmap(NULL, sizeof(*shared), PROT_READ | PROT_WRITE,
                  MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (shared == MAP_FAILED) {
        errorf(error, sizeof(error), "mmap failed: %s", strerror(errno));
        goto cleanup;
    }
    atomic_init(&shared->exec_ns, 0);
    if (pipe2(exec_pipe, O_CLOEXEC | O_NONBLOCK) < 0 ||
        (log_fd = mkostemp(log_name, O_CLOEXEC)) < 0) {
        errorf(error, sizeof(error), "child setup failed: %s", strerror(errno));
        goto cleanup;
    }
    fflush(NULL);
    pid_t observer_pid = getpid();
    spawn_ns = monotonic_ns();
    pid = fork();
    if (pid == 0) {
        close(exec_pipe[0]);
        int child_error = 0;
        if (prctl(PR_SET_PDEATHSIG, SIGKILL) < 0 ||
            dup2(log_fd, STDOUT_FILENO) < 0 || dup2(log_fd, STDERR_FILENO) < 0 ||
            set_cpu(opts->server_cpu) < 0) {
            child_error = errno;
        } else {
            /* Close the race where the observer died before prctl(). */
            if (getppid() != observer_pid)
                _exit(127);
            close(log_fd);
            atomic_store_explicit(&shared->exec_ns, monotonic_ns(), memory_order_release);
            execv(opts->redis, args);
            child_error = errno;
        }
        ssize_t written = write(exec_pipe[1], &child_error, sizeof(child_error));
        (void)written;
        _exit(127);
    }
    if (pid < 0) {
        errorf(error, sizeof(error), "fork failed: %s", strerror(errno));
        goto cleanup;
    }
    close(exec_pipe[1]);
    exec_pipe[1] = -1;
    uint64_t deadline = spawn_ns + (uint64_t)opts->timeout_ms * 1000000;
    while (!interrupted && monotonic_ns() < deadline) {
        int exited = child_status(pid, &reaped, &status);
        if (exited != 0) {
            int child_error = 0;
            ssize_t n = read(exec_pipe[0], &child_error, sizeof(child_error));
            if (n == sizeof(child_error))
                errorf(error, sizeof(error), "child setup/exec failed: %s", strerror(child_error));
            else
                errorf(error, sizeof(error), "child exited before ready (wait status %d)", status);
            goto cleanup;
        }
        ++probes;
        fd = connect_server(opts->port, deadline);
        if (fd >= 0) {
            static const char ping[] = "*1\r\n$4\r\nPING\r\n";
            char pong[7];
            if (send_all(fd, ping, sizeof(ping) - 1, deadline) < 0 ||
                receive_exact(fd, pong, sizeof(pong), deadline) < 0) {
                errorf(error, sizeof(error), "PING failed: %s", strerror(errno));
                goto cleanup;
            }
            ready_ns = monotonic_ns();
            if (memcmp(pong, "+PONG\r\n", sizeof(pong))) {
                errorf(error, sizeof(error), "PING returned an unexpected reply");
                goto cleanup;
            }
            break;
        }
        if (errno != ECONNREFUSED && errno != EINTR) {
            errorf(error, sizeof(error), "connect failed: %s", strerror(errno));
            goto cleanup;
        }
        uint64_t now = monotonic_ns();
        if (now < deadline) {
            uint64_t delay = (uint64_t)opts->poll_us * 1000;
            if (delay > deadline - now)
                delay = deadline - now;
            pause_ns(delay);
        }
    }
    if (interrupted) {
        errorf(error, sizeof(error), "interrupted by signal %d", (int)interrupted);
        goto cleanup;
    }
    if (!ready_ns) {
        errorf(error, sizeof(error), "startup timed out after %u ms", opts->timeout_ms);
        goto cleanup;
    }
    exec_ns = atomic_load_explicit(&shared->exec_ns, memory_order_acquire);
    if (!exec_ns || exec_ns < spawn_ns || exec_ns > ready_ns) {
        errorf(error, sizeof(error), "missing or invalid child exec timestamp");
        goto cleanup;
    }
    if (validate_server(fd, pid, repeat, opts->timeout_ms, error, sizeof(error)) < 0)
        goto cleanup;
    if (child_status(pid, &reaped, &status) != 0)
        errorf(error, sizeof(error), "child exited during validation (wait status %d)", status);

cleanup:
    if (fd >= 0)
        close(fd);
    if (pid > 0 && stop_child(pid, &reaped, &status) < 0 && !error[0])
        errorf(error, sizeof(error), "failed to reap owned child: %s", strerror(errno));
    if (shared != MAP_FAILED)
        munmap(shared, sizeof(*shared));
    if (exec_pipe[0] >= 0)
        close(exec_pipe[0]);
    if (exec_pipe[1] >= 0)
        close(exec_pipe[1]);
    if (data_dir_created && rmdir(data_dir) < 0 && !error[0])
        errorf(error, sizeof(error), "temporary data directory cleanup failed: %s", strerror(errno));
    printf("{\"repeat\":%u,\"status\":\"%s\",\"pid\":%ld,\"port\":%u,"
           "\"poll_us\":%u,\"timeout_ms\":%u,\"server_cpu\":%d,\"observer_cpu\":%d,"
           "\"clock\":\"CLOCK_MONOTONIC\",\"probes\":%u",
           repeat, error[0] ? "error" : "ok", (long)pid, opts->port,
           opts->poll_us, opts->timeout_ms, opts->server_cpu, opts->observer_cpu, probes);
    if (!error[0]) {
        printf(",\"startup_ns\":%" PRIu64 ",\"exec_to_ready_ns\":%" PRIu64
               ",\"spawn_to_exec_ns\":%" PRIu64 ",\"validation\":\"set_get\"",
               ready_ns - spawn_ns, ready_ns - exec_ns, exec_ns - spawn_ns);
    } else {
        printf(",\"error\":");
        json_string(error);
        if (log_fd >= 0) {
            char log[4097];
            off_t length = lseek(log_fd, 0, SEEK_END);
            if (length >= 0) {
                (void)lseek(log_fd, length > 4096 ? length - 4096 : 0, SEEK_SET);
                ssize_t n = read(log_fd, log, sizeof(log) - 1);
                if (n > 0) {
                    log[n] = '\0';
                    printf(",\"redis_log_tail\":");
                    json_string(log);
                }
            }
        }
    }
    puts("}");
    fflush(stdout);
    if (log_fd >= 0) {
        close(log_fd);
        unlink(log_name);
    }
    return error[0] ? -1 : 0;
}

static void usage(FILE *out, const char *program) {
    fprintf(out,
        "Usage: %s --redis PATH --config PATH [options]\n"
        "  --repeats N       independent process starts (default 10)\n"
        "  --port P          loopback Redis port (default 16379)\n"
        "  --timeout-ms N    startup/validation timeout (default 30000)\n"
        "  --poll-us N       delay after refused connection (default 1000)\n"
        "  --server-cpu N    pin Redis child to guest CPU N\n"
        "  --observer-cpu N  pin the observer to guest CPU N\n"
        "Main metric startup_ns: immediately before fork to first valid PONG.\n"
        "exec_to_ready_ns excludes fork/child setup. Validation and shutdown\n"
        "are excluded from both metrics. JSON lines are emitted after cleanup.\n",
        program);
}

static unsigned parse_number(const char *text, unsigned max, const char *name) {
    char *end;
    errno = 0;
    unsigned long value = strtoul(text, &end, 10);
    if (errno || !*text || *end || text[0] == '-' || value > max) {
        fprintf(stderr, "invalid %s: %s\n", name, text);
        exit(2);
    }
    return (unsigned)value;
}

int main(int argc, char **argv) {
    struct options opts = {
        .repeats = 10, .port = 16379, .timeout_ms = 30000, .poll_us = 1000,
        .server_cpu = -1, .observer_cpu = -1,
    };
    for (int i = 1; i < argc; ++i) {
        const char *arg = argv[i];
        if (!strcmp(arg, "--help")) {
            usage(stdout, argv[0]);
            return 0;
        }
        if (i + 1 == argc) {
            usage(stderr, argv[0]);
            return 2;
        }
        const char *value = argv[++i];
        if (!strcmp(arg, "--redis"))
            opts.redis = value;
        else if (!strcmp(arg, "--config"))
            opts.config = value;
        else if (!strcmp(arg, "--repeats"))
            opts.repeats = parse_number(value, 1000000, arg);
        else if (!strcmp(arg, "--port"))
            opts.port = parse_number(value, 65535, arg);
        else if (!strcmp(arg, "--timeout-ms"))
            opts.timeout_ms = parse_number(value, 3600000, arg);
        else if (!strcmp(arg, "--poll-us"))
            opts.poll_us = parse_number(value, 1000000, arg);
        else if (!strcmp(arg, "--server-cpu"))
            opts.server_cpu = (int)parse_number(value, CPU_SETSIZE - 1, arg);
        else if (!strcmp(arg, "--observer-cpu"))
            opts.observer_cpu = (int)parse_number(value, CPU_SETSIZE - 1, arg);
        else {
            fprintf(stderr, "unknown argument: %s\n", arg);
            return 2;
        }
    }
    if (!opts.redis || !opts.config || !opts.repeats || !opts.port ||
        !opts.timeout_ms || !opts.poll_us) {
        usage(stderr, argv[0]);
        return 2;
    }
    if (access(opts.redis, X_OK) < 0 || access(opts.config, R_OK) < 0) {
        fprintf(stderr, "Redis executable or configuration inaccessible: %s\n", strerror(errno));
        return 2;
    }
    if (set_cpu(opts.observer_cpu) < 0) {
        perror("observer CPU affinity");
        return 2;
    }
    struct sigaction action = {.sa_handler = handle_signal};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, NULL) < 0 || sigaction(SIGTERM, &action, NULL) < 0) {
        perror("sigaction");
        return 2;
    }
    for (unsigned repeat = 1; repeat <= opts.repeats; ++repeat) {
        if (run_sample(&opts, repeat) < 0)
            return interrupted ? 128 + interrupted : 1;
    }
    return 0;
}
