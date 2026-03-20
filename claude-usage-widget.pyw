"""
Claude Code Usage Widget — always-on-top desktop overlay.
Reads ~/.claude/stats-cache.json, active sessions, and live usage limits.

Features:
  - Drag anywhere to reposition
  - Click header to expand/collapse (bottom-anchored, body collapses upward)
  - Drag left/right edges to resize width
  - Collapsible all-time stats section
  - Position, width, & collapsed states persist in ~/.claude/widget-state.json
  - Auto-refreshes every 30s (async — UI never blocks)
  - Usage bars with session (5h) and weekly (7d) limits + reset times
  - Plan auto-detected from ~/.claude/.credentials.json

Run:  pythonw claude-usage-widget.pyw   (no console window)
  or: python  claude-usage-widget.pyw   (with console for debugging)
"""

import json
import os
import sys
import threading
import time
import tkinter as tk
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────────────
CLAUDE_DIR = Path.home() / ".claude"
STATS_FILE = CLAUDE_DIR / "stats-cache.json"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
STATE_FILE = CLAUDE_DIR / "widget-state.json"
USAGE_CACHE_FILE = CLAUDE_DIR / "widget-usage-cache.json"
CREDS_FILE = CLAUDE_DIR / ".credentials.json"
REFRESH_MS = 30_000
USAGE_REFRESH_MS = 120_000  # usage API every 2 min (rate-limited)
DEFAULT_WIDTH = 280
MIN_WIDTH = 200
MAX_WIDTH = 500
WIDGET_OPACITY = 0.92
RESIZE_EDGE = 6  # pixels from edge to trigger resize

# Colors
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

PLAN_NAMES = {
    "default_claude_max_20x": "Max 20x",
    "default_claude_max_5x": "Max 5x",
    "default_claude_pro": "Pro",
}


# ─── Data helpers ─────────────────────────────────────────────────────────────

def load_stats():
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_credentials():
    try:
        with open(CREDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("claudeAiOauth", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_active_sessions():
    if not SESSIONS_DIR.exists():
        return 0, []
    sessions = []
    for f in SESSIONS_DIR.glob("*.json"):
        if f.stem.startswith("chrome"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            pid = data.get("pid")
            cwd = data.get("cwd", "")
            if pid and is_pid_running(pid):
                sessions.append({"pid": pid, "cwd": os.path.basename(cwd) if cwd else "?"})
        except (json.JSONDecodeError, OSError):
            pass
    return len(sessions), sessions


def is_pid_running(pid):
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def get_daily_stats(stats, date_str):
    for entry in stats.get("dailyActivity", []):
        if entry.get("date") == date_str:
            return entry
    return {"messageCount": 0, "sessionCount": 0, "toolCallCount": 0}


def get_week_stats(stats):
    today = datetime.now().date()
    totals = {"messages": 0, "sessions": 0, "tools": 0, "days_active": 0}
    for i in range(7):
        d = (today - timedelta(days=i)).isoformat()
        day = get_daily_stats(stats, d)
        if day["messageCount"] > 0:
            totals["days_active"] += 1
        totals["messages"] += day["messageCount"]
        totals["sessions"] += day["sessionCount"]
        totals["tools"] += day["toolCallCount"]
    return totals


def format_tokens(n):
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def get_model_summary(stats):
    usage = stats.get("modelUsage", {})
    total_in = sum(d.get("inputTokens", 0) for d in usage.values())
    total_out = sum(d.get("outputTokens", 0) for d in usage.values())
    total_cache = sum(d.get("cacheReadInputTokens", 0) for d in usage.values())
    return {"input": total_in, "output": total_out, "cache": total_cache}


def fetch_usage(token):
    """Fetch live usage. Returns (data, retry_after) tuple.
    On success: (dict, 0). On 429: (None, seconds). On error: (None, 0)."""
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode()), 0
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry = int(e.headers.get("Retry-After", 30))
            return None, max(retry, 10)
        return None, 0
    except Exception:
        return None, 0


def format_reset_time(iso_str):
    """Convert ISO reset timestamp to a human-readable relative string."""
    if not iso_str:
        return ""
    try:
        reset = datetime.fromisoformat(iso_str)
        now = datetime.now(timezone.utc)
        delta = reset - now
        total_sec = int(delta.total_seconds())
        if total_sec <= 0:
            return "now"
        if total_sec < 3600:
            return f"{total_sec // 60}m"
        if total_sec < 86400:
            h = total_sec // 3600
            m = (total_sec % 3600) // 60
            return f"{h}h {m}m"
        d = total_sec // 86400
        h = (total_sec % 86400) // 3600
        return f"{d}d {h}h"
    except Exception:
        return ""


# ─── Single instance ──────────────────────────────────────────────────────────

LOCK_FILE = CLAUDE_DIR / "widget.lock"

def ensure_single_instance():
    """Use a lock file to prevent multiple widget instances.
    Returns the open file handle (kept open to hold the lock), or None."""
    import msvcrt
    try:
        # Open or create lock file
        fh = open(LOCK_FILE, "w")
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh  # keep open to hold lock
    except (OSError, IOError):
        return None


# ─── Usage cache ──────────────────────────────────────────────────────────────

def load_usage_cache():
    try:
        with open(USAGE_CACHE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_usage_cache(data):
    try:
        with open(USAGE_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


# ─── State persistence ───────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


# ─── Widget ───────────────────────────────────────────────────────────────────

class UsageWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Claude Usage")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", WIDGET_OPACITY)
        self.root.configure(bg=BG)

        # State
        self._mouse_mode = None  # "drag", "resize_left", "resize_right"
        self._mouse_start_x = 0
        self._mouse_start_y = 0
        self._win_start_x = 0
        self._win_start_y = 0
        self._win_start_w = 0
        self._dragged = False
        self.expanded = True
        self.alltime_expanded = True
        self.labels = {}
        self._usage_data = None
        self._usage_lock = threading.Lock()
        self._throb_active = False
        self._throb_step = 0
        self._usage_backoff = USAGE_REFRESH_MS

        # Credentials
        creds = load_credentials()
        self._oauth_token = creds.get("accessToken")
        self._plan_tier = creds.get("rateLimitTier", "")
        self._plan_name = PLAN_NAMES.get(self._plan_tier, creds.get("subscriptionType", "?").title())

        # Load persisted state
        state = load_state()
        self._saved_x = state.get("x")
        self._saved_y = state.get("y")
        self._widget_width = state.get("width", DEFAULT_WIDTH)
        if not state.get("expanded", True):
            self.expanded = False
        if not state.get("alltime_expanded", True):
            self.alltime_expanded = False
        # Section visibility (default all visible)
        vis = state.get("visible", {})
        self._visible = {
            "session": vis.get("session", True),
            "weekly": vis.get("weekly", True),
            "sonnet": vis.get("sonnet", True),
            "alltime": vis.get("alltime", True),
        }

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

        self.title_lbl = tk.Label(self.header, text="Claude Code",
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

        self.live_dot = tk.Label(self.header, text="", font=("Segoe UI", 8),
                                 fg=FG_ACCENT, bg=BG_HEADER)
        self.live_dot.pack(side="right", padx=(0, 6))

        self._build_body()

        # ─── Unified mouse bindings ──────────────────────────────
        # Header widgets: drag + resize + toggle on click
        for w in (self.header, self.chevron, self.title_lbl, self.live_dot):
            self._bind_events(w, toggle=True)
        # Body widgets: drag + resize only
        self._bind_events_recursive(self.body)

        # ─── Position & size ──────────────────────────────────────
        self.root.update_idletasks()
        if self._saved_x is not None and self._saved_y is not None:
            self.root.geometry(f"+{self._saved_x}+{self._saved_y}")
        else:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.root.geometry(f"+{sw - self._widget_width - 16}+{sh - 400}")

        self._lock_width()

        # Load cached usage data immediately (no API hit)
        cached = load_usage_cache()
        if cached:
            self._usage_data = cached
            self._apply_usage(cached)

        # Initial data + start async loops
        self._refresh_async()
        self._fetch_usage_async()

    def _lock_width(self):
        self.root.minsize(self._widget_width, 0)
        self.root.maxsize(self._widget_width, 2000)

    def _unlock_width(self):
        self.root.minsize(MIN_WIDTH, 0)
        self.root.maxsize(MAX_WIDTH, 2000)

    # ─── Unified mouse handling ───────────────────────────────────

    def _bind_events(self, widget, toggle=False, alltime_toggle=False):
        widget._toggle = toggle
        widget._alltime_toggle = alltime_toggle
        widget._orig_cursor = str(widget.cget("cursor"))
        widget.bind("<Motion>", self._on_motion)
        widget.bind("<Button-1>", self._on_press)
        widget.bind("<B1-Motion>", self._on_move)
        widget.bind("<ButtonRelease-1>", self._on_release)

    def _bind_events_recursive(self, widget):
        self._bind_events(widget, toggle=False)
        for child in widget.winfo_children():
            if child is getattr(self, '_alltime_header', None):
                self._bind_events(child, alltime_toggle=True)
                for grandchild in child.winfo_children():
                    self._bind_events(grandchild, alltime_toggle=True)
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
            self.root.geometry(f"{new_w}x{self.root.winfo_height()}+{new_x}+{self.root.winfo_y()}")
            self._dragged = True

    def _on_release(self, event):
        if self._mouse_mode in ("resize_left", "resize_right"):
            self._widget_width = self.root.winfo_width()
            self._lock_width()
            self._persist_state()
        elif self._dragged:
            self._persist_state()
        elif getattr(event.widget, '_alltime_toggle', False):
            self._toggle_alltime()
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
        # Anchor bottom edge: adjust y so bottom stays fixed
        dy = old_h - new_h
        x = self.root.winfo_x()
        y = self.root.winfo_y() + dy
        self.root.geometry(f"{self._widget_width}x{new_h}+{x}+{y}")
        self._persist_state()

    def _toggle_alltime(self, event=None):
        old_h = self.root.winfo_height()
        self.alltime_expanded = not self.alltime_expanded
        if self.alltime_expanded:
            self._alltime_content.pack(fill="x", after=self._alltime_header)
            self._ts_row.pack(fill="x", pady=(4, 0))
            self._ts_inline.pack_forget()
            self._alltime_chevron.config(text="▾")
        else:
            self._alltime_content.pack_forget()
            self._ts_row.pack_forget()
            self._ts_inline.pack(side="right")
            self._alltime_chevron.config(text="▸")
        self.root.update_idletasks()
        new_h = self.root.winfo_reqheight()
        # Anchor bottom edge
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
            "alltime_expanded": self.alltime_expanded,
            "visible": self._visible,
        })

    # ─── Settings menu ────────────────────────────────────────────

    _SECTION_MAP = {
        "session": ("Session (5h)", "_session_frame"),
        "weekly": ("Weekly (7d)", "_weekly_frame"),
        "sonnet": ("Sonnet (7d)", "_sonnet_frame"),
        "alltime": ("All-time", "_alltime_section"),
    }

    def _show_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0, bg=BG, fg=FG,
                       activebackground=BG_HEADER, activeforeground=FG,
                       font=("Segoe UI", 9))
        for key, (label, _) in self._SECTION_MAP.items():
            prefix = "✓ " if self._visible[key] else "   "
            menu.add_command(label=f"{prefix}{label}",
                             command=lambda k=key: self._toggle_section(k))
        menu.tk_popup(event.x_root, event.y_root)

    def _toggle_section(self, key):
        self._visible[key] = not self._visible[key]
        frame = getattr(self, self._SECTION_MAP[key][1])
        if self._visible[key]:
            frame.pack(fill="x")
        else:
            frame.pack_forget()
        self._persist_state()

    # ─── Throb animation ──────────────────────────────────────────────

    def _throb(self):
        if not self._throb_active:
            return
        import math
        # Slow sine wave: 2s full cycle
        t = (self._throb_step % 40) / 40.0
        alpha = 0.4 + 0.6 * (0.5 + 0.5 * math.sin(2 * math.pi * t))
        # Interpolate between dim green and bright accent
        r = int(0x00 + (0x00 - 0x00) * alpha)
        g = int(0x60 + (0xd4 - 0x60) * alpha)
        b = int(0x50 + (0xaa - 0x50) * alpha)
        self.live_dot.config(fg=f"#{r:02x}{g:02x}{b:02x}")
        self._throb_step += 1
        self.root.after(50, self._throb)

    # ─── UI building ──────────────────────────────────────────────────

    def _build_body(self):
        # Combined row: "USAGE LIMITS" (left, soft red) + "Plan: X" (right, blue)
        combo_row = tk.Frame(self.body, bg=BG)
        combo_row.pack(fill="x", pady=(0, 2))
        tk.Label(combo_row, text="USAGE LIMITS",
                 font=("Segoe UI", 7, "bold"), fg=FG_SOFT_RED, bg=BG,
                 anchor="w").pack(side="left")
        tk.Label(combo_row, text=f"Plan: {self._plan_name}",
                 font=("Segoe UI", 8, "bold"), fg=FG_BLUE, bg=BG,
                 anchor="e").pack(side="right")

        # Session (5h) bar
        self._session_frame = tk.Frame(self.body, bg=BG)
        if self._visible["session"]:
            self._session_frame.pack(fill="x")
        self._bar_label_in(self._session_frame, "usage_session_lbl", "Session (5h)")
        self._bar_in(self._session_frame, "usage_session_bar")
        self._bar_detail_in(self._session_frame, "usage_session_detail")

        # Weekly (7d) bar
        self._weekly_frame = tk.Frame(self.body, bg=BG)
        if self._visible["weekly"]:
            self._weekly_frame.pack(fill="x")
        self._bar_label_in(self._weekly_frame, "usage_weekly_lbl", "Weekly (7d)")
        self._bar_in(self._weekly_frame, "usage_weekly_bar")
        self._bar_detail_in(self._weekly_frame, "usage_weekly_detail")

        # Weekly Sonnet bar
        self._sonnet_frame = tk.Frame(self.body, bg=BG)
        if self._visible["sonnet"]:
            self._sonnet_frame.pack(fill="x")
        self._bar_label_in(self._sonnet_frame, "usage_sonnet_lbl", "Sonnet (7d)")
        self._bar_in(self._sonnet_frame, "usage_sonnet_bar")
        self._bar_detail_in(self._sonnet_frame, "usage_sonnet_detail")

        # Rate limit countdown (hidden by default)
        self._rate_limit_lbl = tk.Label(self.body, text="",
                                        font=("Segoe UI", 7),
                                        fg=FG_ORANGE, bg=BG, anchor="w")

        # ─── Today (hidden when all zeros) ─────────────────────────
        self._today_frame = tk.Frame(self.body, bg=BG)
        self._build_section_in(self._today_frame, "TODAY", [
            ("today_messages", "Messages"),
            ("today_sessions", "Sessions"),
            ("today_tools", "Tool calls"),
        ])

        # ─── Last 7 days (hidden when all zeros) ─────────────────
        self._week_frame = tk.Frame(self.body, bg=BG)
        self._build_section_in(self._week_frame, "LAST 7 DAYS", [
            ("week_messages", "Messages"),
            ("week_sessions", "Sessions"),
            ("week_tools", "Tool calls"),
            ("week_active", "Days active"),
        ])

        # Anchor — dynamic sections pack before this
        self._alltime_anchor = tk.Frame(self.body, bg=BG, height=0)
        self._alltime_anchor.pack(fill="x")

        # ─── All-time collapsible section ─────────────────────────
        self._alltime_section = tk.Frame(self.body, bg=BG)
        if self._visible["alltime"]:
            self._alltime_section.pack(fill="x")
        tk.Frame(self._alltime_section, height=1, bg=BORDER).pack(fill="x", pady=3)

        self._alltime_header = tk.Frame(self._alltime_section, bg=BG, cursor="hand2")
        self._alltime_header.pack(fill="x", pady=(2, 0))
        self._alltime_chevron = tk.Label(self._alltime_header,
                                         text="▾" if self.alltime_expanded else "▸",
                                         font=("Segoe UI", 8), fg=FG_DIM, bg=BG)
        self._alltime_chevron.pack(side="left")
        alltime_title = tk.Label(self._alltime_header, text="ALL-TIME",
                                 font=("Segoe UI", 7, "bold"), fg=FG_BLUE,
                                 bg=BG, anchor="w", cursor="hand2")
        alltime_title.pack(side="left", padx=(2, 0))

        # Inline timestamp (shown when alltime collapsed)
        self._ts_inline = tk.Label(self._alltime_header, text="",
                                   font=("Segoe UI", 7), fg=FG_DIM,
                                   bg=BG, anchor="e", cursor="hand2")
        if not self.alltime_expanded:
            self._ts_inline.pack(side="right")

        self._alltime_content = tk.Frame(self._alltime_section, bg=BG)
        if self.alltime_expanded:
            self._alltime_content.pack(fill="x")

        self._row_in(self._alltime_content, "total_input", "Input")
        self._row_in(self._alltime_content, "total_output", "Output")
        self._row_in(self._alltime_content, "total_cache", "Cache reads")
        tk.Frame(self._alltime_content, height=1, bg=BORDER).pack(fill="x", pady=3)
        self._row_in(self._alltime_content, "all_sessions", "Total sessions", fg=FG_DIM)
        self._row_in(self._alltime_content, "all_messages", "Total messages", fg=FG_DIM)

        # Timestamp row (shown when alltime expanded)
        self._ts_row = tk.Frame(self._alltime_section, bg=BG)
        if self.alltime_expanded:
            self._ts_row.pack(fill="x", pady=(4, 0))
        self.ts_label = tk.Label(self._ts_row, text="", font=("Segoe UI", 7),
                                 fg=FG_DIM, bg=BG, anchor="e")
        self.ts_label.pack(side="right")

    def _sep(self):
        tk.Frame(self.body, height=1, bg=BORDER).pack(fill="x", pady=3)

    def _build_section_in(self, parent, title, rows):
        """Build a section inside a frame (for dynamic show/hide)."""
        tk.Frame(parent, height=1, bg=BORDER).pack(fill="x", pady=3)
        tk.Label(parent, text=title, font=("Segoe UI", 7, "bold"),
                 fg=FG_BLUE, bg=BG, anchor="w").pack(fill="x", pady=(4, 1))
        for key, label_text in rows:
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", pady=0)
            tk.Label(row, text=label_text, font=("Segoe UI", 9),
                     fg=FG_DIM, bg=BG, anchor="w").pack(side="left")
            val = tk.Label(row, text="--", font=("Segoe UI", 9, "bold"),
                           fg=FG, bg=BG, anchor="e")
            val.pack(side="right")
            self.labels[key] = val

    def _row_in(self, parent, key, label_text, fg=FG):
        """Build a data row inside a specific parent frame."""
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=0)
        tk.Label(row, text=label_text, font=("Segoe UI", 9),
                 fg=FG_DIM, bg=BG, anchor="w").pack(side="left")
        val = tk.Label(row, text="--", font=("Segoe UI", 9, "bold"),
                       fg=fg, bg=BG, anchor="e")
        val.pack(side="right")
        self.labels[key] = val

    def _bar_label(self, key, text):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", pady=(2, 0))
        tk.Label(row, text=text, font=("Segoe UI", 8),
                 fg=FG_DIM, bg=BG, anchor="w").pack(side="left")
        pct = tk.Label(row, text="", font=("Segoe UI", 8, "bold"),
                       fg=FG, bg=BG, anchor="e")
        pct.pack(side="right")
        self.labels[key] = pct

    def _bar(self, key):
        container = tk.Frame(self.body, bg=BAR_BG, height=10)
        container.pack(fill="x", pady=(1, 0))
        container.pack_propagate(False)
        fill = tk.Frame(container, bg=BAR_GREEN, height=10)
        fill.place(relx=0, rely=0, relwidth=0, relheight=1)
        self.labels[key] = fill

    def _bar_detail(self, key):
        lbl = tk.Label(self.body, text="", font=("Segoe UI", 7),
                       fg=FG_DIM, bg=BG, anchor="w")
        lbl.pack(fill="x", pady=(0, 1))
        self.labels[key] = lbl

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

    def _bar_detail_in(self, parent, key):
        lbl = tk.Label(parent, text="", font=("Segoe UI", 7),
                       fg=FG_DIM, bg=BG, anchor="w")
        lbl.pack(fill="x", pady=(0, 1))
        self.labels[key] = lbl

    def _set(self, key, value, fg=None):
        if key in self.labels:
            self.labels[key].config(text=str(value))
            if fg:
                self.labels[key].config(fg=fg)

    def _set_bar(self, bar_key, lbl_key, detail_key, utilization, resets_at):
        pct = utilization if utilization is not None else 0
        if pct >= 80:
            color = BAR_RED
        elif pct >= 50:
            color = BAR_YELLOW
        else:
            color = BAR_GREEN
        if bar_key in self.labels:
            self.labels[bar_key].config(bg=color)
            self.labels[bar_key].place(relx=0, rely=0,
                                       relwidth=max(0, min(pct, 100)) / 100,
                                       relheight=1)
        if lbl_key in self.labels:
            fg = BAR_RED if pct >= 80 else (FG_ORANGE if pct >= 50 else FG)
            self.labels[lbl_key].config(text=f"{pct:.0f}%", fg=fg)
        if detail_key in self.labels:
            reset_str = format_reset_time(resets_at)
            self.labels[detail_key].config(
                text=f"resets in {reset_str}" if reset_str else "")

    # ─── Async data refresh ───────────────────────────────────────────

    def _refresh_async(self):
        def _work():
            stats = load_stats()
            count, sessions = get_active_sessions()
            self.root.after(0, self._apply_stats, stats, count, sessions)
        threading.Thread(target=_work, daemon=True).start()
        self.root.after(REFRESH_MS, self._refresh_async)

    def _apply_stats(self, stats, count, sessions):
        if count > 0:
            self.live_dot.config(
                text=f"● {count} live",
                fg=FG_ACCENT)
            if not self._throb_active:
                self._throb_active = True
                self._throb()
        else:
            self.live_dot.config(text="○ idle", fg=FG_DIM)
            self._throb_active = False

        if not stats:
            return

        # Today — show/hide dynamically
        today_str = datetime.now().date().isoformat()
        today = get_daily_stats(stats, today_str)
        has_today = (today["messageCount"] > 0 or today["sessionCount"] > 0
                     or today["toolCallCount"] > 0)
        if has_today:
            self._set("today_messages", f"{today['messageCount']:,}")
            self._set("today_sessions", f"{today['sessionCount']:,}")
            self._set("today_tools", f"{today['toolCallCount']:,}")
            if not self._today_frame.winfo_manager():
                self._today_frame.pack(fill="x", before=self._alltime_anchor)
        else:
            self._today_frame.pack_forget()

        # Week — show/hide dynamically
        week = get_week_stats(stats)
        has_week = (week["messages"] > 0 or week["sessions"] > 0
                    or week["tools"] > 0)
        if has_week:
            self._set("week_messages", f"{week['messages']:,}")
            self._set("week_sessions", f"{week['sessions']:,}")
            self._set("week_tools", f"{week['tools']:,}")
            self._set("week_active", f"{week['days_active']}/7")
            if not self._week_frame.winfo_manager():
                self._week_frame.pack(fill="x", before=self._alltime_anchor)
        else:
            self._week_frame.pack_forget()

        # Tokens
        tokens = get_model_summary(stats)
        self._set("total_input", format_tokens(tokens["input"]))
        self._set("total_output", format_tokens(tokens["output"]))
        self._set("total_cache", format_tokens(tokens["cache"]))

        # All-time
        self._set("all_sessions", f"{stats.get('totalSessions', 0):,}")
        self._set("all_messages", f"{stats.get('totalMessages', 0):,}")

        # Timestamp (update both inline and standalone)
        ts_text = f"updated {datetime.now().strftime('%H:%M:%S')}"
        self.ts_label.config(text=ts_text)
        self._ts_inline.config(text=ts_text)

    def _fetch_usage_async(self):
        if not self._oauth_token:
            self.root.after(USAGE_REFRESH_MS, self._fetch_usage_async)
            return

        def _work():
            data, retry_after = fetch_usage(self._oauth_token)
            if data:
                with self._usage_lock:
                    self._usage_data = data
                save_usage_cache(data)
                self.root.after(0, self._apply_usage, data)
                self.root.after(0, self._hide_rate_limit)
                self._usage_backoff = USAGE_REFRESH_MS  # reset on success
            elif retry_after > 0:
                self.root.after(0, self._show_rate_limit_countdown,
                                self._usage_backoff // 1000)
                # Double backoff for next attempt
                self._usage_backoff = min(self._usage_backoff * 2, 3600_000)

        threading.Thread(target=_work, daemon=True).start()
        self.root.after(self._usage_backoff, self._fetch_usage_async)

    def _show_rate_limit_countdown(self, seconds):
        if seconds > 0:
            self._rate_limit_lbl.config(
                text=f"api rate limit: retry in {seconds}s")
            if not self._rate_limit_lbl.winfo_manager():
                self._rate_limit_lbl.pack(fill="x", pady=(1, 0))
            self.root.after(1000, self._show_rate_limit_countdown, seconds - 1)
        else:
            self._rate_limit_lbl.config(text="api rate limit: retrying...")

    def _hide_rate_limit(self):
        self._rate_limit_lbl.pack_forget()

    def _apply_usage(self, data):
        fh = data.get("five_hour") or {}
        self._set_bar("usage_session_bar", "usage_session_lbl",
                       "usage_session_detail",
                       fh.get("utilization", 0), fh.get("resets_at"))

        sd = data.get("seven_day") or {}
        self._set_bar("usage_weekly_bar", "usage_weekly_lbl",
                       "usage_weekly_detail",
                       sd.get("utilization", 0), sd.get("resets_at"))

        ss = data.get("seven_day_sonnet") or {}
        self._set_bar("usage_sonnet_bar", "usage_sonnet_lbl",
                       "usage_sonnet_detail",
                       ss.get("utilization", 0), ss.get("resets_at"))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    _mutex = ensure_single_instance()
    if _mutex is None:
        sys.exit(0)  # another instance is already running
    UsageWidget().run()
