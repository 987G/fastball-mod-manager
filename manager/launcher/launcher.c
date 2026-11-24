// Show Mod Manager.exe: starts the manager (`pythonw -m app`) from the folder this exe sits in, with no console
// window. The Python bundled with a release (python\pythonw.exe beside this exe) is used when it is there; a dev
// copy has none and uses pythonw.exe on PATH. If the manager is already running, `python -m app` only shows its
// window.
// Built by launcher/build.py with the setup's llvm-mingw clang.
#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <wchar.h>

static int fail(const wchar_t *text) {
    MessageBoxW(NULL, text, L"Show Mod Manager", MB_OK | MB_ICONERROR);
    return 1;
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR args, int show) {
    (void)inst, (void)prev, (void)args, (void)show;
    wchar_t dir[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return fail(L"Could not find this program's folder.");
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (!slash) return fail(L"Could not find this program's folder.");
    *slash = 0;

    wchar_t app[MAX_PATH];
    swprintf(app, MAX_PATH, L"%ls\\app\\__main__.py", dir);
    if (GetFileAttributesW(app) == INVALID_FILE_ATTRIBUTES)
        return fail(L"This program must stay in the manager folder (next to the \"app\" folder).");

    wchar_t py[MAX_PATH];
    swprintf(py, MAX_PATH, L"%ls\\python\\pythonw.exe", dir);
    if (GetFileAttributesW(py) == INVALID_FILE_ATTRIBUTES &&
        !SearchPathW(NULL, L"pythonw.exe", NULL, MAX_PATH, py, NULL))
        return fail(L"Python 3 (pythonw.exe) was not found: neither in the \"python\" folder here nor on PATH. "
                    L"Download the manager again, or install Python 3 and add it to PATH, then try again.");

    wchar_t cmd[2 * MAX_PATH + 32];
    swprintf(cmd, sizeof cmd / sizeof cmd[0], L"\"%ls\" -m app", py);
    STARTUPINFOW si = {sizeof si};
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(py, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, dir, &si, &pi))
        return fail(L"Could not start the manager (pythonw -m app).");
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
