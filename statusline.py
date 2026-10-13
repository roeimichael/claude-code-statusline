# /// script
# requires-python = ">=3.10"
# ///

"""Unified status line for Claude Code.

One line, always (Claude Code draws its own mode line below it), left to right:
    project › branch ~N “session-name” │ model · effort │ ctx N% bar used/size │
    5h N% bar →reset │ 7d N% bar →reset │ $cost │ session time │ ✓ mcp N · version │
    skills N │ venv │ :ports

It is designed for a 120-column window: wider windows show more, narrower ones drop
items by priority (see format_statusline). Below the core meter width it can wrap.
Items with nothing to show (no session name, no ports, no venv) are left out, and a
health problem replaces the ✓ cluster with its own item: ▲ discord, ↑ 2.2.0, ~reload.
Standalone: the standard library only, Python 3.10+.

Usage numbers: Claude Code only pipes in the rate limits from *this* session's
last reply, so they lag whenever other sessions (or claude.ai) use the shared
quota. The script also reads the endpoint behind /usage in a background process,
caches it for USAGE_TTL seconds, and shows the higher of the two within a window.
Set STATUSLINE_LIVE_USAGE=0 to turn that off.

Install, one command (uv or pipx; see the README for the repo URL):
    uvx --from git+https://github.com/Doofess/claude-code-statusline claude-statusline install
    (or `python statusline.py install`; `uninstall` undoes it; `--dry-run` shows the changes)

Or by hand:
    1. Drop this file at ~/.claude/statusline.py
    2. Add to ~/.claude/settings.json:
           "statusLine": {
               "type": "command",
               "command": "python ~/.claude/statusline.py",
               "refreshInterval": 10
           }
       Add "--icons nerd" with a Nerd Font, STATUSLINE_THEME=light on a light terminal.
    3. Restart your Claude Code session.

`python statusline.py --demo` prints two sample lines at your window's width (nothing is
read or written). See https://github.com/roeimichael/claude-code-statusline for docs.
"""

import json
import os
import re
import sys
import time
import zlib

# subprocess, socket, datetime and unicodedata are imported where they are used: a render
# with a warm probe cache needs none of them, and it runs every second. pathlib is not
# used at all: it costs ~4 ms of import per render.

# ANSI colors (256-color palette, so macOS Terminal.app renders them too)
def fg(n):
    return f"\033[38;5;{n}m"


RST = "\033[0m"

# Two palettes of xterm-256 indices. The dark one is the default; STATUSLINE_THEME=light
# picks colours with at least 3:1 contrast on a white background. Stage colours (green,
# yellow, orange, red by percentage) are kept for the meters alone: no model family, effort
# level or other text borrows one, so a stage colour always means a stage.
THEMES = {
    "dark": {
        "DIM": 252,      # secondary text: the terminal's usual light grey, not a faint dim
        "SEP": 240,      # the │ between items: dimmer than the labels
        "TRACK": 238,    # the empty part of a bar
        "STAGES": (114, 221, 208), "RED": 203,
        "GREEN": 114, "AMBER": 221, "CYAN": 80,  # clean branch and ticks; dirty branch and updates; name, venv
        "PROJECT": 141,  # violet
        "MODEL": 173,    # Claude terracotta: Opus and unknown models
        "ACCENT": 141,   # the max and xhigh effort levels
        "MODEL_COLORS": {"opus": 173, "sonnet": 75, "haiku": 43, "fable": 213},
    },
    "light": {
        "DIM": 244, "SEP": 250, "TRACK": 250,
        "STAGES": (28, 136, 166), "RED": 160,
        "GREEN": 28, "AMBER": 136, "CYAN": 30,
        "PROJECT": 55, "MODEL": 130, "ACCENT": 55,
        "MODEL_COLORS": {"opus": 130, "sonnet": 26, "haiku": 30, "fable": 127},
    },
}
STAGE_LIMITS = (50, 70, 85)  # yellow from 50%, orange from 70%, red from 85%


def set_theme(name):
    """Install the named palette ('dark' unless name is 'light') into the colour globals."""
    global DIM, SEP, BAR_TRACK, STAGES, RED, GREEN, AMBER, CYAN, PROJECT, MODEL, COST, MODEL_COLORS, EFFORT
    t = THEMES.get(name) or THEMES["dark"]
    DIM, RED, GREEN, AMBER, CYAN = fg(t["DIM"]), fg(t["RED"]), fg(t["GREEN"]), fg(t["AMBER"]), fg(t["CYAN"])
    PROJECT, MODEL, COST = fg(t["PROJECT"]), fg(t["MODEL"]), fg(t["GREEN"])
    STAGES = tuple(zip(STAGE_LIMITS, (fg(n) for n in t["STAGES"])))  # (below this %, colour), then RED
    SEP = f" {fg(t['SEP'])}│{RST} "  # one space each side: room for three bars on one 120-column line
    BAR_TRACK = fg(t["TRACK"])
    MODEL_COLORS = {family: fg(n) for family, n in t["MODEL_COLORS"].items()}
    EFFORT = {"low": DIM, "medium": DIM, "high": DIM, "xhigh": fg(t["ACCENT"]), "max": fg(t["ACCENT"])}


set_theme("dark")


def model_color(model_id):
    m = str(model_id or "").lower()
    for family, color in MODEL_COLORS.items():
        if family in m:
            return color
    return MODEL


# Columns kept free at the window's right edge (Claude Code pads the status line).
EDGE = 4
ANSI_RE = re.compile(r"\033\[[0-9;]*m")


ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0xFE0F}  # zero-width space/joiners, emoji variation selector


def visible_width(s):
    """Terminal cells the string takes: colour codes 0, East Asian wide and emoji 2, combining marks 0."""
    s = ANSI_RE.sub("", s)
    if s.isascii():
        return len(s)
    import unicodedata
    width = 0
    for i, c in enumerate(s):
        o = ord(c)
        if o < 0x300:
            width += 1
        elif o in ZERO_WIDTH or unicodedata.combining(c):
            continue
        elif unicodedata.east_asian_width(c) in "WF" or 0x1F300 <= o <= 0x1FAFF:
            width += 2
        elif 0x2600 <= o <= 0x27BF and s[i + 1:i + 2] == "️":
            width += 2  # a symbol asked to render as an emoji
        else:
            width += 1
    return width


def mid_trunc(s, n):
    """Shorten s to n characters by replacing its middle with '…'."""
    if len(s) <= n:
        return s
    if n <= 1:
        return "…"[:n]
    keep = n - 1
    head = keep // 2
    return s[:head] + "…" + s[len(s) - (keep - head):]
BAR = "\u2588"  # full block: fills the whole cell height
# Bars fill by stage, so the bar and its colour always agree: a 5-cell bar shows 1 cell below
# 25%, 2 below 50 (green never shows more than 2), 3 below 70, 4 below 85 and all 5 from 85
# (red is always full); a 3-segment bar shows 1 while green, 2 while yellow or orange, 3 when
# red. Only exactly 0 (or unknown) is empty.
BAR_FILL = {5: (25, 50, 70, 85), 3: (50, 85)}

# Health marks: MCP servers, Claude Code version and plugin reload state.
SHOW_HEALTH = True

# Edit this list to match the dev ports you care about.
COMMON_PORTS = [3000, 3001, 5173, 5174, 8000, 8080, 8081, 8086, 9086, 9087, 54321]

# Live usage (the data /usage shows). Seconds between background fetches.
USAGE_TTL = 60
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
HOME = os.path.expanduser("~")  # USERPROFILE on Windows
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
USAGE_CACHE = os.path.join(CLAUDE_DIR, "statusline-usage.json")
USAGE_LOCK = os.path.join(CLAUDE_DIR, "statusline-usage.lock")
WINDOWS = ("five_hour", "seven_day")

# Release check: newest Claude Code on your update channel. Seconds between fetches.
RELEASE_TTL = 3600
RELEASE_URL = "https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags"
NATIVE_VERSIONS = os.path.join(HOME, ".local", "share", "claude", "versions")

# Icons. "none" uses plain text; "nerd" needs a Nerd Font in the terminal
# (e.g. CaskaydiaMono Nerd Font). Pick with --icons nerd or STATUSLINE_ICONS=nerd.
# Plain mode also serves the classic Windows console (Consolas), so only glyphs it has.
# Icons only where they stand in for a label or a word, never next to one.
# The Nerd icons are Font Awesome and Devicons code points (U+E73C, U+F017..U+F252), which every Nerd Font
# version has. The Material Design set at U+F0001 and up only exists in Nerd Fonts 3+; an older font draws
# those as placeholder diamonds.
PLAIN_ICONS = {"ok": "\u2713", "up": "\u2191", "warn": "\u25b2", "reload": "~"}
NERD_ICONS = {
    "five_hour": "",   # fa-hourglass-half
    "seven_day": "",   # fa-calendar
    "duration": "",    # fa-clock-o: replaces the word 'session'
    "venv": "",        # dev-python
    "ports": "",       # fa-plug
    "ok": "",          # fa-check-circle
    "up": "",          # fa-arrow-circle-up
    "reload": "",      # fa-refresh
    "warn": "",        # fa-warning
    "tag": "",         # fa-tag: replaces the quotes round the session name
    "segment": "",  # powerline slants: one tilted full-height bar segment
}
ICON = dict(PLAIN_ICONS)


CONSOLE_TTL = 3600  # a session's terminal never changes while it runs; its verdict is kept this long


def console_host(chain, windows):
    """'classic' (a conhost window), 'pty' (ConPTY: Windows Terminal, VS Code, ...) or None (no verdict).

    chain: this process's ancestors, nearest first. windows: {pid: [(window class, visible)]},
    the top-level windows those processes own. Windows attributes a console window to the
    attached client process, not to conhost.exe, so the nearest ancestor owning a visible
    console window decides: a classic window opened from a Windows Terminal shell has
    cmd.exe [ConsoleWindowClass] nearer than pwsh.exe [PseudoConsoleWindow]. No such
    window anywhere up the chain is not a verdict: Claude Code spawns the status line
    without a console of its own, in Windows Terminal just as in the classic console.
    """
    for pid in chain:
        classes = {cls for cls, visible in windows.get(pid, ()) if visible}
        if "ConsoleWindowClass" in classes:
            return "classic"
        if "PseudoConsoleWindow" in classes:
            return "pty"
    return None


def _probe_console_host():
    """console_host() for this process, asked of Windows through ctypes (~6 ms)."""
    import ctypes
    import ctypes.wintypes as w
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", w.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32.CreateToolhelp32Snapshot.restype = w.HANDLE
    parents = {}
    snapshot = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            more = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    chain, pid = [], os.getpid()
    while pid and pid in parents and len(chain) < 16:
        chain.append(pid)
        pid = parents[pid]
    wanted, windows = set(chain), {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
    def visit(hwnd, _):
        owner = w.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in wanted:
            buf = ctypes.create_unicode_buffer(64)
            u32.GetClassNameW(hwnd, buf, 64)
            windows.setdefault(owner.value, []).append((buf.value, bool(u32.IsWindowVisible(hwnd))))
        return True

    u32.EnumWindows(visit, 0)
    return console_host(chain, windows)


def terminal_kind(cache=None, session=None):
    """console_host() for this Windows session, kept CONSOLE_TTL seconds; None elsewhere.

    The verdict is stored per Claude Code session (its session_id), because the parent
    process is no use as a key: Claude Code starts the status line through a shell that
    lives for one render, so the parent pid is new every second and nothing would ever hit.
    Without a session id the parent pid is the fallback key.

    A terminal that announces itself (WT_SESSION, TERM_PROGRAM) is never asked about: the
    answer would be 'keep nerd' and ctypes costs ~6 ms. Any failure is no verdict.
    """
    if os.name != "nt" or os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return None

    def probe():
        try:
            return _probe_console_host()
        except Exception:
            return None
    return _probe_cache(cache).get(f"console:{session or os.getppid()}", CONSOLE_TTL, probe)


def effective_icons(mode, cache=None, session=None):
    """The requested icon set, unless this terminal cannot draw it.

    The classic Windows console shows Nerd Font glyphs as boxes (and miscounts their
    width), so 'nerd' falls back to 'none' there, and only there: no verdict keeps the
    requested set. STATUSLINE_FORCE_ICONS=1 skips the check. cache: the render's ProbeCache.
    """
    if mode != "nerd" or os.environ.get("STATUSLINE_FORCE_ICONS"):
        return mode
    return "none" if terminal_kind(cache, session) == "classic" else mode


def set_icons(mode):
    ICON.clear()
    ICON.update(NERD_ICONS if mode == "nerd" else PLAIN_ICONS)


def ic(name, color=DIM):
    """Icon followed by a space, or '' when the current icon set has none."""
    g = ICON.get(name)
    return f"{color}{g}{RST} " if g else ""


def color_pct(p):
    """Four clear stages: green, yellow from 50%, orange from 70%, red from 85%."""
    for limit, color in STAGES:
        if p < limit:
            return color
    return RED


def bar(p, width=5, segments=3):
    """A bar of `width` full blocks, or `segments` slanted segments with Nerd icons, filled by stage (BAR_FILL)."""
    if ICON.get("segment"):
        seg, width = ICON["segment"], segments
    else:
        seg = BAR
    filled = 0 if p <= 0 else 1 + sum(p >= limit for limit in BAR_FILL[width])
    return f"{color_pct(p)}{seg * filled}{RST}{BAR_TRACK}{seg * (width - filled)}{RST}"


def meter(label, p, icon, with_bar=True, suffix=""):
    """'ctx 43% █████ 430k/1M': label and value in the stage colour, then the bar and a grey suffix.

    'ctx --' (no bar, no suffix) when the reading is unknown.
    """
    if p is None:
        return f"{ic(icon)}{DIM}{label} --{RST}"
    c = color_pct(p)
    out = f"{ic(icon, c)}{c}{label}{RST} {c}{p:.0f}%{RST}"
    if with_bar:
        out += f" {bar(p)}"
    if suffix:
        out += f" {DIM}{suffix}{RST}"
    return out


def fmt_duration(ms):
    if not ms or ms <= 0:
        return ""
    s = ms // 1000
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m"
    return f"{s}s"


RESET_FROM = 70  # a window shows when it resets only once it is this full


def fmt_reset(seconds):
    """Time until a window resets, as an arrow and one unit, at most four characters: →45m, →2h, →3d."""
    if not _number(seconds) or seconds <= 0:
        return ""
    m = int(seconds // 60)
    if m >= 1440:
        return f"→{m // 1440}d"
    if m >= 60:
        return f"→{m // 60}h"
    return f"→{max(m, 1)}m"


def fmt_tokens(n):
    """Compact token count: 950, 12k, 135k, 1M, 1.2M."""
    if n < 1000:
        return str(n)
    if n < 999_500:
        return f"{round(n / 1000)}k"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _tokens_used(cw):
    """Tokens in the context window right now, or None before the first reply (no counts at all)."""
    u = cw.get("current_usage")
    if not isinstance(u, dict) or all(u.get(k) is None for k in TOKEN_KEYS):
        return None
    try:
        return sum(int(u.get(k) or 0) for k in TOKEN_KEYS)
    except (TypeError, ValueError):
        return None


def context_tokens(cw):
    """'used/size' tokens in the context window, or '' when the size or the usage is unknown."""
    size = cw.get("context_window_size")
    used = _tokens_used(cw)
    if not _number(size) or size <= 0 or used is None:
        return ""
    return f"{fmt_tokens(used)}/{fmt_tokens(int(size))}"


def context_pct(cw):
    """Context window usage in percent: Claude Code's figure, else worked out from the tokens.

    A known window size with no usage yet is 0%; None only when the size is unknown too.
    """
    try:
        pct = float(cw.get("used_percentage"))
        if _number(pct):
            return pct
    except (TypeError, ValueError, OverflowError):
        pass
    size = cw.get("context_window_size")
    if not _number(size) or size <= 0:
        return None
    return min(100.0, (_tokens_used(cw) or 0) / size * 100)


def fmt_cost(c):
    return f"{COST}${c:.2f}{RST}"


def run(cmd, timeout=1.0):
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# --- probe cache -------------------------------------------------------------
# With a 1-second refreshInterval every render must be cheap, so the slow probes
# (git, port scan, health) keep their results for a few seconds in one JSON file
# shared by all sessions. Only probe results go in, never the stdin payload's live
# numbers (percentages, cost, session time), so those are always current.

PROBE_CACHE_NAME = "statusline-probe-cache.json"
GIT_TTL = 3        # git status, per directory
PORTS_TTL = 10     # dev port scan
HEALTH_TTL = 10    # version status, plugins to reload, MCP status, per session and project
SKILLS_TTL = 60    # skill count (a folder scan), per project
NAME_REFRESH = 30  # the session name is keyed by transcript size, not a TTL; it is re-stored this often so it never expires


class ProbeCache:
    """{key: {"at": epoch, "v": value}} loaded once per render and saved at the end.

    Values come back as JSON, so tuples come back as lists. A missing, corrupt or
    unwritable file only means the probes run again; it never breaks rendering.
    """

    SKEW = 2          # seconds another session's clock reading may be ahead of ours
    CLAIM_TTL = 5     # seconds before a claim left by a dead session is ignored
    CLAIM_WAIT = 0.25  # seconds to wait for another session's probe of a key we have no value for
    KEEP = 60         # seconds an entry stored without a TTL (put) stays in the file
    MAX_VERDICTS = 40  # terminal verdicts ('console:<session>') kept; the oldest go first

    def __init__(self, path, now):
        self.path, self.now, self.dirty = path, now, False
        self.touched = set()  # keys this render computed: the only ones it writes back
        self.entries = self._read()

    def _read(self):
        if self.path is None:
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            return {}
        return entries if isinstance(entries, dict) else {}

    @staticmethod
    def _at(e):
        """The entry's timestamp, or None when it is not a well-formed entry."""
        if isinstance(e, dict) and "v" in e and _number(e.get("at")):
            return e["at"]
        return None

    def _claim_path(self, key):
        return f"{self.path}.{zlib.crc32(key.encode('utf-8')):08x}.claim"

    def _claim(self, key):
        """True when this session may recompute key now; False when another one already is."""
        claim = self._claim_path(key)
        for _ in range(2):
            try:
                os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return True
            except FileExistsError:
                try:
                    if time.time() - os.stat(claim).st_mtime < self.CLAIM_TTL:
                        return False
                    os.unlink(claim)  # a session died mid-probe
                except OSError:
                    return False
            except OSError:
                return True  # cannot coordinate: just probe
        return False

    def _release(self, key):
        try:
            os.unlink(self._claim_path(key))
        except OSError:
            pass

    def _fresh(self, e, ttl):
        at = self._at(e)
        return at is not None and -self.SKEW <= self.now - at < ttl

    def get(self, key, ttl, compute):
        """The cached value for key while younger than ttl seconds, else compute() (and keep it).

        Only one session recomputes a key at a time (a claim file). The others use the
        expired value if they have one, or wait up to CLAIM_WAIT seconds for the result.
        A session that wins the claim re-reads the file first: another one may have just
        stored the value. A computed value is saved before the claim is released.
        """
        e = self.entries.get(key)
        if self._fresh(e, ttl):
            return e["v"]
        claimed = False
        if self.path is not None:
            claimed = self._claim(key)
            if not claimed:
                if self._at(e) is not None:
                    return e["v"]  # stale, but another session is already on it
                self._await_claim(key)
            e = self._read().get(key)
            if self._fresh(e, ttl):
                if claimed:
                    self._release(key)
                self.entries[key] = e
                return e["v"]
        try:
            value = compute()
            self.entries[key] = {"at": self.now, "v": value, "ttl": ttl}
            self.touched.add(key)
            self.dirty = True
            if claimed:
                self.save()
        finally:
            if claimed:
                self._release(key)
        return value

    def _await_claim(self, key):
        """Wait (briefly) for another session's claim on key to be released."""
        claim = self._claim_path(key)
        deadline = time.monotonic() + self.CLAIM_WAIT
        while os.path.exists(claim) and time.monotonic() < deadline:
            time.sleep(0.005)

    def peek(self, key):
        """A copy of the dict stored under key (even an expired one), or {}."""
        e = self.entries.get(key)
        return dict(e["v"]) if self._at(e) is not None and isinstance(e["v"], dict) else {}

    def age(self, key):
        """Seconds since key was stored, or None when it is missing or malformed."""
        at = self._at(self.entries.get(key))
        return None if at is None else self.now - at

    def put(self, key, value):
        """Store value under key now, without any TTL check."""
        self.entries[key] = {"at": self.now, "v": value}
        self.touched.add(key)
        self.dirty = True

    def save(self):
        """Merge with what is on disk now, then write atomically (tmp file + os.replace).

        Other sessions save to the same file, so this render's computed entries are laid
        over a fresh read (the newest `at` per key wins) instead of over the copy loaded
        at the start. Entries past their own TTL (KEEP seconds when they have none) are dropped.
        """
        if self.path is None or not self.dirty:
            return
        tmp = f"{self.path}.{os.getpid()}.tmp"
        lock = f"{self.path}.lock"
        locked = self._lock(lock)  # without it (timeout, odd folder) the merge is just best effort
        try:
            merged = {k: e for k, e in self._read().items() if self._at(e) is not None}
            for key in self.touched:
                mine, theirs = self.entries[key], merged.get(key)
                if theirs is None or theirs["at"] <= mine["at"]:
                    merged[key] = mine
            entries = {k: e for k, e in merged.items()
                       if -self.SKEW <= self.now - e["at"] < (e.get("ttl") if _number(e.get("ttl")) else self.KEEP)}
            # Terminal verdicts live an hour. A key that never repeats must not pile up, so keep the newest few.
            verdicts = sorted((k for k in entries if k.startswith("console:")), key=lambda k: entries[k]["at"])
            for k in verdicts[:-self.MAX_VERDICTS]:
                del entries[k]
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entries, f)
            for attempt in range(3):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:  # a reader has the file open (Windows)
                    if attempt == 2:
                        raise
                    time.sleep(0.005)
            self.touched.clear()
            self.dirty = False
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        finally:
            if locked:
                try:
                    os.unlink(lock)
                except OSError:
                    pass

    def _lock(self, lock):
        """Hold the save lock (a file made with O_EXCL), waiting up to ~50 ms; False if not got."""
        deadline = time.monotonic() + 0.05
        while True:
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return True
            except FileExistsError:
                try:
                    if time.time() - os.stat(lock).st_mtime > 2:
                        os.unlink(lock)  # left by a killed session
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.002)
            except OSError:
                return False


def _probe_cache(cache):
    """The render's ProbeCache, or a throwaway one (always probes) when none is given."""
    return cache if cache is not None else ProbeCache(None, time.time())


# --- usage -----------------------------------------------------------------

def _epoch(v):
    """resets_at arrives as epoch seconds (stdin) or ISO 8601 (usage endpoint)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window(pct, resets_at, now):
    try:
        pct = float(pct)
    except (TypeError, ValueError, OverflowError):
        return None
    if not _number(pct):
        return None
    reset = _epoch(resets_at)
    if reset is not None and not _number(reset):
        return None
    if reset is not None and reset <= now:
        # The window rolled over; this reading says nothing about the new one.
        return None
    return {"pct": pct, "resets_at": reset}


def merge_usage(stdin_rl, live, now):
    """Combine this session's rate limits with the account-wide live reading.

    Usage only grows within a window, so for the same window the higher number
    is the fresher one. A window that resets later is a newer window and wins.
    Returns {window: {"pct", "resets_at"} | None}; None means unknown.
    """
    stdin_rl = stdin_rl or {}
    live = live or {}
    out = {}
    for key in WINDOWS:
        s = stdin_rl.get(key)
        s = s if isinstance(s, dict) else {}
        l = live.get(key)
        l = l if isinstance(l, dict) else {}
        cands = [
            w for w in (
                _window(s.get("used_percentage"), s.get("resets_at"), now),
                _window(l.get("utilization"), l.get("resets_at"), now),
            ) if w
        ]
        if not cands:
            out[key] = None
            continue
        # Same window when resets_at agree to within a minute (the sources round differently).
        latest = max((w["resets_at"] or 0) for w in cands)
        same = [w for w in cands if (w["resets_at"] or 0) >= latest - 60]
        out[key] = max(same, key=lambda w: w["pct"])
    return out


def read_oauth_token():
    """The Claude subscription token, or None (API-key users, expired, unreadable)."""
    try:
        try:
            with open(os.path.join(CLAUDE_DIR, ".credentials.json"), encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            if sys.platform != "darwin":
                return None
            raw = run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"], 2.0)
        oauth = json.loads(raw)["claudeAiOauth"]
        expires = oauth.get("expiresAt")
        if expires and float(expires) / 1000 <= time.time():
            return None  # Claude Code refreshes it on its next request.
        token = oauth.get("accessToken")
        return token if isinstance(token, str) and token else None
    except Exception:  # no file, bad JSON, null oauth block, odd types: anything means "no token"
        return None


def _number(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and -float("inf") < v < float("inf"))


def _clean_usage_cache(raw):
    """Only the well-shaped parts of a parsed usage cache; anything else is dropped."""
    if not isinstance(raw, dict):
        return {}
    out = {k: raw[k] for k in ("fetched_at", "usage_at", "tags_at") if _number(raw.get(k))}
    if isinstance(raw.get("error"), str):
        out["error"] = raw["error"]
    tags = raw.get("tags")
    if isinstance(tags, dict):
        out["tags"] = {k: v for k, v in tags.items() if isinstance(k, str) and isinstance(v, str)}
    usage = raw.get("usage")
    if isinstance(usage, dict):
        clean = {}
        for key in WINDOWS:
            w = usage.get(key)
            if not isinstance(w, dict) or not _number(w.get("utilization")):
                continue
            resets = w.get("resets_at")
            if resets is not None and not (_number(resets) or (isinstance(resets, str) and _epoch(resets) is not None)):
                continue
            clean[key] = {"utilization": w["utilization"], "resets_at": resets}
        out["usage"] = clean
    return out


def load_usage_cache():
    """The usage cache as a dict of well-shaped values; {} when missing, corrupt or the wrong type."""
    try:
        with open(USAGE_CACHE, encoding="utf-8") as f:
            return _clean_usage_cache(json.load(f))
    except Exception:
        return {}


def refresh_cache():
    """Fetch whatever is stale into the cache. Runs in a detached child process."""
    import urllib.request  # here, not at the top: ~30ms the render itself never needs
    cache = load_usage_cache()
    now = time.time()
    if now - (cache.get("fetched_at") or 0) >= USAGE_TTL:
        _fetch_usage(cache, now)
    if now - (cache.get("tags_at") or 0) >= RELEASE_TTL:
        cache["tags_at"] = now
        try:
            with urllib.request.urlopen(RELEASE_URL, timeout=5) as r:
                cache["tags"] = json.loads(r.read())
        except Exception:
            pass
    tmp = f"{USAGE_CACHE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, USAGE_CACHE)


def _fetch_usage(cache, now):
    import urllib.request
    cache["fetched_at"] = now
    try:
        token = read_oauth_token()
        if not token:
            raise RuntimeError("no oauth token")
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        cache["usage"] = {k: body.get(k) for k in WINDOWS}
        cache["usage_at"] = cache["fetched_at"]
        cache["error"] = None
    except Exception as e:
        cache["error"] = f"{type(e).__name__}: {e}"[:200]


LOCK_STALE = 30  # seconds after which a usage lock counts as left behind by a killed refresh


def _usage_gate():
    """Nonblocking OS lock serializing usage-lock mutations, released even on a crash.

    The guard file stays on disk: deleting it would let different processes lock
    different files with the same path. Only its first byte is locked on Windows.
    """
    gate = None
    try:
        gate = open(f"{USAGE_LOCK}.guard", "a+b")
        if os.name == "nt":
            import msvcrt
            if os.fstat(gate.fileno()).st_size == 0:
                gate.write(b"\0")
                gate.flush()
            gate.seek(0)
            msvcrt.locking(gate.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(gate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return gate
    except OSError:
        if gate is not None:
            gate.close()
        return None


def _take_lock(now, token=""):
    """Acquire the usage lock without stealing another session's fresh replacement."""
    gate = _usage_gate()
    if gate is None:
        return False
    try:
        return _take_lock_held(now, token)
    finally:
        gate.close()


def _take_lock_held(now, token):
    """Create the usage lock holding `token` (who owns it); False when a fresh one exists.

    The OS gate stays held across the stale check and replacement, so another
    session cannot replace or release the lock between those operations.
    """
    try:
        return _make_lock(token)
    except FileExistsError:
        pass
    except OSError:
        return False
    try:
        if now - os.stat(USAGE_LOCK).st_mtime < LOCK_STALE:
            return False  # a refresh is already running
        aside = f"{USAGE_LOCK}.{os.getpid()}.stale"
        os.replace(USAGE_LOCK, aside)  # FileNotFoundError: another session took it over first
        os.unlink(aside)
        return _make_lock(token)  # FileExistsError: someone else won the race after all
    except OSError:
        return False


def _make_lock(token):
    fd = os.open(USAGE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _release_lock(token):
    """Remove the usage lock, but only if it still holds our token (it may be someone else's by now)."""
    gate = _usage_gate()
    if gate is None:
        return
    try:
        with open(USAGE_LOCK, encoding="utf-8") as f:
            mine = token and f.read() == token
        if mine:
            os.unlink(USAGE_LOCK)
    except OSError:
        pass
    finally:
        gate.close()


REFRESH_DEADLINE = 15  # seconds the detached refresh may live, however slow the network is


def _expire(token):
    """The deadline passed: give up the lock (if ours) and leave at once."""
    _release_lock(token)
    os._exit(1)


def _arm_deadline(token):
    import threading
    timer = threading.Timer(REFRESH_DEADLINE, _expire, (token,))
    timer.daemon = True
    timer.start()


def maybe_refresh(cache, now):
    """Start a background fetch when anything in the cache is stale. Never blocks."""
    stale = (now - (cache.get("fetched_at") or 0) >= USAGE_TTL
             or now - (cache.get("tags_at") or 0) >= RELEASE_TTL)
    token = f"{os.getpid()}-{time.time_ns()}"
    if not stale or not _take_lock(now, token):
        return
    import subprocess
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--refresh", "--lock-token", token], **kw)
    except Exception:
        _release_lock(token)


# --- health: version, plugins, MCP ------------------------------------------

def _ver(v):
    try:
        return tuple(int(x) for x in str(v).split("-")[0].split("."))
    except ValueError:
        return ()


def version_status(running, installed, published):
    """('ok'|'restart'|'update'|'unknown', newer version or None)."""
    newest_installed = max(installed, key=_ver, default=None)
    if newest_installed and _ver(newest_installed) > _ver(running):
        return "restart", newest_installed
    if published and _ver(published) > _ver(running):
        return "update", published
    if published or installed:
        return "ok", None
    return "unknown", None


def installed_versions():
    try:
        return [name for name in os.listdir(NATIVE_VERSIONS) if _ver(name)]
    except OSError:
        return []


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def plugins_needing_reload(installed, enabled, loaded_at):
    """Enabled plugins updated on disk after this session loaded its plugins."""
    out = []
    for name, entries in (installed.get("plugins") or {}).items():
        if name not in enabled:
            continue
        updated = max((_epoch(e.get("lastUpdated")) or 0 for e in entries), default=0)
        if updated > loaded_at:
            out.append(name)
    return sorted(out)


def plugins_loaded_at(transcript_path, state=None):
    """When this session last loaded plugins: its first entry, or the last /reload-plugins.

    state: a dict the caller keeps between calls (it is updated in place). With it, a
    transcript that only grew since the last call is scanned from the end of the last
    scan, not from the start; a shorter file or another path is scanned in full.
    """
    state = {} if state is None else state
    path = str(transcript_path)
    try:
        size = os.stat(transcript_path).st_size
    except (OSError, TypeError, ValueError):
        return None
    start = reload_t = 0
    first_t = None
    if (state.get("path") == path and isinstance(state.get("size"), int) and 0 <= state["size"] <= size
            and _number(state.get("first"))):
        start, first_t, reload_t = state["size"], state["first"], state.get("reload")
    try:
        with open(transcript_path, "rb") as f:
            f.seek(start)
            raw = f.read()
    except OSError:
        return None

    def ts_of(line):
        try:
            return _epoch(json.loads(line).get("timestamp"))
        except ValueError:
            return None

    i = raw.rfind(b"<command-name>/reload-plugins</command-name>")
    if i >= 0:
        s = raw.rfind(b"\n", 0, i) + 1
        end = raw.find(b"\n", i)
        t = ts_of(raw[s:end if end >= 0 else None])
        if t:
            reload_t = t
    if first_t is None:
        first_t = _first_timestamp(raw)
    # Resume at a line boundary, so a line still being written is read again, whole.
    state.update(path=path, size=start + raw.rfind(b"\n") + 1, first=first_t, reload=reload_t or None)
    return reload_t or first_t


def _first_timestamp(raw):
    for line in raw.split(b"\n", 50)[:50]:
        if not line.strip():
            continue
        try:
            t = _epoch(json.loads(line).get("timestamp"))
        except ValueError:
            continue
        if t:
            return t
    return None


def mcp_status(logs, session_id, known_plugins=()):
    """(connected, failed) server names from this session's MCP log events.

    logs: {log dir suffix: [event dicts]}. Only connection events count, so a
    tool call that errors on a connected server is not a server problem.
    """
    ok, failed = [], []
    for server, events in logs.items():
        state = None
        for e in sorted((e for e in events if e.get("sessionId") == session_id),
                        key=lambda e: e.get("timestamp", "")):
            connected = _connection_state(e)
            if connected is not None:
                state = connected
        if state is None:
            continue
        (ok if state else failed).append(_mcp_name(server, known_plugins))
    return sorted(ok), sorted(failed)


def _mcp_name(server, known_plugins=()):
    """Log dir suffix to display name: plugin-discord-discord -> discord, claude-ai-Gmail -> Gmail.

    Plugin dirs are plugin-<plugin>-<server>, and both halves may contain hyphens,
    so split where the plugin half is a known plugin or the halves repeat.
    """
    if server.startswith("claude-ai-"):
        return server[10:]
    if not server.startswith("plugin-"):
        return server
    words = server[7:].split("-")
    for i in range(1, len(words)):
        plugin, name = "-".join(words[:i]), "-".join(words[i:])
        if plugin == name or plugin in known_plugins:
            return name
    return server[7:]


def mcp_log_root(project_dir):
    """Claude Code's per-project cache dir, where it writes mcp-logs-<server>/."""
    if not project_dir:
        return None
    # Claude Code's own rule: ASCII letters and digits stay, every other character becomes '-'.
    key = "".join(c if (c.isascii() and c.isalnum()) else "-" for c in str(project_dir))
    if os.name == "nt":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local"),
                            "claude-cli-nodejs", "Cache")
    elif sys.platform == "darwin":
        base = os.path.join(HOME, "Library", "Caches", "claude-cli-nodejs")
    else:
        base = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), "claude-cli-nodejs")
    return os.path.join(base, key)


def read_mcp_logs(root, max_files=30):
    """{server: [connection events]} from the newest `max_files` log files per server, or None without logs.

    Every session in a project shares these folders, so the newest files are often another
    session's: the scan is shared between sessions through the probe cache and mcp_status
    picks a session's own events. Only connection events are kept (they are all it reads),
    so the cached scan stays small.
    """
    if not root:
        return None  # (os.listdir(None) would list the current directory)
    try:
        dirs = [d for d in os.listdir(root) if d.startswith("mcp-logs-")]
    except OSError:
        return None
    logs = {}
    for d in dirs:
        events = []
        folder = os.path.join(root, d)
        try:
            files = sorted((f for f in os.listdir(folder) if f.endswith(".jsonl")), reverse=True)
        except OSError:
            files = []
        for name in files[:max_files]:
            try:
                with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            for line in text.splitlines():
                if "Successfully connected" not in line and "Connection failed" not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and _connection_state(e) is not None:
                    events.append({k: e.get(k) for k in ("sessionId", "timestamp", "debug", "error") if k in e})
        logs[d[9:]] = events
    return logs


def _connection_state(e):
    """True for a 'connected' event, False for a 'connection failed' one, else None."""
    if "Successfully connected" in str(e.get("debug", "")):
        return True
    if str(e.get("error", "")).startswith("Connection failed"):
        return False
    return None


def count_skills(installed, enabled, claude_dir, project_dir=None):
    """Skills available to a session: enabled plugins' skills and commands, yours, the project's.

    A plugin whose plugin.json lists "skills" loads exactly those; otherwise
    each skills/<name>/SKILL.md. This matches what /reload-plugins reports,
    plus the user and project skill folders it leaves out.
    """
    n = 0
    for name, entries in (installed.get("plugins") or {}).items():
        if name not in enabled or not entries:
            continue
        root = entries[-1].get("installPath")
        if not root:
            continue  # nothing on disk to count (and never the current directory)
        listed = _read_json(os.path.join(root, ".claude-plugin", "plugin.json")).get("skills")
        if isinstance(listed, list):
            n += sum(1 for rel in listed if os.path.isfile(os.path.join(root, str(rel), "SKILL.md")))
        else:
            n += _count_skill_files(os.path.join(root, "skills"))
        n += _count_files(os.path.join(root, "commands"), ".md")
    n += _count_skill_files(os.path.join(claude_dir, "skills"))
    if project_dir:
        n += _count_skill_files(os.path.join(project_dir, ".claude", "skills"))
    return n


def _count_skill_files(folder):
    """How many <folder>/*/SKILL.md exist."""
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    return sum(1 for name in names if os.path.isfile(os.path.join(folder, name, "SKILL.md")))


def _count_files(folder, suffix):
    try:
        return sum(1 for name in os.listdir(folder) if name.endswith(suffix)
                   and os.path.isfile(os.path.join(folder, name)))
    except OSError:
        return 0


# Health mark levels, worst last. A problem's level picks the bare marker's glyph and colour.
OK, RELOAD, UPDATE, FAILED = 0, 1, 2, 3
MARKER = {FAILED: ("RED", "warn"), UPDATE: ("AMBER", "up"), RELOAD: ("DIM", "reload")}


def health_marks(running, version, reload_plugins, mcp):
    """Health as [(text, level, word)]: one '✓ mcp 3 · 2.1.296' cluster, or one item per problem.

    All well: a green tick, then in grey what it vouches for (connected MCP servers, the
    running version). Otherwise each problem is its own item, by name: '▲ discord' (red,
    '+N' for more), '↑ 2.2.0' (amber: the version available) or '↑ restart' (installed, needs
    a restart), '~reload' (grey: enabled plugins changed on disk, advice rather than a fault).
    `word` is the one-word reason the bare marker carries when it has room. Nothing known
    (no logs, no version, no plugin list) means no marks.
    """
    ok, failed = mcp if mcp is not None else ([], [])
    state, newer = version
    marks = []
    if failed:
        more = f" +{len(failed) - 1}" if len(failed) > 1 else ""
        marks.append((f"{RED}{ICON['warn']} {mid_trunc(failed[0], 12)}{more}{RST}", FAILED, failed[0]))
    if running and state == "restart":  # a newer build is installed; this window still runs the old one
        marks.append((f"{AMBER}{ICON['up']} restart{RST}", UPDATE, "restart"))
    elif running and state == "update":
        marks.append((f"{AMBER}{ICON['up']} {newer or 'update'}{RST}", UPDATE, newer or "update"))
    if reload_plugins:
        gap = " " if ICON["reload"] != "~" else ""
        marks.append((f"{DIM}{ICON['reload']}{gap}reload{RST}", RELOAD, "reload"))
    if marks:
        return marks
    parts = ([f"mcp {len(ok)}"] if ok else []) + ([str(running)] if running else [])
    if not parts:
        return []
    verified = bool(ok) or state == "ok" or reload_plugins == []
    tick = f"{GREEN}{ICON['ok']}{RST} " if verified else ""
    return [(f"{tick}{DIM}{' · '.join(parts)}{RST}", OK, "")]


def health_state(data, cache=None):
    """(running, version, reload_plugins, mcp, skills) for health_marks and the skills item.

    cache: the render's ProbeCache; the probes are kept HEALTH_TTL seconds per session
    and project (SKILLS_TTL for the skill count, per project).
    """
    cache = _probe_cache(cache)
    running = data.get("version")
    ws = data.get("workspace") or {}
    key = json.dumps([data.get("session_id"), ws.get("project_dir") or data.get("cwd"),
                      data.get("transcript_path"), running])
    version, reload_plugins, mcp = cache.get("health:" + key, HEALTH_TTL, lambda: _health_probe(data, cache))

    def skill_count():
        settings, installed = _settings_and_plugins()
        return count_skills(installed, _enabled(settings), CLAUDE_DIR, ws.get("project_dir"))
    skills = cache.get("skills:" + json.dumps(ws.get("project_dir")), SKILLS_TTL, skill_count)
    return running, version, reload_plugins, mcp, skills


def _settings_and_plugins():
    return (_read_json(os.path.join(CLAUDE_DIR, "settings.json")),
            _read_json(os.path.join(CLAUDE_DIR, "plugins", "installed_plugins.json")))


def _enabled(settings):
    return {k for k, v in (settings.get("enabledPlugins") or {}).items() if v}


def _health_probe(data, cache=None):
    """[version, reload_plugins, mcp]: the slow part of health_state (it reads the transcript and MCP logs).

    cache: the render's ProbeCache, which also keeps how far the transcript was scanned.
    """
    cache = _probe_cache(cache)
    settings, installed = _settings_and_plugins()
    running = data.get("version")
    channel = settings.get("autoUpdatesChannel") or "latest"
    published = (load_usage_cache().get("tags") or {}).get(channel)
    version = version_status(running, installed_versions(), published) if running else ("unknown", None)

    reload_plugins = None
    scan_key = "loaded:" + str(data.get("transcript_path"))
    scan = cache.peek(scan_key)
    loaded_at = plugins_loaded_at(data.get("transcript_path"), scan)
    cache.put(scan_key, scan)
    if loaded_at and installed:
        reload_plugins = plugins_needing_reload(installed, _enabled(settings), loaded_at)

    ws = data.get("workspace") or {}
    # One scan per project, shared by its sessions (they all read the same folders).
    root = mcp_log_root(ws.get("project_dir") or data.get("cwd"))
    logs = cache.get("mcplogs:" + json.dumps(root), HEALTH_TTL, lambda: read_mcp_logs(root))
    known = {k.split("@")[0] for k in (installed.get("plugins") or {})}
    mcp = mcp_status(logs, data.get("session_id"), known) if logs else None
    return [version, reload_plugins, mcp]


# --- workspace probes --------------------------------------------------------

def git_info(cwd, cache=None):
    """(branch, flags) for where_item; None outside a repo, '' without a directory.

    cache: the render's ProbeCache; git status is kept GIT_TTL seconds per directory.
    """
    if not cwd or not os.path.exists(cwd):
        return ""
    status = _probe_cache(cache).get("git:" + str(cwd), GIT_TTL, lambda: git_status(cwd))
    if not status:
        return None
    branch, stg, mod, unt = status
    flags = []
    if stg:
        flags.append(f"{GREEN}+{stg}{RST}")
    if mod:
        flags.append(f"{AMBER}~{mod}{RST}")
    if unt:
        flags.append(f"{DIM}?{unt}{RST}")
    return branch, flags


def git_status(cwd):
    """[branch, staged, modified, untracked] from one git call, or None outside a repo."""
    # One git call instead of five: branch plus staged/modified/untracked counts.
    out = run(["git", "-C", cwd, "status", "--porcelain=v2", "--branch", "--untracked-files=all"])
    branch = oid = ""
    stg = mod = unt = 0
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            branch = line[14:]
        elif line.startswith("# branch.oid "):
            oid = line[13:20]
        elif line[:2] in ("1 ", "2 "):
            stg += line[2] != "."
            mod += line[3] != "."
        elif line.startswith("u "):
            mod += 1
        elif line.startswith("? "):
            unt += 1
    if branch == "(detached)":
        branch = oid
    if not branch:
        return None
    return [branch, stg, mod, unt]


MIN_NAME = 14      # a shortened project name keeps at least this many characters
SHORT_BRANCH = 11  # a tight line shows 'live-usage…': this many characters, then the ellipsis
BRANCH_MIN = 8     # the shortest cut branch, ellipsis included: 'live-us…'


def _where_text(proj, branch, flags):
    parts = []
    if proj:
        parts.append(f"{PROJECT}{proj}{RST}")
    if branch:
        color = AMBER if flags else GREEN
        parts.append(" ".join([f"{color}{branch}{RST}"] + list(flags)))
    return f" {DIM}›{RST} ".join(parts)


def shrink_where(proj, branch, flags, width):
    """(project, branch) made to fit `width` as 'project › branch flags'.

    The project name is what tells windows apart, so it stays whole as long as it
    can: the branch gives way first, cut from its end ('live-us…', never below
    BRANCH_MIN), then dropped with its counts; only then the project loses
    characters from its middle ('claude-co…statusline'), never below MIN_NAME.
    width None: unchanged."""
    if not width:
        return proj, branch

    def fits(p, b):
        return visible_width(_where_text(p, b, flags if b else [])) <= width

    b = branch
    n = len(branch)
    while n > BRANCH_MIN and not fits(proj, b):
        n -= 1
        b = branch[:n - 1] + "…"
    if fits(proj, b):
        return proj, b
    p = proj
    m = len(proj)
    while m > MIN_NAME and not fits(p, ""):
        m -= 1
        p = mid_trunc(proj, m)
    return p, ""


def where_item(proj, git, width=None):
    """'project › branch flags', shrunk to `width` by shrink_where. git: git_info()'s result."""
    branch, flags = git or ("", [])
    return _where_text(*shrink_where(proj, branch, flags, width), flags)


def short_branch(branch):
    """'live-usage-and-redesign' -> 'live-usage…'; short names stay."""
    return branch if len(branch) <= SHORT_BRANCH + 1 else branch[:SHORT_BRANCH] + "…"


# --- session name --------------------------------------------------------------

NAME_MAX = 28           # longest name shown (a hyphenated slug)
TAIL_BYTES = 262_144    # how much of the transcript's end is searched for a title
TITLE_KEYS = (("ai-title", "aiTitle"), ("agent-name", "agentName"), ("custom-title", "customTitle"))


def slugify(text, cap=NAME_MAX):
    """'Statusline.py: usage accuracy' -> 'statusline-py-usage-accuracy', cut at a word boundary
    within `cap` characters (a single longer word is cut hard)."""
    slug = re.sub(r"[\W_]+", "-", str(text).lower()).strip("-")
    if len(slug) <= cap:
        return slug
    if slug[cap] == "-":
        return slug[:cap]
    head = slug[:cap]
    cut = head.rfind("-")
    return head[:cut] if cut > 0 else head


def tail_title(path, max_bytes=TAIL_BYTES):
    """The newest session title in the last `max_bytes` of a transcript, '' when there is none.

    A title is an {"type": "ai-title", "aiTitle": ...} entry (or agent-name / custom-title).
    Transcripts can be 100 MB, so only the end is read; the first, maybe cut, line and a
    half-written last line are skipped because they do not parse.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        raw = f.read(max_bytes)
    lines = raw.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]
    markers = [f'"{kind}"'.encode() for kind, _ in TITLE_KEYS]
    for line in reversed(lines):
        if not any(m in line for m in markers):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        for kind, key in TITLE_KEYS:
            title = entry.get(key) if entry.get("type") == kind else None
            if isinstance(title, str) and title.strip():
                return title.strip()
    return ""


def session_name(data, cache=None):
    """The session's name as a short slug, or '' when it has none.

    The payload's session_name (set with /rename) wins; otherwise the newest title in the
    transcript's tail. The transcript is only searched again when its size changed: the
    result is kept in the probe cache under (path, size), and a grown transcript with no
    title in its tail keeps the name found before.
    """
    given = data.get("session_name")
    if isinstance(given, str) and given.strip():
        return slugify(given)
    path = data.get("transcript_path")
    if not path:
        return ""
    try:
        size = os.stat(path).st_size
    except (OSError, ValueError, TypeError):
        return ""
    cache = _probe_cache(cache)
    key = "name:" + str(path)
    saved = cache.peek(key)
    old = saved.get("name") if isinstance(saved.get("name"), str) else ""
    if saved.get("size") == size and "name" in saved:
        if (cache.age(key) or 0) > NAME_REFRESH:
            cache.put(key, saved)  # keep the entry from expiring while the transcript sits still
        return old
    try:
        title = tail_title(path)
    except OSError:
        title = ""
    name = slugify(title) if title else old
    cache.put(key, {"size": size, "name": name})
    return name


def line_width():
    """Columns available to the status line (COLUMNS minus the edge, at least 20), or None when unknown."""
    try:
        columns = int(os.environ.get("COLUMNS") or 0)
    except ValueError:
        return None
    return max(columns - EDGE, 20) if columns > 0 else None


def venv_tag():
    conda = os.environ.get("CONDA_DEFAULT_ENV")
    if conda and conda != "base":
        return f"{ic('venv', CYAN)}{DIM}env:{RST}{CYAN}{conda}{RST}"
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        return f"{ic('venv', CYAN)}{DIM}venv:{RST}{CYAN}{_basename(venv)}{RST}"
    return ""


def _basename(path):
    """The last component of a path, ignoring a trailing separator ('/x/.venv/' -> '.venv')."""
    return os.path.basename(os.path.normpath(path))


def open_ports(ports=None, timeout=0.02):
    """Ports on 127.0.0.1 that accept a connection within `timeout` seconds.

    All at once, with non-blocking connects and select(): on Windows a closed port
    never refuses quickly, so each one waits out the whole timeout. No threads.
    """
    import select
    import socket
    ports = COMMON_PORTS if ports is None else ports
    pending, up, socks = {}, set(), []
    try:
        for port in ports:
            s = socket.socket()
            pending[s] = port
            s.setblocking(False)
            err = s.connect_ex(("127.0.0.1", port))
            if err == 0:
                up.add(port)
            elif err in (10035, 115, 36, 11, 35):  # WSAEWOULDBLOCK, EINPROGRESS, EAGAIN (Linux, macOS)
                socks.append(s)  # still connecting; anything else was refused at once
        deadline = time.monotonic() + timeout
        while socks:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            _, writable, failed = select.select([], socks, socks, left)
            if not writable and not failed:
                break
            for s in set(writable) | set(failed):
                if s not in failed and s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    up.add(pending[s])
                socks.remove(s)
    except OSError:
        pass
    finally:
        for s in pending:
            s.close()
    return [p for p in ports if p in up]


def ports_tag(cache=None):
    """':3000,5173' for the dev ports that answer. cache: kept PORTS_TTL seconds."""
    up = _probe_cache(cache).get("ports:" + ",".join(map(str, COMMON_PORTS)), PORTS_TTL, open_ports)
    return f"{ic('ports', GREEN)}{GREEN}:{','.join(str(p) for p in up)}{RST}" if up else ""


# --- layout ------------------------------------------------------------------

MODEL_MAX = 20   # a longer display name loses its middle: 'Claude Opus…view Build'
MODEL_SHORT = 8  # on a tiny window the name is cut harder, after everything else has gone


def model_head(data, name_max=MODEL_MAX, effort=True):
    """'Opus 5.5 · high' in the model family's color and the effort's color."""
    model = data.get("model", {}) or {}
    name = str(model.get("display_name") or "Claude")
    mc = model_color(model.get("id") or name)
    head = f"{mc}{mid_trunc(name, name_max)}{RST}"
    level = (data.get("effort") or {}).get("level")
    if level and effort:
        head += f" {DIM}·{RST} {EFFORT.get(level, '')}{level}{RST}"
    return head


def session_time(data):
    """Session time: clock icon and number, or 'session 12m' without icons; '' before any time."""
    dur = fmt_duration(int((data.get("cost", {}) or {}).get("total_duration_ms", 0) or 0))
    if not dur:
        return ""
    return f"{ic('duration')}{DIM}{dur}{RST}" if ICON.get("duration") else f"{DIM}session {dur}{RST}"


def name_tag(name):
    """The session name as a tag: quoted in plain mode, after a tag icon with Nerd icons."""
    if ICON.get("tag"):
        return f"{ic('tag', CYAN)}{CYAN}{name}{RST}"
    return f"{CYAN}“{name}”{RST}"


def gather_extras(data, cache=None):
    """The probed items for format_statusline: project, git, session name, health, ports, venv.

    Everything behind the probe cache (git 3 s, ports 10 s, health 10 s, skills 60 s, the
    name until the transcript grows). A probe that fails only leaves its item out.
    """
    ws = data.get("workspace", {}) or {}
    cwd = ws.get("current_dir") or ws.get("project_dir") or data.get("cwd") or os.getcwd()
    extras = {"proj": _basename(cwd) if cwd else "", "git": None, "name": "", "marks": [],
              "skills": None, "ports": "", "venv": ""}  # marks: health_marks()'s list
    for key, probe in (("git", lambda: git_info(cwd, cache)),
                       ("name", lambda: session_name(data, cache)),
                       ("venv", venv_tag),
                       ("ports", lambda: ports_tag(cache))):
        try:
            extras[key] = probe()
        except Exception:
            pass
    if SHOW_HEALTH:
        try:
            running, version, reload_plugins, mcp, skills = health_state(data, cache)
            extras["marks"] = health_marks(running, version, reload_plugins, mcp)
            extras["skills"] = skills
        except Exception:
            pass
    return extras


MARKER_REASON_ROOM = 8  # free columns the bare marker needs before it adds its one-word reason


def format_statusline(data, usage=None, now=None, show_usage=True, extras=None, width=None):
    """One status line, fitted to `width` down to the core meters (None: no limit).

    usage: merge_usage() result, or None when nothing is known yet. extras: gather_extras()'s
    result; without it only the session's own items show. Left to right:

        project › branch ~N “session-name” │ model · effort │ ctx % bar tokens │
        5h % bar →reset │ 7d % bar →reset │ $cost │ session time │ health │
        skills │ venv │ :ports

    A window's →reset countdown shows only from RESET_FROM percent and belongs to its meter:
    it is never dropped on its own. The line is made for a 120-column window. When it does
    not fit `width`, things go in this order, each only if the line is still too wide:

      1. venv and ports, 2. skills, 3. session time,
      4. the healthy health cluster ('✓ mcp 3 · 2.1.296'),
      5. health problems, last one first, each by name ('▲ discord', '↑ 2.2.0', '~reload'); when
         the last text goes, one bare marker stands in for all of them, with the glyph and colour
         of the worst (▲ red, then ↑ amber, then ~ grey) and, given MARKER_REASON_ROOM free
         columns, its one-word reason ('▲discord'); it outlasts everything below except model
         and percentages,
      6. the session name,
      7. the branch: cut from its end into the room left while the project stays whole,
         then dropped with its counts (it stays amber while present),
      8. the project: squeezed down to 14 characters as the room shrinks, then dropped,
      9. the ctx tokens, 10. the bars (7d, then 5h, then ctx), 11. the cost, 12. the effort,
     13. the model name is cut to 8 characters, 14. the bare marker.

    Model and the three percentages always stay, with their countdowns: at 100% they need
    47 columns ('Opus 5.5 │ ctx 100% │ 5h 100% →1h │ 7d 100% →3d'), the floor of the line.
    """
    now = time.time() if now is None else now
    cw = data.get("context_window", {}) or {}
    ex = extras or {}

    # (label, percentage, icon, suffix, suffix is droppable): the ctx tokens go, countdowns stay.
    meters = [("ctx", context_pct(cw), "ctx", context_tokens(cw), True)]
    if show_usage:
        usage = usage or {}
        for label, key in (("5h", "five_hour"), ("7d", "seven_day")):
            w = usage.get(key)
            left = fmt_reset(w["resets_at"] - now) if w and w["resets_at"] and w["pct"] >= RESET_FROM else ""
            meters.append((label, w["pct"] if w else None, key, left, False))
    cost = fmt_cost(float((data.get("cost", {}) or {}).get("total_cost_usd", 0.0) or 0.0))
    session = session_time(data)
    tag = name_tag(ex["name"]) if ex.get("name") else ""
    skills = f"{DIM}skills{RST} {ex['skills']}" if ex.get("skills") else ""
    marks = list(ex.get("marks") or [])
    worst = max(marks, key=lambda m: m[1], default=None)
    marker = None
    if worst and worst[1] > OK:
        color_name, glyph = MARKER[worst[1]]
        marker = (globals()[color_name], ICON[glyph], worst[2])

    proj = ex.get("proj") or ""
    branch, flags = ex.get("git") or ("", [])

    def where(s, rest):
        """The project › branch item. Once the branch is being given up ('fit', 'short' or
        gone) the item is squeezed into what `rest` (the other items, as they would be
        joined) leaves of the width; the project never goes below MIN_NAME characters."""
        p, b, f = proj, branch, flags
        if s["branch"] is None:
            b, f = "", []
        elif s["branch"] in ("short", "bare"):
            b = short_branch(b)
            f = [] if s["branch"] == "bare" else f
        if s["branch"] != "full" and width:
            taken = visible_width(SEP.join(rest)) + visible_width(SEP)
            if s["name"] and tag:
                taken += visible_width(tag) + 1
            p, b = shrink_where(p, b, f, width - taken)
        if s["proj"] is None:
            p = ""
        return _where_text(p, b, f)

    def items(s):
        out = [model_head(data, MODEL_SHORT if s["model"] == "short" else MODEL_MAX, s["effort"])]
        for i, (label, p, icon, suffix, droppable) in enumerate(meters):
            show = s["tokens"] or not droppable
            out.append(meter(label, p, icon, with_bar=i < s["bars"], suffix=suffix if show else ""))
        if s["cost"]:
            out.append(cost)
        if s["session"] and session:
            out.append(session)
        out += [text for i, (text, _, _) in enumerate(marks) if i not in s["dropped"]]
        if s["marker"] and marker:
            color, glyph, _ = marker
            out.append(f"{color}{glyph}{RST}")
        if s["skills"] and skills:
            out.append(skills)
        if s["extras"]:
            out += [x for x in (ex.get("venv"), ex.get("ports")) if x]
        first = " ".join(x for x in (where(s, out), tag if s["name"] else "") if x)
        line = [first] + out if first else out
        if s["marker"] and marker and marker[2]:
            # The bare marker says why when there is room: '▲discord'.
            color, glyph, word = marker
            # Nerd glyphs draw past their cell: a space keeps '󰀦 discord' from overlapping.
            gap = " " if ICON.get("segment") else ""
            free = MARKER_REASON_ROOM + len(word) if width is None else width - visible_width(SEP.join(line))
            if free >= MARKER_REASON_ROOM:
                line[line.index(f"{color}{glyph}{RST}")] = (
                    f"{color}{glyph}{gap}{mid_trunc(word, free - len(gap))}{RST}")
        return line

    # The drop order, as patches applied one after the other to the state.
    steps = [{"extras": False}, {"skills": False}, {"session": False}]
    healthy = [i for i, (_, level, _) in enumerate(marks) if level == OK]
    needy = [i for i, (_, level, _) in enumerate(marks) if level > OK]
    steps += [{"dropped": i} for i in reversed(healthy)]
    steps += [{"dropped": i, "marker": k == len(needy) - 1} for k, i in enumerate(reversed(needy))]
    steps += [{"name": False}, {"branch": "fit"}, {"branch": "short"}, {"branch": "bare"}, {"branch": None},
              {"proj": None},
              {"tokens": False}, {"bars": 2}, {"bars": 1}, {"bars": 0}, {"cost": False},
              {"effort": False}, {"model": "short"}, {"marker": False}]

    state = {"extras": True, "skills": True, "session": True, "dropped": set(),
             "marker": False, "name": True, "branch": "full", "proj": "full", "tokens": True,
             "bars": len(meters), "cost": True, "effort": True, "model": "full"}
    best = items(state)
    for step in [None] + steps:
        if step:
            step = dict(step)
            if "dropped" in step:
                state["dropped"] = state["dropped"] | {step.pop("dropped")}
            state.update(step)
            best = items(state)
        if width is None or visible_width(SEP.join(best)) <= width:
            break
    return SEP.join(best)


# --- demo --------------------------------------------------------------------

def demo_lines(now=None, width=None):
    """Two realistic sample lines (a healthy session, then one with a failed MCP server and an
    update) rendered at `width`, from fixed data: no probes, no caches, no network."""
    now = time.time() if now is None else now
    data = {"model": {"id": "claude-opus-4-6", "display_name": "Opus 4.6"}, "effort": {"level": "high"},
            "context_window": {"used_percentage": 43, "context_window_size": 1_000_000,
                               "current_usage": {"input_tokens": 2, "cache_creation_input_tokens": 787,
                                                 "cache_read_input_tokens": 429_211}},
            "cost": {"total_cost_usd": 103.21, "total_duration_ms": 5_580_000}}
    usage = merge_usage({"five_hour": {"used_percentage": 74, "resets_at": now + 2 * 3600 + 600},
                         "seven_day": {"used_percentage": 48, "resets_at": now + 5 * 86400}}, None, now)
    extras = {"proj": "claude-code-statusline", "git": ("live-usage-and-redesign", [f"{AMBER}~2{RST}"]),
              "name": "", "skills": 69, "ports": f"{ic('ports', GREEN)}{GREEN}:3000{RST}", "venv": "",
              "marks": health_marks("2.1.296", ("ok", None), [], (["a", "b", "c"], []))}
    healthy = format_statusline(data, usage, now, True, extras, width)
    extras.update(name="statusline-py-usage-accuracy",
                  marks=health_marks("2.1.296", ("update", "2.2.0"), [], (["a", "b"], ["discord"])))
    troubled = format_statusline(data, usage, now, True, extras, width)
    return healthy, troubled


def _opt(args, name):
    """The value after `name` in args ('--icons nerd' -> 'nerd'), or None."""
    return args[args.index(name) + 1] if name in args[:-1] else None


# ---- install / uninstall -------------------------------------------------------------------------
# `python statusline.py install`, or `claude-statusline install` when run from uvx / pipx. It copies this
# file to the Claude config dir and points statusLine at it with the fast launch. Nothing here runs on a
# normal render: main() dispatches on the first argument before any of it is used.

INSTALL_RECORD = "statusline-install.json"  # remembers the statusLine an install replaced
SAMPLE_PAYLOAD = {"model": {"display_name": "Test"}, "context_window": {"used_percentage": 12,
                                                                        "context_window_size": 1000000}}
INSTALL_USAGE = """\
Usage: claude-statusline install [--icons auto|nerd|none] [--no-font] [--dry-run] [--no-test]
       claude-statusline uninstall [--dry-run]
  install    copy the script to ~/.claude and set statusLine (backs up settings.json first); with Nerd
             icons it also installs a Nerd Font if the machine has none and sets it as the font of
             Windows Terminal and VS Code (backing those files up first)
  uninstall  restore the previous statusLine, or remove ours, and delete the copied script
  --icons    nerd needs a Nerd Font in the terminal; auto (default) uses nerd, fetching the font when needed
  --no-font  never install a font or touch terminal settings
  --claude-dir PATH  use another config dir (default: CLAUDE_CONFIG_DIR or ~/.claude)
"""


def _find_python():
    """The python command to write into settings: a name on PATH that is 3.10+, else this
    interpreter when it is not a throwaway virtualenv (uvx and pipx run in one). None if neither."""
    import shutil
    import subprocess
    names = ("python", "python3") if os.name == "nt" else ("python3", "python")
    for name in names:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "-c", "import sys;print(int(sys.version_info>=(3,10)))"],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
        except Exception:
            continue
        if out == "1":
            return name
    if sys.prefix == sys.base_prefix and sys.version_info >= (3, 10):
        return f'"{sys.executable}"' if " " in sys.executable else sys.executable
    return None


NERD_FONT_VERSION = "v3.5.1"  # the release the direct download below is pinned to
NERD_FONT_ZIP = "CascadiaMono"  # its files are CaskaydiaMonoNerdFont-<style>.ttf: the face "CaskaydiaMono Nerd Font"
NERD_FONT_URL = f"https://github.com/ryanoasis/nerd-fonts/releases/download/{NERD_FONT_VERSION}/{NERD_FONT_ZIP}.zip"
NERD_FONT_FACE = "CaskaydiaMono Nerd Font"
NERD_FONT_STYLES = ("Regular", "Bold", "Italic", "BoldItalic")


def _font_dirs():
    return [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
            os.path.join(os.environ.get("WINDIR", ""), "Fonts"),
            os.path.join(HOME, "Library", "Fonts"), "/Library/Fonts",
            os.path.join(HOME, ".local", "share", "fonts"), os.path.join(HOME, ".fonts"),
            "/usr/share/fonts", "/usr/local/share/fonts"]


def _user_font_dir():
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local"),
                            "Microsoft", "Windows", "Fonts")
    if sys.platform == "darwin":
        return os.path.join(HOME, "Library", "Fonts")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "fonts")


def _nerd_font_face():
    """The family name of an installed Nerd Font, as a terminal setting wants it, or None.

    Nerd Font files are named <Family>NerdFont-Regular.ttf (and ...NerdFontMono-..., ...NerdFontPropo-...),
    and the family it registers is '<Family> Nerd Font'. Proportional and Mono variants are skipped so the
    face is the one that keeps one cell per glyph. JetBrainsMono and CaskaydiaMono win when present."""
    found = []
    for d in _font_dirs():
        if not d or not os.path.isdir(d):
            continue
        for _root, _dirs, files in os.walk(d):
            for f in files:
                m = re.match(r"^(.+?)NerdFont-Regular\.(?:ttf|otf)$", f)
                if m:
                    found.append(f"{m.group(1)} Nerd Font")
    if not found:
        return None
    for preferred in ("JetBrainsMono Nerd Font", NERD_FONT_FACE):
        if preferred in found:
            return preferred
    return sorted(found)[0]


def _has_nerd_font():
    """True when any Nerd Font file is installed (not proof the terminal uses it)."""
    if _nerd_font_face():
        return True
    for d in _font_dirs():
        if d and os.path.isdir(d):
            for _root, _dirs, files in os.walk(d):
                if any("nerd" in f.lower() for f in files):
                    return True
    return False


def _extract_fonts(zip_path, dest_dir):
    """Copy the four CaskaydiaMono Nerd Font styles out of the release zip into dest_dir; the paths written."""
    import zipfile
    wanted = {f"CaskaydiaMonoNerdFont-{s}.ttf" for s in NERD_FONT_STYLES}
    os.makedirs(dest_dir, exist_ok=True)
    written = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = os.path.basename(info.filename)
            if name in wanted:
                target = os.path.join(dest_dir, name)
                with z.open(info) as src, open(target, "wb") as out:
                    out.write(src.read())
                written.append(target)
    return written


def _register_fonts_windows(paths):
    """Per-user font registration: registry values plus AddFontResource, so no admin rights are needed."""
    import winreg
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows NT\CurrentVersion\Fonts",
                             0, winreg.KEY_SET_VALUE)
    with key:
        for p in paths:
            style = os.path.splitext(os.path.basename(p))[0].rpartition("-")[2]  # 'BoldItalic'
            name = f"{NERD_FONT_FACE} {re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', style)} (TrueType)"  # as Windows names its own
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, p)
    try:
        import ctypes
        for p in paths:
            ctypes.windll.gdi32.AddFontResourceW(p)
    except Exception:
        pass


def _install_nerd_font():
    """Install a Nerd Font for this user. The face name on success, else None (the reason is printed).

    Windows tries winget and macOS tries Homebrew first (signed, hashed packages); anything that fails
    falls back to downloading the pinned release zip from github.com/ryanoasis/nerd-fonts."""
    import shutil
    import subprocess
    tools = []
    if os.name == "nt" and shutil.which("winget"):
        tools.append(["winget", "install", "--id", "DEVCOM.JetBrainsMonoNerdFont", "-e", "--silent",
                      "--accept-package-agreements", "--accept-source-agreements"])
    elif sys.platform == "darwin" and shutil.which("brew"):
        tools.append(["brew", "install", "--cask", "font-jetbrains-mono-nerd-font"])
    for cmd in tools:
        print(f"  installing a Nerd Font with {cmd[0]} ...")
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except Exception as e:
            print(f"  {cmd[0]} failed ({e})")
        face = _nerd_font_face()
        if face:
            return face
    print(f"  downloading {NERD_FONT_ZIP} Nerd Font {NERD_FONT_VERSION} from GitHub (about 54 MB) ...")
    import tempfile
    import urllib.request
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "font.zip")
            req = urllib.request.Request(NERD_FONT_URL, headers={"User-Agent": "claude-statusline"})
            with urllib.request.urlopen(req, timeout=60) as r, open(zpath, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            written = _extract_fonts(zpath, _user_font_dir())
        if not written:
            print("  the download held no CaskaydiaMono Nerd Font files")
            return None
        if os.name == "nt":
            _register_fonts_windows(written)
        elif sys.platform != "darwin" and shutil.which("fc-cache"):
            subprocess.run(["fc-cache", "-f", _user_font_dir()], capture_output=True, timeout=120)
        return NERD_FONT_FACE
    except Exception as e:
        print(f"  could not install a Nerd Font ({e}); install one from https://www.nerdfonts.com")
        return None


# ---- terminal font settings (JSON with comments, so edited as text) ----

def _json_skip(text, i):
    """The index after the string or comment that starts at text[i], or i when none does."""
    c = text[i]
    if c == '"':
        j = i + 1
        while j < len(text) and text[j] != '"':
            j += 2 if text[j] == "\\" else 1
        return j + 1
    if text.startswith("//", i):
        j = text.find("\n", i)
        return len(text) if j < 0 else j
    if text.startswith("/*", i):
        j = text.find("*/", i + 2)
        return len(text) if j < 0 else j + 2
    return i


def _json_close(text, open_idx):
    """The index of the brace that closes the one at open_idx (strings and comments skipped), or -1."""
    depth, i = 0, open_idx
    while i < len(text):
        j = _json_skip(text, i)
        if j != i:
            i = j
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _first_brace(text):
    i = 0
    while i < len(text):
        j = _json_skip(text, i)
        if j != i:
            i = j
        elif text[i] == "{":
            return i
        else:
            i += 1
    return -1


def _json_space(text, i):
    """Skip whitespace and JSONC comments, leaving strings untouched."""
    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text.startswith(("//", "/*"), i):
            i = _json_skip(text, i)
        else:
            break
    return i


def _json_value_end(text, i):
    """End of one JSONC value, skipping nested objects, arrays, strings and comments."""
    if text[i] == '"':
        return _json_skip(text, i)
    if text[i] not in "{[":
        while i < len(text) and not text[i].isspace() and text[i] not in ",}]":
            if text.startswith(("//", "/*"), i):
                break
            i += 1
        return i
    stack = []
    while i < len(text):
        j = _json_skip(text, i)
        if j != i:
            i = j
            continue
        if text[i] in "{[":
            stack.append("}" if text[i] == "{" else "]")
        elif text[i] in "}]":
            if not stack or text[i] != stack.pop():
                return -1
            if not stack:
                return i + 1
        i += 1
    return -1


def _json_properties(text, brace):
    """Direct object properties as key -> (value start, end); None for an invalid object.

    Only active keys at this level count. Keep the original text so comments and unrelated settings
    survive the edit without requiring a JSONC dependency.
    """
    if brace < 0 or brace >= len(text) or text[brace] != "{":
        return None
    properties, i = {}, _json_space(text, brace + 1)
    while i < len(text) and text[i] != "}":
        if text[i] != '"':
            return None
        end = _json_skip(text, i)
        try:
            key = json.loads(text[i:end])
        except ValueError:
            return None
        i = _json_space(text, end)
        if i >= len(text) or text[i] != ":":
            return None
        start = _json_space(text, i + 1)
        if start >= len(text):
            return None
        end = _json_value_end(text, start)
        if end <= start:
            return None
        properties[key] = (start, end)
        i = _json_space(text, end)
        if i < len(text) and text[i] == ",":
            i = _json_space(text, i + 1)
        elif i >= len(text) or text[i] != "}":
            return None
    return properties if i < len(text) else None


def _json_add_property(text, brace, properties, key, value):
    encoded = json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        encoded = "{ " + encoded[1:-1] + " }"
    addition = f" {json.dumps(key)}: {encoded}"
    if properties:
        addition += ","
    return text[:brace + 1] + addition + text[brace + 1:]


def _wt_font_edit(text, face):
    """(new text, outcome) for a Windows Terminal settings file. profiles.defaults gets the font face:
    'set' (added or replaced a non-Nerd face), 'kept' (already a Nerd Font), 'none' (no defaults block)."""
    brace = _first_brace(text)
    properties = _json_properties(text, brace)
    for key in ("profiles", "defaults"):
        if properties is None or key not in properties:
            return text, "none"
        brace = properties[key][0]
        properties = _json_properties(text, brace)
    if properties is None:
        return text, "none"
    if "font" not in properties:
        return _json_add_property(text, brace, properties, "font", {"face": face}), "set"
    brace = properties["font"][0]
    properties = _json_properties(text, brace)
    if properties is None:
        return text, "none"
    if "face" not in properties:
        return _json_add_property(text, brace, properties, "face", face), "set"
    start, end = properties["face"]
    try:
        old = json.loads(text[start:end])
    except ValueError:
        return text, "none"
    if isinstance(old, str) and "nerd" in old.lower():
        return text, "kept"
    return text[:start] + json.dumps(face, ensure_ascii=False) + text[end:], "set"


def _vscode_font_edit(text, face):
    """(new text, outcome) for VS Code user settings: terminal.integrated.fontFamily gets the Nerd Font first."""
    brace = _first_brace(text)
    properties = _json_properties(text, brace)
    if properties is None:
        return text, "none"
    key = "terminal.integrated.fontFamily"
    if key not in properties:
        return _json_add_property(text, brace, properties, key, face), "set"
    start, end = properties[key]
    try:
        old = json.loads(text[start:end])
    except ValueError:
        return text, "none"
    if isinstance(old, str) and "nerd" in old.lower():
        return text, "kept"
    new = f"{face}, {old}" if isinstance(old, str) and old else face
    return text[:start] + json.dumps(new, ensure_ascii=False) + text[end:], "set"


def _terminal_settings_files():
    """[(label, path, edit function)] for the terminal config files that exist on this machine."""
    app = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
    files = []
    if os.name == "nt":
        for sub in (os.path.join("Packages", "Microsoft.WindowsTerminal_8wekyb3d8bbwe", "LocalState"),
                    os.path.join("Packages", "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe", "LocalState"),
                    os.path.join("Microsoft", "Windows Terminal")):
            files.append(("Windows Terminal", os.path.join(local, sub, "settings.json"), _wt_font_edit))
    if os.name == "nt":
        code = app
    elif sys.platform == "darwin":
        code = os.path.join(HOME, "Library", "Application Support")
    else:
        code = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    for product in ("Code", "Code - Insiders"):
        files.append(("VS Code", os.path.join(code, product, "User", "settings.json"), _vscode_font_edit))
    return [(label, path, fn) for label, path, fn in files if os.path.isfile(path)]


def _configure_terminals(face, save_copy, dry=False):
    """Point every terminal config found at `face`. Returns the lines to print. save_copy(path, label)
    makes a backup first; a file that already names a Nerd Font is left alone."""
    lines, touched = [], False
    for label, path, edit in _terminal_settings_files():
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
            new, outcome = edit(text, face)
        except OSError as e:
            lines.append(f"  {label}: cannot read {path} ({e})")
            continue
        if outcome == "kept":
            lines.append(f"  {label}: already uses a Nerd Font")
        elif outcome == "none":
            lines.append(f"  {label}: set the font to \"{face}\" by hand in {path}")
        elif dry:
            lines.append(f"  {label}: would set the font to \"{face}\" in {path}")
        else:
            save_copy(path, label.replace(" ", "-").lower())
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            os.replace(tmp, path)
            lines.append(f"  {label}: font set to \"{face}\" ({path})")
            touched = True
    if not lines:
        lines.append(f"  no Windows Terminal or VS Code settings found; select \"{face}\" as the font in your"
                     " terminal (iTerm2: Profiles > Text, Terminal.app: Profiles > Font, GNOME Terminal: Profile > Font)")
    if touched and os.name == "nt":
        lines.append("  Windows Terminal reads installed fonts only when it starts: close ALL its windows and open a"
                     " new one before the icons draw")
    return lines


def _launch_command(python, claude_dir, icons):
    """The statusLine command: python -S importing the copied module, so its compiled .pyc is reused."""
    default = os.path.abspath(os.path.join(HOME, ".claude"))
    if os.path.abspath(claude_dir) == default:
        setup = "sys.path.insert(0,os.path.expanduser('~/.claude'))"
    else:
        # Hex keeps paths out of both Python string syntax and the command shell's syntax.
        encoded = os.fsencode(os.path.abspath(claude_dir)).hex()
        setup = (f"p=os.fsdecode(bytes.fromhex('{encoded}'));"
                 "os.environ['CLAUDE_CONFIG_DIR']=p;sys.path.insert(0,p)")
    code = f"import os,sys;{setup};import statusline;statusline.main()"
    return f'{python} -S -c "{code}"' + (" --icons nerd" if icons == "nerd" else "")


def _read_settings(path):
    """settings.json as a dict ({} when missing or empty); ValueError when it is not a JSON object."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _is_ours(status_line):
    cmd = status_line.get("command") if isinstance(status_line, dict) else None
    return isinstance(cmd, str) and "statusline" in cmd


def _smoke_test(command):
    """Run the settings command as Claude Code would, with a sample payload. (ok, output)."""
    import subprocess
    env = dict(os.environ, STATUSLINE_LIVE_USAGE="0")
    try:
        r = subprocess.run(command, shell=True, input=json.dumps(SAMPLE_PAYLOAD), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30, env=env)
    except Exception as e:
        return False, str(e)
    out = (r.stdout or "").strip()
    return r.returncode == 0 and bool(out) and not out.startswith("Claude | --"), out or (r.stderr or "").strip()


def install_main(args):
    """install / uninstall. Returns the exit code; never edits a settings.json it cannot parse."""
    import shutil
    uninstall = args[0] == "uninstall"
    if "-h" in args or "--help" in args:
        print(INSTALL_USAGE, end="")
        return 0
    cdir = os.path.abspath(_opt(args, "--claude-dir") or CLAUDE_DIR)
    dry = "--dry-run" in args
    settings_path = os.path.join(cdir, "settings.json")
    record_path = os.path.join(cdir, INSTALL_RECORD)
    try:
        settings = _read_settings(settings_path)
    except (ValueError, OSError) as e:
        print(f"statusline: cannot read {settings_path} ({e}); nothing was changed")
        return 1
    record = {}
    try:
        with open(record_path, encoding="utf-8") as f:
            record = json.load(f)
        record = record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = None

    def save_copy(path, label=None):
        """Copy path into this run's backup folder (never in a dry run); the folder, or None."""
        if dry:
            return None
        nonlocal backup_dir
        if backup_dir is None:
            import tempfile
            root = os.path.join(cdir, "backups")
            os.makedirs(root, exist_ok=True)
            backup_dir = tempfile.mkdtemp(prefix=f"statusline-{stamp}-", dir=root)
        dest = backup_dir
        name = os.path.basename(path) if label is None else f"{label}-{os.path.basename(path)}"
        shutil.copy2(path, os.path.join(dest, name))
        return dest

    def backup():
        return save_copy(settings_path) if os.path.exists(settings_path) else None

    if uninstall:
        current = settings.get("statusLine")
        if _is_ours(current):
            previous = record.get("previous")
            action = "restored the previous statusLine" if previous else "removed the statusLine"
            if not dry:
                backup()
                if previous:
                    settings["statusLine"] = previous
                else:
                    settings.pop("statusLine", None)
                _write_json(settings_path, settings)
        else:
            action = "left settings.json alone (its statusLine is not this one)"
        if not dry:
            for name in ("statusline.py", INSTALL_RECORD, PROBE_CACHE_NAME, "statusline-usage.json",
                         "statusline-usage.lock"):
                try:
                    os.remove(os.path.join(cdir, name))
                except OSError:
                    pass
            pyc_dir = os.path.join(cdir, "__pycache__")
            for name in (os.listdir(pyc_dir) if os.path.isdir(pyc_dir) else []):
                if name.startswith("statusline"):
                    try:
                        os.remove(os.path.join(pyc_dir, name))
                    except OSError:
                        pass
        print(f"statusline: {action}{' (dry run, nothing written)' if dry else ''}; restart Claude Code")
        return 0

    python = _find_python()
    if python is None:
        print("statusline: Python 3.10 or newer was not found as 'python' or 'python3' on PATH; nothing was changed")
        return 1
    icons = (_opt(args, "--icons") or "auto").lower()
    if icons not in ("auto", "nerd", "none", "plain"):
        print(INSTALL_USAGE, end="")
        return 2
    want_font = icons in ("auto", "nerd") and "--no-font" not in args
    face = _nerd_font_face() if want_font else None
    font_lines = []
    if want_font and dry:
        font_lines = ([] if face or _has_nerd_font() else [f"  would install the Nerd Font {NERD_FONT_FACE}"]) \
            + _configure_terminals(face or NERD_FONT_FACE, save_copy, dry=True)
    elif want_font:
        os.makedirs(cdir, exist_ok=True)
        if face is None and not _has_nerd_font():
            face = _install_nerd_font()
        if face:
            font_lines = _configure_terminals(face, save_copy)
        elif _has_nerd_font():
            font_lines = ["  a Nerd Font is installed; make sure your terminal's font is set to it"]
    font = bool(face) or _has_nerd_font()
    if icons == "auto":
        icons = "nerd" if font else "none"
    icons = "none" if icons == "plain" else icons
    command = _launch_command(python, cdir, icons)
    new_line = dict(settings["statusLine"]) if isinstance(settings.get("statusLine"), dict) else {}
    new_line.update(type="command", command=command, refreshInterval=1)
    dest = os.path.join(cdir, "statusline.py")
    if dry:
        print(f"statusline: dry run in {cdir}\n  copy {os.path.abspath(__file__)} -> {dest}\n"
              f"  statusLine.command = {command}\n  refreshInterval = 1, icons = {icons}")
        for line in font_lines:
            print(line)
        return 0
    os.makedirs(cdir, exist_ok=True)
    backed_up = backup()
    originals = {}
    for path in (settings_path, record_path, dest):
        try:
            with open(path, "rb") as f:
                originals[path] = f.read()
        except FileNotFoundError:
            originals[path] = None
    try:
        same = os.path.exists(dest) and os.path.samefile(dest, __file__)
    except OSError:
        same = False
    if not same:
        shutil.copyfile(__file__, dest)
    pyc_dir = os.path.join(cdir, "__pycache__")  # a stale compile of an older copy must not be reused
    for name in (os.listdir(pyc_dir) if os.path.isdir(pyc_dir) else []):
        if name.startswith("statusline"):
            try:
                os.remove(os.path.join(pyc_dir, name))
            except OSError:
                pass
    previous = record.get("previous") if _is_ours(settings.get("statusLine")) else settings.get("statusLine")
    settings["statusLine"] = new_line
    _write_json(settings_path, settings)
    _write_json(record_path, {"previous": previous, "installed": stamp})
    if "--no-test" not in args:
        ok, out = _smoke_test(command)
        if not ok:
            for path, content in originals.items():
                if content is None:
                    try:
                        os.remove(path)
                    except FileNotFoundError:
                        pass
                else:
                    tmp = f"{path}.{os.getpid()}.tmp"
                    with open(tmp, "wb") as f:
                        f.write(content)
                    os.replace(tmp, path)
            print(f"statusline: the status line failed its self-test, so settings.json was put back.\n  {out}")
            return 1
        sample = re.sub(r"\x1b\[[0-9;]*m", "", out.splitlines()[0] if out else "")
        print(f"statusline: self-test ok: {sample}")
    print(f"statusline: installed to {dest}")
    if backed_up:
        print(f"  settings.json backed up in {backed_up}")
    for line in font_lines:
        print(line)
    if icons == "nerd" and not font:
        print("  note: no Nerd Font found; install one and select it in your terminal, or reinstall with --icons none")
    elif icons == "none":
        print("  plain icons: run `install --icons nerd` to get the Nerd Font icon set (it installs the font too)")
    print("  restart Claude Code to see it")
    return 0


def main():
    args = sys.argv[1:]  # argv[0] is this file, or '-c' when main() is imported and called
    if args and args[0] in ("install", "uninstall"):
        return install_main(args)
    if "--refresh" in args:
        # The spawning render created the lock and passed its token; a manual run takes its own.
        token = _opt(args, "--lock-token")
        if token is None:
            token = f"{os.getpid()}-{time.time_ns()}"
            if not _take_lock(time.time(), token):
                return
        _arm_deadline(token)
        try:
            refresh_cache()
        finally:
            _release_lock(token)
        return
    mode = _opt(args, "--icons") or os.environ.get("STATUSLINE_ICONS", "none")
    set_theme(os.environ.get("STATUSLINE_THEME", "dark"))
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--demo" in args:  # sample lines for a screenshot: no stdin, no probes, nothing written
        set_icons(effective_icons(mode))
        print("\n".join(demo_lines(width=line_width())))
        sys.exit(0)
    try:
        now = time.time()
        probes = ProbeCache(os.path.join(CLAUDE_DIR, PROBE_CACHE_NAME), now)
        raw = read_stdin()
        data = json.loads(raw) if raw.strip() else {}
        sid = data.get("session_id") if isinstance(data, dict) else None
        set_icons(effective_icons(mode, probes, sid[:64] if isinstance(sid, str) and sid else None))
        stdin_rl = data.get("rate_limits")
        stdin_rl = stdin_rl if isinstance(stdin_rl, dict) else {}
        live = None
        if os.environ.get("STATUSLINE_LIVE_USAGE", "1") != "0":
            cache = load_usage_cache()
            live = cache.get("usage")
            if read_oauth_token() or live:
                maybe_refresh(cache, now)
        try:
            usage = merge_usage(stdin_rl, live, now)
        except Exception:
            usage = merge_usage(stdin_rl, None, now)  # a bad cache must not hide the live numbers
        known = any(usage.values())
        # The 5h/7d items show once either source has had the windows (as '--' when they have
        # expired); a session that never had them (an API key, no live data yet) shows neither.
        show_usage = known or bool(stdin_rl) or bool(live)
        # One line, fitted to the window; every slow probe sits behind the probe cache.
        line = format_statusline(data, usage if known else None, now, show_usage,
                                 gather_extras(data, probes), line_width())
        print(line)
        probes.save()  # never raises
    except Exception:
        print("Claude | --")
    sys.exit(0)


def read_stdin():
    """The whole of stdin as text, decoded as UTF-8 whatever the console code page is."""
    buf = getattr(sys.stdin, "buffer", None)
    if buf is None:  # a text stream without bytes behind it (tests, odd hosts)
        return sys.stdin.read()
    return buf.read().decode("utf-8", "replace")


if __name__ == "__main__":
    sys.exit(main())
