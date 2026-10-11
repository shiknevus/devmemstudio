#include "../pack_core.h"
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>
static int fail_tar;
static int fail_write, fail_read, fail_rename, short_read;
static unsigned read_calls;
static BOOL fake_write(HANDLE,LPCVOID,DWORD,LPDWORD,LPOVERLAPPED);
static BOOL fake_read(HANDLE,LPVOID,DWORD,LPDWORD,LPOVERLAPPED);
static BOOL fake_move(LPCWSTR,LPCWSTR,DWORD);
static BOOL fake_create(LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES, LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION);
#define CreateProcessW fake_create
#define WriteFile fake_write
#define ReadFile fake_read
#define MoveFileExW fake_move
#include "../pack_core.c"
#undef CreateProcessW
#undef WriteFile
#undef ReadFile
#undef MoveFileExW
static BOOL fake_create(LPCWSTR app, LPWSTR cmd, LPSECURITY_ATTRIBUTES pa, LPSECURITY_ATTRIBUTES ta, BOOL inherit,
                        DWORD flags, LPVOID env, LPCWSTR cwd, LPSTARTUPINFOW si, LPPROCESS_INFORMATION pi)
{
    const wchar_t *base = wcsrchr(app, L'\\'); base = base ? base + 1 : app;
    if (fail_tar && !_wcsicmp(base, L"tar.exe")) { SetLastError(ERROR_FILE_NOT_FOUND); return FALSE; }
    return CreateProcessW(app, cmd, pa, ta, inherit, flags, env, cwd, si, pi);
}
static BOOL fake_write(HANDLE h,LPCVOID data,DWORD size,LPDWORD done,LPOVERLAPPED overlap)
{
    if (fail_write) { if (done) *done = 0; SetLastError(ERROR_DISK_FULL); return FALSE; }
    return WriteFile(h,data,size,done,overlap);
}
static BOOL fake_read(HANDLE h,LPVOID data,DWORD size,LPDWORD done,LPOVERLAPPED overlap)
{
    if (fail_read) { if (done) *done = 0; SetLastError(ERROR_CRC); return FALSE; }
    if (short_read) { DWORD cap = 300 + (++read_calls % 7) * 211; if (size > cap) size = cap; } /* network-share short reads */
    return ReadFile(h,data,size,done,overlap);
}
static BOOL fake_move(LPCWSTR source,LPCWSTR target,DWORD flags)
{
    if (fail_rename) { SetLastError(ERROR_ACCESS_DENIED); return FALSE; }
    return MoveFileExW(source,target,flags);
}
static int no_pending(const wchar_t *root)
{
    wchar_t *pat = pack_join(root,L".bitpack-*.tmp"); WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pat,&fd); free(pat);
    if (h != INVALID_HANDLE_VALUE) { FindClose(h); return 0; } return GetLastError() == ERROR_FILE_NOT_FOUND;
}
static int passed, failed;
#define CHECK(name, test) do { if (test) { ++passed; printf("PASS %s\n", name); } else { ++failed; printf("FAIL %s (line %d)\n", name, __LINE__); } } while (0)
static void fixture_size(const wchar_t *path, unsigned char seed, DWORD size)
{
    HANDLE h = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) { ++failed; return; }
    unsigned char data[8192]; for (size_t i = 0; i < sizeof data; ++i) data[i] = (unsigned char)(i + seed);
    DWORD n; if (size > sizeof data || !WriteFile(h, data, size, &n, NULL) || n != size) ++failed;
    CloseHandle(h);
}
static void fixture(const wchar_t *path, unsigned char seed) { fixture_size(path, seed, 4096); }
static DWORD WINAPI signal_cancel(void *handle) { Sleep(150); SetEvent((HANDLE)handle); return 0; }
int wmain(int argc, wchar_t **argv)
{
    if (argc != 3) return 2;
    wchar_t *root = pack_fullpath(argv[1]), *stub = pack_fullpath(argv[2]);
    if (!root || !stub) return 2;
    CreateDirectoryW(root, NULL);
    CHECK("project empty", !pack_valid_project(L""));
    CHECK("project quote unicode", pack_valid_project(L"项目O'Hara;$()"));
    CHECK("project whitespace", !pack_valid_project(L"A B") && !pack_valid_project(L"A\tB") && !pack_valid_project(L"A\x3000"));
    CHECK("project illegal", !pack_valid_project(L"A/B") && !pack_valid_project(L"A\"B") && !pack_valid_project(L"A\nB"));
    wchar_t project[65]; wmemset(project, L'A', 64); project[64] = 0;
    CHECK("project oversized", !pack_valid_project(project)); project[63] = 0;
    CHECK("project maximum", pack_valid_project(project));
    wchar_t *drive = pack_parent(L"\\\\?\\C:\\a.bit"); CHECK("drive-root parent", drive && !wcscmp(drive, L"\\\\?\\C:\\")); free(drive);
    wchar_t *unc = pack_fullpath(L"\\\\server\\share\\a.bit"); CHECK("UNC conversion", unc && !wcscmp(unc, L"\\\\?\\UNC\\server\\share\\a.bit")); free(unc);
    wchar_t *device = pack_fullpath(L"\\\\.\\NUL"); CHECK("device path rejected", !device); free(device);
    const wchar_t *values[] = { L"a b", L"quote\"inside", L"C:\\trailing\\", L"'single;$()" };
    wchar_t *line = command_line(L"tool.exe", values, ARRAY_COUNT(values));
    int n = 0; wchar_t **parsed = CommandLineToArgvW(line, &n);
    int match = parsed && n == 5;
    for (int i = 1; match && i < n; ++i) match = !wcscmp(parsed[i], values[i - 1]);
    CHECK("Windows argument quoting roundtrip", match); if (parsed) LocalFree(parsed); free(line);
    wchar_t decoded[64];
    decode_tool_text("\xE4\xB8\xAD", 3, decoded, 64); CHECK("tool text UTF-8", !wcscmp(decoded, L"中"));
    decode_tool_text("ab\xE4\xB8", 4, decoded, 64); CHECK("tool text split UTF-8 tail", !wcscmp(decoded, L"ab"));
    decode_tool_text("\xD6\xD0", 2, decoded, 64); CHECK("tool text ANSI (GBK) fallback", GetACP() != 936 || !wcscmp(decoded, L"中"));
    wchar_t *input = pack_join(root, L"source.bit"), *sunny = pack_join(root, L"sunny_fpga.bit"), *log = pack_join(root, L"child.log");
    fixture(input, 1); fixture(sunny, 2);
    PackJob j; ZeroMemory(&j, sizeof j); j.input = input; j.output_dir = root; wcscpy(j.project, L"CORE"); j.timeout_seconds = 30;
    j.cancel = CreateEventW(NULL, TRUE, FALSE, NULL);
    SetEvent(j.cancel); CHECK("pre-cancel no publication", pack_run(&j) == PACK_CANCELLED && !j.result); ResetEvent(j.cancel);
    j.started = GetTickCount64() - 2000; j.timeout_seconds = 1;
    CHECK("whole-task timeout", interrupted(&j) == PACK_TIMEOUT);
    j.started = GetTickCount64();
    const wchar_t *sleep_args[] = { L"sleep" };
    CHECK("child timeout", run_tool(&j, stub, sleep_args, ARRAY_COUNT(sleep_args), pack_display(root), log) == PACK_TIMEOUT);
    j.timeout_seconds = 30; j.started = GetTickCount64(); ResetEvent(j.cancel);
    HANDLE thread = CreateThread(NULL, 0, signal_cancel, j.cancel, 0, NULL);
    CHECK("child cancellation", run_tool(&j, stub, sleep_args, ARRAY_COUNT(sleep_args), pack_display(root), log) == PACK_CANCELLED);
    WaitForSingleObject(thread, INFINITE); CloseHandle(thread); ResetEvent(j.cancel);
    j.started = GetTickCount64(); const wchar_t *fail_args[] = { L"fail" };
    CHECK("stderr and exit code", run_tool(&j, stub, fail_args, ARRAY_COUNT(fail_args), pack_display(root), log) == PACK_FAILED && wcsstr(j.error, L"controlled child failure") && wcsstr(j.error, L"7"));
    j.started = GetTickCount64(); j.timeout_seconds = 1; const wchar_t *desc_args[] = { L"descendant" };
    int drc = run_tool(&j, stub, desc_args, ARRAY_COUNT(desc_args), pack_display(root), log);
    FILE *f = _wfopen(log, L"r"); unsigned long pid = 0; if (f) { fscanf(f, "%lu", &pid); fclose(f); }
    HANDLE descendant = pid ? OpenProcess(SYNCHRONIZE, FALSE, (DWORD)pid) : NULL;
    CHECK("timeout kills descendant", drc == PACK_TIMEOUT && pid && (!descendant || WaitForSingleObject(descendant, 1000) == WAIT_OBJECT_0)); if (descendant) CloseHandle(descendant);
    HANDLE listing = CreateFileW(log, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    const char good_listing[] = "sunny_fpga.bit\n"; DWORD listed;
    WriteFile(listing, good_listing, (DWORD)strlen(good_listing), &listed, NULL); CloseHandle(listing);
    CHECK("ZIP listing accepts one expected entry", verify_listing(&j, log) == PACK_OK);
    listing = CreateFileW(log, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    const char multiple_listing[] = "sunny_fpga.bit\nextra.bit\n";
    WriteFile(listing, multiple_listing, (DWORD)strlen(multiple_listing), &listed, NULL); CloseHandle(listing);
    CHECK("ZIP listing rejects extra entry", verify_listing(&j, log) == PACK_FAILED);
    listing = CreateFileW(log, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    char oversized_listing[128]; memset(oversized_listing,'\n',sizeof oversized_listing); memcpy(oversized_listing,good_listing,strlen(good_listing));
    memcpy(oversized_listing+100,"extra.bit",9);
    WriteFile(listing, oversized_listing, sizeof oversized_listing, &listed, NULL); CloseHandle(listing);
    CHECK("ZIP listing rejects truncated inspection", verify_listing(&j, log) == PACK_FAILED);
    j.timeout_seconds = 30; j.engine = ENGINE_AUTO; fail_tar = 1;
    int fallback = pack_run(&j); fail_tar = 0;
    CHECK("automatic PowerShell fallback", fallback == PACK_OK && !wcscmp(j.backend, L"PowerShell/.NET"));
    CHECK("fallback preserves source and existing sunny", GetFileAttributesW(input) != INVALID_FILE_ATTRIBUTES && GetFileAttributesW(sunny) != INVALID_FILE_ATTRIBUTES);
    free(j.result); j.result = NULL;
    fail_write = 1;
    CHECK("disk-full abort cleans pending output", pack_run(&j) == PACK_FAILED && no_pending(root) && GetFileAttributesW(input) != INVALID_FILE_ATTRIBUTES && wcsstr(j.error,L"112"));
    fail_write = 0; fail_read = 1;
    CHECK("read failure abort cleans pending output", pack_run(&j) == PACK_FAILED && no_pending(root) && GetFileAttributesW(input) != INVALID_FILE_ATTRIBUTES);
    fail_read = 0;
    j.nopack = 1; j.force = 1; fail_rename = 1;
    CHECK("publish failure cleans pending and preserves target", pack_run(&j) == PACK_FAILED && no_pending(root) && GetFileAttributesW(sunny) != INVALID_FILE_ATTRIBUTES);
    fail_rename = 0; j.force = 0;
    j.input = sunny; j.nopack = 1;
    CHECK("copy self is no-op", pack_run(&j) == PACK_OK && GetFileAttributesW(sunny) != INVALID_FILE_ATTRIBUTES);
    free(j.result); j.result = NULL;
    j.input = input; j.nopack = 1; j.force = 0;
    CHECK("copy refuses overwrite", pack_run(&j) == PACK_FAILED && !j.result);
    j.force = 1; CHECK("copy explicit force", pack_run(&j) == PACK_OK); free(j.result); j.result = NULL;
    wchar_t *alias = pack_join(root, L"alias.bit");
    int hardlink = CreateHardLinkW(alias, sunny, NULL) != 0;
    j.input = alias;
    CHECK("hardlink source detected", hardlink && pack_run(&j) == PACK_OK && !wcscmp(j.backend, L"无需复制")); free(j.result); j.result = NULL;
    wchar_t *pending = pack_join(root, L"pending.tmp"); fixture(pending, 3);
    j.input = input; j.nopack = 0; wcscpy(j.project, L"COLLISION"); j.started = GetTickCount64();
    CHECK("publish initial", publish(&j, pending) == PACK_OK); wchar_t *first = j.result; j.result = NULL;
    fixture(pending, 4); CHECK("publish collision", publish(&j, pending) == PACK_OK && _wcsicmp(first, j.result));
    CHECK("old output retained", GetFileAttributesW(first) != INVALID_FILE_ATTRIBUTES); free(first); free(j.result); j.result = NULL;
    wchar_t *scanroot = pack_join(root, L"scan"); CreateDirectoryW(scanroot, NULL);
    for (int i = 0; i < 100; ++i) { wchar_t name[32]; swprintf(name, 32, L"item%03d.bit", i); wchar_t *p = pack_join(scanroot, name); fixture(p, (unsigned char)i); free(p); }
    wchar_t *fakebit = pack_join(scanroot, L"directory.bit"); CreateDirectoryW(fakebit, NULL);
    BitList *list = calloc(1, sizeof *list); list->root = pack_dup(scanroot);
    CHECK("scan more than 64 excludes directories", pack_scan(list) == PACK_OK && list->count == 100 && !list->truncated);
    CHECK("scan sorted", list->count == 100 && _wcsicmp(list->items[0], list->items[99]) < 0);
    pack_list_free(list);
    list = calloc(1, sizeof *list); list->root = pack_dup(scanroot); list->cancel = j.cancel; SetEvent(j.cancel);
    CHECK("scan cancellation", pack_scan(list) == PACK_CANCELLED); ResetEvent(j.cancel); pack_list_free(list);
    /* Short reads of different sizes on each side must still compare by offset. */
    wchar_t *same_a = pack_join(root, L"same_a.bin"), *same_b = pack_join(root, L"same_b.bin");
    fixture_size(same_a, 5, 8000); fixture_size(same_b, 5, 8000);
    j.started = GetTickCount64(); j.timeout_seconds = 30; short_read = 1;
    CHECK("compare short reads equal", compare_files(&j, same_a, same_b) == PACK_OK);
    fixture_size(same_b, 6, 8000); CHECK("compare short reads differ", compare_files(&j, same_a, same_b) == PACK_FAILED);
    fixture_size(same_b, 5, 7000); CHECK("compare shorter copy", compare_files(&j, same_a, same_b) == PACK_FAILED);
    CHECK("compare longer original", compare_files(&j, same_b, same_a) == PACK_FAILED);
    short_read = 0;
    fixture_size(same_b, 5, 8000); CHECK("compare full reads equal", compare_files(&j, same_a, same_b) == PACK_OK);
    /* The first failing setup call is reported, not ERROR_INSUFFICIENT_BUFFER from a later probe call. */
    wchar_t *missing_log = pack_join(root, L"missing-dir\\tool.log");
    CHECK("setup error keeps first cause", run_tool(&j, stub, fail_args, ARRAY_COUNT(fail_args), pack_display(root), missing_log) == PACK_FAILED
          && wcsstr(j.error, L"系统错误 3）"));
    /* Every cleanup warning is kept; overflow ends with an ellipsis. */
    j.warning[0] = 0; add_warning(&j, L"first"); add_warning(&j, L"second");
    CHECK("warnings appended", !wcscmp(j.warning, L"first\nsecond"));
    wchar_t long_line[1500]; wmemset(long_line, L'x', 1499); long_line[1499] = 0;
    add_warning(&j, long_line); add_warning(&j, long_line); add_warning(&j, L"lost");
    size_t wlen = wcslen(j.warning);
    CHECK("warning overflow bounded", wlen < PACK_ERROR_CAP && j.warning[wlen - 1] == L'…' && !wcsstr(j.warning, L"lost")
          && !wcsncmp(j.warning, L"first\nsecond\n", 13));
    /* A junction beside the .bit must not block the non-recursive auto pick. */
    wchar_t *pickroot = pack_join(root, L"pick"), *pickbit = pickroot ? pack_join(pickroot, L"only.bit") : NULL;
    wchar_t *pickdir = pack_join(root, L"pick-target"), command[1024];
    CreateDirectoryW(pickroot, NULL); CreateDirectoryW(pickdir, NULL); fixture(pickbit, 7);
    swprintf(command, 1024, L"mklink /J \"%ls\\link\" \"%ls\" >nul", pack_display(pickroot), pack_display(pickdir));
    int junction = _wsystem(command) == 0;
    list = calloc(1, sizeof *list); list->root = pack_dup(pickroot);
    CHECK("non-recursive scan ignores junction", junction && pack_scan(list) == PACK_OK && list->count == 1 && !list->skipped);
    pack_list_free(list);
    list = calloc(1, sizeof *list); list->root = pack_dup(pickroot); list->recursive = 1;
    CHECK("recursive scan skips junction", junction && pack_scan(list) == PACK_OK && list->count == 1 && list->skipped == 1);
    pack_list_free(list);
    wchar_t *link = pack_join(pickroot, L"link"); RemoveDirectoryW(link); free(link);
    free(pickroot); free(pickbit); free(pickdir); free(missing_log); free(same_a); free(same_b);
    CloseHandle(j.cancel); free(alias); free(pending); free(scanroot); free(fakebit); free(input); free(sunny); free(log); free(root); free(stub);
    printf("CORE RESULT: %d passed, %d failed\n", passed, failed);
    return failed ? 1 : 0;
}
