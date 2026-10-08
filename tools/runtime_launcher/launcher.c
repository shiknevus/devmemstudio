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

typedef struct {
    unsigned char *packed;
    unsigned char *raw;
    DWORD packed_size, raw_size;
    int ok;
} Block;

static DWORD WINAPI inflate(LPVOID context)
{
    Block *block = context;
    DECOMPRESSOR_HANDLE decompressor = NULL;
    SIZE_T size = 0;
    block->ok = CreateDecompressor(COMPRESS_ALGORITHM_LZMS, NULL, &decompressor)
             && Decompress(decompressor, block->packed, block->packed_size, block->raw, block->raw_size, &size)
             && size == block->raw_size;
    if (decompressor)
        CloseDecompressor(decompressor);
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
        blocks[i] = (Block){packed + packed_at, raw + raw_at, runtime_blocks[i].packed, runtime_blocks[i].raw, 0};
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
    unsigned char digest[32];
    ok = ok && sha256(raw, RUNTIME_RAW_SIZE, digest) && !memcmp(digest, runtime_sha256, 32);
    size_t offset = 0;
    for (size_t i = 0; ok && i < RUNTIME_FILE_COUNT; ++i) {
        wchar_t *path = join(staging, runtime_files[i].path);
        ok = path && make_parents(staging, runtime_files[i].path) && write_file(path, raw + offset, runtime_files[i].size);
        free(path);
        offset += runtime_files[i].size;
    }
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

static int run_app(const wchar_t *root, const wchar_t *arguments, DWORD *exit_code)
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
        STARTUPINFOW startup;
        PROCESS_INFORMATION process;
        GetStartupInfoW(&startup);   /* keeps shortcut identity and show state, desktop, redirected std handles */
        startup.lpReserved = NULL;
        startup.cbReserved2 = 0;
        startup.lpReserved2 = NULL;
        started = CreateProcessW(exe, command, NULL, NULL, (startup.dwFlags & STARTF_USESTDHANDLES) != 0,
                                 0, NULL, NULL, &startup, &process);
        if (started) {
            AllowSetForegroundWindow(process.dwProcessId);
            CloseHandle(process.hThread);
            WaitForSingleObject(process.hProcess, INFINITE);
            if (!GetExitCodeProcess(process.hProcess, exit_code))
                *exit_code = 1;
            CloseHandle(process.hProcess);
        }
    }
    free(exe);
    free(command);
    return started;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show)
{
    (void)previous;
    (void)show;
    const wchar_t *problem = L"无法读取本机用户目录（LOCALAPPDATA）。";
    wchar_t *appdata = environment(L"LOCALAPPDATA"), *top = NULL, *base = NULL, *root = NULL, *staging = NULL, *aside = NULL;
    HANDLE mutex = NULL;
    int owned = 0;
    DWORD exit_code = 3;
    if (!appdata || !(top = join(appdata, L"DevmemStudio")) || !(base = join(top, L"runtime")))
        goto done;
    problem = L"无法创建运行库缓存目录 %LOCALAPPDATA%\\DevmemStudio\\runtime。";
    make_tree(base);
    if (!plain_directory(base) || !(root = join(base, RUNTIME_ID)))
        goto done;
    mutex = CreateMutexW(NULL, FALSE, L"Local\\DevmemStudio.Runtime." RUNTIME_ID);
    DWORD wait = mutex ? WaitForSingleObject(mutex, 120000) : WAIT_FAILED;
    if (wait != WAIT_OBJECT_0 && wait != WAIT_ABANDONED)
        goto done;
    owned = 1;
    if (!lock_runtime(root)) {
        problem = L"解压运行库失败。请确认磁盘空间充足、用户目录可写；必要时删除 %LOCALAPPDATA%\\DevmemStudio\\runtime 后重试。";
        if (!(staging = sibling(base, RUNTIME_ID, L"tmp")) || !extract(instance, staging)) {
            if (staging)
                remove_tree(staging);
            goto done;
        }
        if (GetFileAttributesW(root) != INVALID_FILE_ATTRIBUTES
            && (!(aside = sibling(base, RUNTIME_ID, L"old")) || !MoveFileExW(root, aside, 0))) {
            free(root);   /* a damaged copy still in use: run the fresh extraction where it is */
            root = staging;
            staging = NULL;
        }
        if (staging && !MoveFileExW(staging, root, 0)) {
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
    if (!run_app(root, arguments, &exit_code)) {
        exit_code = 3;
        goto done;
    }
    problem = NULL;
    const wchar_t *leaf = wcsrchr(root, L'\\');
    sweep(base, leaf ? leaf + 1 : root);
done:
    if (owned)
        ReleaseMutex(mutex);
    if (mutex)
        CloseHandle(mutex);
    unlock_runtime();
    if (problem && !wcsstr(arguments, L"--smoke-test"))
        MessageBoxW(NULL, problem, L"DevmemStudio " RUNTIME_VERSION, MB_OK | MB_ICONERROR);
    free(appdata);
    free(top);
    free(base);
    free(root);
    free(staging);
    free(aside);
    return (int)exit_code;
}
