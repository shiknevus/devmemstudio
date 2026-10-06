#ifndef PACK_CORE_H
#define PACK_CORE_H
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <stddef.h>
#define PACK_VERSION L"2.0.1"
#define PROJECT_MAX 63
#define PACK_ERROR_CAP 2048
#define PACK_PATH_LIMIT 32760
#define PACK_SCAN_LIMIT 100000
#define PACK_SCAN_DEPTH 128
#define PACK_OK 0
#define PACK_FAILED 1
#define PACK_USAGE 2
#define PACK_CANCELLED 3
#define PACK_TIMEOUT 4

typedef enum { ENGINE_AUTO, ENGINE_TAR, ENGINE_POWERSHELL } PackEngine;
typedef struct {
    wchar_t *input;
    wchar_t *output_dir;
    wchar_t project[PROJECT_MAX + 1];
    int nopack;
    int force;
    PackEngine engine;
    DWORD timeout_seconds;
    HANDLE cancel;
    HWND notify;
    void (*progress)(const wchar_t *text); /* optional CLI stage callback */
    ULONGLONG started;
    wchar_t *result;
    wchar_t error[PACK_ERROR_CAP];
    wchar_t warning[PACK_ERROR_CAP];
    wchar_t backend[32];
    ULONGLONG source_bytes;
    DWORD elapsed_ms;
} PackJob;
typedef struct {
    wchar_t **items;
    size_t count;
    size_t capacity;
    size_t skipped;
    int truncated;
    int recursive;
    int status;
    wchar_t error[PACK_ERROR_CAP];
    wchar_t *root;
    HANDLE cancel;
} BitList;
#define WM_PACK_STATUS (WM_APP + 10)
#define WM_PACK_DONE (WM_APP + 11)
#define WM_SCAN_DONE (WM_APP + 12)

wchar_t *pack_dup(const wchar_t *s);
wchar_t *pack_join(const wchar_t *dir, const wchar_t *name);
wchar_t *pack_fullpath(const wchar_t *path);
wchar_t *pack_parent(const wchar_t *path);
const wchar_t *pack_display(const wchar_t *path); /* UNC extended paths are displayed verbatim. */
int pack_valid_project(const wchar_t *s);
int pack_valid_input(const wchar_t *path, wchar_t *error, size_t cap);
void pack_system_error(wchar_t *error, size_t cap, const wchar_t *action, DWORD code);
int pack_run(PackJob *job);
int pack_scan(BitList *list);
void pack_list_free(BitList *list);
void pack_job_free(PackJob *job);
#endif
