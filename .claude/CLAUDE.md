# Claude Code Usage Widget

Single-file Python/tkinter desktop widget (`claude-usage-widget.pyw`). Always-on-top overlay showing Claude Code usage stats.

## Architecture

- **One file**: `claude-usage-widget.pyw` — no modules, no dependencies beyond stdlib
- **Data source**: Reads `~/.claude/stats-cache.json` (usage) and `~/.claude/sessions/*.json` (live sessions)
- **State**: Persists position + collapse state to `~/.claude/widget-state.json`
- **Platform**: Windows only (uses `ctypes.windll` for PID checks, tkinter `overrideredirect`)

## Key constraints

- Zero external dependencies — stdlib only (tkinter, json, ctypes, pathlib)
- Keep it a single file — no splitting into modules
- Windows 10/11 + Python 3.10+
- 30s auto-refresh cycle (`REFRESH_MS`)
- Config constants live at top of file (colors, dimensions, refresh rate)

## How the widget reads data

- `load_stats()` → parses `stats-cache.json` for daily activity, token counts, model usage
- `get_active_sessions()` → scans session JSONs, verifies PIDs via `OpenProcess`
- `get_model_summary()` → aggregates input/output/cache tokens across all models

## Deploying

After code changes, always kill the running widget before rebuilding:

```
taskkill //F //IM ClaudeUsageWidget.exe 2>/dev/null
sleep 2
cd "/c/Users/oddsm/OneDrive/PPCB Plugin 2025/PPCB - Claude Widget"
pyinstaller --onefile --windowed --icon=claude-widget.ico --name="ClaudeUsageWidget" claude-usage-widget.pyw
powershell -Command "Start-Process 'C:\\Users\\oddsm\\OneDrive\\PPCB Plugin 2025\\PPCB - Claude Widget\\dist\\ClaudeUsageWidget.exe'"
```

The widget has a single-instance mutex — launching a second copy will silently exit. Always kill the old one first.
