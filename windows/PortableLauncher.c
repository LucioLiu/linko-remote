#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)instance;
    (void)previous;
    (void)arguments;
    (void)show;
    static wchar_t root[32768], python[32768];
    wchar_t command[] = L"pythonw.exe -B -m lanpilot.peer_client --portable";
    DWORD length = GetModuleFileNameW(NULL, root, ARRAYSIZE(root));
    wchar_t *separator = wcsrchr(root, L'\\');
    const wchar_t runtime[] = L"\\runtime\\pythonw.exe";

    if (length && length < ARRAYSIZE(root) && separator &&
        (size_t)(separator - root) + ARRAYSIZE(runtime) <= ARRAYSIZE(python)) {
        *separator = L'\0';
        wcscpy(python, root);
        wcscat(python, runtime);
        STARTUPINFOW startup = {0};
        PROCESS_INFORMATION child = {0};
        startup.cb = sizeof(startup);
        if (CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, root, &startup, &child)) {
            CloseHandle(child.hThread);
            CloseHandle(child.hProcess);
            return 0;
        }
    }
    MessageBoxW(NULL, L"请先完整解压 ZIP，再双击打开邻控试用版。", L"邻控便携试用版", MB_OK | MB_ICONERROR);
    return 1;
}
