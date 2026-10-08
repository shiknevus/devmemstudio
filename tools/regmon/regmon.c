/* regmon: resident register sampler behind DevmemStudio's monitor (aarch64 Linux, no libc).
 *
 * usage: regmon <period_us> <addr>...
 * Maps each register page of /dev/mem once, then per sample prints "@<CLOCK_MONOTONIC ns>"
 * and one "0x%08X" line per address (the same stream as the devmem shell loop), after a
 * "#regmon 1" banner. Output is batched about every 40 ms. Exits on stdin EOF (the SSH
 * channel closed), a broken stdout or parent death. A full output pipe (network stall)
 * pauses sampling until it drains. Access faults terminate the run with exit code 2. */

typedef unsigned long u64;
typedef unsigned int u32;

enum { SYS_fcntl = 25, SYS_openat = 56, SYS_close = 57, SYS_read = 63, SYS_write = 64, SYS_ppoll = 73,
       SYS_setitimer = 103, SYS_clock_gettime = 113, SYS_rt_sigaction = 134,
       SYS_prctl = 167, SYS_getppid = 173, SYS_mmap = 222 };
enum { EINTR = 4, EAGAIN = 11, POLLIN = 1, POLLOUT = 4, POLLNVAL = 0x20 };

#define MAX_REGS 64
#define NSEC 1000000000UL
#define FLUSH_NS 40000000UL
#define SAMPLE_MAX (22 + 11 * MAX_REGS)

struct ts { long sec, nsec; };
struct pfd { int fd; short events, revents; };
struct out { u64 used; char data[8192]; };
struct action { void (*handler)(int); u64 flags; void (*restorer)(void); u64 mask; };
static volatile u64 active_address, deadline, period_ns, paused;
static long watch_stdin = 1;
static u64 now(void);
static void fault(int sig);
void regmon_sigreturn(void);

static long sys6(long n, long a, long b, long c, long d, long e, long f)
{
    register long x8 __asm__("x8") = n;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    register long x4 __asm__("x4") = e;
    register long x5 __asm__("x5") = f;
    __asm__ volatile ("svc #0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2), "r"(x3), "r"(x4), "r"(x5)
                      : "memory", "cc");
    return x0;
}
#define SYS(n, a, b, c) sys6(n, (long)(a), (long)(b), (long)(c), 0, 0, 0)

__asm__(".text\n.global _start\n.type _start, %function\n_start:\n"
        "\tmov x29, #0\n\tmov x30, #0\n\tmov x0, sp\n\tbl regmon_main\n"
        "\tmov x8, #94\n\tsvc #0\n"
        ".global regmon_sigreturn\nregmon_sigreturn:\n\tmov x8, #139\n\tsvc #0\n");

/* gcc may emit calls to these even when freestanding */
void *memset(void *dst, int c, u64 n)
{
    char *p = dst;
    while (n--) *p++ = (char)c;
    return dst;
}

void *memcpy(void *dst, const void *src, u64 n)
{
    char *p = dst;
    const char *q = src;
    while (n--) *p++ = *q++;
    return dst;
}

static void put(struct out *o, const char *s, u64 n)
{
    while (n--) o->data[o->used++] = *s++;
}

static void put_dec(struct out *o, u64 v)
{
    char s[20];
    int i = 20;
    do s[--i] = (char)('0' + v % 10); while (v /= 10);
    put(o, s + i, 20 - i);
}

static void put_hex32(struct out *o, u32 v)
{
    static const char digits[] = "0123456789ABCDEF";
    char s[11];
    s[0] = '0';
    s[1] = 'x';
    for (int i = 9; i >= 2; i--, v >>= 4) s[i] = digits[v & 15];
    s[10] = '\n';
    put(o, s, 11);
}

static int stdin_closed(void)
{
    char junk[64];
    long n = SYS(SYS_read, 0, junk, sizeof junk);
    return n == 0 || (n < 0 && n != -EINTR && n != -EAGAIN);
}

/* full output pipe: pause until it drains; 1 = channel closed meanwhile, -2 = error */
static int wait_output(void)
{
    struct pfd fds[2] = { { 1, POLLOUT, 0 }, { 0, POLLIN, 0 } };
    int result = 0;
    paused = 1;
    for (;;) {
        long r = sys6(SYS_ppoll, (long)fds, watch_stdin ? 2 : 1, 0, 0, 8, 0);
        if (r == -EINTR) continue;
        if (r < 0) {
            result = -2;
            break;
        }
        if (watch_stdin && fds[1].revents) {
            if (fds[1].revents & POLLNVAL) watch_stdin = 0;
            else if (stdin_closed()) {
                result = 1;
                break;
            }
        }
        if (fds[0].revents) break;   /* writable, or an error the next write reports */
    }
    deadline = now() + period_ns + NSEC;
    paused = 0;
    return result;
}

static int flush(struct out *o)
{
    u64 done = 0;
    while (done < o->used) {
        long n = SYS(SYS_write, 1, o->data + done, o->used - done);
        if (n == -EINTR) continue;
        if (n == -EAGAIN) {
            int r = wait_output();
            if (r) return r == 1 ? -1 : -2;
            continue;
        }
        if (n <= 0) return n == -32 ? -1 : -2;   /* EPIPE is normal cancellation */
        done += (u64)n;
    }
    o->used = 0;
    return 0;
}

static long fail(struct out *o, const char *msg, long err)
{
    u64 n = 0;
    while (msg[n]) n++;
    o->used = 0;
    put(o, "regmon: ", 8);
    put(o, msg, n);
    if (err) {
        put(o, " (errno ", 8);
        put_dec(o, (u64)err);
        put(o, ")", 1);
    }
    put(o, "\n", 1);
    SYS(SYS_write, 2, o->data, o->used);
    return 1;
}

static int parse(const char *s, u64 *value)
{
    u64 base = 10, v = 0;
    int any = 0;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        base = 16;
        s += 2;
    }
    for (; *s; s++) {
        u64 d;
        if (*s >= '0' && *s <= '9') d = (u64)(*s - '0');
        else if (base == 16 && (*s | 32) >= 'a' && (*s | 32) <= 'f') d = (u64)((*s | 32) - 'a' + 10);
        else return 0;
        if (v > (~0UL - d) / base) return 0;
        v = v * base + d;
        any = 1;
    }
    *value = v;
    return any;
}

static u64 now(void)
{
    struct ts t;
    if (SYS(SYS_clock_gettime, 1, &t, 0) < 0) fault(0);
    return (u64)t.sec * NSEC + (u64)t.nsec;
}

static void fault(int sig)
{
    if (sig == 14 && (paused || now() <= deadline)) return;
    if (sig != 13) {
        struct out o;
        o.used = 0;
        const char *msg = sig == 14 ? "regmon: sampling stalled at " :
                          sig == 0 ? "regmon: clock failure at " : "regmon: register access fault at ";
        u64 len = 0;
        while (msg[len]) len++;
        put(&o, msg, len);
        put_hex32(&o, (u32)active_address);
        SYS(SYS_write, 2, o.data, o.used);
    }
    SYS(94, sig == 13 ? 0 : 2, 0, 0);
    for (;;) { }
}

static long protect(void)
{
    for (long fd = 1; fd <= 2; fd++) {
        long flags = SYS(SYS_fcntl, fd, 3, 0);   /* F_GETFL */
        if (flags < 0 || SYS(SYS_fcntl, fd, 4, flags | 04000) < 0) return -1;
    }
    struct action a = { fault, 0x04000000, regmon_sigreturn, 0 };   /* SA_RESTORER */
    const int signals[] = { 7, 11, 13, 14 };   /* BUS, SEGV, PIPE, ALRM */
    for (u64 i = 0; i < sizeof signals / sizeof signals[0]; i++)
        if (sys6(SYS_rt_sigaction, signals[i], (long)&a, 0, 8, 0, 0) < 0) return -1;
    deadline = now() + NSEC;
    long timer[] = { 0, 250000, 0, 250000 };
    return SYS(SYS_setitimer, 0, timer, 0);
}

long regmon_main(long *sp)
{
    long argc = sp[0], count = argc - 2, maps = 0;
    char **argv = (char **)(sp + 1), **env = argv + argc + 1;
    u64 page = 4096, period, addr[MAX_REGS], map_base[MAX_REGS];
    char *map_ptr[MAX_REGS];
    volatile u32 *reg[MAX_REGS];
    struct out o;

    o.used = 0;
    while (*env) env++;
    for (u64 *aux = (u64 *)(env + 1); aux[0]; aux += 2)
        if (aux[0] == 6) page = aux[1];   /* AT_PAGESZ */
    if (count < 1 || count > MAX_REGS || !parse(argv[1], &period) || period < 50 || period > 60000000)
        return fail(&o, "usage: regmon <period_us 50..60000000> <addr>... (1-64 addresses)", 0);
    period *= 1000;
    period_ns = period;
    for (long i = 0; i < count; i++)
        if (!parse(argv[i + 2], &addr[i]) || (addr[i] & 3) || (addr[i] >> 48))
            return fail(&o, "bad register address", 0);

    SYS(SYS_prctl, 1, 9, 0);   /* PR_SET_PDEATHSIG, SIGKILL */
    if (SYS(SYS_getppid, 0, 0, 0) == 1) return 0;
    if (protect() < 0) return fail(&o, "cannot install sampling protection", 0);
    long fd = sys6(SYS_openat, -100, (long)"/dev/mem", 04010000, 0, 0, 0);   /* O_RDONLY | O_SYNC */
    if (fd < 0) return fail(&o, "cannot open /dev/mem", -fd);
    for (long i = 0; i < count; i++) {
        u64 base = addr[i] & ~(page - 1);
        long j = 0;
        while (j < maps && map_base[j] != base) j++;
        if (j == maps) {
            long p = sys6(SYS_mmap, 0, (long)page, 1, 1, fd, (long)base);   /* PROT_READ, MAP_SHARED */
            if ((u64)p > -4096UL) return fail(&o, "cannot map /dev/mem", -p);
            map_base[maps] = base;
            map_ptr[maps++] = (char *)p;
        }
        reg[i] = (volatile u32 *)(map_ptr[j] + (addr[i] - base));
    }
    SYS(SYS_close, fd, 0, 0);

    put(&o, "#regmon 1\n", 10);
    long result = flush(&o);
    if (result) return result == -1 ? 0 : 2;
    struct pfd in = { 0, POLLIN, 0 };
    u64 next = 0, flushed = now();
    for (;;) {
        u64 t = now();
        deadline = t + period + NSEC;
        put(&o, "@", 1);
        put_dec(&o, t);
        put(&o, "\n", 1);
        for (long i = 0; i < count; i++) {
            active_address = addr[i];
            put_hex32(&o, *reg[i]);
        }
        active_address = 0;
        u64 done = now();
        next = next ? next + period : t + period;
        if (next < done) next = t + period;   /* behind: re-anchor, no catch-up burst */
        if (next - flushed >= FLUSH_NS || o.used > sizeof o.data - SAMPLE_MAX) {
            result = flush(&o);
            if (result) return result == -1 ? 0 : 2;
            flushed = done;
        }
        for (;;) {
            u64 cur = now();
            if (cur >= next) break;
            struct ts left = { (long)((next - cur) / NSEC), (long)((next - cur) % NSEC) };
            long r = sys6(SYS_ppoll, (long)&in, watch_stdin, (long)&left, 0, 8, 0);
            if (r < 0 && r != -EINTR) return fail(&o, "sampling wait failed", -r);
            if (r <= 0) continue;
            if (in.revents & POLLNVAL) {
                watch_stdin = 0;   /* no stdin to watch */
                continue;
            }
            if (stdin_closed()) {
                flush(&o);
                return 0;
            }
        }
    }
}
