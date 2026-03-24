"""
Performance Widget — always-on-top desktop overlay.
Shows CPU, RAM, and GPU usage in real time.

Features:
  - Drag anywhere to reposition
  - Click header to expand/collapse (bottom-anchored, body collapses upward)
  - Drag left/right edges to resize width
  - Collapsible GPU section
  - Position, width, & collapsed states persist in ~/.claude/perf-widget-state.json
  - Auto-refreshes every 2s (async — UI never blocks)
  - Color-coded usage bars (green/yellow/red)
  - Zero external dependencies (Windows ctypes + nvidia-smi for GPU)

Run:  pythonw performance-widget.pyw   (no console window)
  or: python  performance-widget.pyw   (with console for debugging)
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────────────
STATE_DIR = Path.home() / ".claude"
STATE_FILE = STATE_DIR / "perf-widget-state.json"
LOCK_FILE = STATE_DIR / "perf-widget.lock"
REFRESH_MS = 2_000
GPU_REFRESH_MS = 3_000
DEFAULT_WIDTH = 260
MIN_WIDTH = 180
MAX_WIDTH = 450
WIDGET_OPACITY = 0.95
RESIZE_EDGE = 6

# Colors (matches Claude Usage Widget)
BG = "#1a1a2e"
BG_HEADER = "#0f3460"
FG = "#e0e0e0"
FG_DIM = "#b0b0c0"
FG_ACCENT = "#00d4aa"
FG_WARN = "#ff6b6b"
FG_ORANGE = "#ffaa44"
FG_BLUE = "#4fc3f7"
FG_SOFT_RED = "#e07070"
BORDER = "#2a2a4a"
BAR_BG = "#2a2a4a"
BAR_GREEN = "#00d4aa"
BAR_YELLOW = "#ffaa44"
BAR_RED = "#ff6b6b"


# ─── Windows API structures ──────────────────────────────────────────────────

class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


# ─── Data helpers ─────────────────────────────────────────────────────────────

def get_system_times():
    """Get system-wide CPU times (idle, kernel, user) via Windows API."""
    idle = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    return (
        idle.dwLowDateTime | (idle.dwHighDateTime << 32),
        kernel.dwLowDateTime | (kernel.dwHighDateTime << 32),
        user.dwLowDateTime | (user.dwHighDateTime << 32),
    )


def calc_cpu_percent(prev, curr):
    """Calculate CPU % from two GetSystemTimes snapshots.
    Note: kernel time includes idle time on Windows."""
    idle_delta = curr[0] - prev[0]
    kernel_delta = curr[1] - prev[1]
    user_delta = curr[2] - prev[2]
    total = kernel_delta + user_delta
    if total == 0:
        return 0.0
    busy = total - idle_delta
    return max(0.0, min(100.0, (busy / total) * 100))


def get_memory_info():
    """Get RAM usage via Windows API."""
    mem = MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    return {
        "percent": mem.dwMemoryLoad,
        "total_gb": mem.ullTotalPhys / (1024 ** 3),
        "used_gb": (mem.ullTotalPhys - mem.ullAvailPhys) / (1024 ** 3),
        "avail_gb": mem.ullAvailPhys / (1024 ** 3),
    }


def get_cpu_info():
    """Get CPU name, core count, and frequency from registry."""
    info = {"name": "Unknown CPU", "cores": os.cpu_count() or 0, "freq_ghz": 0.0}
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        # Shorten verbose CPU names
        import re
        name = re.sub(r"\(R\)|\(TM\)", "", name)  # Intel(R) Core(TM) → Intel Core
        name = re.sub(r"\s+", " ", name).strip()
        # Drop leading generation prefix if present (e.g. "13th Gen Intel Core")
        name = re.sub(r"^\d+\w*\s+Gen\s+", "", name)
        for prefix in ("Intel Core ", "AMD Ryzen "):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        info["name"] = name
        mhz = winreg.QueryValueEx(key, "~MHz")[0]
        info["freq_ghz"] = mhz / 1000
        winreg.CloseKey(key)
    except OSError:
        pass
    return info


def get_gpu_info():
    """Get GPU info via nvidia-smi. Returns dict or None if unavailable."""
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,"
             "temperature.gpu,name,power.draw,power.limit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return None
        parts = [p.strip() for p in result.stdout.strip().split(",")]
        if len(parts) < 5:
            return None

        def _val(s):
            return None if s in ("[N/A]", "[Not Supported]", "") else s

        gpu_util = float(_val(parts[0])) if _val(parts[0]) else None
        mem_used_mb = float(_val(parts[1])) if _val(parts[1]) else None
        mem_total_mb = float(_val(parts[2])) if _val(parts[2]) else None
        temp = float(_val(parts[3])) if _val(parts[3]) else None
        name = parts[4].replace("NVIDIA ", "").replace("GeForce ", "")
        power_draw = float(_val(parts[5])) if len(parts) > 5 and _val(parts[5]) else None
        power_limit = float(_val(parts[6])) if len(parts) > 6 and _val(parts[6]) else None

        mem_used_gb = mem_used_mb / 1024 if mem_used_mb is not None else None
        mem_total_gb = mem_total_mb / 1024 if mem_total_mb is not None else None
        mem_pct = ((mem_used_mb / mem_total_mb) * 100
                   if mem_used_mb is not None and mem_total_mb else None)

        return {
            "utilization": gpu_util,
            "mem_used_gb": mem_used_gb,
            "mem_total_gb": mem_total_gb,
            "mem_percent": mem_pct,
            "temp": temp,
            "name": name,
            "power_draw": power_draw,
            "power_limit": power_limit,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None


def get_process_count():
    """Get running process count via Windows API."""
    try:
        # Use EnumProcesses
        arr = (wintypes.DWORD * 4096)()
        cb_needed = wintypes.DWORD()
        ctypes.windll.psapi.EnumProcesses(
            ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(cb_needed))
        return cb_needed.value // ctypes.sizeof(wintypes.DWORD)
    except Exception:
        return 0


# ─── Network helpers ──────────────────────────────────────────────────────────

IF_TYPE_ETHERNET = 6
IF_TYPE_IEEE80211 = 71  # WiFi
IF_OPER_STATUS_OPERATIONAL = 5  # MIB_IF_OPER_STATUS_OPERATIONAL (old API)
# Keywords in adapter descriptions that indicate virtual/filter sub-layers
_SKIP_KEYWORDS = ('Filter', 'QoS', 'WFP', 'Hyper-V', 'Virtual', 'Loopback',
                  'WAN Miniport', 'Teredo', 'SSTP', 'L2TP', 'IKEv2', 'PPTP',
                  'Kernel Debug', '6to4', 'IP-HTTPS', 'Wintun')


class MIB_IFROW(ctypes.Structure):
    _fields_ = [
        ("wszName", ctypes.c_wchar * 256),
        ("dwIndex", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwMtu", wintypes.DWORD),
        ("dwSpeed", wintypes.DWORD),
        ("dwPhysAddrLen", wintypes.DWORD),
        ("bPhysAddr", ctypes.c_ubyte * 8),
        ("dwAdminStatus", wintypes.DWORD),
        ("dwOperStatus", wintypes.DWORD),
        ("dwLastChange", wintypes.DWORD),
        ("dwInOctets", wintypes.DWORD),
        ("dwInUcastPkts", wintypes.DWORD),
        ("dwInNUcastPkts", wintypes.DWORD),
        ("dwInDiscards", wintypes.DWORD),
        ("dwInErrors", wintypes.DWORD),
        ("dwInUnknownProtos", wintypes.DWORD),
        ("dwOutOctets", wintypes.DWORD),
        ("dwOutUcastPkts", wintypes.DWORD),
        ("dwOutNUcastPkts", wintypes.DWORD),
        ("dwOutDiscards", wintypes.DWORD),
        ("dwOutErrors", wintypes.DWORD),
        ("dwOutQLen", wintypes.DWORD),
        ("dwDescrLen", wintypes.DWORD),
        ("bDescr", ctypes.c_ubyte * 256),
    ]


def get_network_bytes():
    """Get total bytes in/out across active Ethernet/WiFi interfaces.
    Uses GetIfTable (32-bit counters). Caller handles wraps."""
    try:
        iphlpapi = ctypes.windll.iphlpapi
        size = wintypes.DWORD(0)
        iphlpapi.GetIfTable(None, ctypes.byref(size), False)
        buf = (ctypes.c_ubyte * size.value)()
        ret = iphlpapi.GetIfTable(buf, ctypes.byref(size), False)
        if ret != 0:
            return None

        num = wintypes.DWORD.from_buffer(buf, 0).value
        row_size = ctypes.sizeof(MIB_IFROW)
        base = ctypes.sizeof(wintypes.DWORD)

        total_in = 0
        total_out = 0

        for i in range(num):
            off = base + i * row_size
            if off + row_size > len(buf):
                break
            row = MIB_IFROW.from_buffer(buf, off)
            if row.dwOperStatus != IF_OPER_STATUS_OPERATIONAL:
                continue
            if row.dwType not in (IF_TYPE_ETHERNET, IF_TYPE_IEEE80211):
                continue
            # Skip virtual adapters and filter sub-layers
            descr = bytes(row.bDescr[:row.dwDescrLen]).decode(
                'ascii', errors='replace').rstrip('\x00')
            if any(kw in descr for kw in _SKIP_KEYWORDS):
                continue
            total_in += row.dwInOctets
            total_out += row.dwOutOctets

        return {"bytes_in": total_in, "bytes_out": total_out}
    except Exception:
        return None


def format_data_size(b):
    """Format byte count to human-readable string."""
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.0f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"


def format_speed(mbps):
    """Format speed in MB/s with appropriate precision."""
    if mbps >= 10:
        return f"{mbps:.0f}"
    if mbps >= 1:
        return f"{mbps:.1f}"
    return f"{mbps:.2f}"


# ─── Single instance ─────────────────────────────────────────────────────────

def ensure_single_instance():
    import msvcrt
    try:
        fh = open(LOCK_FILE, "w")
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    except (OSError, IOError):
        return None


# ─── State persistence ───────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


# ─── Widget ──────────────────────────────────────────────────────────────────

class PerfWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Performance")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", WIDGET_OPACITY)
        self.root.configure(bg=BG)

        # State
        self._mouse_mode = None
        self._mouse_start_x = 0
        self._mouse_start_y = 0
        self._win_start_x = 0
        self._win_start_y = 0
        self._win_start_w = 0
        self._dragged = False
        self.expanded = True
        self.gpu_expanded = True
        self.labels = {}
        self._prev_cpu_times = get_system_times()
        self._gpu_available = None  # None = unknown yet
        self._gpu_data = None
        self._cpu_info = get_cpu_info()
        self._prev_net = None
        self._net_last_time = time.monotonic()
        self._net_total_in = 0
        self._net_total_out = 0

        # Load persisted state
        state = load_state()
        self._saved_x = state.get("x")
        self._saved_y = state.get("y")
        self._widget_width = state.get("width", DEFAULT_WIDTH)
        if not state.get("expanded", True):
            self.expanded = False
        if not state.get("gpu_expanded", True):
            self.gpu_expanded = False
        self._font_offset = state.get("font_offset", 0)
        vis = state.get("visible", {})
        self._visible = {
            "cpu": vis.get("cpu", True),
            "ram": vis.get("ram", True),
            "net": vis.get("net", True),
            "gpu": vis.get("gpu", True),
        }
        self._max_net_speed = state.get("max_net_speed", 0)  # MB/s, persisted

        # ─── Layout: body (top) → header (bottom) ──────────────────
        self.body = tk.Frame(self.root, bg=BG, padx=10, pady=6,
                             highlightbackground=BORDER, highlightthickness=1)
        if self.expanded:
            self.body.pack(fill="x", side="top")

        self.header = tk.Frame(self.root, bg=BG_HEADER, padx=8, pady=3)
        self.header.pack(fill="x", side="top")

        self.chevron = tk.Label(self.header, text="▲" if self.expanded else "▼",
                                font=("Segoe UI", 8), fg=FG_DIM, bg=BG_HEADER)
        self.chevron.pack(side="left", padx=(0, 4))

        self.title_lbl = tk.Label(self.header, text="Performance",
                                  font=("Segoe UI", 10, "bold"),
                                  fg=FG_ACCENT, bg=BG_HEADER)
        self.title_lbl.pack(side="left")

        self.close_btn = tk.Label(self.header, text="✕", font=("Segoe UI", 10),
                                  fg=FG_DIM, bg=BG_HEADER, cursor="hand2")
        self.close_btn.pack(side="right")
        self.close_btn.bind("<Button-1>", lambda e: self.root.destroy())

        self._gear_btn = tk.Label(self.header, text="⚙", font=("Segoe UI", 9),
                                  fg=FG_DIM, bg=BG_HEADER, cursor="hand2")
        self._gear_btn.pack(side="right", padx=(0, 4))
        self._gear_btn.bind("<Button-1>", self._show_menu)

        self.status_dot = tk.Label(self.header, text="●", font=("Segoe UI", 8),
                                   fg=FG_ACCENT, bg=BG_HEADER)
        self.status_dot.pack(side="right", padx=(0, 6))

        self._build_body()

        # ─── Unified mouse bindings ──────────────────────────────
        for w in (self.header, self.chevron, self.title_lbl, self.status_dot):
            self._bind_events(w, toggle=True)
        self._bind_events_recursive(self.body)

        # ─── Position & size ──────────────────────────────────────
        self.root.update_idletasks()
        if self._saved_x is not None and self._saved_y is not None:
            self.root.geometry(f"+{self._saved_x}+{self._saved_y}")
        else:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.root.geometry(f"+{sw - self._widget_width - 16}+{sh - 350}")

        self._lock_width()

        if self._font_offset != 0:
            self._adjust_all_fonts(self._font_offset)
            self.root.update_idletasks()
            new_h = self.root.winfo_reqheight()
            self.root.geometry(f"{self._widget_width}x{new_h}")

        # Start async refresh loops
        self._refresh_cpu_ram_async()
        self._refresh_gpu_async()

    def _lock_width(self):
        self.root.minsize(self._widget_width, 0)
        self.root.maxsize(self._widget_width, 2000)

    def _unlock_width(self):
        self.root.minsize(MIN_WIDTH, 0)
        self.root.maxsize(MAX_WIDTH, 2000)

    # ─── Unified mouse handling ───────────────────────────────────

    def _bind_events(self, widget, toggle=False, gpu_toggle=False):
        widget._toggle = toggle
        widget._gpu_toggle = gpu_toggle
        widget._orig_cursor = str(widget.cget("cursor"))
        widget.bind("<Motion>", self._on_motion)
        widget.bind("<Button-1>", self._on_press)
        widget.bind("<B1-Motion>", self._on_move)
        widget.bind("<ButtonRelease-1>", self._on_release)

    def _bind_events_recursive(self, widget):
        self._bind_events(widget, toggle=False)
        for child in widget.winfo_children():
            if child is getattr(self, '_gpu_header', None):
                self._bind_events(child, gpu_toggle=True)
                for grandchild in child.winfo_children():
                    self._bind_events(grandchild, gpu_toggle=True)
                continue
            self._bind_events_recursive(child)

    def _on_motion(self, event):
        rx = event.x_root - self.root.winfo_rootx()
        w = self.root.winfo_width()
        widget = event.widget
        if rx <= RESIZE_EDGE or rx >= w - RESIZE_EDGE:
            widget.config(cursor="sb_h_double_arrow")
        else:
            widget.config(cursor=getattr(widget, '_orig_cursor', ''))

    def _on_press(self, event):
        rx = event.x_root - self.root.winfo_rootx()
        w = self.root.winfo_width()
        self._mouse_start_x = event.x_root
        self._mouse_start_y = event.y_root
        self._win_start_x = self.root.winfo_x()
        self._win_start_y = self.root.winfo_y()
        self._win_start_w = self.root.winfo_width()
        self._dragged = False

        if rx <= RESIZE_EDGE:
            self._mouse_mode = "resize_left"
            self._unlock_width()
        elif rx >= w - RESIZE_EDGE:
            self._mouse_mode = "resize_right"
            self._unlock_width()
        else:
            self._mouse_mode = "drag"

    def _on_move(self, event):
        dx = event.x_root - self._mouse_start_x
        dy = event.y_root - self._mouse_start_y

        if self._mouse_mode == "drag":
            if abs(dx) > 3 or abs(dy) > 3:
                self._dragged = True
            x = self._win_start_x + dx
            y = self._win_start_y + dy
            self.root.geometry(f"+{x}+{y}")
        elif self._mouse_mode == "resize_right":
            new_w = max(MIN_WIDTH, min(MAX_WIDTH, self._win_start_w + dx))
            self.root.geometry(f"{new_w}x{self.root.winfo_height()}")
            self._dragged = True
        elif self._mouse_mode == "resize_left":
            new_w = max(MIN_WIDTH, min(MAX_WIDTH, self._win_start_w - dx))
            new_x = self._win_start_x + (self._win_start_w - new_w)
            self.root.geometry(
                f"{new_w}x{self.root.winfo_height()}+{new_x}+{self.root.winfo_y()}")
            self._dragged = True

    def _on_release(self, event):
        if self._mouse_mode in ("resize_left", "resize_right"):
            self._widget_width = self.root.winfo_width()
            self._lock_width()
            self._persist_state()
        elif self._dragged:
            self._persist_state()
        elif getattr(event.widget, '_gpu_toggle', False):
            self._toggle_gpu()
        elif getattr(event.widget, '_toggle', False):
            self._toggle()
        self._mouse_mode = None

    # ─── Toggle ───────────────────────────────────────────────────

    def _toggle(self):
        old_h = self.root.winfo_height()
        self.expanded = not self.expanded
        if self.expanded:
            self.body.pack(fill="x", side="top", before=self.header)
            self.chevron.config(text="▲")
        else:
            self.body.pack_forget()
            self.chevron.config(text="▼")
        self.root.update_idletasks()
        new_h = self.root.winfo_reqheight()
        dy = old_h - new_h
        x = self.root.winfo_x()
        y = self.root.winfo_y() + dy
        self.root.geometry(f"{self._widget_width}x{new_h}+{x}+{y}")
        self._persist_state()

    def _toggle_gpu(self, event=None):
        old_h = self.root.winfo_height()
        self.gpu_expanded = not self.gpu_expanded
        if self.gpu_expanded:
            self._gpu_content.pack(fill="x", after=self._gpu_header)
            self._gpu_chevron.config(text="▾")
        else:
            self._gpu_content.pack_forget()
            self._gpu_chevron.config(text="▸")
        self.root.update_idletasks()
        new_h = self.root.winfo_reqheight()
        dy = old_h - new_h
        x = self.root.winfo_x()
        y = self.root.winfo_y() + dy
        self.root.geometry(f"{self._widget_width}x{new_h}+{x}+{y}")
        self._persist_state()

    def _persist_state(self):
        save_state({
            "x": self.root.winfo_x(),
            "y": self.root.winfo_y(),
            "width": self._widget_width,
            "expanded": self.expanded,
            "gpu_expanded": self.gpu_expanded,
            "visible": self._visible,
            "font_offset": self._font_offset,
            "max_net_speed": self._max_net_speed,
        })

    # ─── Settings menu ────────────────────────────────────────────

    _SECTION_MAP = {
        "cpu": ("CPU", "_cpu_frame"),
        "ram": ("RAM", "_ram_frame"),
        "net": ("NET", "_net_frame"),
        "gpu": ("GPU", "_gpu_section"),
    }

    def _show_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0, bg=BG, fg=FG,
                       activebackground=BG_HEADER, activeforeground=FG,
                       font=("Segoe UI", 9))
        for key, (label, _) in self._SECTION_MAP.items():
            prefix = "✓ " if self._visible[key] else "   "
            menu.add_command(label=f"{prefix}{label}",
                             command=lambda k=key: self._toggle_section(k))
        menu.add_separator()
        menu.add_command(label=f"  Font +  (current: {self._font_offset:+d})",
                         command=self._font_increase)
        menu.add_command(label=f"  Font −  (current: {self._font_offset:+d})",
                         command=self._font_decrease)
        menu.tk_popup(event.x_root, event.y_root)

    def _toggle_section(self, key):
        self._visible[key] = not self._visible[key]
        frame = getattr(self, self._SECTION_MAP[key][1])
        if self._visible[key]:
            frame.pack(fill="x")
        else:
            frame.pack_forget()
        self._persist_state()

    def _font_increase(self):
        self._adjust_all_fonts(1)
        self._font_offset += 1
        self._refit()
        self._persist_state()

    def _font_decrease(self):
        self._adjust_all_fonts(-1)
        self._font_offset -= 1
        self._refit()
        self._persist_state()

    def _refit(self):
        """Recalculate window size after content changes, anchoring bottom edge."""
        old_h = self.root.winfo_height()
        self.root.update_idletasks()
        new_h = self.root.winfo_reqheight()
        dy = old_h - new_h
        x = self.root.winfo_x()
        y = self.root.winfo_y() + dy
        self.root.geometry(f"{self._widget_width}x{new_h}+{x}+{y}")

    def _adjust_all_fonts(self, delta):
        self._adjust_fonts_recursive(self.root, delta)

    def _adjust_fonts_recursive(self, widget, delta):
        try:
            raw = widget.cget("font")
            if raw and "Segoe" in str(raw):
                parts = widget.tk.splitlist(raw)
                family = parts[0]
                size = int(parts[1])
                new_size = max(5, size + delta)
                if len(parts) > 2:
                    widget.config(font=(family, new_size, parts[2]))
                else:
                    widget.config(font=(family, new_size))
        except (tk.TclError, AttributeError, IndexError, ValueError):
            pass
        for child in widget.winfo_children():
            self._adjust_fonts_recursive(child, delta)

    # ─── UI building ──────────────────────────────────────────────

    def _build_body(self):
        # Title row
        title_row = tk.Frame(self.body, bg=BG)
        title_row.pack(fill="x", pady=(0, 2))
        tk.Label(title_row, text="SYSTEM MONITOR",
                 font=("Segoe UI", 7, "bold"), fg=FG_SOFT_RED, bg=BG,
                 anchor="w").pack(side="left")
        self._proc_lbl = tk.Label(title_row, text="",
                                  font=("Segoe UI", 7), fg=FG_DIM, bg=BG,
                                  anchor="e")
        self._proc_lbl.pack(side="right")

        # ─── CPU section ──────────────────────────────────────────
        self._cpu_frame = tk.Frame(self.body, bg=BG)
        if self._visible["cpu"]:
            self._cpu_frame.pack(fill="x")

        self._bar_label_in(self._cpu_frame, "cpu_lbl", "CPU")
        self._bar_in(self._cpu_frame, "cpu_bar")
        self._cpu_detail = tk.Label(
            self._cpu_frame, text=f"{self._cpu_info['cores']} cores",
            font=("Segoe UI", 7), fg=FG_DIM, bg=BG, anchor="w")
        self._cpu_detail.pack(fill="x", pady=(0, 2))

        # ─── RAM section ──────────────────────────────────────────
        self._ram_frame = tk.Frame(self.body, bg=BG)
        if self._visible["ram"]:
            self._ram_frame.pack(fill="x")

        self._bar_label_in(self._ram_frame, "ram_lbl", "RAM")
        self._bar_in(self._ram_frame, "ram_bar")
        self._ram_detail = tk.Label(
            self._ram_frame, text="",
            font=("Segoe UI", 7), fg=FG_DIM, bg=BG, anchor="w")
        self._ram_detail.pack(fill="x", pady=(0, 2))

        # ─── NET section ──────────────────────────────────────────
        self._net_frame = tk.Frame(self.body, bg=BG)
        if self._visible["net"]:
            self._net_frame.pack(fill="x")

        self._bar_label_in(self._net_frame, "net_lbl", "NET")
        self._bar_in(self._net_frame, "net_bar")
        self._net_detail = tk.Label(
            self._net_frame, text="measuring...",
            font=("Segoe UI", 7), fg=FG_DIM, bg=BG, anchor="w")
        self._net_detail.pack(fill="x", pady=(0, 2))

        # ─── GPU section (collapsible) ────────────────────────────
        self._gpu_section = tk.Frame(self.body, bg=BG)
        if self._visible["gpu"]:
            self._gpu_section.pack(fill="x")

        tk.Frame(self._gpu_section, height=1, bg=BORDER).pack(fill="x", pady=3)

        self._gpu_header = tk.Frame(self._gpu_section, bg=BG, cursor="hand2")
        self._gpu_header.pack(fill="x", pady=(2, 0))
        self._gpu_chevron = tk.Label(
            self._gpu_header,
            text="▾" if self.gpu_expanded else "▸",
            font=("Segoe UI", 8), fg=FG_DIM, bg=BG)
        self._gpu_chevron.pack(side="left")
        self._gpu_title = tk.Label(
            self._gpu_header, text="GPU",
            font=("Segoe UI", 7, "bold"), fg=FG_BLUE, bg=BG,
            anchor="w", cursor="hand2")
        self._gpu_title.pack(side="left", padx=(2, 0))
        self._gpu_name_lbl = tk.Label(
            self._gpu_header, text="",
            font=("Segoe UI", 7), fg=FG_DIM, bg=BG,
            anchor="e", cursor="hand2")
        self._gpu_name_lbl.pack(side="right")

        self._gpu_content = tk.Frame(self._gpu_section, bg=BG)
        if self.gpu_expanded:
            self._gpu_content.pack(fill="x")

        # GPU utilization bar
        self._bar_label_in(self._gpu_content, "gpu_lbl", "Usage")
        self._bar_in(self._gpu_content, "gpu_bar")

        # VRAM bar
        self._bar_label_in(self._gpu_content, "vram_lbl", "VRAM")
        self._bar_in(self._gpu_content, "vram_bar")

        # GPU detail (temp + power)
        self._gpu_detail = tk.Label(
            self._gpu_content, text="",
            font=("Segoe UI", 7), fg=FG_DIM, bg=BG, anchor="w")
        self._gpu_detail.pack(fill="x", pady=(0, 1))

        # "No GPU" label (hidden by default)
        self._no_gpu_lbl = tk.Label(
            self._gpu_content, text="nvidia-smi not found",
            font=("Segoe UI", 8), fg=FG_DIM, bg=BG, anchor="w")

    # ─── Bar helpers ──────────────────────────────────────────────

    def _bar_label_in(self, parent, key, text):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(2, 0))
        tk.Label(row, text=text, font=("Segoe UI", 8),
                 fg=FG_DIM, bg=BG, anchor="w").pack(side="left")
        pct = tk.Label(row, text="", font=("Segoe UI", 8, "bold"),
                       fg=FG, bg=BG, anchor="e")
        pct.pack(side="right")
        self.labels[key] = pct

    def _bar_in(self, parent, key):
        container = tk.Frame(parent, bg=BAR_BG, height=10)
        container.pack(fill="x", pady=(1, 0))
        container.pack_propagate(False)
        fill = tk.Frame(container, bg=BAR_GREEN, height=10)
        fill.place(relx=0, rely=0, relwidth=0, relheight=1)
        self.labels[key] = fill

    def _set_bar(self, bar_key, lbl_key, pct):
        """Update a bar and its percentage label."""
        if pct is None:
            pct = 0
        if pct >= 80:
            color = BAR_RED
        elif pct >= 50:
            color = BAR_YELLOW
        else:
            color = BAR_GREEN

        if bar_key in self.labels:
            self.labels[bar_key].config(bg=color)
            self.labels[bar_key].place(
                relx=0, rely=0,
                relwidth=max(0, min(pct, 100)) / 100,
                relheight=1)
        if lbl_key in self.labels:
            fg = BAR_RED if pct >= 80 else (FG_ORANGE if pct >= 50 else FG)
            self.labels[lbl_key].config(text=f"{pct:.0f}%", fg=fg)

    # ─── Async data refresh ───────────────────────────────────────

    def _refresh_cpu_ram_async(self):
        def _work():
            curr = get_system_times()
            cpu_pct = calc_cpu_percent(self._prev_cpu_times, curr)
            self._prev_cpu_times = curr
            mem = get_memory_info()
            procs = get_process_count()
            net = get_network_bytes()
            now = time.monotonic()
            self.root.after(0, self._apply_cpu_ram, cpu_pct, mem, procs, net, now)
        threading.Thread(target=_work, daemon=True).start()
        self.root.after(REFRESH_MS, self._refresh_cpu_ram_async)

    def _apply_cpu_ram(self, cpu_pct, mem, procs, net, now):
        # CPU
        self._set_bar("cpu_bar", "cpu_lbl", cpu_pct)
        freq_str = f"{self._cpu_info['freq_ghz']:.1f} GHz" if self._cpu_info['freq_ghz'] else ""
        parts = [f"{self._cpu_info['cores']} cores"]
        if freq_str:
            parts.append(freq_str)
        self._cpu_detail.config(text=" · ".join(parts))

        # RAM
        self._set_bar("ram_bar", "ram_lbl", mem["percent"])
        self._ram_detail.config(
            text=f"{mem['used_gb']:.1f} / {mem['total_gb']:.1f} GB"
                 f"  ·  {mem['avail_gb']:.1f} GB free")

        # Process count
        if procs > 0:
            self._proc_lbl.config(text=f"{procs} procs")

        # Status dot color based on peak usage
        peak = max(cpu_pct, mem["percent"])
        if peak >= 90:
            self.status_dot.config(fg=BAR_RED)
        elif peak >= 70:
            self.status_dot.config(fg=BAR_YELLOW)
        else:
            self.status_dot.config(fg=FG_ACCENT)

        # Network
        self._apply_net(net, now)

    def _apply_net(self, net, now):
        if net is None:
            return

        if self._prev_net is not None:
            elapsed = now - self._net_last_time
            if elapsed > 0:
                delta_in = net["bytes_in"] - self._prev_net["bytes_in"]
                delta_out = net["bytes_out"] - self._prev_net["bytes_out"]
                # Handle 32-bit counter wrap — skip sample
                if delta_in < 0:
                    delta_in = 0
                if delta_out < 0:
                    delta_out = 0

                self._net_total_in += delta_in
                self._net_total_out += delta_out

                # Speed in MB/s (bytes → megabytes)
                speed_in = delta_in / (elapsed * 1_000_000)
                speed_out = delta_out / (elapsed * 1_000_000)
                total_speed = speed_in + speed_out

                # Update cached max speed
                if total_speed > self._max_net_speed:
                    self._max_net_speed = total_speed
                    self._persist_state()

                # Bar: current speed as % of max observed
                effective_max = max(self._max_net_speed, 1)
                pct = min(100, (total_speed / effective_max) * 100)
                self._set_bar("net_bar", "net_lbl", pct)

                # Detail line
                total_data = self._net_total_in + self._net_total_out
                max_str = format_speed(self._max_net_speed)
                self._net_detail.config(
                    text=f"\u2193{format_speed(speed_in)} \u2191{format_speed(speed_out)}"
                         f" / {max_str} MB/s \u00b7 {format_data_size(total_data)}")

        self._prev_net = net
        self._net_last_time = now

    def _refresh_gpu_async(self):
        def _work():
            gpu = get_gpu_info()
            self.root.after(0, self._apply_gpu, gpu)
        threading.Thread(target=_work, daemon=True).start()
        self.root.after(GPU_REFRESH_MS, self._refresh_gpu_async)

    def _apply_gpu(self, gpu):
        if gpu is None:
            if self._gpu_available is None:
                # First check — GPU not available
                self._gpu_available = False
                self._no_gpu_lbl.pack(fill="x", pady=(2, 0))
            return

        self._gpu_available = True
        self._no_gpu_lbl.pack_forget()

        # GPU name in header
        self._gpu_name_lbl.config(text=gpu["name"])

        # Usage bar
        self._set_bar("gpu_bar", "gpu_lbl", gpu["utilization"])

        # VRAM bar
        vram_pct = gpu["mem_percent"]
        self._set_bar("vram_bar", "vram_lbl", vram_pct)

        # Detail line
        detail_parts = []
        if gpu["temp"] is not None:
            temp = gpu["temp"]
            temp_fg = BAR_RED if temp >= 85 else (FG_ORANGE if temp >= 70 else FG_DIM)
            detail_parts.append(f"{temp:.0f}°C")
            self._gpu_detail.config(fg=temp_fg)

        if gpu["mem_used_gb"] is not None and gpu["mem_total_gb"] is not None:
            detail_parts.append(
                f"{gpu['mem_used_gb']:.1f}/{gpu['mem_total_gb']:.1f} GB")

        if gpu["power_draw"] is not None:
            pw = f"{gpu['power_draw']:.0f}W"
            if gpu["power_limit"] is not None:
                pw += f"/{gpu['power_limit']:.0f}W"
            detail_parts.append(pw)

        self._gpu_detail.config(text=" · ".join(detail_parts))

        # Factor GPU into status dot
        gpu_util = gpu["utilization"] or 0
        gpu_temp = gpu["temp"] or 0
        if gpu_temp >= 90 or gpu_util >= 95:
            self.status_dot.config(fg=BAR_RED)
        elif gpu_temp >= 80 or gpu_util >= 80:
            self.status_dot.config(fg=BAR_YELLOW)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    _mutex = ensure_single_instance()
    if _mutex is None:
        sys.exit(0)
    PerfWidget().run()
