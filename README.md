# claude-code-statusline

One status line for [Claude Code](https://claude.com/claude-code): the same bar in every project, no per-repo config, one Python file, standard library only.

At 120 columns, in a dirty branch, with 74% of the 5-hour window used:

```
claude-…tusline › live-usage-… │ Opus 4.6 · high │ ctx 43% █████ 430k/1M │ 5h 74% █████ →2h │ 7d 48% █████ │ $103.21
```

At 200 columns everything fits, including the session name, the health cluster and the dev ports:

```
claude-code-statusline › live-usage-and-redesign ~2 │ Opus 4.6 · high │ ctx 43% █████ 430k/1M │ 5h 74% █████ →2h │ 7d 48% █████ │ $103.21 │ session 1h33m │ ✓ mcp 3 · 2.1.296 │ skills 69 │ :3000
```

And when something is wrong, each problem is its own item:

```
claude-code-statusline › live-usage-and-redesign ~2 “statusline-py-usage-accuracy” │ Opus 4.6 · high │ ctx 43% █████ 430k/1M │ 5h 74% █████ →2h │ 7d 48% █████ │ $103.21 │ ▲ discord │ ↑ 2.2.0
```

With `--icons nerd` the bars become three slanted segments and a few labels become glyphs. That needs a screenshot rather than a code block: run `python ~/.claude/statusline.py --demo --icons nerd` in your terminal to render these two sample lines at your window's width (nothing is read, fetched or written).

It is made for a 120-column window and drops optional items to fit narrower windows. The core model and percentage meters stay visible; allow at least 51 terminal columns (47 for the core plus Claude Code's padding). Below that minimum the core can wrap.

## What it shows

Left to right:

| Item | Looks like | Colour | Meaning |
|---|---|---|---|
| project › branch | `claude-code-statusline › main ~2 ?1` | project violet; branch green when clean, amber when dirty | Working directory name, then the git branch with `+staged ~modified ?untracked` counts |
| session name | `“fix-login-redirect”` | cyan | The session's name when it has one: the payload's `session_name` (`/rename`), else the newest `ai-title` / `agent-name` / `custom-title` entry in the last 256 KB of the transcript, as a hyphenated slug of at most 28 characters. With Nerd icons a tag glyph replaces the quotes |
| model · effort | `Opus 4.6 · high` | Opus terracotta, Sonnet blue, Haiku teal, Fable pink; effort grey, `xhigh` and `max` purple | Model display name (cut in the middle past 20 characters) and the reasoning effort when set |
| ctx | `ctx 43% █████ 430k/1M` | stage colour | Context window usage, a bar and the tokens used of the window size. `ctx 0%` before the first reply, `ctx --` only when the window size is unknown |
| 5h / 7d | `5h 74% █████ →2h` | stage colour | Plan usage for the 5-hour and 7-day windows, account-wide (see below). From 70% the time until the window resets follows the bar: `→45m`, `→2h`, `→3d`. `--` when a window has reset and no newer reading exists |
| cost | `$103.21` | green | Session cost in USD |
| session time | `session 12m` | grey | Wall-clock time of the session; a clock glyph replaces the word with Nerd icons |
| health | `✓ mcp 3 · 2.1.296` | green tick, grey text | All well: MCP servers connected in this session (from Claude Code's MCP logs) and the running Claude Code version. The tick only appears when something was actually checked |
| health problems | `▲ discord` `↑ 2.2.0` `~reload` | red, amber, grey | Each problem by name, replacing the cluster: a failed MCP server (`+N` for more), a newer Claude Code published (`↑ restart` when it is installed but this window still runs the old one), enabled plugins changed on disk after this session loaded them (advice, not a fault) |
| skills | `skills 69` | grey | Skills available to the session: enabled plugins' skills and commands, yours, the project's |
| venv, ports | `venv:.venv` `:3000,5173` | cyan, green | Active virtualenv or conda env, and the dev ports currently listening (`COMMON_PORTS`). Only when found |

Stage colours: green below 50%, yellow from 50, orange from 70, red from 85. Bars fill by stage too, so a bar never contradicts its colour: a 5-cell bar shows 1 cell below 25%, 2 below 50, 3 below 70, 4 below 85 and all 5 from 85; a 3-segment Nerd bar shows 1 segment while green, 2 while yellow or orange, 3 when red. Only exactly 0 is empty.

### Fitting the window

The line is kept to `COLUMNS` minus 4 (Claude Code pads the right edge). A wide window shows everything; a narrow one drops items in this order, each only while the line is still too wide:

1. venv and ports
2. skills
3. session time
4. the healthy health cluster
5. health problems, last first, each by name; when the last text goes, one bare marker stands in for all of them with the glyph and colour of the worst (`▲` red, `↑` amber, `~` grey) and, given 8 free columns, its one-word reason: `▲discord`. It outlasts everything below except model and percentages, so a failure is never invisible
6. the session name
7. the branch: it is cut from its end to fit the room left while the project stays whole, then dropped with its dirty counts (it stays amber while present)
8. the project: squeezed down to 14 characters, then dropped
9. the ctx tokens
10. the bars (7d, then 5h, then ctx)
11. the cost
12. the effort
13. the model name, cut to 8 characters
14. the bare marker

Model and the three percentages always stay, with their `→reset` countdowns: `Opus 4.6 │ ctx 100% │ 5h 100% →1h │ 7d 100% →3d` is the 47-column floor.

### Icons

Plain mode (the default) uses only glyphs that Consolas, Cascadia, Menlo and DejaVu have: `█` bars, `✓ ↑ ▲ ~ → ›`. `--icons nerd` (or `STATUSLINE_ICONS=nerd`) swaps in [Nerd Font](https://www.nerdfonts.com/) glyphs (Font Awesome and Devicons code points, so any Nerd Font version works) only where they replace a word: the bars become slanted segments, the health marks, the clock, the session-name tag, venv and ports get glyphs. Labels such as `ctx`, `$` and the model name stay plain text in both modes.

The classic Windows console host cannot draw Nerd Font glyphs, so `nerd` falls back to plain there. Claude Code spawns the status line without a console of its own, so the script looks up the process chain for the window that hosts the session (a `ConsoleWindowClass` window means conhost, a `PseudoConsoleWindow` means Windows Terminal, VS Code or another ConPTY host); the verdict is cached for an hour. No verdict keeps the icons you asked for. `STATUSLINE_FORCE_ICONS=1` skips the check.

### Light terminals

`STATUSLINE_THEME=light` swaps the palette for one with at least 3:1 contrast on a white background (grey 244, green 28, yellow 136, orange 166, red 160, lighter separators). The default is the dark palette.

## Install

**Requirements:** Python 3.10 or newer, a terminal with 256 colours (Windows Terminal, iTerm2, macOS Terminal, GNOME Terminal). No Nerd Font needed.

### One command

With [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/) on the machine (macOS, Linux and Windows):

```bash
uvx --from git+https://github.com/Doofess/claude-code-statusline claude-statusline install
# or
pipx run --spec git+https://github.com/Doofess/claude-code-statusline claude-statusline install
```

It does everything the icons need, so the line works as soon as you restart Claude Code:

1. Copies `statusline.py` to `~/.claude`, backs up `settings.json` (to `~/.claude/backups/statusline-<time>/`), and sets `statusLine` to the fast launch below with `refreshInterval` 1.
2. **Installs a Nerd Font** when the machine has none: winget on Windows and Homebrew on macOS when present, otherwise a download of the pinned CaskaydiaMono Nerd Font release from GitHub (about 56 MB, four files, per user, no admin rights).
3. **Sets that font in your terminal** when it can: Windows Terminal (`profiles.defaults.font.face`) and VS Code (`terminal.integrated.fontFamily`). A file that already names a Nerd Font is left alone; one that names another font gets the Nerd Font in its place, and every file is backed up first. Other terminals (iTerm2, Terminal.app, GNOME Terminal) cannot be configured safely from a script, so it prints where to pick the font.
4. Renders a sample line as a self-test and puts `settings.json` back if that fails.

Nothing stays installed in uv or pipx: the status line is the one copied file, so a render never pays for a package wrapper. **Windows Terminal reads installed fonts only when it starts**, so after a first install close every Windows Terminal window and open a new one, then restart Claude Code.

| Flag | Effect |
| --- | --- |
| `--icons auto` (default) | Nerd icons, installing the font if needed; plain when no font can be had. `nerd` and `none` force one (`none` skips the font work). |
| `--no-font` | Never install a font or edit terminal settings. |
| `--dry-run` | Print what it would do (font, terminal files, settings); write nothing. |
| `--no-test` | Skip the self-test. |
| `--claude-dir PATH` | Another config directory (default `CLAUDE_CONFIG_DIR`, else `~/.claude`). |

Run it again to update (it replaces the script and keeps your original `statusLine` for uninstall). To remove it:

```bash
uvx --from git+https://github.com/Doofess/claude-code-statusline claude-statusline uninstall
```

That restores the `statusLine` you had before, or removes ours, and deletes the copied script and its caches. It refuses to touch a `settings.json` that is not valid JSON.

Without uv or pipx, `python statusline.py install` does the same from a clone, and the two manual steps below still work.

### 1. Drop the file

```bash
# macOS / Linux
curl -o ~/.claude/statusline.py https://raw.githubusercontent.com/roeimichael/claude-code-statusline/main/statusline.py
```

```powershell
# Windows (PowerShell)
iwr https://raw.githubusercontent.com/roeimichael/claude-code-statusline/main/statusline.py -OutFile "$env:USERPROFILE\.claude\statusline.py"
```

Or copy `statusline.py` from this repo to `~/.claude/statusline.py`.

### 2. Wire it into settings

In `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python ~/.claude/statusline.py",
    "refreshInterval": 10
  }
}
```

The same command works on macOS, Linux and Windows (Claude Code expands `~`). `refreshInterval` (seconds) re-renders while the session is idle; without it Claude Code only redraws on new messages, so the usage numbers freeze between turns.

With a Nerd Font set as your terminal font (e.g. *JetBrainsMono Nerd Font*), add the flag:

```json
"command": "python ~/.claude/statusline.py --icons nerd"
```

Getting the font: `winget install DEVCOM.JetBrainsMonoNerdFont` on Windows, `brew install --cask font-jetbrains-mono-nerd-font` on macOS, or download one from [nerdfonts.com](https://www.nerdfonts.com). Then select it in the terminal's font setting (Windows Terminal: Settings, Defaults, Appearance, Font face). Without the font the glyphs show as boxes; leave the flag off and the plain mode needs nothing extra.

Optional, a faster launch: `python -S` skips the `site` module, and importing the script lets Python reuse the compiled module in `~/.claude/__pycache__` instead of compiling the file on every render. Same output, a few milliseconds less per render:

```json
"command": "python -S -c \"import os,sys;sys.path.insert(0,os.path.expanduser('~/.claude'));import statusline;statusline.main()\" --icons nerd"
```

If you use [uv](https://github.com/astral-sh/uv), `uv run ~/.claude/statusline.py` works as well.

### 3. Restart Claude Code

New session, new bar. `python ~/.claude/statusline.py --demo` shows the two sample lines above at your window's width.

## How it works

Claude Code pipes a JSON payload to the `statusLine` command (model, context window, cost, this session's rate limits, workspace, transcript path). The script reads it from stdin (always as UTF-8, whatever the console code page), runs a few local probes, and prints one line.

**Probe cache.** The slow probes keep their results in one small JSON file shared by all sessions, `~/.claude/statusline-probe-cache.json`: git status 3 s per directory, dev ports 10 s, health 10 s per session, the MCP log scan 10 s per project (shared between the project's sessions), the skill count 60 s, the session name until the transcript grows, the console verdict an hour. Only one session recomputes an expired or missing entry at a time; the others reuse the old value or wait briefly for the new one. The payload's live numbers (percentages, cost, time) are never cached. A missing, corrupt or unwritable cache file only means the probes run again.

**Live usage.** The `rate_limits` in the payload come from *this session's* last API reply. Your plan quota is shared by every session, claude.ai and the desktop app, so a quiet session shows old numbers until it next talks to the API, and a new session has none. So the script also reads the endpoint behind `/usage`, `https://api.anthropic.com/api/oauth/usage`, in a detached background process at most once per `USAGE_TTL` (60 s) and keeps the result in `~/.claude/statusline-usage.json`; rendering never waits on the network. Usage only grows within a window, so for each window the higher of the two readings is shown, and a reading for a newer window replaces one whose window has reset.

*What is read:* the Claude subscription token Claude Code already stores in `~/.claude/.credentials.json` (or the macOS Keychain). *What is sent:* that token, as a bearer header, to `api.anthropic.com` only; the same background process also reads the published Claude Code versions from `registry.npmjs.org` (no token) once an hour, for the `↑` mark. Nothing else leaves your machine. `STATUSLINE_LIVE_USAGE=0` turns the fetching off; the bar then shows the payload's numbers alone.

*API-key users* have no subscription token, so nothing is fetched, and Claude Code sends no `rate_limits` either: the `5h` and `7d` items are not shown at all. They appear as soon as either source has had them, and read `--` once a window has reset with no newer reading. The endpoint is undocumented and could change; if it fails, the bar falls back to the payload's numbers.

**Timing.** Everything slow is imported lazily or cached, so a warm render is a few tens of milliseconds, which matters because Claude Code cancels a render that is still running when the next update arrives.

## Customize

Everything lives at the top of `statusline.py`:

- **Ports** — `COMMON_PORTS`.
- **Colours** — `THEMES` holds the dark and light palettes as xterm-256 indices; `STAGE_LIMITS` the 50/70/85 thresholds; `BAR_FILL` how bars fill by stage.
- **Health** — `SHOW_HEALTH = False` leaves out the health items and the skill count.
- **Usage refresh** — `USAGE_TTL`, `RELEASE_TTL`; `RESET_FROM` is the percentage from which a window shows its countdown.
- **Items and drop order** — `format_statusline()` builds the items and lists the drop order as patches (`steps`); its docstring is the reference.

Claude Code also honours `.claude/settings.local.json` inside a project, so one repo can point at a different script while everything else inherits this one.

## Differences from upstream

This fork keeps the one-file, standard-library shape and adds:

- Account-wide usage from the `/usage` endpoint merged with the payload's numbers, in a background process with an on-disk cache and a lock (see above).
- A health cluster: MCP servers connected in this session, the running Claude Code version against the installed builds and the published release, plugins changed on disk since they were loaded.
- The session name from the transcript, the skill count, a probe cache shared between sessions, and width fitting with a documented drop order instead of a fixed layout.
- Nerd icon mode with a fallback for the classic Windows console, a light palette, terminal-cell width measurement (wide and zero-width characters), and UTF-8 stdin on any Windows code page.
- `--demo` for screenshots, `--refresh` (the background fetch) and an importable `main()` for the fast launch.

## License

MIT, see [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. The script is small on purpose; please keep additions modular and opt-in.

Tests: `python -m unittest discover` (stdlib only). Point `CLAUDE_CONFIG_DIR` at a scratch folder and set `STATUSLINE_LIVE_USAGE=0` when running the script by hand, so your real caches stay untouched.
