"""
Claude Code Usage Widget — always-on-top desktop overlay.
Reads ~/.claude/stats-cache.json and active sessions.

Features:
  - Drag anywhere on header to reposition
  - Click header to expand/collapse
  - Position & collapsed state persist in ~/.claude/widget-state.json
  - Auto-refreshes every 30s

Run:  pythonw claude-usage-widget.pyw   (no console window)
  or: python  claude-usage-widget.pyw   (with console for debugging)
"""

import json
import os
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────────────
CLAUDE_DIR = Path.home() / ".claude"
STATS_FILE = CLAUDE_DIR / "stats-cache.json"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
STATE_FILE = CLAUDE_DIR / "widget-state.json"
REFRESH_MS = 30_000
WIDGET_WIDTH = 280
WIDGET_OPACITY = 0.92

# Colors
BG = "#1a1a2e"
BG_HEADER = "#0f3460"
FG = "#e0e0e0"
FG_DIM = "#888899"
FG_ACCENT = "#00d4aa"
FG_WARN = "#ff6b6b"
FG_BLUE = "#4fc3f7"
BORDER = "#2a2a4a"


# ─── Data helpers ─────────────────────────────────────────────────────────────

def load_stats():
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


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
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._dragged = False
        self.expanded = True
        self.labels = {}

        # Load persisted state
        state = load_state()
        self._saved_x = state.get("x")
        self._saved_y = state.get("y")
        if not state.get("expanded", True):
            self.expanded = False

        # ─── Header (always visible) ─────────────────────────────────
        self.header = tk.Frame(self.root, bg=BG_HEADER, padx=8, pady=5, cursor="fleur")
        self.header.pack(fill="x")

        self.chevron = tk.Label(self.header, text="▼" if self.expanded else "▶",
                                font=("Segoe UI", 8), fg=FG_DIM, bg=BG_HEADER)
        self.chevron.pack(side="left", padx=(0, 4))

        self.title_lbl = tk.Label(self.header, text="Claude Code",
                                  font=("Segoe UI", 10, "bold"),
                                  fg=FG_ACCENT, bg=BG_HEADER)
        self.title_lbl.pack(side="left")

        self.live_dot = tk.Label(self.header, text="", font=("Segoe UI", 8),
                                 fg=FG_ACCENT, bg=BG_HEADER)
        self.live_dot.pack(side="left", padx=(6, 0))

        close_btn = tk.Label(self.header, text="✕", font=("Segoe UI", 10),
                             fg=FG_DIM, bg=BG_HEADER, cursor="hand2")
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda e: self.root.destroy())

        # Drag bindings on header + children
        for w in (self.header, self.chevron, self.title_lbl, self.live_dot):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", self._end_drag)

        # ─── Body (collapsible) ──────────────────────────────────────
        self.body = tk.Frame(self.root, bg=BG, padx=10, pady=6,
                             highlightbackground=BORDER, highlightthickness=1)
        if self.expanded:
            self.body.pack(fill="x")

        self._build_body()

        # ─── Position ────────────────────────────────────────────────
        self.root.update_idletasks()
        if self._saved_x is not None and self._saved_y is not None:
            self.root.geometry(f"+{self._saved_x}+{self._saved_y}")
        else:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.root.update_idletasks()
            wh = self.root.winfo_reqheight()
            x = sw - WIDGET_WIDTH - 16
            y = sh - wh - 60
            self.root.geometry(f"+{x}+{y}")

        # Fix width
        self.root.minsize(WIDGET_WIDTH, 0)
        self.root.maxsize(WIDGET_WIDTH, 2000)

        # Initial data
        self._refresh()
        self.root.after(REFRESH_MS, self._auto_refresh)

    # ─── Drag ─────────────────────────────────────────────────────────

    def _start_drag(self, event):
        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root
        self._win_start_x = self.root.winfo_x()
        self._win_start_y = self.root.winfo_y()
        self._dragged = False

    def _on_drag(self, event):
        dx = event.x_root - self._drag_start_x
        dy = event.y_root - self._drag_start_y
        if abs(dx) > 3 or abs(dy) > 3:
            self._dragged = True
        x = self._win_start_x + dx
        y = self._win_start_y + dy
        self.root.geometry(f"+{x}+{y}")

    def _end_drag(self, event):
        if self._dragged:
            # Save position
            self._persist_state()
        else:
            # Click without drag = toggle expand/collapse
            self._toggle()

    def _toggle(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.body.pack(fill="x")
            self.chevron.config(text="▼")
        else:
            self.body.pack_forget()
            self.chevron.config(text="▶")
        self._persist_state()

    def _persist_state(self):
        save_state({
            "x": self.root.winfo_x(),
            "y": self.root.winfo_y(),
            "expanded": self.expanded,
        })

    # ─── UI building ──────────────────────────────────────────────────

    def _build_body(self):
        self._section("TODAY", FG_BLUE)
        self._row("today_messages", "Messages")
        self._row("today_sessions", "Sessions")
        self._row("today_tools", "Tool calls")

        self._sep()

        self._section("LAST 7 DAYS", FG_BLUE)
        self._row("week_messages", "Messages")
        self._row("week_sessions", "Sessions")
        self._row("week_tools", "Tool calls")
        self._row("week_active", "Days active")

        self._sep()

        self._section("ALL-TIME TOKENS", FG_BLUE)
        self._row("total_input", "Input")
        self._row("total_output", "Output")
        self._row("total_cache", "Cache reads")

        self._sep()

        self._row("all_sessions", "Total sessions", fg=FG_DIM)
        self._row("all_messages", "Total messages", fg=FG_DIM)

        # Timestamp row
        ts_row = tk.Frame(self.body, bg=BG)
        ts_row.pack(fill="x", pady=(4, 0))
        self.ts_label = tk.Label(ts_row, text="", font=("Segoe UI", 7),
                                 fg=FG_DIM, bg=BG, anchor="e")
        self.ts_label.pack(side="right")

    def _section(self, title, color):
        tk.Label(self.body, text=title, font=("Segoe UI", 7, "bold"),
                 fg=color, bg=BG, anchor="w").pack(fill="x", pady=(4, 1))

    def _sep(self):
        tk.Frame(self.body, height=1, bg=BORDER).pack(fill="x", pady=3)

    def _row(self, key, label_text, fg=FG):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", pady=0)
        lbl = tk.Label(row, text=label_text, font=("Segoe UI", 9),
                       fg=FG_DIM, bg=BG, anchor="w")
        lbl.pack(side="left")
        val = tk.Label(row, text="—", font=("Segoe UI", 9, "bold"),
                       fg=fg, bg=BG, anchor="e")
        val.pack(side="right")
        self.labels[key] = val

    def _set(self, key, value, fg=None):
        if key in self.labels:
            self.labels[key].config(text=str(value))
            if fg:
                self.labels[key].config(fg=fg)

    # ─── Data refresh ─────────────────────────────────────────────────

    def _refresh(self):
        stats = load_stats()

        # Active sessions (shown in header)
        count, sessions = get_active_sessions()
        if count > 0:
            self.live_dot.config(text=f"● {count} live", fg=FG_ACCENT)
        else:
            self.live_dot.config(text="○ idle", fg=FG_DIM)

        if not stats:
            return

        # Today
        today_str = datetime.now().date().isoformat()
        today = get_daily_stats(stats, today_str)
        self._set("today_messages", f"{today['messageCount']:,}")
        self._set("today_sessions", f"{today['sessionCount']:,}")
        self._set("today_tools", f"{today['toolCallCount']:,}")

        # Week
        week = get_week_stats(stats)
        self._set("week_messages", f"{week['messages']:,}")
        self._set("week_sessions", f"{week['sessions']:,}")
        self._set("week_tools", f"{week['tools']:,}")
        self._set("week_active", f"{week['days_active']}/7")

        # Tokens
        tokens = get_model_summary(stats)
        self._set("total_input", format_tokens(tokens["input"]))
        self._set("total_output", format_tokens(tokens["output"]))
        self._set("total_cache", format_tokens(tokens["cache"]))

        # All-time
        self._set("all_sessions", f"{stats.get('totalSessions', 0):,}")
        self._set("all_messages", f"{stats.get('totalMessages', 0):,}")

        # Timestamp
        self.ts_label.config(text=f"updated {datetime.now().strftime('%H:%M:%S')}")

    def _auto_refresh(self):
        self._refresh()
        self.root.after(REFRESH_MS, self._auto_refresh)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    UsageWidget().run()
