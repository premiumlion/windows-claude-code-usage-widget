# Claude Code Usage Widget

Single-file Python/tkinter desktop widget (`claude-usage-widget.pyw`). Always-on-top overlay showing Claude Code usage stats.

## Architecture

- **One file**: `claude-usage-widget.pyw` — no modules, no dependencies beyond stdlib
- **Data sources**: `~/.claude/stats-cache.json` (stats), `~/.claude/sessions/*.json` (live sessions), `~/.claude/.credentials.json` (OAuth + plan), Anthropic OAuth API (usage limits)
- **State**: Persists position, width, collapse state, section visibility to `~/.claude/widget-state.json`
- **Usage cache**: `~/.claude/widget-usage-cache.json` — cached API data for instant startup
- **Single instance**: Lock file at `~/.claude/widget.lock`
- **Platform**: Windows only (uses `ctypes.windll` for PID checks, `msvcrt` for file locking, tkinter `overrideredirect`)

## Key constraints

- Zero external dependencies — stdlib only (tkinter, json, ctypes, msvcrt, pathlib)
- Keep it a single file — no splitting into modules
- Windows 10/11 + Python 3.10+
- 30s stats refresh cycle (`REFRESH_MS`), 2 min usage API cycle (`USAGE_REFRESH_MS`)
- Config constants live at top of file (colors, dimensions, refresh rate)

## How the widget reads data

- `load_stats()` → parses `stats-cache.json` for daily activity, token counts, model usage
- `get_active_sessions()` → scans session JSONs, verifies PIDs via `OpenProcess`
- `get_model_summary()` → aggregates input/output/cache tokens across all models
- `fetch_usage()` → hits Anthropic OAuth API for rate limit utilization (returns data + retry_after)
- `load_usage_cache()` / `save_usage_cache()` → persist API data to avoid hitting API on every restart

## Deploying

After code changes, always kill the running widget before rebuilding:

```
taskkill //F //IM ClaudeUsageWidget.exe 2>/dev/null
rm -f "$USERPROFILE/.claude/widget.lock" 2>/dev/null
sleep 2
cd "/c/Users/oddsm/OneDrive/PPCB Plugin 2025/PPCB - Claude Widget"
pyinstaller --onefile --windowed --icon=claude-widget.ico --name="ClaudeUsageWidget" claude-usage-widget.pyw
powershell -Command "Start-Process 'C:\\Users\\oddsm\\OneDrive\\PPCB Plugin 2025\\PPCB - Claude Widget\\dist\\ClaudeUsageWidget.exe'"
```

The widget has a single-instance lock file — launching a second copy will silently exit. Always kill the old one and remove the lock first.
