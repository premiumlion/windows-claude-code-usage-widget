# Claude Code Usage Widget

A lightweight Windows desktop widget that displays your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) usage stats in a compact, always-on-top overlay.

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)
![Windows](https://img.shields.io/badge/platform-Windows-0078D6.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

## What it shows

| Section | Data |
|---------|------|
| **Header** | Live session count (with project names) |
| **Today** | Messages, sessions, tool calls |
| **Last 7 days** | Messages, sessions, tool calls, days active |
| **All-time tokens** | Input, output, cache reads (across all models) |
| **Totals** | Lifetime sessions and messages |

## Features

- **Always-on-top** — stays visible over other windows
- **Drag to reposition** — grab the header and move it anywhere
- **Click to collapse/expand** — click the header to toggle
- **Persistent state** — position and collapsed state saved across restarts
- **Auto-refresh** — updates every 30 seconds
- **Zero dependencies** — uses only Python stdlib (`tkinter`, `json`, `ctypes`)
- **Dark theme** — clean dark UI that doesn't distract

## Requirements

- **Windows 10/11**
- **Python 3.10+** with tkinter (included in standard Python install)
- **Claude Code** installed and used at least once (creates `~/.claude/stats-cache.json`)

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/claude-code-usage-widget.git
cd claude-code-usage-widget
```

No dependencies to install — it's a single Python file using only the standard library.

## Usage

```bash
# Run without console window (recommended)
pythonw claude-usage-widget.pyw

# Run with console (for debugging)
python claude-usage-widget.pyw
```

### Auto-start on Windows boot

1. Press `Win + R`, type `shell:startup`, press Enter
2. Right-click in the folder → New → Shortcut
3. Target: `pythonw "C:\path\to\claude-usage-widget.pyw"`
4. Name it "Claude Usage Widget"

## How it works

The widget reads data from Claude Code's local files:

| File | Data |
|------|------|
| `~/.claude/stats-cache.json` | Usage stats (daily activity, token counts, model usage) |
| `~/.claude/sessions/*.json` | Active session detection (checks PIDs) |
| `~/.claude/widget-state.json` | Widget position and expand/collapse state (created by widget) |

No API keys or network access needed — everything is read from local files that Claude Code already maintains.

## Configuration

Edit the constants at the top of `claude-usage-widget.pyw`:

```python
REFRESH_MS = 30_000      # Refresh interval (milliseconds)
WIDGET_WIDTH = 280       # Widget width in pixels
WIDGET_OPACITY = 0.92    # Window opacity (0.0 - 1.0)

# Colors (dark theme)
BG = "#1a1a2e"           # Body background
BG_HEADER = "#0f3460"    # Header background
FG_ACCENT = "#00d4aa"    # Accent color (title, live indicator)
FG_BLUE = "#4fc3f7"      # Section headers
```

## Contributing

Pull requests welcome. This is a simple single-file widget — keep it that way.

## License

MIT
