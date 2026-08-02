# -*- coding: utf-8 -*-
"""
TrayHider - 隐藏 / 恢复 Windows 系统托盘图标的小工具。
只隐藏托盘图标，不会关闭图标对应的程序。

双引擎:
  - 经典托盘 (Win10 / 旧版 Win11): 通过 ToolbarWindow32 的 TB_HIDEBUTTON 可逆隐藏
  - 新版托盘 (Win11 22H2+): 通过 Shell_NotifyIconGetRect 探测图标,
    NIM_DELETE 隐藏, 恢复时广播 TaskbarCreated 让程序重新注册图标

特性:
  - 列出当前托盘里的小程序，可选择隐藏 / 恢复显示
  - 关闭窗口不退出程序，任务栏和托盘中均不显示
  - 再次双击 exe 重新打开窗口（单实例）
  - 开机自启（首次运行自动写入注册表 Run 键，带 --quiet 参数，开机不弹窗）
  - 隐藏名单持久化，重启后继续生效；目标程序稍后出现时会被自动隐藏
  - 任何异常都不会弹窗，仅静默写入 error.log
"""
import ctypes
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
import uuid
import winreg
from ctypes import wintypes

APP_NAME = "TrayHider"
WINDOW_TITLE = "TrayHider 托盘图标管理器"
MUTEX_NAME = "Global\\TrayHider_Mutex_7F3A9C21"
IPC_HOST, IPC_PORT = "127.0.0.1", 47313
POLL_INTERVAL = 2.0
FULL_SCAN_INTERVAL = 15.0
UID_SCAN_RANGE = range(6)
IS64 = ctypes.sizeof(ctypes.c_void_p) == 8

# ---------------------------------------------------------------- 路径 / 日志

def _data_dir():
    try:
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return os.path.dirname(os.path.abspath(sys.argv[0]))

CONFIG_PATH = os.path.join(_data_dir(), "config.json")
LOG_PATH = os.path.join(_data_dir(), "error.log")

def log_error(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg) + "\n")
    except Exception:
        pass

def _excepthook(exc_type, exc, tb):
    log_error("".join(traceback.format_exception(exc_type, exc, tb)))

sys.excepthook = _excepthook  # 绝不弹窗

# ---------------------------------------------------------------- Win32 定义
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.EnumWindows.argtypes = [ctypes.c_void_p, LPARAM]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

WM_USER = 0x400
TB_HIDEBUTTON = WM_USER + 4
TB_GETBUTTON = WM_USER + 23
TB_BUTTONCOUNT = WM_USER + 24
TBSTATE_HIDDEN = 0x08

MEM_RESERVE, MEM_COMMIT, MEM_RELEASE = 0x2000, 0x1000, 0x8000
PAGE_READWRITE = 0x04
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
HWND_BROADCAST = 0xFFFF
NIM_DELETE = 2

TASKBAR_CREATED_MSG = user32.RegisterWindowMessageW("TaskbarCreated")
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, LPARAM)

class TBBUTTON(ctypes.Structure):
    _fields_ = [
        ("iBitmap", wintypes.INT),
        ("idCommand", wintypes.INT),
        ("fsState", wintypes.BYTE),
        ("fsStyle", wintypes.BYTE),
        ("bReserved", wintypes.BYTE * (6 if IS64 else 2)),
        ("dwData", ctypes.c_void_p),
        ("iString", ctypes.c_void_p),
    ]

class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("guidItem", ctypes.c_byte * 16)]

class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uTimeout", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]

Shell_NotifyIconGetRect = shell32.Shell_NotifyIconGetRect
Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wintypes.RECT)]
Shell_NotifyIconGetRect.restype = ctypes.c_long
Shell_NotifyIconW = shell32.Shell_NotifyIconW
Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]

shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD,
                                         wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
shell32.SHGetKnownFolderPath.restype = ctypes.c_long

def expand_known_folder(path):
    """把注册表里的 '{KNOWNFOLDER-GUID}\\sub\\app.exe' 展开为绝对路径。"""
    m = re.match(r"^\{([0-9A-Fa-f-]{36})\}\\(.*)$", path or "")
    if not m:
        return path
    try:
        g = _GUID.from_buffer_copy(uuid.UUID(m.group(1)).bytes_le)
        raw = ctypes.c_void_p()
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(raw))
        if hr == 0 and raw.value:
            base = ctypes.wstring_at(raw.value)
            ctypes.windll.ole32.CoTaskMemFree(raw)
            return base + "\\" + m.group(2)
    except Exception as e:
        log_error("expand_known_folder: " + repr(e))
    return path

def get_registry_uid_map():
    """从 HKCU\\Control Panel\\NotifyIconSettings 读取 {exe路径(小写): {uID, ...}}。
    托盘图标的 uID 由程序自定（如 OneDrive=501、Outlook=12345），
    Windows 会记录在这里；用于补全小范围扫描探不到的高 uID 图标。"""
    result = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Control Panel\NotifyIconSettings") as base:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(base, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(base, name) as sub:
                        uid = winreg.QueryValueEx(sub, "UID")[0]
                        path = winreg.QueryValueEx(sub, "ExecutablePath")[0]
                except OSError:
                    continue
                if not isinstance(uid, int) or not isinstance(path, str) or not path:
                    continue
                full = expand_known_folder(path)
                if full:
                    result.setdefault(full.lower(), set()).add(uid)
    except OSError as e:
        log_error("uid map: " + repr(e))
    return result

# ================================================================ 引擎 A: 经典托盘工具栏

def find_toolbars():
    """找到通知区域与溢出区里的 ToolbarWindow32 句柄（Win10/旧版 Win11）。"""
    tbs = []
    try:
        h_tray = user32.FindWindowW("Shell_TrayWnd", None)
        h_notify = user32.FindWindowExW(h_tray, 0, "TrayNotifyWnd", None) if h_tray else 0
        h_pager = user32.FindWindowExW(h_notify, 0, "SysPager", None) if h_notify else 0
        h_tb = user32.FindWindowExW(h_pager, 0, "ToolbarWindow32", None) if h_pager else 0
        if h_tb:
            tbs.append(h_tb)
        h_ov = user32.FindWindowW("NotifyIconOverflowWindow", None)
        if h_ov:
            h_tb2 = user32.FindWindowExW(h_ov, 0, "ToolbarWindow32", None)
            if h_tb2:
                tbs.append(h_tb2)
    except Exception as e:
        log_error("find_toolbars: " + repr(e))
    return tbs

def _read_traydata(hproc, addr):
    try:
        raw = (ctypes.c_ubyte * 64)()
        n = ctypes.c_size_t(0)
        if not kernel32.ReadProcessMemory(hproc, addr, raw, 64, ctypes.byref(n)) or n.value < 12:
            return None
        b = bytes(raw)
        hwnd = int.from_bytes(b[0:8], "little")
        uid = int.from_bytes(b[8:12], "little")
        if not hwnd:
            return None
        tip = ""
        try:
            buf = (ctypes.c_wchar * 128)()
            n2 = ctypes.c_size_t(0)
            if kernel32.ReadProcessMemory(hproc, addr + 32, buf, ctypes.sizeof(buf), ctypes.byref(n2)):
                data = ctypes.string_at(ctypes.byref(buf), n2.value)
                tip = data.decode("utf-16-le", "ignore").split("\x00")[0].strip()
        except Exception:
            tip = ""
        return hwnd, uid, tip
    except Exception:
        return None

def exe_path_from_hwnd(hwnd):
    try:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        return exe_path_from_pid(pid.value)
    except Exception:
        return ""

def exe_path_from_pid(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(h)

def enum_toolbar_icons(tb):
    icons = []
    if not IS64:
        return icons
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(tb, ctypes.byref(pid))
    if not pid.value:
        return icons
    hproc = kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_LIMITED_INFORMATION,
        False, pid.value)
    if not hproc:
        return icons
    remote = kernel32.VirtualAllocEx(hproc, None, ctypes.sizeof(TBBUTTON),
                                     MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE)
    if not remote:
        kernel32.CloseHandle(hproc)
        return icons
    try:
        count = user32.SendMessageW(tb, TB_BUTTONCOUNT, 0, 0)
        if count <= 0 or count > 512:
            return icons
        for i in range(count):
            try:
                if not user32.SendMessageW(tb, TB_GETBUTTON, i, remote):
                    continue
                btn = TBBUTTON()
                n = ctypes.c_size_t(0)
                if not kernel32.ReadProcessMemory(hproc, remote, ctypes.byref(btn),
                                                  ctypes.sizeof(btn), ctypes.byref(n)):
                    continue
                if not btn.dwData:
                    continue
                info = _read_traydata(hproc, btn.dwData)
                if not info:
                    continue
                hwnd, uid, tip = info
                exe = exe_path_from_hwnd(hwnd)
                key = (exe.lower() if exe else ("tip:" + tip).lower())
                icons.append({
                    "tb": tb, "id": btn.idCommand, "hwnd": hwnd, "uid": uid,
                    "tip": tip, "exe": exe, "key": key, "running": True,
                    "hidden": bool(btn.fsState & TBSTATE_HIDDEN),
                })
            except Exception as e:
                log_error("enum item: " + repr(e))
    finally:
        kernel32.VirtualFreeEx(hproc, remote, 0, MEM_RELEASE)
        kernel32.CloseHandle(hproc)
    return icons

def set_icon_hidden(ic, hide):
    """经典托盘: 只隐藏/显示托盘按钮，不会关闭目标程序。"""
    try:
        user32.SendMessageW(ic["tb"], TB_HIDEBUTTON, ic["id"], 1 if hide else 0)
    except Exception as e:
        log_error("set_icon_hidden: " + repr(e))

# ================================================================ 引擎 B: 新版托盘 (Win11)

def probe_icon(hwnd, uid):
    """查询指定 (hwnd, uid) 是否存在托盘图标。"""
    try:
        nid = NOTIFYICONIDENTIFIER()
        nid.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
        nid.hWnd, nid.uID = hwnd, uid
        rc = wintypes.RECT()
        return Shell_NotifyIconGetRect(ctypes.byref(nid), ctypes.byref(rc)) == 0
    except Exception:
        return False

def delete_icon(hwnd, uid):
    """按 (hwnd, uid) 删除托盘图标，不关闭目标程序。"""
    try:
        nid = NOTIFYICONDATAW()
        nid.cbSize = NOTIFYICONDATAW.guidItem.offset
        nid.hWnd, nid.uID = hwnd, uid
        Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    except Exception as e:
        log_error("delete_icon: " + repr(e))

def broadcast_taskbar_created():
    """广播 TaskbarCreated，让各程序重新注册自己的托盘图标（用于恢复显示）。"""
    try:
        user32.PostMessageW(HWND_BROADCAST, TASKBAR_CREATED_MSG, 0, 0)
    except Exception as e:
        log_error("broadcast: " + repr(e))

def window_text(hwnd):
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        return buf.value.strip()
    except Exception:
        return ""

def enum_windows_by_pid():
    """{pid: [hwnd, ...]}，包含不可见的顶层窗口。"""
    result = {}
    def cb(hwnd, _):
        try:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                result.setdefault(pid.value, []).append(hwnd)
        except Exception:
            pass
        return True
    try:
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    except Exception as e:
        log_error("EnumWindows: " + repr(e))
    return result

# ================================================================ 后台引擎

class Engine:
    """周期性扫描托盘图标，并按隐藏名单维持隐藏/显示状态。"""
    def __init__(self, cfg):
        self.cfg = cfg
        self.icons = []          # 给 GUI 的快照
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopped = False
        self.force_full = True
        # 引擎 B 状态
        self.slots = {}          # key -> set((hwnd, uid))
        self.found = {}          # key -> {"exe","tip","present","running"}
        self.pid_exe = {}        # pid -> exe (缓存)
        self.reg_uids = {}       # exe(小写) -> {uID,...} (来自注册表)
        self.last_full = 0.0

    def _candidate_uids(self, key):
        """某程序要探测的 uID 集合: 常用小范围 + 注册表记录的高 uID。"""
        cands = set(UID_SCAN_RANGE)
        extra = self.reg_uids.get(key)
        if extra:
            cands |= extra
        return cands

    # ---------------- 主循环 ----------------
    def loop(self):
        while not self.stopped:
            try:
                tbs = find_toolbars()
                if tbs:
                    self.cycle_toolbar(tbs)
                else:
                    self.cycle_nim()
            except Exception as e:
                log_error("engine: " + repr(e))
            self.wake.wait(POLL_INTERVAL)
            self.wake.clear()

    def snapshot(self):
        with self.lock:
            return [dict(x) for x in self.icons]

    # ---------------- 引擎 A ----------------
    def cycle_toolbar(self, tbs):
        icons = []
        for tb in tbs:
            try:
                icons.extend(enum_toolbar_icons(tb))
            except Exception as e:
                log_error("cycle_toolbar: " + repr(e))
        hidden = self.cfg.hidden_set()
        for ic in icons:
            want = ic["key"] in hidden
            if ic["hidden"] != want:
                set_icon_hidden(ic, want)
                ic["hidden"] = want
        with self.lock:
            self.icons = icons

    # ---------------- 引擎 B ----------------
    def _exe_of_pid(self, pid):
        if pid not in self.pid_exe:
            self.pid_exe[pid] = exe_path_from_pid(pid)
        return self.pid_exe[pid]

    def _note_found(self, key, hwnd, uid, exe, present):
        info = self.found.get(key)
        if not info:
            info = {"key": key, "exe": exe, "tip": "", "present": False, "running": True}
            self.found[key] = info
        info["present"] = present
        tip = window_text(hwnd)
        if tip:
            info["tip"] = tip
        s = self.slots.setdefault(key, set())
        s.add((hwnd, uid))
        if len(s) > 16:
            self.slots[key] = set(list(s)[-16:])

    def cycle_nim(self):
        hidden = self.cfg.hidden_set()
        wins_by_pid = enum_windows_by_pid()

        # 1) 快速复探已知槽位: 图标是否还在; 在且应隐藏 -> 再删
        for key, slots in list(self.slots.items()):
            for (h, uid) in list(slots):
                try:
                    if probe_icon(h, uid):
                        if key in hidden:
                            delete_icon(h, uid)
                            if key in self.found:
                                self.found[key]["present"] = False
                        elif key in self.found:
                            self.found[key]["present"] = True
                except Exception:
                    pass

        # 2) 隐藏名单中的程序: 检查其窗口是否(重新)注册了图标 -> 删除
        for pid, hwnds in wins_by_pid.items():
            exe = self._exe_of_pid(pid)
            if not exe or exe.lower() not in hidden:
                continue
            key = exe.lower()
            for h in hwnds:
                for uid in self._candidate_uids(key):
                    try:
                        if probe_icon(h, uid):
                            delete_icon(h, uid)
                            self._note_found(key, h, uid, exe, present=False)
                    except Exception:
                        pass

        # 3) 周期性全量发现新图标 (供 GUI 列表)
        now = time.time()
        if self.force_full or now - self.last_full > FULL_SCAN_INTERVAL:
            self.force_full = False
            self.last_full = now
            try:
                self.reg_uids = get_registry_uid_map()
            except Exception as e:
                log_error("reg_uids: " + repr(e))
            seen = set()
            for pid, hwnds in wins_by_pid.items():
                exe = self._exe_of_pid(pid)
                if not exe:
                    continue
                key = exe.lower()
                for h in hwnds:
                    for uid in self._candidate_uids(key):
                        try:
                            if probe_icon(h, uid):
                                seen.add(key)
                                if key in hidden:
                                    delete_icon(h, uid)
                                    self._note_found(key, h, uid, exe, present=False)
                                else:
                                    self._note_found(key, h, uid, exe, present=True)
                        except Exception:
                            pass
            # 清理: 不再存在且未隐藏的记录
            for key in list(self.found.keys()):
                if key not in seen and key not in hidden:
                    self.found.pop(key, None)
                    self.slots.pop(key, None)

        # 4) 更新 running 标志并发布快照
        running_exes = set()
        for pid in wins_by_pid:
            exe = self._exe_of_pid(pid)
            if exe:
                running_exes.add(exe.lower())
        out = []
        for key, info in self.found.items():
            info["running"] = key in running_exes
            if info["present"] or key in hidden:
                out.append({
                    "key": key, "exe": info["exe"], "tip": info["tip"],
                    "running": info["running"], "hidden": key in hidden,
                })
        with self.lock:
            self.icons = out

# ================================================================ 配置

class Config:
    def __init__(self):
        self.first_run = False
        self.hidden = []
        self.autostart = True
        self._lock = threading.Lock()
        self.load()

    def load(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.hidden = [str(x).lower() for x in d.get("hidden", [])]
            self.autostart = bool(d.get("autostart", True))
        except FileNotFoundError:
            self.first_run = True
        except Exception as e:
            log_error("config load: " + repr(e))

    def save(self):
        try:
            with self._lock:
                data = {"hidden": sorted(self.hidden), "autostart": self.autostart}
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log_error("config save: " + repr(e))

    def hidden_set(self):
        with self._lock:
            return set(self.hidden)

    def set_hidden(self, key, hide=True):
        key = key.lower()
        with self._lock:
            if hide and key not in self.hidden:
                self.hidden.append(key)
            if not hide and key in self.hidden:
                self.hidden.remove(key)
        self.save()

# ================================================================ 开机自启

def _autostart_cmd():
    if getattr(sys, "frozen", False):
        return '"{}" --quiet'.format(sys.executable)
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script = os.path.abspath(__file__)
    if os.path.exists(pyw):
        return '"{}" "{}" --quiet'.format(pyw, script)
    return '"{}" "{}" --quiet'.format(sys.executable, script)

def set_autostart(enabled):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        log_error("autostart: " + repr(e))
        return False

def get_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_QUERY_VALUE) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except Exception:
        return False

# ================================================================ 单实例 IPC

def notify_running_instance():
    """已有实例在运行时，通知它显示窗口。"""
    try:
        s = socket.create_connection((IPC_HOST, IPC_PORT), timeout=2)
        s.sendall(b"show")
        s.close()
        return
    except Exception:
        pass
    try:
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass

def ipc_server(root, app):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((IPC_HOST, IPC_PORT))
        s.listen(8)
    except Exception as e:
        log_error("ipc bind: " + repr(e))
        return
    while True:
        try:
            conn, _ = s.accept()
            try:
                data = conn.recv(64)
            finally:
                conn.close()
            if b"show" in data:
                root.after(0, app.show_window)
        except Exception as e:
            log_error("ipc loop: " + repr(e))
            time.sleep(1)

# ================================================================ GUI

import tkinter as tk
from tkinter import ttk

def _display_name(ic):
    if ic["exe"]:
        return os.path.basename(ic["exe"])
    return ic["tip"] or "未知程序"

def _key_name(k):
    if k.startswith("tip:"):
        return k[4:] or "未知程序"
    return os.path.basename(k)

class App:
    def __init__(self, root, cfg, engine):
        self.root, self.cfg, self.engine = root, cfg, engine
        root.title(WINDOW_TITLE)
        root.geometry("660x460")
        root.minsize(580, 400)

        top = ttk.Frame(root, padding=(8, 8, 8, 0))
        top.pack(fill="both", expand=True)
        self.status_lbl = ttk.Label(top, text="正在扫描托盘图标……")
        self.status_lbl.pack(anchor="w")

        body = ttk.Frame(top)
        body.pack(fill="both", expand=True, pady=6)
        cols = ("name", "tip", "status")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("name", text="程序")
        self.tree.heading("tip", text="提示文本")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=190)
        self.tree.column("tip", width=250)
        self.tree.column("status", width=140, anchor="center")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bar = ttk.Frame(root, padding=(8, 0, 8, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="隐藏选中", command=self.hide_selected).pack(side="left")
        ttk.Button(bar, text="恢复显示", command=self.show_selected).pack(side="left", padx=6)
        ttk.Button(bar, text="刷新", command=self.force_refresh).pack(side="left")
        self.autostart_var = tk.BooleanVar(value=self.cfg.autostart)
        ttk.Checkbutton(bar, text="开机自启", variable=self.autostart_var,
                        command=self.toggle_autostart).pack(side="right")

        ttk.Label(root, padding=(8, 0, 8, 8), foreground="#666666",
                  text="关闭本窗口不会退出程序，任务栏和托盘中都不会显示；再次双击 TrayHider.exe 即可重新打开。"
                  ).pack(fill="x")

        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        root.reportcallbackexception = lambda *a: log_error("gui: " + repr(a))
        self.refresh()

    # ---------------- 行为 ----------------
    def show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
        except Exception as e:
            log_error("show_window: " + repr(e))

    def hide_selected(self):
        for k in self._selected():
            self.cfg.set_hidden(k, True)
        self.engine.wake.set()

    def show_selected(self):
        keys = self._selected()
        for k in keys:
            self.cfg.set_hidden(k, False)
        if keys:
            broadcast_taskbar_created()  # 让程序重新注册托盘图标
        self.engine.wake.set()

    def force_refresh(self):
        self.engine.force_full = True
        self.engine.wake.set()

    def toggle_autostart(self):
        v = bool(self.autostart_var.get())
        if set_autostart(v):
            self.cfg.autostart = v
            self.cfg.save()
        else:
            self.autostart_var.set(get_autostart())

    def _selected(self):
        try:
            return list(self.tree.selection())
        except Exception:
            return []

    # ---------------- 列表刷新 ----------------
    def refresh(self):
        try:
            snap = self.engine.snapshot()
            hidden = self.cfg.hidden_set()
            data = {}
            for ic in snap:
                k = ic["key"]
                if k not in data:
                    data[k] = (_display_name(ic), ic["tip"], k in hidden, ic.get("running", True))
            for k in hidden:
                if k not in data:
                    data[k] = (_key_name(k), "", True, False)

            self.status_lbl.config(
                text=("检测到 {} 个托盘图标（只隐藏图标，不会关闭程序）".format(len(snap))
                      if snap else
                      "正在扫描托盘图标……（若无图标运行则列表为空）"))

            cur = set(self.tree.get_children())
            for k, (name, tip, hid, running) in data.items():
                status = (("已隐藏" if hid else "显示中") if running else "未运行（已记录隐藏）")
                vals = (name, tip, status)
                if k in cur:
                    self.tree.item(k, values=vals)
                else:
                    self.tree.insert("", "end", iid=k, values=vals)
            for k in cur - set(data):
                self.tree.delete(k)
        except Exception as e:
            log_error("refresh: " + repr(e))
        self.root.after(1500, self.refresh)

# ================================================================ 调试入口

def enum_test():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tbs = find_toolbars()
    out = []
    if tbs:
        for tb in tbs:
            for ic in enum_toolbar_icons(tb):
                out.append({"mode": "toolbar", "exe": ic["exe"], "tip": ic["tip"], "hidden": ic["hidden"]})
    else:
        reg = get_registry_uid_map()
        for pid, hwnds in enum_windows_by_pid().items():
            exe = exe_path_from_pid(pid)
            if not exe:
                continue
            uids = set(UID_SCAN_RANGE) | reg.get(exe.lower(), set())
            for h in hwnds:
                for uid in uids:
                    if probe_icon(h, uid):
                        out.append({"mode": "nim", "exe": exe, "uid": uid, "hwnd": hex(h)})
    try:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        print("total:", len(out))
    except Exception:
        pass

# ================================================================ main

def main():
    if "--enum-test" in sys.argv:
        enum_test()
        return

    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        notify_running_instance()   # 已运行：仅显示已有窗口后退出
        return

    cfg = Config()
    if cfg.first_run:
        set_autostart(True)         # 首次运行默认开启开机自启
        cfg.autostart = True
        cfg.save()

    engine = Engine(cfg)
    threading.Thread(target=engine.loop, daemon=True).start()

    root = tk.Tk()
    app = App(root, cfg, engine)
    threading.Thread(target=ipc_server, args=(root, app), daemon=True).start()

    if "--quiet" in sys.argv:       # 开机自启时不显示窗口
        root.withdraw()

    try:
        root.mainloop()
    except Exception as e:
        log_error("mainloop: " + repr(e))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_error("fatal: " + repr(e))
