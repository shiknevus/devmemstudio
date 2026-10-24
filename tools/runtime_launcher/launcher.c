/* DevmemStudio single-file launcher: unpacks the embedded runtime once per version, then runs it. */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define _WIN32_WINNT 0x0602
#include <windows.h>
#include <compressapi.h>
#include <bcrypt.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <wctype.h>
#include "runtime_manifest.h"

#define RES_PAYLOAD 501
#define PATH_CAP 32768
#define SWEEP_AGE 6000000000ULL   /* 10 min in 100 ns: never touch a fresh extraction */
#define STAMP_SLACK 20000000ULL   /* 2 s: FAT time resolution */

static HANDLE locks[RUNTIME_FILE_COUNT];

static wchar_t *join(const wchar_t *left, const wchar_t *right)
{
    size_t a = wcslen(left), b = wcslen(right);
    wchar_t *path = a + b + 2 <= PATH_CAP ? malloc((a + b + 2) * sizeof(wchar_t)) : NULL;
    if (path) {
        memcpy(path, left, a * sizeof(wchar_t));
        path[a] = L'\\';
        memcpy(path + a + 1, right, (b + 1) * sizeof(wchar_t));
    }
    return path;
}

static wchar_t *sibling(const wchar_t *base, const wchar_t *name, const wchar_t *tag)
{
    static unsigned serial;
    wchar_t leaf[160];
    swprintf(leaf, 160, L"%ls.%ls-%lu-%llu-%u", name, tag, GetCurrentProcessId(),
             (unsigned long long)GetTickCount64(), ++serial);
    return join(base, leaf);
}

static int plain_directory(const wchar_t *path)
{
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_DIRECTORY)
        && !(attributes & FILE_ATTRIBUTE_REPARSE_POINT);
}

static ULONGLONG ticks(const FILETIME *time)
{
    return ((ULONGLONG)time->dwHighDateTime << 32) | time->dwLowDateTime;
}

static void unlock_runtime(void)
{
    for (size_t i = 0; i < RUNTIME_FILE_COUNT; ++i)
        if (locks[i]) {
            CloseHandle(locks[i]);
            locks[i] = NULL;
        }
}

/* Every file present with its exact size and stamp; read-only shares stay open while the app runs. */
static int lock_runtime(const wchar_t *root)
{
    if (!plain_directory(root))
        return 0;
    for (size_t i = 0; i < RUNTIME_FILE_COUNT; ++i) {
        wchar_t *path = join(root, runtime_files[i].path);
        HANDLE file = path ? CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING,
                                         FILE_FLAG_OPEN_REPARSE_POINT, NULL) : INVALID_HANDLE_VALUE;
        BY_HANDLE_FILE_INFORMATION info;
        free(path);
        if (file == INVALID_HANDLE_VALUE || !GetFileInformationByHandle(file, &info)
            || (info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))
            || info.nFileSizeHigh || info.nFileSizeLow != runtime_files[i].size
            || ticks(&info.ftLastWriteTime) + STAMP_SLACK < RUNTIME_MTIME
            || ticks(&info.ftLastWriteTime) > RUNTIME_MTIME + STAMP_SLACK) {
            if (file != INVALID_HANDLE_VALUE)
                CloseHandle(file);
            unlock_runtime();
            return 0;
        }
        locks[i] = file;
    }
    return 1;
}

static void make_tree(wchar_t *path)
{
    for (wchar_t *cursor = path; *cursor; ++cursor)
        if (*cursor == L'\\' && cursor > path && cursor[-1] != L':' && cursor[-1] != L'\\') {
            *cursor = 0;
            CreateDirectoryW(path, NULL);   /* existing or unreachable levels are fine; the caller checks the result */
            *cursor = L'\\';
        }
    CreateDirectoryW(path, NULL);
}

static int make_parents(const wchar_t *root, const wchar_t *relative)
{
    wchar_t *path = join(root, relative);
    if (!path)
        return 0;
    for (wchar_t *cursor = path + wcslen(root) + 1; *cursor; ++cursor) {
        if (*cursor != L'\\')
            continue;
        *cursor = 0;
        int ok = CreateDirectoryW(path, NULL) || GetLastError() == ERROR_ALREADY_EXISTS;
        *cursor = L'\\';
        if (!ok) {
            free(path);
            return 0;
        }
    }
    free(path);
    return 1;
}

static int sha256(const unsigned char *data, size_t size, unsigned char digest[32])
{
    BCRYPT_ALG_HANDLE algorithm = NULL;
    BCRYPT_HASH_HANDLE hash = NULL;
    int ok = BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, NULL, 0))
          && BCRYPT_SUCCESS(BCryptCreateHash(algorithm, &hash, NULL, 0, NULL, 0, 0));
    while (ok && size) {
        ULONG chunk = size > 0x40000000 ? 0x40000000 : (ULONG)size;
        ok = BCRYPT_SUCCESS(BCryptHashData(hash, (PUCHAR)data, chunk, 0));
        data += chunk;
        size -= chunk;
    }
    ok = ok && BCRYPT_SUCCESS(BCryptFinishHash(hash, digest, 32, 0));
    if (hash)
        BCryptDestroyHash(hash);
    if (algorithm)
        BCryptCloseAlgorithmProvider(algorithm, 0);
    return ok;
}

static int write_file(const wchar_t *path, const unsigned char *data, DWORD size)
{
    HANDLE file = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE)
        return 0;
    int ok = 1;
    while (ok && size) {
        DWORD done = 0;
        ok = WriteFile(file, data, size > (1u << 24) ? (1u << 24) : size, &done, NULL) && done;
        data += done;
        size -= done;
    }
    FILETIME stamp = {(DWORD)RUNTIME_MTIME, (DWORD)(RUNTIME_MTIME >> 32)};
    ok = ok && SetFileTime(file, NULL, NULL, &stamp);   /* the stamp is what later launches check */
    return CloseHandle(file) && ok;
}

/* Start-up stages in order: the launcher's, then the app's (QT on), which it reports through the Progress block. */
enum { STAGE_CHECK, STAGE_WAIT, STAGE_INFLATE, STAGE_WRITE, STAGE_START, STAGE_QT, STAGE_MODULES, STAGE_UI,
       STAGE_WINDOW, STAGE_PROJECT, STAGE_COUNT };
#define INFLATE_SHARE 450   /* permille of the unpack */
#define WRITE_SHARE 550

typedef struct {   /* shared with devmem_studio/launcher_splash.py */
    volatile LONG stage;
    volatile LONGLONG at[STAGE_COUNT];   /* QueryPerformanceCounter as each app stage began */
} Progress;

static Progress *progress;
static HANDLE splash_ready, splash_gone;
static volatile LONG launch_stage, launch_round, unpacking, unpack_permille;
static volatile double stage_at[STAGE_COUNT];   /* clock_ms as each launcher stage began; 0: not reached */

static double qpc_ms(LONGLONG count)
{
    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    return (double)count * 1000.0 / (double)frequency.QuadPart;
}

static double clock_ms(void)   /* GetTickCount64 steps by 15.6 ms: too coarse for animation */
{
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    return qpc_ms(now.QuadPart);
}

static void set_stage(LONG stage)
{
    if (stage == STAGE_INFLATE) {
        InterlockedExchange(&unpack_permille, 0);
        InterlockedExchange(&unpacking, 1);
    }
    if (!stage_at[stage])
        stage_at[stage] = clock_ms();
    InterlockedExchange(&launch_stage, stage);
}

/* A start (again, after Retry): the bar and the hand-over begin anew. */
static void begin_launch(void)
{
    for (int i = 0; i < STAGE_COUNT; ++i) {
        stage_at[i] = 0;
        if (progress)
            progress->at[i] = 0;
    }
    if (progress)
        progress->stage = 0;
    InterlockedExchange(&unpacking, 0);
    if (splash_ready)
        ResetEvent(splash_ready);
    if (splash_gone)
        ResetEvent(splash_gone);
    InterlockedIncrement(&launch_round);
    set_stage(STAGE_CHECK);
}

static void unpack_advance(DWORD bytes, LONG share)
{
    InterlockedExchangeAdd(&unpack_permille, (LONG)((ULONGLONG)bytes * share / RUNTIME_RAW_SIZE));
}

typedef struct {
    unsigned char *packed;
    unsigned char *raw;
    DWORD packed_size, raw_size;
    const unsigned char *sha256;
    int ok;
} Block;

/* Unpacks and verifies one block; the blocks run in parallel. */
static DWORD WINAPI inflate(LPVOID context)
{
    Block *block = context;
    DECOMPRESSOR_HANDLE decompressor = NULL;
    SIZE_T size = 0;
    unsigned char digest[32];
    block->ok = CreateDecompressor(COMPRESS_ALGORITHM_LZMS, NULL, &decompressor)
             && Decompress(decompressor, block->packed, block->packed_size, block->raw, block->raw_size, &size)
             && size == block->raw_size
             && sha256(block->raw, block->raw_size, digest) && !memcmp(digest, block->sha256, 32);
    if (decompressor)
        CloseDecompressor(decompressor);
    if (block->ok)
        unpack_advance(block->raw_size, INFLATE_SHARE);
    return 0;
}

#define WRITERS 4

typedef struct {
    const wchar_t *staging;
    const unsigned char *raw;
    const size_t *offsets;
    volatile LONG next, failed;
} Writer;

/* Writers take the next file in turn: per-file create/close costs (and on-close scans) overlap. */
static DWORD WINAPI write_files(LPVOID context)
{
    Writer *job = context;
    for (LONG i; !job->failed && (i = InterlockedIncrement(&job->next) - 1) < RUNTIME_FILE_COUNT;) {
        wchar_t *path = join(job->staging, runtime_files[i].path);
        if (!path || !make_parents(job->staging, runtime_files[i].path)
            || !write_file(path, job->raw + job->offsets[i], runtime_files[i].size))
            InterlockedExchange(&job->failed, 1);
        else
            unpack_advance(runtime_files[i].size, WRITE_SHARE);
        free(path);
    }
    return 0;
}

static int extract(HINSTANCE instance, const wchar_t *staging)
{
    HRSRC resource = FindResourceW(instance, MAKEINTRESOURCEW(RES_PAYLOAD), RT_RCDATA);
    HGLOBAL loaded = resource ? LoadResource(instance, resource) : NULL;
    unsigned char *packed = loaded ? LockResource(loaded) : NULL;
    DWORD packed_size = resource ? SizeofResource(instance, resource) : 0;
    if (!packed || !packed_size || !CreateDirectoryW(staging, NULL))
        return 0;
    unsigned char *raw = VirtualAlloc(NULL, RUNTIME_RAW_SIZE, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!raw)
        return 0;
    Block blocks[RUNTIME_BLOCK_COUNT];
    HANDLE threads[RUNTIME_BLOCK_COUNT] = {0};
    size_t packed_at = 0, raw_at = 0;
    for (size_t i = 0; i < RUNTIME_BLOCK_COUNT; ++i) {
        blocks[i] = (Block){packed + packed_at, raw + raw_at, runtime_blocks[i].packed, runtime_blocks[i].raw,
                            runtime_blocks[i].sha256, 0};
        packed_at += runtime_blocks[i].packed;
        raw_at += runtime_blocks[i].raw;
    }
    int ok = packed_at == packed_size && raw_at == RUNTIME_RAW_SIZE;
    for (size_t i = 1; ok && i < RUNTIME_BLOCK_COUNT; ++i)
        threads[i] = CreateThread(NULL, 0, inflate, &blocks[i], 0, NULL);   /* NULL: inflated below instead */
    for (size_t i = 0; ok && i < RUNTIME_BLOCK_COUNT; ++i) {
        if (threads[i]) {
            WaitForSingleObject(threads[i], INFINITE);
            CloseHandle(threads[i]);
            threads[i] = NULL;
        } else {
            inflate(&blocks[i]);
        }
        ok = blocks[i].ok;
    }
    for (size_t i = 0; i < RUNTIME_BLOCK_COUNT; ++i)   /* after a failure, never free memory a thread still writes */
        if (threads[i]) {
            WaitForSingleObject(threads[i], INFINITE);
            CloseHandle(threads[i]);
        }
    static size_t offsets[RUNTIME_FILE_COUNT];
    size_t offset = 0;
    for (size_t i = 0; i < RUNTIME_FILE_COUNT; ++i) {
        offsets[i] = offset;
        offset += runtime_files[i].size;
    }
    ok = ok && offset == RUNTIME_RAW_SIZE;
    if (ok) {
        set_stage(STAGE_WRITE);
        Writer job = {staging, raw, offsets, 0, 0};
        HANDLE writers[WRITERS] = {0};
        for (size_t i = 0; i < WRITERS; ++i)
            writers[i] = CreateThread(NULL, 0, write_files, &job, 0, NULL);   /* NULL: the others (or this thread) carry on */
        write_files(&job);
        for (size_t i = 0; i < WRITERS; ++i)
            if (writers[i]) {
                WaitForSingleObject(writers[i], INFINITE);
                CloseHandle(writers[i]);
            }
        ok = !job.failed;
    }
    if (ok)
        InterlockedExchange(&unpack_permille, 1000);
    VirtualFree(raw, 0, MEM_RELEASE);
    return ok;
}

static void remove_tree(const wchar_t *path)
{
    wchar_t *pattern = join(path, L"*");
    WIN32_FIND_DATAW entry;
    HANDLE search = pattern ? FindFirstFileExW(pattern, FindExInfoBasic, &entry, FindExSearchNameMatch, NULL, 0)
                            : INVALID_HANDLE_VALUE;
    free(pattern);
    if (search != INVALID_HANDLE_VALUE) {
        do {
            if (!wcscmp(entry.cFileName, L".") || !wcscmp(entry.cFileName, L".."))
                continue;
            wchar_t *child = join(path, entry.cFileName);
            if (!child)
                continue;
            if ((entry.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) && !(entry.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT))
                remove_tree(child);
            else if (entry.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
                RemoveDirectoryW(child);   /* a link: remove the link, never its target */
            else
                DeleteFileW(child);
            free(child);
        } while (FindNextFileW(search, &entry));
        FindClose(search);
    }
    RemoveDirectoryW(path);
}

#define MOVE_PATIENCE 5000ULL   /* ms */

/* No directory renames while a file inside is open: on-access scans hold fresh files for a moment. */
static int move_directory(const wchar_t *from, const wchar_t *to)
{
    ULONGLONG until = GetTickCount64() + MOVE_PATIENCE;
    while (!MoveFileExW(from, to, 0)) {
        DWORD error = GetLastError();
        if ((error != ERROR_ACCESS_DENIED && error != ERROR_SHARING_VIOLATION) || GetTickCount64() >= until)
            return 0;
        Sleep(25);
    }
    return 1;
}

/* Old versions and leftovers: a runtime still in use holds open files, so its rename fails and it stays. */
static void sweep(const wchar_t *base, const wchar_t *keep)
{
    wchar_t *pattern = join(base, L"*");
    WIN32_FIND_DATAW entry;
    HANDLE search = pattern ? FindFirstFileExW(pattern, FindExInfoBasic, &entry, FindExSearchNameMatch, NULL, 0)
                            : INVALID_HANDLE_VALUE;
    free(pattern);
    if (search == INVALID_HANDLE_VALUE)
        return;
    FILETIME now_time;
    GetSystemTimeAsFileTime(&now_time);
    ULONGLONG now = ticks(&now_time);
    do {
        if (!(entry.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || (entry.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)
            || !wcscmp(entry.cFileName, L".") || !wcscmp(entry.cFileName, L"..") || !_wcsicmp(entry.cFileName, keep)
            || ticks(&entry.ftCreationTime) + SWEEP_AGE > now)
            continue;
        wchar_t *old = join(base, entry.cFileName), *doomed = sibling(base, L"sweep", L"del");
        if (old && doomed && MoveFileExW(old, doomed, 0))
            remove_tree(doomed);
        free(old);
        free(doomed);
    } while (FindNextFileW(search, &entry));
    FindClose(search);
}

static wchar_t *environment(const wchar_t *name)
{
    DWORD size = GetEnvironmentVariableW(name, NULL, 0);
    wchar_t *value = size && size < PATH_CAP ? malloc(size * sizeof(wchar_t)) : NULL;
    if (value && GetEnvironmentVariableW(name, value, size) + 1 != size) {
        free(value);
        value = NULL;
    }
    return value;
}

/* Killing the launcher kills the app; the app's own children (updater, browser) break away silently. */
static HANDLE app_job(void)
{
    HANDLE job = CreateJobObjectW(NULL, NULL);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    ZeroMemory(&limits, sizeof limits);
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
                                            | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK;
    if (job && !SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof limits)) {
        CloseHandle(job);
        job = NULL;
    }
    return job;
}

#ifndef PROC_THREAD_ATTRIBUTE_JOB_LIST
#define PROC_THREAD_ATTRIBUTE_JOB_LIST 0x0002000D   /* Windows 10 */
#endif

/* Born in the job: a launcher killed inside CreateProcess must not leave the suspended app behind. */
static LPPROC_THREAD_ATTRIBUTE_LIST job_attribute(HANDLE *job)
{
    SIZE_T size = 0;
    InitializeProcThreadAttributeList(NULL, 1, 0, &size);
    LPPROC_THREAD_ATTRIBUTE_LIST list = *job && size ? malloc(size) : NULL;
    if (list && !InitializeProcThreadAttributeList(list, 1, 0, &size)) {
        free(list);
        return NULL;
    }
    if (list && !UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST, job, sizeof *job, NULL, NULL)) {
        DeleteProcThreadAttributeList(list);
        free(list);
        return NULL;
    }
    return list;
}

/* Splash, at every start: one bar runs through all stages, timed by the starts before; full, the splash closes and only
   then the main window shows. Starting, it is owned: updaters take an unowned window for an error. Failures show here
   with Retry/Cancel. */
#define SPLASH_TIMEOUT 60000ULL
#define SPLASH_FRAME (1000.0 / 60)   /* ms */
#define SPLASH_EASE 40.0   /* ms: time constant of the bar toward the progress */
#define SPLASH_SPEED 2.0   /* permille per ms at most: no jumps */
#define SPLASH_FINISH 60.0   /* ms to fill the rest once the app is ready */
#define SPLASH_HOLD 50.0   /* ms full before it closes */
#define SPLASH_WIDTH 420   /* 96-dpi pixels */
#define SPLASH_HEIGHT 112
#define SPLASH_PAD 26
#define SPLASH_GLYPH 40
#define SPLASH_TEXT (SPLASH_PAD + SPLASH_GLYPH + 18)
#define SPLASH_CLASS L"DevmemStudioLauncherSplash"

#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif

static const wchar_t *const stage_text[STAGE_COUNT] = {
    L"正在检查运行环境…",
    L"正在等待另一个 DevmemStudio 准备运行环境…",
    L"首次运行，正在解压运行环境…",
    L"首次运行，正在写入运行环境…",
    L"正在启动 Python 运行时…",
    L"正在加载 Qt 界面库…",
    L"正在加载程序模块…",
    L"正在初始化界面样式…",
    L"正在创建主窗口…",
    L"正在载入上次导入的 top 文件…",
};

/* ms per stage, averaged over the starts before: warm, and cold for starts that unpack (inflate and write count as one) */
static double expected[2][STAGE_COUNT] = {
    {[STAGE_CHECK] = 6, [STAGE_START] = 150, [STAGE_QT] = 25, [STAGE_MODULES] = 55, [STAGE_UI] = 20, [STAGE_WINDOW] = 490,
     [STAGE_PROJECT] = 60},
    {[STAGE_CHECK] = 6, [STAGE_INFLATE] = 850, [STAGE_START] = 340, [STAGE_QT] = 40, [STAGE_MODULES] = 80, [STAGE_UI] = 30,
     [STAGE_WINDOW] = 1800, [STAGE_PROJECT] = 100},
};

typedef struct {
    char magic[4];
    UINT32 version;
    double ms[2][STAGE_COUNT];
} Timings;

static HANDLE splash_done, splash_answer, splash_thread, progress_map;
static HINSTANCE splash_instance;
static wchar_t *splash_times;   /* where the averages are kept */
static int splash_enabled, splash_silent;
static volatile LONG splash_failure, splash_choice;
static const wchar_t *volatile splash_problem;   /* non-NULL: show it with Retry/Cancel */

static struct {   /* splash thread only */
    HWND window, owner, retry, cancel;
    HANDLE answer, gone;
    HFONT title, note, strong;
    HICON logo;
    UINT dpi;
    LONG failure, answered, round;
    double shown, drawn, last, ready_at, full_at, finish_rate;   /* shown: permille; drawn: as last invalidated */
    const wchar_t *problem, *text;   /* text, percent: the status line as painted */
    int percent;
} splash;

/* %LOCALAPPDATA%\DevmemStudio\splash\<hash of this EXE's folder>.times: each copy loads its own settings and top. */
static wchar_t *splash_location(void)
{
    static wchar_t self[PATH_CAP];
    DWORD length = GetModuleFileNameW(NULL, self, PATH_CAP);
    wchar_t *slash = length && length < PATH_CAP ? wcsrchr(self, L'\\') : NULL, *appdata = environment(L"LOCALAPPDATA"), *path = NULL;
    if (slash && appdata) {
        unsigned long long hash = 14695981039346656037ULL;   /* FNV-1a */
        for (const wchar_t *c = self; c < slash; ++c)
            hash = (hash ^ (unsigned long long)towupper(*c)) * 1099511628211ULL;
        wchar_t leaf[64];
        swprintf(leaf, 64, L"DevmemStudio\\splash\\%016llx.times", hash);
        path = join(appdata, leaf);
    }
    free(appdata);
    return path;
}

static void splash_timings(int save)
{
    if (!splash_times)
        return;
    wchar_t *slash = wcsrchr(splash_times, L'\\');
    if (save) {
        *slash = 0;
        CreateDirectoryW(splash_times, NULL);
        *slash = L'\\';
    }
    HANDLE file = CreateFileW(splash_times, save ? GENERIC_WRITE : GENERIC_READ, FILE_SHARE_READ, NULL,
                              save ? CREATE_ALWAYS : OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE)
        return;
    Timings timings = {{'D', 'M', 'S', 'T'}, 2, {{0}}};
    DWORD done = 0;
    if (save) {
        memcpy(timings.ms, expected, sizeof expected);
        WriteFile(file, &timings, sizeof timings, &done, NULL);
    } else if (ReadFile(file, &timings, sizeof timings, &done, NULL) && done == sizeof timings
               && !memcmp(timings.magic, "DMST", 4) && timings.version == 2) {
        for (int cold = 0; cold < 2; ++cold)
            for (int i = 0; i < STAGE_COUNT; ++i)
                if (timings.ms[cold][i] >= 0 && timings.ms[cold][i] <= 60000)   /* NaN fails both */
                    expected[cold][i] = timings.ms[cold][i];
    }
    CloseHandle(file);
}

static UINT window_dpi(HWND window)
{
    typedef UINT (WINAPI *DpiForWindow)(HWND);
    DpiForWindow dpi_for = (DpiForWindow)(void (*)(void))GetProcAddress(GetModuleHandleW(L"user32"), "GetDpiForWindow");
    UINT dpi = dpi_for ? dpi_for(window) : 0;
    if (!dpi) {
        HDC screen = GetDC(NULL);
        dpi = (UINT)GetDeviceCaps(screen, LOGPIXELSY);
        ReleaseDC(NULL, screen);
    }
    return dpi ? dpi : 96;
}

static HFONT splash_font(UINT dpi, int points, int weight)
{
    return CreateFontW(-MulDiv(points, (int)dpi, 72), 0, 0, 0, weight, 0, 0, 0, DEFAULT_CHARSET, OUT_DEFAULT_PRECIS,
                       CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
}

static int px(int value)
{
    return MulDiv(value, (int)splash.dpi, 96);
}

static void fill(HDC dc, int left, int top, int right, int bottom, COLORREF color)
{
    RECT box = {left, top, right, bottom};
    SetDCBrushColor(dc, color);
    FillRect(dc, &box, GetStockObject(DC_BRUSH));
}

static COLORREF mix(COLORREF from, COLORREF to, double part)
{
    return RGB((int)(GetRValue(from) + (GetRValue(to) - GetRValue(from)) * part + 0.5),
               (int)(GetGValue(from) + (GetGValue(to) - GetGValue(from)) * part + 0.5),
               (int)(GetBValue(from) + (GetBValue(to) - GetBValue(from)) * part + 0.5));
}

/* [from, to) in fractional pixels within [left, right): the edge pixels take their coverage, so it glides. */
static void span(HDC dc, int left, int right, int top, int bottom, double from, double to, COLORREF color, COLORREF track)
{
    from = max(from, left);
    to = min(to, right);
    if (to <= from)
        return;
    int first = (int)from, last = (int)to;
    if (first == last) {
        fill(dc, first, top, first + 1, bottom, mix(track, color, to - from));
        return;
    }
    fill(dc, first, top, first + 1, bottom, mix(track, color, first + 1 - from));
    fill(dc, first + 1, top, last, bottom, color);
    if (to > last)
        fill(dc, last, top, last + 1, bottom, mix(track, color, to - last));
}

static RECT splash_row(int top, int bottom)   /* the text column; 96-dpi rows */
{
    RECT row = {px(SPLASH_TEXT), px(top), px(SPLASH_WIDTH) - px(SPLASH_PAD), px(bottom)};
    return row;
}

/* Only what the update region touches: a frame is usually just the bar (text and icon cost ~1 ms). */
static void splash_paint(HDC target)
{
    RECT box, area, overlap, title = splash_row(18, 44), status = splash_row(48, 68), bar = splash_row(80, 84);
    RECT glyph = {px(SPLASH_PAD), px(24), px(SPLASH_PAD) + px(SPLASH_GLYPH), px(24) + px(SPLASH_GLYPH)};
    GetClientRect(splash.window, &box);
    if (GetClipBox(target, &area) == ERROR || !IntersectRect(&area, &area, &box))
        return;
    HDC dc = CreateCompatibleDC(target);   /* drawn off screen, just the area: the bar animates without flicker */
    HBITMAP canvas = CreateCompatibleBitmap(target, area.right - area.left, area.bottom - area.top);
    HGDIOBJ previous_bitmap = SelectObject(dc, canvas), previous_font = SelectObject(dc, splash.title);
    SetViewportOrgEx(dc, -area.left, -area.top, NULL);   /* still drawn in client coordinates */
    COLORREF accent = splash.problem ? RGB(0xC4, 0x2B, 0x1C) : RGB(0x24, 0x63, 0xDC), track = RGB(0xE3, 0xE9, 0xF0);
    fill(dc, 0, 0, box.right, box.bottom, RGB(0xFF, 0xFF, 0xFF));
    SetDCBrushColor(dc, RGB(0xD5, 0xDE, 0xE7));
    FrameRect(dc, &box, GetStockObject(DC_BRUSH));
    fill(dc, 0, 0, px(4), box.bottom, accent);
    if (splash.logo && IntersectRect(&overlap, &area, &glyph))
        DrawIconEx(dc, glyph.left, glyph.top, splash.logo, px(SPLASH_GLYPH), px(SPLASH_GLYPH), 0, NULL, DI_NORMAL);
    SetBkMode(dc, TRANSPARENT);
    if (IntersectRect(&overlap, &area, &title)) {
        SetTextColor(dc, RGB(0x23, 0x37, 0x4A));
        DrawTextW(dc, L"DevmemStudio " RUNTIME_VERSION, -1, &title, DT_SINGLELINE | DT_BOTTOM | DT_NOPREFIX);
    }
    if (splash.problem) {
        SelectObject(dc, splash.strong);
        SetTextColor(dc, accent);
        DrawTextW(dc, L"启动失败", -1, &status, DT_SINGLELINE | DT_NOPREFIX);
        SelectObject(dc, splash.note);
        SetTextColor(dc, RGB(0x33, 0x47, 0x5B));
        RECT message = {status.left, px(74), status.right, box.bottom};
        DrawTextW(dc, splash.problem, -1, &message, DT_WORDBREAK | DT_EDITCONTROL | DT_NOPREFIX);
    } else {
        if (splash.text && IntersectRect(&overlap, &area, &status)) {
            wchar_t percent[8];
            swprintf(percent, 8, L"%d%%", splash.percent);
            SelectObject(dc, splash.strong);
            SetTextColor(dc, accent);
            DrawTextW(dc, percent, -1, &status, DT_SINGLELINE | DT_RIGHT | DT_NOPREFIX);
            RECT text = status;
            text.right -= px(44);   /* clear of the percentage */
            SelectObject(dc, splash.note);
            SetTextColor(dc, RGB(0x6F, 0x82, 0x95));
            DrawTextW(dc, splash.text, -1, &text, DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS);
        }
        if (IntersectRect(&overlap, &area, &bar)) {
            fill(dc, bar.left, bar.top, bar.right, bar.bottom, track);
            span(dc, bar.left, bar.right, bar.top, bar.bottom, bar.left, bar.left + (bar.right - bar.left) * splash.shown / 1000,
                 accent, track);
        }
    }
    BitBlt(target, area.left, area.top, area.right - area.left, area.bottom - area.top, dc, area.left, area.top, SRCCOPY);
    SelectObject(dc, previous_font);
    SelectObject(dc, previous_bitmap);
    DeleteObject(canvas);
    DeleteDC(dc);
}

/* Height kept centered where the splash is. */
static void splash_place(int height)
{
    RECT now;
    GetWindowRect(splash.window, &now);
    SetWindowPos(splash.window, NULL, now.left, (now.top + now.bottom - height) / 2, px(SPLASH_WIDTH), height,
                 SWP_NOZORDER | SWP_NOACTIVATE);
}

static void splash_show_failure(const wchar_t *problem)
{
    splash.problem = problem;
    splash.answered = 0;
    HDC dc = GetDC(splash.window);
    HGDIOBJ previous = SelectObject(dc, splash.note);
    RECT text = {0, 0, px(SPLASH_WIDTH) - px(SPLASH_PAD) - px(SPLASH_TEXT), 0};
    DrawTextW(dc, problem, -1, &text, DT_CALCRECT | DT_WORDBREAK | DT_EDITCONTROL | DT_NOPREFIX);
    SelectObject(dc, previous);
    ReleaseDC(splash.window, dc);
    int top = px(74) + text.bottom + px(18), right = px(SPLASH_WIDTH) - px(SPLASH_PAD), width = px(84), height = px(28);
    SetWindowTextW(splash.window, L"DevmemStudio " RUNTIME_VERSION);
    SetWindowLongPtrW(splash.window, GWL_EXSTYLE, GetWindowLongPtrW(splash.window, GWL_EXSTYLE) & ~WS_EX_NOACTIVATE);
    SetWindowLongPtrW(splash.window, GWLP_HWNDPARENT, 0);   /* unowned: updaters see the failure, before its buttons do */
    SetWindowPos(splash.cancel, NULL, right - width, top, width, height, SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW);
    SetWindowPos(splash.retry, NULL, right - 2 * width - px(8), top, width, height,
                 SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW);
    splash_place(top + height + px(20));
    ShowWindow(splash.window, SW_SHOW);
    SetForegroundWindow(splash.window);
    SetFocus(splash.retry);
    InvalidateRect(splash.window, NULL, FALSE);
}

static void splash_show_progress(void)
{
    splash.problem = NULL;
    SetWindowLongPtrW(splash.window, GWLP_HWNDPARENT, (LONG_PTR)splash.owner);   /* owned again before it looks it */
    SetWindowTextW(splash.window, L"");
    SetWindowLongPtrW(splash.window, GWL_EXSTYLE, GetWindowLongPtrW(splash.window, GWL_EXSTYLE) | WS_EX_NOACTIVATE);
    ShowWindow(splash.retry, SW_HIDE);
    ShowWindow(splash.cancel, SW_HIDE);
    splash_place(px(SPLASH_HEIGHT));
    splash.text = NULL;
    InvalidateRect(splash.window, NULL, FALSE);
}

static double splash_at(LONG stage)   /* clock_ms as the stage began; 0: not reached */
{
    if (stage <= STAGE_START)
        return stage_at[stage];
    LONGLONG at = progress ? progress->at[stage] : 0;
    return at > 0 ? qpc_ms(at) : 0;
}

static LONG splash_stage(void)
{
    LONG stage = launch_stage, app = progress ? progress->stage : 0;
    return stage == STAGE_START && app > STAGE_START && app < STAGE_COUNT ? app : stage;
}

static double splash_weight(LONG stage)   /* share of the bar: waiting has none, writing is part of the unpack */
{
    if (stage == STAGE_WAIT || stage == STAGE_WRITE || (stage == STAGE_INFLATE && !unpacking))
        return 0;
    return max(expected[unpacking != 0][stage], 1.0);
}

/* The stages before in full, this one by unpack progress or by time against the starts before; late, it nears its end. */
static double splash_target(LONG stage, double now)
{
    LONG part = stage == STAGE_WRITE ? STAGE_INFLATE : stage == STAGE_WAIT ? STAGE_CHECK : stage;
    double before = 0, total = 0, done;
    for (LONG s = 0; s < STAGE_COUNT; ++s) {
        before += s < part ? splash_weight(s) : 0;
        total += splash_weight(s);
    }
    if (part == STAGE_INFLATE) {   /* a block inflates in one call: creep meanwhile */
        double elapsed = now - stage_at[STAGE_INFLATE];
        done = max((double)unpack_permille, stage == STAGE_INFLATE ? (INFLATE_SHARE - 50) * elapsed / (elapsed + 300) : 0) / 1000;
    } else {
        double began = splash_at(part), x = began ? max(now - began, 0.0) / splash_weight(part) : 0;
        done = x < 0.8 ? x : 1 - 0.04 / (x - 0.6);   /* the last fifth slows down gradually: still moving when late */
    }
    return 1000 * (before + splash_weight(part) * done) / total;
}

/* This start's stage times into the averages for the next start: the cold ones when it unpacked. */
static void splash_learn(double ready_at)
{
    double at[STAGE_COUNT];
    for (LONG s = 0; s < STAGE_COUNT; ++s)
        at[s] = splash_at(s);
    if (at[STAGE_WAIT])
        return;
    double *average = expected[at[STAGE_INFLATE] != 0];
    for (LONG s = 0; s < STAGE_COUNT; ++s) {
        if (s == STAGE_WAIT || s == STAGE_WRITE || (s == STAGE_INFLATE && !at[s]))
            continue;
        double end = ready_at;
        for (LONG next = s + 1; next < STAGE_COUNT; ++next)
            if (next != STAGE_WAIT && next != STAGE_WRITE && at[next]) {
                end = at[next];
                break;
            }
        average[s] = (average[s] + (at[s] ? min(max(end - at[s], 0.0), 60000.0) : 0)) / 2;
    }
    splash_timings(1);
}

static void splash_hand_over(double now)   /* the app waits to show its window: fill the rest */
{
    splash.ready_at = now;
    splash.finish_rate = max(2 * SPLASH_SPEED, (1000 - splash.shown) / SPLASH_FINISH);
}

static int splash_finished(double now)   /* full for a moment */
{
    return splash.ready_at && !splash.problem && splash.full_at && now - splash.full_at >= SPLASH_HOLD;
}

static void splash_tick(void)
{
    LONG failure = splash_failure;
    const wchar_t *problem = splash_problem;
    if (problem && failure != splash.failure) {
        splash.failure = failure;
        splash.ready_at = splash.full_at = 0;
        splash_show_failure(problem);
    } else if (!problem && splash.problem) {
        splash_show_progress();
    }
    if (splash.problem)
        return;
    double now = clock_ms(), step = splash.last ? min(now - splash.last, 100.0) : 0;
    LONG stage = splash_stage(), round = launch_round;
    splash.last = now;
    if (round != splash.round) {   /* a start (again, after Retry) */
        splash.round = round;
        splash.shown = splash.ready_at = splash.full_at = 0;
    }
    if (splash.ready_at) {
        splash.shown = min(splash.shown + splash.finish_rate * step, 1000.0);
    } else {
        double gap = splash_target(stage, now) - splash.shown;
        if (gap > 0)   /* exponential approach, capped in speed, with a floor so it arrives */
            splash.shown += min(gap, min(max(gap * step / (SPLASH_EASE + step), 0.5 * step), SPLASH_SPEED * step));
    }
    if (splash.shown >= 1000 && !splash.full_at)
        splash.full_at = now;
    const wchar_t *text = splash.ready_at ? L"正在打开主窗口…" : stage_text[stage];
    int percent = (int)(splash.shown / 10);
    if (text != splash.text || percent != splash.percent) {
        RECT status = splash_row(48, 68);
        splash.text = text;
        splash.percent = percent;
        InvalidateRect(splash.window, &status, FALSE);
    }
    if (splash.shown != splash.drawn) {
        RECT bar = splash_row(80, 84);
        splash.drawn = splash.shown;
        InvalidateRect(splash.window, &bar, FALSE);
    }
}

static LRESULT CALLBACK splash_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam)
{
    if (window != splash.window)
        return DefWindowProcW(window, message, wparam, lparam);
    switch (message) {
    case WM_PAINT: {
        PAINTSTRUCT paint;
        splash_paint(BeginPaint(window, &paint));
        EndPaint(window, &paint);
        return 0;
    }
    case WM_PRINTCLIENT:   /* themed buttons draw their corners from it */
        splash_paint((HDC)wparam);
        return 0;
    case WM_ERASEBKGND:
        return 1;
    case WM_CTLCOLORBTN:
        return (LRESULT)GetStockObject(WHITE_BRUSH);
    case WM_MOUSEACTIVATE:
        return splash.problem ? MA_ACTIVATE : MA_NOACTIVATE;
    case WM_SETCURSOR:
        if (!splash.problem)
            break;
        SetCursor(LoadCursorW(NULL, IDC_ARROW));
        return TRUE;
    case DM_GETDEFID:
        return MAKELRESULT(IDRETRY, DC_HASDEFID);
    case WM_CLOSE:   /* Alt+F4 */
        return splash_proc(window, WM_COMMAND, IDCANCEL, 0);
    case WM_COMMAND:
        if ((LOWORD(wparam) == IDRETRY || LOWORD(wparam) == IDCANCEL) && splash.problem && !splash.answered) {
            splash.answered = 1;
            InterlockedExchange(&splash_choice, LOWORD(wparam));
            SetEvent(splash.answer);
        }
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

/* The card, centered on the monitor with the cursor; fonts, icon and buttons at its DPI. */
static int splash_create(void)
{
    WNDCLASSEXW type = {sizeof type, CS_DROPSHADOW, splash_proc, 0, 0, splash_instance, NULL,
                        LoadCursorW(NULL, IDC_APPSTARTING), NULL, NULL, SPLASH_CLASS, NULL};
    POINT cursor = {0, 0};
    GetCursorPos(&cursor);
    MONITORINFO monitor = {sizeof monitor, {0, 0, 0, 0}, {0, 0, 0, 0}, 0};
    if ((!RegisterClassExW(&type) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS)
        || !GetMonitorInfoW(MonitorFromPoint(cursor, MONITOR_DEFAULTTOPRIMARY), &monitor))
        return 0;
    const RECT *work = &monitor.rcWork;
    splash.owner = CreateWindowExW(WS_EX_TOOLWINDOW, SPLASH_CLASS, L"", WS_POPUP, 0, 0, 0, 0, NULL, NULL, splash_instance, NULL);
    splash.window = splash.owner ? CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE, SPLASH_CLASS, L"",
                                                   WS_POPUP | WS_CLIPCHILDREN, work->left, work->top, 1, 1,
                                                   splash.owner, NULL, splash_instance, NULL) : NULL;
    if (!splash.window)
        return 0;
    splash.dpi = window_dpi(splash.window);   /* placed on the target monitor first, so this is its DPI */
    splash.title = splash_font(splash.dpi, 12, FW_SEMIBOLD);
    splash.note = splash_font(splash.dpi, 9, FW_NORMAL);
    splash.strong = splash_font(splash.dpi, 9, FW_SEMIBOLD);
    splash.logo = LoadImageW(splash_instance, MAKEINTRESOURCEW(101), IMAGE_ICON, px(SPLASH_GLYPH), px(SPLASH_GLYPH), 0);
    splash.retry = CreateWindowExW(0, L"BUTTON", L"重试", WS_CHILD | WS_TABSTOP | BS_DEFPUSHBUTTON, 0, 0, 0, 0,
                                   splash.window, (HMENU)(INT_PTR)IDRETRY, splash_instance, NULL);
    splash.cancel = CreateWindowExW(0, L"BUTTON", L"取消", WS_CHILD | WS_TABSTOP | BS_PUSHBUTTON, 0, 0, 0, 0,
                                    splash.window, (HMENU)(INT_PTR)IDCANCEL, splash_instance, NULL);
    SendMessageW(splash.retry, WM_SETFONT, (WPARAM)splash.note, FALSE);
    SendMessageW(splash.cancel, WM_SETFONT, (WPARAM)splash.note, FALSE);
    int width = px(SPLASH_WIDTH), height = px(SPLASH_HEIGHT);
    SetWindowPos(splash.window, NULL, (work->left + work->right - width) / 2, (work->top + work->bottom - height) / 2,
                 width, height, SWP_NOZORDER | SWP_NOACTIVATE);
    return 1;
}

static void splash_destroy(void)
{
    if (splash.window)
        DestroyWindow(splash.window);
    if (splash.owner)
        DestroyWindow(splash.owner);
    HGDIOBJ fonts[] = {splash.title, splash.note, splash.strong};
    for (int i = 0; i < 3; ++i)
        if (fonts[i])
            DeleteObject(fonts[i]);
    if (splash.logo)
        DestroyIcon(splash.logo);
}

static void splash_run(void)
{
    HANDLE done = splash_done, ready = splash_ready;   /* own copies: splash_stop may give up on us */
    splash.answer = splash_answer;
    splash.gone = splash_gone;
    splash_timings(0);
    if (!splash_create()) {
        splash_destroy();
        ZeroMemory(&splash, sizeof splash);
        return;
    }
    splash_tick();
    ShowWindow(splash.window, SW_SHOWNOACTIVATE);
    /* 60 fps from a high-resolution timer (a plain wait rounds to 15.6 ms steps, ~21 fps). Tick, paint, then wait:
       input still queued would end every wait early and starve "done". */
    HANDLE frame = CreateWaitableTimerExW(NULL, NULL, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    if (!frame)
        frame = CreateWaitableTimerW(NULL, FALSE, NULL);   /* before Windows 10 1803 */
    double next = clock_ms();
    for (;;) {
        double now = clock_ms();
        if (!splash.ready_at && !splash.problem && ready && WaitForSingleObject(ready, 0) == WAIT_OBJECT_0)
            splash_hand_over(now);
        if (now >= next) {
            splash_tick();
            next = next + SPLASH_FRAME > now ? next + SPLASH_FRAME : now + SPLASH_FRAME;
        }
        MSG message;
        while (PeekMessageW(&message, NULL, 0, 0, PM_REMOVE))
            if (!IsDialogMessageW(splash.window, &message)) {   /* Enter: Retry, Esc: Cancel, Tab */
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        if (WaitForSingleObject(done, 0) == WAIT_OBJECT_0 || splash_finished(clock_ms()))
            break;
        double wait = max(next - clock_ms(), 0.0);
        LARGE_INTEGER due;
        due.QuadPart = -(LONGLONG)(wait * 10000) - 1;   /* relative, 100 ns */
        HANDLE wakers[3];
        DWORD count = 0;
        wakers[count++] = done;
        if (ready && !splash.ready_at)
            wakers[count++] = ready;
        int timed = frame && SetWaitableTimer(frame, &due, 0, NULL, NULL, FALSE);
        if (timed)
            wakers[count++] = frame;
        if (MsgWaitForMultipleObjects(count, wakers, FALSE, timed ? INFINITE : (DWORD)wait + 1, QS_ALLINPUT) == WAIT_OBJECT_0)
            break;
    }
    if (frame)
        CloseHandle(frame);
    splash_destroy();
    if (splash.ready_at) {   /* handed over: the app's window first, the file write after */
        if (splash.gone)
            SetEvent(splash.gone);
        splash_learn(splash.ready_at);
    }
    ZeroMemory(&splash, sizeof splash);
}

/* The app sets "ready" right before it shows its window and waits for "gone": the splash never covers the window. */
static DWORD WINAPI splash_main(LPVOID unused)
{
    (void)unused;
    HANDLE gone = splash_gone;
    splash_run();
    if (gone)
        SetEvent(gone);
    return 0;
}

static void splash_open(void)
{
    if (splash_ready)
        ResetEvent(splash_ready);
    if (splash_gone)
        ResetEvent(splash_gone);
    splash_done = CreateEventW(NULL, TRUE, FALSE, NULL);
    splash_answer = CreateEventW(NULL, FALSE, FALSE, NULL);
    splash_thread = splash_done && splash_answer ? CreateThread(NULL, 0, splash_main, NULL, 0, NULL) : NULL;
    if (!splash_thread && splash_gone)
        SetEvent(splash_gone);
}

static void splash_start(HINSTANCE instance, const wchar_t *arguments)
{
    splash_instance = instance;
    splash_silent = wcsstr(arguments, L"--smoke-test") != NULL;
    splash_enabled = !splash_silent && !wcsstr(arguments, L"--offscreen");   /* acceptance runs stay windowless */
    wchar_t name[64], event[80];
    swprintf(name, 64, L"Local\\DevmemStudio.Splash.%lu", GetCurrentProcessId());
    if (splash_enabled) {
        swprintf(event, 80, L"%ls.ready", name);
        splash_ready = CreateEventW(NULL, TRUE, FALSE, event);
        swprintf(event, 80, L"%ls.gone", name);
        splash_gone = CreateEventW(NULL, TRUE, FALSE, event);
        swprintf(event, 80, L"%ls.progress", name);
        progress_map = CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, sizeof(Progress), event);
        progress = progress_map ? MapViewOfFile(progress_map, FILE_MAP_WRITE, 0, 0, sizeof(Progress)) : NULL;
        splash_times = splash_location();
    }
    SetEnvironmentVariableW(L"DEVMEMSTUDIO_LAUNCHER_SPLASH", splash_ready && splash_gone && progress ? name : NULL);
    if (splash_enabled)
        splash_open();
}

static void splash_stop(void)
{
    if (splash_thread) {
        SetEvent(splash_done);
        if (WaitForSingleObject(splash_thread, 2000) != WAIT_OBJECT_0) {
            CloseHandle(splash_thread);   /* still running: it keeps its events */
            splash_thread = splash_done = splash_answer = NULL;
            return;
        }
        CloseHandle(splash_thread);
    }
    if (splash_done)
        CloseHandle(splash_done);
    if (splash_answer)
        CloseHandle(splash_answer);
    splash_thread = splash_done = splash_answer = NULL;
}

/* Shows the problem in the splash (an error box without one); nonzero for Retry. */
static int splash_fail(const wchar_t *problem)
{
    splash_problem = problem;   /* before any new splash thread looks */
    InterlockedIncrement(&splash_failure);
    if (splash_enabled && (!splash_thread || WaitForSingleObject(splash_thread, 0) == WAIT_OBJECT_0)) {
        splash_stop();
        splash_open();   /* stopped at the app's window (or its signal), or never started */
    }
    if (splash_thread) {
        HANDLE waits[2] = {splash_answer, splash_thread};
        DWORD woke = WaitForMultipleObjects(2, waits, FALSE, INFINITE);
        if (splash_ready)
            ResetEvent(splash_ready);   /* the failed app's: no hand-over once the problem clears */
        splash_problem = NULL;
        if (woke == WAIT_OBJECT_0)
            return splash_choice == IDRETRY;
        splash_stop();   /* no window could be made */
    }
    splash_problem = NULL;
    return !splash_silent && MessageBoxW(NULL, problem, L"DevmemStudio " RUNTIME_VERSION, MB_RETRYCANCEL | MB_ICONERROR) == IDRETRY;
}

typedef struct {
    DWORD process;
    int found;
} Seek;

static BOOL CALLBACK seek_window(HWND window, LPARAM context)
{
    Seek *seek = (Seek *)context;
    DWORD process = 0;
    GetWindowThreadProcessId(window, &process);
    if (process == seek->process && IsWindowVisible(window)) {
        seek->found = 1;
        return FALSE;
    }
    return TRUE;
}

/* Keep the splash until it has handed over (the app said "ready"), or else the app shows any window of its own
   (an error box), exits, or times out. Nonzero: it exited first; the splash stays for a failure. */
static int splash_until_window(HANDLE process, DWORD id)
{
    Seek seek = {id, 0};
    ULONGLONG until = GetTickCount64() + SPLASH_TIMEOUT;
    while (splash_thread && GetTickCount64() < until) {
        HANDLE waits[2] = {process, splash_thread};
        DWORD woke = WaitForMultipleObjects(2, waits, FALSE, 25);
        if (woke == WAIT_OBJECT_0)
            return 1;
        if (woke == WAIT_OBJECT_0 + 1)
            break;
        if (splash_ready && WaitForSingleObject(splash_ready, 0) == WAIT_OBJECT_0)
            continue;   /* handing over: the splash ends itself */
        EnumWindows(seek_window, (LPARAM)&seek);
        if (seek.found)
            break;
    }
    splash_stop();
    return 0;
}

static int run_app(const wchar_t *root, const wchar_t *arguments, DWORD *exit_code, int *windowless)
{
    static wchar_t self[PATH_CAP];
    DWORD length = GetModuleFileNameW(NULL, self, PATH_CAP);
    if (!length || length >= PATH_CAP)
        return 0;
    SetEnvironmentVariableW(L"DEVMEMSTUDIO_LAUNCHER", self);
    wchar_t launcher_pid[32];
    swprintf(launcher_pid, 32, L"%lu", GetCurrentProcessId());
    SetEnvironmentVariableW(L"DEVMEMSTUDIO_LAUNCHER_PID", launcher_pid);
    wchar_t *slash = wcsrchr(self, L'\\');
    if (slash)
        *slash = 0;
    SetEnvironmentVariableW(L"DEVMEMSTUDIO_LAUNCHER_DIR", self);   /* settings stay beside the single EXE */
    wchar_t *exe = join(root, L"DevmemStudio.exe");
    size_t size = exe ? wcslen(exe) + wcslen(arguments) + 4 : 0;
    wchar_t *command = size && size <= PATH_CAP ? malloc(size * sizeof(wchar_t)) : NULL;
    int started = 0;
    if (command) {
        swprintf(command, size, L"\"%ls\" %ls", exe, arguments);
        STARTUPINFOEXW startup;
        PROCESS_INFORMATION process;
        ZeroMemory(&startup, sizeof startup);
        GetStartupInfoW(&startup.StartupInfo);   /* keeps shortcut identity and show state, desktop, redirected std handles */
        startup.StartupInfo.lpReserved = NULL;
        startup.StartupInfo.cbReserved2 = 0;
        startup.StartupInfo.lpReserved2 = NULL;
        int inherit = (startup.StartupInfo.dwFlags & STARTF_USESTDHANDLES) != 0;
        HANDLE job = app_job();
        startup.lpAttributeList = job_attribute(&job);
        startup.StartupInfo.cb = sizeof startup;
        int in_job = startup.lpAttributeList && CreateProcessW(exe, command, NULL, NULL, inherit,
                                                               CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT,
                                                               NULL, NULL, &startup.StartupInfo, &process);
        startup.StartupInfo.cb = sizeof startup.StartupInfo;
        started = in_job || CreateProcessW(exe, command, NULL, NULL, inherit, CREATE_SUSPENDED, NULL, NULL,
                                           &startup.StartupInfo, &process);
        if (startup.lpAttributeList) {
            DeleteProcThreadAttributeList(startup.lpAttributeList);
            free(startup.lpAttributeList);
        }
        if (started) {
            if (!in_job && job && !AssignProcessToJobObject(job, process.hProcess)) {   /* e.g. an outer job forbids nesting */
                CloseHandle(job);
                job = NULL;
            }
            if (ResumeThread(process.hThread) == (DWORD)-1) {
                TerminateProcess(process.hProcess, 3);
                started = 0;
            }
            AllowSetForegroundWindow(process.dwProcessId);
            CloseHandle(process.hThread);
            *windowless = splash_until_window(process.hProcess, process.dwProcessId);
            WaitForSingleObject(process.hProcess, INFINITE);
            if (!GetExitCodeProcess(process.hProcess, exit_code))
                *exit_code = 1;
            CloseHandle(process.hProcess);
        }
        if (job)
            CloseHandle(job);
    }
    free(exe);
    free(command);
    return started;
}

/* One start: unpack if needed, then run the app until it exits. NULL, or the problem to show. */
static const wchar_t *launch(HINSTANCE instance, const wchar_t *arguments, DWORD *exit_code)
{
    static wchar_t early_exit[160];
    const wchar_t *problem = L"无法读取本机用户目录（LOCALAPPDATA）。";
    wchar_t *appdata = environment(L"LOCALAPPDATA"), *top = NULL, *base = NULL, *root = NULL, *staging = NULL, *aside = NULL;
    HANDLE mutex = NULL;
    int owned = 0, windowless = 0;
    *exit_code = 3;
    begin_launch();
    if (!appdata || !(top = join(appdata, L"DevmemStudio")) || !(base = join(top, L"runtime")))
        goto done;
    problem = L"无法创建运行库缓存目录 %LOCALAPPDATA%\\DevmemStudio\\runtime。";
    make_tree(base);
    if (!plain_directory(base) || !(root = join(base, RUNTIME_ID)))
        goto done;
    problem = L"等待其他 DevmemStudio 准备运行库超时（120 秒）。请稍后重试；仍失败时结束残留的 DevmemStudio 进程后再启动。";
    mutex = CreateMutexW(NULL, FALSE, L"Local\\DevmemStudio.Runtime." RUNTIME_ID);
    DWORD wait = mutex ? WaitForSingleObject(mutex, 0) : WAIT_FAILED;
    if (wait == WAIT_TIMEOUT) {
        set_stage(STAGE_WAIT);
        wait = WaitForSingleObject(mutex, 120000);
        set_stage(STAGE_CHECK);
    }
    if (wait != WAIT_OBJECT_0 && wait != WAIT_ABANDONED)
        goto done;
    owned = 1;
    if (!lock_runtime(root)) {
        problem = L"解压运行库失败。请确认磁盘空间充足、用户目录可写；必要时删除 %LOCALAPPDATA%\\DevmemStudio\\runtime 后重试。";
        set_stage(STAGE_INFLATE);
        int unpacked = (staging = sibling(base, RUNTIME_ID, L"tmp")) && extract(instance, staging);
        if (!unpacked) {
            if (staging)
                remove_tree(staging);
            goto done;
        }
        if (GetFileAttributesW(root) != INVALID_FILE_ATTRIBUTES
            && (!(aside = sibling(base, RUNTIME_ID, L"old")) || !move_directory(root, aside))) {
            free(root);   /* a damaged copy still in use: run the fresh extraction where it is */
            root = staging;
            staging = NULL;
        }
        if (staging && !move_directory(staging, root)) {
            free(root);
            root = staging;
            staging = NULL;
        }
        if (aside)
            remove_tree(aside);
        if (!lock_runtime(root))
            goto done;
    }
    ReleaseMutex(mutex);
    owned = 0;
    problem = L"无法启动 DevmemStudio。";
    set_stage(STAGE_START);
    if (!run_app(root, arguments, exit_code, &windowless)) {
        *exit_code = 3;
        goto done;
    }
    problem = NULL;
    const wchar_t *leaf = wcsrchr(root, L'\\');
    sweep(base, leaf ? leaf + 1 : root);
    if (windowless && *exit_code) {
        swprintf(early_exit, 160, *exit_code > 0xFFFF ? L"程序在显示窗口前退出（退出代码 0x%08lX）。反复失败时删除 %%LOCALAPPDATA%%\\DevmemStudio\\runtime 后重试。"
                                                       : L"程序在显示窗口前退出（退出代码 %lu）。反复失败时删除 %%LOCALAPPDATA%%\\DevmemStudio\\runtime 后重试。",
                 *exit_code);
        problem = early_exit;
    }
done:
    if (owned)
        ReleaseMutex(mutex);
    if (mutex)
        CloseHandle(mutex);
    unlock_runtime();
    free(appdata);
    free(top);
    free(base);
    free(root);
    free(staging);
    free(aside);
    return problem;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show)
{
    (void)previous;
    (void)show;
    DWORD exit_code = 3;
    const wchar_t *problem;
    splash_start(instance, arguments);
    while ((problem = launch(instance, arguments, &exit_code)) && splash_fail(problem)) {
        /* Retry: the whole start again, in the same splash */
    }
    splash_stop();
    return (int)exit_code;
}
