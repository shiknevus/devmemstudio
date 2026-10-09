"""Run the real regmon-aarch64 binary under the unicorn CPU emulator with faked Linux syscalls.

No board needed: checks the sample stream, deadline pacing, batching and every exit path on a
virtual clock. Needs `pip install unicorn` (kept out of the app's requirements), e.g.
    python -m venv artifacts/emu-venv && artifacts/emu-venv/Scripts/pip install unicorn
    artifacts/emu-venv/Scripts/python tools/regmon/emulate_test.py
"""
from pathlib import Path
import re
import struct
import sys
import unittest

from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_INTR, UC_HOOK_MEM_READ
from unicorn.arm64_const import UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0, UC_ARM64_REG_X8, UC_ARM64_REG_LR

BINARY = Path(__file__).resolve().parents[2] / "assets" / "regmon" / "regmon-aarch64"
STACK_TOP, STACK_SIZE = 0x7FFF0000, 0x40000
MMAP_BASE = 0x10000000
EPIPE, EACCES, ENOMEM = 32, 13, 12
REG_ARGS = [UC_ARM64_REG_X0 + i for i in range(6)]


class Board:
    """Faked kernel: /dev/mem pages, a virtual monotonic clock, a stdin pipe and stdout."""

    def __init__(self, args, page=4096, eof_at=None, read_cost=0, open_error=0, mmap_error=0,
                 ppid=1234, stdin_closed=False, epipe_after=None, max_ns=5 * 10**9,
                 blocked_after=None, unblock_at=None, fault_at=None, stall_at=None, max_write=None):
        self.uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        self.args, self.page = args, page
        self.clock = 10**12
        self.eof_at, self.read_cost = eof_at, read_cost
        self.open_error, self.mmap_error, self.ppid = open_error, mmap_error, ppid
        self.stdin_closed, self.epipe_after, self.max_ns = stdin_closed, epipe_after, max_ns
        self.stdout, self.stderr, self.writes = b"", b"", []
        self.maps = {}   # virtual base -> physical base
        self.reads = {}  # physical address -> count
        self.exit_code = None
        self.syscalls = []
        self.handlers, self.fd_flags = {}, {}
        self.pending_signal = None
        self.blocked_after, self.fault_at, self.stall_at = blocked_after, fault_at, stall_at
        self.unblock_at = unblock_at   # stdout drains again at this clock (None: never)
        self.max_write = max_write
        self.next_alarm, self.signal_context, self.resume_signal = None, None, False
        self.restorers, self.signal_returns = {}, 0
        self._load()

    def _load(self):
        data = BINARY.read_bytes()
        assert data[:4] == b"\x7fELF" and data[4] == 2 and struct.unpack_from("<H", data, 18)[0] == 183
        entry, phoff = struct.unpack_from("<QQ", data, 24)
        phentsize, phnum = struct.unpack_from("<HH", data, 54)
        for i in range(phnum):
            kind, flags, offset, vaddr, _, filesz, memsz, _ = struct.unpack_from("<IIQQQQQQ", data, phoff + i * phentsize)
            if kind == 1:
                base = vaddr & ~0xFFF
                size = (vaddr + memsz - base + 0xFFF) & ~0xFFF
                self.uc.mem_map(base, size)
                self.uc.mem_write(vaddr, data[offset:offset + filesz])
        self.entry = entry
        self.uc.mem_map(STACK_TOP - STACK_SIZE, STACK_SIZE)
        strings, pointers = b"", []
        area = STACK_TOP - 0x1000
        for text in ["regmon", *self.args]:
            pointers.append(area + len(strings))
            strings += text.encode() + b"\0"
        self.uc.mem_write(area, strings)
        env = [area + len(strings)]
        self.uc.mem_write(env[0], b"PATH=/bin\0")
        words = [len(pointers), *pointers, 0, *env, 0, 6, self.page, 0, 0]
        sp = (area - 8 * len(words) - 64) & ~15
        self.uc.mem_write(sp, struct.pack(f"<{len(words)}Q", *words))
        self.uc.reg_write(UC_ARM64_REG_SP, sp)
        self.uc.hook_add(UC_HOOK_INTR, self._syscall)

    def _device_read(self, uc, access, address, size, value, data):
        base = address & ~(self.page - 1)
        physical = self.maps[base] + (address - base)
        if physical == self.fault_at or physical == self.stall_at:
            self.pending_signal = 7 if physical == self.fault_at else 14
            if physical == self.stall_at:
                self.clock += 2 * 10**9
            uc.emu_stop()
            return
        count = self.reads.get(physical, 0) + 1
        self.reads[physical] = count
        self.clock += self.read_cost
        uc.mem_write(address, struct.pack("<I", ((physical & 0xFFFF) << 16 | count) & 0xFFFFFFFF))

    def _syscall(self, uc, intno, data):
        number = uc.reg_read(UC_ARM64_REG_X8)
        if number == 139:    # rt_sigreturn: resume the interrupted sampler context
            uc.context_restore(self.signal_context)
            self.signal_context = None
            self.signal_returns += 1
            self.resume_signal = True
            uc.emu_stop()
            return
        a = [uc.reg_read(r) for r in REG_ARGS]
        self.syscalls.append(number)
        result = self._dispatch(uc, number, a)
        uc.reg_write(UC_ARM64_REG_X0, result & 0xFFFFFFFFFFFFFFFF)
        if self.exit_code is not None or self.clock > 10**12 + self.max_ns:
            uc.emu_stop()
        elif self.next_alarm is not None and self.clock >= self.next_alarm and self.signal_context is None:
            while self.next_alarm <= self.clock:
                self.next_alarm += 250_000_000
            self.pending_signal = 14
            uc.emu_stop()

    def _dispatch(self, uc, number, a):
        self.clock += 1000   # 1 µs per syscall
        if number == 94:     # exit_group
            self.exit_code = a[0]
            return 0
        if number == 25:     # fcntl: both output streams must be nonblocking
            if a[1] == 3:
                return self.fd_flags.get(a[0], 0)
            assert a[1] == 4 and a[0] in (1, 2) and a[2] & 0o4000
            self.fd_flags[a[0]] = a[2]
            return 0
        if number == 134:    # rt_sigaction
            handler, flags, restorer, mask = struct.unpack('<QQQQ', bytes(uc.mem_read(a[1], 32)))
            assert a[0] in (7, 11, 13, 14) and a[3] == 8 and flags == 0x04000000 and restorer
            self.handlers[a[0]] = handler
            self.restorers[a[0]] = restorer
            return 0
        if number == 103:    # periodic watchdog
            assert a[0] == 0
            assert struct.unpack('<qqqq', bytes(uc.mem_read(a[1], 32))) == (0, 250000, 0, 250000)
            self.next_alarm = self.clock + 250_000_000
            return 0
        if number == 113:    # clock_gettime
            assert a[0] == 1, "sampler must use CLOCK_MONOTONIC"
            uc.mem_write(a[1], struct.pack("<qq", self.clock // 10**9, self.clock % 10**9))
            return 0
        if number == 167:    # prctl
            assert (a[0], a[1]) == (1, 9), "expected PR_SET_PDEATHSIG SIGKILL"
            return 0
        if number == 173:
            return self.ppid
        if number == 56:     # openat
            path = bytes(uc.mem_read(a[1], 16)).split(b"\0")[0]
            assert path == b"/dev/mem" and a[2] == 0o4010000, (path, oct(a[2]))
            return -self.open_error if self.open_error else 3
        if number == 57:
            return 0
        if number == 222:    # mmap(addr, len, prot, flags, fd, offset)
            assert a[1] == self.page and a[2] == 1 and a[3] == 1 and a[4] == 3 and a[5] % self.page == 0, a
            if self.mmap_error:
                return -self.mmap_error
            base = MMAP_BASE + len(self.maps) * max(self.page, 0x10000)
            self.uc.mem_map(base, max(self.page, 0x1000))
            self.uc.hook_add(UC_HOOK_MEM_READ, self._device_read, begin=base, end=base + self.page - 1)
            self.maps[base] = a[5]
            return base
        if number == 64:     # write
            chunk = bytes(uc.mem_read(a[1], a[2]))
            if a[0] == 1:
                if self._stdout_blocked():
                    return -11
                if self.epipe_after is not None and len(self.writes) >= self.epipe_after:
                    return -EPIPE
                if self.max_write:
                    chunk = chunk[:self.max_write]
                self.stdout += chunk
                self.writes.append((self.clock, chunk))
            else:
                self.stderr += chunk
            return len(chunk)
        if number == 73:     # ppoll(fds, nfds, timeout, sigmask, sigsetsize)
            assert a[3] == 0
            timeout = None
            if a[2]:
                sec, nsec = struct.unpack("<qq", bytes(uc.mem_read(a[2], 16)))
                timeout = sec * 10**9 + nsec
            fds = [struct.unpack("<ih", bytes(uc.mem_read(a[0] + 8 * i, 6))) for i in range(a[1])]
            assert all(fd_events in ((0, 1), (1, 4)) for fd_events in fds), fds
            ready = {}   # pollfd index -> (clock when ready, revents)
            for index, (fd, _) in enumerate(fds):
                if fd == 0 and self.stdin_closed:
                    ready[index] = (self.clock, 0x20)
                elif fd == 0 and self.eof_at is not None:
                    ready[index] = (max(self.clock, self.eof_at), 0x11)
                elif fd == 1 and not self._stdout_blocked():
                    ready[index] = (self.clock, 4)
                elif fd == 1 and self.unblock_at is not None:
                    ready[index] = (self.unblock_at, 4)
            limit = None if timeout is None else self.clock + timeout
            when = min((at for at, _ in ready.values()), default=None)
            if when is not None and (limit is None or when <= limit) and (self.next_alarm is None or when <= self.next_alarm):
                self.clock = max(self.clock, when)
                for index in range(len(fds)):
                    at, revents = ready.get(index, (None, 0))
                    uc.mem_write(a[0] + 8 * index + 6, struct.pack("<h", revents if at is not None and at <= self.clock else 0))
                return sum(at <= self.clock for at, _ in ready.values())
            if self.next_alarm is not None and (limit is None or limit >= self.next_alarm):
                self.clock = self.next_alarm
                return -4   # timer signal interrupts a sleeping ppoll
            assert limit is not None, "ppoll would block forever"
            self.clock = limit
            return 0
        if number == 63:     # read(0): the channel closed
            return 0
        raise AssertionError(f"unexpected syscall {number} {a}")

    def _stdout_blocked(self):
        return (self.blocked_after is not None and len(self.writes) >= self.blocked_after
                and (self.unblock_at is None or self.clock < self.unblock_at))

    def run(self):
        entry = self.entry
        while True:
            self.uc.emu_start(entry, 0)
            if self.pending_signal is not None:
                signal = self.pending_signal
                self.pending_signal = None
                self.signal_context = self.uc.context_save()
                self.uc.reg_write(UC_ARM64_REG_SP, self.uc.reg_read(UC_ARM64_REG_SP) - 8192)
                self.uc.reg_write(UC_ARM64_REG_X0, signal)
                self.uc.reg_write(UC_ARM64_REG_LR, self.restorers[signal])
                entry = self.handlers[signal]
            elif self.resume_signal:
                self.resume_signal = False
                entry = self.uc.reg_read(UC_ARM64_REG_PC)
            else:
                break
        return self

    def samples(self):
        lines = self.stdout.decode().splitlines()
        assert lines[0] == "#regmon 1", lines[:2]
        samples, stamp = [], None
        for line in lines[1:]:
            if line.startswith("@"):
                stamp, values = int(line[1:]), []
                samples.append((stamp, values))
            else:
                assert re.fullmatch(r"0x[0-9A-F]{8}", line), line
                values.append(int(line, 16))
        return samples


class RegmonEmulation(unittest.TestCase):
    def test_32_registers_keep_complete_samples_and_deadlines(self):
        addresses = [0xB0100800 + i * 4 for i in range(32)]
        board = Board(["5000", *map(hex, addresses)], eof_at=10**12 + 10**9).run()
        self.assertEqual((board.exit_code, board.stderr), (0, b""))
        samples = board.samples()
        self.assertTrue(195 <= len(samples) <= 201, len(samples))
        self.assertEqual(len(board.maps), 1)
        for index, (_, values) in enumerate(samples, 1):
            self.assertEqual(values, [(address & 0xFFFF) << 16 | index for address in addresses])
        gaps = [b[0] - a[0] for a, b in zip(samples, samples[1:])]
        self.assertTrue(all(4_990_000 <= gap <= 5_020_000 for gap in gaps))

    def test_watchdog_returns_normally_during_healthy_long_interval_sampling(self):
        board = Board(['60000000', '0xB0119E08'], eof_at=10**12 + 10**9).run()
        self.assertEqual(board.exit_code, 0)
        self.assertEqual(board.stderr, b'')
        self.assertGreaterEqual(board.signal_returns, 3)
        self.assertEqual(len(board.samples()), 1)
    def test_bus_fault_reports_address_and_stops_without_retry(self):
        address = 0xB0119E08
        board = Board(['5000', hex(address)], fault_at=address).run()
        self.assertEqual(board.exit_code, 2)
        self.assertIn(b'register access fault at 0xB0119E08', board.stderr)
        self.assertEqual(board.stdout, b'#regmon 1\n')

    def test_interruptible_stall_triggers_watchdog(self):
        board = Board(['5000', '0xB0119E08'], stall_at=0xB0119E08).run()
        self.assertEqual(board.exit_code, 2)
        self.assertIn(b'sampling stalled at 0xB0119E08', board.stderr)

    def test_output_stall_pauses_sampling_and_resumes_after_drain(self):
        board = Board(['5000', '0xB0119E08'], blocked_after=2, unblock_at=10**12 + 3 * 10**9,
                      eof_at=10**12 + 4 * 10**9).run()
        self.assertEqual((board.exit_code, board.stderr), (0, b''))   # the watchdog stays quiet
        self.assertGreaterEqual(board.signal_returns, 10)
        stamps = [stamp for stamp, _ in board.samples()]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        pause = max(range(len(gaps)), key=gaps.__getitem__)
        self.assertGreater(gaps[pause], 2_800_000_000)                 # no sampling while paused
        self.assertLess(stamps[pause], 10**12 + 2 * 10**8)
        self.assertGreaterEqual(stamps[pause + 1], 10**12 + 3 * 10**9)
        self.assertGreaterEqual(min(gaps[pause + 1:]), 4_990_000)       # resumes without a burst
        self.assertGreater(len(stamps) - pause, 150)
        self.assertEqual(board.reads[0xB0119E08], len(stamps))

    def test_channel_close_during_output_stall_exits_cleanly(self):
        board = Board(['5000', '0xB0119E08'], blocked_after=2, eof_at=10**12 + 2 * 10**9).run()
        self.assertEqual((board.exit_code, board.stderr), (0, b''))
        self.assertGreaterEqual(board.clock, 10**12 + 2 * 10**9)

    def test_partial_writes_preserve_complete_sample_stream(self):
        board = Board(['5000', '0xB0119E08'], max_write=3, eof_at=10**12 + 10**8).run()
        self.assertEqual(board.exit_code, 0)
        self.assertTrue(board.samples())
        self.assertTrue(all(len(values) == 1 for _, values in board.samples()))

    def test_samples_on_deadlines_batches_and_exits_on_channel_close(self):
        addresses = [0xB0119E08, 0xB0119FC0, 0xB011A1C0]
        board = Board(["5000", *map(hex, addresses)], eof_at=10**12 + 10**9).run()
        self.assertEqual(board.exit_code, 0)
        self.assertEqual(board.stderr, b"")
        self.assertEqual(sorted(board.maps.values()), [0xB0119000, 0xB011A000])   # one map per page
        samples = board.samples()
        self.assertTrue(195 <= len(samples) <= 201, len(samples))
        # deadline pacing: a fixed wake-up latency (3 faked syscalls) and no drift
        jitter = [stamp - samples[0][0] - 5_000_000 * index for index, (stamp, _) in enumerate(samples[1:], 1)]
        self.assertGreaterEqual(min(jitter), 0)
        self.assertLessEqual(max(jitter), 20_000)
        for index, (_, values) in enumerate(samples, 1):
            self.assertEqual(values, [(a & 0xFFFF) << 16 | index for a in addresses])
        self.assertTrue(24 <= len(board.writes) <= 30, len(board.writes))      # ~40 ms batches
        gaps = [b[0] - a[0] for a, b in zip(board.writes[1:], board.writes[2:])]
        self.assertLessEqual(max(gaps), 41_000_000)
        self.assertTrue(board.stdout.endswith(b"\n"))

    def test_slow_period_flushes_every_sample(self):
        board = Board(["100000", "0xFFCA0044"], eof_at=10**12 + 10**9).run()
        self.assertEqual(len(board.samples()), 10)
        self.assertEqual(len(board.writes), 11)   # banner + one write per sample

    def test_lagging_reads_hold_the_actual_rate_without_catch_up_bursts(self):
        board = Board(["5000", "0xB0119E08", "0xB0119E0C"], eof_at=10**12 + 10**9, read_cost=3_000_000).run()
        samples = board.samples()
        periods = [b[0] - a[0] for a, b in zip(samples, samples[1:])]
        self.assertGreaterEqual(min(periods), 6_000_000)
        self.assertLess(max(periods), 6_100_000)

    def test_page_size_comes_from_auxv(self):
        board = Board(["1000", "0xB0119E08", "0xB0128000"], page=65536, eof_at=10**12 + 10**7).run()
        self.assertEqual(sorted(board.maps.values()), [0xB0110000, 0xB0120000])
        self.assertEqual(board.exit_code, 0)

    def test_broken_stdout_ends_the_sampler(self):
        board = Board(["5000", "0xB0119E08"], epipe_after=3).run()
        self.assertEqual(board.exit_code, 0)
        self.assertEqual(len(board.writes), 3)

    def test_closed_stdin_is_ignored_and_sampling_continues(self):
        board = Board(["5000", "0xB0119E08"], stdin_closed=True, epipe_after=5).run()
        self.assertEqual(board.exit_code, 0)
        self.assertGreater(len(board.samples()), 20)
        self.assertNotIn(73, board.syscalls[-1:])

    def test_orphan_exits_before_touching_dev_mem(self):
        board = Board(["5000", "0xB0119E08"], ppid=1).run()
        self.assertEqual((board.exit_code, board.stdout, board.maps), (0, b"", {}))

    def test_startup_errors_exit_nonzero_without_banner(self):
        cases = {
            "usage": (["5000"], {}),
            "usage ": (["10", "0xB0119E08"], {}),
            "bad register address": (["5000", "0xB0119E0A"], {}),
            "bad register address ": (["5000", "zz"], {}),
            "cannot open /dev/mem (errno 13)": (["5000", "0xB0119E08"], {"open_error": EACCES}),
            "cannot map /dev/mem (errno 12)": (["5000", "0xB0119E08"], {"mmap_error": ENOMEM}),
        }
        for expected, (args, options) in cases.items():
            with self.subTest(expected):
                board = Board(args, **options).run()
                self.assertEqual(board.exit_code, 1)
                self.assertEqual(board.stdout, b"")
                self.assertIn(expected.strip(), board.stderr.decode())


if __name__ == "__main__":
    unittest.main(verbosity=2, argv=sys.argv[:1])
