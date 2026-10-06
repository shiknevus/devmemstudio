/* Bit_pack 2.0: Unicode GUI + script-friendly CLI, shared safe packaging core. */
#define COBJMACROS
#include "pack_core.h"
#include <shlobj.h>
#include <shobjidl.h>
#include <shellapi.h>
#include <commctrl.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>
#include <errno.h>

#define IDD_MAIN 102
#define IDC_PROJ 1001
#define IDC_INPUT 1002
#define IDC_LBL_PROJ 1003
#define IDC_LBL_INPUT 1004
#define IDC_STATUS 1005
#define IDC_NOPACK 1006
#define IDC_FILE 1007
#define IDC_OUTPUT 1008
#define IDC_LBL_OUTPUT 1009
#define IDC_OUT_BROWSE 1010
#define IDC_SCAN 1011
#define IDC_PROGRESS 1012
#define IDC_TIMEOUT 1013
#define IDC_LBL_TIMEOUT 1014
#define IDC_VERSION 1015
#define IDC_TITLE 1016
#define IDC_OPEN 1017
#define IDC_HINT 1018
#define IDC_SECONDS 1019
#define GUI_LIST_MAX 2000
#define HEADER_DLU 40 /* header/footer bands, must match app.rc */
#define FOOTER_DLU 180

enum { STATUS_INFO, STATUS_BUSY, STATUS_OK, STATUS_ERROR };

typedef struct {
    HWND dlg;
    HFONT title_font;
    HBRUSH line_brush;
    int header_bottom, footer_top; /* pixels */
    int high_contrast;
    int status_kind;
    wchar_t *last_result;
    HANDLE thread;
    HANDLE cancel;
    int busy; /* 1 scan, 2 pack */
    int close_requested;
    int output_custom;
    int setting_output;
    BitList *files;
    BitList *scan;
    PackJob *job;
    wchar_t *lastdir;
} GuiState;
static GuiState ui;
static HANDLE cli_cancel;
static HANDLE console_out;

static void print_text(const wchar_t *text)
{
    DWORD mode, done;
    if (!console_out || console_out == INVALID_HANDLE_VALUE) return;
    if (GetConsoleMode(console_out, &mode)) {
        WriteConsoleW(console_out, text, (DWORD)wcslen(text), &done, NULL);
    } else {
        int size = WideCharToMultiByte(CP_UTF8, 0, text, -1, NULL, 0, NULL, NULL);
        char *bytes = size > 0 ? malloc((size_t)size) : NULL;
        if (bytes) {
            WideCharToMultiByte(CP_UTF8, 0, text, -1, bytes, size, NULL, NULL);
            WriteFile(console_out, bytes, (DWORD)(size - 1), &done, NULL); free(bytes);
        }
    }
}
static void print_line(const wchar_t *text) { print_text(text); print_text(L"\r\n"); }
static BOOL WINAPI console_handler(DWORD event)
{
    if (event == CTRL_C_EVENT || event == CTRL_BREAK_EVENT || event == CTRL_CLOSE_EVENT) {
        if (cli_cancel) SetEvent(cli_cancel);
        return TRUE;
    }
    return FALSE;
}
static const wchar_t help_text[] =
L"Bit_pack " PACK_VERSION L" — sunny_fpga 安全打包工具\r\n"
L"用法：\r\n"
L"  pack_bit.exe                         图形界面\r\n"
L"  pack_bit.exe 项目号                   自动选择当前目录唯一的 .bit\r\n"
L"  pack_bit.exe 项目号 --input 文件.bit [--output 目录]\r\n"
L"  pack_bit.exe --nopack --input 文件.bit [--output 目录] [--force]\r\n"
L"选项：\r\n"
L"  --project 项目号                     与位置参数二选一；1–63 字符\r\n"
L"  --input / -i 文件                    可包含空格、中文、长路径\r\n"
L"  --output / -o 目录                   已存在目录；默认源文件所在目录\r\n"
L"  --engine auto|tar|powershell          默认 auto：tar 失败后回退\r\n"
L"  --timeout 秒                         整个任务超时，默认 300；范围 1–86400\r\n"
L"  --nopack                             仅复制为 sunny_fpga.bit，保留源文件\r\n"
L"  --force                              仅允许复制模式替换已有目标\r\n"
L"  --scan 目录                          递归扫描 .bit（供工作台使用）\r\n"
L"  --progress                           输出任务阶段（供工作台使用）\r\n"
L"  --cancel-event 名称                  监听已有 Windows 事件（供工作台使用）\r\n"
L"  --help / -h                          显示帮助\r\n"
L"  --version                            显示版本\r\n"
L"ZIP 内仅包含 sunny_fpga.bit；源文件永不删除；同名 ZIP 自动追加序号。\r\n"
L"路径有空格时用双引号包围。重定向输出编码为 UTF-8。\r\n"
L"退出码：0 成功，1 操作失败，2 参数错误，3 已取消，4 超时。";

static int parse_seconds(const wchar_t *s, DWORD *value)
{
    if (!s || !*s) return 0;
    for (const wchar_t *p = s; *p; ++p) if (*p < L'0' || *p > L'9') return 0;
    errno = 0; wchar_t *end;
    unsigned long v = wcstoul(s, &end, 10);
    if (errno || *end || v < 1 || v > 86400) return 0;
    *value = (DWORD)v; return 1;
}
static int run_cli(int argc, wchar_t **argv)
{
    PackJob *j = calloc(1, sizeof *j);
    if (!j) return PACK_FAILED;
    j->timeout_seconds = 300;
    const wchar_t *input = NULL, *output = NULL, *project = NULL, *cancel_event = NULL;
    int progress_set = 0;
    const wchar_t *scan_root = NULL;
    int engine_set = 0, timeout_set = 0, nopack_set = 0, force_set = 0;
    int rc = PACK_USAGE;
    for (int i = 1; i < argc; ++i) {
        const wchar_t *a = argv[i];
        if (!wcscmp(a, L"--help") || !wcscmp(a, L"-h") || !wcscmp(a, L"/?")) {
            if (argc != 2) goto bad;
            print_line(help_text); rc = PACK_OK; goto done;
        }
        if (!wcscmp(a, L"--version")) {
            if (argc != 2) goto bad;
            print_line(L"Bit_pack " PACK_VERSION); rc = PACK_OK; goto done;
        }
        if (!wcscmp(a, L"--scan")) { if (scan_root || ++i == argc || !*argv[i]) goto bad; scan_root = argv[i]; continue; }
        if (!wcscmp(a, L"--cancel-event")) { if (cancel_event || ++i == argc || !*argv[i]) goto bad; cancel_event = argv[i]; continue; }
        if (!wcscmp(a, L"--progress")) { if (progress_set++) goto bad; j->progress = print_line; continue; }
        if (!wcscmp(a, L"--nopack")) { if (nopack_set++) goto bad; j->nopack = 1; continue; }
        if (!wcscmp(a, L"--force")) { if (force_set++) goto bad; j->force = 1; continue; }
        if (!wcscmp(a, L"--input") || !wcscmp(a, L"-i")) { if (input || ++i == argc) goto bad; input = argv[i]; continue; }
        if (!wcscmp(a, L"--output") || !wcscmp(a, L"-o")) { if (output || ++i == argc) goto bad; output = argv[i]; continue; }
        if (!wcscmp(a, L"--project")) { if (project || ++i == argc) goto bad; project = argv[i]; continue; }
        if (!wcscmp(a, L"--engine")) {
            if (engine_set++ || ++i == argc) goto bad;
            if (!_wcsicmp(argv[i], L"auto")) j->engine = ENGINE_AUTO;
            else if (!_wcsicmp(argv[i], L"tar")) j->engine = ENGINE_TAR;
            else if (!_wcsicmp(argv[i], L"powershell")) j->engine = ENGINE_POWERSHELL;
            else goto bad;
            continue;
        }
        if (!wcscmp(a, L"--timeout")) { if (timeout_set++ || ++i == argc || !parse_seconds(argv[i], &j->timeout_seconds)) goto bad; continue; }
        if (a[0] == L'-' || project) goto bad;
        project = a;
    }
    if (scan_root) {
        if (input || output || project || engine_set || timeout_set || nopack_set || force_set || progress_set) goto bad;
        BitList *list = calloc(1, sizeof *list);
        if (!list) { print_line(L"[错误] 扫描内存不足。"); rc = PACK_FAILED; goto done; }
        list->root = pack_fullpath(scan_root);
        list->recursive = 1;
        cli_cancel = cancel_event ? OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, FALSE, cancel_event)
                                  : CreateEventW(NULL, TRUE, FALSE, NULL);
        if (!list->root || !cli_cancel) {
            print_line(L"[错误] 无法初始化目录扫描或取消事件。");
            rc = PACK_FAILED;
        } else {
            list->cancel = cli_cancel;
            SetConsoleCtrlHandler(console_handler, TRUE);
            rc = pack_scan(list);
            if (!rc) {
                for (size_t i = 0; i < list->count; ++i) {
                    print_text(L"FILE\t"); print_line(pack_display(list->items[i]));
                }
            } else print_line(list->error[0] ? list->error : L"目录扫描已取消。");
            wchar_t summary[160];
            swprintf(summary, 160, L"SCAN\t%llu\t%llu\t%d", (unsigned long long)list->count,
                     (unsigned long long)list->skipped, list->truncated);
            print_line(summary);
            SetConsoleCtrlHandler(console_handler, FALSE);
        }
        if (cli_cancel) CloseHandle(cli_cancel);
        cli_cancel = NULL;
        pack_list_free(list);
        goto done;
    }
    if ((!j->nopack && !pack_valid_project(project)) || (j->nopack && project) || (j->force && !j->nopack) || (j->nopack && engine_set)) goto bad;
    if (project) wcscpy(j->project, project);
    if (input) j->input = pack_fullpath(input);
    else {
        BitList *list = calloc(1, sizeof *list);
        if (!list) { rc = PACK_FAILED; goto done; }
        list->root = pack_fullpath(L".");
        if (!list->root || pack_scan(list)) { print_line(list->error); pack_list_free(list); rc = PACK_FAILED; goto done; }
        if (list->count != 1 || list->skipped || list->truncated) {
            print_line(L"[错误] 当前目录没有唯一可确认的 .bit 文件，请用 --input 明确指定源文件。");
            pack_list_free(list); rc = PACK_FAILED; goto done;
        }
        j->input = pack_dup(list->items[0]); pack_list_free(list);
    }
    if (!j->input) { print_line(L"[错误] 输入路径无效、过长或内存不足。"); rc = PACK_FAILED; goto done; }
    j->output_dir = output ? pack_fullpath(output) : pack_parent(j->input);
    if (!j->output_dir) { print_line(L"[错误] 输出路径无效、过长或内存不足。"); rc = PACK_FAILED; goto done; }
    cli_cancel = cancel_event ? OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, FALSE, cancel_event)
                              : CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!cli_cancel) { print_line(L"[错误] 无法创建取消事件。"); rc = PACK_FAILED; goto done; }
    j->cancel = cli_cancel;
    SetConsoleCtrlHandler(console_handler, TRUE);
    rc = pack_run(j);
    if (!rc) {
        print_line(j->nopack ? L"完成：复制成功，源文件保留。" : L"完成：ZIP 内容校验通过，源文件保留。");
        print_line(pack_display(j->result));
        wchar_t info[256];
        swprintf(info, 256, L"引擎：%ls；源文件：%llu 字节；耗时：%lu ms。", j->backend,
                 (unsigned long long)j->source_bytes, (unsigned long)j->elapsed_ms);
        print_line(info);
    } else print_line(j->error);
    if (j->warning[0]) { print_text(L"[警告] "); print_line(j->warning); }
    SetConsoleCtrlHandler(console_handler, FALSE);
    CloseHandle(cli_cancel); cli_cancel = NULL;
    goto done;
bad:
    print_line(L"[参数错误] 请检查项目号、选项及其值；使用 --help 查看用法。输入不会被截断。");
done:
    pack_job_free(j); return rc;
}

static wchar_t *control_text(HWND dlg, int id)
{
    HWND h = GetDlgItem(dlg, id);
    int length = GetWindowTextLengthW(h);
    if (length >= PACK_PATH_LIMIT) return NULL;
    wchar_t *r = calloc((size_t)length + 1, sizeof *r);
    if (r) GetWindowTextW(h, r, length + 1);
    return r;
}
static void save_browse_dir(const wchar_t *dir)
{
    HKEY key;
    if (RegCreateKeyExW(HKEY_CURRENT_USER, L"Software\\BitPackTool", 0, NULL, 0, KEY_SET_VALUE, NULL, &key, NULL) == ERROR_SUCCESS) {
        RegSetValueExW(key, L"LastBrowseDir", 0, REG_SZ, (const BYTE *)dir, (DWORD)((wcslen(dir) + 1) * sizeof *dir)); RegCloseKey(key);
    }
    free(ui.lastdir); ui.lastdir = pack_dup(dir);
}
static void load_browse_dir(void)
{
    DWORD bytes = 0;
    if (RegGetValueW(HKEY_CURRENT_USER, L"Software\\BitPackTool", L"LastBrowseDir", RRF_RT_REG_SZ, NULL, NULL, &bytes) != ERROR_SUCCESS || !bytes || bytes > PACK_PATH_LIMIT * sizeof(wchar_t)) return;
    wchar_t *value = calloc((size_t)bytes / sizeof(wchar_t) + 1, sizeof(wchar_t));
    if (!value) return;
    if (RegGetValueW(HKEY_CURRENT_USER, L"Software\\BitPackTool", L"LastBrowseDir", RRF_RT_REG_SZ, NULL, value, &bytes) == ERROR_SUCCESS)
        ui.lastdir = value;
    else free(value);
}
static wchar_t *browse_path(HWND owner, int folder, const wchar_t *title)
{
    IFileOpenDialog *dialog = NULL;
    wchar_t *result = NULL;
    HRESULT hr = CoCreateInstance(&CLSID_FileOpenDialog, NULL, CLSCTX_INPROC_SERVER, &IID_IFileOpenDialog, (void **)&dialog);
    if (FAILED(hr)) { MessageBoxW(owner, L"无法打开文件选择对话框。", L"错误", MB_OK | MB_ICONERROR); return NULL; }
    FILEOPENDIALOGOPTIONS options = 0;
    IFileOpenDialog_GetOptions(dialog, &options);
    options |= FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_DONTADDTORECENT;
    options |= folder ? FOS_PICKFOLDERS : FOS_FILEMUSTEXIST;
    IFileOpenDialog_SetOptions(dialog, options);
    IFileOpenDialog_SetTitle(dialog, title);
    if (!folder) {
        COMDLG_FILTERSPEC filter[] = { { L"FPGA bit 文件 (*.bit)", L"*.bit" } };
        IFileOpenDialog_SetFileTypes(dialog, 1, filter);
    }
    if (ui.lastdir) {
        IShellItem *item = NULL;
        if (SUCCEEDED(SHCreateItemFromParsingName(pack_display(ui.lastdir), NULL, &IID_IShellItem, (void **)&item))) {
            IFileOpenDialog_SetFolder(dialog, item); IShellItem_Release(item);
        }
    }
    if (SUCCEEDED(IFileOpenDialog_Show(dialog, owner))) {
        IShellItem *item = NULL;
        if (SUCCEEDED(IFileOpenDialog_GetResult(dialog, &item))) {
            wchar_t *path = NULL;
            if (SUCCEEDED(IShellItem_GetDisplayName(item, SIGDN_FILESYSPATH, &path))) {
                result = pack_fullpath(path); CoTaskMemFree(path);
            }
            IShellItem_Release(item);
        }
    }
    IFileOpenDialog_Release(dialog);
    if (result) {
        wchar_t *dir = folder ? pack_dup(result) : pack_parent(result);
        if (dir) { save_browse_dir(dir); free(dir); }
    }
    return result;
}
static void set_output(const wchar_t *text)
{
    ui.setting_output = 1;
    SetDlgItemTextW(ui.dlg, IDC_OUTPUT, text);
    ui.setting_output = 0;
}
static void auto_output(const wchar_t *source)
{
    if (ui.output_custom) return;
    wchar_t *parent = pack_parent(source);
    if (parent) { set_output(pack_display(parent)); free(parent); }
}
static void set_status(int kind, const wchar_t *text)
{
    ui.status_kind = kind;
    SetDlgItemTextW(ui.dlg, IDC_STATUS, text);
    InvalidateRect(GetDlgItem(ui.dlg, IDC_STATUS), NULL, TRUE);
}
static void set_busy(int busy)
{
    ui.busy = busy;
    const int ids[] = { IDC_PROJ, IDC_INPUT, IDC_OUTPUT, IDC_NOPACK, IDC_FILE, IDC_SCAN, IDC_OUT_BROWSE, IDC_TIMEOUT, IDOK };
    for (size_t i = 0; i < sizeof ids / sizeof ids[0]; ++i) EnableWindow(GetDlgItem(ui.dlg, ids[i]), !busy);
    if (!busy) EnableWindow(GetDlgItem(ui.dlg, IDC_PROJ), IsDlgButtonChecked(ui.dlg, IDC_NOPACK) != BST_CHECKED);
    EnableWindow(GetDlgItem(ui.dlg, IDCANCEL), TRUE);
    SetDlgItemTextW(ui.dlg, IDCANCEL, busy ? L"取消任务" : L"关闭");
    if (busy) ShowWindow(GetDlgItem(ui.dlg, IDC_OPEN), SW_HIDE);
    ShowWindow(GetDlgItem(ui.dlg, IDC_PROGRESS), busy ? SW_SHOWNA : SW_HIDE);
    SendDlgItemMessageW(ui.dlg, IDC_PROGRESS, PBM_SETMARQUEE, busy != 0, 30);
}
static int dlu_y(HWND dlg, int y)
{
    RECT r = { 0, y, 0, y }; MapDialogRect(dlg, &r); return r.top;
}
static void refresh_theme(void)
{
    HIGHCONTRASTW hc = { sizeof hc, 0, NULL };
    ui.high_contrast = SystemParametersInfoW(SPI_GETHIGHCONTRAST, sizeof hc, &hc, 0) && (hc.dwFlags & HCF_HIGHCONTRASTON);
}
/* White content, separator under the header, grey command footer (Windows dialog convention). */
static void paint_background(HWND dlg, HDC dc)
{
    RECT rc; GetClientRect(dlg, &rc);
    RECT body = rc, footer = rc, line = rc;
    body.bottom = footer.top = ui.footer_top;
    FillRect(dc, &body, GetSysColorBrush(COLOR_WINDOW));
    FillRect(dc, &footer, GetSysColorBrush(COLOR_BTNFACE));
    line.top = ui.footer_top; line.bottom = line.top + 1; FillRect(dc, &line, ui.line_brush);
    line.top = ui.header_bottom; line.bottom = line.top + 1; FillRect(dc, &line, ui.line_brush);
}
static INT_PTR control_color(HDC dc, HWND ctl, UINT msg)
{
    int id = GetDlgCtrlID(ctl);
    /* Disabled edits keep the system disabled look. */
    if (msg == WM_CTLCOLORSTATIC && (id == IDC_PROJ || id == IDC_OUTPUT || id == IDC_TIMEOUT)) return FALSE;
    int footer = id == IDC_HINT || id == IDOK || id == IDCANCEL;
    COLORREF color = GetSysColor(COLOR_WINDOWTEXT);
    if (id == IDC_TITLE) color = RGB(26, 26, 26);
    else if (id == IDC_VERSION || id == IDC_HINT) color = RGB(112, 112, 112);
    else if (id == IDC_STATUS) {
        static const COLORREF kinds[] = { RGB(68, 68, 68), RGB(0, 95, 184), RGB(16, 124, 16), RGB(196, 43, 28) };
        color = kinds[ui.status_kind];
    }
    SetTextColor(dc, color); SetBkMode(dc, TRANSPARENT);
    return (INT_PTR)GetSysColorBrush(footer ? COLOR_BTNFACE : COLOR_WINDOW);
}
static void open_output(void)
{
    if (!ui.last_result) return;
    PIDLIST_ABSOLUTE item = ILCreateFromPathW(pack_display(ui.last_result));
    if (item && SUCCEEDED(SHOpenFolderAndSelectItems(item, 0, NULL, 0))) { ILFree(item); return; }
    if (item) ILFree(item);
    wchar_t *dir = pack_parent(ui.last_result); /* fallback: open the folder only */
    if (dir) { ShellExecuteW(ui.dlg, L"open", pack_display(dir), NULL, NULL, SW_SHOWNORMAL); free(dir); }
}
static DWORD WINAPI scan_thread(void *data)
{
    BitList *list = data; pack_scan(list);
    PostMessageW(ui.dlg, WM_SCAN_DONE, 0, 0); return 0;
}
static DWORD WINAPI pack_thread(void *data)
{
    PackJob *job = data; int rc = pack_run(job);
    PostMessageW(job->notify, WM_PACK_DONE, (WPARAM)rc, 0); return 0;
}
static void start_scan(wchar_t *root, int recursive)
{
    ui.scan = calloc(1, sizeof *ui.scan);
    if (!ui.scan) { free(root); return; }
    ui.scan->root = root; ui.scan->recursive = recursive;
    ResetEvent(ui.cancel); ui.scan->cancel = ui.cancel;
    set_busy(1); set_status(STATUS_BUSY, L"正在扫描 .bit 文件（跳过目录联接和符号链接）…");
    ui.thread = CreateThread(NULL, 0, scan_thread, ui.scan, 0, NULL);
    if (!ui.thread) {
        pack_list_free(ui.scan); ui.scan = NULL; set_busy(0);
        set_status(STATUS_ERROR, L"启动扫描线程失败。");
    }
}
/* Inline field hint instead of a modal box; falls back to a message box. */
static void field_error(int id, const wchar_t *title, const wchar_t *text)
{
    HWND edit = GetDlgItem(ui.dlg, id);
    SendMessageW(ui.dlg, WM_NEXTDLGCTL, (WPARAM)edit, TRUE);
    SendMessageW(edit, EM_SETSEL, 0, -1);
    EDITBALLOONTIP tip = { sizeof tip, title, text, TTI_WARNING };
    if (!SendMessageW(edit, EM_SHOWBALLOONTIP, 0, (LPARAM)&tip)) MessageBoxW(ui.dlg, text, title, MB_OK | MB_ICONWARNING);
}
/* Widen the drop-down to the longest path (capped to the monitor) so deep run paths stay readable. */
static void fit_dropdown(HWND combo, const BitList *list, size_t visible)
{
    HDC dc = GetDC(combo);
    if (!dc) return;
    HGDIOBJ old = SelectObject(dc, (HFONT)SendMessageW(combo, WM_GETFONT, 0, 0));
    LONG widest = 0;
    for (size_t i = 0; i < visible; ++i) {
        const wchar_t *t = pack_display(list->items[i]); SIZE sz;
        if (GetTextExtentPoint32W(dc, t, (int)wcslen(t), &sz) && sz.cx > widest) widest = sz.cx;
    }
    SelectObject(dc, old); ReleaseDC(combo, dc);
    MONITORINFO info; ZeroMemory(&info, sizeof info); info.cbSize = sizeof info;
    widest += GetSystemMetrics(SM_CXVSCROLL) + 16;
    if (GetMonitorInfoW(MonitorFromWindow(combo, MONITOR_DEFAULTTONEAREST), &info) && widest > info.rcWork.right - info.rcWork.left)
        widest = info.rcWork.right - info.rcWork.left;
    SendMessageW(combo, CB_SETDROPPEDWIDTH, (WPARAM)widest, 0);
}
static void finish_thread(void)
{
    if (ui.thread) { WaitForSingleObject(ui.thread, INFINITE); CloseHandle(ui.thread); ui.thread = NULL; }
    set_busy(0);
}
static void start_pack(void)
{
    wchar_t *source = control_text(ui.dlg, IDC_INPUT);
    wchar_t *output = control_text(ui.dlg, IDC_OUTPUT);
    wchar_t *project = control_text(ui.dlg, IDC_PROJ);
    wchar_t *timeout = control_text(ui.dlg, IDC_TIMEOUT);
    PackJob *job = calloc(1, sizeof *job);
    if (!source || !output || !project || !timeout || !job) goto fail;
    job->input = pack_fullpath(source); job->output_dir = pack_fullpath(output);
    job->nopack = IsDlgButtonChecked(ui.dlg, IDC_NOPACK) == BST_CHECKED;
    if (!job->nopack && !pack_valid_project(project)) {
        field_error(IDC_PROJ, L"项目号无效", L"1–63 个字符，不能包含空白或 \\ / : * ? \" < > |"); goto release;
    }
    if (!parse_seconds(timeout, &job->timeout_seconds)) {
        field_error(IDC_TIMEOUT, L"超时无效", L"请输入 1–86400 之间的整数秒"); goto release;
    }
    if (!job->input || !job->output_dir) goto fail;
    if (!pack_valid_input(job->input, job->error, PACK_ERROR_CAP)) {
        MessageBoxW(ui.dlg, job->error, L"输入检查", MB_OK | MB_ICONWARNING); goto release;
    }
    if (!job->nopack) wcscpy(job->project, project);
    if (job->nopack) {
        wchar_t *dest = pack_join(job->output_dir, L"sunny_fpga.bit");
        if (!dest) goto fail;
        DWORD attr = GetFileAttributesW(dest);
        if (attr != INVALID_FILE_ATTRIBUTES && _wcsicmp(dest, job->input)) {
            if (MessageBoxW(ui.dlg, L"输出目录已存在 sunny_fpga.bit。是否替换？源文件仍会保留。", L"确认替换", MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2) != IDYES) { free(dest); goto release; }
            job->force = 1;
        }
        free(dest);
    }
    ResetEvent(ui.cancel); job->cancel = ui.cancel; job->notify = ui.dlg;
    ui.job = job; set_busy(2);
    set_status(STATUS_BUSY, L"正在准备任务…");
    ui.thread = CreateThread(NULL, 0, pack_thread, job, 0, NULL);
    if (!ui.thread) { ui.job = NULL; set_busy(0); goto fail; }
    job = NULL; goto release;
fail:
    MessageBoxW(ui.dlg, L"无法启动任务：路径过长、内存不足或线程创建失败。", L"错误", MB_OK | MB_ICONERROR);
release:
    pack_job_free(job); free(source); free(output); free(project); free(timeout);
}
static void center_dialog(HWND dlg)
{
    POINT cursor; GetCursorPos(&cursor);
    HMONITOR monitor = MonitorFromPoint(cursor, MONITOR_DEFAULTTONEAREST);
    MONITORINFO info; ZeroMemory(&info, sizeof info); info.cbSize = sizeof info;
    RECT r; GetWindowRect(dlg, &r);
    if (GetMonitorInfoW(monitor, &info)) {
        int w = r.right - r.left, h = r.bottom - r.top;
        int x = info.rcWork.left + (info.rcWork.right - info.rcWork.left - w) / 2;
        int y = info.rcWork.top + (info.rcWork.bottom - info.rcWork.top - h) / 2;
        SetWindowPos(dlg, NULL, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
    }
}
static INT_PTR CALLBACK dialog_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_INITDIALOG: {
        ui.dlg = dlg;
        ui.cancel = CreateEventW(NULL, TRUE, FALSE, NULL);
        if (!ui.cancel) { EndDialog(dlg, -1); return TRUE; }
        HICON large = LoadImageW(GetModuleHandleW(NULL), MAKEINTRESOURCEW(101), IMAGE_ICON, 32, 32, LR_SHARED);
        HICON small = LoadImageW(GetModuleHandleW(NULL), MAKEINTRESOURCEW(101), IMAGE_ICON, 16, 16, LR_SHARED);
        SendMessageW(dlg, WM_SETICON, ICON_BIG, (LPARAM)large);
        SendMessageW(dlg, WM_SETICON, ICON_SMALL, (LPARAM)small);
        SetWindowTextW(dlg, L"Bit_pack " PACK_VERSION L" — FPGA 安全打包工具");
        refresh_theme();
        ui.header_bottom = dlu_y(dlg, HEADER_DLU); ui.footer_top = dlu_y(dlg, FOOTER_DLU);
        ui.line_brush = CreateSolidBrush(RGB(223, 223, 223));
        LOGFONTW lf;
        if (GetObjectW((HFONT)SendMessageW(dlg, WM_GETFONT, 0, 0), sizeof lf, &lf)) {
            lf.lfHeight = lf.lfHeight * 3 / 2; lf.lfWeight = FW_BOLD;
            ui.title_font = CreateFontIndirectW(&lf);
            if (ui.title_font) SendDlgItemMessageW(dlg, IDC_TITLE, WM_SETFONT, (WPARAM)ui.title_font, FALSE);
        }
        SetDlgItemTextW(dlg, IDC_TITLE, L"FPGA Bit 打包");
        SetDlgItemTextW(dlg, IDC_VERSION, L"源文件始终保留 · ZIP 自动防重名 · 发布前校验内容 · v" PACK_VERSION);
        SetDlgItemTextW(dlg, IDC_LBL_PROJ, L"项目号(&P)：");
        SetDlgItemTextW(dlg, IDC_LBL_INPUT, L"源文件(&I)：");
        SetDlgItemTextW(dlg, IDC_LBL_OUTPUT, L"输出目录(&O)：");
        SetDlgItemTextW(dlg, IDC_FILE, L"选文件(&F)…");
        SetDlgItemTextW(dlg, IDC_SCAN, L"扫描目录(&D)…");
        SetDlgItemTextW(dlg, IDC_OUT_BROWSE, L"浏览(&B)…");
        SetDlgItemTextW(dlg, IDC_NOPACK, L"仅复制为 sunny_fpga.bit，不压缩(&C)");
        SetDlgItemTextW(dlg, IDC_LBL_TIMEOUT, L"超时(&T)：");
        SetDlgItemTextW(dlg, IDC_SECONDS, L"秒");
        SetDlgItemTextW(dlg, IDC_TIMEOUT, L"300");
        SetDlgItemTextW(dlg, IDC_HINT, L"提示：可将 .bit 文件或文件夹直接拖入窗口");
        SetDlgItemTextW(dlg, IDC_OPEN, L"<a>在资源管理器中显示输出文件</a>");
        SetDlgItemTextW(dlg, IDOK, L"开始打包");
        SetDlgItemTextW(dlg, IDCANCEL, L"关闭");
        SendDlgItemMessageW(dlg, IDC_PROJ, EM_SETCUEBANNER, TRUE, (LPARAM)L"例如 A100");
        SendDlgItemMessageW(dlg, IDC_INPUT, CB_SETCUEBANNER, 0, (LPARAM)L"选择、粘贴或拖入 .bit 文件");
        SendDlgItemMessageW(dlg, IDC_OUTPUT, EM_SETCUEBANNER, TRUE, (LPARAM)L"已存在的输出目录");
        SendDlgItemMessageW(dlg, IDC_PROJ, EM_SETLIMITTEXT, 1024, 0); /* reject, never silently truncate */
        SendDlgItemMessageW(dlg, IDC_INPUT, CB_LIMITTEXT, PACK_PATH_LIMIT - 1, 0);
        SendDlgItemMessageW(dlg, IDC_OUTPUT, EM_SETLIMITTEXT, PACK_PATH_LIMIT - 1, 0);
        SendDlgItemMessageW(dlg, IDC_TIMEOUT, EM_SETLIMITTEXT, 10, 0);
        load_browse_dir(); center_dialog(dlg); DragAcceptFiles(dlg, TRUE);
        wchar_t *cwd = pack_fullpath(L".");
        if (cwd) { set_output(pack_display(cwd)); start_scan(cwd, 0); }
        return TRUE;
    }
    case WM_ERASEBKGND:
    case WM_PRINTCLIENT: /* themed buttons in the footer ask for the parent background */
        if (ui.high_contrast) return FALSE;
        paint_background(dlg, (HDC)wp);
        SetWindowLongPtrW(dlg, DWLP_MSGRESULT, TRUE); return TRUE;
    case WM_CTLCOLORDLG:
        return ui.high_contrast ? FALSE : (INT_PTR)GetSysColorBrush(COLOR_WINDOW);
    case WM_CTLCOLORSTATIC:
    case WM_CTLCOLORBTN:
        return ui.high_contrast ? FALSE : control_color((HDC)wp, (HWND)lp, msg);
    case WM_SYSCOLORCHANGE:
    case WM_SETTINGCHANGE:
        refresh_theme(); InvalidateRect(dlg, NULL, TRUE); return FALSE;
    case WM_NOTIFY: {
        const NMHDR *n = (const NMHDR *)lp;
        if (n->idFrom == IDC_OPEN && (n->code == NM_CLICK || n->code == NM_RETURN)) { open_output(); return TRUE; }
        return FALSE;
    }
    case WM_PACK_STATUS:
        set_status(STATUS_BUSY, (const wchar_t *)lp); free((void *)lp); return TRUE;
    case WM_SCAN_DONE: {
        finish_thread();
        BitList *list = ui.scan; ui.scan = NULL;
        if (ui.close_requested) { pack_list_free(list); EndDialog(dlg, IDCANCEL); return TRUE; }
        if (list->status == PACK_CANCELLED) { pack_list_free(list); set_status(STATUS_INFO, L"已取消扫描，原选择保持不变。"); return TRUE; }
        if (list->status) { set_status(STATUS_ERROR, list->error); MessageBoxW(dlg, list->error, L"扫描失败", MB_OK | MB_ICONERROR); pack_list_free(list); return TRUE; }
        pack_list_free(ui.files); ui.files = list;
        SendDlgItemMessageW(dlg, IDC_INPUT, CB_RESETCONTENT, 0, 0);
        size_t visible = list->count < GUI_LIST_MAX ? list->count : GUI_LIST_MAX;
        SendDlgItemMessageW(dlg, IDC_INPUT, CB_INITSTORAGE, (WPARAM)visible, (LPARAM)(visible * 512));
        for (size_t i = 0; i < visible; ++i) SendDlgItemMessageW(dlg, IDC_INPUT, CB_ADDSTRING, 0, (LPARAM)pack_display(list->items[i]));
        fit_dropdown(GetDlgItem(dlg, IDC_INPUT), list, visible);
        if (list->count) {
            SendDlgItemMessageW(dlg, IDC_INPUT, CB_SETCURSEL, 0, 0);
            auto_output(list->items[0]);
        }
        wchar_t text[512], skipped[96] = L"";
        if (list->skipped) swprintf(skipped, 96, L"（跳过 %llu 项：联接、权限或深度限制）", (unsigned long long)list->skipped);
        if (!list->count) swprintf(text, 512, L"未找到 .bit 文件%ls。请点“选文件”或“扫描目录”，也可直接拖入。", skipped);
        else swprintf(text, 512, L"找到 %llu 个 .bit 文件%ls%ls。%ls", (unsigned long long)list->count, skipped,
                      list->truncated ? L"，已达 100000 上限，结果不完整" : L"",
                      list->count > GUI_LIST_MAX ? L"下拉框仅显示前 2000 项，其它文件可粘贴完整路径。" : L"请确认源文件和输出目录。");
        set_status(STATUS_INFO, text);
        HWND project = GetDlgItem(dlg, IDC_PROJ);
        if (IsWindowEnabled(project) && !GetWindowTextLengthW(project)) SendMessageW(dlg, WM_NEXTDLGCTL, (WPARAM)project, TRUE);
        return TRUE;
    }
    case WM_PACK_DONE: {
        finish_thread();
        PackJob *j = ui.job; ui.job = NULL;
        int rc = (int)wp;
        if (!ui.close_requested) {
            if (!rc) {
                free(ui.last_result); ui.last_result = pack_dup(j->result);
                const wchar_t *shown = pack_display(j->result), *name = wcsrchr(shown, L'\\');
                size_t scap = wcslen(shown) + 160;
                wchar_t *done = malloc(scap * sizeof *done);
                if (done) {
                    swprintf(done, scap, L"%ls：%ls（%ls，%lu ms）", j->nopack ? L"复制完成" : L"打包完成",
                             name ? name + 1 : shown, j->backend, (unsigned long)j->elapsed_ms);
                    set_status(j->warning[0] ? STATUS_INFO : STATUS_OK, done); free(done);
                }
                if (ui.last_result) ShowWindow(GetDlgItem(dlg, IDC_OPEN), SW_SHOWNA);
                size_t cap = wcslen(pack_display(j->result)) + wcslen(j->warning) + 512;
                wchar_t *text = malloc(cap * sizeof *text);
                if (text) {
                    swprintf(text, cap, L"%ls\n输出：%ls\n源文件保留；引擎：%ls；耗时：%lu ms。%ls%ls",
                             j->nopack ? L"复制完成，内容校验通过。" : L"打包完成，ZIP 内容校验通过。",
                             pack_display(j->result), j->backend, (unsigned long)j->elapsed_ms,
                             j->warning[0] ? L"\n警告：" : L"", j->warning);
                    MessageBoxW(dlg, text, L"完成", MB_OK | (j->warning[0] ? MB_ICONWARNING : MB_ICONINFORMATION)); free(text);
                }
            } else {
                set_status(rc == PACK_CANCELLED ? STATUS_INFO : STATUS_ERROR, j->error);
                if (rc != PACK_CANCELLED) MessageBoxW(dlg, j->error, L"任务未完成", MB_OK | MB_ICONERROR);
            }
        }
        pack_job_free(j);
        if (ui.close_requested) EndDialog(dlg, IDCANCEL);
        return TRUE;
    }
    case WM_DROPFILES: {
        HDROP drop = (HDROP)wp;
        if (!ui.busy && DragQueryFileW(drop, 0xFFFFFFFF, NULL, 0) == 1) {
            UINT n = DragQueryFileW(drop, 0, NULL, 0);
            wchar_t *path = malloc(((size_t)n + 1) * sizeof *path);
            if (path) {
                DragQueryFileW(drop, 0, path, n + 1);
                wchar_t *full = pack_fullpath(path); free(path);
                if (full) {
                    wchar_t error[PACK_ERROR_CAP];
                    DWORD attr = GetFileAttributesW(full);
                    if (attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY)) start_scan(full, 1);
                    else {
                        if (pack_valid_input(full, error, PACK_ERROR_CAP)) { SetDlgItemTextW(dlg, IDC_INPUT, pack_display(full)); auto_output(full); }
                        else MessageBoxW(dlg, error, L"输入检查", MB_OK | MB_ICONWARNING);
                        free(full);
                    }
                }
            }
        }
        DragFinish(drop); return TRUE;
    }
    case WM_COMMAND: {
        int id = LOWORD(wp), event = HIWORD(wp);
        if (id == IDC_OUTPUT && event == EN_CHANGE && !ui.busy && !ui.setting_output) ui.output_custom = 1;
        if (id == IDC_INPUT && event == CBN_EDITCHANGE && !ui.busy && !ui.output_custom) {
            wchar_t *text = control_text(dlg, IDC_INPUT);
            const wchar_t *dot = text ? wcsrchr(text, L'.') : NULL;
            if (dot && !_wcsicmp(dot, L".bit")) {
                wchar_t *path = pack_fullpath(text);
                if (path) { auto_output(path); free(path); }
            }
            free(text);
        }
        if (id == IDC_INPUT && event == CBN_SELCHANGE && !ui.busy && ui.files) {
            LRESULT sel = SendDlgItemMessageW(dlg, IDC_INPUT, CB_GETCURSEL, 0, 0);
            if (sel >= 0 && (size_t)sel < ui.files->count) auto_output(ui.files->items[sel]);
        }
        if (id == IDCANCEL) {
            if (ui.busy) { SetEvent(ui.cancel); EnableWindow(GetDlgItem(dlg, IDCANCEL), FALSE); set_status(STATUS_BUSY, L"正在取消并清理本次任务…"); }
            else EndDialog(dlg, IDCANCEL);
            return TRUE;
        }
        if (ui.busy) return FALSE;
        switch (id) {
        case IDC_FILE: {
            wchar_t *path = browse_path(dlg, 0, L"选择 FPGA .bit 文件");
            if (path) { SetDlgItemTextW(dlg, IDC_INPUT, pack_display(path)); auto_output(path); free(path); }
            return TRUE;
        }
        case IDC_SCAN: {
            wchar_t *root = browse_path(dlg, 1, L"选择扫描目录（包含子目录）");
            if (root) start_scan(root, 1);
            return TRUE;
        }
        case IDC_OUT_BROWSE: {
            wchar_t *path = browse_path(dlg, 1, L"选择输出目录");
            if (path) { ui.output_custom = 1; set_output(pack_display(path)); free(path); }
            return TRUE;
        }
        case IDC_NOPACK:
            EnableWindow(GetDlgItem(dlg, IDC_PROJ), IsDlgButtonChecked(dlg, IDC_NOPACK) != BST_CHECKED);
            SetDlgItemTextW(dlg, IDOK, IsDlgButtonChecked(dlg, IDC_NOPACK) == BST_CHECKED ? L"开始复制" : L"开始打包"); return TRUE;
        case IDOK: start_pack(); return TRUE;
        }
        break;
    }
    case WM_CLOSE:
        if (ui.busy) { ui.close_requested = 1; SetEvent(ui.cancel); set_status(STATUS_BUSY, L"正在取消并清理，完成后关闭…"); }
        else EndDialog(dlg, IDCANCEL);
        return TRUE;
    case WM_DESTROY:
        DragAcceptFiles(dlg, FALSE);
        if (ui.title_font) { DeleteObject(ui.title_font); ui.title_font = NULL; }
        if (ui.line_brush) { DeleteObject(ui.line_brush); ui.line_brush = NULL; }
        /* Drain heap-backed status messages after a close, before HWND reuse. */
        { MSG pending; while (PeekMessageW(&pending, dlg, WM_PACK_STATUS, WM_PACK_STATUS, PM_REMOVE)) free((void *)pending.lParam); }
        return TRUE;
    }
    return FALSE;
}
int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR cmdline, int show)
{
    (void)previous; (void)cmdline; (void)show;
    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) return PACK_FAILED;
    if (argc > 1) {
        /* Preserve redirection. Attach only if stdout isn't already a valid handle. */
        console_out = GetStdHandle(STD_OUTPUT_HANDLE);
        if (!console_out || console_out == INVALID_HANDLE_VALUE) {
            AttachConsole(ATTACH_PARENT_PROCESS); console_out = GetStdHandle(STD_OUTPUT_HANDLE);
        }
        int rc = run_cli(argc, argv); LocalFree(argv); return rc;
    }
    LocalFree(argv);
    HRESULT hr = CoInitializeEx(NULL, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
    if (FAILED(hr)) { MessageBoxW(NULL, L"初始化文件选择组件失败。", L"错误", MB_OK | MB_ICONERROR); return PACK_FAILED; }
    INITCOMMONCONTROLSEX controls = { sizeof controls, ICC_PROGRESS_CLASS | ICC_STANDARD_CLASSES | ICC_LINK_CLASS };
    InitCommonControlsEx(&controls);
    INT_PTR r = DialogBoxParamW(instance, MAKEINTRESOURCEW(IDD_MAIN), NULL, dialog_proc, 0);
    if (r == -1) {
        wchar_t error[PACK_ERROR_CAP];
        pack_system_error(error, PACK_ERROR_CAP, L"无法创建主窗口", GetLastError());
        MessageBoxW(NULL, error, L"错误", MB_OK | MB_ICONERROR);
    }
    pack_list_free(ui.files); pack_list_free(ui.scan); pack_job_free(ui.job); free(ui.lastdir); free(ui.last_result);
    if (ui.cancel) CloseHandle(ui.cancel);
    CoUninitialize();
    return r == -1 ? PACK_FAILED : PACK_OK;
}
