/* Safe, cancellable packaging. No user data is interpolated into scripts.
 * The source is held read-only; only task-owned temporary files are removed. */
#include "pack_core.h"
#include <shlobj.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <wchar.h>
#include <wctype.h>
#include <string.h>

#define IO_CHUNK (256 * 1024)
#define ARRAY_COUNT(a) (sizeof(a) / sizeof((a)[0]))

wchar_t *pack_dup(const wchar_t *s)
{
    if (!s) return NULL;
    size_t n = wcslen(s) + 1;
    wchar_t *r = malloc(n * sizeof *r);
    if (r) memcpy(r, s, n * sizeof *r);
    return r;
}
wchar_t *pack_join(const wchar_t *dir, const wchar_t *name)
{
    size_t a = wcslen(dir), b = wcslen(name);
    int slash = a && dir[a - 1] != L'\\' && dir[a - 1] != L'/';
    if (a + b + (size_t)slash >= PACK_PATH_LIMIT) { SetLastError(ERROR_FILENAME_EXCED_RANGE); return NULL; }
    wchar_t *r = malloc((a + b + (size_t)slash + 1) * sizeof *r);
    if (!r) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); return NULL; }
    memcpy(r, dir, a * sizeof *r);
    if (slash) r[a++] = L'\\';
    memcpy(r + a, name, (b + 1) * sizeof *r);
    return r;
}
wchar_t *pack_fullpath(const wchar_t *path)
{
    if (!path || !*path) { SetLastError(ERROR_INVALID_NAME); return NULL; }
    if (wcsncmp(path, L"\\\\.\\", 4) == 0) { SetLastError(ERROR_INVALID_NAME); return NULL; }
    DWORD needed = GetFullPathNameW(path, 0, NULL, NULL);
    if (!needed || needed > PACK_PATH_LIMIT - 8) { SetLastError(ERROR_FILENAME_EXCED_RANGE); return NULL; }
    wchar_t *abs = malloc(((size_t)needed + 1) * sizeof *abs);
    if (!abs) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); return NULL; }
    DWORD got = GetFullPathNameW(path, needed + 1, abs, NULL);
    if (!got || got > needed) { free(abs); return NULL; }
    for (wchar_t *p = abs; *p; ++p) if (*p == L'/') *p = L'\\';
    if (wcsncmp(abs, L"\\\\?\\", 4) == 0) return abs;
    wchar_t *r;
    if (wcsncmp(abs, L"\\\\", 2) == 0) {
        r = malloc((wcslen(abs) + 7) * sizeof *r);
        if (r) swprintf(r, wcslen(abs) + 7, L"\\\\?\\UNC\\%ls", abs + 2);
    } else {
        r = malloc((wcslen(abs) + 5) * sizeof *r);
        if (r) swprintf(r, wcslen(abs) + 5, L"\\\\?\\%ls", abs);
    }
    free(abs);
    if (!r) SetLastError(ERROR_NOT_ENOUGH_MEMORY);
    return r;
}
const wchar_t *pack_display(const wchar_t *p)
{
    if (p && wcsncmp(p, L"\\\\?\\", 4) == 0 && wcsncmp(p + 4, L"UNC\\", 4) != 0) return p + 4;
    return p ? p : L"";
}
wchar_t *pack_parent(const wchar_t *path)
{
    wchar_t *r = pack_dup(path);
    if (!r) return NULL;
    wchar_t *end = wcsrchr(r, L'\\');
    if (!end) { free(r); return NULL; }
    /* Preserve the slash of a drive root, including extended drive roots. */
    if (end > r && end[-1] == L':') end[1] = 0;
    else *end = 0;
    return r;
}
void pack_system_error(wchar_t *error, size_t cap, const wchar_t *action, DWORD code)
{
    wchar_t msg[512] = L"";
    FormatMessageW(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, NULL,
                   code, 0, msg, 512, NULL);
    size_t n = wcslen(msg);
    while (n && (msg[n - 1] == L'\r' || msg[n - 1] == L'\n')) msg[--n] = 0;
    swprintf(error, cap, L"%ls（系统错误 %lu）：%ls", action, (unsigned long)code, msg);
}
int pack_valid_project(const wchar_t *s)
{
    if (!s || !*s || wcslen(s) > PROJECT_MAX) return 0;
    for (; *s; ++s) if (*s < 32 || iswspace(*s) || wcschr(L"\\/:*?\"<>|", *s)) return 0;
    return 1;
}
int pack_valid_input(const wchar_t *path, wchar_t *error, size_t cap)
{
    const wchar_t *base = wcsrchr(path, L'\\');
    base = base ? base + 1 : path;
    const wchar_t *dot = wcsrchr(base, L'.');
    if (!dot || _wcsicmp(dot, L".bit")) {
        swprintf(error, cap, L"源文件必须是 .bit 文件。"); return 0;
    }
    DWORD attr = GetFileAttributesW(path);
    if (attr == INVALID_FILE_ATTRIBUTES) {
        pack_system_error(error, cap, L"源文件不存在或无法访问", GetLastError()); return 0;
    }
    if (attr & FILE_ATTRIBUTE_DIRECTORY) {
        swprintf(error, cap, L"源路径是目录，请选择普通 .bit 文件。"); return 0;
    }
    return 1;
}
static int interrupted(PackJob *j)
{
    if (j->cancel && WaitForSingleObject(j->cancel, 0) == WAIT_OBJECT_0) {
        swprintf(j->error, PACK_ERROR_CAP, L"已取消；源文件和已有输出保持不变。"); return PACK_CANCELLED;
    }
    if (j->timeout_seconds && GetTickCount64() - j->started >= (ULONGLONG)j->timeout_seconds * 1000) {
        swprintf(j->error, PACK_ERROR_CAP, L"任务超过 %lu 秒，已终止；源文件和已有输出保持不变。",
                 (unsigned long)j->timeout_seconds); return PACK_TIMEOUT;
    }
    return PACK_OK;
}
static void status(PackJob *j, const wchar_t *text)
{
    if (j->progress) j->progress(text);
    if (!j->notify) return;
    wchar_t *copy = pack_dup(text);
    if (copy && !PostMessageW(j->notify, WM_PACK_STATUS, 0, (LPARAM)copy)) free(copy);
}
/* Keep the bitstream build time, as CopyFileW did in 1.x. Best effort. */
static void keep_mtime(HANDLE src, HANDLE dst)
{
    FILETIME t;
    if (GetFileTime(src, NULL, NULL, &t)) SetFileTime(dst, NULL, NULL, &t);
}
/* flush: only for files that get published; task-private temp files skip the disk sync. */
static int stream_copy(PackJob *j, HANDLE src, HANDLE dst, int flush)
{
    unsigned char *buf = malloc(IO_CHUNK);
    if (!buf) { pack_system_error(j->error, PACK_ERROR_CAP, L"分配复制缓冲区失败", ERROR_NOT_ENOUGH_MEMORY); return PACK_FAILED; }
    int rc = PACK_OK;
    for (;;) {
        rc = interrupted(j);
        if (rc) break;
        DWORD n = 0;
        if (!ReadFile(src, buf, IO_CHUNK, &n, NULL)) {
            pack_system_error(j->error, PACK_ERROR_CAP, L"读取文件失败", GetLastError()); rc = PACK_FAILED; break;
        }
        if (!n) break;
        DWORD offset = 0;
        while (offset < n) {
            DWORD written = 0;
            if (!WriteFile(dst, buf + offset, n - offset, &written, NULL) || !written) {
                pack_system_error(j->error, PACK_ERROR_CAP, L"写入文件失败（请检查磁盘空间和权限）", GetLastError()); rc = PACK_FAILED; break;
            }
            offset += written;
        }
        if (rc) break;
    }
    if (!rc && flush && !FlushFileBuffers(dst)) {
        pack_system_error(j->error, PACK_ERROR_CAP, L"刷新输出文件失败", GetLastError()); rc = PACK_FAILED;
    }
    free(buf); return rc;
}
static int compare_files(PackJob *j, const wchar_t *a, const wchar_t *b)
{
    HANDLE x = CreateFileW(a, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    HANDLE y = CreateFileW(b, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    unsigned char *buf = malloc(IO_CHUNK * 2);
    int rc = PACK_FAILED;
    if (x == INVALID_HANDLE_VALUE || y == INVALID_HANDLE_VALUE || !buf) {
        pack_system_error(j->error, PACK_ERROR_CAP, L"无法读取归档校验文件", !buf ? ERROR_NOT_ENOUGH_MEMORY : GetLastError()); goto done;
    }
    for (;;) {
        rc = interrupted(j); if (rc) break;
        DWORD na = 0, nb = 0;
        if (!ReadFile(x, buf, IO_CHUNK, &na, NULL) || !ReadFile(y, buf + IO_CHUNK, IO_CHUNK, &nb, NULL)) {
            pack_system_error(j->error, PACK_ERROR_CAP, L"读取归档校验数据失败", GetLastError()); rc = PACK_FAILED; break;
        }
        if (na != nb || memcmp(buf, buf + IO_CHUNK, na)) {
            swprintf(j->error, PACK_ERROR_CAP, L"ZIP 解压内容与源文件不一致，未发布输出。"); rc = PACK_FAILED; break;
        }
        if (!na) { rc = PACK_OK; break; }
    }
done:
    if (x != INVALID_HANDLE_VALUE) CloseHandle(x);
    if (y != INVALID_HANDLE_VALUE) CloseHandle(y);
    free(buf); return rc;
}
static wchar_t *new_id(void)
{
    GUID id;
    wchar_t text[40];
    if (FAILED(CoCreateGuid(&id)) || !StringFromGUID2(&id, text, 40)) return NULL;
    return pack_dup(text);
}
static wchar_t *make_workdir(PackJob *j)
{
    wchar_t *temp = malloc(PACK_PATH_LIMIT * sizeof *temp);
    if (!temp) return NULL;
    DWORD n = GetTempPathW(PACK_PATH_LIMIT, temp);
    wchar_t *dir = NULL;
    if (!n || n >= PACK_PATH_LIMIT) goto done;
    wchar_t *id = new_id();
    if (!id) goto done;
    wchar_t name[80]; swprintf(name, 80, L"bitpack-%ls", id); free(id);
    wchar_t *plain = pack_join(temp, name);
    if (plain) { dir = pack_fullpath(plain); free(plain); }
    if (dir && !CreateDirectoryW(dir, NULL)) {
        pack_system_error(j->error, PACK_ERROR_CAP, L"创建独立临时目录失败", GetLastError()); free(dir); dir = NULL;
    }
done:
    free(temp);
    if (!dir && !j->error[0]) pack_system_error(j->error, PACK_ERROR_CAP, L"获取临时目录失败", GetLastError());
    return dir;
}
/* Windows CRT quoting: quote every argument, double backslashes before quotes/end. */
static wchar_t *command_line(const wchar_t *exe, const wchar_t **args, size_t count)
{
    size_t cap = wcslen(exe) * 2 + 8;
    for (size_t i = 0; i < count; ++i) cap += wcslen(args[i]) * 2 + 4;
    if (cap >= 32767) { SetLastError(ERROR_FILENAME_EXCED_RANGE); return NULL; }
    wchar_t *r = malloc(cap * sizeof *r); if (!r) return NULL;
    wchar_t *o = r;
    for (size_t i = 0; i <= count; ++i) {
        const wchar_t *s = i ? args[i - 1] : exe;
        if (i) *o++ = L' ';
        *o++ = L'"';
        while (*s) {
            size_t slash = 0;
            while (*s == L'\\') { ++slash; ++s; }
            size_t emit = (*s == L'"' || !*s) ? slash * 2 : slash;
            while (emit--) *o++ = L'\\';
            if (*s == L'"') *o++ = L'\\';
            if (*s) *o++ = *s++;
        }
        *o++ = L'"';
    }
    *o = 0; return r;
}
/* PowerShell writes UTF-8; system tar writes the ANSI code page (GBK on zh-CN). */
static void decode_tool_text(const char *raw, int n, wchar_t *text, int cap)
{
    for (int cut = 0; cut < 4 && cut < n; ++cut) { /* tolerate a UTF-8 sequence split by the read limit */
        int len = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, raw, n - cut, text, cap - 1);
        if (len > 0) { text[len] = 0; return; }
    }
    int len = MultiByteToWideChar(CP_ACP, 0, raw, n, text, cap - 1);
    text[len > 0 ? len : 0] = 0;
}
static void log_error(PackJob *j, const wchar_t *log, DWORD exit_code)
{
    char raw[1024]; DWORD n = 0;
    HANDLE h = CreateFileW(log, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    wchar_t text[1024] = L"";
    if (h != INVALID_HANDLE_VALUE) {
        if (ReadFile(h, raw, sizeof raw - 1, &n, NULL) && n) decode_tool_text(raw, (int)n, text, 1024);
        CloseHandle(h);
    }
    swprintf(j->error, PACK_ERROR_CAP, L"压缩工具失败（退出码 %lu）。\n%ls", (unsigned long)exit_code, text);
}
static int run_tool(PackJob *j, const wchar_t *exe, const wchar_t **args, size_t count,
                    const wchar_t *cwd, const wchar_t *log)
{
    int rc = interrupted(j); if (rc) return rc;
    wchar_t *cmd = command_line(exe, args, count);
    if (!cmd) { pack_system_error(j->error, PACK_ERROR_CAP, L"构造工具参数失败", ERROR_NOT_ENOUGH_MEMORY); return PACK_FAILED; }
    SECURITY_ATTRIBUTES sa = { sizeof sa, NULL, TRUE };
    HANDLE output = CreateFileW(log, GENERIC_WRITE, FILE_SHARE_READ, &sa, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    HANDLE input = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, NULL);
    HANDLE group = CreateJobObjectW(NULL, NULL);
    PROCESS_INFORMATION pi; ZeroMemory(&pi, sizeof pi);
    STARTUPINFOEXW sx; ZeroMemory(&sx, sizeof sx); sx.StartupInfo.cb = sizeof sx;
    SIZE_T bytes = 0;
    InitializeProcThreadAttributeList(NULL, 1, 0, &bytes);
    sx.lpAttributeList = malloc(bytes);
    int attrs_ready = 0;
    if (output == INVALID_HANDLE_VALUE || input == INVALID_HANDLE_VALUE || !group || !sx.lpAttributeList) goto setup_failed;
    if (!InitializeProcThreadAttributeList(sx.lpAttributeList, 1, 0, &bytes)) goto setup_failed;
    attrs_ready = 1;
    HANDLE handles[] = { output, input };
    if (!UpdateProcThreadAttribute(sx.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, sizeof handles, NULL, NULL)) goto setup_failed;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim; ZeroMemory(&lim, sizeof lim);
    lim.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(group, JobObjectExtendedLimitInformation, &lim, sizeof lim)) goto setup_failed;
    sx.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    sx.StartupInfo.hStdInput = input;
    sx.StartupInfo.hStdOutput = sx.StartupInfo.hStdError = output;
    if (!CreateProcessW(exe, cmd, NULL, NULL, TRUE,
                        CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT,
                        NULL, cwd, &sx.StartupInfo, &pi)) goto setup_failed;
    if (!AssignProcessToJobObject(group, pi.hProcess)) {
        DWORD e = GetLastError(); TerminateProcess(pi.hProcess, PACK_FAILED);
        pack_system_error(j->error, PACK_ERROR_CAP, L"无法管理压缩子进程", e); rc = PACK_FAILED; goto finish;
    }
    if (ResumeThread(pi.hThread) == (DWORD)-1) goto setup_failed;
    for (;;) {
        DWORD w = WaitForSingleObject(pi.hProcess, 50);
        if (w == WAIT_OBJECT_0) {
            DWORD code = 0;
            if (!GetExitCodeProcess(pi.hProcess, &code)) {
                pack_system_error(j->error, PACK_ERROR_CAP, L"读取工具退出码失败", GetLastError()); rc = PACK_FAILED;
            } else if (code) { log_error(j, log, code); rc = PACK_FAILED; }
            else rc = interrupted(j);
            break;
        }
        if (w == WAIT_FAILED) { pack_system_error(j->error, PACK_ERROR_CAP, L"等待压缩进程失败", GetLastError()); rc = PACK_FAILED; break; }
        rc = interrupted(j); if (rc) break;
    }
    goto finish;
setup_failed:
    pack_system_error(j->error, PACK_ERROR_CAP, L"启动系统压缩工具失败", GetLastError()); rc = PACK_FAILED;
finish:
    if (group) { if (rc) TerminateJobObject(group, (UINT)rc); CloseHandle(group); }
    if (pi.hProcess) { WaitForSingleObject(pi.hProcess, 5000); CloseHandle(pi.hProcess); }
    if (pi.hThread) CloseHandle(pi.hThread);
    if (attrs_ready) DeleteProcThreadAttributeList(sx.lpAttributeList);
    free(sx.lpAttributeList);
    if (output != INVALID_HANDLE_VALUE) CloseHandle(output);
    if (input != INVALID_HANDLE_VALUE) CloseHandle(input);
    free(cmd); return rc;
}
/* Fixed names inside a per-job work directory. No projects or external paths in script. */
static const char ps_script[] =
"param([ValidateSet('pack','verify')][string]$Action)\r\n"
"$ErrorActionPreference='Stop'\r\n"
"[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)\r\n"
"Add-Type -AssemblyName System.IO.Compression\r\n"
"Add-Type -AssemblyName System.IO.Compression.FileSystem\r\n"
"$src=Join-Path $PSScriptRoot 'sunny_fpga.bit'\r\n"
"$dest=Join-Path $PSScriptRoot 'archive.zip'\r\n"
"try {\r\n"
" if($Action -eq 'pack'){\r\n"
"  $z=[IO.Compression.ZipFile]::Open($dest,[IO.Compression.ZipArchiveMode]::Create)\r\n"
"  try {\r\n"
"   $e=$z.CreateEntry('sunny_fpga.bit',[IO.Compression.CompressionLevel]::Optimal)\r\n"
"   $t=[IO.File]::GetLastWriteTime($src); if($t.Year -ge 1980 -and $t.Year -le 2107){$e.LastWriteTime=$t}\r\n"
"   $i=[IO.File]::OpenRead($src); try {$o=$e.Open(); try {$i.CopyTo($o,262144)} finally {$o.Dispose()}} finally {$i.Dispose()}\r\n"
"  } finally {$z.Dispose()}\r\n"
" }\r\n"
" $z=[IO.Compression.ZipFile]::OpenRead($dest)\r\n"
" try {\r\n"
"  if($z.Entries.Count -ne 1 -or $z.Entries[0].FullName -cne 'sunny_fpga.bit'){throw 'Unexpected ZIP entries'}\r\n"
"  $sha=[Security.Cryptography.SHA256]::Create()\r\n"
"  try {\r\n"
"   $i=[IO.File]::OpenRead($src); try {$a=[BitConverter]::ToString($sha.ComputeHash($i)); $size=$i.Length} finally {$i.Dispose()}\r\n"
"   $i=$z.Entries[0].Open(); try {$b=[BitConverter]::ToString($sha.ComputeHash($i))} finally {$i.Dispose()}\r\n"
"   if($a -cne $b -or $size -ne $z.Entries[0].Length){throw 'ZIP content verification failed'}\r\n"
"  } finally {$sha.Dispose()}\r\n"
" } finally {$z.Dispose()}\r\n"
" exit 0\r\n"
"} catch {[Console]::Error.WriteLine($_.Exception.ToString()); exit 1}\r\n";
static int write_script(PackJob *j, const wchar_t *path)
{
    HANDLE h = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) { pack_system_error(j->error, PACK_ERROR_CAP, L"创建回退脚本失败", GetLastError()); return PACK_FAILED; }
    DWORD n = 0;
    int ok = WriteFile(h, ps_script, (DWORD)(sizeof ps_script - 1), &n, NULL) && n == sizeof ps_script - 1;
    DWORD e = GetLastError(); CloseHandle(h);
    if (!ok) pack_system_error(j->error, PACK_ERROR_CAP, L"写入回退脚本失败", e);
    return ok ? PACK_OK : PACK_FAILED;
}
static int verify_listing(PackJob *j, const wchar_t *log)
{
    char text[64]; DWORD n = 0;
    HANDLE h = CreateFileW(log, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (h == INVALID_HANDLE_VALUE) { pack_system_error(j->error, PACK_ERROR_CAP, L"读取 ZIP 目录失败", GetLastError()); return PACK_FAILED; }
    LARGE_INTEGER size;
    int ok = GetFileSizeEx(h, &size) && size.QuadPart < (LONGLONG)sizeof text && ReadFile(h, text, sizeof text - 1, &n, NULL);
    CloseHandle(h);
    if (ok) { text[n] = 0; while (n && (text[n - 1] == '\r' || text[n - 1] == '\n')) text[--n] = 0; ok = strcmp(text, "sunny_fpga.bit") == 0; }
    if (!ok) swprintf(j->error, PACK_ERROR_CAP, L"ZIP 目录校验失败：归档必须仅包含 sunny_fpga.bit。");
    return ok ? PACK_OK : PACK_FAILED;
}
static int make_archive(PackJob *j, const wchar_t *work, const wchar_t *snapshot,
                        const wchar_t *archive, const wchar_t *script, const wchar_t *log,
                        const wchar_t *verify_dir, const wchar_t *verified)
{
    wchar_t system[MAX_PATH];
    if (!GetSystemDirectoryW(system, MAX_PATH)) { pack_system_error(j->error, PACK_ERROR_CAP, L"获取系统工具目录失败", GetLastError()); return PACK_FAILED; }
    wchar_t *tar = pack_join(system, L"tar.exe");
    wchar_t *ps = pack_join(system, L"WindowsPowerShell\\v1.0\\powershell.exe");
    if (!tar || !ps) { free(tar); free(ps); return PACK_FAILED; }
    int rc = PACK_FAILED;
    if (j->engine != ENGINE_POWERSHELL) {
        status(j, L"正在压缩（系统 tar）…");
        const wchar_t *create[] = { L"-a", L"-cf", L"archive.zip", L"--", L"sunny_fpga.bit" };
        rc = run_tool(j, tar, create, ARRAY_COUNT(create), pack_display(work), log);
        if (!rc) {
            status(j, L"正在校验 ZIP 目录及解压内容…");
            const wchar_t *list[] = { L"-tf", L"archive.zip" };
            rc = run_tool(j, tar, list, ARRAY_COUNT(list), pack_display(work), log);
            if (!rc) rc = verify_listing(j, log);
            if (!rc && !CreateDirectoryW(verify_dir, NULL)) { pack_system_error(j->error, PACK_ERROR_CAP, L"创建校验目录失败", GetLastError()); rc = PACK_FAILED; }
            if (!rc) {
                const wchar_t *extract[] = { L"-xf", L"archive.zip", L"-C", L"verify", L"--", L"sunny_fpga.bit" };
                rc = run_tool(j, tar, extract, ARRAY_COUNT(extract), pack_display(work), log);
            }
            if (!rc) rc = compare_files(j, snapshot, verified);
            if (!rc) wcscpy(j->backend, L"tar");
        }
        if (rc && rc != PACK_CANCELLED && rc != PACK_TIMEOUT && j->engine == ENGINE_AUTO) {
            status(j, L"tar 不可用或校验失败，正在回退到 PowerShell…");
            DeleteFileW(archive);
        }
    }
    if (j->engine == ENGINE_POWERSHELL || (rc == PACK_FAILED && j->engine == ENGINE_AUTO)) {
        j->error[0] = 0;
        rc = write_script(j, script);
        if (!rc) {
            status(j, L"正在压缩并校验（PowerShell/.NET）…");
            const wchar_t *args[] = { L"-NoLogo", L"-NoProfile", L"-NonInteractive", L"-ExecutionPolicy", L"Bypass", L"-File", pack_display(script), L"pack" };
            rc = run_tool(j, ps, args, ARRAY_COUNT(args), pack_display(work), log);
            if (!rc) wcscpy(j->backend, L"PowerShell/.NET");
        }
    }
    free(tar); free(ps); return rc;
}
static int reserve_output(PackJob *j, wchar_t **path, HANDLE *handle)
{
    wchar_t *id = new_id();
    if (!id) { pack_system_error(j->error, PACK_ERROR_CAP, L"生成任务编号失败", ERROR_NOT_ENOUGH_MEMORY); return PACK_FAILED; }
    wchar_t name[96]; swprintf(name, 96, L".bitpack-%ls.tmp", id); free(id);
    *path = pack_join(j->output_dir, name);
    if (!*path) { pack_system_error(j->error, PACK_ERROR_CAP, L"输出路径过长或内存不足", GetLastError()); return PACK_FAILED; }
    *handle = CreateFileW(*path, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (*handle == INVALID_HANDLE_VALUE) {
        pack_system_error(j->error, PACK_ERROR_CAP, L"无法在输出目录创建文件", GetLastError()); free(*path); *path = NULL; return PACK_FAILED;
    }
    return PACK_OK;
}
static int publish(PackJob *j, const wchar_t *pending)
{
    int rc = interrupted(j); if (rc) return rc;
    SYSTEMTIME st; GetLocalTime(&st);
    wchar_t stem[128];
    swprintf(stem, 128, L"%ls_bit_%04u%02u%02u%02u%02u%02u", j->project,
             st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    for (unsigned i = 0; i < 100000; ++i) {
        wchar_t name[160];
        if (j->nopack) wcscpy(name, L"sunny_fpga.bit");
        else if (i) swprintf(name, 160, L"%ls_%02u.zip", stem, i);
        else swprintf(name, 160, L"%ls.zip", stem);
        wchar_t *dest = pack_join(j->output_dir, name);
        if (!dest) { pack_system_error(j->error, PACK_ERROR_CAP, L"输出文件路径过长", GetLastError()); return PACK_FAILED; }
        /* Same directory/volume: atomic rename, never a cross-volume copy. */
        DWORD flags = MOVEFILE_WRITE_THROUGH;
        if (j->nopack && j->force) flags |= MOVEFILE_REPLACE_EXISTING;
        if (MoveFileExW(pending, dest, flags)) { j->result = dest; return PACK_OK; }
        DWORD e = GetLastError(); free(dest);
        if (!j->nopack && (e == ERROR_ALREADY_EXISTS || e == ERROR_FILE_EXISTS)) {
            rc = interrupted(j); if (rc) return rc;
            continue;
        }
        if (j->nopack && (e == ERROR_ALREADY_EXISTS || e == ERROR_FILE_EXISTS))
            swprintf(j->error, PACK_ERROR_CAP, L"输出目录已存在 sunny_fpga.bit，未覆盖。确认需要替换时使用 --force。");
        else pack_system_error(j->error, PACK_ERROR_CAP, L"发布输出文件失败", e);
        return PACK_FAILED;
    }
    swprintf(j->error, PACK_ERROR_CAP, L"同名输出过多，请换一个项目号。"); return PACK_FAILED;
}
static int same_file(HANDLE a, const wchar_t *b)
{
    HANDLE h = CreateFileW(b, 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
    if (h == INVALID_HANDLE_VALUE) return 0;
    BY_HANDLE_FILE_INFORMATION x, y;
    int yes = GetFileInformationByHandle(a, &x) && GetFileInformationByHandle(h, &y) &&
              x.dwVolumeSerialNumber == y.dwVolumeSerialNumber && x.nFileIndexHigh == y.nFileIndexHigh && x.nFileIndexLow == y.nFileIndexLow;
    CloseHandle(h); return yes;
}
static void cleanup_file(PackJob *j, const wchar_t *p)
{
    if (!p) return;
    if (!DeleteFileW(p)) {
        DWORD e = GetLastError();
        if (e != ERROR_FILE_NOT_FOUND && e != ERROR_PATH_NOT_FOUND)
            swprintf(j->warning, PACK_ERROR_CAP, L"临时文件清理失败，可稍后手动清理：%.*ls（错误 %lu）", 1500, pack_display(p), (unsigned long)e);
    }
}
int pack_run(PackJob *j)
{
    int rc = PACK_FAILED;
    wchar_t *work = NULL, *snapshot = NULL, *archive = NULL, *script = NULL, *log = NULL;
    wchar_t *verify_dir = NULL, *verified = NULL, *pending = NULL;
    HANDLE src = INVALID_HANDLE_VALUE, dst = INVALID_HANDLE_VALUE, zip = INVALID_HANDLE_VALUE;
    j->started = GetTickCount64(); j->error[0] = j->warning[0] = 0;
    if (!j->nopack && !pack_valid_project(j->project)) {
        swprintf(j->error, PACK_ERROR_CAP, L"项目号必须为 1–63 个字符，不能包含空白或 \\ / : * ? \" < > |。" ); goto done;
    }
    if (!pack_valid_input(j->input, j->error, PACK_ERROR_CAP)) goto done;
    DWORD attr = GetFileAttributesW(j->output_dir);
    if (attr == INVALID_FILE_ATTRIBUTES || !(attr & FILE_ATTRIBUTE_DIRECTORY)) {
        swprintf(j->error, PACK_ERROR_CAP, L"输出目录不存在或不是目录，请先创建目录。"); goto done;
    }
    rc = interrupted(j); if (rc) goto done;
    rc = PACK_FAILED;
    /* Disallow concurrent writes/deletion, so the copied snapshot is consistent. */
    src = CreateFileW(j->input, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    if (src == INVALID_HANDLE_VALUE) { pack_system_error(j->error, PACK_ERROR_CAP, L"读取源文件失败（可能正在被其它程序写入）", GetLastError()); goto done; }
    LARGE_INTEGER size;
    if (!GetFileSizeEx(src, &size)) { pack_system_error(j->error, PACK_ERROR_CAP, L"读取源文件大小失败", GetLastError()); goto done; }
    j->source_bytes = (ULONGLONG)size.QuadPart;
    if (j->nopack) {
        wchar_t *dest = pack_join(j->output_dir, L"sunny_fpga.bit");
        if (!dest) goto done;
        if (same_file(src, dest)) { j->result = dest; wcscpy(j->backend, L"无需复制"); rc = PACK_OK; goto done; }
        DWORD existing = GetFileAttributesW(dest); free(dest);
        if (existing != INVALID_FILE_ATTRIBUTES && !j->force) {
            swprintf(j->error, PACK_ERROR_CAP, L"输出目录已存在 sunny_fpga.bit，未覆盖。确认需要替换时使用 --force。"); goto done;
        }
    }
    rc = reserve_output(j, &pending, &dst); if (rc) goto done;
    if (j->nopack) {
        status(j, L"正在复制（源文件保留）…");
        rc = stream_copy(j, src, dst, 1);
        if (!rc) keep_mtime(src, dst);
        CloseHandle(dst); dst = INVALID_HANDLE_VALUE;
        if (!rc) { status(j, L"正在校验副本…"); rc = compare_files(j, j->input, pending); }
        if (!rc) { rc = publish(j, pending); if (!rc) wcscpy(j->backend, L"复制"); }
        goto done;
    }
    work = make_workdir(j); if (!work) { rc = PACK_FAILED; goto done; }
    snapshot = pack_join(work, L"sunny_fpga.bit"); archive = pack_join(work, L"archive.zip");
    script = pack_join(work, L"pack.ps1"); log = pack_join(work, L"tool.log");
    verify_dir = pack_join(work, L"verify"); verified = verify_dir ? pack_join(verify_dir, L"sunny_fpga.bit") : NULL;
    if (!snapshot || !archive || !script || !log || !verify_dir || !verified) {
        pack_system_error(j->error, PACK_ERROR_CAP, L"分配任务路径失败", ERROR_NOT_ENOUGH_MEMORY); rc = PACK_FAILED; goto done;
    }
    status(j, L"正在创建独立源文件快照…");
    HANDLE stage = CreateFileW(snapshot, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (stage == INVALID_HANDLE_VALUE) { pack_system_error(j->error, PACK_ERROR_CAP, L"创建快照失败", GetLastError()); rc = PACK_FAILED; goto done; }
    rc = stream_copy(j, src, stage, 0);
    if (!rc) keep_mtime(src, stage); /* ZIP entry time = bitstream build time */
    CloseHandle(stage); if (rc) goto done;
    rc = make_archive(j, work, snapshot, archive, script, log, verify_dir, verified); if (rc) goto done;
    status(j, L"校验通过，正在发布 ZIP…");
    zip = CreateFileW(archive, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    if (zip == INVALID_HANDLE_VALUE) { pack_system_error(j->error, PACK_ERROR_CAP, L"读取已校验 ZIP 失败", GetLastError()); rc = PACK_FAILED; goto done; }
    rc = stream_copy(j, zip, dst, 1);
    CloseHandle(zip); zip = INVALID_HANDLE_VALUE;
    CloseHandle(dst); dst = INVALID_HANDLE_VALUE;
    if (!rc) rc = publish(j, pending);
done:
    if (src != INVALID_HANDLE_VALUE) CloseHandle(src);
    if (dst != INVALID_HANDLE_VALUE) CloseHandle(dst);
    if (zip != INVALID_HANDLE_VALUE) CloseHandle(zip);
    cleanup_file(j, pending); cleanup_file(j, verified);
    if (verify_dir && !RemoveDirectoryW(verify_dir) && GetLastError() != ERROR_PATH_NOT_FOUND && GetLastError() != ERROR_FILE_NOT_FOUND)
        swprintf(j->warning, PACK_ERROR_CAP, L"校验目录未能清理：%.*ls", 1500, pack_display(verify_dir));
    cleanup_file(j, snapshot); cleanup_file(j, archive); cleanup_file(j, script); cleanup_file(j, log);
    if (work && !RemoveDirectoryW(work)) swprintf(j->warning, PACK_ERROR_CAP, L"临时目录未能清理：%.*ls", 1500, pack_display(work));
    free(pending); free(verified); free(verify_dir); free(snapshot); free(archive); free(script); free(log); free(work);
    if (rc == PACK_FAILED && !j->error[0]) pack_system_error(j->error, PACK_ERROR_CAP, L"操作失败", ERROR_NOT_ENOUGH_MEMORY);
    j->elapsed_ms = (DWORD)(GetTickCount64() - j->started);
    return rc;
}
static int list_add(BitList *l, const wchar_t *path)
{
    if (l->count >= PACK_SCAN_LIMIT) { l->truncated = 1; return 0; }
    if (l->count == l->capacity) {
        size_t capacity = l->capacity ? l->capacity * 2 : 128;
        wchar_t **p = realloc(l->items, capacity * sizeof *p);
        if (!p) { l->status = PACK_FAILED; swprintf(l->error, PACK_ERROR_CAP, L"扫描列表内存不足。"); return 0; }
        l->items = p; l->capacity = capacity;
    }
    wchar_t *s = pack_dup(path);
    if (!s) { l->status = PACK_FAILED; return 0; }
    l->items[l->count++] = s; return 1;
}
static void scan_dir(BitList *l, const wchar_t *dir, unsigned depth)
{
    if (l->status || l->truncated) return;
    if (l->cancel && WaitForSingleObject(l->cancel, 0) == WAIT_OBJECT_0) { l->status = PACK_CANCELLED; return; }
    wchar_t *pat = pack_join(dir, L"*");
    if (!pat) { ++l->skipped; return; }
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileExW(pat, FindExInfoBasic, &fd, FindExSearchNameMatch, NULL, FIND_FIRST_EX_LARGE_FETCH);
    free(pat);
    if (h == INVALID_HANDLE_VALUE) { if (GetLastError() != ERROR_FILE_NOT_FOUND) ++l->skipped; return; }
    do {
        if (l->status || l->truncated) break;
        if (l->cancel && WaitForSingleObject(l->cancel, 0) == WAIT_OBJECT_0) { l->status = PACK_CANCELLED; break; }
        if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L"..")) continue;
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) { ++l->skipped; continue; }
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            if (!l->recursive) continue;
            if (depth >= PACK_SCAN_DEPTH) { ++l->skipped; continue; }
            wchar_t *sub = pack_join(dir, fd.cFileName);
            if (sub) { scan_dir(l, sub, depth + 1); free(sub); } else ++l->skipped;
        } else {
            const wchar_t *dot = wcsrchr(fd.cFileName, L'.');
            if (dot && !_wcsicmp(dot, L".bit")) {
                wchar_t *full = pack_join(dir, fd.cFileName);
                if (full) { list_add(l, full); free(full); } else ++l->skipped;
            }
        }
    } while (FindNextFileW(h, &fd));
    DWORD e = GetLastError();
    if (!l->status && !l->truncated && e != ERROR_NO_MORE_FILES) ++l->skipped;
    FindClose(h);
}
static int path_compare(const void *a, const void *b)
{
    return _wcsicmp(*(wchar_t * const *)a, *(wchar_t * const *)b);
}
int pack_scan(BitList *l)
{
    DWORD attr = GetFileAttributesW(l->root);
    if (attr == INVALID_FILE_ATTRIBUTES || !(attr & FILE_ATTRIBUTE_DIRECTORY)) {
        pack_system_error(l->error, PACK_ERROR_CAP, L"扫描目录不存在或无法访问", attr == INVALID_FILE_ATTRIBUTES ? GetLastError() : ERROR_DIRECTORY);
        l->status = PACK_FAILED; return l->status;
    }
    scan_dir(l, l->root, 0);
    if (l->count > 1) qsort(l->items, l->count, sizeof *l->items, path_compare);
    return l->status;
}
void pack_list_free(BitList *l)
{
    if (!l) return;
    for (size_t i = 0; i < l->count; ++i) free(l->items[i]);
    free(l->items); free(l->root); free(l);
}
void pack_job_free(PackJob *j)
{
    if (!j) return;
    free(j->input); free(j->output_dir); free(j->result); free(j);
}
