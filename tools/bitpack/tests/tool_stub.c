#ifndef UNICODE
#define UNICODE
#endif
#define _UNICODE
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
/* A bounded, controllable child process for timeout, cancellation and stderr tests. */
int wmain(int argc, wchar_t **argv)
{
    if (argc > 1 && !wcscmp(argv[1], L"sleep")) { Sleep(30000); return 0; }
    if (argc > 1 && !wcscmp(argv[1], L"fail")) { fputs("controlled child failure\n", stderr); return 7; }
    if (argc > 1 && !wcscmp(argv[1], L"descendant")) {
        wchar_t cmd[32768]; GetModuleFileNameW(NULL, cmd, 32768);
        wchar_t line[32768]; swprintf(line, 32768, L"\"%ls\" sleep", cmd);
        STARTUPINFOW si = {0}; PROCESS_INFORMATION pi = {0}; si.cb = sizeof si;
        if (!CreateProcessW(cmd, line, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi)) return 8;
        wprintf(L"%lu\n", (unsigned long)pi.dwProcessId); fflush(stdout);
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess); Sleep(30000); return 0;
    }
    return 9;
}
