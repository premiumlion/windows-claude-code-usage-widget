# Claude Code Usage Widget

A lightweight Windows desktop widget that displays your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) usage stats in a compact, always-on-top overlay.

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)
![Windows](https://img.shields.io/badge/platform-Windows-0078D6.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

## What it shows

| Section | Data |
|---------|------|
| **Header** | Live session count (throbbing indicator), collapse/expand, settings |
| **Usage Limits** | Session (5h), Weekly (7d), Sonnet (7d) — live bars with % and reset countdown |
| **Plan** | Auto-detected plan tier (Pro, Max 5x, Max 20x) |
| **Today** | Messages, sessions, tool calls (hidden when no data) |
| **Last 7 days** | Messages, sessions, tool calls, days active (hidden when no data) |
| **All-time** | Input/output/cache tokens, total sessions and messages (collapsible) |

## Features

- **Always-on-top** — stays visible over other windows
- **Drag anywhere** — grab any part of the widget to reposition
- **Resize** — drag left/right edges to adjust width (200–500px)
- **Click header to collapse/expand** — body collapses upward, bottom-anchored
- **Collapsible All-time section** — with inline timestamp when collapsed
- **Settings dropdown** — gear icon to show/hide individual sections (Session, Weekly, Sonnet, All-time)
- **Live usage bars** — fetched from Anthropic OAuth API with color-coded thresholds
- **Rate limit handling** — exponential backoff with visible countdown on 429
- **Usage cache** — instant display on startup from cached API data, no immediate API hit
- **Single instance** — lock file prevents duplicate processes
- **Persistent state** — position, width, collapse state, section visibility all saved
- **Auto-refresh** — stats every 30s, usage API every 2 min
- **Dark theme** — clean dark UI that doesn't distract

## Requirements

- **Windows 10/11**
- **Python 3.10+** with tkinter (included in standard Python install)
- **Claude Code** installed and used at least once (creates `~/.claude/` data files)

## Installation

```bash
git clone https://github.com/premiumlion/windows-claude-code-usage-widget.git
cd windows-claude-code-usage-widget
```

No dependencies to install — it's a single Python file using only the standard library.

## Usage

### Run directly

```bash
# Run without console window (recommended)
pythonw claude-usage-widget.pyw

# Run with console (for debugging)
python claude-usage-widget.pyw
```

### Build as standalone exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --icon=claude-widget.ico --name="ClaudeUsageWidget" claude-usage-widget.pyw
# Exe output: dist/ClaudeUsageWidget.exe
```

### Auto-start on Windows boot

1. Press `Win + R`, type `shell:startup`, press Enter
2. Right-click in the folder → New → Shortcut
3. Target: `pythonw "C:\path\to\claude-usage-widget.pyw"` (or the built exe path)
4. Name it "Claude Usage Widget"

## How it works

The widget reads data from Claude Code's local files and the Anthropic OAuth API:

| Source | Data |
|--------|------|
| `~/.claude/stats-cache.json` | Usage stats (daily activity, token counts, model usage) |
| `~/.claude/sessions/*.json` | Active session detection (checks PIDs via `OpenProcess`) |
| `~/.claude/.credentials.json` | OAuth token for API calls, plan tier detection |
| `~/.claude/widget-state.json` | Widget position, width, collapse state, section visibility |
| `~/.claude/widget-usage-cache.json` | Cached API usage data for instant startup |
| Anthropic OAuth API | Live rate limit utilization (session/weekly/sonnet %) |

## Configuration

Edit the constants at the top of `claude-usage-widget.pyw`:

```python
REFRESH_MS = 30_000          # Stats refresh interval (milliseconds)
USAGE_REFRESH_MS = 120_000   # Usage API refresh interval
DEFAULT_WIDTH = 280          # Default widget width in pixels
MIN_WIDTH = 200              # Minimum resize width
MAX_WIDTH = 500              # Maximum resize width
WIDGET_OPACITY = 0.92        # Window opacity (0.0 - 1.0)

# Colors (dark theme)
BG = "#1a1a2e"               # Body background
BG_HEADER = "#0f3460"        # Header background
FG_ACCENT = "#00d4aa"        # Accent color (title, live indicator)
FG_BLUE = "#4fc3f7"          # Section headers
FG_SOFT_RED = "#e07070"      # Usage limits label
```

## Contributing

Pull requests welcome. This is a simple single-file widget — keep it that way.

## License

MIT
