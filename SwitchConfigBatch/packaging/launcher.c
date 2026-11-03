#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <wchar.h>
#include <stdlib.h>

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, LPWSTR args, int show) {
    (void)instance; (void)previous; (void)show;
    wchar_t root[32768], python[32768], script[32768];
    DWORD n = GetModuleFileNameW(NULL, root, 32768);
    if (!n || n >= 32768) return 1;
    wchar_t *last = wcsrchr(root, L'\\');
    if (!last) return 1;
    *last = 0;
    if (wcslen(root) > 32000) return 1;
    swprintf(python, 32768, L"%ls\\runtime\\pythonw.exe", root);
    swprintf(script, 32768, L"%ls\\application\\windows_entry.py", root);
    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES || GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES) {
        MessageBoxW(NULL, L"运行库不完整。请先完整解压ZIP，保持EXE、runtime和application目录在一起。", L"SwitchConfigBatch", MB_ICONERROR);
        return 2;
    }
    size_t length = wcslen(python) + wcslen(script) + wcslen(args) + 12;
    if (length >= 32767) return 3;
    wchar_t *command = calloc(length, sizeof(wchar_t));
    if (!command) return 4;
    swprintf(command, length, L"\"%ls\" \"%ls\" %ls", python, script, args);
    STARTUPINFOW startup = {0}; startup.cb = sizeof(startup);
    PROCESS_INFORMATION process = {0};
    BOOL ok = CreateProcessW(python, command, NULL, NULL, FALSE, CREATE_UNICODE_ENVIRONMENT, NULL, root, &startup, &process);
    free(command);
    if (!ok) {
        wchar_t message[1024];
        swprintf(message, 1024, L"启动运行时失败，Windows错误代码：%lu。请确认使用64位Windows并已完整解压。", GetLastError());
        MessageBoxW(NULL, message, L"SwitchConfigBatch", MB_ICONERROR);
        return 5;
    }
    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hProcess);
    if (code != 0) {
        MessageBoxW(NULL, L"程序未正常结束。请查看 %LOCALAPPDATA%\\SwitchConfigBatch\\startup.log。若没有日志，请检查运行库是否齐全。", L"SwitchConfigBatch", MB_ICONERROR);
    }
    return (int)code;
}
