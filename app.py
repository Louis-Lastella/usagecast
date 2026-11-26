#!/usr/bin/env python3
"""Usagecast: see what eats your AI usage.

Reads Hermes Agent's ~/.hermes/state.db read-only. For every session the real token counts Anthropic reported
(session_model_usage, priced with the API prices below) are split call by call over what was in the context at that
moment: tools, skills, plugins, system prompt parts, thinking, cache rebuilds after pauses.

  python3 app.py                          server on 127.0.0.1:7682 (env: PORT, HERMES_HOME, USAGECAST_*)
  python3 app.py --test                   self-test
  python3 app.py --ntfy-test              send a test alert
  <hermes-venv>/python app.py --snapshot  measure system prompt parts and tool schemas (the server does this daily)
"""
import base64, bisect, copy, glob, gzip, hmac, html, json, math, os, random, re, secrets, shutil, sqlite3, statistics, subprocess, sys, tempfile, threading, time, traceback, urllib.error, urllib.request
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))   # pets.py next to this file, also when run through exec() or runpy
from pets import KINDS as PETS, STATES as MOODS, pet   # noqa: E402
# USAGECAST_ROOT: snapshot() sets it before pointing HERMES_HOME at a profile; Hermes re-runs this script on import
HERMES = Path(os.environ.get("USAGECAST_ROOT") or os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
HERMES_PY = HERMES / "hermes-agent" / "venv" / "bin" / "python"
DATA = Path(os.environ.get("USAGECAST_DATA", ROOT / "data"))
PORT = int(os.environ.get("PORT", "7682"))
PUBLIC_URL = os.environ.get("USAGECAST_URL", "")   # dashboard address, opened when an alert is tapped
REPO_URL = "https://github.com/Louis-Lastella/usagecast"
VERSION = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").is_file() else "dev"
CPT = 3.5            # characters per token, only for proportions; amounts always come from the real token counts
IMAGE_TOK = 3000     # one image in the history, same units (agent.log shows ~3,900 real tokens)
LONG_CTX = 100_000   # a step that has to read more history than this counts as expensive; learned per agent up to 2x
LONG = {}            # agent -> learned long-context line, set by compute("30")
PERIODS = {"w": 7, "1": 1, "7": 7, "30": 30}   # days; "w" = since the weekly limit reset, 7 days without limit data
DEFAULT_P = "w"

# $ per million tokens: input, cache write 5 min, cache write 1 h, cache read, output.
# Source: platform.claude.com/docs/en/about-claude/pricing, as of 2026-10-08. The longest matching prefix wins.
PRICES = {
    "claude-fable-5-1": (10, 12.5, 20, 0.25, 50), "claude-fable-5": (10, 12.5, 20, 1, 50),
    "claude-mythos-5-1": (10, 12.5, 20, 0.25, 50), "claude-mythos-5": (10, 12.5, 20, 1, 50),
    "claude-opus-5-5": (4, 5, 8, 0.2, 20), "claude-opus-5": (5, 6.25, 10, 0.5, 25),
    "claude-opus-4-5": (5, 6.25, 10, 0.5, 25), "claude-opus-4-6": (5, 6.25, 10, 0.5, 25),
    "claude-opus-4-7": (5, 6.25, 10, 0.5, 25), "claude-opus-4-8": (5, 6.25, 10, 0.5, 25),
    "claude-opus-4": (15, 18.75, 30, 1.5, 75),
    "claude-sonnet-5-5": (2, 2.5, 4, 0.1, 10), "claude-sonnet-5": (2, 2.5, 4, 0.2, 10),
    "claude-sonnet-4": (3, 3.75, 6, 0.3, 15), "claude-haiku-4": (1, 1.25, 2, 0.1, 5),
}

# Fixed sections Hermes builds into every system prompt; each runs until the next marker found.
# SOUL.md, plugins and the memory provider are added per installation, see markers(). Ids are shown as seg.<id>.
HERMES_MARKERS = [
    ("base", r"^You run on |^# Finishing the job"),
    ("computer_use", r"^# Computer Use"),
    ("base", r"^Host: "),
    ("skills", r"^## Skills \(mandatory\)"),
    ("memory", r"^═+\nMEMORY"),
    ("base", r"^Conversation started:"),
]
# Components with their own name and explanation in the locale files (comp.<key>, comp.<key>.why)
FIXED = ("schema", "rebuild", "break", "think", "reply", "user", "sub", "other", "task:background_review",
         "task:compression", "task:approval", "task:title", "tool:skill_view")
TOOL_HINTS = ("terminal", "browser_exec", "session_search", "read_file", "vision_analyze")  # tip.tool.<name>
HERMES_PROJ, NO_PROJ = "@hermes", "@none"   # project ids, shown as proj.hermes / proj.none
OPT_SKIP = {"homebrew", "containerd", "local", "google", "bin", "lib"}  # system folders under /opt and /srv, no projects


# ---------- Language ----------
LOC = {p.stem: json.loads(p.read_text("utf-8")) for p in sorted((ROOT / "locales").glob("*.json"), key=lambda p: (p.stem != "en", p.stem))}  # English first
DEFAULT_LANG = os.environ.get("USAGECAST_LANG", "en") if os.environ.get("USAGECAST_LANG", "en") in LOC else "en"
_req = threading.local()   # language and URL of the request being rendered


def lang():
    return getattr(_req, "lang", None) or SET["lang"]


def tr(key, **kw):
    """Text for `key` in the current language; falls back to English, then to the key itself."""
    s = LOC[lang()].get(key)
    s = LOC["en"].get(key, key) if s is None else s
    return s.format(**kw) if kw else s


def pick_lang(wanted, headers):
    """?lang= beats the cookie, then the saved language (USAGECAST_LANG, English). The browser language is not used."""
    if wanted in LOC:
        return wanted
    m = re.search(r"(?:^|;)\s*lang=([a-z]+)", headers.get("Cookie", ""))
    return m.group(1) if m and m.group(1) in LOC else SET["lang"]


# ---------- Settings ----------
# /settings saves them in data/settings.json; the environment variables only give the defaults.
def _env_ntfy():
    """(server, topic) from USAGECAST_NTFY, a full topic URL."""
    url = os.environ.get("USAGECAST_NTFY", "")
    u = urlparse(url if "://" in url else "https://" + url)
    return (f"{u.scheme}://{u.netloc}", u.path.strip("/")) if url else ("https://ntfy.sh", "")


_SRV, _TOPIC = _env_ntfy()
ALL = "*"   # "source" setting: every Hermes home, the main one and each profile
DEFAULTS = {
    "theme": "system", "lang": DEFAULT_LANG, "currency": "USD", "numbers": "compact", "period": DEFAULT_P, "limit_view": "used",
    "cost_unit": "money", "source": ALL,
    "alert_five": True, "five_min": 30, "alert_week": True, "alert_extra": True, "alert_free": True, "alert_steps": True,
    "alert_digest": True, "alert_chat": True, "chat_pct": 0.5, "alert_spike": True, "spike_pct": 3.0, "extra_steps": "",
    "guard": False, "guard_jobs": [], "ingest_token": "",
    "quiet": False, "quiet_from": "22:00", "quiet_to": "07:00",
    "push": "own" if _TOPIC else "off", "ntfy_server": _SRV, "ntfy_topic": _TOPIC, "ntfy_token": "", "url": PUBLIC_URL,
    "bark_url": "", "bark_key": "", "bark_user": "", "bark_pass": "",
    "tg_bot": "off", "tg_thread": "", "tg_menu": True, "tg_pin": True, "pet": "terminal",
}
CHOICES = {"theme": ("system", "light", "dark"), "lang": tuple(LOC), "currency": ("USD", "EUR"), "numbers": ("compact", "full"),
           "period": tuple(PERIODS), "limit_view": ("used", "left"), "cost_unit": ("money", "week"), "push": ("off", "own", "hermes", "bark", "telegram"),
           "pet": ("off", *PETS)}
RANGES = {"five_min": (5, 240), "chat_pct": (0.1, 50.0), "spike_pct": (0.5, 50.0)}
URL_RX = r"https?://[^\s\"'<>]+"
HHMM = r"([01]\d|2[0-3]):[0-5]\d"
FORMATS = {"tg_thread": r"\d{0,12}", "quiet_from": HHMM, "quiet_to": HHMM, "ntfy_topic": r"[A-Za-z0-9_-]{1,64}", "ntfy_server": URL_RX, "url": f"({URL_RX})?",
           "bark_url": f"({URL_RX})?", "bark_key": r"[A-Za-z0-9_-]{0,64}", "bark_user": r"[^\s:]{0,64}",
           "extra_steps": r"(\d+(\.\d+)?(\s*[,; ]\s*\d+(\.\d+)?)*)?"}
# Sub-pages of /settings and the keys each one saves: a form changes only its own section, so a checkbox missing from
# one page never switches off a setting of another. Every key of DEFAULTS is in exactly one section (selftest).
SECTIONS = {
    "look": ("theme", "lang", "currency", "numbers", "period", "limit_view", "cost_unit", "pet"),
    "alerts": ("alert_five", "five_min", "alert_week", "alert_free", "alert_steps", "alert_extra", "extra_steps", "alert_chat", "chat_pct",
               "alert_spike", "spike_pct", "alert_digest", "quiet", "quiet_from", "quiet_to"),
    "notify": ("push", "ntfy_topic", "bark_url", "bark_key", "bark_user", "bark_pass", "tg_bot", "tg_thread", "tg_menu", "tg_pin"),
    "budget": ("guard", "guard_jobs"),
    "advanced": ("ingest_token", "source", "ntfy_server", "ntfy_token", "url"),
}
OLD_ANCHORS = {"look": "look", "limits": "look", "alerts": "alerts", "push": "notify", "tg": "notify", "budget": "budget",
               "machines": "advanced", "src": "advanced"}   # /settings#<card> of the one-page settings -> sub-page


def load_settings():
    try:
        saved = json.loads((DATA / "settings.json").read_text())
    except (OSError, ValueError):
        saved = {}
    ok = lambda k, d: type(saved.get(k)) is type(d) and (k not in CHOICES or saved[k] in CHOICES[k])
    return {k: saved[k] if ok(k, d) else d for k, d in DEFAULTS.items()}


SET = load_settings()


def profiles():
    """Hermes profiles (<home>/profiles/<name> with an own state.db) that /settings can analyse instead."""
    d = HERMES / "profiles"
    return sorted(p.name for p in d.iterdir() if (p / "state.db").is_file()) if d.is_dir() else []


def source_home():
    """Hermes home the analysis reads (state.db, logs, config.yaml, cron, SOUL.md): the one at_home() set for this
    thread, else the main one or the chosen profile. Code and limits always come from the main home, profiles share the
    install and the account."""
    h = getattr(_req, "home", None)
    if h:
        return h
    return HERMES / "profiles" / SET["source"] if SET["source"] in profiles() else HERMES


def homes():
    """(agent, home) of every Hermes home the dashboard covers: the main one ("") and each profile, or the chosen one."""
    if SET["source"] == ALL:
        return [("", HERMES), *((n, HERMES / "profiles" / n) for n in profiles())]
    return [(SET["source"], HERMES / "profiles" / SET["source"])] if SET["source"] in profiles() else [("", HERMES)]


@contextmanager
def at_home(h):
    """Points source_home() of this thread at h while the block runs."""
    old, _req.home = getattr(_req, "home", None), h
    try:
        yield
    finally:
        _req.home = old


def agent_label(a):
    return a or "Hermes"


def bot_key(x):
    """A bot = an agent on one platform; cron runs and subagents count as one bot per agent."""
    return f'{x.get("agent", "")}:{x.get("platform") or x.get("src") or "?"}'


def bot_label(key):
    """"Atlas · Telegram": the bot's own name (data/bots.json, see refresh_bots()), else the agent's name."""
    a, _, plat = key.rpartition(":")
    names = ((read_json(DATA / "bots.json") or {}).get("names") or {}).get(a) or {}
    return f'{names.get(plat) or names.get("telegram") or next(iter(names.values()), None) or agent_label(a)} · {origin_label(plat)}'


def chat_label(ch):
    """(name, context) of a chat from chat_index(); the topic number or chat id only when no name is known."""
    if ch["name"]:
        return ch["name"], ch["ctx"] or (tr("chat.thread") if ch["n"] and ch["kind"] != "topic" else "")
    if ch["kind"] == "topic":
        return tr("chat.topic", n=ch["n"]), ch["ctx"]
    return {"cron": tr("untitled"), "cli": tr("chat.cli")}.get(ch["kind"]) or ch["id"] or tr("chat.none"), ch["ctx"]


def bot_chat(x):
    """"Atlas · Telegram · Shop": the bot and chat of a session, unescaped."""
    return f'{bot_label(bot_key(x))} · {chat_label(x.get("chat") or NOCHAT)[0]}'


def by_bot(sess):
    """{bot key: [$, {chat key: [$, chat, sessions]}]} of an analysis' sessions."""
    g = defaultdict(lambda: [0.0, defaultdict(lambda: [0.0, NOCHAT, 0])])
    for x in sess.values():
        b, ch = g[bot_key(x)], x.get("chat") or NOCHAT
        b[0] += x["cost"]
        c = b[1][ch["key"]]
        c[0] += x["cost"]; c[1] = ch; c[2] += 1
    return g


def source_name():
    hs = homes()
    if len(hs) > 1:
        return tr("src.all", n=len(hs))
    return "Hermes" if hs[0][1] == HERMES else tr("src.profile", name=html.escape(hs[0][0]))


def clean(form, cur, keys=DEFAULTS):
    """Settings from a submitted /settings form: a missing checkbox is off, an invalid value keeps the old one.
    keys: the settings the form holds (a section of SECTIONS); all others stay as they are."""
    s = dict(cur)
    for k in keys:
        d, v = DEFAULTS[k], form.get(k, "").strip()
        if isinstance(d, bool):
            s[k] = k in form
        elif k in CHOICES:
            s[k] = v if v in CHOICES[k] else s[k]
        elif k in RANGES:
            try:
                x = type(d)(v.replace(",", "."))
            except ValueError:
                x = None
            s[k] = min(max(x, RANGES[k][0]), RANGES[k][1]) if x is not None and math.isfinite(x) else s[k]
        elif k == "source":
            s[k] = v if k in form and v in ("", ALL, *profiles()) else s[k]   # "" is the main home, not a missing field
        elif k == "tg_bot":
            s[k] = v if v in ("off", "hermes", *profiles()) else s[k]
        elif k == "ingest_token":
            pass   # only the "new token" button sets it
        elif k == "guard_jobs":
            ids = [j["id"] for j in cron_list()]
            s[k] = [j for j in ids if "gj_" + j in form] if ids else s[k]   # unreadable jobs.json: keep the marks
        elif k in ("ntfy_token", "bark_pass"):   # empty field = keep the saved secret
            s[k] = "" if ("token_clear" if k == "ntfy_token" else "bark_pass_clear") in form else v[:256] or s[k]
        elif k in form and re.fullmatch(FORMATS[k], v):
            s[k] = v.rstrip("/")
    return s


def save_settings(s):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / "settings.json.tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:  # holds the ntfy token
        json.dump(s, fh, indent=1)
    tmp.replace(DATA / "settings.json")
    moved = s.get("source") != SET.get("source")
    SET.clear()
    SET.update(s)
    if moved:   # another Hermes home: nothing computed so far is valid
        with LOCK:
            CACHE.clear()
        CAL.clear()
        RATE.clear()
        threading.Thread(target=ensure_snap, daemon=True).start()


def new_topic():
    return "usagecast-" + secrets.token_hex(8)   # whoever knows a ntfy topic can read it, so 64 random bits


def quiet_now(now, s=None):
    s = s or SET
    t, a, b = datetime.fromtimestamp(now).strftime("%H:%M"), s["quiet_from"], s["quiet_to"]
    return s["quiet"] and (a <= t < b if a <= b else t >= a or t < b)


def same_origin(h):
    """Settings are saved only from Usagecast's own pages: the browser's Sec-Fetch-Site (https), else Origin vs Host."""
    if h.get("Sec-Fetch-Site"):
        return h["Sec-Fetch-Site"] == "same-origin"
    o = urlparse(h.get("Origin") or "")
    return bool(o.netloc) and o.netloc == h.get("Host")


FX = {}   # ECB reference rate: rate = dollars per euro
try:
    FX.update(json.loads((DATA / "fx.json").read_text()))
except (OSError, ValueError):
    pass


def refresh_fx(max_age=6 * 3600):
    """Euro amounts use the ECB's daily reference rate (published on working days around 16:00 CET)."""
    if time.time() - FX.get("at", 0) < max_age:
        return
    try:
        with urllib.request.urlopen("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", timeout=15) as r:
            x = r.read().decode()
        FX.update(rate=float(re.search(r"currency=.USD. rate=.([\d.]+)", x)[1]), date=re.search(r"time=.([\d-]+)", x)[1], at=time.time())
    except (OSError, ValueError, TypeError) as err:
        print("fx:", err, flush=True)
        return
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "fx.json").write_text(json.dumps(FX))


def nf(v, digits=0):
    """Number with the language's thousands and decimal separators."""
    return f"{v:,.{digits}f}".translate(str.maketrans(",.", tr("num.sep")))


def money(v):
    """API value in the chosen unit: money, or the estimated share of the weekly limit (RATE, see limit_split)."""
    k = RATE.get("k") if SET["cost_unit"] == "week" else None
    if not k:
        return cash(v)
    x = v * k
    return "< " + tr("fmt.week", v=nf(0.1, 1)) if 0 < x < 0.1 else tr("fmt.week", v=nf(x, 1))


def cash(v):
    rate = FX.get("rate") if SET["currency"] == "EUR" else None   # all prices are in dollars
    key, v = ("fmt.eur", v / rate) if rate else ("fmt.money", v)
    return "< " + tr(key, v=nf(0.01, 2)) if 0 < v < 0.01 else tr(key, v=nf(v, 2))


def pct(v, tot):
    return tr("fmt.pct", v=nf(v / (tot or 1) * 100, 1))


def pc(u):
    return tr("fmt.pct", v=nf(u))


def num(n):
    n = float(n or 0)
    if SET["numbers"] == "full":
        return nf(n)
    if n >= 1e9:
        return tr("num.b", v=nf(n / 1e9, 1))
    if n >= 1e6:
        return tr("num.m", v=nf(n / 1e6, 1))
    return tr("num.k", v=nf(n / 1e3)) if n >= 1e3 else nf(n)


def cnt(n):
    return nf(round(n or 0))


def hm(t):
    return datetime.fromtimestamp(t).strftime("%H:%M")


def when(t, kind):
    """Date in the language's pattern; kind: day, daytime, datetime, short."""
    dt, L = datetime.fromtimestamp(t), LOC[lang()]
    return L["fmt." + kind].format(wd=L["weekdays"][dt.weekday()], mon=L["months"][dt.month - 1], d=dt.day,
                                    m=dt.month, y=dt.year, h=dt.hour, hm=dt.strftime("%H:%M"))


# ---------- Analysis ----------
def tok(s):
    return len(s or "") / CPT


def model_name(m):
    """"Opus 5.5" from claude-opus-5-5 (a date suffix like -20251001 is dropped); other ids stay as they are."""
    if not (m or "").startswith("claude-"):
        return m or ""
    name, *rest = m[7:].split("-")
    return " ".join([name.title(), ".".join(x for x in rest if x.isdigit() and len(x) < 3)]).strip()


def models_of(rows, ttl):
    """Model names of (model, i, r, w, o) rows, the one that cost most first."""
    c = defaultdict(float)
    for m, i, r, w, o in rows:
        c[model_name(m)] += sum(row_cost(m, i, r, w, o, ttl))
    return sorted(c, key=lambda k: -c[k])


def known_model(model):
    return any(model.startswith(k) for k in PRICES)


def price(model):
    # ponytail: unknown Claude models fall back to Opus 5.5 prices, the UI marks them as estimated
    key = max((k for k in PRICES if model.startswith(k)), key=len, default="claude-opus-5-5")
    return [p / 1e6 for p in PRICES[key]]


def row_cost(model, i, r, w, o, ttl):
    """Cost of one usage row in $: (input, cache read, cache write, output)."""
    p = price(model)
    return (i or 0) * p[0], (r or 0) * p[3], (w or 0) * (p[2] if ttl > 300 else p[1]), (o or 0) * p[4]


def config_text():
    try:
        return (source_home() / "config.yaml").read_text()
    except OSError:
        return ""


def config_value(section, key, text=None):
    """Simple value section.key from Hermes' config.yaml, without a YAML library."""
    m = re.search(rf"^{section}:[ \t]*\n(?:[ \t]*\n|[ \t]+.*\n)*?[ \t]+{key}:[ \t]*['\"]?([^'\"\s#]*)",
                  config_text() if text is None else text, re.M)
    return m.group(1) if m else ""


def cache_ttl():
    return 3600 if config_value("prompt_caching", "cache_ttl").lower() == "1h" else 300


def plugin_names(text=None):
    """Enabled plugins (plugins.enabled) plus the memory provider, without platform adapters like platforms/ntfy."""
    text = config_text() if text is None else text
    m = re.search(r"^plugins:[ \t]*\n(?:[ \t]+.*\n)*?[ \t]+enabled:[ \t]*\n((?:[ \t]+-.*\n)+)", text, re.M)
    names = re.findall(r"-[ \t]*['\"]?([^'\"\s#]+)", m.group(1)) if m else []
    return [n for n in names + [config_value("memory", "provider", text)] if n and "/" not in n]


def name_rx(n):
    """Name as a regex, no matter if written with -, _ or spaces and in which case."""
    return "(?i:" + "[-_ ]?".join(map(re.escape, re.split(r"[-_ ]+", n))) + ")"


def markers(plugins=None):
    """(system prompt sections, message injections) for this Hermes installation, as [(id, regex)]."""
    plugins = plugin_names() if plugins is None else plugins
    soul = "soul" if (source_home() / "SOUL.md").is_file() else "base"
    prompt = [(soul, r"\A"), *HERMES_MARKERS, *(("plugin:" + n, rf"^#+ .*{name_rx(n)}") for n in plugins)]
    inject = [*(("plugin:" + n, rf"^(?:<\w+>\s*)?[^\w\n]*{name_rx(n)}") for n in plugins),
              ("notes", r"^\[(?:Note|System note|Context from)")]
    return prompt, inject


def segments(text, markers):
    """[(id, section)] split at the markers; text before the first marker is 'misc'."""
    hits = sorted((m.start(), label) for label, rx in markers for m in [re.search(rx, text, re.M)] if m)
    if not hits or hits[0][0] > 0:
        hits.insert(0, (0, "misc"))
    return [(label, text[s:e]) for (s, label), (e, _) in zip(hits, hits[1:] + [(len(text), "")]) if text[s:e].strip()]


def injections(content, api, marks):
    extra = api or ""
    if extra and content:
        extra = extra[len(content):] if extra.startswith(content) else extra.replace(content, "", 1)
    return segments(extra, marks) if extra.strip() else []


def tool_of(tc):
    f = tc.get("function") or {}
    name, args = f.get("name") or "?", f.get("arguments") or ""
    if name == "tool_call":  # lazily loaded tools: use the real name
        try:
            a = json.loads(args)
            name, args = a.get("name") or name, json.dumps(a.get("arguments") or {})
        except (ValueError, AttributeError):
            pass
    return name, args


def key_of(name, args):
    if name != "skill_view":
        return "tool:" + name
    try:
        a = json.loads(args)
        return "skill:" + a.get("name", "?") + (" / " + a["file_path"] if a.get("file_path") else "")
    except (ValueError, AttributeError, TypeError):
        return "skill:?"


def prefix_for(prompt, snap, marks):
    p = defaultdict(float)
    if prompt:
        for label, seg in segments(prompt, marks):
            p["sys:" + label] += tok(seg)
    else:
        for label, t in snap.get("prompt", {}).items():
            p["sys:" + label] += t
    p["schema"] = sum(snap.get("tools", {}).values())
    return p


def simulate(msgs, prefix, extra_out=0.0, inject=()):
    """Replays a session call by call: what was in the prompt at every API call.

    msgs: (role, content, api_content, tool_name, tool_calls, tool_call_id, timestamp, reasoning)
    extra_out: output per call that Hermes does not store as text (thinking) but that stays in the history.
    inject: markers for blocks that plugins and Hermes attach to messages (see markers()).
    Per call: (start time, gap to the previous call, prompt before{}, prompt now{}, output{}, [called components]).
    Plus sizes [(time, component, tokens)] of tool results and plugin injections."""
    ctx, before, last, prev, calls, sizes, pending = defaultdict(float, prefix), {}, None, None, [], [], {}
    for role, content, api, tname, tcalls, tcid, ts, reasoning in msgs:
        if role == "assistant":
            t = prev if prev is not None else ts  # the call starts with the message before it
            now = dict(ctx)
            out, keys = defaultdict(float), []
            out["reply"] += tok(content)
            out["think"] += tok(reasoning) + extra_out
            for tc in json.loads(tcalls or "[]"):
                name, args = tool_of(tc)
                k = key_of(name, args)
                pending[tc.get("id")] = k
                out[k] += tok(args)
                keys.append(k)
            for k, v in out.items():
                ctx[k] += v
            calls.append((t, t - last if last is not None else float("inf"), before, now, dict(out), keys))
            before, last = now, t
        elif role == "tool":
            k = pending.get(tcid) or "tool:" + (tname or "?")
            n = tok(content) + IMAGE_TOK * (content or "").count("[screenshot]")
            ctx[k] += n
            sizes.append((ts, k, n))
        else:  # user, session_meta
            ctx["user"] += tok(content) + IMAGE_TOK * (content or "").count("[Image attached")
            for label, seg in injections(content, api, inject):
                ctx["inj:" + label] += tok(seg)
                sizes.append((ts, "inj:" + label, tok(seg)))
        prev = ts
    return calls, sizes


def is_prefix(k):
    return k == "schema" or k.startswith("sys:")


def cache_split(before, now, gap, ttl, rho=None):
    """What a call read from the cache, what it wrote anew and how much of the old prompt was lost.

    rho: share of the previous prompt that really came from the cache according to agent.log. Without the log the
    rule applies: after more than `ttl` seconds of pause the cache is gone. The cache always returns the beginning
    of the prompt, so system prompt and tools first, then the history."""
    cand = before or {k: v for k, v in now.items() if is_prefix(k)}
    if rho is None:
        rho = 1.0 if before and gap <= ttl else 0.0
    total, pre = sum(cand.values()), sum(v for k, v in cand.items() if is_prefix(k))
    keep = total if rho > 0.97 else rho * total  # small deviations are measurement noise
    f = min(keep / pre, 1.0) if pre else 0.0
    g = min(max(keep - pre, 0.0) / (total - pre), 1.0) if total > pre else 0.0
    hit = {k: v * (f if is_prefix(k) else g) for k, v in cand.items()}
    new = {k: v - max(before.get(k, 0.0), hit.get(k, 0.0)) for k, v in now.items()
           if v - max(before.get(k, 0.0), hit.get(k, 0.0)) > 1e-9}
    lost = sum(max(v - hit.get(k, 0.0), 0.0) for k, v in before.items())
    return hit, new, lost


LOG_RX = re.compile(r"^(\S+ \S+),(\d+) \w+ \[([^\]]+)\] agent\.conversation_loop: API call #\d+: .*? in=(\d+) out=\d+ "
                    r".*?latency=[\d.]+s(?: cache=(\d+)/)?")
_LOGS = {}


def log_index():
    """Real values per API call from Hermes' agent.log: {session: ([end time], [(total input, of that from cache)])}.
    Rotated files are read only once."""
    idx = defaultdict(list)
    for f in glob.glob(str(source_home() / "logs" / "agent.log*")):
        try:
            sig = (os.path.getmtime(f), os.path.getsize(f))
        except OSError:
            continue
        if _LOGS.get(f, (None,))[0] != sig:
            rows = []
            with (gzip.open if f.endswith(".gz") else open)(f, "rt", errors="replace") as fh:
                for line in fh:
                    m = "API call #" in line and LOG_RX.match(line)
                    if m:
                        t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + int(m.group(2)) / 1000
                        rows.append((m.group(3), t, int(m.group(4)), int(m.group(5) or 0)))
            _LOGS[f] = (sig, rows)
        for sid, t, i, r in _LOGS[f][1]:
            idx[sid].append((t, i, r))
    return {k: ([x[0] for x in sorted(v)], [x[1:] for x in sorted(v)]) for k, v in idx.items()}


def origin(src, title):
    """Language-neutral origin id: the source, or cron:<job name> for cron sessions."""
    if src == "cron":
        return "cron:" + (title or "").split(" · ")[0]
    return src or "?"


CHAT_COLS = ("source", "title", "parent_session_id", "chat_id", "chat_type", "thread_id", "display_name", "origin_json")
NOCHAT = dict(key="?", name="", ctx="", kind="", n="", id="")


def chat_index(c):
    """{session id: (platform, chat)} of every session in a Hermes db. chat = dict(key, name, ctx, kind, n, id), names as
    stored, no display text: key platform:chat_id:thread_id is stable, name is the Telegram topic or the last part of a
    "Server / #channel / thread" name, ctx the rest (or the chat a topic lives in). A topic without a stored name takes
    it from another session of the same topic. Cron sessions are their job, subagents get the chat that started them."""
    cols = {r[1] for r in c.execute("pragma table_info(sessions)")}
    raw, topics, out = {}, {}, {}
    for sid, src, title, par, cid, ct, th, dn, oj in c.execute(
            f"select id, {', '.join(k if k in cols else 'null' for k in CHAT_COLS)} from sessions order by started_at"):
        try:
            o = json.loads(oj) if oj else {}
        except ValueError:
            o = {}
        o = o if isinstance(o, dict) else {}   # old rows have no origin_json: the plain columns are all there is
        plat = src if src in ("cron", "subagent") else o.get("platform") or src or "?"
        cid, th, ct = str(o.get("chat_id") or cid or ""), str(o.get("thread_id") or th or ""), o.get("chat_type") or ct or ""
        if plat == "telegram" and th and o.get("chat_topic"):   # elsewhere chat_topic is the channel description
            topics[(cid, th)] = o["chat_topic"]
        raw[sid] = (src, title, par, plat, cid, ct, th, o.get("chat_name") or (dn if ct == "dm" else "") or "")
    for sid, (src, title, par, plat, cid, ct, th, name) in raw.items():
        if src == "cron":
            out[sid] = (plat, dict(NOCHAT, key=origin(src, title), name=origin(src, title)[5:], kind="cron"))
        elif plat == "telegram" and th:
            out[sid] = (plat, dict(key=f"{plat}:{cid}:{th}", name=topics.get((cid, th), ""), ctx=name, kind="topic", n=th, id=cid))
        else:
            head, _, last = name.rpartition(" / ")
            out[sid] = (plat, dict(key=f"{plat}:{cid}:{th}", name=last, ctx=head, kind=ct or ("cli" if plat == "cli" else ""), n=th, id=cid))
    for sid, (src, _, par, *_r) in raw.items():
        hops = 0
        while src == "subagent" and par in raw and raw[par][0] == "subagent" and hops < 20:   # a subagent's subagent
            par, hops = raw[par][2], hops + 1
        if src == "subagent":
            out[sid] = ("subagent", out[par][1] if par in raw and raw[par][0] != "subagent" else NOCHAT)
    return out


def floor(t, hourly):
    if hourly:
        return t - t % 3600
    return datetime.fromtimestamp(t).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


IMG = ("[screenshot]", "[Image attached")   # how Hermes stores images in the history
BREAKS = ("shrunk", "image", "turn", "deep", "other", "sim")


def break_cause(rows, apos, j, real, prefix):
    """Why the cache broke right before assistant row j although there was no pause. shrunk: the prompt got smaller,
    older history was removed or rewritten. image: a new image while more than 3 are in the history (Hermes drops old
    ones from the request). turn: a new message came in (the gateway reloads the conversation, images saved as text
    change). deep: only the system prompt was read from cache, the change sat more than ~20 blocks back. other: none
    of these. sim: no log values for this step, the replay only saw the start of the prompt change."""
    if not real[j]:
        return "sim"
    new = rows[apos[j - 1] + 1:apos[j]] if j else rows[:apos[0]]
    if j and real[j - 1] and real[j][0] < real[j - 1][0]:
        return "shrunk"
    imgs = lambda rs: sum((m[1] or "").count(x) for m in rs for x in IMG)
    if imgs(new) and imgs(rows[:apos[j]]) > 3:
        return "image"
    if any(m[0] == "user" for m in new):
        return "turn"
    return "deep" if real[j][1] <= prefix * 1.1 else "other"


def split_turns(steps, users):
    """[(start, end, steps, $)] of one session: a turn = the API steps between two user messages. steps: [(time, $)]
    in order, users: sorted user message times."""
    out = []
    for t, c in steps:
        i = bisect.bisect_right(users, t)
        start = users[i - 1] if i else steps[0][0]
        if out and out[-1][0] == start:
            out[-1] = (start, t, out[-1][2] + 1, out[-1][3] + c)
        else:
            out.append((start, t, 1, c))
    return out


def analyze(c, since, snap, ttl, sid=None, logs=None, until=math.inf):
    """Splits the real costs since `since` (or of one session) over components, origin and time.
    Invariant: sum(comp) == real cost of the calls in the period. Contains only ids, no display text."""
    logs = log_index() if logs is None else logs
    pm, im = markers()
    cond, arg = ("u.session_id = ?", sid) if sid else ("u.last_seen > ?", since)
    usage = defaultdict(list)
    for r in c.execute(f"""select u.session_id, u.model, coalesce(nullif(u.task, ''), 'chat'), u.api_call_count,
            u.input_tokens, u.cache_read_tokens, u.cache_write_tokens, u.output_tokens, coalesce(u.last_seen, 0)
            from session_model_usage u where {cond} and u.model like 'claude%'""", (arg,)):
        usage[r[0]].append(r[1:])
    ids = list(usage)
    q = ",".join("?" * len(ids))
    cols = {r[1] for r in c.execute("pragma table_info(sessions)")}
    root, par = (k if k in cols else "null" for k in ("git_repo_root", "parent_session_id"))
    meta = {r[0]: r[1:] for r in c.execute(
        f"select id, source, title, system_prompt_hash, started_at, {root}, {par} from sessions where id in ({q})", ids)}
    prompts = dict(c.execute(f"""select hash, prompt from system_prompts where hash in
        (select system_prompt_hash from sessions where id in ({q}))""", ids))
    chats = chat_index(c)
    msgs = defaultdict(list)
    for r in c.execute(f"""select session_id, role, content, api_content, tool_name, tool_calls, tool_call_id,
            timestamp, reasoning from messages where session_id in ({q}) order by session_id, id""", ids):
        msgs[r[0]].append(r[1:])

    hourly = sid is None and min(until, time.time()) - since < 2 * 86400
    comp, where = defaultdict(float), defaultdict(float)
    hours = defaultdict(lambda: defaultdict(float))       # hour start -> component -> $
    hsteps, hsess = defaultdict(int), defaultdict(set)    # hour start -> steps, sessions
    models = defaultdict(lambda: [0.0, 0.0, 0.0])        # calls, tokens, $
    uses, sizes_acc = defaultdict(int), defaultdict(lambda: [0, 0.0])
    sess, steps, breaks, heavy = {}, [], defaultdict(lambda: [0, 0.0]), []   # breaks: cause -> [count, $]
    ctxc = defaultdict(float)   # 10k context bucket -> $
    tot = dict(calls=0.0, tokens=0.0, writes=0.0, avoid=0.0, extra=0.0, n_steps=0, logged=0)
    repos, prx = home_repos(), project_rx()

    for s, rows in usage.items():
        src, title, hsh, started, repo_root, parent = meta.get(s, ("?", None, None, 0, None, None))
        sc, n_calls, turns, last, pk, ctx1 = defaultdict(float), 0, [], [0.0], [], 0
        tk0 = tot["tokens"]

        def put(k, v, t):
            sc[k] += v
            last[0] = max(last[0], t)
            h = t - t % 3600
            hours[h][k] += v
            hsess[h].add(s)

        for m, task, n, i, r, w, o, seen in (x for x in rows if x[1] != "chat"):
            if since < seen <= until:
                cost = sum(row_cost(m, i, r, w, o, ttl))
                put("task:" + task, cost, seen)
                models[m][0] += n; models[m][1] += i + r + w + o; models[m][2] += cost
                tot["calls"] += n; tot["tokens"] += i + r + w + o
        chat = [x for x in rows if x[1] == "chat"]
        if not chat:
            pass
        elif not msgs.get(s):
            seen = max(x[7] for x in chat)
            if since < seen <= until:
                cost = sum(sum(row_cost(m, i, r, w, o, ttl)) for m, _, _, i, r, w, o, _ in chat)
                put("other", cost, seen)
                for m, _, n, i, r, w, o, _ in chat:
                    models[m][0] += n; models[m][1] += i + r + w + o; models[m][2] += sum(row_cost(m, i, r, w, o, ttl))
                    tot["calls"] += n; tot["tokens"] += i + r + w + o
        else:
            ci, cr, cw, co = (sum(x) for x in zip(*(row_cost(m, i, r, w, o, ttl) for m, _, _, i, r, w, o, _ in chat)))
            I, R, W, O = (sum(x) for x in zip(*((i, r, w, o) for _, _, _, i, r, w, o, _ in chat)))
            chat_cost = ci + cr + cw + co
            pre = prefix_for(prompts.get(hsh), snap, pm)
            calls, sizes = simulate(msgs[s], pre, inject=im)
            # Output Hermes does not store as text (thinking) still stays in the history: second pass
            kt = (I + R + W) / (sum(sum(x[3].values()) for x in calls) or 1)  # real tokens per simulated token
            missing = O / kt - sum(sum(x[4].values()) for x in calls)
            if missing > 0 and calls:
                calls, sizes = simulate(msgs[s], pre, missing / len(calls), im)
                kt = (I + R + W) / (sum(sum(x[3].values()) for x in calls) or 1)
            # Attach real values per call from the log (end of the call = timestamp of the answer)
            lt, lv = logs.get(s, ([], []))
            ends = [m[6] for m in msgs[s] if m[0] == "assistant"]
            real = []
            for at in ends:
                i = bisect.bisect_left(lt, at - 3)
                real.append(lv[i] if i < len(lt) and abs(lt[i] - at) <= 3 else None)
            splits = []
            for j, (t, gap, before, now, out, keys) in enumerate(calls):
                rl, rho = real[j], None
                if rl:
                    prev_in = real[j - 1][0] if j and real[j - 1] else \
                        (sum(before.values()) or sum(v for k, v in now.items() if is_prefix(k))) * kt
                    rho = min(rl[1] / prev_in, 1.0) if prev_in else 0.0
                hit, new, lost = cache_split(before, now, gap, ttl, rho)
                splits.append((hit, new, lost, "rebuild" if gap > ttl else "break", rl is not None))
            # Spread the real cost per token type over the simulated shares
            SR = sum(sum(x[0].values()) for x in splits)
            SW = sum(sum(x[1].values()) + x[2] for x in splits)
            SO = sum(sum(x[4].values()) for x in calls)
            fr, fw = (cr / SR, (cw + ci) / SW) if SR and SW else (0.0, (cr + cw + ci) / (SR + SW or 1))
            fo = co / SO if SO else 0.0
            p_cost, apos, tsteps = 0.0, [i for i, m in enumerate(msgs[s]) if m[0] == "assistant"], []
            # ponytail: a result is written once and read on every later step; rebuilds and compression are ignored
            ts = [x[0] for x in calls]
            for m in msgs[s]:
                if m[0] == "tool" and since < m[6] <= until:
                    n = tok(m[1]) + IMAGE_TOK * sum((m[1] or "").count(x) for x in IMG)
                    later = len(ts) - bisect.bisect_right(ts, m[6])
                    heavy.append((n * (fw + fr * later), s, m[3] or "?", n * kt, later, m[6]))
            for j, ((t, gap, before, now, out, keys), (hit, new, lost, kind, logged)) in enumerate(zip(calls, splits)):
                if t <= since or t > until:
                    continue
                parts = defaultdict(float)
                for k, v in hit.items():
                    parts[k] += v * fr
                for k, v in new.items():
                    parts[k] += v * fw
                parts[kind] += lost * fw
                for k, v in out.items():
                    parts[k] += v * fo
                if not SO:
                    parts["think"] += co / len(calls)
                cost = sum(parts.values())
                for k, v in parts.items():
                    put(k, v, t)
                p_cost += cost
                tsteps.append((t, cost))
                n_calls += 1
                hsteps[t - t % 3600] += 1
                tot["n_steps"] += 1; tot["logged"] += logged
                ctx_tok = sum(now.values()) * kt
                ctxc[int(ctx_tok) // 10_000 * 10_000] += cost
                pk.append((t, ctx_tok))
                # for the cache duration tip: what 1 h instead of 5 min (or the other way round) would have changed
                tot["writes"] += (sum(new.values()) + lost) * fw
                if kind == "rebuild" and gap <= 3600:
                    tot["avoid"] += lost * fw
                if not lost and 300 < gap <= 3600:
                    tot["extra"] += sum(before.values()) * fw
                for k in keys:
                    uses[k] += 1
                why = ""
                if kind == "break" and lost > 1e-9:
                    why = break_cause(msgs[s], apos, j, real, sum(v for k, v in now.items() if is_prefix(k)) * kt)
                    breaks[why][0] += 1
                    breaks[why][1] += lost * fw
                if sid:
                    steps.append((t, ctx_tok, cost, lost * fw, (kind + ":" + why if why else kind) if lost > 1e-9 else "", keys))
            turns = split_turns(tsteps, sorted(m[6] for m in msgs[s] if m[0] == "user"))
            if turns and started > since:   # what a fresh task needed: peak context of the first turn
                ctx1 = max(ct for t, ct in pk if t <= turns[0][1])
            for t, k, n in sizes:
                if since < t <= until:
                    sizes_acc[k][0] += 1; sizes_acc[k][1] += n
            f = p_cost / chat_cost if chat_cost else 0.0  # share of the session inside the period
            for m, _, n, i, r, w, o, _ in chat:
                models[m][0] += n * f; models[m][1] += (i + r + w + o) * f; models[m][2] += sum(row_cost(m, i, r, w, o, ttl)) * f
                tot["calls"] += n * f; tot["tokens"] += (i + r + w + o) * f
        total_s = sum(sc.values())
        if total_s <= 0:
            continue
        for k, v in sc.items():
            comp["sub" if src == "subagent" and sid is None else k] += v
        org = origin(src, title)
        where[org] += total_s
        top = max(sc.items(), key=lambda x: x[1])[0]
        platform, chat = chats.get(s, (src, NOCHAT))
        sess[s] = dict(title=title, src=src, origin=org, platform=platform, chat=chat, calls=n_calls, cost=total_s, top=top, started=started, comp=dict(sc),
                       turns=turns, ctx1=ctx1, last=last[0], parent=parent if src == "subagent" else None, tokens=tot["tokens"] - tk0,
                       models=models_of(((x[0], *x[3:7]) for x in rows), ttl),
                       project=project_of((m[4] for m in msgs.get(s, ()) if m[4]), repos, prx, repo_root))
    # 5 min -> 1 h: rebuilds after pauses up to 1 h disappear, every write costs 2/1.25 = 1.6x. The other way round accordingly.
    if ttl <= 300:
        ttl_save, ttl_alt = tot["avoid"] - (tot["writes"] - tot["avoid"]) * 0.6, 3600
    else:
        ttl_save, ttl_alt = tot["writes"] * 0.375 - tot["extra"] * 0.625, 300
    return dict(comp=comp, where=where, models=models, hours=hours, hsteps=hsteps, hsess=hsess, sess=sess, steps=steps,
                uses=uses, ctx=dict(ctxc), sizes=sizes_acc, breaks=dict(breaks), heavy=sorted(heavy, reverse=True)[:10], total=sum(comp.values()), ttl=ttl, ttl_alt=ttl_alt, ttl_save=ttl_save,
                since=since, until=until, hourly=hourly, **tot)


# ---------- Projects ----------
def home_repos(home=None):
    """Git repos directly in the home folder or one level deeper (e.g. ~/projects/app): {relative path: name}."""
    home, out = Path.home() if home is None else Path(home), {}
    try:
        dirs = [d for d in home.iterdir() if not d.name.startswith(".") and d.is_dir()]
    except OSError:
        return out
    for d in dirs:
        try:
            if (d / ".git").exists():
                out[d.name] = d.name
            else:
                out.update({f"{d.name}/{x.name}": x.name for x in d.iterdir() if not x.name.startswith(".") and (x / ".git").exists()})
        except OSError:
            pass
    return out


def project_rx(home=None, hermes=None):
    """Paths in tool calls: Hermes' own folder, /opt/x and /srv/x, anything under ~ (as ~, $HOME or spelled out)."""
    home, hermes = str(Path.home() if home is None else home), str(HERMES if hermes is None else hermes)
    h = rf"(?:{re.escape(home)}|~|\$HOME)"
    herm = re.escape(hermes) + (f"|{h}{re.escape(hermes[len(home):])}" if hermes.startswith(home + "/") else "")
    return re.compile(rf"(?P<herm>(?:{herm})(?![\w.-])(?P<junk>/(?:cache|sandboxes)\b)?)|/(?:opt|srv)/(?P<opt>[\w.-]+)"
                      rf"|{h}/(?P<home>(?!\.)[\w.-]+(?:/(?!\.)[\w.-]+)?)")


def project_of(texts, repos, rx, repo_root=None):
    """Project of a session: its git root if Hermes knows it (terminal sessions only), otherwise the project whose path
    shows up most often in the tool calls. Hermes' own folder only counts if hardly anything else shows up, because
    almost every session touches skills or scripts there on the side."""
    if repo_root:
        return Path(repo_root).name
    n = Counter()
    for t in texts:
        for m in rx.finditer(t):
            if m.group("herm"):
                n[HERMES_PROJ] += not m.group("junk")
            elif m.group("opt"):
                n[m.group("opt")] += m.group("opt") not in OPT_SKIP
            else:
                p = m.group("home")
                name = repos.get(p) or repos.get(p.split("/")[0])
                if name:
                    n[name] += 1
    hermes = n.pop(HERMES_PROJ, 0)
    best = [x for x in n.most_common(1) if x[1]]
    # ponytail: majority of paths; a session spanning two projects counts fully for the more frequent one
    if best and best[0][1] >= (max(2, hermes / 4) if hermes else 1):
        return best[0][0]
    return HERMES_PROJ if hermes else None


# ---------- Labels and tips (display text, current language) ----------
def ranked(comp, group_skills=True):
    g = defaultdict(float)
    for k, v in comp.items():
        g["tool:skill_view" if group_skills and k.startswith("skill:") else k] += v
    return sorted(((k, v) for k, v in g.items() if v > 0), key=lambda x: -x[1])


def ttl_text(ttl):
    return tr("ttl.1h") if ttl > 300 else tr("ttl.5m")


def seg_label(name):
    """System prompt part or message injection: soul, base, ..., plugin:<name>."""
    if name.startswith("plugin:"):
        return tr("seg.plugin", name=name[7:])
    return tr("seg." + name) if "seg." + name in LOC["en"] else name


def origin_label(k):
    if k.startswith("cron:"):
        return tr("origin.cron", name=k[5:] or tr("untitled"))
    return tr("src." + k) if "src." + k in LOC["en"] else (k or "?").capitalize()


def proj_label(n):
    return {HERMES_PROJ: tr("proj.hermes"), NO_PROJ: tr("proj.none")}.get(n, n)


def label(k, ttl=300):
    """(name, explanation) of a component key."""
    if k in FIXED:
        return tr("comp." + k), tr("comp." + k + ".why", ttl=ttl_text(ttl))
    kind, _, name = k.partition(":")
    if kind == "tool":
        return tr("label.tool", name=name), tr("label.tool.why", name=name)
    if kind == "skill":
        return tr("label.skill", name=name), tr("label.skill.why")
    if kind == "inj":
        return seg_label(name), tr("label.inj.why")
    if kind == "sys":
        return (seg_label(name) if name.startswith("plugin:") else tr("label.sys", part=seg_label(name))), tr("label.sys.why")
    if kind == "task":
        return tr("label.task", name=name), tr("label.task.why")
    return k, ""


EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")   # Hermes' reasoning_effort steps


def fix(*args):
    """Copyable hermes command under a tip, aimed at the analysed profile."""
    h = source_home()
    words = ["hermes", *(["-p", h.name] if h != HERMES else []), *map(str, args)]
    return f'<code class="cmd fix">{html.escape(" ".join(words))}</code>'


def idle_sets(tools, sets, used):
    """Enabled toolsets none of whose tools ran, heaviest first: [(name, schema cost)]. sets: tool -> its toolsets."""
    cost, busy = defaultdict(float), set()
    for n, v in tools.items():
        for k in sets.get(n, ()):
            cost[k] += v
            if n in used:
                busy.add(k)
    return sorted(((k, v) for k, v in cost.items() if k not in busy), key=lambda x: -x[1])


def long_line(peaks):
    """Learned long-context line of one agent: where 3 of 4 fresh tasks end (peak context of a chat's first turn),
    rounded up to 10k, between LONG_CTX and 2x. LONG_CTX until there are 10 such tasks."""
    if len(peaks) < 10:
        return LONG_CTX
    p = sorted(peaks)[len(peaks) * 3 // 4]
    return min(max(-(-int(p) // 10_000) * 10_000, LONG_CTX), 2 * LONG_CTX)


def week_tight():
    """True while the weekly limit is at 80 % or heading for TIGHT: then every step counts and the line stays low."""
    for key, u, frac, *_ in windows(STATE["limits"], time.time()):
        if key == "seven_day":
            return u >= 80 or (week_forecast(u, frac) or 0) >= TIGHT
    return False


def long_lines(d):
    """[(agent, line in force, learned line)] of every agent in an analysis."""
    t = week_tight()
    return [(a, LONG_CTX if t else LONG.get(a, LONG_CTX), LONG.get(a, LONG_CTX)) for a, _, _ in d.get("parts") or [("", None, d)]]


def shared_tips(d):
    """Tips that read only the analysis, no agent config: on a merged analysis they cover every agent at once."""
    tot, out = d["total"] or 1, []
    p = lambda v: pct(v, tot)
    br = d["comp"].get("break", 0)
    if br > 0.05 * tot:
        out.append((br * 0.9, tr("tip.break"), tr("tip.break.text", p=p(br))))
    ls = long_lines(d)
    lng = sum(v for (_, _, x), (_, t, _) in zip(d.get("parts") or [("", None, d)], ls) for b, v in x["ctx"].items() if b >= t)
    if lng > 0.2 * tot:
        lo, hi = min(t for _, t, _ in ls), max(t for _, t, _ in ls)
        out.append((lng / 2, tr("tip.long"), tr("tip.long.text", p=p(lng), n=nf(lo) if lo == hi else f"{nf(lo)}–{nf(hi)}")))
    tl = sorted(((k, v) for k, v in d["comp"].items() if k.startswith("tool:")), key=lambda x: -x[1])
    if tl and tl[0][1] > 0.08 * tot:
        name = tl[0][0][5:]
        hint = tr("tip.tool." + name) if name in TOOL_HINTS else tr("tip.tool.any", name=html.escape(name))
        out.append((tl[0][1] / 3, tr("tip.tool", name=name), hint + " " + tr("tip.share", p=p(tl[0][1]))))
    return out


def tools_used(x):
    """Tool names an analysis ran; any loaded skill counts as skill_view."""
    u = x["uses"]
    return {k.split(":", 1)[1] for k in u if k.startswith("tool:")} | ({"skill_view"} if any(k.startswith("skill:") for k in u) else set())


def tips(d, snap, shared=True, also=frozenset()):
    """[(rough saving in $, title, html text)], biggest saving first; the overview shows the saving as % of the period."""
    tot, out = d["total"] or 1, shared_tips(d) if shared else []
    p = lambda v: pct(v, tot)
    save = d["ttl_save"]
    if save > 0.03 * tot:
        key = "tip.ttl1h" if d["ttl_alt"] > 300 else "tip.ttl5m"
        out.append((save, tr(key), tr(key + ".text", p=p(save))
                    + fix("config", "set", "prompt_caching.cache_ttl", "1h" if key == "tip.ttl1h" else "5m")))
    tools = snap.get("tools", {})
    used = tools_used(d) | also   # also = what ran in the last 30 days: a quiet day is no reason to turn a tool off
    st = sum(tools.values()) or 1
    unused = sorted(((n, d["comp"].get("schema", 0) * t / st) for n, t in tools.items() if n not in used), key=lambda x: -x[1])
    s = sum(v for _, v in unused)
    if s > 0.02 * tot:
        top = ", ".join(f"{html.escape(n)} ({p(v)})" for n, v in unused[:4])
        idle = [k for k, _ in idle_sets(tools, snap.get("sets") or {}, used)][:6]
        cmd = fix("tools", "disable", "--platform", snap.get("platform") or "cli", *idle) if idle else ""
        out.append((s, tr("tip.unused"), tr("tip.unused.text", n=len(unused), p=p(s), top=top) + cmd))
    think = d["comp"].get("think", 0)
    effort = config_value("agent", "reasoning_effort")
    if think > 0.15 * tot and effort in EFFORTS[3:]:
        out.append((think * 0.3, tr("tip.think"), tr("tip.think.text", p=p(think), effort=html.escape(effort),
                                                       highest=tr("tip.think.highest") if effort in ("xhigh", "max") else "")
                    + fix("config", "set", "agent.reasoning_effort", EFFORTS[EFFORTS.index(effort) - 1])))
    bg = d["comp"].get("task:background_review", 0)
    if bg > 0.05 * tot:  # agent.log shows the review reads the history with its own prompt, so without cache hits
        every = ((f"{a}.{b}", config_value(a, b) or "10") for a, b in (("skills", "creation_nudge_interval"), ("memory", "nudge_interval")))
        cmd = "".join(fix("config", "set", k, 2 * int(v)) for k, v in every if v.isdigit() and int(v))   # 0 = already off
        out.append((bg / 2, tr("tip.review"), tr("tip.review.text", p=p(bg)) + cmd))
    live = {j.get("name") or "" for j in cron_list()}   # finished one-shot jobs can't be run less often
    for org, v in sorted(d["where"].items(), key=lambda x: -x[1]):
        if org.startswith("cron:") and org[5:] in live and v > 0.05 * tot:
            out.append((v / 2, tr("tip.cron", name=org[5:] or tr("untitled")), tr("tip.cron.text", p=p(v))))
    mem = config_value("memory", "provider")
    for k, v in d["comp"].items():
        if k.startswith("inj:") and v > 0.03 * tot:
            plug = k[4:].removeprefix("plugin:") if k.startswith("inj:plugin:") else ""
            cmd = fix("plugins", "disable", plug) if plug and plug != mem else ""   # a memory provider isn't a plugin switch
            out.append((v, tr("tip.inj", name=seg_label(k[4:])), tr("tip.inj.text", p=p(v)) + cmd))
    return sorted(out, key=lambda x: -x[0])


def all_tips(d):
    """Tips of every agent in an analysis, each from its own prompt snapshot and with its own hermes -p command.
    Shared tips run once on the whole; the same own tip of several agents becomes one card with a line per agent.
    ponytail: an own tip's share text is of its own agent's cost, the "saves" figure is of the whole."""
    many, own = len(d["parts"]) > 1, {}
    month = {a: tools_used(x) for a, _, x in data_for("30")["parts"]}
    out = shared_tips(d) if many else []
    for a, h, x in d["parts"]:
        with at_home(h):
            for sv, t, txt in tips(x, load_snap(), shared=not many, also=month.get(a, set())):
                own.setdefault(t, []).append((sv, a, txt))
    for t, g in own.items():
        txt = "<br>".join(f"<strong>{html.escape(agent_label(a))}:</strong> {x}" for _, a, x in g) if many else g[0][2]
        out.append((sum(sv for sv, _, _ in g), t, txt))
    return sorted(out, key=lambda x: -x[0])


_LH, _LH_LOCK = {"key": None, "off": 0, "rows": []}, threading.Lock()


def limit_history(days=7, acct=None):
    """Rows of data/limits.jsonl newer than `days` for account `acct` (None = the primary one, ALL = every account; rows
    without "a" belong to the primary account). The file only grows, so it is parsed once and later calls read just the lines appended since
    (a page used to parse all of it up to four times). A shorter file or another first line means it was rewritten:
    start over."""
    prim = next((a["id"] for a in STATE["accts"] if a["primary"]), "")
    acct = prim if acct is None else acct
    cut, f = time.time() - days * 86400, DATA / "limits.jsonl"
    try:
        with _LH_LOCK, open(f, "rb") as fh:
            st, head = os.fstat(fh.fileno()), fh.read(64)
            key = (str(f), st.st_ino, head)
            if key != _LH["key"] or st.st_size < _LH["off"]:
                _LH.update(key=key, off=0, rows=[])
            fh.seek(_LH["off"])
            new = fh.read()
            end = new.rfind(b"\n") + 1   # a half-written last line waits for the next call
            for line in new[:end].splitlines():
                try:
                    _LH["rows"].append(json.loads(line))
                except ValueError:
                    pass
            _LH["off"] += end
            return [h for h in _LH["rows"] if h["t"] > cut and (acct == ALL or h.get("a", prim) == acct)]
    except OSError:
        return []


# ---------- Background: limits + snapshot ----------
LIMITS_PY = r'''
import json, time, urllib.request
def usage(tok):
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + (tok or ""), "Accept": "application/json",
        "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
try:   # every OAuth account of Hermes' credential pool, each with its own token
    from agent.credential_pool import load_pool, get_pool_strategy
    try:   # an account an outside controller wants used first (pool_preference.json, not in every Hermes)
        from agent.credential_pool import preferred_entry_id
        preferred = preferred_entry_id("anthropic")
    except Exception:
        preferred = None
    pool = load_pool("anthropic")
    es = sorted((e for e in pool.entries() if e.auth_type == "oauth"), key=lambda e: e.priority)
except Exception:
    es = []
if not es:
    try:
        from agent.anthropic_credentials import resolve_anthropic_token
    except ImportError:  # Hermes before October 2026
        from agent.anthropic_adapter import resolve_anthropic_token
    print(json.dumps({"strategy": "", "accounts": [{"id": "", "label": "", "priority": 0, "requests": 0, "status": None,
                                                    "avail": True, "usage": usage(resolve_anthropic_token())}]}))
    raise SystemExit
try:
    from hermes_cli.config import load_config_readonly
    m = load_config_readonly().get("model")
    model = m.get("default") if isinstance(m, dict) else m
except Exception:
    model = None
try:
    ok = {e.id for e in pool._available_entries(clear_expired=False, refresh=False, model=model)[0]}
except Exception:   # private API moved: treat all as available
    ok = {e.id for e in es}
out = []
for e in es:
    a = {"id": e.id, "label": e.label, "priority": e.priority, "requests": e.request_count, "status": e.last_status,
         "avail": e.id in ok}
    try:
        if e.expires_at_ms and e.expires_at_ms < (time.time() + 120) * 1000:   # idle token ran out: Hermes' locked refresh
            e = pool.try_refresh_matching(credential_id=e.id)
            if e is None:
                raise RuntimeError("refresh failed")
        a["usage"] = usage(e.access_token)
    except Exception as x:   # never the message: it could quote a token
        a["error"] = "HTTP %s" % x.code if hasattr(x, "code") else type(x).__name__
    out.append(a)
print(json.dumps({"strategy": get_pool_strategy("anthropic"), "preferred": preferred, "accounts": out}))
'''
STATE = {"limits": None, "at": 0.0, "ok": 0.0, "accts": []}  # at = last attempt, ok = last successful fetch (primary account)
LIMITS_LOCK = threading.Lock()


def pick(accts, prev, strategy, now, preferred=None):
    """Which pooled account is in use. Hermes keeps its current pick in memory only, so: `use` = 5 h %, week % or request
    count rose since the previous reading (in use for 30 min after), `next` = the account Hermes would pick next (the
    available `preferred` one first, as Hermes' select() does, else by strategy), `primary` = the one to show: preferred,
    else in use and next, else the most recently risen. Only the primary and accounts that rose in this very reading
    keep `use`, so after a switch the old account drops its badge instead of showing it for 30 min.
    prev/returned state: {id: {"5h", "7d", "n", "used_at"}} (data/accounts.json)."""
    state, out = {}, []
    for a in accts:
        u, p = a.get("usage") or {}, prev.get(a["id"]) or {}
        cur = {"5h": (u.get("five_hour") or {}).get("utilization"), "7d": (u.get("seven_day") or {}).get("utilization"),
               "n": a.get("requests")}
        used = p.get("used_at") or 0
        if any(v is not None and p.get(k) is not None and v > p[k] for k, v in cur.items()):
            used = now
        state[a["id"]] = {**{k: p.get(k) if v is None else v for k, v in cur.items()}, "used_at": used}
        out.append({"id": a["id"], "use": bool(used) and now - used < 1800, "next": False, "primary": False, "_t": used})
    av = sorted((a.get("priority") or 0, i) for i, a in enumerate(accts) if a.get("avail"))
    if strategy == "least_used":
        av.sort(key=lambda x: accts[x[1]].get("requests") or 0)   # stable: ties by priority
    pref = next((i for i, a in enumerate(accts) if a.get("avail") and a["id"] == preferred), None) if preferred else None
    if pref is not None:
        out[pref]["next"] = True
    elif av and strategy != "random":   # random: no way to tell
        out[av[0][1]]["next"] = True
    busy = sorted((o for o in out if o["use"]), key=lambda o: -o["_t"])
    nxt = [o for o in out if o["next"]]
    top = out[pref] if pref is not None else next((o for o in busy if o["next"]), None) or (busy or nxt or out)[0]
    for o in out:
        o["primary"] = o is top
        o["use"] = o["use"] and (o is top or o["_t"] == now)
        del o["_t"]
    return out, state


def refresh_limits(max_age):
    """Fetch the subscription limits of every pooled account through Hermes' OAuth login (tokens stay in the Hermes
    process), find the one in use and log them."""
    with LIMITS_LOCK:
        if time.time() - STATE["at"] < max_age:
            return
        r = None
        try:
            r = subprocess.run([str(HERMES_PY), "-c", LIMITS_PY], cwd=HERMES / "hermes-agent",
                               capture_output=True, text=True, timeout=40)
            data = json.loads(r.stdout)
            accts = data["accounts"]
            if not accts:
                raise ValueError("no accounts")
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            print("limits: not available:", (r.stderr.strip().splitlines() or [""])[-1] if r else "", flush=True)
            STATE["at"] = time.time() - max(max_age - 60, 0)  # try again in a minute
            return
        now, f = time.time(), DATA / "accounts.json"
        try:
            prev = json.loads(f.read_text())
        except (OSError, ValueError):
            prev = {}
        flags, keep = pick(accts, prev, data.get("strategy") or "", now, data.get("preferred"))
        DATA.mkdir(parents=True, exist_ok=True)
        f.with_suffix(".tmp").write_text(json.dumps(keep))
        f.with_suffix(".tmp").replace(f)
        STATE["accts"] = [{**o, "label": a.get("label") or "", "avail": a.get("avail"), "status": a.get("status"),
                           "limits": a.get("usage"), "error": a.get("error")} for o, a in zip(flags, accts)]
        top = next(a for a in STATE["accts"] if a["primary"])
        if top["limits"]:
            STATE.update(limits=top["limits"], ok=now)
        else:
            print("limits: not available:", top["error"], flush=True)
        STATE["at"] = now
        with open(DATA / "limits.jsonl", "a") as fh:
            for a in STATE["accts"]:
                if not a["limits"]:
                    continue
                row = {"t": round(now), **({"a": a["id"]} if a["id"] else {})}
                for k in ("five_hour", "seven_day"):
                    row[k] = (a["limits"].get(k) or {}).get("utilization")
                x = a["limits"].get("extra_usage") or {}
                if x.get("is_enabled") and x.get("used_credits") is not None:
                    row["extra"] = round(x["used_credits"] / 10 ** (x.get("decimal_places") or 0), 2)
                fh.write(json.dumps(row) + "\n")


def fresh_limits():
    """Page loads: with an earlier reading answer at once and fetch in the background (the fetch takes ~2 s).
    Only the very first reading after a start is waited for."""
    if STATE["limits"] is None:
        refresh_limits(120)
    elif time.time() - STATE["at"] >= 120 and not LIMITS_LOCK.locked():
        threading.Thread(target=refresh_limits, args=(120,), daemon=True).start()


# ---------- Limits: who used them ----------
CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
_CC = {}      # transcript -> (mtime, [(time, $)])
RATE = {}     # limit_split over 30 days: k = weekly-limit % per API dollar
FC = {}       # weekly rhythm: prof = 168 hourly shares of a limit week, method weighted/linear, backtest errors


def cc_calls(since):
    """[(time, $)] of Claude Code's API calls since `since`: this machine's transcripts (deduplicated: Claude Code
    writes one line per content block, all with the same message id and usage) plus the hours other machines reported."""
    out = []
    for p in CLAUDE.rglob("*.jsonl"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m < since:
            continue
        if _CC.get(p, (None,))[0] != m:
            seen, rows = set(), []
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        x = json.loads(line)
                        msg, u = x["message"], x["message"]["usage"]
                        key, model = (msg.get("id"), x.get("requestId")), str(msg.get("model") or "")
                        t = datetime.fromisoformat(x["timestamp"].replace("Z", "+00:00")).timestamp()
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                    if x.get("type") != "assistant" or key in seen or not model.startswith("claude"):
                        continue
                    seen.add(key)
                    w1h = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
                    pr = price(model)
                    rows.append((t, (u.get("input_tokens") or 0) * pr[0] + (u.get("cache_read_input_tokens") or 0) * pr[3]
                                 + ((u.get("cache_creation_input_tokens") or 0) - w1h) * pr[1] + w1h * pr[2]
                                 + (u.get("output_tokens") or 0) * pr[4]))
            _CC[p] = (m, rows)
        out += [r for r in _CC[p][1] if r[0] >= since]
    return out + [(h, c) for x in ingest_hosts().values() for h, c in x.get("hours", []) if h >= since]


HOST_RX = r"[A-Za-z0-9 ._-]{1,40}"


def parse_ingest(raw):
    """(host, since, rows) of a report from tools/cc-report.py, None when it is malformed. rows: [hour, model, input,
    cache_write, cache_read, output(, of the writes 1-hour cache)] with non-negative numbers."""
    try:
        body = json.loads(raw)
        host, rows, since = body["host"], body["hours"], body.get("since")
        assert isinstance(host, str) and re.fullmatch(HOST_RX, host) and isinstance(rows, list)
        assert since is None or isinstance(since, (int, float))
        out = []
        for r in rows:
            assert isinstance(r, list) and len(r) in (6, 7) and isinstance(r[1], str)
            nums = [r[0], *r[2:]]
            assert all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e13 for v in nums)
            out.append([int(r[0]) - int(r[0]) % 3600, r[1][:80], *nums[1:], *([0] if len(r) == 6 else [])])
        return host, since, out
    except (ValueError, KeyError, TypeError, AssertionError, AttributeError):
        return None


def save_ingest(host, since, rows, now=None):
    """Prices a report and stores it per host (data/ingest/<host>.json): hours from `since` on are replaced, older ones
    stay for 35 days, so sending the same report twice changes nothing."""
    now = now or time.time()
    cost = defaultdict(float)
    for h, model, i, w, r, o, w1 in rows:
        pr, w1 = price(model), min(w1, w)
        cost[h] += i * pr[0] + (w - w1) * pr[1] + w1 * pr[2] + r * pr[3] + o * pr[4]
    first = since if since is not None else min(cost, default=now)
    f = DATA / "ingest" / f"{host}.json"
    keep = {int(h): c for h, c in (read_json(f) or {}).get("hours", []) if now - 35 * 86400 < h < first}
    keep.update(cost)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"host": host, "at": round(now), "hours": sorted([h, round(c, 6)] for h, c in keep.items())}))


def ingest_hosts():
    """{host: {at, hours: [[hour, $]]}} of the other machines that report their Claude Code usage."""
    d = DATA / "ingest"
    return {x["host"]: x for x in (read_json(f) for f in sorted(d.glob("*.json"))) if x} if d.is_dir() else {}


def limit_split(hist, hours, cc, since, min_cost=0.25):
    """Who used the weekly limit since `since`, measured per clock hour: the growth between the 10-minute readings
    goes to Hermes and Claude Code (split by their API cost in that hour) or, in hours where neither ran, to "other"
    (Claude app, other devices). k = limit % per API dollar over the hours with Hermes or Claude Code. None without
    readings."""
    # ponytail: hour-level attribution; other usage in an hour where Hermes also ran counts as Hermes
    pts = [h for h in hist if h["t"] >= since and h.get("seven_day") is not None]
    if len(pts) < 2:
        return None
    ccost = defaultdict(float)
    for t, c in cc:
        ccost[t - t % 3600] += c
    out, seen = {"hermes": 0.0, "cc": 0.0, "rest": 0.0}, set()
    for a, b in zip(pts, pts[1:]):
        du, h = b["seven_day"] - a["seven_day"], a["t"] - a["t"] % 3600
        if du < 0 or b["t"] - a["t"] > 1800:   # weekly reset, or a gap without readings
            continue
        seen.add(h)
        hc, c = sum(hours.get(h, {}).values()), ccost.get(h, 0.0)
        if hc + c < min_cost:
            out["rest"] += du
        else:
            out["hermes"] += du * hc / (hc + c)
            out["cc"] += du * c / (hc + c)
    cost = sum(sum(hours.get(h, {}).values()) + ccost.get(h, 0.0) for h in seen)
    used = out["hermes"] + out["cc"]
    out.update(start=pts[0]["t"], before=pts[0]["seven_day"], hours=len(seen),
               gap=pts[-1]["seven_day"] - pts[0]["seven_day"] - used - out["rest"],
               k=used / cost if cost >= 5 and used >= 5 else None)  # 1 % steps: below that it is mostly rounding
    return out


def extra_months(hist):
    """[[start, used]] per billing month of the extra credits, from the readings: a month ends where the counter drops
    (calendar month or any other reset day, whatever the account does)."""
    out, prev = [], None
    for h in hist:
        x = h.get("extra")
        if x is None:
            continue
        if prev is None or x < prev:
            out.append([h["t"], x])
        out[-1][1], prev = max(out[-1][1], x), x
    return out


def overrun_usd(fc):
    """What the part of the week forecast above 100 % would cost in extra credits (billed at API prices), or None."""
    return (fc - 100) / RATE["k"] if fc and fc > 100 and RATE.get("k") else None


# ---------- Limits: forecast + alerts ----------
WINDOWS = (("five_hour", 5 * 3600), ("seven_day", 7 * 86400), ("seven_day_opus", 7 * 86400), ("seven_day_sonnet", 7 * 86400))


def windows(L, now):
    """[(key, utilization %, share of the window elapsed or None, reset time or None, length)]."""
    out = []
    for key, length in WINDOWS:
        w = (L or {}).get(key) or {}
        if w.get("utilization") is None:
            continue
        try:
            reset = datetime.fromisoformat(w["resets_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            reset = None
        frac = min(max(1 - (reset - now) / length, 0.0), 1.0) if reset else None
        out.append((key, float(w["utilization"]), frac, reset, length))
    return out


def week_forecast(u, frac):
    """Utilization at the reset. Linear from the pace since the window started, or, when the backtest found the weekly
    rhythm more accurate (FC), divided by the share of a usual week that has passed by now. None in the first half day."""
    if not frac or frac < 1 / 14:
        return None
    p = FC.get("prof") if FC.get("method") == "weighted" else None
    if p:
        x = min(frac, 1.0) * 168
        i = int(x)
        a = sum(p[:i]) + (p[i] * (x - i) if i < 168 else 0.0)
        if a >= 0.1:
            return u / a
    return u / frac


def rhythm(cost, ws, since):
    """Weekly rhythm from hourly API cost {hour: $}: (profile = mean share of each of the 168 hours of a limit week,
    {lin, wtd} = mean absolute % error of the final week cost predicted every hour from day 2 to day 6, each week
    against the profile of the others). Uses up to 4 complete limit weeks before ws that lie after since; needs 3."""
    weeks = [w for w in ([cost.get(s + 3600 * h, 0.0) for h in range(168)] for s in
                         (ws - 7 * 86400 * i for i in range(1, 5)) if s >= since) if sum(w) > 0][:4]
    if len(weeks) < 3:
        return None, None
    norm = [[c / sum(w) for c in w] for w in weeks]
    mean = lambda rows: [sum(r[h] for r in rows) / len(rows) for h in range(168)]
    err = {"lin": [], "wtd": []}
    for i, w in enumerate(weeks):
        p = mean([x for j, x in enumerate(norm) if j != i])
        for t in range(24, 145):
            used, a = sum(w[:t]), sum(p[:t])
            lin = used * 168 / t
            err["lin"].append(abs(lin - sum(w)) / sum(w))
            err["wtd"].append(abs((used / a if a >= 0.1 else lin) - sum(w)) / sum(w))
    return mean(norm), {k: sum(v) / len(v) * 100 for k, v in err.items()}


def rate(hist, key, u, start, now):
    """Usage in % per second over the last hour, only from points of this window; None without 10 min of data."""
    pts = [h for h in hist if h["t"] >= max(start, now - 3600) and h.get(key) is not None]
    if not pts or now - pts[0]["t"] < 600:
        return None
    return max((u - pts[0][key]) / (now - pts[0]["t"]), 0.0)


def alerts(L, hist, sent, now, s=None):
    """Due alerts [(tag, title, text)] and the new state. A tag stands for one window; the caller stores it after a
    successful send, so every alert comes at most once per window. s: settings (switches, 5-hour threshold)."""
    s = s or SET
    sent, out, full = {k: v for k, v in sent.items() if k == "extra_used" or k.startswith("extra@") or now - v < 8 * 86400}, [], []
    for key, u, frac, reset, length in windows(L, now):
        name = tr("win." + key)
        if u >= 100:
            full.append(name)
            if length <= 5 * 3600 and reset:
                sent["full:" + key] = reset   # remembered for "free again"
        if not reset or u >= 100:
            continue
        if key == "seven_day" and s["alert_steps"]:
            step = next((x for x in (90, 80) if u >= x), None)   # only the highest step reached, once per week
            if step and not any(f"{key}@{x}:{reset:.0f}" in sent for x in (80, 90) if x >= step):
                out.append((f"{key}@{step}:{reset:.0f}", tr("alert.step", name=name, u=pc(u)),
                            tr("alert.step.text", reset=when(reset, "daytime"), pct=pc((100 - u) / max((reset - now) / 86400, 1)))))
        tag = f"{key}:{reset:.0f}"
        if tag in sent:
            continue
        if length <= 5 * 3600:
            r = rate(hist, key, u, reset - length, now)
            fa = now + (100 - u) / r if r else None
            if s["alert_five"] and fa and fa - now < s["five_min"] * 60 and fa < reset:
                out.append((tag, tr("alert.five", name=name), tr("alert.five.text", u=pc(u), full=hm(fa), reset=hm(reset))))
        else:
            fc = week_forecast(u, frac)
            if s["alert_week"] and fc and fc > 100 and frac >= 1 / 7:
                start = reset - length
                out.append((tag, tr("alert.week", name=name),
                            tr("alert.week.text", u=pc(u), frac=pc(frac * 100), full=when(start + (now - start) * 100 / u, "daytime"),
                               reset=when(reset, "daytime"))))
    for k in [k for k in sent if k.startswith("full:")]:
        tag = f"free:{k[5:]}:{sent[k]:.0f}"
        if s["alert_free"] and 0 <= now - sent[k] < 3600 and tag not in sent:   # within the hour after the reset
            out.append((tag, tr("alert.free", name=tr("win." + k[5:])), tr("alert.free.text", reset=hm(sent[k]))))
    x = (L or {}).get("extra_usage") or {}
    used = x.get("used_credits")
    if x.get("is_enabled") and used is not None:
        prev, dp = sent.get("extra_used"), 10 ** (x.get("decimal_places") or 0)
        if prev is not None and used < prev:   # a new billing month: the steps count again
            sent = {k: v for k, v in sent.items() if not k.startswith("extra@")}
        steps = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", s["extra_steps"])]
        step = max((v for v in steps if used / dp >= v), default=None)   # only the highest step reached, once per month
        if step is not None and not any(f"extra@{v:g}" in sent for v in steps if v >= step):
            out.append((f"extra@{step:g}", tr("alert.extra.step", step=nf(step, 0 if step == int(step) else 2), cur=x.get("currency") or ""),
                        tr("alert.extra.step.text", used=nf(used / dp, 2), limit=nf((x.get("monthly_limit") or 0) / dp, 2), cur=x.get("currency") or "")))
        reset = next((w[3] for w in windows(L, now) if w[3]), 0)  # 5-hour window first, otherwise the week
        tag = f"extra:{reset:.0f}"
        if s["alert_extra"] and prev is not None and used > prev and tag not in sent:  # the state stays old until the alert went out
            out.append((tag, tr("alert.extra"), tr("alert.extra.text", used=nf(used / dp, 2),
                                                     limit=nf((x.get("monthly_limit") or 0) / dp, 2), cur=x.get("currency") or "",
                                                     full=tr("alert.full", names=", ".join(full)) + " " if full else "")))
        else:
            sent["extra_used"] = used
    return out, sent


def hermes_env(prefix, home=HERMES):
    """Variables starting with prefix from a Hermes home's .env."""
    env = {}
    try:
        for line in (home / ".env").read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip().startswith(prefix):
                env[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return env


def tg_target():
    """(bot token, chat, topic) of the Telegram bot chosen in the settings: the bot and home channel of that Hermes home."""
    if SET["tg_bot"] == "off":
        return None
    env = hermes_env("TELEGRAM_", HERMES / "profiles" / SET["tg_bot"] if SET["tg_bot"] in profiles() else HERMES)
    chat = (env.get("TELEGRAM_HOME_CHANNEL") or "").split(",")[0].strip()
    return (env["TELEGRAM_BOT_TOKEN"], chat, SET["tg_thread"]) if env.get("TELEGRAM_BOT_TOKEN") and chat else None


def tg_call(method, **body):
    """(ok, result or error text) of one Bot API call through the chosen bot. Errors never echo the URL (it holds the token)."""
    t = tg_target()
    if not t:
        return False, tr("set.tg.off")
    req = urllib.request.Request(f"https://api.telegram.org/bot{t[0]}/{method}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return True, json.loads(r.read()).get("result")
    except urllib.error.HTTPError as err:
        try:
            return False, f'{err.code} {json.loads(err.read()).get("description", "")}'
        except ValueError:
            return False, str(err.code)
    except (OSError, ValueError) as err:
        return False, str(getattr(err, "reason", "")) or type(err).__name__


def tg_open(t, path="/"):
    """Inline button that opens the dashboard: as the Mini App in a private chat, as a link in groups."""
    url = SET["url"] + path
    if not url.startswith("https://"):
        return {}
    btn = {"web_app": {"url": url}} if not t[1].startswith("-") else {"url": url}
    return {"reply_markup": {"inline_keyboard": [[{"text": tr("tg.open"), **btn}]]}}


def push_on():
    return bool(tg_target()) if SET["push"] == "telegram" else bool(bark_target() or ntfy_target())


BARK_LEVEL, BARK_MAX = {4: "timeSensitive", 3: "active"}, 2500   # other priorities: passive; body limit from the APNs payload


def hermes_bark(home=HERMES):
    """(url, device key, user, password) of the Bark server Hermes itself pushes to (secrets/bark.env), otherwise None."""
    try:
        env = dict(x.strip().split("=", 1) for x in (home / "secrets" / "bark.env").read_text().splitlines() if "=" in x)
    except (OSError, ValueError):
        return None
    env = {k.strip(): v.strip().strip("'\"") for k, v in env.items()}
    if not (env.get("BARK_URL") and env.get("BARK_KEY")):
        return None
    return env["BARK_URL"].rstrip("/"), env["BARK_KEY"], env.get("BARK_USER", ""), env.get("BARK_PASS", "")


def bark_target():
    """(url, device key, user, password) of the Bark server to push to: the own one, Hermes' one, or None."""
    if SET["push"] == "hermes":
        return hermes_bark()
    if SET["push"] == "bark" and SET["bark_url"] and SET["bark_key"]:
        return SET["bark_url"], SET["bark_key"], SET["bark_user"], SET["bark_pass"]
    return None


def send_bark(t, title, text, prio, path=""):
    """(sent, the server's reply) of one Bark push; sent only when Bark answers code 200."""
    url, key, user, pw = t
    body = {"device_key": key, "title": title, "body": text[:BARK_MAX] or " ", "level": BARK_LEVEL.get(prio, "passive"),
            "group": "usagecast", **({"url": SET["url"] + path} if SET["url"] else {})}
    headers = {"Content-Type": "application/json; charset=utf-8",
               **({"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()} if user or pw else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/push", json.dumps(body).encode(), headers), timeout=15) as r:
            raw = r.read(400).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as err:
        return False, f"{err.code} {err.read(400).decode('utf-8', 'replace').strip()}"
    except OSError as err:
        return False, str(getattr(err, "reason", "")) or type(err).__name__
    try:
        ok = json.loads(raw).get("code") == 200
    except (ValueError, AttributeError):
        ok = False
    return ok, f"{200 if ok else 502} {raw}"


def hermes_ntfy():
    """(server, topic, token) of the channel Hermes itself sends to (NTFY_* in ~/.hermes/.env), otherwise None."""
    env = hermes_env("NTFY_")
    if not env.get("NTFY_HOME_CHANNEL"):
        return None
    return (env.get("NTFY_SERVER_URL") or "https://ntfy.sh").rstrip("/"), env["NTFY_HOME_CHANNEL"], env.get("NTFY_TOKEN")


def ntfy_target():
    """(server, topic, token) of the push target chosen in the settings, None while push is off."""
    if SET["push"] == "hermes":
        return hermes_ntfy()
    if SET["push"] == "own" and SET["ntfy_topic"]:
        return SET["ntfy_server"], SET["ntfy_topic"], SET["ntfy_token"] or None
    return None


def send_push(title, text, prio=4, path=""):
    """(sent, the server's reply). Priority 4 = high (limit almost full, week won't last), 3 = normal, 2 = low."""
    if SET["push"] == "telegram":
        t = tg_target()
        if not t:
            return False, tr("set.tg.off")
        ok, r = tg_call("sendMessage", chat_id=t[1], **({"message_thread_id": int(t[2])} if t[2] else {}), parse_mode="HTML",
                        text=f"<b>{html.escape(title)}</b>\n{html.escape(text)}", disable_notification=prio <= 2, **tg_open(t, path))
        return ok, "200 OK" if ok else r
    b = bark_target()
    if b:
        ok, r = send_bark(b, title, text, prio, path)
        if ok or SET["push"] != "hermes" or not hermes_ntfy():   # hermes: Bark did not take it, Hermes' ntfy does
            return ok, r
    t = ntfy_target()
    if not t:
        return False, tr("set.push.state.off")
    server, topic, token = t
    body = {"topic": topic, "title": title, "message": text, "priority": prio, **({"click": SET["url"] + path} if SET["url"] else {})}
    headers = {"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(server, json.dumps(body).encode(), headers), timeout=15) as r:
            return True, f"{r.status} {r.read(400).decode('utf-8', 'replace').strip()}"
    except urllib.error.HTTPError as err:
        return False, f"{err.code} {err.read(400).decode('utf-8', 'replace').strip()}"
    except OSError as err:
        return False, str(err)


def notify(title, text, prio=4, path=""):
    ok, reply = send_push(title, text, prio, path)
    if not ok:
        print("push:", reply, flush=True)
    return ok


def digest(now, sent, L, get=None, s=None):
    """Sunday-evening summary of the limit week, once per ISO week: (tag, title, text) or None. The comparison is
    against the same stretch of time one week earlier."""
    s, get = s or SET, get or data_for
    dt = datetime.fromtimestamp(now)
    tag = "digest:" + dt.strftime("%G-%V")
    if not s["alert_digest"] or dt.weekday() != 6 or dt.hour < 18 or tag in sent:
        return None
    w, d30 = get("w"), get("30")
    prev = sum(sum(v.values()) for h, v in d30["hours"].items() if w["since"] - 7 * 86400 <= h < now - 7 * 86400)
    delta = tr("alert.digest.delta", d=signed(w["total"], prev)) if prev else ""
    top, wk = ranked(w["comp"]), next((x for x in windows(L, now) if x[0] == "seven_day"), None)
    return tag, tr("alert.digest"), tr("alert.digest.text", u=pc(wk[1]) if wk else "–", reset=when(wk[3], "daytime") if wk and wk[3] else "–",
                                       cost=cash(w["total"]), delta=delta, top=label(top[0][0], w["ttl"])[0] if top else "–")


NOT_CHAT = ("cron", "subagent")   # sources that are no conversation with a person


def watch(d30, now, sent, k, s=None):
    """Chat watch and spike alerts [(tag, title, text, path)] for runs with a step in the last 20 minutes: a long chat
    whose replies have become expensive, and a cron run or a single reply far above its usual cost. k: weekly % per
    API dollar (limit_split); without it both stay silent."""
    s = s or SET
    if not k:
        return []
    med = lambda v: statistics.median(v) if v else 0.0
    chats = [x for x in d30["sess"].values() if x["src"] not in NOT_CHAT]
    usual = med([t[3] for x in chats for t in x["turns"]])
    fresh = med([t[3] for x in chats if x["started"] >= d30["since"] for t in x["turns"][:3]])
    out = []
    for sid, x in d30["sess"].items():
        if now - x["last"] > 1200:
            continue
        title, path = x["title"] or tr("untitled"), "/s/" + quote(sid)
        if x["src"] == "cron":
            runs = [y["cost"] for y in d30["sess"].values() if y["origin"] == x["origin"] and y["started"] < x["started"]]
            m = med(runs) if len(runs) >= 3 else 0.0
            if s["alert_spike"] and x["cost"] * k >= max(3 * m * k, s["spike_pct"]) and f"spike:{sid}" not in sent:
                text = tr("alert.spike.cron", name=x["origin"][5:] or tr("untitled"), x=pct(x["cost"] * k, 100))
                out.append((f"spike:{sid}", tr("alert.spike"), text + (" " + tr("alert.spike.usual", m=pct(m * k, 100)) if m else ""), path))
            continue
        if x["src"] in NOT_CHAT or not x["turns"]:
            continue
        t0, _, n, c = x["turns"][-1]
        if s["alert_spike"] and c * k >= max(3 * usual * k, s["spike_pct"]) and f"spike:{sid}:{t0:.0f}" not in sent:
            out.append((f"spike:{sid}:{t0:.0f}", tr("alert.spike"), tr("alert.spike.turn", title=title, n=n, x=pct(c * k, 100)), path))
        done = x["turns"][:-1][-3:]   # ponytail: the newest turn may still run, it counts once the next message came
        per = sum(t[3] for t in done) / 3 * k if len(done) == 3 else 0.0
        if s["alert_chat"] and per >= s["chat_pct"] and f"chat:{sid}" not in sent:
            out.append((f"chat:{sid}", tr("alert.chat"), tr("alert.chat.text", title=title, x=pct(per, 100), y=pct(fresh * k, 100)), path))
    return out


PRIO = {"free": 3, "seven_day@80": 3, "digest": 2, "chat": 3}   # ntfy priority by alert kind, everything else 4 (high)


def alerts_pool(accts, sent, now, hist=limit_history, s=None):
    """alerts() for the primary account alone, or once per account with limits when several are pooled: tags get the
    prefix "<id>:" (own slice of `sent`), titles "<label>: ". Same (msgs, new) as alerts()."""
    if len(accts) < 2:
        return alerts(STATE["limits"], hist(), sent, now, s)
    pre = {a["id"]: a["id"] + ":" for a in accts}
    new = {k: v for k, v in sent.items() if now - v < 8 * 86400 and not k.startswith(tuple(pre.values()))}
    out = []
    for a in accts:
        if not a["limits"]:
            continue
        p = pre[a["id"]]
        m, st = alerts(a["limits"], hist(acct=a["id"]), {k[len(p):]: v for k, v in sent.items() if k.startswith(p)}, now, s)
        new.update({p + k: v for k, v in st.items()})
        out += [(p + t, tr("acal.of", label=a["label"] or a["id"], text=ti), *rest) for t, ti, *rest in m]
    return out, new


def check_alerts():
    f = DATA / "alerts.json"
    try:
        sent = json.loads(f.read_text())
    except (OSError, ValueError):
        sent = {}
    if not STATE["limits"] or quiet_now(time.time()):  # in quiet hours due alerts wait, nothing is marked as sent
        return
    now = time.time()
    msgs, new = alerts_pool(STATE["accts"], sent, now)
    msgs += [x for x in [digest(now, sent, STATE["limits"])] if x]
    if RATE.get("k") and (SET["alert_chat"] or SET["alert_spike"]):
        msgs += watch(data_for("30"), now, sent, RATE["k"])
    for tag, title, text, *path in msgs:
        kind = next((tag[len(a["id"]) + 1:] for a in STATE["accts"] if a["id"] and tag.startswith(a["id"] + ":")), tag)
        if notify(title, text, PRIO.get(kind.split(":")[0], 4), *path):
            new[tag] = time.time()
    if new != sent:
        DATA.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(new))


def snap_path():
    h = source_home()
    return DATA / ("snapshot.json" if h == HERMES else f"snapshot-{h.name}.json")


def load_snap():
    try:
        return json.loads(snap_path().read_text())
    except (OSError, ValueError):
        return {}


STATUS_URL = "https://status.claude.com"
STATUS = {}


def refresh_bots(max_age=86400):
    """Bot names for "Bots & chats": Telegram getMe, Discord users/@me, Slack auth.test with each home's token, at most
    once a day into data/bots.json. A failure keeps the last name. Tokens stay in here: never logged, never shown."""
    old = read_json(DATA / "bots.json") or {}
    if time.time() - old.get("at", 0) < max_age:
        return
    names = old.get("names") or {}
    for a, h in homes():
        env = hermes_env("", h)
        for plat, var, url, auth, pick in (
                ("telegram", "TELEGRAM_BOT_TOKEN", "https://api.telegram.org/bot{t}/getMe", "", lambda j: j["result"]["first_name"]),
                ("discord", "DISCORD_BOT_TOKEN", "https://discord.com/api/v10/users/@me", "Bot {t}", lambda j: j.get("global_name") or j["username"]),
                ("slack", "SLACK_BOT_TOKEN", "https://slack.com/api/auth.test", "Bearer {t}", lambda j: j["user"])):
            t = env.get(var)
            if not t:
                continue
            hdr = {"User-Agent": f"DiscordBot (usagecast, {VERSION})", **({"Authorization": auth.format(t=t)} if auth else {})}
            try:
                req = urllib.request.Request(url.format(t=t), b"" if plat == "slack" else None, hdr)
                with urllib.request.urlopen(req, timeout=10) as r:
                    names.setdefault(a, {})[plat] = str(pick(json.loads(r.read())))[:40]
            except Exception:  # noqa: BLE001 - offline, bad token, odd reply: keep the old name; the error text may hold the URL
                print("bots:", plat, agent_label(a), "name not fetched", flush=True)
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "bots.json").write_text(json.dumps({"at": round(time.time()), "names": names}))


def refresh_status():
    """Claude's public status page; an incident shows up as a line under the limits."""
    try:
        with urllib.request.urlopen(STATUS_URL + "/api/v2/status.json", timeout=15) as r:
            st = json.loads(r.read())["status"]
        STATUS.clear(); STATUS.update(st)
    except (OSError, ValueError, KeyError):
        STATUS.clear()


def ensure_snap():
    """Measures prompt and tools again for every analysed Hermes home whose snapshot is missing or a day old."""
    for _, h in homes():
        with at_home(h):
            snap = snap_path()
        if not snap.exists() or time.time() - snap.stat().st_mtime > 86400:
            try:
                subprocess.run([str(HERMES_PY), str(Path(__file__).resolve()), "--snapshot"], cwd=HERMES / "hermes-agent",
                               capture_output=True, timeout=600, env={**os.environ, "USAGECAST_HOME": str(h)})
            except (OSError, subprocess.TimeoutExpired) as e:
                print("snapshot:", e, flush=True)


def acct_name():
    """Label of the primary account when there are several, else "" (one account: nothing to name)."""
    a = next((a for a in STATE["accts"] if a["primary"]), None) if len(STATE["accts"]) > 1 else None
    return (a["label"] or a["id"][:8]) if a else ""


def tg_texts(wk, fh, fc, n, now, name=""):
    """Menu button text (short: it is a button; use first, a phone cuts the end) and pinned message; `name` = primary account when there are several."""
    menu = tr("tg.menu.text", w=round(wk[0]), f=round(fh[0]))
    pin = [tr("tg.pin.week", w=round(wk[0]), fc=round(fc) if fc is not None else "–"),
           tr("tg.pin.five", f=round(fh[0]), at=hm(fh[2]) if fh[2] else "–"), tr("live.n", n=n), tr("tg.pin.at", at=hm(now))]
    if name:
        menu, pin = tr("apin.menu", label=name if len(name) <= 16 else name[:15] + "…", w=round(wk[0]), f=round(fh[0])), [tr("apin.pin", label=name)] + pin
    return menu, "\n".join(pin)


def tg_status():
    """10-minute step: the limits as the text of the bot's menu button, and one pinned status message edited in place."""
    t, L = tg_target(), STATE["limits"]
    if not t or not L or not SET["url"].startswith("https://"):
        return
    now, dest = time.time(), f'{SET["tg_bot"]}:{t[1]}:{t[2]}'
    st = read_json(DATA / "tg.json") or {}
    st = st if st.get("dest") == dest else {"dest": dest}   # another bot, chat or topic: start over there
    old = dict(st)
    win = {k: (u, frac, reset) for k, u, frac, reset, _ in windows(L, now)}
    wk, fh = win.get("seven_day"), win.get("five_hour")
    if not wk or not fh:
        return
    fc = week_forecast(wk[0], wk[1]) if wk[1] is not None else None
    menu, pin = tg_texts(wk, fh, fc, live()["n"], now, acct_name())
    if SET["tg_menu"]:
        text = menu
        if st.get("menu") != text and tg_call("setChatMenuButton", chat_id=t[1], menu_button={
                "type": "web_app", "text": text, "web_app": {"url": SET["url"] + "/"}})[0]:
            st["menu"] = text
    if SET["tg_pin"]:
        text = pin
        if st.get("pin_text") != text:
            ok, r = tg_call("editMessageText", chat_id=t[1], message_id=st["pin"], text=text, **tg_open(t)) if st.get("pin") else (False, "400 new")
            ok = ok or "not modified" in str(r)
            if not ok and str(r).startswith("400"):   # gone or never sent; a network error just waits for the next round
                ok, r = tg_call("sendMessage", chat_id=t[1], **({"message_thread_id": int(t[2])} if t[2] else {}), text=text,
                                disable_notification=True, **tg_open(t))
                if ok:
                    st["pin"] = r["message_id"]
                    tg_call("pinChatMessage", chat_id=t[1], message_id=st["pin"], disable_notification=True)
            if ok:
                st["pin_text"] = text
    if st != old:
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "tg.json").write_text(json.dumps(st))


def background():
    while True:
        ensure_snap()
        refresh_limits(540)
        check_alerts()
        try:
            tg_status()
        except Exception as e:  # noqa: BLE001 - the loop must keep running
            print("telegram:", type(e).__name__, e, flush=True)
        guard()
        refresh_status()
        refresh_bots()
        watch_config()
        if SET["currency"] == "EUR":
            refresh_fx()
        time.sleep(600)


PC_HOST = os.environ.get("USAGECAST_PC", "").strip().lower()   # tailnet machine /api/summary reports as `pc`
NET_WATCH = os.environ.get("USAGECAST_WATCH", "").strip().partition("=")   # "Name=https://host/health": `down` lists Name
NET = {"pc": None, "up": None}   # last result of net_loop; the request path only reads this


def pc_online(raw, host=None):
    """True/False for the USAGECAST_PC peer in `tailscale status --json` output, None if unreadable, unset or no such peer."""
    host = (host or PC_HOST).lower()
    try:
        peers = json.loads(raw).get("Peer") or {}
        return next((bool(p["Online"]) for p in peers.values() if host and str(p.get("HostName")).lower() == host), None)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def net_loop():
    """Every 60 s: is the USAGECAST_PC machine online in the tailnet, does the USAGECAST_WATCH address answer (< 500)."""
    while True:
        try:
            NET["pc"] = pc_online(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5).stdout) if PC_HOST else None
        except (OSError, subprocess.SubprocessError):
            NET["pc"] = None
        if NET_WATCH[2]:
            try:
                with urllib.request.urlopen(NET_WATCH[2], timeout=5):
                    NET["up"] = True
            except urllib.error.HTTPError as ex:
                NET["up"] = ex.code < 500
            except (OSError, ValueError):
                NET["up"] = False
        time.sleep(60)


def snapshot():
    """Runs in the Hermes venv: measures system prompt parts and tool schemas as Hermes currently sends them.
    It only builds Hermes' agent object and reads its prompt and tool list; no request goes to a model. The platform
    is the one most sessions come from, because the tool selection depends on it."""
    import logging
    logging.disable(logging.WARNING)
    sys.path.insert(0, str(HERMES / "hermes-agent"))
    os.chdir(HERMES / "hermes-agent")
    os.environ["USAGECAST_ROOT"] = str(HERMES)
    if os.environ.get("USAGECAST_HOME"):
        _req.home = Path(os.environ["USAGECAST_HOME"])   # the home ensure_snap() asked for
    os.environ["HERMES_HOME"] = str(source_home())   # a profile loads its own config, tools and SOUL.md
    from run_agent import AIAgent
    from agent.system_prompt import build_system_prompt
    platform = (connect().execute("""select source from sessions where source not in ('cron', 'subagent')
        group by source order by count(*) desc limit 1""").fetchone() or ("cli",))[0]
    agent = AIAgent(model=config_value("model", "default") or None, provider=config_value("model", "provider") or None,
                    platform=platform, quiet_mode=True)
    prompt = defaultdict(float)
    for label_, seg in segments(build_system_prompt(agent), markers()[0]):
        prompt[label_] += tok(seg)
    tools = {t["function"]["name"]: len(json.dumps(t)) / CPT for t in (agent.tools or [])}
    try:  # ponytail: private Hermes helper; if it moves, the unused-tools tip just loses its command
        from hermes_cli.config import load_config_readonly
        from hermes_cli.tools_config import _get_platform_tools
        from toolsets import resolve_toolset
        on = {k: set(resolve_toolset(k)) for k in _get_platform_tools(load_config_readonly(), platform)}
        sets = {n: sorted(k for k, ts in on.items() if n in ts or n.startswith(f"mcp__{k.replace('-', '_')}__"))
                for n in tools}   # toolsets enabled on this platform that hold the tool
    except Exception:  # noqa: BLE001
        sets = {}
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = snap_path().with_suffix(".tmp")
    tmp.write_text(json.dumps({"prompt": prompt, "tools": tools, "sets": sets, "platform": platform, "at": time.time()}))
    tmp.replace(snap_path())


# ---------- HTML ----------
e = html.escape
STATIC = ROOT / "static"
STATIC_FILES = {"style.css": "text/css; charset=utf-8", "haptics.js": "text/javascript; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "wrapped.js": "text/javascript; charset=utf-8", "manifest.webmanifest": "application/manifest+json",
                "icon.svg": "image/svg+xml", "icon-180.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png"}
ICONS = {  # 24 grid, stroke in currentColor
    "/": '<path d="M4 16a8 8 0 1 1 16 0"/><path d="M12 16l3.5-4.5"/>',
    "/history": '<path d="M5 19v-7M10 19V6M15 19v-4M20 19V9"/>',
    "/details": '<path d="M9 7h11M9 12h11M9 17h11M4.5 7h.01M4.5 12h.01M4.5 17h.01"/>',
    "/sessions": '<path d="M20 14.5a2 2 0 0 1-2 2H8.5L4 20V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>',
    "/settings": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    "/projects": '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.8l2 2h7.2a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
}
NAV = (("nav.summary", (("/", "nav.overview"), ("/history", "nav.history"))),
       ("nav.breakdown", (("/details", "nav.details"), ("/sessions", "nav.sessions"), ("/projects", "nav.projects"))))
ALIASES = {"/verlauf": "/history", "/projekte": "/projects"}  # paths of the first, German-only version
LOGO = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path class="lt" d="M5 16a7 7 0 0 1 14 0"/>'
        '<path class="la" d="M5 16a7 7 0 0 1 10.5-6.06"/><path class="ln" d="M12 16l2.6-3"/></svg>')
COLORS = 5  # colored components in the stacked history, plus "rest"


def brk(s):
    """Escape and let long names like memory_tencentdb_conversation_search wrap at _ and /."""
    return e(s).replace("_", "_<wbr>").replace("/", "/<wbr>")


def link(path, **q):
    q = urlencode({k: v for k, v in q.items() if v})
    return path + ("?" + q if q else "")


def chip(text, on, href):
    return f'<a href="{href}"{" class=on aria-current=true" if on else ""}>{text}</a>'


def group(k):
    return "tool:skill_view" if k.startswith("skill:") else k


def table(head, rows, cls=(), tcls=""):
    """head: locale keys. cls: CSS classes per column, l = left aligned, o = hidden on phones."""
    c = lambda i: f' class="{cls[i]}"' if i < len(cls) and cls[i] else ""
    if SET["cost_unit"] == "week" and RATE.get("k"):   # ponytail: text replace on money()'s output, a short money() if this breaks
        head = ["th.cost.wk" if x == "th.cost" else x for x in head]
        unit, short = tr("fmt.week", v=""), tr("fmt.pct", v="")
        rows = [[str(x).replace(unit, short) for x in r] for r in rows]
    h = "".join(f"<th{c(i)}>{e(tr(x))}</th>" for i, x in enumerate(head))
    b = "".join("<tr>" + "".join(f"<td{c(i)}>{x}</td>" for i, x in enumerate(r)) + "</tr>" for r in rows) \
        or f'<tr><td colspan="{len(head)}">{tr("nodata")}</td></tr>'
    return f'<div class="scroll"><table{f" class={tcls}" if tcls else ""}><tr>{h}</tr>{b}</table></div>'


def amount(v, tot):
    return money(v) if SET["cost_unit"] == "week" and RATE.get("k") else f"{pct(v, tot)} · {money(v)}"


def bars(items, tot, ttl, explain=True, n=12, p=None, soft=False, name_of=None, raw=False):
    """raw: the why from name_of is HTML (the chats of a bot), not a sentence to escape."""
    if not items:
        return f'<p class="hint">{tr("nodata")}</p>'
    name_of = name_of or (lambda k: label(k, ttl))
    top = items[0][1] or 1
    out = ""
    for k, v in items[:n]:
        name, why = name_of(k)
        head = (f'<div class="row"><span>{brk(name)}</span><span class="v">{amount(v, tot)}</span></div>'
                f'<div class="t{" soft" if soft else ""}"><i style="width:{v / top * 100:.1f}%"></i></div>')
        if explain and why and (k in FIXED or not k.startswith("tool:")):   # the sentence for tools would repeat on every tool
            out += f'<details class="bar"><summary>{head}</summary><div class="why">{why if raw else e(why)}</div></details>'
        else:
            out += f'<div class="bar">{head}</div>'
    rest = sum(v for _, v in items[n:])
    if rest:
        more = f'<a href="{link("/details", p=p)}">{tr("nav.details")}</a>' if p else tr("nav.details")
        out += f'<p class="hint">{tr("bars.rest", n=len(items) - n, p=pct(rest, tot), details=more)}</p>'
    return out


def dur(s):
    """Rough duration: ~59 min, ~1 h 20 min."""
    m = max(round(s / 60), 1)
    return tr("dur.min", m=m) if m < 60 else tr("dur.hm", h=m // 60, m=m % 60)


def until(t):
    if not t:
        return ""
    s = t - time.time()
    if s < 3600:
        return tr("until.min", m=max(int(s // 60), 1))
    if s < 86400:
        return tr("until.hm", h=int(s // 3600), m=int(s % 3600 // 60))
    return tr("until.day", at=when(t, "daytime"))


def meter(u, frac=None, hot=False, left=False):
    """Bar with a mark where the time stands. left: shows what is left, bar and mark both run down."""
    pos = 1 - frac if left and frac is not None else frac
    mark = (f'<b class="now" style="left:{pos * 100:.1f}%" title="{tr("lim.elapsed", p=pc(frac * 100))}"><span class="sr">{tr("lim.elapsed", p=pc(frac * 100))}</span></b>'
            if frac is not None else "")
    w = 100 - u if left else u
    return f'<div class="meter{" hot" if hot else ""}"><i style="width:{min(max(w, 0), 100):.1f}%"></i>{mark}</div>'


LIVE_MAX = 900   # s; a busy mark older than this is a run that died, not one that works


JOB_KEEP, JOB_QUIET = 900, 1800   # s a finished/stopped job stays listed; s a pid-less progress file counts without a write
JOB_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}")
IMG_TYPES, IMG_MAX = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}, 5 << 20


def proc(pid):
    """(parent pid, start time in clock ticks) of a running process, None when it is gone or a zombie."""
    try:
        f = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return None if f[0] == "Z" else (int(f[1]), int(f[19]))
    except (OSError, ValueError, IndexError, TypeError):
        return None


def cmd_label(c):
    """Short label of a shell command: leading cd steps, env assignments and the home path dropped, whitespace folded."""
    c = re.sub(r"^(\s*(cd\s+\S+\s*(&&|;)|\w+=\S*\s|exec\s))+\s*", "", str(c or "")).replace(str(Path.home()), "~")
    return " ".join(c.split())[:90]


def job_share(g):
    """Done share 0..1 of a progress file: (done + frac) / total, or frac alone; None when it reports none."""
    try:
        fr, t = min(max(float(g.get("frac") or 0), 0), 1), float(g.get("total") or 0)
        return min((float(g.get("done") or 0) + fr) / t, 1) if t > 0 else fr if "frac" in g else None
    except (TypeError, ValueError):
        return None


def jobs(now=None, hs=None):
    """Background work of every home: live Hermes background processes (processes.json) and progress files
    (progress/<name>.json, see tools/progress.py). A file whose pid is such a process or runs under one fills that row."""
    now, out = now or time.time(), []
    for a, h in hs or homes():
        rows, by_pid = [], {}
        for p in read_json(h / "processes.json") or []:
            st = proc(p.get("pid")) if isinstance(p, dict) else None
            if st and (not p.get("host_start_time") or st[1] == p["host_start_time"]):
                rows.append(dict(agent=a, cmd=cmd_label(p.get("command")), since=p.get("started_at") or now,
                                 owner=p.get("owner_task_id") or p.get("parent_session_id"), prog=None))
                by_pid[int(p["pid"])] = rows[-1]
        for f in sorted((h / "progress").glob("*.json")):
            g = read_json(f)
            if not isinstance(g, dict) or not JOB_NAME.fullmatch(f.stem):
                continue
            try:
                at, pid = float(g.get("at") or f.stat().st_mtime), g.get("pid")
            except (OSError, TypeError, ValueError):
                continue
            pid = pid if isinstance(pid, int) and pid > 1 else None
            end = g.get("state") if g.get("state") in ("done", "failed") else "done" if job_share(g) == 1 and g.get("total") else None
            if not end and pid and not proc(pid):
                end = "stopped"
            if end and now - at > JOB_KEEP or not end and not pid and now - at > JOB_QUIET:
                continue
            host, seen = None, set()
            while pid and pid not in seen and not end:   # walk up the parents to a Hermes background process
                if pid in by_pid and not by_pid[pid]["prog"]:
                    host = by_pid[pid]
                    break
                seen.add(pid)
                pid = (proc(pid) or (0,))[0]
            try:
                iv = int(Path(g["image"]).stat().st_mtime) if isinstance(g.get("image"), str) else None
            except OSError:
                iv = None
            g = dict(g, name=f.stem, end=end, at=at, iv=iv, share=1.0 if end == "done" else job_share(g))
            if host:
                host["prog"] = g
            else:
                rows.append(dict(agent=a, cmd="", since=g.get("started") or at, owner=None, prog=g))
        if rows:
            with at_home(h):
                try:
                    ci = chat_index(connect())
                except sqlite3.Error:
                    ci = {}
            for r in rows:
                r["platform"], r["chat"] = ci.get(r["owner"], (None, None))
        out += rows
    return sorted(out, key=lambda r: r["since"])


def job_image(name, h):
    """(bytes, content type) of a progress file's preview image in home h, None unless name, type, place and size check out."""
    g = read_json(h / "progress" / f"{name}.json") if h is not None and JOB_NAME.fullmatch(name) else None
    f = Path(g["image"]) if isinstance(g, dict) and isinstance(g.get("image"), str) else None
    try:
        if f and f.is_absolute() and f.suffix.lower() in IMG_TYPES and f.is_file() and f.stat().st_size <= IMG_MAX:
            return f.read_bytes(), IMG_TYPES[f.suffix.lower()]
    except OSError:
        pass
    return None


def live(now=None, hs=None):
    """Agents at work right now. While a turn runs, Hermes keeps an activity text on the session ("tool running:
    terminal") and clears it at the end; subagents are sessions with a parent. Plus the gateway and the next cron runs."""
    now = now or time.time()
    runs = []
    for a, h in hs or homes():
        with at_home(h):
            try:
                c, ttl = connect(), cache_ttl()
                rows = c.execute("""select id, source, title, parent_session_id, last_activity_description, started_at, model,
                    input_tokens, cache_read_tokens, cache_write_tokens, output_tokens from sessions where ended_at is null
                    and coalesce(last_activity_description, '') != '' and last_activity_at > ? order by started_at""",
                                 (now - LIVE_MAX,)).fetchall()
                ci = chat_index(c) if rows else {}
                for sid, src, title, parent, what, started, model, i, r, w, o in rows:
                    turn = c.execute("select max(timestamp) from messages where session_id = ? and role = 'user'", (sid,)).fetchone()[0]
                    plat, ch = ci.get(sid, (src, NOCHAT))
                    runs.append(dict(id=sid, agent=a, src=src, platform=plat, chat=ch, title=title, parent=parent, what=what, since=turn or started or now, model=model_name(model),
                                     cost=sum(row_cost(model or "", i or 0, r or 0, w or 0, o or 0, ttl))))
            except sqlite3.Error:
                pass   # no state.db yet, or an older Hermes without the activity columns
    gw, up, down = read_json(HERMES / "gateway_state.json"), None, []
    if isinstance(gw, dict):
        try:
            os.kill(int(gw.get("pid")), 0)
            up = gw.get("gateway_state") == "running"
        except PermissionError:
            up = gw.get("gateway_state") == "running"
        except (TypeError, ValueError, OSError):
            up = False
        down = sorted(k for k, v in (gw.get("platforms") or {}).items()
                      if isinstance(v, dict) and (v.get("state") != "connected" or v.get("needs_attention")))
    nxt = []
    for j in all_jobs() if hs is None else ():
        try:
            t = datetime.fromisoformat(j["next_run_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if j.get("enabled", True) and j.get("state") == "scheduled" and not j.get("no_agent") and t > now:
            nxt.append((t, j.get("name") or ""))
    return dict(runs=runs, n=sum(r["src"] != "subagent" for r in runs), up=up, down=down, next=sorted(nxt)[:3], at=now, jobs=jobs(now, hs))


def job_row(j, p, now, big=False):
    """One background job: title, bot · chat, step, runtime, time left, bar and share. Small: a thumbnail and a link to
    the session that started it (Right now). big: on that session's page, the last image in full card width instead."""
    g = j["prog"] or {}
    sh, end, left = g.get("share"), g.get("end"), job_left(j, now)
    meta = [e(bot_chat(j)) if j["owner"] and j["chat"] else "", e(str(g.get("step") or "")[:60]),
            e(str(g.get("detail") or "")[:40]), "" if end else tr("live.for", d=dur(now - j["since"])),
            tr("live.left", d=dur(left)) if left else ""]
    src = f'/job/{quote(g["name"])}.img?a={quote(j["agent"])}&amp;v={g["iv"]}' if g.get("iv") else ""
    img = f'<img class="thumb" src="{src}" alt="" loading="lazy">' if src and not big else ""
    bar = f'<span class="meter{" hot" if end in ("failed", "stopped") else ""}"><i style="width:{sh * 100:.1f}%"></i></span>' if sh is not None else ""
    v = tr(f"live.job.{end}") if end else f"{nf(sh * 100)} %" if sh is not None else ""
    tag, href = ("a", f' href="/s/{quote(j["owner"])}?p={e(p)}"') if j["owner"] and not big else ("div", "")
    return (f'<{tag} class="job bg"{href}>{img}<span class="run"><span class="fname">{e(str(g.get("title") or j["cmd"])[:70])}</span>'
            f'<span class="why">{" · ".join(x for x in meta if x)}</span>{bar}</span><span class="v{" hot" if end in ("failed", "stopped") else ""}">{v}</span></{tag}>'
        + (f'<img class="shot" src="{src}" alt="{e(str(g.get("title") or ""))}">' if src and big else ""))


def live_card(L, p):
    """"Right now" on the overview: who works on what, for how long, and what that session has cost so far. /live
    serves the card alone; static/app.js swaps it every few seconds while the page is in view."""
    ids = {r["id"] for r in L["runs"]}
    kids = defaultdict(list)
    for r in L["runs"]:
        if r["src"] == "subagent" and r["parent"] in ids:
            kids[r["parent"]].append(r)

    def row(r, sub=""):
        meta = ["" if sub else e(bot_chat(r)), e(r.get("model") or ""), e((r["what"] or "")[:48]), tr("live.for", d=dur(L["at"] - r["since"]))]
        return (f'<a class="job{sub}" href="/s/{quote(r["id"])}?p={e(p)}"><span class="run"><span class="fname">{e((r["title"] or tr("untitled"))[:70])}</span>'
                f'<span class="why">{" · ".join(x for x in meta if x)}</span></span><span class="v">{money(r["cost"])}</span></a>'
                + "".join(row(k, " sub") for k in kids[r["id"]]))

    tops = [r for r in L["runs"] if not (r["src"] == "subagent" and r["parent"] in ids)]
    subs, bg = len(L["runs"]) - len(tops), L.get("jobs") or []
    head = (tr("live.n", n=L["n"]) + (" · " + tr("live.subs", n=subs) if subs else "")) if L["runs"] else tr("live.none")
    head += " · " + tr("live.bg", n=len(bg)) if bg else ""
    notes = [tr("live.gw.off")] if L["up"] is False else [tr("live.gw.down", names=e(", ".join(L["down"])))] if L["down"] else []
    nxt = ", ".join(f"{e(n or tr('untitled'))} {hm(t)}" for t, n in L["next"])
    return (f'<section class="card live" id="live" data-p="{e(p)}" data-n="{L["n"]}"><h2>{tr("live.h")}</h2><p class="state">{head}</p>'
            + (f'<div class="jobs">{"".join(row(r) for r in tops)}</div>' if tops else "")
            + (f'<div class="jobs bgs">{"".join(job_row(j, p, L["at"]) for j in bg)}</div>' if bg else "")
            + "".join(f'<p class="hint hot">{x}</p>' for x in notes)
            + (f'<p class="hint">{tr("live.next", list=nxt)}</p>' if nxt else "") + "</section>")


def job_left(j, now):
    """Rough seconds left of a job row from its share done and runtime, None when unknown or finished."""
    g = j["prog"] or {}
    sh = g.get("share")
    return (now - float(g.get("started") or j["since"])) * (1 - sh) / sh if not g.get("end") and sh and 0.02 <= sh < 1 else None


def summary_jobs(J, now):
    """Background jobs for /api/summary: up to 3 running {name, pct, eta_s}, then up to 2 that finished well in the
    last 10 minutes with "done": true. Failed and stopped ones are left out."""
    out, fin = [], []
    for j in J:
        g = j["prog"] or {}
        sh, left = g.get("share"), job_left(j, now)
        row = {"name": str(g.get("title") or g.get("name") or j["cmd"])[:40], "pct": None if sh is None else int(sh * 100),
               "eta_s": None if left is None else round(left)}
        if not g.get("end"):
            out.append(row)
        elif g["end"] == "done" and now - g["at"] <= 600:
            fin.append((g["at"], dict(row, done=True)))
    return out[:3] + [r for _, r in sorted(fin, key=lambda x: -x[0])[:2]]


def api_summary():
    """Compact JSON for widgets: limits with reset times, the week forecast and this limit week's cost."""
    fresh_limits()
    L, now = STATE["limits"] or {}, time.time()
    out = {"at": round(STATE["ok"]) or None, "limits": {}}
    for key, u, frac, reset, length in windows(L, now):
        out["limits"][key] = {"used": u, "resets_at": round(reset) if reset else None,
                              "elapsed": round(frac, 3) if frac is not None else None}
    w = out["limits"].get("seven_day")
    if w and w["elapsed"] is not None:
        w["forecast"] = week_forecast(w["used"], w["elapsed"])
    x = L.get("extra_usage") or {}
    if x.get("is_enabled"):
        out["extra"] = {k: x.get(k) for k in ("used_credits", "monthly_limit", "currency", "decimal_places")}
        dp, m = 10 ** (x.get("decimal_places") or 0), extra_months(limit_history(400))
        out["extra"].update(used=(x.get("used_credits") or 0) / dp, limit=(x.get("monthly_limit") or 0) / dp,
                            month_start=round(m[-1][0]) if m else None)
        est = overrun_usd((w or {}).get("forecast"))
        if est is not None:
            out["extra"]["estimate_usd"] = round(est, 2)   # credits the week would need at this pace
    if len(STATE["accts"]) > 1:   # the fields above describe the primary one
        out["accounts"] = [{"label": a["label"] or a["id"][:8], "use": a["use"], "next": a["next"], "primary": a["primary"],
                            **{k: ((a["limits"] or {}).get(k) or {}).get("utilization")
                               for k in ("five_hour", "seven_day")}} for a in STATE["accts"]]
    lv = live()
    out["agents"], out["jobs"] = lv["n"], summary_jobs(lv.get("jobs") or [], lv["at"])
    out["pc"] = NET["pc"]
    out["down"] = (["Hermes"] if lv["up"] is False else []) + ([NET_WATCH[0]] if NET_WATCH[2] and NET["up"] is False else [])
    d = data_for("w")
    out["week_cost_usd"] = round(d["total"], 2) if d["week"] else None
    return json.dumps(out)


TIGHT = 85   # forecast % from which the verdict says "Tight": the backtest was 16-47 % off, so 99 % is no "On track"


def win_txt(key, u, frac, reset, length, hist, now, lv, fc=None):
    """Verdict line under one limit window's meter and whether the meter is hot. fc: a week forecast worked out elsewhere."""
    start = reset - length if reset else now
    if key == "seven_day":
        fc = week_forecast(u, frac) if fc is None else fc
        if u >= 100:
            txt = tr("lim.full", reset=until(reset))
        elif fc is None:
            txt = tr("lim.young", reset=until(reset))
        elif fc > 100:
            txt = tr("lim.week.over", full=when(start + (now - start) * 100 / u, "daytime"), reset=until(reset))
        else:
            txt = tr("lim.week.tight" if fc >= TIGHT else "lim.week.lands", fc=tr("lim.left", p=pc(100 - fc)) if lv else pc(fc), frac=pc(frac * 100), reset=until(reset))
        return txt, u >= 80 or (fc or 0) > 100
    hot = u >= 80
    if u >= 100:
        txt = tr("lim.full", reset=until(reset))
    elif length <= 5 * 3600:
        r = rate(hist, key, u, start, now) if reset else None
        fa = now + (100 - u) / r if r else None
        if fa and fa < reset:
            txt, hot = tr("lim.five.full_at", left=dur(fa - now), full=hm(fa), reset=until(reset)), hot or fa - now < 1800
        elif r is not None:
            at = min(u + r * (reset - now), 100)
            txt = tr("lim.five.tight" if at >= TIGHT else "lim.five.enough", reset=hm(reset), at=tr("lim.left", p=pc(100 - at)) if lv else pc(at))
        else:
            txt = until(reset)
    else:
        fc = week_forecast(u, frac)
        txt = (tr("lim.forecast", fc=pc(fc)) + " " if fc else "") + until(reset)
    return txt, hot


def budget(u, reset, now):
    # ponytail: even split of what is left; "left today" would need the limit value at midnight
    return tr("lim.budget", pct=pc((100 - u) / max((reset - now) / 86400, 1)))


def acct_head(a, now):
    """Name of a pooled account and one calm badge: limit (until the full window resets), in use or next."""
    full = [r for k, u, f, r, n in windows(a["limits"], now) if u >= 100]
    if full or not a["avail"]:
        t = max(full) if full and all(full) else None
        b = ("badge hot", tr("acct.limit", at=hm(t) if t - now < 86400 else when(t, "daytime")) if t else tr("acct.out"))
    else:
        b = ("badge", tr("acct.use")) if a["use"] else ("badge next", tr("acct.next")) if a["next"] else None
    tag = f'<span class="{b[0]}">{b[1]}</span>' if b else ""
    return f'<div class="acct"><span class="who">{e(a["label"] or a["id"])}</span>{tag}</div>'


def pet_window(ws):
    """The window the mascot follows (ws as from windows()): the 5-hour one on plans that have it (it is what throttles
    first), else the week."""
    ws = {k: w for k, *w in ws}
    return next(((k, *ws[k]) for k in ("five_hour", "seven_day") if k in ws), None)


def pet_state(ws, avail):
    """Mascot mood, from the window it follows only: limit (a window full or the account not available, both block
    work) > tight (>= 85 %, or the week forecast >= TIGHT when it follows the week) > work (>= 50 %) > calm."""
    if not avail or any(u >= 100 for _, u, *_ in ws):
        return "limit"
    k, u, f, *_ = pet_window(ws) or (None, 0, None)
    if u >= 85 or k == "seven_day" and (week_forecast(u, f) or 0) >= TIGHT:
        return "tight"
    return "work" if u >= 50 else "calm"


def pet_html(ws, avail):
    """The mascot for a set of windows: the primary account's, or all accounts pooled."""
    if SET["pet"] not in PETS or not ws:
        return ""
    st = pet_state(ws, avail)
    used = (pet_window(ws) or (None, 0))[1]
    return f'<span class="pet" data-st="{st}" role="img" aria-label="{tr("pet." + st)}">{pet(SET["pet"], st, used)}</span>'


def big_row(u, lv, pet):
    return (f'<div class="lbl">{tr("lim.week")}</div><div class="bigrow"><div class="big">{nf(100 - u if lv else u)}'
            f'<span>{tr("lim.unit.left" if lv else "lim.unit")}</span></div>{pet}</div>')


def lim_row(key, u, frac, hot, lv, txt, bud=""):
    return (f'<div class="lim"><div class="row"><span>{tr("lim.week" if key == "seven_day" else "win." + key)}</span>'
            f'<b>{tr("lim.left", p=pc(100 - u)) if lv else pc(u)}</b></div>{meter(u, frac, hot, lv)}<div class="why">{txt}</div>{bud}</div>')


def pooled(accts, now):
    """All accounts as one, as a windows() list: per window the mean use (100 % = all full), the mean elapsed share and
    the next reset. Only windows every account has."""
    ws = [windows(a["limits"], now) for a in accts]
    out = []
    for key, length in WINDOWS[:2]:
        xs = [w for x in ws for w in x if w[0] == key]
        if len(xs) < len(accts):
            continue
        fr, rs = [w[2] for w in xs if w[2] is not None], [w[3] for w in xs if w[3]]
        # ponytail: the mean elapsed share is the pooled clock; with resets days apart the forecast is a rough blend
        out.append((key, sum(w[1] for w in xs) / len(xs), sum(fr) / len(fr) if fr else None, min(rs) if rs else None, length))
    return out


def pool_history(accts):
    """Limits log rows averaged over the accounts, only where every account was read at the same time."""
    by = {}
    for a in accts:
        for h in limit_history(acct=a["id"]):
            by.setdefault(h["t"], []).append(h)
    return [{"t": t, **{k: sum(h.get(k) or 0 for h in hs) / len(hs) for k in ("five_hour", "seven_day")}}
            for t, hs in sorted(by.items()) if len(hs) == len(accts)]


def pool_cols(accts, ws, now, lv):
    """Left and right column of the pooled view. Daily budget = what each account has left per day up to its own
    reset, averaged like the use."""
    hist, left, right = pool_history(accts), "", ""
    own = [w for a in accts for w in windows(a["limits"], now) if w[0] == "seven_day"]
    fcs = [week_forecast(w[1], w[2]) for w in own]
    fc = sum(fcs) / len(fcs) if fcs and None not in fcs else None   # each account to its own reset; the blended clock misleads
    for key, u, frac, reset, length in ws:
        txt, hot = win_txt(key, u, frac, reset, length, hist, now, lv, fc)
        if key != "seven_day":
            right += lim_row(key, u, frac, hot, lv, txt)
            continue
        left = big_row(u, lv, pet_html(ws, any(a["avail"] for a in accts))) + f'{meter(u, frac, hot, lv)}<p class="fc">{txt}</p>'
        per = [(100 - w[1]) / max((w[3] - now) / 86400, 1) for w in own if w[1] < 100 and w[3]]
        if per:
            left += f'<p class="why">{tr("lim.budget", pct=pc(sum(per) / len(accts)))}</p>'
    return left, right


def acct_block(a, now, lv):
    """A non-primary account in the limits card: week (verdict + daily budget) left, the other windows right."""
    hist, wk, rest = limit_history(acct=a["id"]), "", ""
    for key, u, frac, reset, length in windows(a["limits"], now):
        txt, hot = win_txt(key, u, frac, reset, length, hist, now, lv)
        bud = f'<div class="why">{budget(u, reset, now)}</div>' if key == "seven_day" and u < 100 and reset else ""
        row = lim_row(key, u, frac, hot, lv, txt, bud)
        if key == "seven_day":
            wk = row
        else:
            rest += row
    if not a["limits"]:
        wk = f'<p class="hint">{tr("acct.error", err=e(a["error"] or "?"))}</p>'
    return f'{acct_head(a, now)}<div>{wk}</div><div>{rest}</div>'


def limits_card(week_cost, split=None):
    fresh_limits()
    L, now = STATE["limits"], time.time()
    if not L:
        return f'<section class="card"><h2>{tr("lim.title")}</h2><p class="hint">{tr("lim.unavailable")}</p></section>'
    hist, left, right, week = limit_history(), "", "", None
    lv = SET["limit_view"] == "left"
    accts = STATE["accts"] if len(STATE["accts"]) > 1 else []
    pw = pooled(accts, now) if accts and all(a["limits"] for a in accts) else []
    pet = "" if pw else pet_html(windows(L, now), all(a["avail"] for a in STATE["accts"] if a["primary"]))
    for key, u, frac, reset, length in windows(L, now):
        start = reset - length if reset else now
        txt, hot = win_txt(key, u, frac, reset, length, hist, now, lv)
        if key == "seven_day":
            fc = week_forecast(u, frac)
            week = (start, reset, length, u, fc)
            left = big_row(u, lv, pet) + f'{meter(u, frac, hot, lv)}<p class="fc">{txt}</p>'
            if u < 100 and reset:
                left += f'<p class="why">{budget(u, reset, now)}</p>'
            more = ""
            if week_cost is not None:
                more += f'<p class="why">{tr("lim.weekcost", cost=cash(week_cost))}</p>'
            if split and split["hermes"] + split["cc"] + split["rest"] >= 1:
                parts = {k: pc(split[k]) for k in ("hermes", "cc", "rest")}
                extra = "".join(" " + tr("lim.split." + k, p=pc(split[k])) for k in ("before", "gap") if split[k] >= 1)
                more += (f'<p class="why">{tr("lim.split" if CLAUDE.is_dir() or ingest_hosts() else "lim.split.nocc", start=when(split["start"], "daytime"), **parts)}'
                         f'{extra}</p>')
            hosts = ingest_hosts()
            if hosts:
                names = ", ".join(f"{e(h)} ({when(x['at'], 'daytime')})" for h, x in hosts.items())
                more += f'<p class="why">{tr("lim.hosts", hosts=names)}</p>'
            if FC.get("method"):
                gain = pc((1 - FC["wtd"] / FC["lin"]) * 100) if FC["lin"] else pc(0)
                more += f'<p class="why">{tr("lim.fc." + FC["method"], gain=gain, lin=pc(FC["lin"]), wtd=pc(FC["wtd"]))}</p>'
            if RATE.get("k"):
                more += f'<p class="why">{tr("lim.rate", cost=cash(1 / RATE["k"]), n=RATE["hours"])}</p>'
            if more:
                left += f'<details class="more"><summary>{tr("lim.more")}</summary>{more}</details>'
            continue
        right += lim_row(key, u, frac, hot, lv, txt)
    x = L.get("extra_usage") or {}
    if x.get("is_enabled") and x.get("monthly_limit"):
        dp, used = 10 ** (x.get("decimal_places") or 0), x.get("used_credits") or 0
        value = tr("lim.extra.value", used=nf(used / dp, 2), limit=nf(x["monthly_limit"] / dp, 2), cur=e(x.get("currency") or ""))
        wk = next((w for w in windows(L, now) if w[0] == "seven_day"), None)
        fc = week_forecast(wk[1], wk[2]) if wk else None
        est = overrun_usd(fc)
        why = tr("lim.extra.pace", over=pc(fc - 100), cost=cash(est)) if est is not None else tr("lim.extra.why")
        # ponytail: reads the whole limits log on every overview; cache by mtime if it ever gets slow
        past = "".join(f'<p class="why">{tr("lim.extra.month", start=when(t, "day"), used=nf(u, 2), cur=e(x.get("currency") or ""))}</p>'
                       for t, u in reversed(extra_months(limit_history(400))[:-1]))
        past = f'<details class="more"><summary>{tr("lim.extra.past")}</summary>{past}</details>' if past else ""
        right += (f'<div class="lim"><div class="row"><span>{tr("lim.extra")}</span><b>{value}</b></div>'
                  f'{meter(used / x["monthly_limit"] * 100)}<div class="why">{why}</div>{past}</div>')
    spark = ""
    if week and week[1]:
        start, reset, length, u, fc = week
        pts = [(h["t"], h["seven_day"]) for h in hist if h["t"] >= start and h.get("seven_day") is not None] + [(now, u)]
        if len(pts) >= 3:
            top = max(100, u, fc or 0) * 1.05   # headroom: the 100 % line and an overshoot stay inside
            xy = lambda t, v: f"{(t - start) / length * 100:.2f},{100 - v / top * 100:.1f}"
            cap = 100 - 100 / top * 100
            fcl = f'<polyline class="fcl" points="{xy(now, u)} {xy(reset, fc)}"/>' if fc else ""
            lg = f'<span><span class="dot c0"></span>{tr("lim.trend.week")}</span>' + (
                f'<span><span class="dash"></span>{tr("lim.trend.fc")}</span>' if fc else "") + f'<span><span class="dash cap"></span>{tr("lim.trend.cap")}</span>'
            spark = (f'<div class="trend"><svg class="spark" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
                     f'<line class="cap" x1="0" x2="100" y1="{cap:.1f}" y2="{cap:.1f}"/><polyline class="w" points="{" ".join(xy(t, v) for t, v in pts)}"/>{fcl}</svg>'
                     f'<div class="axis"><span>{tr("lim.trend", at=when(start, "daytime"))}</span><span>{tr("lim.trend.reset", at=when(reset, "daytime"))}</span></div>'
                     f'<div class="lg">{lg}</div></div>')
    left = left or f'<div class="lbl">{tr("lim.week")}</div><p class="hint">{tr("nodata")}</p>'
    stale = now - STATE["ok"] > 1800
    stamp = tr("lim.stale", at=when(STATE["ok"], "daytime")) if stale else tr("lim.stamp", at=hm(STATE["ok"]))
    if STATUS.get("indicator") not in (None, "none"):
        stamp += "<br>" + tr("lim.status", desc=html.escape(STATUS.get("description", "")), url=STATUS_URL)
    top = "".join(acct_head(a, now) for a in accts if a["primary"])
    rest = "".join(acct_block(a, now, lv) for a in accts if not a["primary"])
    if not pw:
        return (f'<section class="card hero" aria-label="{tr("lim.title")}">{top}<div>{left}</div><div>{right}'
                f'<p class="why">{stamp}</p></div>{spark}{rest}</section>')
    # Several accounts: one pooled view, today's per-account view folded below (app.js remembers it open per device)
    pl, pr = pool_cols(accts, pw, now, lv)
    return (f'<section class="card hero" aria-label="{tr("lim.title")}"><div class="acct"><span class="who">{tr("lim.pool")}</span></div>'
            f'<div>{pl}</div><div>{pr}<p class="why">{stamp}</p></div>'
            f'<details class="more accts" id="accts"><summary>{tr("lim.pool.each", n=len(accts))}</summary>'
            f'<div class="hero-in">{top}<div>{left}</div><div>{right}</div>{spark}{rest}</div></details>'
            f'<script>if(localStorage.accts)document.getElementById("accts").open=true</script></section>')


def layout(title, active, p, h1, sub, body, tabs=True, keep=None, back=""):
    """h1 and sub are already escaped. keep: filters that survive a period switch. back: link above the title."""
    nav = "".join(f'<div class="grp">{tr(g)}</div>' + "".join(
        f'<a href="{link(href, p=p)}"{" class=on aria-current=page" if href == active else ""}>'
        f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICONS[href]}</svg>{tr(name)}</a>' for href, name in items) for g, items in NAV)
    on = active == "/settings"
    nav += (f'<a class="end{" on" if on else ""}" href="/settings"{" aria-current=page" if on else ""}>'
            f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICONS["/settings"]}</svg>{tr("nav.settings")}</a>')
    gear = (f'<a class="gear{" on" if on else ""}" href="/settings" aria-label="{tr("nav.settings")}"{" aria-current=page" if on else ""}>'
            f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICONS["/settings"]}</svg></a>')   # phones only, see style.css
    seg = "".join(chip(tr("period." + k), k == p, link(active, p=k, **(keep or {}))) for k in PERIODS)
    a, b = custom(p) or (time.time() - 6 * 86400, time.time() + 1)
    iso = lambda t: datetime.fromtimestamp(t).date().isoformat()
    today, hidden = iso(time.time()), "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in (keep or {}).items())
    rng = (f'<details class="range{" on" if custom(p) else ""}"><summary>{e(pname(p)) if custom(p) else tr("range.custom")}</summary>'
           f'<form method="get" action="{active}">{hidden}<input type="date" name="from" value="{iso(a)}" max="{today}" aria-label="{tr("range.from")}" required>'
           f'<span>–</span><input type="date" name="to" value="{iso(b - 3600)}" max="{today}" aria-label="{tr("range.to")}" required>'
           f'<button class="btn primary">{tr("range.go")}</button></form></details>')
    seg = f'<div class="periods"><nav class="seg" aria-label="{tr("aria.period")}">{seg}</nav>{rng}</div>' if tabs else ""
    foot = (tr("foot.limits", at=hm(STATE["ok"]) if STATE["ok"] else "–") + "<br>"
            + f'<a href="/settings/notify">{tr("foot.ntfy.on" if push_on() else "foot.ntfy.off")}</a>')
    u = urlparse(getattr(_req, "url", "/"))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    langs = []
    for code in LOC:
        q["lang"] = code
        on = " class=on aria-current=true" if code == lang() else ""
        langs.append(f'<a href="{e(link(u.path, **q))}" hreflang="{code}" lang="{code}"{on}>{LOC[code]["lang.name"]}</a>')
    return head(title) + f"""<body><div class="app"><aside><a class="brand" href="{link("/", p=p)}"><span class="logo">{LOGO}</span><span>Usagecast</span></a>
<nav aria-label="{tr("aria.pages")}">{nav}</nav><div class="foot">{foot}</div></aside>
<main><header class="top"><div>{back}<h1>{h1}</h1><p class="sub">{sub}</p></div>{seg}{gear}</header>
{body}
<footer class="pf"><nav aria-label="{tr("aria.lang")}">{" · ".join(langs)}</nav><a href="{REPO_URL}">Usagecast {VERSION}</a></footer></main></div></body></html>"""


def head(title, extra=""):
    """<head> of every page: theme, zoom lock, scripts and styles (cache buster = newest static file), app icons."""
    th = SET["theme"]
    metas = "".join(f'<meta name="theme-color" content="{c}"' + (f' media="(prefers-color-scheme: {m})"' if th == "system" else "") + ">"
                    for m, c in (("light", "#f7f6f4"), ("dark", "#141312")) if th in ("system", m))
    try:
        v = int(max((STATIC / n).stat().st_mtime for n in ("style.css", "haptics.js", "app.js", "wrapped.js")))
    except OSError:
        v = 0
    theme = f' data-theme="{th}"' if th != "system" else ""
    return f"""<!doctype html><html lang="{lang()}"{theme}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,minimum-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover"><title>{e(title)}</title>
<script>for(const t of["gesturestart","gesturechange"])addEventListener(t,e=>e.preventDefault())</script>
<script type="module" src="/haptics.js?v={v}"></script><script defer src="/app.js?v={v}"></script>
<link rel="stylesheet" href="/style.css?v={v}"><link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon-180.png"><link rel="manifest" href="/manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Usagecast">
{metas}{extra}</head>
"""


CACHE, LOCK = {}, threading.Lock()


def connect():
    return sqlite3.connect(f"file:{source_home() / 'state.db'}?mode=ro", uri=True, timeout=15)


RANGE_RX = re.compile(r"(\d{4}-\d\d-\d\d)\.\.(\d{4}-\d\d-\d\d)")


def custom(p):
    """(start, end) of a custom period like "2026-10-01..2026-10-07" (both days included), otherwise None."""
    try:
        a, b = sorted(datetime.fromisoformat(x) for x in RANGE_RX.fullmatch(p or "").groups())
    except (AttributeError, ValueError):
        return None
    return a.timestamp(), (b + timedelta(days=1)).timestamp()


def pname(p):
    """Name of a period: "Limit week", "7 days", or the dates of a custom one."""
    if p in PERIODS:
        return tr("period." + p)
    a, b = custom(p)
    return when(a, "day") if b - a <= 86400 + 3600 else f"{when(a, 'day')} – {when(b - 3600, 'day')}"


def since_for(p):
    """(start of the period, whether it really is the limit week)."""
    if p == "w":
        fresh_limits()
        try:
            return datetime.fromisoformat(STATE["limits"]["seven_day"]["resets_at"]).timestamp() - 7 * 86400, True
        except (KeyError, TypeError, ValueError):
            pass
    if custom(p):
        return custom(p)[0], False
    return time.time() - PERIODS[p] * 86400, False


REFRESHING = set()
SAME = ("since", "until", "hourly", "ttl", "ttl_alt")   # equal in every part of a merged analysis


def merge(d, *more):
    """Adds the analyses of other Hermes homes into d: amounts add up, hour sets join, [count, $] lists add per place,
    the ten heaviest results stay. Session ids differ between homes, so sessions simply join."""
    def add(a, b):
        for k, v in b.items():
            if k not in a:
                a[k] = v
            elif isinstance(v, dict):
                add(a[k], v)
            elif isinstance(v, set):
                a[k] |= v
            elif isinstance(v, list):
                a[k] = sorted(a[k] + v, reverse=True)[:10] if k == "heavy" else [x + y for x, y in zip(a[k], v)]
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                a[k] += v
    for x in more:
        add(d, {k: v for k, v in x.items() if k not in SAME})
    return d


def compute(p):
    who = SET["source"]
    try:
        since, week = since_for(p)
        parts = []
        for a, h in homes():   # every agent alone with its own logs, prompt and config, then added up
            with at_home(h):
                x = analyze(connect(), since, load_snap(), cache_ttl(), until=(custom(p) or (0, math.inf))[1])
            for v in x["sess"].values():
                v["agent"] = a
            parts.append((a, h, x))
        d = parts[0][2] if len(parts) == 1 else merge(copy.deepcopy(parts[0][2]), *(x for *_, x in parts[1:]))
        d.update(week=week, p=p, at=time.time(), parts=parts)
        if SET["source"] != who:
            return d
        if p == "30":
            LONG.update({a: long_line([v["ctx1"] for v in x["sess"].values() if v.get("ctx1") and v["src"] not in ("cron", "subagent")])
                         for a, _, x in parts})
            r = limit_split(limit_history(30), d["hours"], cc_calls(since), since) or {}
            k, (ws, wk) = r.get("k"), since_for("w")
            u, cw = ((STATE["limits"] or {}).get("seven_day") or {}).get("utilization"), sum(
                sum(v.values()) for h, v in d["hours"].items() if h >= ws)
            if k and wk and u and cw:
                k = min(k, u / cw)   # ponytail: the rate drifts; never let Hermes' week cost exceed the measured week
            RATE.clear()
            RATE.update(k=k, hours=r.get("hours", 0))
            if wk:
                hc = defaultdict(float)
                for h, v in d["hours"].items():
                    hc[h] += sum(v.values())
                for t, c in cc_calls(since):
                    hc[t - t % 3600] += c
                prof, bt = rhythm(hc, round(ws / 3600) * 3600, since)
                FC.clear()
                if prof:   # the rhythm goes live only when it beat the straight line by at least 10 % in the backtest
                    FC.update(prof=prof, method="weighted" if bt["wtd"] <= 0.9 * bt["lin"] else "linear", lin=round(bt["lin"], 1),
                              wtd=round(bt["wtd"], 1))
                    keep = {k: FC[k] for k in ("method", "lin", "wtd")}
                    if (read_json(DATA / "forecast.json") or {}).get("method") != keep["method"]:
                        DATA.mkdir(parents=True, exist_ok=True)
                        (DATA / "forecast.json").write_text(json.dumps({**keep, "at": round(time.time())}))
        with LOCK:
            for k in [k for k in CACHE if custom(k) and k != p]:
                CACHE.pop(k)   # ponytail: keep only the latest custom period in memory
            CACHE[p] = (time.time(), d)
        return d
    finally:
        with LOCK:
            REFRESHING.discard(p)


def data_for(p):
    """Fresh cache (< 120 s) as is; a stale one is served at once and recomputed in the background."""
    with LOCK:
        hit = CACHE.get(p)
        if hit and (time.time() - hit[0] < 120 or p in REFRESHING):
            return hit[1]
        if hit:
            REFRESHING.add(p)
    if hit:
        threading.Thread(target=compute, args=(p,), daemon=True).start()
        return hit[1]
    return compute(p)


def warm():
    """Keeps the periods of the overview precomputed, so no page load waits for the analysis."""
    while True:
        now = f"{datetime.now():%Y-%m}"
        # 24 h and 7 days hourly (a cold 7 days took ~7 s); a page load in between refreshes them anyway
        hourly = [p for p in ("1", "7") if time.time() - CACHE.get(p, (0,))[0] > 3300]
        jobs = [(compute, p) for p in dict.fromkeys(("w", "30", SET["period"], *hourly))]
        # /wrapped opens at once: this month (its previous one comes along) and this year, refreshed before they reach
        # WRAP_TTL (a real year costs 20-60 s of CPU, so not every round)
        jobs += [(wrapped, *wrap_span({"m": [now]}), WRAP_TTL - 900), (wrapped, *wrap_span({"y": [now[:4]]}), WRAP_TTL - 900)]
        for f, *a in jobs:
            try:
                f(*a)
            except Exception as e:  # noqa: BLE001 - keep warming, the page computes on demand anyway
                print("warm:", e, flush=True)
        time.sleep(300)


def period_text(d):
    if custom(d["p"]):
        return pname(d["p"])
    if d["week"]:
        return tr("since.w", at=when(d["since"], "daytime"))
    return tr("since." + (d["p"] if d["p"] in ("1", "30") else "7"))


def window_sum(d, since):
    h0 = since - since % 3600
    return sum(sum(v.values()) for h, v in d["hours"].items() if h >= h0)


def span_sum(d, a, b):
    a = round(a / 3600) * 3600   # limit resets land a few ms off the full hour; the hour buckets start on it
    return sum(sum(v.values()) for h, v in d["hours"].items() if a <= h < b)


def signed(cur, prev):
    """+18 % / −7 % / ±0 %: change against an earlier value."""
    d = abs(cur / prev - 1) * 100
    return ("±" if d < 0.5 else "+" if cur >= prev else "−") + pc(d)


def week_delta(d30, cur, since, now):
    """"+18 % vs. last week" for a stretch since `since`, against the same stretch seven days earlier."""
    prev = span_sum(d30, since - 7 * 86400, now - 7 * 86400)
    return tr("kpi.delta", d=signed(cur, prev)) if prev > 0.01 and since >= now - 7.01 * 86400 else ""


def per_day(d):
    """{day start: [{component: $}, steps, {sessions}]}"""
    out = defaultdict(lambda: [defaultdict(float), 0, set()])
    for h, comp in d["hours"].items():
        a = out[floor(h, False)]
        for k, v in comp.items():
            a[0][group(k)] += v
        a[1] += d["hsteps"].get(h, 0)
        a[2] |= d["hsess"].get(h, set())
    return out


# ---------- Wrapped: a month or a year in numbers (the /wrapped slides render it) ----------
NIGHT_END = 5      # 00:00-04:59 is night; the "latest" session start counts from 05:00 on
# Usage type: the first rule that holds, in WRAP_TYPES order; explorer when none does.
EDGE_HITS = 3      # edge: a limit window hit 100 % at least this often
CONDUCTOR = 0.3    # conductor: subagents ran at least this share of the API price
NIGHT_OWL = 0.2    # owl: at least this share of the messages came at night
FOCUS = 0.6        # focus: the top project took at least this share of the API price
STEADY = 0.8       # steady: messages on at least this share of the days
WRAP_TYPES = ("edge", "conductor", "owl", "focus", "steady", "explorer")
WRAP = {}          # (start, end, source) -> (time, wrap_period()); a month's analysis takes ~10 s
WRAP_TTL = 3600    # s; a running period is recomputed after this, a finished one stays
WRAPPING = {}      # (start, end, source) -> the thread computing wrapped() for the page
WRAP_MOOD = {"edge": 100, "conductor": 60, "focus": 60}   # type -> 5 h use that puts the mascot at the limit or at work; else calm


def wrap_type(w):
    ok = (sum(w["limits"]["hits"].values()) >= EDGE_HITS, w["subagents"]["cost"] >= CONDUCTOR * w["cost"] > 0,
          w["night"] >= NIGHT_OWL, bool(w["projects"]) and w["projects"][0][2] >= FOCUS,
          bool(w["days"]) and sum(map(bool, w["days"])) >= STEADY * len(w["days"]))
    return next((k for k, y in zip(WRAP_TYPES, ok) if y), "explorer")


def limit_moments(a, b):
    """Limits log in [a, b): times a window reached 100 % (per account), the highest weekly reading, account switches."""
    hits, prev, rise = {"five_hour": 0, "seven_day": 0}, {}, {}
    rows = [h for h in limit_history((time.time() - a) / 86400 + 1, ALL) if h["t"] < b]
    for h in rows:
        for k in hits:
            v, p = h.get(k), prev.get((h.get("a", ""), k))
            if v is None:
                continue
            hits[k] += (p or 0) < 100 <= v
            if k == "five_hour" and p is not None and v > p:
                rise[h["t"]] = max(rise.get(h["t"], (0, "")), (v - p, h.get("a", "")))
            prev[(h.get("a", ""), k)] = v
    # ponytail: the account in use is not logged; the one whose 5 h use rose most per reading counts as in use
    seq = [x for _, (_, x) in sorted(rise.items())]
    tight = max(((h["seven_day"], h["t"], h.get("a", "")) for h in rows if h.get("seven_day") is not None), key=lambda x: x[0], default=None)
    return dict(hits=hits, tightest=tight and dict(used=tight[0], t=tight[1], a=tight[2]),
                switches=sum(x != y for x, y in zip(seq, seq[1:])))


def wrapped(start, end, ttl=WRAP_TTL):
    """Everything the Wrapped slides show for the dates [start, end), a month or a year; a running one is `partial` and
    is recomputed when its cache is older than ttl seconds. prev = the same for the month or year before,
    None without sessions there."""
    ps = start.replace(year=start.year - 1) if (end - start).days > 31 else (start - timedelta(days=1)).replace(day=1)
    p = wrap_period(ps, start)
    return {**wrap_period(start, end, ttl), "prev": p if p["sessions"] else None}


def wrap_period(start, end, ttl=WRAP_TTL):
    """wrapped() without prev. Ranked lists are top first, [name, value, share]; messages, hours (24 counts by local
    hour), days (count per day so far) and the streak count chat messages, no cron or subagent runs."""
    key = (start, end, SET["source"])
    hit = WRAP.get(key)
    if hit and (not hit[1]["partial"] or time.time() - hit[0] < ttl):
        return hit[1]
    a, b = (datetime.combine(x, datetime.min.time()).timestamp() for x in (start, end))
    d = data_for(f"{start}..{end - timedelta(days=1)}")
    tot, sess = d["total"], d["sess"]
    chats = [x for x in sess.values() if x["src"] not in NOT_CHAT]
    hours, days = [0] * 24, [0] * max(0, (min(end, datetime.now().date() + timedelta(days=1)) - start).days)
    for x in chats:
        for t, *_ in x["turns"]:
            lt = datetime.fromtimestamp(t)
            hours[lt.hour] += 1
            if 0 <= (lt.date() - start).days < len(days):
                days[(lt.date() - start).days] += 1
    streak = run = 0
    for n in days:
        run = run + 1 if n else 0
        streak = max(streak, run)
    top = lambda c, of: [[k, v, v / of if of else 0.0] for k, v in sorted(c.items(), key=lambda kv: -kv[1])[:5]]
    proj, tools, models, subs = defaultdict(float), Counter(), defaultdict(float), [x for x in sess.values() if x["src"] == "subagent"]
    for x in sess.values():
        if x["project"]:
            proj[x["project"]] += x["cost"]
    for k, n in d["uses"].items():
        if k.startswith(("tool:", "skill:")):
            tools[k[5:] if k[0] == "t" else "skill_view"] += n
    for m, (_, _, c) in d["models"].items():
        models[model_name(m)] += c
    big = max(sess.items(), key=lambda kv: kv[1]["tokens"], default=None)
    starts = [x["started"] for x in chats if a <= x["started"] < b]
    late = lambda t: ((datetime.fromtimestamp(t).hour - NIGHT_END) % 24, datetime.fromtimestamp(t).minute)
    w = dict(start=start.isoformat(), end=end.isoformat(), partial=b > time.time(), tokens=round(d["tokens"]), cost=tot,
             sessions=len(sess), messages=sum(hours), projects=top(proj, tot), tools=top(tools, sum(tools.values())),
             models=top(models, tot), hours=hours, peak_hour=hours.index(max(hours)) if any(hours) else None,
             latest=max(starts, key=late, default=None), night=sum(hours[:NIGHT_END]) / (sum(hours) or 1), days=days,
             streak=streak, biggest=big and dict(id=big[0], title=big[1]["title"], agent=big[1].get("agent", ""),
                                                 tokens=round(big[1]["tokens"]), cost=big[1]["cost"]),
             limits=limit_moments(a, b),
             subagents=dict(n=len(subs), cost=sum(x["cost"] for x in subs), models=top(Counter(x["models"][0] for x in subs if x["models"]), len(subs))))
    w["type"] = wrap_type(w)
    WRAP[key] = (time.time(), w)   # ponytail: finished ones kept until a restart; they only change if Hermes rewrites old rows
    return w


def wrap_span(q):
    """(start, end) dates of ?m=YYYY-MM or ?y=YYYY (this month without either), None for anything else."""
    m, y = (q.get(k, [""])[0] for k in ("m", "y"))
    try:
        s = datetime.strptime(y, "%Y").date() if y else datetime.strptime(m or f"{datetime.now():%Y-%m}", "%Y-%m").date()
    except ValueError:
        return None
    if not 2000 <= s.year < 2100:
        return None
    return s, s.replace(year=s.year + 1) if y else (s + timedelta(days=32)).replace(day=1)


def wrap_months():
    """{"YYYY-MM"} of the months with Claude sessions in the analysed homes: one query per home, no analysis."""
    out = set()
    for _, h in homes():
        with at_home(h):
            try:
                c = connect()
                out |= {m for (m,) in c.execute("""select distinct strftime('%Y-%m', started_at, 'unixepoch', 'localtime') from sessions
                    where id in (select session_id from session_model_usage where model like 'claude%')""") if m}
                c.close()
            except sqlite3.Error:
                pass
    return out


def wrap_links(today):
    """The history page's row "Wrapped: Oct · Nov · 2026": every month and year with sessions links to its slides."""
    ms = sorted(wrap_months())
    mon = lambda m: LOC[lang()]["months"][int(m[5:]) - 1] + ("" if m[:4] == str(today.year) else f" {m[:4]}")
    links = [(f"m={m}", mon(m)) for m in ms] + [(f"y={y}", y) for y in sorted({m[:4] for m in ms})]
    return f'<p class="hint">{tr("wrap.title")}: ' + " · ".join(f'<a href="/wrapped?{q}">{t}</a>' for q, t in links) + "</p>" if ms else ""


def recap_month(today):
    """Last month as YYYY-MM on days 1-7 of a month (the overview offers its Wrapped then), else None."""
    return f"{today - timedelta(days=today.day):%Y-%m}" if today.day <= 7 else None


def wrap_ready(start, end):
    """wrapped() when it is cached or done within 2 s, else None while a thread computes it (a cold month takes 10-20 s)."""
    k = (start, end, SET["source"])
    with LOCK:
        t = WRAPPING.get(k)
        if not (t and t.is_alive()):
            t = WRAPPING[k] = threading.Thread(target=wrapped, args=(start, end), daemon=True)
            t.start()
    t.join(2)
    return None if t.is_alive() else wrapped(start, end)


def wbars(items):
    """Ranked bars on a slide: (name, value text, size), top first; widths relative to the top one."""
    top = max((s for *_, s in items), default=0) or 1
    return '<ul class="wb">' + "".join(f'<li><span>{e(k)}</span><b>{v}</b><i style="--w:{max(2, s / top * 100):.0f}%;--d:{800 + 90 * j}ms"></i></li>'
                                       for j, (k, v, s) in enumerate(items)) + "</ul>"


def wdays(days, start, yr):
    """Active days as dots: a month in rows of 7, a year as one row of 31 per month."""
    cells, k, n = [], 0, max(1, sum(map(bool, days)))
    for i, c in enumerate(days):
        if yr and i and (start + timedelta(days=i)).day == 1:
            cells += ['<i class="x"></i>'] * (-len(cells) % 31)
        k += bool(c)
        cells.append(f'<i class="a" style="--d:{800 + 700 * k // n}ms"></i>' if c else "<i></i>")
    return f'<div class="wd{" y" if yr else ""}">{"".join(cells)}</div>'


def page_wrapped(q):
    """/wrapped?m=YYYY-MM or ?y=YYYY: the period as full-screen story slides (static/wrapped.js moves and animates them)."""
    span = wrap_span(q)
    if not span:
        return None
    start, end = span
    yr, T, w = (end - start).days > 31, lambda k, **kw: tr("wrap." + k, **kw), wrap_ready(start, end)
    name = str(start.year) if yr else f'{LOC[lang()]["wrap.months"][start.month - 1]} {start.year}'
    big = lambda v, unit="": f'<div class="big"><b class="up">{v}</b><span>{unit}</span></div>'
    word = lambda v: f'<div class="big wt">{e(v)}</div>'
    pv = w and w["prev"]   # the month or year before: its short name (or year) ends the comparison line
    pl = pv and (pv["start"][:4] if yr else LOC[lang()]["months"][int(pv["start"][5:7]) - 1])
    cmp = lambda cur, prev: f'<p class="cmp">{T("vs", d=signed(cur, prev), p=pl)}</p>' if pv and prev else ""
    of = lambda key, name: pv and {k: v for k, v, _ in pv[key]}.get(name)   # the same project/tool last period
    say = lambda *x: "".join(f'<p class="say">{s}</p>' for s in x if s)
    S = []
    if w is None:
        S.append(word(T("loading")) + say(T("loading.why")))
    elif not w["sessions"]:
        S.append(word(T("empty")) + say(T("empty.why")))
    else:
        S.append(f'<p class="wk">{T("total.k")}</p>' + big(num(w["tokens"]), T("tokens")) + cmp(w["tokens"], pv and pv["tokens"])
                 + say(T("totals", cost=cash(w["cost"]), sessions=cnt(w["sessions"]), messages=cnt(w["messages"]))))
        if w["projects"]:
            S.append(f'<p class="wk">{T("project.k")}</p>' + word(proj_label(w["projects"][0][0])) + cmp(w["projects"][0][1], of("projects", w["projects"][0][0])) + say(T("project", share=pc(w["projects"][0][2] * 100)))
                     + wbars([(proj_label(k), pc(s * 100), s) for k, _, s in w["projects"]]))
        if w["tools"]:
            t0 = w["tools"][0]
            S.append(f'<p class="wk">{T("tool.k")}</p>' + word(t0[0]) + cmp(t0[1], of("tools", t0[0])) + say(T("tool", n=cnt(t0[1]), share=pc(t0[2] * 100)))
                     + wbars([(k, cnt(n), n) for k, n, _ in w["tools"][:3]])
                     + (f'<p class="wk wsub">{T("models")}</p>' + wbars([(k, pc(s * 100), s) for k, _, s in w["models"][:3]]) if w["models"] else ""))
        if w["peak_hour"] is not None:
            mx, hs = max(w["hours"]), w["hours"]
            heat = "".join(f'<i style="--d:{800 + 25 * h}ms;background:var(--h{math.ceil(c / mx * 4)})"></i>' for h, c in enumerate(hs))
            axis = "".join(f'<span>{tr("fmt.hour", h=h)}</span>' for h in (0, 6, 12, 18))
            S.append(f'<p class="wk">{T("peak.k")}</p>' + word(tr("fmt.hour", h=w["peak_hour"]))
                     + say(T("night", share=pc(w["night"] * 100)) + (" " + T("latest", t=when(w["latest"], "short")) if w["latest"] else ""))
                     + f'<div class="wh">{heat}</div><div class="axis">{axis}</div>')
        if any(w["days"]):
            b = w["biggest"]
            S.append(f'<p class="wk">{T("streak.k")}</p>' + big(cnt(w["streak"]), T("day" if w["streak"] == 1 else "days")) + cmp(w["streak"], pv and pv["streak"])
                     + say(T("active", n=cnt(sum(map(bool, w["days"]))), of=cnt(len(w["days"]))))
                     + wdays(w["days"], start, yr)
                     + say(b and T("biggest", title=e(b["title"] or tr("untitled")), tokens=num(b["tokens"]), cost=cash(b["cost"]))))
        L = w["limits"]
        if L["tightest"]:
            S.append(f'<p class="wk">{T("limits.k")}</p>' + big(cnt(sum(L["hits"].values())), T("times")) + cmp(sum(L["hits"].values()), pv and sum(pv["limits"]["hits"].values()))
                     + say(T("hits", five=cnt(L["hits"]["five_hour"]), week=cnt(L["hits"]["seven_day"])),
                           T("tightest", used=pc(L["tightest"]["used"]), day=when(L["tightest"]["t"], "day"))
                           + (" " + T("switches", n=cnt(L["switches"])) if L["switches"] else "")))
        if w["subagents"]["n"]:
            sa = w["subagents"]
            S.append(f'<p class="wk">{T("subs.k")}</p>' + big(cnt(sa["n"]), T("subs.unit")) + cmp(sa["n"], pv and pv["subagents"]["n"])
                     + say(T("subs", cost=cash(sa["cost"]), share=pc(sa["cost"] / (w["cost"] or 1) * 100)))
                     + wbars([(k, cnt(n), n) for k, n, _ in sa["models"][:3]]))
        S.append(f'<p class="wk">{T("type.k")}</p>' + word(tr("wrap.type." + w["type"])) + say(tr("wrap.type." + w["type"] + ".why"))
                 + f'<div class="wpet">{pet_html([("five_hour", WRAP_MOOD.get(w["type"], 20), None)], True)}</div>')
    n = len(S)
    slides = "".join(f'<section class="ws" id="s{i}" aria-label="{i + 1} / {n}"><nav class="tz"><a class="prev" href="#s{max(i - 1, 0)}" aria-label="{T("back")}"></a>'
                     f'<a class="next" href="#s{min(i + 1, n - 1)}" aria-label="{T("next")}"></a></nav>{x}</section>' for i, x in enumerate(S))
    prog = "".join(f'<i{" class=on" if not i else ""}></i>' for i in range(n))
    title = name + (f' · {T("sofar")}' if end > datetime.now().date() else "")
    return (head(f'{T("title")} · {name}', "" if w else '<meta http-equiv="refresh" content="3">')
            + f'<body class="wr"><div class="wcol"><div class="prog" aria-hidden="true">{prog}</div>'
            f'<nav class="whead" aria-label="{T("title")}"><span>{title}</span><a class="wx" href="/" aria-label="{T("close")}">'
            f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></a></nav>'
            f'<div class="story" data-sep="{tr("num.sep")}">{slides}</div></div>'
            f'<script type="module" src="/wrapped.js?v={int((STATIC / "wrapped.js").stat().st_mtime)}"></script></body></html>')


def page_overview(p):
    d, d30, snap = data_for(p), data_for("30"), load_snap()
    w = d if p == "w" else data_for("w")
    tot, ttl, now = d["total"] or 1, d["ttl"], time.time()
    active = {min(int((now - h) // 86400), 29) for h, v in d30["hours"].items() if sum(v.values()) > 0}   # 24-h slices: 30, not 31 dates
    sums = {"1": window_sum(d30, now - 86400), "7": window_sum(d30, now - 7 * 86400), "30": d30["total"]}
    dl = lambda v, since: f'<small class="d">{x}</small>' if (x := week_delta(d30, v, since, now)) else ""
    kpis = [("main", pname(p), money(d["total"]), tr("kpi.main.sub", tokens=num(d["tokens"]), steps=cnt(d["calls"]))
             + (dl(d["total"], d["since"]) if p in ("w", "7") else ""))]
    kpis += [("", tr("period." + k), money(sums[k]), tr("kpi.api") + (dl(sums[k], now - 7 * 86400) if k == "7" else ""))
             for k in [k for k in ("7", "30", "1") if k != p][:2]]
    kpis += [("", tr("kpi.avg"), money(d30["total"] / max(len(active), 1)), tr("kpi.avg.sub", n=len(active))),
             ("", tr("kpi.sessions"), cnt(len(d["sess"])), tr("kpi.sessions.sub"))]
    kpi_html = "".join(f'<div class="kpi {c}"><span>{l}</span><b>{v}</b><small>{s}</small></div>' for c, l, v, s in kpis)
    tip_html = "".join(f'<div class="tip"><b>{e(t)}</b><p>{txt}</p><span class="gain">{tr("tip.saves", p=pct(sv, tot))}</span></div>'
                       for sv, t, txt in all_tips(d)) \
        or f'<p class="hint">{tr("ov.nothing")}</p>'
    models = sorted(d["models"].items(), key=lambda x: -x[1][2])
    models_html = f'<section class="card"><h2>{tr("ov.models")}</h2><ol class="rank">' + "".join(
        f'<li><span class="n">{i}</span><span class="m">{brk(m)}{"" if known_model(m) else " <small>" + tr("ov.estimated") + "</small>"}</span><span class="v">{money(x[2])}</span><b>{pct(x[2], tot)}</b></li>'
        for i, (m, x) in enumerate(models, 1)) + "</ol></section>"
    if len(models) < 2 and all(known_model(m) for m, _ in models):
        models_html = ""   # one model at 100 % says nothing
    bots = by_bot(d["sess"])

    def fold(k):   # a bot row opens to its top chats, each leading to its sessions
        chats = sorted(bots[k][1].items(), key=lambda c: -c[1][0])
        labs = [chat_label(ch) for _, (_, ch, _) in chats[:8]]
        one = len({cx for _, cx in labs}) == 1   # every topic in the same DM: saying so on each line is noise
        lis = "".join(f'<li><a href="{link("/sessions", p=p, bot=k, chat=ck)}"><span>{e(nm)}{f" <small>{e(cx)}</small>" if cx and not one else ""}</span>'
                      f'<span class="v">{amount(v, tot)}</span></a></li>' for (ck, (v, _, _)), (nm, cx) in zip(chats, labs))
        more = (f'<p class="hint"><a href="{link("/sessions", p=p, view="cron") if k.endswith(":cron") else link("/sessions", p=p, bot=k)}">'
                f'{tr("ov.bots.all", n=cnt(len(chats)))}</a></p>' if len(chats) > 8 else "")
        return bot_label(k), f'<ul class="chats">{lis}</ul>{more}'
    bots_html = (f'<section class="card"><h2>{tr("ov.bots")}</h2>'
                 f'{bars(sorted(((k, v[0]) for k, v in bots.items()), key=lambda x: -x[1]), tot, ttl, n=20, soft=True, name_of=fold, raw=True)}</section>')
    logged = tr("ov.logged", p=pc(d["logged"] / (d["n_steps"] or 1) * 100))
    push = "" if push_on() else f'<p class="hint pushoff">{tr("ov.push", url="/settings/notify")}</p>'
    split = limit_split(limit_history(), d30["hours"], cc_calls(w["since"]), w["since"]) if w["week"] else None
    now_live = live()
    rm = recap_month(datetime.now().date())
    recap = f'<p class="note"><a href="/wrapped?m={rm}">{tr("ov.wrap", month=LOC[lang()]["wrap.months"][int(rm[5:]) - 1])}</a></p>' if rm and rm in wrap_months() else ""
    body = f"""{limits_card(w["total"] if w["week"] else None, split)}{live_card(now_live, p)}{guard_note()}{push}{recap}
<div class="kpis">{kpi_html}</div>
<div class="grid g2">
<section class="card"><h2>{tr("ov.eats")}</h2>
{bars(ranked(d["comp"]), tot, ttl, n=10, p=p)}
<details class="more"><summary>{tr("det.how")}</summary><p class="why">{tr("ov.eats.hint")}</p><p class="why">{logged}</p></details></section>
<div class="col">{bots_html}{models_html}</div></div>
<section class="sec"><h2>{tr("ov.save")}</h2>
<p class="hint">{tr("ov.save.hint")}</p>
<div class="tips">{tip_html}</div></section>"""
    return layout("Usagecast", "/", p, tr("ov.h1"), tr("sub.calc", src=source_name(), period=period_text(d), at=hm(d["at"])), body)


WATCH = (("model", "default"), ("model", "provider"), ("agent", "reasoning_effort"), ("prompt_caching", "cache_ttl"),
         ("memory", "provider"))   # config values that change what a step costs, plus the plugin list


def read_json(f):
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def read_jsonl(f):
    try:
        return [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def watch_config():
    """Appends the cost-relevant config to data/config.jsonl whenever it changed (the first line is the baseline)."""
    text = config_text()
    cfg = {f"{s}.{k}": config_value(s, k, text) for s, k in WATCH}
    cfg["plugins"] = ", ".join(plugin_names(text))
    rows = read_jsonl(DATA / "config.jsonl")
    last = rows[-1]["cfg"] if rows else None
    if cfg != last:
        changes = {k: [last.get(k, ""), v] for k, v in cfg.items() if last.get(k, "") != v} if last else {}
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "config.jsonl", "a") as fh:
            fh.write(json.dumps({"t": round(time.time()), "cfg": cfg, "changes": changes}) + "\n")


def change_text(k, a, b):
    if k == "plugins":
        old, new = set(filter(None, a.split(", "))), set(filter(None, b.split(", ")))
        return "plugins " + " ".join([*(f"+{x}" for x in sorted(new - old)), *(f"−{x}" for x in sorted(old - new))])
    return f"{k.split('.')[-1]} {a or '–'} → {b or '–'}"


def events(since, until=math.inf):
    """[(time, text)]: Hermes updates (pull/merge in the install's git reflog) and the config changes watch_config saw."""
    out = []
    try:
        r = subprocess.run(["git", "-C", str(HERMES / "hermes-agent"), "reflog", "--date=unix", "--format=%gd %gs"],
                           capture_output=True, text=True, timeout=10)
        out += [(int(m[1]), tr("ev.update")) for m in re.finditer(r"@\{(\d+)\} (?:pull|merge|rebase \(finish\))", r.stdout)]
    except (OSError, subprocess.TimeoutExpired):
        pass
    out += [(x["t"], tr("ev.config", what=", ".join(change_text(k, a, b) for k, (a, b) in x["changes"].items())))
            for x in read_jsonl(DATA / "config.jsonl") if x.get("changes")]
    return sorted(x for x in out if since <= x[0] < until)


def per_step(d30, a, b):
    """Average API value per step between a and b; None below 20 steps."""
    a = round(a / 3600) * 3600
    steps = sum(n for h, n in d30["hsteps"].items() if a <= h < b)
    return span_sum(d30, a, b) / steps if steps >= 20 else None


def event_list(d, d30):
    now, rows = time.time(), ""
    for t, text in events(d["since"], min(d["until"], now)):
        a, b = per_step(d30, t - 7 * 86400, t), per_step(d30, t, min(t + 7 * 86400, now))
        cmp = tr("ev.cmp", a=money(a), b=money(b), d=signed(b, a)) if a and b else tr("ev.early")
        rows += f'<li><b>{when(t, "daytime")}</b> {e(text)}<span class="why">{cmp}</span></li>'
    return f'<p class="hint">{tr("ev.hint")}</p><ul class="evs">{rows}</ul>' if rows else ""


def stacked(d):
    """Stacked bars per day (per hour for short periods): the biggest components in color, everything else as rest."""
    hourly, ttl = d["hourly"], d["ttl"]
    top = [k for k, _ in ranked(d["comp"])[:COLORS]]
    cols = defaultdict(lambda: defaultdict(float))
    for h, comp in d["hours"].items():
        b = floor(h, hourly)
        for k, v in comp.items():
            cols[b][group(k) if group(k) in top else "rest"] += v
    keys, t, step = [], floor(d["since"], hourly), 3600 if hourly else 86400
    while t <= min(time.time(), d["until"] - 1):
        keys.append(t)
        t = floor(t + step + (0 if hourly else 7200), hourly)  # +2 h survives daylight saving changes
    if not keys or not d["total"]:
        return f'<p class="hint">{tr("nodata")}</p>'
    names = [label(k, ttl)[0] for k in top] + [tr("rest")]
    tl = lambda k: tr("fmt.hour", h=datetime.fromtimestamp(k).hour) if hourly else when(k, "day")
    sums = [sum(cols[k].values()) for k in keys]
    mx, w, svg = max(sums), 100 / len(keys), ""
    bw = min(w * .66, 4.5)  # no blocks when there are only a few days
    for i, (k, s) in enumerate(zip(keys, sums)):
        y, rects, tip = 100.0, "", f"{tl(k)}: {money(s)}"
        for j, c in enumerate(top + ["rest"]):
            v = cols[k].get(c, 0.0)
            if v > 0:
                y -= v / mx * 96
                rects += f'<rect class="c{j}" x="{i * w + (w - bw) / 2:.2f}" y="{y:.2f}" width="{bw:.2f}" height="{v / mx * 96:.2f}"/>'
                tip += f"\n{names[j]}: {money(v)}"
        svg += f'<g><title>{e(tip)}</title><rect class="hit" x="{i * w:.2f}" y="0" width="{w:.2f}" height="100"/>{rects}</g>'
    step = 3600 if hourly else 86400
    for t, text in events(keys[0], min(d["until"], time.time())):   # dashed line per update / config change
        i = max(bisect.bisect_right(keys, t) - 1, 0)
        x = (i + min((t - keys[i]) / step, 1)) * w
        svg += f'<line class="ev" x1="{x:.2f}" x2="{x:.2f}" y1="0" y2="100"><title>{e(when(t, "daytime") + " · " + text)}</title></line>'
    peak = max(range(len(keys)), key=lambda i: sums[i])
    ax = [tl(keys[i]) for i in (0, len(keys) // 2, -1)]
    legend = "".join(f'<span><span class="dot c{j}"></span>{brk(n)}</span>' for j, n in enumerate(names))
    return (f'<svg class="chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" '
            f'aria-label="{tr("hist.per.hour" if hourly else "hist.per.day")}">{svg}</svg>'
            f'<div class="axis"><span>{ax[0]}</span><span>{ax[1]}</span><span>{ax[2]}</span></div>'
            f'<div class="lg">{legend}</div><p class="hint">{tr("hist.peak", at=tl(keys[peak]), v=money(sums[peak]))}</p>')


def heatmap(d):
    heat, wd_tot = defaultdict(float), defaultdict(float)
    for h, comp in d["hours"].items():
        lt = time.localtime(h)
        heat[(lt.tm_wday, lt.tm_hour)] += sum(comp.values())
        wd_tot[lt.tm_wday] += sum(comp.values())
    if not heat:
        return f'<p class="hint">{tr("nodata")}</p>'
    wds, mx, cells = LOC[lang()]["weekdays"], max(heat.values()) or 1, ""
    for wd in range(7):
        cells += f"<span>{wds[wd]}</span>"
        for hr in range(24):
            v = heat.get((wd, hr), 0.0)
            lvl = 1 + min(int(math.sqrt(v / mx) * 4), 3) if v > 0 else 0  # square root, so small values stay visible
            cells += f'<i class="h{lvl}" title="{tr("heat.cell", wd=wds[wd], h=hr, h2=hr + 1, v=money(v))}"></i>'
    cells += "<span></span>" + "".join(f'<span class="hx">{tr("fmt.hour", h=hr)}</span>' for hr in (0, 6, 12, 18))
    (pw, ph), pv = max(heat.items(), key=lambda x: x[1])
    bw = max(wd_tot, key=wd_tot.get)
    scale = "".join(f'<i class="h{i}"></i>' for i in range(5))
    return (f'<div class="heat">{cells}</div><div class="scale">{tr("heat.less")}{scale}{tr("heat.more")}</div>'
            f'<p class="hint">{tr("heat.peak", wd=wds[pw], h=ph, h2=ph + 1, v=money(pv), bwd=wds[bw], bv=money(wd_tot[bw]))}</p>')


CAL = {}   # day_costs() cache


def day_costs():
    """{local date: $} for the last year, cached 10 min. Cheap on purpose: each session's API cost is spread over
    its days by message count (sessions without messages go to their start day)."""
    # ponytail: spread by messages, not by call time; analyze() over a year would take far too long
    if time.time() - CAL.get("at", 0) < 600:
        return CAL["days"]
    since, ttl, c = time.time() - 371 * 86400, cache_ttl(), connect()
    cost, spread, days = defaultdict(float), defaultdict(dict), defaultdict(float)
    for sid, model, i, r, w, o in c.execute("""select session_id, model, input_tokens, cache_read_tokens, cache_write_tokens,
            output_tokens from session_model_usage where model like 'claude%'"""):
        cost[sid] += sum(row_cost(model, i, r, w, o, ttl))
    for sid, day, n in c.execute("""select session_id, date(timestamp, 'unixepoch', 'localtime'), count(*) from messages
            where timestamp >= ? group by 1, 2""", (since,)):
        spread[sid][day] = n
    first = datetime.fromtimestamp(since).date().isoformat()
    start = {sid: day for sid, day in c.execute("select id, date(started_at, 'unixepoch', 'localtime') from sessions") if day and day >= first}
    for sid, v in cost.items():
        sp = spread.get(sid) or ({start[sid]: 1} if sid in start else {})
        n = sum(sp.values())
        for day, k in sp.items():
            days[day] += v * k / n
    CAL.update(at=time.time(), days=days)
    return days


def calendar(days, today=None):
    """GitHub-style year: one column per week, Monday at the top, a month name over its first Monday."""
    today = today or datetime.now().date()
    first = datetime.fromisoformat(min((k for k, v in days.items() if v > 0), default=today.isoformat())).date()
    d = max(today - timedelta(days=today.weekday() + 52 * 7), first - timedelta(days=first.weekday()))
    old = today - timedelta(days=today.weekday() + 25 * 7)   # phones show the last 26 weeks
    weeks = (today - d).days // 7 + 1
    shown, out, mx = {}, "", max(days.values(), default=0) or 1
    while d <= today:
        x = " x" if d < old else ""
        if d.weekday() == 0:
            out += f'<span{x and " class=x"}>{LOC[lang()]["months"][d.month - 1] if d.day <= 7 or not out else ""}</span>'
        v = shown[d] = days.get(d.isoformat(), 0.0)
        lvl = 1 + min(int(math.sqrt(v / mx) * 4), 3) if v > 0 else 0   # square root, so small days stay visible
        out += f'<i class="h{lvl}{x}" title="{tr("cal.cell", day=when(datetime(d.year, d.month, d.day).timestamp(), "day"), v=money(v))}"></i>'
        d += timedelta(days=1)
    active = [x for x in shown.items() if x[1] > 0]
    if not active:
        return f'<p class="hint">{tr("nodata")}</p>'
    bd, bv = max(active, key=lambda x: x[1])
    scale = "".join(f'<i class="h{i}"></i>' for i in range(5))
    return (f'<div class="cal" role="img" aria-label="{tr("cal.title")}" style="max-width:{weeks * 22}px">{out}</div>'
            f'<div class="scale">{tr("heat.less")}{scale}{tr("heat.more")}</div>'
            f'<p class="hint">{tr("cal.sum", n=len(active), total=len(shown), day=when(datetime(bd.year, bd.month, bd.day).timestamp(), "day"), v=money(bv))}</p>')


def overlay(w, d30):
    """Cumulative API value through the limit week: this week over the three before, all from the same start."""
    now, wk = time.time(), 7 * 86400
    hrs, lines, top = sorted(d30["hours"].items()), [], 0.0
    for k in range(4):
        s, acc, pts = round(w["since"] / 3600) * 3600 - k * wk, 0.0, [(0.0, 0.0)]
        for h, comp in hrs:
            if s <= h < min(s + wk, now):
                acc += sum(comp.values())
                pts.append((min((h + 3600 - s) / wk, 1.0), acc))
        lines.append(pts)
        top = max(top, acc)
    if not top or len(lines[1]) < 2:
        return f'<p class="hint">{tr("nodata")}</p>'
    svg = "".join(f'<polyline class="w{k}" points="{" ".join(f"{x * 100:.2f},{100 - v / top * 96:.2f}" for x, v in pts)}"/>'
                  for k, pts in reversed(list(enumerate(lines))))
    days = "".join(f"<span>{LOC[lang()]['weekdays'][datetime.fromtimestamp(w['since'] + i * 86400).weekday()]}</span>" for i in range(7))
    same = span_sum(d30, w["since"] - wk, now - wk)
    cur = lines[0][-1][1]
    note = tr("ovl.now", cur=money(cur), prev=money(same), d=signed(cur, same)) if same > 0.01 else ""
    return (f'<svg class="ovl" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="{tr("ovl.title")}">{svg}</svg>'
            f'<div class="axis days">{days}</div><div class="lg"><span><span class="dot c0"></span>{tr("ovl.this")}</span>'
            f'<span><span class="dot n"></span>{tr("ovl.before")}</span></div>' + (f'<p class="hint">{note}</p>' if note else ""))


def page_history(p):
    d, d30 = data_for(p), data_for("30")
    tot, ttl = d["total"] or 1, d["ttl"]
    rows = []
    for t, (comp, steps, ss) in sorted(per_day(d).items(), reverse=True):
        c = sum(comp.values())
        if c > 0:
            rows.append((when(t, "day"), money(c), pct(c, tot), cnt(steps), cnt(len(ss)),
                         f'<span class="why">{e(label(max(comp.items(), key=lambda x: x[1])[0], ttl)[0])}</span>'))
    body = f"""{wrap_links(datetime.now().date())}<section class="card"><div class="row"><h2>{tr("hist.per.hour" if d["hourly"] else "hist.per.day")}</h2>
<span class="v">{money(d["total"])}</span></div>
<p class="hint">{tr("hist.hint")}</p>
{stacked(d)}{event_list(d, d30)}</section>
<section class="card"><h2>{tr("hist.when")}</h2>
<p class="hint">{tr("hist.when.hint")}</p>{heatmap(d30)}</section>
<section class="card"><h2>{tr("ovl.title")}</h2>
<p class="hint">{tr("ovl.hint")}</p>{overlay(data_for("w"), d30)}</section>
<section class="card"><h2>{tr("cal.title")}</h2>
<p class="hint">{tr("cal.hint")}</p>{calendar(day_costs())}</section>
<section class="sec"><h2>{tr("hist.days")}</h2>
{table(["th.day", "th.cost", "th.share", "th.steps", "th.sessions", "th.top"], rows, ("", "", "o", "", "o", "l o"))}</section>"""
    return layout(tr("nav.history") + " · Usagecast", "/history", p, tr("nav.history"), tr("sub.hermes", src=source_name(), period=period_text(d)), body)


def page_details(p):
    d, snap = data_for(p), load_snap()
    tot, comp, uses, sizes = d["total"] or 1, d["comp"], d["uses"], d["sizes"]
    avg = lambda k: num(sizes[k][1] / sizes[k][0]) if sizes.get(k) and sizes[k][0] else "–"
    tools = sorted(((k, v) for k, v in ranked(comp) if k.startswith("tool:")), key=lambda x: -x[1])
    skills = sorted(((k, v) for k, v in comp.items() if k.startswith("skill:")), key=lambda x: -x[1])
    inj = sorted(((k, v) for k, v in comp.items() if k.startswith("inj:")), key=lambda x: -x[1])
    sysp = sorted(((k, v) for k, v in comp.items() if k.startswith("sys:")), key=lambda x: -x[1])
    tasks = sorted(((k, v) for k, v in comp.items() if k.startswith("task:") or k in ("sub", "other", "rebuild", "break", "think", "reply", "user")), key=lambda x: -x[1])
    st = sum(snap.get("tools", {}).values()) or 1
    schemas = sorted(snap.get("tools", {}).items(), key=lambda x: -x[1])
    skill_uses = sum(n for k, n in uses.items() if k.startswith("skill:"))
    trow = lambda k, v: (brk(tr("det.skill_view") if k == "tool:skill_view" else k[5:]),
                         cnt(skill_uses if k == "tool:skill_view" else uses.get(k, 0)),
                         "–" if k == "tool:skill_view" else avg(k), money(v), pct(v, tot))
    how = tr("det.how.text", ttl=ttl_text(d["ttl"]), p=pc(d["logged"] / (d["n_steps"] or 1) * 100))
    ls = long_lines(d)
    how += " " + tr("det.long", lo=nf(LONG_CTX), hi=nf(2 * LONG_CTX), lines=", ".join(
        (agent_label(a) + " " if len(ls) > 1 else "") + nf(t) + (" " + tr("det.long.learned", n=nf(m)) if m != t else "") for a, t, m in ls))
    hv = [(f'{brk(tool)}<span class="why">{when(t, "short")}</span>',
           f'<a href="/s/{quote(sid)}?p={quote(p)}">{e((d["sess"].get(sid, {}).get("title") or tr("untitled"))[:70])}</a>',
           num(n), tr("det.heavy.steps", n=cnt(later)), money(c)) for c, sid, tool, n, later, t in d["heavy"]]
    brs = sorted(d["breaks"].items(), key=lambda x: -x[1][1])
    breaks = (f'<section class="sec"><h2>{tr("det.breaks")}</h2><p class="hint">{tr("det.breaks.hint")}</p>'
              + table(["th.cause", "th.breaks", "th.cost", "th.share"],
                      [(f'{tr("brk." + k)}<span class="why">{tr("brk." + k + ".why")}</span>', cnt(n), money(v), pct(v, tot)) for k, (n, v) in brs],
                      ("l", "", "", "o")) + "</section>") if brs else ""
    skills_html = (f'<section class="sec"><h2>{tr("det.skills")}</h2>' + table(["th.skill", "th.loaded", "th.avg_size", "th.cost", "th.share"],
                   [(brk(k[6:]), cnt(uses.get(k, 0)), avg(k), money(v), pct(v, tot)) for k, v in skills], ("", "", "", "", "o")) + "</section>\n") if skills else ""
    plugins_html = (f'<section class="sec"><h2>{tr("det.plugins")}</h2>' + table(["th.plugin", "th.messages", "th.avg_tokens", "th.cost", "th.share"],
                    [(brk(seg_label(k[4:])), cnt(sizes.get(k, [0])[0]), avg(k), money(v), pct(v, tot)) for k, v in inj], ("", "", "", "", "o")) + "</section>\n") if inj else ""
    body = f"""<section><h2>{tr("det.tools")}</h2>
<p class="hint">{tr("det.tools.hint")}</p>
{table(["th.tool", "th.calls", "th.avg_return", "th.cost", "th.share"], [trow(k, v) for k, v in tools], ("", "", "", "", "o"))}</section>
<section class="sec"><h2>{tr("det.heavy")}</h2>
<p class="hint">{tr("det.heavy.hint")}</p>
{table(["th.result", "th.session", "th.size", "th.carried", "th.cost"], hv, ("", "l o", "", "o", ""))}</section>
{skills_html}{plugins_html}<section class="sec"><h2>{tr("det.sys")}</h2>
<p class="hint">{tr("det.sys.hint", at=when(snap.get("at", 0), "short"))}</p>
{table(["th.part", "th.tokens_step", "th.cost", "th.share"], [(e(seg_label(k[4:])), num(snap.get("prompt", {}).get(k[4:], 0)), money(v), pct(v, tot)) for k, v in sysp]
       + [(tr("det.schemas_all"), num(st), money(comp.get("schema", 0)), pct(comp.get("schema", 0), tot))], ("", "", "", "o"))}</section>
<section class="sec"><h2>{tr("det.schemas")}</h2>
<p class="hint">{tr("det.schemas.hint")}</p>
{table(["th.tool", "th.tokens_step", "th.calls", "th.cost", "th.share"],
       [(brk(n), num(t), cnt(uses.get("tool:" + n, 0) if n != "skill_view" else skill_uses), money(comp.get("schema", 0) * t / st), pct(comp.get("schema", 0) * t / st, tot)) for n, t in schemas], ("", "", "", "", "o"))}</section>
<section class="sec"><h2>{tr("det.tasks")}</h2>
{table(["th.item", "th.cost", "th.share"], [(e(label(k, d["ttl"])[0]), money(v), pct(v, tot)) for k, v in tasks])}</section>
{breaks}<section class="sec"><h2>{tr("det.how")}</h2>
<p class="hint">{how}</p></section>"""
    return layout(tr("nav.details") + " · Usagecast", "/details", p, tr("nav.details"), tr("sub.details", src=source_name(), period=period_text(d)), body)


def all_jobs():
    """Every cron job of the analysed Hermes homes (dicts from cron/jobs.json), "_agent" = the profile it belongs to."""
    out = []
    for a, h in homes():
        try:
            jobs = json.loads((h / "cron" / "jobs.json").read_text())
            jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
            out += [{**j, "_agent": a} for j in jobs if isinstance(j, dict)]
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    return out


def cron_list():
    """Recurring cron jobs that are not finished; one-shot jobs are left out, they end on their own."""
    return [j for j in all_jobs() if isinstance(j.get("id"), str) and j.get("state") != "completed"
            and (j.get("repeat") or {}).get("times") is None]


def cron_costs(d):
    """{job name: [runs, $]} of the cron sessions in an analysis, and the days it covers."""
    g = defaultdict(lambda: [0, 0.0])
    for x in d["sess"].values():
        if x["origin"].startswith("cron:"):
            a = g[x["origin"][5:]]
            a[0] += 1; a[1] += x["cost"]
    return g, max((min(time.time(), d["until"]) - d["since"]) / 86400, 1 / 24)


def guard_state():
    try:
        return json.loads((DATA / "guard.json").read_text())
    except (OSError, ValueError):
        return {}


def guard_plan(L, now, jobs, state, s=None):
    """Budget guard: (pause, resume) job ids. The marked jobs pause while the week forecast is above 100 % (from the
    second day of the week), the week is at 90 % or the 5-hour window at 85 %. Once none of that holds, or the guard is
    off, or a job is no longer marked, the jobs the guard paused itself come back. jobs: {id: state from jobs.json};
    a job the user paused is never "scheduled", so the guard never takes it over."""
    s = s or SET
    if not L:
        return [], []   # limits unknown: change nothing
    w = {k: (u, frac) for k, u, frac, _, _ in windows(L, now)}
    u, frac = w.get("seven_day", (0.0, None))
    tight = s["guard"] and (u >= 90 or w.get("five_hour", (0.0,))[0] >= 85
                            or bool(frac and frac >= 1 / 7 and (week_forecast(u, frac) or 0) > 100))
    mine = [j for j in state.get("paused", []) if jobs.get(j) == "paused"]
    pause = [j for j in s["guard_jobs"] if jobs.get(j) == "scheduled"] if tight else []
    return pause, [j for j in mine if not tight or j not in s["guard_jobs"]]


def guard_pool(accts, L, now, jobs, state, s=None):
    """guard_plan() for the primary's limits L; with several accounts jobs pause only when every available account
    would pause on its own (the pool rotates to a free one otherwise) and come back as soon as one of them is free."""
    pause, resume = guard_plan(L, now, jobs, state, s)
    av = [a for a in accts if a["avail"]] if len(accts) > 1 else []
    if av:
        plans = [guard_plan(a["limits"], now, jobs, state, s) for a in av]
        pause = [j for j in plans[0][0] if all(j in p for p, _ in plans[1:])]
        resume = list(dict.fromkeys(j for _, r in plans for j in r))
    return pause, resume


def guard():
    """Budget guard step of the 10-minute loop: pauses or resumes the marked jobs through the hermes CLI."""
    jobs, state = {j["id"]: j for j in cron_list()}, guard_state()
    pause, resume = guard_pool(STATE["accts"], STATE["limits"], time.time(), {k: j.get("state") for k, j in jobs.items()}, state)
    held = {j for j in state.get("paused", []) if jobs.get(j, {}).get("state") == "paused"}
    if not pause and not resume and held == set(state.get("paused", [])):
        return
    exe = shutil.which("hermes") or str(HERMES / "hermes-agent" / "venv" / "bin" / "hermes")
    done = {"pause": [], "resume": []}
    for act, ids in (("pause", pause), ("resume", resume)):
        for j in ids:
            try:
                r = subprocess.run([exe, *(["-p", jobs[j]["_agent"]] if jobs.get(j, {}).get("_agent") else []), "cron", act, j], capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    done[act].append(j)
                else:
                    print("guard:", act, j, r.stderr.strip()[-300:], flush=True)
            except (OSError, subprocess.TimeoutExpired) as err:
                print("guard:", act, j, err, flush=True)
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "guard.json").write_text(json.dumps({"paused": sorted((held - set(done["resume"])) | set(done["pause"]))}))
    for act in ("pause", "resume"):
        if done[act]:
            notify(tr("alert.guard"), tr("alert.guard." + act, names=", ".join(jobs[j].get("name") or j for j in done[act])), 2)


def guard_note():
    """Overview line while the guard holds jobs back."""
    jobs = {j["id"]: j for j in cron_list()}
    held = [jobs[j].get("name") or j for j in guard_state().get("paused", []) if jobs.get(j, {}).get("state") == "paused"]
    return f'<p class="hint">{tr("ov.guard", names=e(", ".join(held)))}</p>' if held else ""


def cron_jobs(d, p):
    sched = {j.get("name"): j.get("schedule_display") or (j.get("schedule") if isinstance(j.get("schedule"), dict) else {}).get("display") or ""
             for j in all_jobs()}
    g, days = cron_costs(d)
    rows = sorted(g.items(), key=lambda kv: -kv[1][1])
    s = sum(c for _, (_, c) in rows)
    hint = tr("cron.hint", n=len(rows), cost=money(s), p=pct(s, d["total"]), week=money(s / days * 7))
    return (f'<p class="hint">{hint}</p>'
            + table(["th.cronjob", "th.schedule", "th.runs", "th.per_run", "th.cost", "th.per_week"],
                    [(f'<a href="{link("/sessions", p=p, src="cron", q=n)}">{e(n)}</a>' if n else tr("untitled"),
                      f"<code>{e(sched[n])}</code>" if sched.get(n) else "–", cnt(r), money(c / r), money(c), money(c / days * 7))
                     for n, (r, c) in rows], ("", "l o", "", "o", "", "")))


def page_sessions(p, q):
    d = data_for(p)
    tot = d["total"] or 1
    arg = lambda k: (q.get(k) or [""])[0].strip()
    view, src, bot, chat, proj, term = arg("view"), arg("src"), arg("bot"), arg("chat"), arg("proj"), arg("q")
    sub, cron = tr("sub.hermes", src=source_name(), period=period_text(d)), view == "cron"
    sel = [(s, x) for s, x in d["sess"].items() if (not proj or (x["project"] or NO_PROJ) == proj)
           and (not term or term.lower() in (x["title"] or "").lower()) and (not chat or (x.get("chat") or NOCHAT)["key"] == chat)]
    counts = Counter(bot_key(x) for _, x in sel)
    rows = sorted(((s, x) for s, x in sel if (not src or x["src"] == src) and (not bot or bot_key(x) == bot)), key=lambda r: -r[1]["cost"])
    chips = chip(tr("chip.all"), not src and not bot and not cron, link("/sessions", p=p, chat=chat, proj=proj, q=term)) + "".join(
        chip(f"{e(bot_label(k))}<span>{n}</span>", (k == bot) if not cron else k.endswith(":cron") and bot in ("", k),
             link("/sessions", p=p, view="cron", bot=k) if k.endswith(":cron") else link("/sessions", p=p, bot=k, chat=chat, proj=proj, q=term))
        for k, n in counts.most_common())   # a cron chip opens the per-job table, its rows lead to the runs; old src= links still filter
    hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in (("p", p), ("src", src), ("bot", bot), ("chat", chat), ("proj", proj)) if v)
    search = (f'<form class="search" action="/sessions" role="search">{hidden}<input type="search" name="q" value="{e(term)}" '
              f'placeholder="{tr("ses.search")}" aria-label="{tr("ses.search")}"></form>')
    flt = f'<div class="filters"><nav class="chips" aria-label="{tr("aria.bot")}">{chips}</nav>{search}</div>'
    if cron:
        mine = dict(d, sess={s: x for s, x in d["sess"].items() if bot_key(x) == bot}) if bot else d
        return layout(tr("ses.cron") + " · Usagecast", "/sessions", p, tr("nav.sessions"), sub, flt + cron_jobs(mine, p), keep={"view": "cron", "bot": bot})
    if proj:
        clear = f'<a href="{link("/sessions", p=p, src=src, bot=bot, chat=chat, q=term)}">{tr("ses.clear")}</a>'
        flt += f'<p class="hint">{tr("ses.only_project", name=e(proj_label(proj)), clear=clear)}</p>'
    if chat:
        ch = next((x["chat"] for _, x in sel if x.get("chat")), None)
        clear = f'<a href="{link("/sessions", p=p, src=src, bot=bot, proj=proj, q=term)}">{tr("ses.clear")}</a>'
        flt += f'<p class="hint">{tr("ses.only_chat", name=e(chat_label(ch)[0] if ch else chat), clear=clear)}</p>'
    s_cost = sum(x["cost"] for _, x in rows)
    kids = defaultdict(lambda: [0, 0.0])   # session -> [subagents it started, their $ in the period]
    for x in d["sess"].values():
        if x.get("parent"):
            kids[x["parent"]][0] += 1; kids[x["parent"]][1] += x["cost"]

    def rel(s, x):
        if x.get("parent"):
            t = d["sess"].get(x["parent"], {}).get("title") or tr("untitled")
            return " · " + tr("ses.by", parent=f'<a href="{link("/s/" + quote(x["parent"]), p=p)}">{e(t[:50])}</a>')
        return " · " + tr("ses.subs", n=cnt(kids[s][0]), cost=money(kids[s][1])) if s in kids else ""
    trs = [(f'<a href="/s/{quote(s)}?p={p}">{e((x["title"] or tr("untitled"))[:80])}</a>'
             f'<span class="why">{" · ".join(e(v) for v in (bot_chat(x), ", ".join(x.get("models") or ())) if v)} · {when(x["last"], "daytime")}{rel(s, x)}</span>',
            e(proj_label(x["project"] or NO_PROJ)) if x["project"] else "–", cnt(x["calls"]), money(x["cost"]), pct(x["cost"], tot),
            f'<span class="why">{e(label(x["top"], d["ttl"])[0])}</span>') for s, x in rows[:200]]
    body = f"""{flt}
<p class="hint">{tr("ses.count", n=cnt(len(rows)), cost=money(s_cost), p=pct(s_cost, tot))}</p>
{table(["th.session", "th.project", "th.steps", "th.cost", "th.share", "th.top"], trs, ("", "l o", "", "", "o", "l o"), "titles")}"""
    return layout(tr("nav.sessions") + " · Usagecast", "/sessions", p, tr("nav.sessions"), sub, body, keep={"src": src, "bot": bot, "chat": chat, "proj": proj, "q": term})


def page_projects(p):
    d = data_for(p)
    tot = d["total"] or 1
    g = defaultdict(lambda: [0, 0, 0.0, defaultdict(float)])
    for x in d["sess"].values():
        a = g[x["project"] or NO_PROJ]
        a[0] += 1; a[1] += x["calls"]; a[2] += x["cost"]
        for k, v in x["comp"].items():
            a[3][group(k)] += v
    rows = sorted(g.items(), key=lambda kv: -kv[1][2])
    top = rows[0][1][2] if rows else 1
    trs = [(f'<a href="{link("/sessions", p=p, proj=n)}">{e(proj_label(n))}</a><div class="t{" soft" if n in (NO_PROJ, HERMES_PROJ) else ""}">'
            f'<i style="width:{c / (top or 1) * 100:.1f}%"></i></div>', cnt(s), cnt(st), money(c), pct(c, tot),
            f'<span class="why">{e(label(max(comp.items(), key=lambda x: x[1])[0], d["ttl"])[0]) if comp else ""}</span>')
           for n, (s, st, c, comp) in rows]
    body = f"""{table(["th.project", "th.sessions", "th.steps", "th.cost", "th.share", "th.top"], trs, ("", "", "o", "", "", "l o"), "titles")}
<details class="more"><summary>{tr("det.how")}</summary><p class="why">{tr("proj.hint", hermes=tr("proj.hermes"))}</p></details>"""
    return layout(tr("nav.projects") + " · Usagecast", "/projects", p, tr("nav.projects"), tr("sub.projects", src=source_name(), period=period_text(d)), body)


def subagents(c, sid, ttl):
    """[(id, title, API calls, $)] of the subagents a session started, their own subagents included."""
    if "parent_session_id" not in {r[1] for r in c.execute("pragma table_info(sessions)")}:
        return []
    out = []
    for k, title in c.execute("""with recursive k(id) as (select id from sessions where parent_session_id = ? and source = 'subagent'
            union select s.id from sessions s join k on s.parent_session_id = k.id where s.source = 'subagent')
            select s.id, s.title from sessions s join k using (id) order by s.started_at""", (sid,)).fetchall():
        rows = c.execute("""select model, api_call_count, input_tokens, cache_read_tokens, cache_write_tokens, output_tokens
            from session_model_usage where session_id = ? and model like 'claude%'""", (k,)).fetchall()
        out.append((k, title, sum(x[1] or 0 for x in rows), sum(sum(row_cost(m, i, r, w, o, ttl)) for m, _, i, r, w, o in rows),
                    models_of(((m, i, r, w, o) for m, _, i, r, w, o in rows), ttl)))
    return out


def page_session(sid, p):
    for a, h in homes():   # the session lives in one of the homes
        with at_home(h):
            ttl, c = cache_ttl(), connect()
            d = analyze(c, 0, load_snap(), ttl, sid=sid)
            if d["sess"]:
                kids, pid = subagents(c, sid, ttl), d["sess"][sid].get("parent")
                ptitle = pid and (c.execute("select title from sessions where id = ?", (pid,)).fetchone() or [None])[0]
                task = (d["sess"][sid]["title"] or "").endswith("…") and (c.execute(
                    "select content from messages where session_id = ? and role = 'user' order by id limit 1", (sid,)).fetchone() or [None])[0]
                break
    if not d["sess"]:
        return None
    pre = f"-p {e(a)} " if a else ""
    x = d["sess"][sid]
    x["agent"] = a
    ch = x.get("chat") or NOCHAT
    subs = sum(k[3] for k in kids)
    if subs:
        d["comp"]["sub"] = subs   # the subagents it started count as one more part of this session
    tot = d["total"] + subs or 1
    rb = [s for s in d["steps"] if s[4]]
    short = lambda k: k.split(":", 1)[1] if k.startswith(("tool:", "skill:")) else label(k)[0]
    names = lambda keys: ", ".join(f"{short(k)}{f' ×{keys.count(k)}' if keys.count(k) > 1 else ''}" for k in dict.fromkeys(keys))
    one_day = len({datetime.fromtimestamp(s[0]).date() for s in d["steps"]}) == 1
    steps = [(hm(t) if one_day else when(t, "short"), num(ctx), money(cost),
              f'{money(r)} <span class="why">{tr("ses.after_pause") if kind == "rebuild" else tr("ses.no_pause") + " · " + tr("brk." + kind[6:])}</span>'
              if kind else "",
              f'<span class="why">{brk(names(keys))}</span>') for t, ctx, cost, r, kind, keys in d["steps"]]
    sub = " · ".join([f'<a href="{link("/sessions", p=p)}">{tr("ses.back")}</a>', e(bot_label(bot_key(x))),
                      f'<a href="{link("/sessions", p=p, bot=bot_key(x), chat=ch["key"])}">{e(chat_label(ch)[0])}</a>',
                      *([tr("ses.by", parent=f'<a href="{link("/s/" + quote(pid), p=p)}">{e((ptitle or tr("untitled"))[:60])}</a>')] if pid else []),
                      tr("ses.project", name=e(proj_label(x["project"] or NO_PROJ))),
                      tr("ses.started", at=when(x["started"] or 0, "datetime")), *([e(", ".join(x["models"]))] if x.get("models") else []),
                      tr("ses.steps", n=cnt(x["calls"])), money(x["cost"]),
                      *([tr("ses.with_subs", cost=money(x["cost"] + subs))] if kids else [])])
    ktab = ""
    if kids:
        trs = [(f'<a href="{link("/s/" + quote(k), p=p)}">{e((t or tr("untitled"))[:80])}</a>'
            + (f'<span class="why">{e(", ".join(ms))}</span>' if ms else ""), cnt(n), money(v), pct(v, tot)) for k, t, n, v, ms in kids]
        ktab = f"""<section class="sec"><h2>{tr("ses.subs_h")}</h2>
<p class="hint">{tr("ses.subs.hint", n=cnt(len(kids)), cost=money(subs), p=pct(subs, tot))}</p>
{table(["th.session", "th.steps", "th.cost", "th.share"], trs, ("", "", "", ""), "titles")}</section>
"""
    now, mine = time.time(), [j for j in jobs() if j["owner"] == sid and j["agent"] == a]
    jtab = (f'<section class="card live"><h2>{tr("ses.jobs_h")}</h2><div class="jobs bgs">'
            + "".join(job_row(j, p, now, True) for j in mine) + "</div></section>\n") if mine else ""
    logged = tr("ses.logged", p=pc(d["logged"] / d["n_steps"] * 100)) if d["n_steps"] else ""
    task = f'<details class="more task"><summary>{tr("ses.task")}</summary><p class="why">{e(task)}</p></details>' if task else ""
    body = f"""{jtab}{task}<p class="hint">{tr("ses.resume")} <code class="cmd">hermes {pre}--resume {e(sid)}</code></p>
<section class="card"><h2>{tr("ses.eats")}</h2>
{bars(ranked(d["comp"], group_skills=False), tot, ttl, explain=False, n=40)}</section>
{ktab}<section class="sec"><h2>{tr("ses.steps_h")}</h2>
<p class="hint">{tr("ses.steps.hint", n=len(rb), cost=money(sum(s[3] for s in rb)))} {logged}</p>
{table(["th.time", "th.history", "th.cost", "th.rebuild", "th.tools"], steps, ("", "", "", "", "l"))}</section>"""
    title = x["title"] or tr("untitled")
    return layout(f"{title} · Usagecast", "/sessions", p, e(title), sub, body, tabs=False)


LAST_PUSH = {}   # result of the last test push, shown on the settings page
INGEST_SHOWN = {}   # when the ingest token was created: shown in full for 10 minutes after that
APP_STORE = "https://apps.apple.com/us/app/ntfy/id1625396347"
COPY_JS = ("var i=document.getElementById('ntfy_topic');i.select();document.execCommand('copy');"
           "if(navigator.clipboard)navigator.clipboard.writeText(i.value);this.textContent=this.dataset.done")


def opts(k, items, off=()):
    """Radio buttons as a segmented control."""
    return (f'<span class="opts" role="radiogroup" aria-labelledby="{k}-l">' + "".join(
        f'<label><input type="radio" name="{k}" value="{v}"{" checked" if v == SET[k] else ""}{" disabled" if v in off else ""}>'
        f'<span>{t}</span></label>' for v, t in items) + "</span>")


def select(k, items, cur):
    return f'<select id="{k}" name="{k}">' + "".join(
        f'<option value="{v}"{" selected" if v == cur else ""}>{t}</option>' for v, t in items) + "</select>"


def check(k):
    return f'<input type="checkbox" id="{k}" name="{k}"{" checked" if SET[k] else ""}>'


def field(k, name, ctl, why=""):
    """One settings row: name and explanation left, control right. k: id of the control."""
    why = '<div class="why">' + why + "</div>" if why else ""   # a div: the push steps hold a list
    lab = (f'<label for="{k}" id="{k}-l">{name}</label>' if re.search(rf'<(?:input|select|button)\b[^>]*\bid="{k}"', ctl)
           else f'<span class="fname" id="{k}-l">{name}</span>')
    sw = " sw" if re.fullmatch(r'<input type="checkbox"[^<]*>', ctl) else ""   # just a switch: stays beside its name on phones
    return f'<div class="field{sw}"><div>{lab}{why}</div><div class="ctl">{ctl}</div></div>'


def card(k, title, inner, hint=""):
    hint = '<p class="hint">' + hint + "</p>" if hint else ""
    return f'<section class="card" id="{k}">' + (f"<h2>{title}</h2>" if title else "") + f"{hint}{inner}</section>"


def pet_picker():
    """Mascot picker on the look page: off + every mascot as a radio tile with its static calm drawing and its name."""
    off = ('<svg class="pick-off" viewBox="0 0 120 120" aria-hidden="true" focusable="false">'
           '<rect x="22" y="22" width="76" height="76" rx="18"/></svg>')
    tiles = [("off", off)] + [(k, pet(k, "calm", 0)) for k in PETS]
    grid = (f'<div class="picks" role="radiogroup" aria-label="{T("pet")}">' + "".join(
        f'<label class="pick"><input type="radio" name="pet" value="{k}"{" checked" if k == SET["pet"] else ""}>'
        f'<span>{svg}<b>{T("pet." + k)}</b></span></label>' for k, svg in tiles) + "</div>")
    return card("mascot", T("pet"), grid, T("pet.why"))


def checks(k, label, items):
    """Radio buttons as an iOS checkmark list, one row per choice: items = (value, name, one-line hint, disabled)."""
    return (f'<div class="checks" role="radiogroup" aria-label="{label}">' + "".join(
        f'<label class="chk"><input type="radio" name="{k}" value="{v}"{" checked" if v == SET[k] else ""}{" disabled" if off else ""}>'
        f'<span>{t}' + (f'<small>{why}</small>' if why else "") + '</span></label>' for v, t, why, off in items) + "</div>")


def T(k):
    return tr("set." + k)


def settings_list():
    """/settings: one row per sub-page with its current state, like the iOS Settings app."""
    s = SET
    quiet = tr("set.row.quiet", a=s["quiet_from"], b=s["quiet_to"]) if s["quiet"] else T("row.noquiet")
    vals = {"look": f'{T("theme." + s["theme"])} · {LOC[lang()]["lang.name"]} · {s["currency"]}',
            "alerts": tr("set.row.alerts", n=sum(s[k] for k in SECTIONS["alerts"] if k.startswith("alert_")), quiet=quiet),
            "notify": T("via." + s["push"]) if push_on() else T("off"),
            "budget": tr("set.row.guard", n=len(s["guard_jobs"])) if s["guard"] else T("off"), "advanced": ""}
    rows = "".join(f'<a class="srow" href="/settings/{k}"><span>{T(TITLES[k])}</span><span class="sval">{v}</span></a>' for k, v in vals.items())
    old = ",".join(f"{a}:{b}" for a, b in OLD_ANCHORS.items())   # links from before the sub-pages: /settings#push and so on
    js = (f'<script>var h=location.hash.slice(1);for(const p of "{old}".split(","))'
          'if(h&&p.split(":")[0]==h)location.replace("/settings/"+p.split(":")[1])</script>')
    return layout(T("h1") + " · Usagecast", "/settings", s["period"], T("h1"), "", f'<nav class="slist" aria-label="{T("h1")}">{rows}</nav>{js}', tabs=False)


def set_look(s, done):
    if FX.get("rate"):
        fx = tr("set.fx", date=when(datetime.fromisoformat(FX["date"]).timestamp(), "day"), rate=nf(FX["rate"], 4))
    else:
        fx = T("fx.none" if s["currency"] == "EUR" else "fx.usd")
    look = (field("theme", T("theme"), opts("theme", [(v, T("theme." + v)) for v in CHOICES["theme"]]))
            + field("lang", T("lang"), select("lang", [(c, LOC[c]["lang.name"]) for c in LOC], lang()), T("lang.why"))
            + field("currency", T("currency"), select("currency", [(c, T("currency." + c)) for c in CHOICES["currency"]], s["currency"]), fx)
            + field("numbers", T("numbers"), opts("numbers", [(v, T("numbers." + v)) for v in CHOICES["numbers"]]))
            + field("period", T("period"), select("period", [(k, tr("period." + k)) for k in PERIODS], s["period"]), T("period.why")))
    limits = (field("limit_view", T("view"), opts("limit_view", [(v, T("view." + v)) for v in CHOICES["limit_view"]]), T("view.why"))
              + field("cost_unit", T("cost"), opts("cost_unit", [(v, T("cost." + v)) for v in CHOICES["cost_unit"]]),
                      T("cost.why" if RATE.get("k") else "cost.none")))
    return card("look", "", look) + card("limits", T("limits"), limits) + pet_picker()


def set_alerts(s, done):
    lo, hi = RANGES["five_min"]
    five = (f'{check("alert_five")}<input type="number" id="five_min" name="five_min" value="{s["five_min"]}" min="{lo}" max="{hi}"'
            f' inputmode="numeric" aria-label="{T("a.five.min")}"><span class="why">{T("min")}</span>')
    quiet = (f'{check("quiet")}<span class="range"><input type="time" id="quiet_from" name="quiet_from" value="{s["quiet_from"]}" aria-label="{T("quiet.from")}">'
             f'<span class="why">–</span><input type="time" id="quiet_to" name="quiet_to" value="{s["quiet_to"]}" aria-label="{T("quiet.to")}"></span>')
    share = lambda k: (f'<input type="number" id="{k}" name="{k}" value="{s[k]}" min="{RANGES[k][0]}" max="{RANGES[k][1]}" step="0.1"'
                       f' inputmode="decimal" aria-label="{T(k)}"><span class="why">{T("pct")}</span>')
    nok = "" if RATE.get("k") else " " + T("a.nok")
    acur = ((STATE["limits"] or {}).get("extra_usage") or {}).get("currency") or ""
    lim = (field("alert_five", T("a.five"), five, T("a.five.why")) + field("alert_week", T("a.week"), check("alert_week"), T("a.week.why"))
           + field("alert_free", T("a.free"), check("alert_free"), T("a.free.why"))
           + field("alert_steps", T("a.steps"), check("alert_steps"), T("a.steps.why")))
    costs = (field("alert_extra", T("a.extra"), check("alert_extra"), T("a.extra.why"))
             + field("extra_steps", T("a.extra.steps"), f'<input id="extra_steps" name="extra_steps" value="{e(s["extra_steps"])}"'
                     f' inputmode="decimal" placeholder="10, 20" spellcheck="false" autocomplete="off"><span class="why">{e(acur)}</span>',
                     T("a.extra.steps.why"))
             + field("alert_chat", T("a.chat"), check("alert_chat") + share("chat_pct"), T("a.chat.why") + nok)
             + field("alert_spike", T("a.spike"), check("alert_spike") + share("spike_pct"), T("a.spike.why") + nok))
    digest = field("alert_digest", T("a.digest"), check("alert_digest"), T("a.digest.why")) + field("quiet", T("quiet"), quiet, T("quiet.why"))
    return card("al-limits", T("ag.limits"), lim) + card("al-costs", T("ag.costs"), costs) + card("al-digest", T("ag.digest"), digest)


def set_notify(s, done):
    host = urlparse(s["ntfy_server"]).netloc
    hermes_ok, tg_ok = bool(hermes_bark() or hermes_ntfy()), bool(tg_target())
    bots = [(n or "hermes", e(agent_label(n))) for n, h in [("", HERMES), *((x, HERMES / "profiles" / x) for x in profiles())]
            if hermes_env("TELEGRAM_BOT_TOKEN", h)]
    chan = checks("push", T("via"), [("off", T("via.off"), "", False), ("own", T("via.own"), tr("set.via.own.why", server=e(host)), False),
                           ("hermes", T("via.hermes"), T("via.hermes.why" if hermes_ok else "via.nohermes"), not hermes_ok),
                           ("bark", T("via.bark"), T("via.bark.why"), False),
                           ("telegram", T("via.telegram"), T("via.tg.why" if tg_ok else "via.notg"), not tg_ok)])
    if not s["ntfy_topic"]:
        own = field("setup", T("setup"), f'<button class="btn primary" id="setup" name="action" value="setup">{T("setup.btn")}</button>', T("setup.why"))
    else:
        android = f'ntfy://{host}/{s["ntfy_topic"]}' + ("" if s["ntfy_server"].startswith("https:") else "?secure=false")
        steps = "".join(f"<li>{x}</li>" for x in (tr("set.ios.1", url=APP_STORE), T("ios.2"), T("ios.3")))
        ios = f'{T("ios")}<ol class="steps">{steps}</ol>' + (tr("set.ios.own", server=e(s["ntfy_server"])) if s["ntfy_server"] != "https://ntfy.sh" else "")
        own = (field("ntfy_topic", T("topic"), f'<input id="ntfy_topic" name="ntfy_topic" value="{e(s["ntfy_topic"])}" class="mono" pattern="[A-Za-z0-9_\\-]+" maxlength="64"'
                     f' spellcheck="false" autocomplete="off" autocapitalize="off"><button type="button" class="btn" data-done="{T("copied")}"'
                     f' onclick="{COPY_JS}">{T("copy")}</button>', T("topic.why"))
               + field("android", T("subscribe"), f'<a class="btn" id="android" href="{e(android)}">{T("android")}</a>', ios)
               + field("newtopic", T("newtopic"), f'<button class="btn" id="newtopic" name="action" value="newtopic"'
                       f' data-q="{e(T("newtopic.confirm"))}" onclick="return confirm(this.dataset.q)">{T("newtopic.btn")}</button>', T("newtopic.why")))
    bpw = (f'<input type="password" id="bark_pass" name="bark_pass" placeholder="{T("token.saved") if s["bark_pass"] else ""}"'
           f' autocomplete="new-password">' + (f'<label class="why"><input type="checkbox" name="bark_pass_clear"> {T("bark.pass.clear")}</label>'
                                               if s["bark_pass"] else ""))
    mono = ' class="mono" spellcheck="false" autocomplete="off" autocapitalize="off"'
    bark = (field("bark_url", T("bark.url"), f'<input type="url" id="bark_url" name="bark_url" value="{e(s["bark_url"])}" class="wide" placeholder="https://">',
                  T("bark.url.why"))
            + field("bark_key", T("bark.key"), f'<input id="bark_key" name="bark_key" value="{e(s["bark_key"])}" maxlength="64"{mono}>', T("bark.key.why"))
            + field("bark_user", T("bark.user"), f'<input id="bark_user" name="bark_user" value="{e(s["bark_user"])}" maxlength="64"{mono}>', T("bark.user.why"))
            + field("bark_pass", T("bark.pass"), bpw, T("bark.pass.why")))
    test = field("test", T("test"), f'<button class="btn" id="test" name="action" value="test">{T("test.btn")}</button>', T("test.why")) if push_on() else ""
    tgc = (field("tg_bot", T("tg.bot"), select("tg_bot", [("off", T("tg.bot.off")), *bots], s["tg_bot"]), T("tg.bot.why" if bots else "tg.nobot"))
           + field("tg_thread", T("tg.thread"), f'<input id="tg_thread" name="tg_thread" value="{e(s["tg_thread"])}" inputmode="numeric"'
                   f' pattern="[0-9]*" maxlength="12" autocomplete="off">', T("tg.thread.why"))
           + field("tg_menu", T("tg.menu"), check("tg_menu"), T("tg.menu.why")) + field("tg_pin", T("tg.pin"), check("tg_pin"), T("tg.pin.why"))
           + f'<p><button type="button" class="btn" id="tg-home" hidden>{T("tg.home")}</button></p>')   # static/app.js shows it in Telegram
    # The own-topic rows show only while "own topic" is ticked (CSS :has, so no JS; without :has they always show)
    return card("chan", T("via"), chan + f'<div class="ch-own">{own}</div><div class="ch-bark">{bark}</div>' + test) + card("tg", T("tg"), tgc, T("tg.hint"))


def set_budget(s, done):
    jl, (gc, gdays), held = cron_list(), cron_costs(data_for("30")), guard_state().get("paused", [])
    rows = "".join(f'<div class="job"><input type="checkbox" id="gj_{e(j["id"])}" name="gj_{e(j["id"])}"{" checked" if j["id"] in s["guard_jobs"] else ""}>'
                   f'<label for="gj_{e(j["id"])}">{e(j.get("name") or j["id"])}</label><span class="why">'
                   f'{tr("set.guard.week", cost=money(gc[j.get("name")][1] / gdays * 7)) if j.get("name") in gc else "–"}'
                   f'{" · " + T("guard.held") if j["id"] in held and j.get("state") == "paused" else ""}</span></div>' for j in jl)
    # The job list shows only while the guard is on (CSS :has, so no JS; without :has it always shows)
    jobs = (f'<div class="gjobs">{field("guard_jobs", T("guard.jobs"), "", T("guard.jobs.why"))}<div class="jobs">{rows}</div></div>' if jl
            else f'<p class="hint">{T("guard.none")}</p>')
    return card("budget", "", field("guard", T("guard.on"), check("guard"), T("guard.on.why")) + jobs)


def set_advanced(s, done):
    tk, url = s["ingest_token"], s["url"] or "https://dashboard.example:8443"
    full = done == "ingest" and time.time() - INGEST_SHOWN.get("at", 0) < 600
    if not tk:
        machines = field("ingest", T("ing.token"), f'<button class="btn primary" id="ingest" name="action" value="ingest">{T("ing.create")}</button>', T("ing.why"))
    else:
        machines = field("ingest_tok", T("ing.token"), f'<input id="ingest_tok" value="{e(tk if full else tk[:4] + "…" + tk[-4:])}" readonly class="mono"'
                         f' spellcheck="false">' + (f'<button type="button" class="btn" data-done="{T("copied")}" onclick="{COPY_JS.replace("ntfy_topic", "ingest_tok")}">'
                                                    f'{T("copy")}</button>' if full else "")
                         + f'<button class="btn" name="action" value="ingest" data-q="{e(T("ing.rotate.confirm"))}" onclick="return confirm(this.dataset.q)">'
                         f'{T("ing.rotate")}</button>', T("ing.shown" if full else "ing.masked"))
        cmd = f'curl -fsSO {url}/cc-report.py && python3 cc-report.py --url {url} --token {tk if full else "TOKEN"} --install'
        machines += field("ingest_cmd", T("ing.install"), "", T("ing.install.why")) + f'<pre class="cmd">{e(cmd)}</pre>'
        hosts = ingest_hosts()
        lines = "".join(f'<div class="job"><span class="fname">{e(h)}</span><span class="why">{tr("set.ing.host", at=when(x["at"], "daytime"), cost=money(sum(c for t, c in x["hours"] if t >= time.time() - 7 * 86400)))}</span></div>'
                        for h, x in hosts.items())
        machines += field("ingest_hosts", T("ing.hosts"), "", "" if hosts else T("ing.none")) + (f'<div class="jobs">{lines}</div>' if hosts else "")
    ps, main = profiles(), tr("set.src.main", path=e(str(HERMES)))
    source = (field("source", T("src.label"), select("source", [(ALL, T("src.all")), ("", main), *((x, e(x)) for x in ps)],
                                                     s["source"] if s["source"] in (ALL, *ps) else ""), T("src.why")) if ps
              else field("source", T("src.label"), f'<span class="why">{main}</span>', T("src.none")))
    token = (f'<input type="password" id="ntfy_token" name="ntfy_token" placeholder="{T("token.saved") if s["ntfy_token"] else ""}"'
             f' autocomplete="new-password">' + (f'<label class="why"><input type="checkbox" name="token_clear"> {T("token.clear")}</label>'
                                                 if s["ntfy_token"] else ""))
    server = (field("ntfy_server", T("server.url"), f'<input type="url" id="ntfy_server" name="ntfy_server" value="{e(s["ntfy_server"])}" class="wide">',
                    T("server.why")) + field("ntfy_token", T("token"), token, T("token.why")))
    addr = field("url", T("url"), f'<input type="url" id="url" name="url" value="{e(s["url"])}" class="wide" placeholder="https://">'
                 f'<button class="btn" name="action" value="useurl">{T("useurl")}</button>', T("url.why"))
    return (card("machines", T("ing"), machines, T("ing.hint")) + card("src", T("src"), source)
            + card("server", T("server"), server, T("server.hint")) + card("addr", "", addr))


SET_PAGES = {"look": set_look, "alerts": set_alerts, "notify": set_notify, "budget": set_budget, "advanced": set_advanced}
TITLES = {"look": "look", "alerts": "alerts", "notify": "notify", "budget": "guard", "advanced": "adv"}   # set.<title> keys
SUBS = {"alerts": "alerts.hint", "notify": "push.hint", "budget": "guard.hint", "advanced": "sub"}


def page_settings(q, sec=""):
    """/settings (the list) or /settings/<sec>: a form that saves only that section's keys (SECTIONS)."""
    if sec not in SET_PAGES:
        return settings_list()
    s, done = SET, (q.get("done") or [""])[0]
    note = ""
    if done == "test" and time.time() - LAST_PUSH.get("at", 0) < 600:
        note = tr("set.test.ok" if LAST_PUSH["ok"] else "set.test.fail", reply=f'<code>{e(LAST_PUSH["reply"])}</code>')
        fail, code = not LAST_PUSH["ok"] and (bark_target() or ntfy_target()), LAST_PUSH["reply"][:3]
        if fail and code in ("401", "403"):
            note += " " + T("test.auth")
        elif fail and not code.isdigit():
            note += " " + T("test.net")
    elif done in ("save", "setup", "newtopic", "useurl"):
        note = T("done." + done)
    note = f'<p class="note" role="status">{note}</p>' if note else ""
    names = {"ntfy_topic": "topic", "ntfy_server": "server.url", "bark_url": "bark.url", "bark_key": "bark.key", "bark_user": "bark.user", "url": "url", "quiet_from": "quiet.from", "quiet_to": "quiet.to",
             "extra_steps": "a.extra.steps", "tg_thread": "tg.thread"}
    bad = [T(names[k]) for k in (q.get("bad") or [""])[0].split(",") if k in names]
    if bad:
        note = f'<p class="note" role="alert">{tr("set.bad", fields=", ".join(bad))}</p>'
    body = (f'{note}<form method="post" action="/settings" class="set" data-saved="{e(T("done.save").rstrip("."))}" data-bad="{e(T("bad.one"))}"'
            f' data-fail="{e(T("fail"))}"><input type="hidden" name="section" value="{sec}">'
            '<button class="sr" name="action" value="save" tabindex="-1" aria-hidden="true"></button>'  # Enter in a field = save
            + SET_PAGES[sec](s, done)
            + f'<div class="save"><button class="btn primary" name="action" value="save">{T("save")}</button></div></form>')
    back = f'<a class="back" href="/settings">{T("h1")}</a>'   # static/app.js swaps it for Telegram's back button
    title = T(TITLES[sec])
    return layout(f"{title} · Usagecast", "/settings", s["period"], title, T(SUBS[sec]) if sec in SUBS else "", body, tabs=False, back=back)


PROXY_TOKEN = os.environ.get("USAGECAST_PROXY_TOKEN", "")   # needed by requests that arrive through a Cloudflare tunnel


class Handler(BaseHTTPRequestHandler):
    cookie = None

    def parse_request(self):
        """A request through Cloudflare (which always sets Cf-Connecting-Ip) needs USAGECAST_PROXY_TOKEN as Bearer; none
        set means no way in from outside. Direct and tailnet requests stay as they are."""
        if not super().parse_request():
            return False
        if self.headers.get("Cf-Connecting-Ip") is not None and not (PROXY_TOKEN and hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), ("Bearer " + PROXY_TOKEN).encode())):
            self.send_error(403)
            return False
        return True

    def send(self, b, ctype, cache="no-cache"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", cache)
        if self.cookie:
            self.send_header("Set-Cookie", self.cookie)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        wanted = (q.get("lang") or [""])[0]
        _req.lang, _req.url = pick_lang(wanted, self.headers), self.path
        self.cookie = f"lang={wanted}; Path=/; Max-Age=31536000; SameSite=Lax" if wanted in LOC else None
        if q.get("from") and q.get("to"):
            q["p"] = [f'{q["from"][0]}..{q["to"][0]}']   # the custom-range form
        p = q.get("p", [SET["period"]])[0]
        p = p if p in PERIODS or custom(p) else SET["period"]
        path = ALIASES.get(u.path, u.path)
        name = path.lstrip("/")
        if name in STATIC_FILES:
            try:
                return self.send((STATIC / name).read_bytes(), STATIC_FILES[name], "max-age=86400")
            except OSError:
                return self.send_error(404)
        pages = {"/": page_overview, "/history": page_history, "/details": page_details, "/projects": page_projects}
        try:
            if path in pages:
                body = pages[path](p)
            elif path == "/sessions":
                body = page_sessions(p, q)
            elif path.startswith("/s/"):
                body = page_session(unquote(path[3:]), p)
            elif path == "/settings" or path.startswith("/settings/") and path[10:] in SECTIONS:
                body = page_settings(q, path[10:])
            elif path == "/wrapped":
                body = page_wrapped(q)
            elif path.startswith("/job/") and path.endswith(".img"):
                img = job_image(unquote(path[5:-4]), dict(homes()).get((q.get("a") or [""])[0]))
                return self.send(*img) if img else self.send_error(404)
            elif path == "/live":
                return self.send(live_card(live(), p).encode(), "text/html; charset=utf-8")
            elif path == "/api/summary":
                return self.send(api_summary().encode(), "application/json")
            elif path == "/cc-report.py":
                return self.send((ROOT / "tools" / "cc-report.py").read_bytes(), "text/x-python; charset=utf-8")
            elif path == "/health":
                body = "ok"
            else:
                body = None
        except Exception:
            traceback.print_exc()
            return self.send_error(500)
        if body is None:
            return self.send_error(404)
        self.send(body.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        """Only /settings: saves the form's section, then runs the button's action (set up, new topic, test, use this address)."""
        _req.lang, _req.url = pick_lang("", self.headers), self.path
        n = self.headers.get("Content-Length", "0")
        if urlparse(self.path).path == "/api/ingest":
            tok = SET["ingest_token"]
            if not tok or not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + tok).encode()):
                return self.send_error(401)
            if not n.isdigit() or int(n) > 2 * 1024 * 1024:
                return self.send_error(400)
            rep = parse_ingest(self.rfile.read(int(n)))
            if not rep:
                return self.send_error(400)
            save_ingest(*rep)
            return self.send(b'{"ok": true}', "application/json")
        if urlparse(self.path).path != "/settings":
            return self.send_error(404)
        if not same_origin(self.headers) or not n.isdigit() or int(n) > 65536:
            return self.send_error(403)
        form = {k: v[-1] for k, v in parse_qs(self.rfile.read(int(n)).decode("utf-8", "replace"), keep_blank_values=True).items()}
        action, sec = form.get("action", "save"), form.get("section", "")
        keys = SECTIONS.get(sec, ())   # no or an unknown section: nothing is saved
        s = clean(form, SET, keys)
        auto = self.headers.get("X-Usagecast-Auto") == "1"   # static/app.js saves a changed field: JSON back, no redirect
        bad = [k for k in FORMATS if k in keys and k in form and form[k].strip().rstrip("/") != s[k]]   # clean() kept the old value
        if auto and not keys:
            return self.send(b'{"ok": false, "bad": []}', "application/json")
        if action == "ingest":
            s["ingest_token"] = secrets.token_urlsafe(24)
            INGEST_SHOWN["at"] = time.time()
        if action in ("setup", "newtopic"):
            s["ntfy_topic"] = new_topic()
            s["push"] = "own" if action == "setup" else s["push"]
        elif action == "useurl" and re.fullmatch(URL_RX, self.headers.get("Origin") or ""):
            s["url"] = self.headers["Origin"].rstrip("/")
        save_settings(s)
        _req.lang = s["lang"]
        if s["currency"] == "EUR":
            refresh_fx()
        if action == "test":
            ok, reply = send_push(tr("alert.test"), tr("alert.test.text"), 3)
            LAST_PUSH.update(at=time.time(), ok=ok, reply=reply)
        if auto:
            self.cookie = f"lang={s['lang']}; Path=/; Max-Age=31536000; SameSite=Lax"
            return self.send(json.dumps({"ok": True, "bad": bad, "num": {k: s[k] for k in keys if k in RANGES}}).encode(), "application/json")
        self.send_response(303)
        self.send_header("Location", ("/settings/" + sec if sec in SECTIONS else "/settings") + f"?done={quote(action)}"
                         + (f"&bad={','.join(bad)}" if bad else ""))
        self.send_header("Set-Cookie", f"lang={s['lang']}; Path=/; Max-Age=31536000; SameSite=Lax")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


# ---------- Self-test ----------
def test_db(base=0.0):
    """In-memory Hermes database with one chat session (pause after 1,000 s) and one cron run without history."""
    tc = json.dumps([{"id": "a", "function": {"name": "terminal", "arguments": '{"command": "ls /opt/shop"}'}}])
    msgs = [("user", "hi " * 50, None, None, None, None, base + 0.0, None),
            ("assistant", "", None, None, tc, None, base + 1.0, "think " * 20),
            ("tool", "x" * 700, None, "terminal", None, "a", base + 2.0, None),
            ("assistant", "done", None, None, None, None, base + 3.0, None),
            ("user", "go on", None, None, None, None, base + 1000.0, None),
            ("assistant", "ok", None, None, None, None, base + 1001.0, None)]
    db = sqlite3.connect(":memory:")
    db.executescript("""create table sessions(id, source, title, system_prompt_hash, started_at, git_repo_root, parent_session_id);
        create table system_prompts(hash, prompt);
        create table messages(id integer primary key, session_id, role, content, api_content, tool_name, tool_calls,
            tool_call_id, timestamp, reasoning);
        create table session_model_usage(session_id, model, task, api_call_count, input_tokens, cache_read_tokens,
            cache_write_tokens, output_tokens, last_seen);""")
    db.executemany("insert into sessions values(?, ?, ?, null, ?, null, null)",
                   [("s", "telegram", "Test", base), ("c", "cron", "Daily report · Oct 08 16:31", base)])
    db.executemany("insert into session_model_usage values(?, 'claude-opus-5-5', ?, ?, ?, ?, ?, ?, ?)",
                   [("s", "", 3, 10, 5000, 3000, 400, base + 1001), ("s", "background_review", 1, 0, 2000, 500, 100, base + 1001),
                    ("c", "", 2, 5, 1000, 800, 50, base + 500)])
    db.executemany("""insert into messages(session_id, role, content, api_content, tool_name, tool_calls, tool_call_id,
        timestamp, reasoning) values('s', ?, ?, ?, ?, ?, ?, ?, ?)""", msgs)
    db.commit()  # an open transaction would make backup() wait forever
    return db, msgs


DEMO_TOOLS = {"terminal": (1500, "command", "git -C {p} status"), "read_file": (9000, "path", "{p}/app.py"),
              "search_files": (3000, "path", "{p}"), "patch": (600, "path", "{p}/app.py"), "web_search": (4000, "query", "release notes"),
              "web_extract": (12000, "url", "https://example.com/docs"), "browser_exec": (2500, "code", "page_info()"),
              "session_search": (30000, "query", "last deploy"), "skill_view": (6000, "name", "deploy")}
DEMO_TITLES = ("Fix the checkout bug", "Write release notes", "Plan the week", "Migrate the database", "Review open pull requests",
               "Draft a blog post", "Tidy up the wiki", "Debug the deploy")


def demo_db(now):
    """A month of made-up sessions for --demo: chats, CLI work on three projects, two cron jobs, now and then a pause."""
    rnd, (db, _) = random.Random(7), test_db(now - 30 * 86400)
    for day in range(30, -1, -1):
        d0 = datetime.fromtimestamp(now - day * 86400).replace(hour=0, minute=0, second=0, microsecond=0)
        for _ in range(rnd.randint(1, 3) if d0.weekday() >= 5 else rnd.randint(3, 7)):
            t = d0.timestamp() + rnd.uniform(8, 23) * 3600   # a day person
            if t > now - 1800:
                continue
            kind, proj = rnd.choices(("telegram", "cli", "cron"), (5, 3, 2))[0], rnd.choice(("/opt/shop", "/srv/blog", "/opt/wiki", "/tmp"))
            title = (f'{rnd.choice(("Daily report", "Inbox digest"))} · {datetime.fromtimestamp(t).strftime("%b %d %H:%M")}'
                     if kind == "cron" else rnd.choice(DEMO_TITLES))
            n = rnd.randint(4, 12) if kind == "cron" else rnd.randint(3, 25) if kind == "telegram" else rnd.randint(10, 60)
            sid = datetime.fromtimestamp(t).strftime("%Y%m%d_%H%M%S_") + "%06x" % rnd.getrandbits(24)
            rows = [(sid, "user", "please " * rnd.randint(5, 80), None, None, None, t, None)]
            ctx, prev, calls = 40000 + len(rows[0][2]) / 4, 0, []   # tokens: system prompt + tool schemas + history
            for j in range(n):
                gap = rnd.uniform(400, 2400) if rnd.random() < 0.08 else rnd.uniform(10, 90)
                if t + gap > now - 300:
                    break
                t += gap
                calls.append((ctx, ctx - prev, j == 0 or gap > 300))   # context, new since the last call, cache expired
                prev = ctx
                if j == n - 1:
                    rows.append((sid, "assistant", "Done. " * 20, None, None, None, t, "think " * rnd.randint(10, 200)))
                    break
                tool = rnd.choices(list(DEMO_TOOLS), (30, 22, 10, 12, 6, 3, 6, 1, 4))[0]
                size, arg, val = DEMO_TOOLS[tool]
                call = json.dumps([{"id": f"c{j}", "function": {"name": tool, "arguments": json.dumps({arg: val.format(p=proj)})}}])
                res = "x" * int(size * rnd.uniform(0.2, 2))
                rows += [(sid, "assistant", "", None, call, None, t, "think " * rnd.randint(10, 200)),
                         (sid, "tool", res, tool, None, f"c{j}", t + 2, None)]
                ctx += 400 + len(res) / 4
            if not calls:
                continue
            read, write = sum(0 if cold else c - a for c, a, cold in calls), sum(c if cold else a for c, a, cold in calls)
            db.execute("insert into sessions values(?, ?, ?, null, ?, null, null)", (sid, kind, title, rows[0][6]))
            db.execute("insert into session_model_usage values(?, 'claude-opus-5-5', '', ?, ?, ?, ?, ?, ?)",
                       (sid, len(calls), 5 * len(calls), round(read), round(write), rnd.randint(150, 600) * len(calls), rows[-1][6]))
            db.executemany("""insert into messages(session_id, role, content, tool_name, tool_calls, tool_call_id, timestamp,
                reasoning) values(?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    for col in ("chat_id", "chat_type", "thread_id", "display_name", "origin_json"):
        db.execute(f"alter table sessions add column {col}")
    dm = {"platform": "telegram", "chat_id": "100", "chat_name": "Alex", "chat_type": "dm"}
    places = [*({**dm, "thread_id": t, **({"chat_topic": n} if n else {})} for t, n in (("11", "Shop"), ("12", "Blog"), ("13", None))), dm,
              {"platform": "discord", "chat_id": "300", "chat_name": "Studio / #releases / v2 launch", "chat_type": "thread", "thread_id": "300"},
              {"platform": "discord", "chat_id": "400", "chat_name": "alex", "chat_type": "dm"}]
    for (sid,) in db.execute("select id from sessions where source = 'telegram'").fetchall():
        o = rnd.choices(places, (5, 3, 2, 3, 2, 1))[0]
        db.execute("update sessions set source = ?, origin_json = ? where id = ?", (o["platform"], json.dumps(o), sid))
    db.commit()
    return db


def demo():
    """Usagecast on made-up data (README screenshots, a first look): no Hermes needed, no limit fetch, no alerts."""
    global HERMES, DATA, CLAUDE
    now, tmp = time.time(), Path(tempfile.mkdtemp(prefix="usagecast-demo-"))
    HERMES, DATA, CLAUDE = tmp, tmp / "data", tmp / "claude"
    (tmp / "cron").mkdir(parents=True)
    DATA.mkdir()
    demo_db(now).backup(disk := sqlite3.connect(tmp / "state.db"))
    disk.close()
    (tmp / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "d1", "name": "Daily report", "schedule_display": "0 7 * * *", "state": "scheduled", "repeat": {"times": None}},
        {"id": "d2", "name": "Inbox digest", "schedule_display": "every 360m", "state": "scheduled", "repeat": {"times": None}}]}))
    (DATA / "snapshot.json").write_text(json.dumps({"prompt": {}, "at": now, "tools": {
        "terminal": 1100, "read_file": 480, "search_files": 510, "patch": 560, "web_search": 230, "web_extract": 320, "browser_exec": 1060,
        "session_search": 2000, "skill_view": 270, "memory": 900, "todo": 390, "cronjob": 2800, "delegate_task": 1650}}))
    reset, five = now + 2.6 * 86400, now + 2.2 * 3600
    start = reset - 7 * 86400
    with open(DATA / "limits.jsonl", "w") as f:   # last week ramps to 85 %, this one to 62 % so far; 5-hour windows refill
        for t in range(int(now - 7 * 86400), int(now), 600):
            wk = 62 * ((t - start) / (now - start)) ** 1.15 if t >= start else 85 * (t - start + 7 * 86400) / (7 * 86400)
            ex = 12.4 * (t - now + 7 * 86400) / (4 * 86400) if t < now - 3 * 86400 else 5.01 * (t - now + 3 * 86400) / (3 * 86400)
            f.write(json.dumps({"t": t, "five_hour": round(min(12 * ((t - five) % 18000) / 3600, 100)), "seven_day": round(wk),
                                "extra": round(ex, 2)}) + "\n")
    iso = lambda t: datetime.fromtimestamp(t).astimezone().isoformat()
    STATE.update(limits={"five_hour": {"utilization": 34.0, "resets_at": iso(five)}, "seven_day": {"utilization": 62.0, "resets_at": iso(reset)},
                         "extra_usage": {"is_enabled": True, "used_credits": 501, "monthly_limit": 2500, "decimal_places": 2, "currency": "USD"}},
                 at=1e12, ok=now)   # at far ahead: refresh_limits() never fetches
    other = {"five_hour": {"utilization": 3.0, "resets_at": None}, "seven_day": {"utilization": 8.0, "resets_at": iso(reset)}}
    STATE["accts"] = [   # two pooled accounts: "work" is in use, "personal" is next
        dict(id="a1", label="work", use=True, next=False, primary=True, avail=True, status=None, limits=STATE["limits"], error=None),
        dict(id="a2", label="personal", use=False, next=True, primary=False, avail=True, status=None, limits=other, error=None)]
    (DATA / "ingest").mkdir()   # a laptop that reports its Claude Code usage in the evenings
    (DATA / "ingest" / "macbook.json").write_text(json.dumps({"host": "macbook", "at": round(now - 300), "hours": [
        [h, 0.8 + 0.1 * (h // 3600 % 5)] for h in range(int(now - 7 * 86400) // 3600 * 3600, int(now), 3600)
        if datetime.fromtimestamp(h).hour in (19, 20, 21)]}))
    (DATA / "bots.json").write_text(json.dumps({"at": round(now), "names": {"": {"telegram": "Atlas", "discord": "Atlas"}}}))
    SET.clear(); SET.update(DEFAULTS, push="off", ntfy_topic="", url="", guard=True, guard_jobs=["d2"], ingest_token="demo"); FX.clear()
    print(f"usagecast demo on http://127.0.0.1:{PORT} (data in {tmp})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def selftest_v4():
    """Merged homes add up; live() finds busy sessions, nests subagents, skips idle, dead and ended ones."""
    _req.lang = "en"
    a = {"comp": {"x": 1.0}, "hours": {0: {"x": 1.0}}, "hsess": {0: {"s"}}, "models": {"m": [1, 10, 1.0]},
         "heavy": [(1.0, "a")], "sess": {"s": {}}, "steps": [], "total": 1.0, "since": 5, "hourly": True}
    b = {"comp": {"x": 2.0, "y": 1.0}, "hours": {0: {"x": 3.0}}, "hsess": {0: {"t"}}, "models": {"m": [1, 10, 2.0]},
         "heavy": [(2.0, "b")], "sess": {"t": {}}, "steps": [], "total": 3.0, "since": 5, "hourly": True}
    m = merge(copy.deepcopy(a), b)
    assert m["comp"] == {"x": 3.0, "y": 1.0} and m["hours"][0]["x"] == 4.0 and m["hsess"][0] == {"s", "t"}
    assert m["models"]["m"] == [2, 20, 3.0] and m["heavy"][0] == (2.0, "b") and set(m["sess"]) == {"s", "t"}
    assert m["total"] == 4.0 and m["since"] == 5 and m["hourly"] is True and a["total"] == 1.0
    with tempfile.TemporaryDirectory() as tdir:
        db = sqlite3.connect(Path(tdir) / "state.db")
        db.executescript("""create table sessions (id, source, title, parent_session_id, last_activity_description,
            last_activity_at, started_at, ended_at, model, input_tokens, cache_read_tokens, cache_write_tokens, output_tokens);
            create table messages (session_id, role, timestamp);""")
        t0 = time.time()
        db.executemany("insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("a", "telegram", "Work", None, "tool running: terminal", t0 - 5, t0 - 600, None, "claude-opus-4", 1000, 0, 0, 100),
            ("b", "subagent", "Sub", "a", "tool running: read_file", t0 - 5, t0 - 60, None, "claude-opus-4", 10, 0, 0, 1),
            ("c", "telegram", "Idle", None, "", t0 - 5, t0 - 600, None, None, 0, 0, 0, 0),
            ("d", "cli", "Dead", None, "tool running: x", t0 - 7200, t0 - 9000, None, None, 0, 0, 0, 0),
            ("e", "cli", "Ended", None, "tool running: x", t0 - 5, t0 - 600, t0 - 1, None, 0, 0, 0, 0)])
        db.execute("insert into messages values ('a', 'user', ?)", (t0 - 120,))
        db.commit()
        db.close()
        L = live(t0, [("", Path(tdir))])
        assert [r["id"] for r in L["runs"]] == ["a", "b"] and L["n"] == 1 and L["runs"][0]["since"] == t0 - 120
        assert L["runs"][0]["cost"] > 0 and L["next"] == []
        card_ = live_card(L, "w")
        assert card_.count('<a class="job') == 2 and 'class="job sub"' in card_ and "{" not in card_
        assert 'id="live" data-p="w" data-n="1"' in card_
        _req.lang, hp, me = "en", Path(tdir), os.getpid()   # background jobs: processes.json + progress files
        (hp / "progress").mkdir()
        (hp / "processes.json").write_text(json.dumps([
            dict(pid=me, host_start_time=proc(me)[1], command="cd /x && A=1 B=$X/y bash render.sh  big", started_at=t0 - 300, owner_task_id="a"),
            dict(pid=me, host_start_time=1, command="reused pid"), dict(pid=2 ** 22 + 7, command="gone")]))
        pj = lambda n, **kw: (hp / "progress" / f"{n}.json").write_text(json.dumps(kw))
        pj("r", title="Batch", done=1, total=4, frac=0.5, step="img2", detail="Sample 256/512", pid=me, started=t0 - 300, at=t0 - 2,
           image=str(hp / "p.png"))
        pj("old", title="Old", state="done", at=t0 - JOB_KEEP - 1)
        pj("fin", title="Fin", done=3, total=3, at=t0 - 60)
        pj("quiet", title="Quiet", frac=0.3, at=t0 - JOB_QUIET - 1)
        pj("dead", title="Dead", frac=0.3, pid=2 ** 22 + 7, at=t0 - 10)
        pj(".hid", title="Hidden", at=t0)
        (hp / "p.png").write_bytes(b"\x89PNG")
        J = jobs(t0, [("", hp)])
        assert [(j["cmd"], (j["prog"] or {}).get("name"), (j["prog"] or {}).get("end")) for j in J] == [
            ("bash render.sh big", "r", None), ("", "fin", "done"), ("", "dead", "stopped")], J
        cj = live_card(dict(runs=[], n=0, up=True, down=[], next=[], at=t0, jobs=J), "w")
        assert 'style="width:37.5%"' in cj and "38 %" in cj and 'href="/s/a?p=w"' in cj and "/job/r.img?a=&amp;v=" in cj
        assert "Sample 256/512" in cj and ">Done<" in cj and ">Stopped<" in cj and "background jobs: 3" in cj and "{" not in cj
        assert tr("live.left", d=dur(500)) in cj and cj.count("thumb") == 1
        assert summary_jobs(J, t0) == [{"name": "Batch", "pct": 37, "eta_s": 500}, {"name": "Fin", "pct": 100, "eta_s": None, "done": True}]   # stopped left out
        pj("ok2", title="Ok2", state="done", at=t0 - 599); pj("bad", title="Bad", state="failed", at=t0 - 5)
        J2 = jobs(t0, [("", hp)])
        assert summary_jobs(J2, t0) == [{"name": "Batch", "pct": 37, "eta_s": 500}, {"name": "Fin", "pct": 100, "eta_s": None, "done": True},
                                        {"name": "Ok2", "pct": 100, "eta_s": None, "done": True}]   # newest finished first, summary_jobs(J2, t0)   # failed/stopped out
        assert [j["name"] for j in summary_jobs(J2, t0 + 2)] == ["Batch", "Fin"]   # Ok2 is now older than 600 s
        assert [r["name"] for r in summary_jobs(J, t0 + 541)] == ["Batch"]   # Fin is 601 s old
        ts = lambda *p: json.dumps({"Peer": {f"k{i}": {"HostName": h, "Online": o} for i, (h, o) in enumerate(p)}})
        assert pc_online(ts(("x", False), ("box-1", True)), "box-1") is True and pc_online(ts(("box-1", False)), "box-1") is False
        assert pc_online(ts(("Box-1", True)), "BOX-1") is True and pc_online(ts(("x", True)), "box-1") is None and pc_online("{}", "box-1") is None
        assert pc_online("nope", "box-1") is None and pc_online("", "box-1") is None and pc_online("[]", "box-1") is None
        assert pc_online('{"Peer": {"a": 1}}', "box-1") is None and pc_online(ts(("", True)), "") is None
        assert isinstance(NET_WATCH[2], str), "NET_WATCH shadowed: net_loop would die on its first round"
        big_ = job_row(J[0], "w", t0, True)
        assert 'class="shot"' in big_ and "href" not in big_ and "thumb" not in big_ and "{" not in big_
        (hp / "big.png").write_bytes(b"0" * (IMG_MAX + 1))
        pj("t", image=str(hp / "state.db")); pj("b", image=str(hp / "big.png")); pj("rel", image="p.png")
        assert job_image("r", hp) == (b"\x89PNG", "image/png") and job_image("fin", hp) is None
        assert all(job_image(n_, hp) is None for n_ in ("../r", ".hid", "t", "b", "rel", "nope"))
        old_ = dict(SET)
        SET.update(tg_bot="off", push="telegram")
        assert tg_target() is None and send_push("t", "x")[0] is False and push_on() is False
        SET.clear(); SET.update(old_)
        assert "live." not in live_card(dict(runs=[], n=0, up=False, down=[], next=[(t0, "x")], at=t0), "w")


def selftest_chats():
    """Bots & chats: chat names from origin_json with every fallback, subagents in their starter's chat, grouping per bot."""
    _req.lang = "en"
    db = sqlite3.connect(":memory:")
    db.execute("create table sessions(id, source, title, started_at, parent_session_id, chat_id, chat_type, thread_id, display_name, origin_json)")
    tg = lambda th, topic=None: json.dumps({"platform": "telegram", "chat_id": "42", "chat_name": "Sam", "chat_type": "dm", "thread_id": th,
                                            **({"chat_topic": topic} if topic else {})})
    db.executemany("insert into sessions values (?,?,?,?,?,?,?,?,?,?)", [
        ("a", "telegram", "A", 1, None, "42", "dm", "7", "Sam", tg("7", "Shop")),
        ("b", "telegram", "B", 2, None, "42", "dm", "7", "Sam", tg("7")),   # same topic, name not stored with this one
        ("c", "telegram", "C", 3, None, "42", "dm", "9", "Sam", tg("9")),   # a topic nobody named
        ("d", "discord", "D", 4, None, "55", "thread", "55", "Sam", json.dumps({"platform": "discord", "chat_id": "55", "chat_type": "thread",
         "chat_name": "Guild / #dev / release", "chat_topic": "what the channel is about", "thread_id": "55"})),
        ("e", "cron", "Daily report · Oct 08 16:31", 5, None, None, "dm", None, None, json.dumps({"platform": "local", "chat_type": "dm"})),
        ("f", "telegram", "F", 6, None, "42", "dm", None, "Sam", None),   # old row without origin_json
        ("g", "subagent", "G", 7, "b", None, None, None, None, None),
        ("h", "subagent", "H", 8, "g", None, None, None, None, None),     # a subagent's subagent
        ("i", "slack", "I", 9, None, "C1", "group", "171.2", None, json.dumps({"platform": "slack", "chat_id": "C1", "chat_type": "group",
         "chat_name": "#ops", "thread_id": "171.2"})),
        ("j", "subagent", "J", 10, "gone", None, None, None, None, None)])
    ci = chat_index(db)
    lab = lambda k: chat_label(ci[k][1])
    assert [lab(k) for k in "abcdfi"] == [("Shop", "Sam"), ("Shop", "Sam"), ("Topic 9", "Sam"), ("release", "Guild / #dev"), ("Sam", ""),
                                          ("#ops", "Thread")], ci
    assert ci["a"][1]["key"] == ci["b"][1]["key"] == "telegram:42:7" and ci["d"][0] == "discord" and ci["f"][1]["key"] == "telegram:42:"
    assert ci["e"] == ("cron", {**NOCHAT, "key": "cron:Daily report", "name": "Daily report", "kind": "cron"})
    assert ci["g"] == ci["h"] == ("subagent", ci["b"][1]) and ci["j"] == ("subagent", NOCHAT) and lab("j") == ("No chat recorded", "")
    old_db = test_db()[0]   # an older Hermes without the chat columns
    assert chat_index(old_db)["s"] == ("telegram", {**NOCHAT, "key": "telegram::"}) and chat_index(old_db)["c"][1]["name"] == "Daily report"
    sess = {k: dict(agent="w" if k in "gh" else "", src="x", platform=ci[k][0], chat=ci[k][1], cost=float(n)) for n, k in enumerate("abcdegh", 1)}
    g = by_bot(sess)
    assert set(g) == {":telegram", ":discord", ":cron", "w:subagent"} and g[":telegram"][0] == 6.0 and g["w:subagent"][0] == 13.0
    assert {k: (v[0], v[2]) for k, v in g[":telegram"][1].items()} == {"telegram:42:7": (3.0, 2), "telegram:42:9": (3.0, 1)}
    assert list(g["w:subagent"][1]) == ["telegram:42:7"] and bot_key({"src": "cli"}) == ":cli"


def selftest_wrapped():
    """Wrapped on the demo month (moved into October 2026, every project path made /opt/shop) and on an empty month."""
    global HERMES, DATA
    old, D = (HERMES, DATA), datetime(2026, 10, 1).date()
    with tempfile.TemporaryDirectory() as tmp:
        HERMES, DATA = Path(tmp), Path(tmp) / "data"
        DATA.mkdir()
        db = demo_db(datetime(2026, 10, 31, 12).timestamp())   # data from Oct 1 12:00 to Oct 31 12:00
        db.execute("update messages set tool_calls = replace(replace(tool_calls, '/srv/blog', '/opt/shop'), '/opt/wiki', '/opt/shop')")
        db.commit()
        db.backup(disk := sqlite3.connect(HERMES / "state.db"))
        disk.close()
        t = lambda d, h: datetime(2026, 10, d, h).timestamp()
        (DATA / "limits.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"t": t(2, 9), "a": "x", "five_hour": 50, "seven_day": 20}, {"t": t(2, 9), "a": "y", "five_hour": 0, "seven_day": 5},
            {"t": t(2, 10), "a": "x", "five_hour": 100, "seven_day": 30}, {"t": t(2, 10), "a": "y", "five_hour": 0, "seven_day": 5},
            {"t": t(2, 11), "a": "x", "five_hour": 100, "seven_day": 31}, {"t": t(2, 11), "a": "y", "five_hour": 40, "seven_day": 9},
            {"t": t(9, 9), "a": "x", "five_hour": 0, "seven_day": 100}, {"t": t(9, 9), "a": "y", "five_hour": 90, "seven_day": 60},
            {"t": t(9, 10), "a": "x", "five_hour": 30, "seven_day": 100}, {"t": t(9, 10), "a": "y", "five_hour": 90, "seven_day": 60}]))
        w = wrapped(D, datetime(2026, 11, 1).date())
        q = lambda sql: db.execute(sql).fetchall()
        assert w["sessions"] == q("select count(distinct session_id) from session_model_usage")[0][0], w["sessions"]
        assert w["tokens"] == q("select sum(input_tokens + cache_read_tokens + cache_write_tokens + output_tokens) from session_model_usage")[0][0]
        assert w["messages"] == sum(w["hours"]) == q("""select count(*) from messages m join sessions s on s.id = m.session_id
            where m.role = 'user' and s.source not in ('cron', 'subagent')""")[0][0]
        active = {datetime.fromtimestamp(x).date() for (x,) in q("""select m.timestamp from messages m join sessions s on s.id = m.session_id
            where m.role = 'user' and s.source not in ('cron', 'subagent')""")}
        run = [D + timedelta(days=i) in active for i in range(31)]
        assert len(w["days"]) == 31 and [bool(n) for n in w["days"]] == run, w["days"]
        assert w["streak"] == max(len(x) for x in "".join("1" if r else " " for r in run).split()), w["streak"]
        assert w["projects"][0][0] == "shop" and w["projects"][0][2] > FOCUS and w["type"] == "focus", (w["projects"], w["type"])
        assert w["tools"][0][0] == "terminal" and w["models"][0][0] == "Opus 5.5" and w["night"] == 0 and 8 <= w["peak_hour"] <= 23
        assert w["limits"] == dict(hits={"five_hour": 1, "seven_day": 1}, tightest=dict(used=100, t=t(9, 9), a="x"), switches=2), w["limits"]
        assert w["biggest"]["tokens"] == max(n for (n,) in q("""select sum(input_tokens + cache_read_tokens + cache_write_tokens + output_tokens)
            from session_model_usage group by session_id"""))
        assert w["prev"] is None and not w["partial"] and w["subagents"] == dict(n=0, cost=0, models=[])
        json.dumps(w)
        e = wrapped(datetime(2026, 8, 1).date(), datetime(2026, 9, 1).date())   # no data at all
        assert (e["sessions"], e["tokens"], e["projects"], e["biggest"], e["latest"], e["streak"], e["prev"], e["type"]) == (0, 0, [], None, None, 0, None, "explorer")
        assert wrapped(datetime(2026, 1, 1).date(), datetime(2027, 1, 1).date())["prev"] is None   # a year, 2025 empty
        now_ = datetime.now().date()
        assert wrapped(now_.replace(day=1), (now_.replace(day=28) + timedelta(days=4)).replace(day=1))["partial"]
        assert wrap_type({**w, "limits": {"hits": {"five_hour": 3}}}) == "edge" and wrap_type({**w, "subagents": {"cost": w["cost"]}}) == "conductor"
        assert wrap_type({**w, "night": 0.5}) == "owl" and wrap_type({**w, "projects": [], "days": [1, 1, 1, 1, 0]}) == "steady"
        assert wrap_type({**w, "projects": [], "days": [1, 0, 0]}) == "explorer"
        for code in LOC:   # the slides: every one for the demo month, one calm slide for an empty month, none for junk
            _req.lang = code
            pg, em = page_wrapped({"m": ["2026-10"]}), page_wrapped({"m": ["2026-08"]})
            text = re.sub(r"<[^>]+>", " ", pg + em + page_wrapped({"y": ["2026"]}))
            assert pg.count('<section class="ws"') == 7 and em.count('<section class="ws"') == 1 and tr("wrap.empty") in em, (pg, em)
            assert "{" not in text and "wrap." not in text and 'class="pet"' in pg and "refresh" not in pg, text
        sep = datetime(2026, 9, 1).date()   # September = the demo month with fewer of everything: a comparison line under each big number
        WRAP[(sep, D, SET["source"])] = (0, {**w, "prev": None, "tokens": w["tokens"] // 2, "streak": 2, "projects": [["shop", 1.0, 1.0]], "tools": [["terminal", 10, 1.0]],
                                             "limits": {**w["limits"], "hits": {"five_hour": 1, "seven_day": 0}}, "start": sep.isoformat()})
        _req.lang = "en"
        cm = page_wrapped({"m": ["2026-10"]})
        assert cm.count('class="cmp"') == 5 and "vs Sep</p>" in cm and 'class="cmp"' not in page_wrapped({"m": ["2026-08"]}), cm   # tokens, project, tool, streak, limits (no subagents in Oct)
        assert 'class="cmp"' not in page_wrapped({"y": ["2026"]})   # 2025 is empty
        _req.lang = "de"
        assert "zu Sept.</p>" in page_wrapped({"m": ["2026-10"]})
        _req.lang = "en"
        assert wrap_months() == {"2026-10"} and wrap_links(datetime(2026, 11, 20)) == ('<p class="hint">Wrapped: <a href="/wrapped?m=2026-10">Oct</a>'
                                                                                         ' · <a href="/wrapped?y=2026">2026</a></p>'), wrap_links(datetime(2026, 11, 20))
        assert "Oct 2026</a>" in wrap_links(datetime(2027, 3, 1))
        assert recap_month(datetime(2026, 11, 1)) == recap_month(datetime(2026, 11, 7)) == "2026-10" and recap_month(datetime(2026, 11, 8)) is None
        assert recap_month(datetime(2027, 1, 3)) == "2026-12"
        assert page_wrapped({"m": ["2026-13"]}) is page_wrapped({"y": ["1999"]}) is None and wrap_span({"y": ["2026"]})[1] == datetime(2027, 1, 1).date()
        k = (D, datetime(2026, 11, 1).date(), SET["source"])   # cache: finished kept, partial kept for WRAP_TTL
        fin = WRAP[k] = (0, WRAP[k][1])
        part = {**fin[1], "partial": True}
        assert wrap_period(*k[:2]) is fin[1]
        WRAP[k] = (time.time(), part)
        assert wrap_period(*k[:2]) is part and wrap_period(*k[:2], 0) is not part   # warm() refreshes with ttl 0
        WRAP[k] = (time.time(), part)
        WRAP[k] = (time.time() - WRAP_TTL - 1, part)
        assert wrap_period(*k[:2]) is not part and not WRAP[k][1]["partial"]
        busy = threading.Event()   # a cold period: the page answers with a slide that reloads itself
        WRAPPING[k] = threading.Thread(target=busy.wait, daemon=True)
        WRAPPING[k].start()
        cold = page_wrapped({"m": ["2026-10"]})
        busy.set()
        assert 'http-equiv="refresh"' in cold and tr("wrap.loading") in cold, cold
    HERMES, DATA = old
    CACHE.clear(); WRAP.clear(); WRAPPING.clear()


def selftest_bark():
    """Bark: payload, Basic Auth, levels, body cut, code 200 only; Hermes' bark.env; hermes mode falls back to ntfy."""
    got = []

    class Fake(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            code, out = (200, {"code": 200}) if got[-1][2]["device_key"] == "K" else (400, {"code": 400, "message": "failed to get device token"})
            b = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base, old = f"http://127.0.0.1:{srv.server_port}", dict(SET)
    try:
        SET.update(push="bark", bark_url=base, bark_key="K", bark_user="u", bark_pass="p", url="https://dash.example")
        assert push_on() and send_push("T", "x" * 3000, 4, "/s/1") == (True, '200 {"code": 200}')
        path, auth, b = got[-1]
        assert path == "/push" and auth == "Basic " + base64.b64encode(b"u:p").decode()
        assert (b["title"], len(b["body"]), b["level"], b["group"], b["url"]) == ("T", BARK_MAX, "timeSensitive", "usagecast", "https://dash.example/s/1")
        assert [send_bark(bark_target(), "t", "", q)[0] and got[-1][2]["level"] for q in (3, 2)] == ["active", "passive"] and got[-1][2]["body"] == " "
        SET.update(bark_user="", bark_pass="", url="")
        assert send_push("t", "m", 3)[0] and got[-1][1] is None and "url" not in got[-1][2]
        SET["bark_key"] = "bad"
        ok, reply = send_push("t", "m", 3)
        assert not ok and reply.startswith("400 "), reply
        SET["bark_url"] = ""
        assert bark_target() is None and not push_on() and send_push("t", "m")[0] is False
        with tempfile.TemporaryDirectory() as tdir:   # Hermes' Bark, and hermes mode falling back to Hermes' ntfy
            h = Path(tdir)
            assert hermes_bark(h) is None
            (h / "secrets").mkdir()
            (h / "secrets" / "bark.env").write_text(f"BARK_URL={base}/\nBARK_USER=hu\nBARK_PASS='hp'\nBARK_KEY=K\n")
            assert hermes_bark(h) == (base, "K", "hu", "hp")
            (h / "secrets" / "bark.env").write_text(f"BARK_URL={base}\n")
            assert hermes_bark(h) is None
        g = globals()
        hb, hn, sent = g["hermes_bark"], g["hermes_ntfy"], []

        class Ntfy(BaseHTTPRequestHandler):
            def do_POST(self):
                sent.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        nsrv = ThreadingHTTPServer(("127.0.0.1", 0), Ntfy)
        threading.Thread(target=nsrv.serve_forever, daemon=True).start()
        try:
            g["hermes_ntfy"] = lambda: (f"http://127.0.0.1:{nsrv.server_port}", "topic", None)
            SET["push"] = "hermes"
            g["hermes_bark"] = lambda home=HERMES: (base, "K", "", "")
            n = len(got)
            assert send_push("t", "m", 3)[0] and len(got) == n + 1 and not sent   # Bark took it
            g["hermes_bark"] = lambda home=HERMES: (base, "bad", "", "")
            assert send_push("t", "m", 3)[0] and sent and sent[-1]["topic"] == "topic"   # Bark refused: ntfy
            g["hermes_bark"] = lambda home=HERMES: None
            assert push_on() and send_push("t", "m", 3)[0] and len(sent) == 2   # no Bark in Hermes: ntfy as before
        finally:
            g["hermes_bark"], g["hermes_ntfy"] = hb, hn
            nsrv.shutdown()
        cur = {**SET, "bark_pass": "old"}   # the password field: empty keeps, the clear box removes, never echoed
        assert clean({"bark_pass": ""}, cur, SECTIONS["notify"])["bark_pass"] == "old"
        assert clean({"bark_pass": "new"}, cur, SECTIONS["notify"])["bark_pass"] == "new"
        assert clean({"bark_pass_clear": "1"}, cur, SECTIONS["notify"])["bark_pass"] == ""
        c = clean({"bark_url": "javascript:x", "bark_key": "a b", "bark_user": "a:b"}, {**cur, "bark_url": "https://b.example"}, SECTIONS["notify"])
        assert (c["bark_url"], c["bark_key"], c["bark_user"]) == ("https://b.example", cur["bark_key"], cur["bark_user"])
        _req.lang = "en"
        SET.update(push="bark", bark_pass="secret-pw")
        page = page_settings({}, "notify")
        assert 'value="bark"' in page and 'class="ch-bark"' in page and "secret-pw" not in page and 'name="bark_pass_clear"' in page
    finally:
        SET.clear()
        SET.update(old)
        srv.shutdown()


def selftest():
    selftest_v4()
    selftest_chats()
    selftest_bark()
    global HERMES, DATA, CLAUDE
    SET.clear(); SET.update(DEFAULTS); FX.clear(); FC.clear()   # the installed settings must not change the expected formats
    selftest_wrapped()
    # Locales: same keys and placeholders everywhere, every literal key used in the code exists
    src = Path(__file__).read_text("utf-8")
    used = set(re.findall(r'tr\(\s*"([\w.:-]+)"(?=\s*[,)])', src)) | set(re.findall(r'"(th\.[\w]+)"', src))  # "x." + k: below
    used |= {f"comp.{k}" for k in FIXED} | {f"comp.{k}.why" for k in FIXED} | {f"tip.tool.{k}" for k in TOOL_HINTS}
    used |= {f"period.{k}" for k in PERIODS} | {f"win.{k}" for k, _ in WINDOWS} | {f"since.{k}" for k in ("w", "1", "7", "30")}
    used |= {f"seg.{k}" for k, _ in HERMES_MARKERS} | {"seg.soul", "seg.misc", "seg.notes", "tip.ttl1h.text", "tip.ttl5m.text"}
    used |= {f"brk.{k}" for k in BREAKS} | {f"brk.{k}.why" for k in BREAKS}
    used |= {f"wrap.type.{k}" for k in WRAP_TYPES} | {f"wrap.type.{k}.why" for k in WRAP_TYPES}
    used |= {"hist.per.hour", "hist.per.day", "ses.after_pause", "ses.no_pause", "foot.ntfy.on", "foot.ntfy.off", "tip.ttl1h", "tip.ttl5m"}
    for code, texts in LOC.items():
        assert set(texts) == set(LOC["en"]), (code, set(texts) ^ set(LOC["en"]))
        for k, v in texts.items():
            if isinstance(v, str) and not k.startswith("fmt."):  # date patterns use different fields per language
                assert set(re.findall(r"{(\w+)", v)) == set(re.findall(r"{(\w+)", LOC["en"][k])), (code, k)
    assert not used - set(LOC["en"]), used - set(LOC["en"])
    assert pick_lang("", {"Accept-Language": "de-DE,de"}) == DEFAULT_LANG and pick_lang("", {"Cookie": "a=1; lang=de"}) == "de"
    assert pick_lang("en", {"Cookie": "lang=de"}) == "en" and pick_lang("xx", {}) == DEFAULT_LANG
    # Number and date formats per language
    _req.lang = "de"
    assert (money(1234.5), pct(1, 8), num(1_234_567), num(5400), cnt(12345)) == ("1.234,50 $", "12,5 %", "1,2 Mio.", "5 Tsd.", "12.345")
    assert when(datetime(2026, 10, 8, 9, 5).timestamp(), "daytime") == "Do 08.10. 09:05"
    _req.lang = "en"
    assert (money(1234.5), pct(1, 8), num(1_234_567), num(5400), money(0.001)) == ("$1,234.50", "12.5%", "1.2M", "5K", "< $0.01")
    assert when(datetime(2026, 10, 8, 9, 5).timestamp(), "daytime") == "Thu Oct 8, 09:05"
    SET.update(currency="EUR", numbers="full"); FX["rate"] = 1.25
    assert (money(10), num(1_234_567)) == ("€8.00", "1,234,567")
    SET.update(currency="USD", numbers="compact"); FX.clear()
    SET["cost_unit"], RATE["k"] = "week", 0.5
    assert (money(4), money(0.1), cash(4), dur(59 * 60), dur(80 * 60)) == ("2.0% of week", "< 0.1% of week", "$4.00", "~59 min", "~1 h 20 min")
    t = table(["th.cost"], [(money(4),)])
    assert "<th>% of week</th>" in t and "<td>2.0%</td>" in t and ">2.0% of week</span>" in bars([("x", 4.0)], 8.0, 300, False), t
    SET["cost_unit"] = "money"; RATE.clear()
    assert (signed(100.3, 100), signed(120, 100), signed(90, 100)) == ("±0%", "+20%", "−10%")
    assert origin("cron", None) == "cron:" and origin_label("cron:") == "Cron: Untitled" and origin_label("cron:a") == "Cron: a"
    assert '<span class="fname" id="theme-l">' in field("theme", "T", opts("theme", [("system", "S")])) and '<label for="x"' in field("x", "X", '<input id="x">')
    # Limit split: the growth of each hour goes to whoever ran then, gaps without readings count for nobody
    vals = [10 + i for i in range(7)] + [16 + i for i in range(1, 7)] + [22 + 0.5 * i for i in range(1, 7)]
    hist = [{"t": 7200 + 600 * i, "seven_day": v} for i, v in enumerate(vals)] + [{"t": 7200 + 600 * 18 + 3600, "seven_day": 27}]
    sp = limit_split(hist, {7200: {"tool:x": 6.0}}, [(10800 + 100, 2.0)], 0)
    assert {k: sp[k] for k in ("hermes", "cc", "rest", "gap", "before", "hours")} == {"hermes": 6, "cc": 6, "rest": 3, "gap": 2, "before": 10, "hours": 3}, sp
    assert sp["k"] == 1.5 and limit_split(hist[:1], {}, [], 0) is None
    assert (signed(12, 10), signed(8, 10)) == ("+20%", "−20%")
    t0 = 1_800_000_000.0
    d30 = {"hours": {t0 - 8 * 86400: {"x": 10.0}, t0 - 3600: {"x": 12.0}}}
    assert week_delta(d30, 12.0, t0 - 86400, t0) == "+20% vs. last week" and week_delta(d30, 12.0, t0 - 20 * 86400, t0) == ""
    assert span_sum({"hours": {3600: {"x": 1.0}}}, 3600.4, 7200) == span_sum({"hours": {3600: {"x": 1.0}}}, 3599.6, 7200) == 1.0  # resets a few ms off
    # Config, markers, prices
    cfg = ("model:\n  default: m\n  provider: anthropic\nmemory:\n\n  provider: memory_tencentdb\n"
           "plugins:\n  enabled:\n    - ponytail\n    - 'superpowers'\n    - platforms/ntfy\n  disabled: []\n")
    assert config_value("model", "provider", cfg) == "anthropic" and config_value("memory", "provider", cfg) == "memory_tencentdb"
    assert plugin_names(cfg) == ["ponytail", "superpowers", "memory_tencentdb"], plugin_names(cfg)
    pm, im = markers(plugin_names(cfg))
    s = segments("hello\n\nPONYTAIL MODE ACTIVE x\n<EXTREMELY_IMPORTANT>\nsuperpowers y\n[Note: model changed]", im)
    assert [l for l, _ in s] == ["misc", "plugin:ponytail", "plugin:superpowers", "notes"], s
    assert segments("me\n# memory-tencentdb\nx", pm)[-1][0] == "plugin:memory_tencentdb"   # - and _ are equivalent
    assert price("claude-opus-5-5")[0] == 4e-6 and price("claude-opus-4-8")[4] == 25e-6 and price("claude-opus-4-1-x")[0] == 15e-6
    assert known_model("claude-opus-5-5") and not known_model("claude-opus-9")
    assert [model_name(m) for m in ("claude-opus-5-5", "claude-haiku-4-5-20251001", "claude-sonnet-5", "gpt-image-2", None)] == \
        ["Opus 5.5", "Haiku 4.5", "Sonnet 5", "gpt-image-2", ""]
    CACHE["t"] = (0, "stale"); REFRESHING.add("t")
    assert data_for("t") == "stale"; CACHE.pop("t"); REFRESHING.discard("t")
    # Replay and cache
    db, msgs = test_db()
    calls, _ = simulate(msgs, {"schema": 100.0})
    splits = [cache_split(b, n, g, 300) for _, g, b, n, _, _ in calls]
    assert [x[2] > 0 for x in splits] == [False, False, True], splits     # pause > 5 min: rebuild
    assert "tool:terminal" in splits[1][1] and splits[1][0]["schema"] == 100  # result written anew, prefix read
    assert not cache_split(calls[2][2], calls[2][3], calls[2][1], 3600)[2]    # no rebuild with 1 h
    hit, new, lost = cache_split({"schema": 100, "user": 100}, {"schema": 100, "user": 150}, 1, 300, rho=0.5)
    assert hit == {"schema": 100, "user": 0} and new == {"user": 50} and lost == 100  # log: the cache returns the beginning
    snap = {"tools": {"terminal": 100.0}}
    d = analyze(db, -1, snap, 300, logs={})
    real = sum(sum(row_cost("claude-opus-5-5", *r, 300)) for r in [(10, 5000, 3000, 400), (0, 2000, 500, 100), (5, 1000, 800, 50)])
    assert abs(d["total"] - real) < 1e-12, (d["total"], real)               # nothing gets lost, nothing gets added
    assert d["comp"]["rebuild"] > 0 and d["comp"]["task:background_review"] > 0 and d["comp"]["tool:terminal"] > 0
    assert d["where"].keys() == {"telegram", "cron:Daily report"} and d["sess"]["s"]["project"] == "shop"
    late = analyze(db, 999, snap, 300, logs={})    # the period cuts by call time
    early = analyze(db, -1, snap, 300, logs={}, until=999)
    assert abs(early["total"] + late["total"] - d["total"]) < 1e-12 and early["total"] > 0     # until cuts the same way
    a, b = custom("2026-10-07..2026-10-01")
    assert (a, b) == (datetime(2026, 10, 1).timestamp(), datetime(2026, 10, 8).timestamp()) and custom("2026-13-01..2026-10-01") is None and not custom("7")
    assert 0 < late["total"] < d["total"] and late["comp"]["rebuild"] > 0 and "tool:terminal" not in late["uses"]
    logged = analyze(db, -1, snap, 300, logs={"s": ([1.0, 3.0, 1001.0], [(5000, 0), (6000, 0), (6100, 0)])})
    assert logged["comp"]["break"] > 0 and abs(logged["total"] - real) < 1e-12 and logged["logged"] == 3  # break without pause
    assert abs(sum(v for _, v in logged["breaks"].values()) - logged["comp"]["break"]) < 1e-12 and set(logged["breaks"]) <= set(BREAKS)
    R = lambda role, content="": (role, content, None, None, None, None, 0, None)
    rows, ap = [R("user"), R("assistant"), R("tool", "[screenshot] " * 4), R("assistant"), R("user", "next"), R("assistant"), R("tool", "x"), R("assistant")], [1, 3, 5, 7]
    rl = [(100, 0), (200, 150), (300, 100), (250, 40)]
    assert [break_cause(rows, ap, j, rl, 50) for j in (1, 2, 3)] == ["image", "turn", "shrunk"]
    rl[3] = (350, 50)
    assert (break_cause(rows, ap, 3, rl, 50), break_cause(rows, ap, 3, rl, 10), break_cause(rows, ap, 3, rl[:3] + [None], 10)) == ("deep", "other", "sim")
    assert abs(sum(sum(v.values()) for v in d["hours"].values()) - d["total"]) < 1e-12        # the history adds up
    assert sum(d["hsteps"].values()) == d["n_steps"] == 3
    assert [x[1:3] + (x[4],) for x in d["heavy"]] == [("s", "terminal", 1)] and d["heavy"][0][0] > 0   # written by the next call, read by 1 later
    # Projects: majority of paths, Hermes' own folder only without another project, system folders don't count
    rx, repos = project_rx("/h/u", "/h/u/.hermes"), {"code/app": "app", "tool": "tool"}
    assert project_of(['{"command": "cat ~/.hermes/x; cd /opt/shop && ls /opt/shop/src"}'], repos, rx) == "shop"
    assert project_of(["~/.hermes/skills/a", "/h/u/.hermes/b"], repos, rx) == HERMES_PROJ
    assert project_of(["/opt/homebrew/bin/x", "~/.hermes/cache/y", "~/Downloads/z"], repos, rx) is None
    assert project_of(["/h/u/code/app/main.py", "$HOME/tool/x"], repos, rx) in ("app", "tool")
    assert project_of([], repos, rx, "/srv/git/werk") == "werk"
    assert project_of(["~/.hermes/a"] * 12 + ["/opt/shop"] * 2, repos, rx) == HERMES_PROJ  # a side mention doesn't count
    # Forecast: linear since the week started, 5-hour pace only from points of the running window
    assert abs(week_forecast(34, 0.47) - 72.34) < 0.01 and week_forecast(5, 0.05) is None
    hist = [{"t": 1000, "five_hour": 40}, {"t": 2800, "five_hour": 50}]
    assert abs(rate(hist, "five_hour", 60, 0, 4600) - 20 / 3600) < 1e-12 and rate(hist, "five_hour", 60, 3000, 4600) is None
    # Alerts: once per window, extra credits only from the second reading on and only when they rise
    now = 1_800_000_000.0
    iso = lambda t: datetime.fromtimestamp(t).astimezone().isoformat()
    L = {"five_hour": {"utilization": 80, "resets_at": iso(now + 7200)}, "seven_day": {"utilization": 60, "resets_at": iso(now + 3.5 * 86400)},
         "extra_usage": {"is_enabled": True, "used_credits": 100, "monthly_limit": 2500, "decimal_places": 2, "currency": "EUR"}}
    hist = [{"t": now - 3000, "five_hour": 40}]                   # 40 % per 50 min: full in 25 min, the week lands at 120 %
    msgs, st = alerts(L, hist, {}, now)
    assert sorted(t.split(":")[0] for t, _, _ in msgs) == ["five_hour", "seven_day"] and st["extra_used"] == 100, msgs
    sent = {**st, **{t: now for t, _, _ in msgs}}
    assert alerts(L, hist, sent, now + 60)[0] == []
    L["extra_usage"]["used_credits"] = 150
    msgs, st = alerts(L, hist, sent, now + 60)
    assert [t.split(":")[0] for t, _, _ in msgs] == ["extra"] and st["extra_used"] == 100  # state only after sending
    assert alerts(L, hist, {**st, msgs[0][0]: now}, now + 120) == ([], {**st, msgs[0][0]: now, "extra_used": 150})
    kinds = lambda s: [t.split(":")[0] for t, _, _ in alerts(L, hist, {}, now, {**SET, **s})[0]]
    # Free again within the hour after a full 5-hour window; weekly steps: only the highest one, each once
    L2 = {"five_hour": {"utilization": 100, "resets_at": iso(now + 600)}, "seven_day": {"utilization": 85, "resets_at": iso(now + 43200)}}
    m, st = alerts(L2, [], {}, now)
    assert [t.split(":")[0] for t, _, _ in m] == ["seven_day@80"] and st["full:five_hour"] == now + 600, (m, st)
    L2.update(five_hour={"utilization": 0, "resets_at": None}, seven_day={"utilization": 91, "resets_at": iso(now + 43200)})
    sent2 = {**st, m[0][0]: now}
    m2, st2 = alerts(L2, [], sent2, now + 700)
    assert sorted(t.split(":")[0] for t, _, _ in m2) == ["free", "seven_day@90"], m2
    assert alerts(L2, [], {**st2, **{t: now for t, _, _ in m2}}, now + 800)[0] == [] and alerts(L2, [], sent2, now + 4300)[0][0][0].startswith("seven_day@90")
    assert alerts(L2, [], sent2, now + 700, {**SET, "alert_free": False, "alert_steps": False})[0] == []
    sun = datetime(2026, 10, 11, 19, 0).timestamp()   # a Sunday evening
    fake = {"w": {"since": sun - 6 * 86400, "total": 10.0, "comp": {"think": 6.0, "tool:terminal": 4.0}, "ttl": 300},
            "30": {"hours": {sun - 10 * 86400: {"think": 8.0}, sun - 5 * 86400: {"think": 10.0}}}}
    dg = digest(sun, {}, {"seven_day": {"utilization": 70, "resets_at": iso(sun + 86400)}}, fake.get)
    assert dg[0] == "digest:2026-41" and "+25%" in dg[2] and "$10.00" in dg[2] and "70%" in dg[2], dg
    assert digest(sun, {dg[0]: sun}, {}, fake.get) is None and digest(sun - 3600 * 2, {}, {}, fake.get) is None
    assert digest(sun, {}, {}, fake.get, {**SET, "alert_digest": False}) is None
    assert kinds({"alert_five": False}) == kinds({"five_min": 20}) == ["seven_day"] and kinds({"alert_week": False}) == ["five_hour"]
    # Page loads never wait for a limits fetch once there is a reading; only the very first one is waited for
    saved, orig, calls = dict(STATE), refresh_limits, []
    globals()["refresh_limits"] = lambda age: (time.sleep(0.5), calls.append(age))
    STATE.update(limits={"seven_day": {}}, at=0.0)
    t0 = time.time(); fresh_limits(); assert time.time() - t0 < 0.25
    STATE.update(limits=None)
    t0 = time.time(); fresh_limits(); assert time.time() - t0 >= 0.5 and calls
    globals()["refresh_limits"] = orig
    STATE.clear(); STATE.update(saved)
    # Settings: a missing checkbox is off, invalid input keeps the old value, the token stays unless removed
    s = clean({"theme": "dark", "currency": "XXX", "five_min": "3", "ntfy_topic": "bad topic", "quiet_from": "25:00",
               "url": "javascript:x", "ntfy_token": ""}, {**SET, "ntfy_token": "tk"})
    assert (s["theme"], s["currency"], s["five_min"], s["ntfy_topic"], s["quiet_from"], s["url"], s["ntfy_token"], s["alert_five"]) == \
        ("dark", SET["currency"], 5, SET["ntfy_topic"], SET["quiet_from"], SET["url"], "tk", False), s
    assert clean({"token_clear": "1", "ntfy_token": "x"}, {**SET, "ntfy_token": "tk"})["ntfy_token"] == ""
    assert clean({"url": ""}, {**SET, "url": "https://a"})["url"] == "" and clean({"url": "https://a.b:8443/"}, SET)["url"] == "https://a.b:8443"
    qs, at = {**SET, "quiet": True, "quiet_from": "22:00", "quiet_to": "07:00"}, lambda h, m: datetime(2026, 10, 8, h, m).timestamp()
    assert quiet_now(at(23, 0), qs) and quiet_now(at(6, 59), qs) and not quiet_now(at(7, 0), qs) and not quiet_now(at(23, 0), SET)
    assert quiet_now(at(13, 0), {**qs, "quiet_from": "12:00", "quiet_to": "14:00"}) and not quiet_now(at(15, 0), {**qs, "quiet_from": "12:00", "quiet_to": "14:00"})
    assert same_origin({"Sec-Fetch-Site": "same-origin"}) and not same_origin({"Sec-Fetch-Site": "cross-site", "Origin": "http://h", "Host": "h"})
    assert same_origin({"Origin": "http://h:1", "Host": "h:1"}) and not same_origin({"Origin": "http://evil", "Host": "h:1"}) and not same_origin({"Host": "h"})
    assert re.fullmatch(r"usagecast-[0-9a-f]{16}", new_topic())
    # Every page renders in every language without leftover locale keys
    with tempfile.TemporaryDirectory() as tmp:
        HERMES, DATA = Path(tmp), Path(tmp) / "data"
        db, _ = test_db(time.time() - 3600)
        db.backup(disk := sqlite3.connect(Path(tmp) / "state.db"))
        disk.execute("insert into sessions values ('k', 'subagent', 'Subagent: check…', null, ?, null, 's')", (time.time() - 1800,))
        disk.execute("insert into messages (session_id, role, content, timestamp) values ('k', 'user', 'check <all> of it', ?), ('k', 'assistant', 'ok', ?)", (time.time() - 1800, time.time() - 1700))
        disk.execute("insert into session_model_usage values ('k', 'claude-opus-5-5', '', 2, 10, 4000, 1000, 200, ?)", (time.time() - 1700,))
        for col in ("chat_id", "chat_type", "thread_id", "display_name", "origin_json"):
            disk.execute(f"alter table sessions add column {col}")
        disk.execute("update sessions set origin_json = ? where id = 's'", (json.dumps(
            {"platform": "telegram", "chat_id": "42", "chat_name": "Sam", "chat_type": "dm", "thread_id": "7", "chat_topic": "Shop"}),))
        disk.commit()
        disk.close()
        STATE["at"] = time.time()  # no limit fetch during the test
        # Source: only a profile with its own state.db can be chosen, anything else keeps the old value
        (HERMES / "profiles" / "work").mkdir(parents=True)
        (HERMES / "profiles" / "junk").mkdir()
        db.backup(pdb := sqlite3.connect(HERMES / "profiles" / "work" / "state.db"))
        pdb.close()
        assert profiles() == ["work"] and clean({"source": "work"}, SET)["source"] == "work"
        assert clean({"source": "../x"}, SET)["source"] == clean({"source": "junk"}, SET)["source"] == SET["source"]
        SET["source"] = "work"
        assert source_home() == HERMES / "profiles" / "work" and snap_path().name == "snapshot-work.json"
        assert fix("tools", "list") == '<code class="cmd fix">hermes -p work tools list</code>' and "work" in source_name()
        SET["source"] = "gone"
        assert source_home() == HERMES and fix("x") == '<code class="cmd fix">hermes x</code>' and source_name() == "Hermes"
        SET["source"] = ""
        assert idle_sets({"a": 2, "b": 1, "c": 5, "d": 1}, {"a": ["web"], "b": ["web", "search"], "c": ["browser"]}, {"b"}) \
            == [("browser", 5)]
        # Limits card: pace verdict and daily budget with half the week gone
        old, nw, _req.lang = STATE["limits"], time.time(), "en"
        STATE["limits"] = {"seven_day": {"utilization": 60, "resets_at": iso(nw + 3.5 * 86400)},
                           "five_hour": {"utilization": 30, "resets_at": iso(nw + 3600)}}
        STATE["limits"]["extra_usage"] = {"is_enabled": True, "used_credits": 501, "monthly_limit": 2500, "decimal_places": 2, "currency": "EUR"}
        RATE.update(k=0.5, hours=10)
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "limits.jsonl").write_text("".join(json.dumps({"t": nw - 3 * 86400 + i * 3600, "seven_day": 50 + i}) + "\n" for i in range(3)))
        c = limits_card(None)
        assert len(limit_history()) == 3
        with open(DATA / "limits.jsonl", "a") as f:
            f.write(json.dumps({"t": nw, "seven_day": 61}) + "\n" + '{"t": ')   # half a line: not read yet
        assert [h["seven_day"] for h in limit_history()][-1] == 61 and len(limit_history()) == 4
        (DATA / "limits.jsonl").write_text(json.dumps({"t": nw, "seven_day": 1}) + "\n")
        assert [h["seven_day"] for h in limit_history()] == [1]   # rewritten: start over
        (DATA / "limits.jsonl").unlink()
        # Several accounts: pick() per strategy, restart via data/accounts.json, one log row per account, history per account
        U = lambda h, w: {"five_hour": {"utilization": h}, "seven_day": {"utilization": w}}
        ac = [{"id": "w1", "label": "work", "priority": 0, "requests": 5, "avail": True, "usage": U(10, 20)},
              {"id": "p2", "label": "personal", "priority": 1, "requests": 2, "avail": True, "usage": U(0, 0)}]
        fl = lambda r: [(o["id"], o["use"], o["next"], o["primary"]) for o in r[0]]
        first = [("w1", False, True, True), ("p2", False, False, False)]   # first reading ever: nothing in use
        assert fl(pick(ac, {}, "fill_first", nw)) == first and fl(pick(ac, {}, "round_robin", nw)) == first
        assert fl(pick(ac, {}, "least_used", nw)) == [("w1", False, False, False), ("p2", False, True, True)]
        assert fl(pick(ac, {}, "random", nw)) == [("w1", False, False, True), ("p2", False, False, False)]
        r = pick([dict(ac[0], avail=False), ac[1]], {}, "fill_first", nw)
        assert fl(r) == [("w1", False, False, False), ("p2", False, True, True)]   # exhausted: the next one
        r = pick(ac, pick(ac, {}, "fill_first", nw)[1], "fill_first", nw + 600)
        assert not any(o["use"] for o in r[0])   # nothing rose
        r = pick([ac[0], dict(ac[1], requests=3)], r[1], "fill_first", nw + 600)
        assert fl(r) == [("w1", False, True, False), ("p2", True, False, True)]   # in use beats next
        assert pick(ac, r[1], "fill_first", nw + 2399)[0][1]["primary"] and pick(ac, r[1], "fill_first", nw + 2401)[0][0]["primary"]
        both = {"w1": {"5h": 10, "7d": 20, "n": 5, "used_at": nw - 100}, "p2": {"5h": 0, "7d": 0, "n": 2, "used_at": nw - 50}}
        assert pick(ac, both, "fill_first", nw)[0][0]["primary"]   # both in use: the one that is also next
        assert pick(ac, both, "random", nw)[0][1]["primary"]       # else the most recently risen
        assert fl(pick(ac, both, "fill_first", nw)) == [("w1", True, True, True), ("p2", False, False, False)]   # older riser: no badge
        assert fl(pick(ac, {}, "fill_first", nw, "p2")) == [("w1", False, False, False), ("p2", False, True, True)]   # preferred
        assert fl(pick([ac[0], dict(ac[1], avail=False)], {}, "fill_first", nw, "p2"))[0] == ("w1", False, True, True)   # benched: ignored
        sw = {"w1": {"5h": 10, "7d": 20, "n": 5, "used_at": nw - 600}, "p2": {"5h": 0, "7d": 0, "n": 1, "used_at": nw - 300}}
        assert fl(pick(ac, sw, "fill_first", nw, "p2")) == [("w1", False, False, False), ("p2", True, True, True)]   # switched over
        assert [o["use"] for o in pick([dict(ac[0], requests=6), dict(ac[1], requests=3)], sw, "fill_first", nw)[0]] == [True, True]   # both rising
        r = pick([{"id": "", "avail": True, "usage": U(5, 5)}], {}, "", nw)
        assert fl(r) == [("", False, True, True)] and r[1][""]["7d"] == 5   # single account
        st0, run0, feed = dict(STATE), subprocess.run, {"strategy": "fill_first", "accounts": ac}
        subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(feed), "")
        try:
            refresh_limits(0)
            assert STATE["limits"] == U(10, 20) and [a["primary"] for a in STATE["accts"]] == [True, False]
            assert json.loads((DATA / "accounts.json").read_text())["p2"]["7d"] == 0
            STATE.update(limits=None, accts=[])   # restart: only accounts.json remembers
            ac[1]["usage"] = U(3, 1)
            refresh_limits(0)
            assert STATE["limits"] == U(3, 1) and [a["use"] for a in STATE["accts"]] == [False, True], STATE["accts"]
            _req.lang = "en"   # w1 (work) and p2 (personal, primary): the texts name the primary account, one account stays as before
            wk_, fh_ = (62.0, 0.5, 0), (34.0, 0.5, nw + 60)
            menu_, pin_ = tg_texts(wk_, fh_, 70, 1, nw, acct_name())
            m1_, p1_ = tg_texts(wk_, fh_, 70, 1, nw)
            assert acct_name() == "personal" and menu_ == "W 62% · 5h 34% · personal" and pin_.splitlines()[0] == "Account: personal", (menu_, pin_)
            assert tg_texts(wk_, fh_, 70, 1, nw, "claude-second")[0] == "W 62% · 5h 34% · claude-second"   # numbers first: a narrow button cuts the name, not the use
            assert tg_texts(wk_, fh_, 70, 1, nw, "a-very-long-account-x")[0].endswith("· a-very-long-acc…")
            assert m1_ == "W 62% · 5h 34%" and p1_ == pin_.split("\n", 1)[1] and "Account" not in p1_
            assert json.loads(api_summary())["accounts"] == [
                {"label": "work", "use": False, "next": True, "primary": False, "five_hour": 10, "seven_day": 20},
                {"label": "personal", "use": True, "next": False, "primary": True, "five_hour": 3, "seven_day": 1}], api_summary()
            with open(DATA / "limits.jsonl", "a") as fh:
                fh.write(json.dumps({"t": nw, "seven_day": 2}) + "\n")   # rows without "a" belong to the primary account
            assert [h["seven_day"] for h in limit_history()] == [0, 1, 2] and [h["seven_day"] for h in limit_history(acct="w1")] == [20, 20]
            ok0, ac[1] = STATE["ok"], {"id": "p2", "label": "personal", "priority": 1, "requests": 9, "avail": True, "error": "HTTP 500"}
            refresh_limits(0)
            assert STATE["ok"] == ok0 and STATE["limits"] == U(3, 1) and STATE["accts"][1]["error"] == "HTTP 500"
            feed = {"strategy": "", "accounts": [{"id": "", "avail": True, "usage": U(7, 8)}]}
            refresh_limits(0)
            assert STATE["limits"] == U(7, 8) and "\"a\"" not in (DATA / "limits.jsonl").read_text().splitlines()[-1]
            assert [h["seven_day"] for h in limit_history()] == [2, 8]   # single account: untagged rows only
            assert acct_name() == "" and "accounts" not in json.loads(api_summary())
        finally:
            subprocess.run = run0
            STATE.clear(); STATE.update(st0)
        (DATA / "limits.jsonl").unlink()
        assert c.count('class="fcl"') == 1 and c.count('class="cap"') == 1 and "Reset " in c, c   # trend: this week, 100 % line, forecast
        assert "runs ~20% over, about $" in c and "Extra credits this month" in c, c
        RATE.clear()
        assert "Too fast:" in c and tr("lim.budget", pct=pc(40 / 3.5)) in c, c
        STATE["limits"]["seven_day"]["utilization"] = 40
        assert "On track:</strong> about 80%" in limits_card(None)
        STATE["limits"]["seven_day"]["utilization"] = 45
        assert "Tight:</strong> about 90%" in limits_card(None)   # 85-100 % is no longer "On track"
        # Several accounts: the primary one on top (name, badge, extras), one block per other account; one account = as before
        one, acc0 = limits_card(None), STATE["accts"]
        A = lambda i, lab, lim, **k: {"id": i, "label": lab, "use": False, "next": False, "primary": False, "avail": True,
                                      "status": None, "limits": lim, "error": None, **k}
        STATE["accts"] = [A("", "", STATE["limits"], next=True, primary=True)]
        assert limits_card(None) == one and 'class="acct"' not in one and "badge" not in one
        p2 = {"five_hour": {"utilization": 100, "resets_at": iso(nw + 1800)}, "seven_day": {"utilization": 20, "resets_at": iso(nw + 2 * 86400)}}
        STATE["accts"] = [A("p2", "personal", p2), A("w1", "work", STATE["limits"], use=True, primary=True),
                          A("x3", "", None, avail=False, next=True, error="HTTP 500")]
        m = limits_card(None)
        assert m.index(">work<") < m.index(">personal<") < m.index(">x3<"), m   # primary first, then pool order
        assert '<span class="badge">In use</span>' in m and f'<span class="badge hot">Limit until {hm(nw + 1800)}</span>' in m, m
        assert '<span class="badge hot">Not available</span>' in m and "No reading right now (HTTP 500)." in m, m
        assert m.count("Extra credits this month") == 1 and m.count(tr("lim.week")) == 2 and m.count("Daily budget") == 2, m
        assert tr("lim.budget", pct=pc(80 / 2)) in m and "Full. Reset in" in m and m.count('class="acct"') == 3, m
        STATE["accts"][2].update(avail=True, limits=p2, error=None)
        assert '<span class="badge next">' not in limits_card(None)   # a full window beats "next"
        STATE["accts"][2]["limits"] = STATE["limits"]
        _req.lang = "de"
        assert '<span class="badge next">Als Nächstes</span>' in limits_card(None) and "In Benutzung" in limits_card(None)
        _req.lang, STATE["accts"] = "en", acc0
        # Mascots: every drawing is valid SVG with its colors in style (var() in fill="" is not safe everywhere)
        from xml.etree import ElementTree as ET
        for k in PETS:
            for st in MOODS:
                g = pet(k, st, 62)
                ET.fromstring(g)
                assert "{" not in g and "var(--" in g and not re.search(r' (fill|stroke)="var', g), (k, st, g)
        assert pet("bot", "calm", 0).count('opacity="1"') == 4 and pet("bot", "calm", 80).count('opacity="1"') == 1
        assert 'y="44"' in pet("coffee", "calm", 0) and 'y="98"' in pet("coffee", "tight", 100)   # full / empty mug
        W = lambda f, w: {"five_hour": {"utilization": f, "resets_at": iso(nw + 3600)}, "seven_day": {"utilization": w, "resets_at": iso(nw + 3.5 * 86400)}}
        assert pet_state(windows(W(0, 20), nw), True) == pet_state(windows(W(49, 20), nw), True) == pet_state(windows(None, nw), True) == "calm" and pet_state(windows(W(50, 20), nw), True) == "work"
        assert pet_state(windows(W(86, 20), nw), True) == "tight" and pet_state(windows(W(10, 45), nw), True) == "calm"   # follows the 5 h window, not the week forecast
        wonly = {"seven_day": W(0, 45)["seven_day"]}
        assert pet_state(windows(wonly, nw), True) == "tight" and pet_window(windows(wonly, nw))[:2] == ("seven_day", 45.0) and pet_window(windows(W(30, 70), nw))[:2] == ("five_hour", 30.0)
        assert pet_state(windows(W(100, 20), nw), True) == pet_state(windows(W(10, 100), nw), True) == pet_state(windows(W(10, 20), nw), False) == "limit"
        STATE["limits"] = W(10, 20)
        c = limits_card(None)
        assert c.count('<span class="pet" data-st="calm" role="img" aria-label="Mascot: calm') == 1 and 'class="pet-calm"' in c and 'class="pet-work"' not in c, c
        STATE["limits"] = W(60, 20)
        assert 'data-st="work"' in limits_card(None)
        STATE["limits"] = W(10, 20)
        STATE["accts"] = [A("w1", "work", STATE["limits"], use=True, primary=True, avail=False), A("p2", "personal", W(1, 1), next=True)]
        c = limits_card(None)   # two accounts: pooled view (mean of 10/20 and 1/1), the per-account view folded below
        assert c.count('class="pet"') == 1 and 'data-st="calm"' in c and 'id="accts"' in c and "2 accounts" in c and ">10<" in c, c
        assert [w[:2] for w in pooled(STATE["accts"], nw)] == [("five_hour", 5.5), ("seven_day", 10.5)]
        STATE["accts"][1]["avail"] = False
        assert 'data-st="limit"' in limits_card(None)   # all accounts benched
        STATE["accts"][1]["limits"] = None
        assert 'id="accts"' not in limits_card(None) and 'data-st="limit"' in limits_card(None)   # one without data: per account as before
        STATE["accts"], STATE["limits"] = acc0, W(90, 20)
        assert 'data-st="tight"' in limits_card(None) and limits_card(None).count("<svg class=\"pet-") == 1
        SET["pet"] = "terminal"
        assert "! 90%" in limits_card(None)   # the number is the 5 h window's, not the week's 20 %
        SET["pet"] = "off"
        assert 'class="pet' not in limits_card(None)
        SET["pet"] = DEFAULTS["pet"]
        STATE["limits"] = old
        cl = Path(tmp) / "claude" / "projects" / "-x"
        cl.mkdir(parents=True)
        msg = {"type": "assistant", "requestId": "r", "timestamp": "2026-10-08T10:00:00Z", "message": {"id": "m", "model": "claude-opus-5-5",
               "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 300, "output_tokens": 50,
                         "cache_creation": {"ephemeral_1h_input_tokens": 100}}}}
        (cl / "s.jsonl").write_text(json.dumps(msg) + "\n" + json.dumps(msg) + "\n" + '{"type": "user"}\n')
        CLAUDE = cl.parent
        pr = price("claude-opus-5-5")
        assert cc_calls(0) == [(datetime(2026, 10, 8, 10, tzinfo=timezone.utc).timestamp(), 10 * pr[0] + 1000 * pr[3] + 200 * pr[1] + 100 * pr[2] + 50 * pr[4])]
        (HERMES / "config.yaml").write_text("model:\n  default: m\nagent:\n  reasoning_effort: high\nplugins:\n  enabled:\n    - a\n")
        watch_config(); watch_config()
        assert events(0) == [] and len(read_jsonl(DATA / "config.jsonl")) == 1   # baseline only
        (HERMES / "config.yaml").write_text("model:\n  default: m\nagent:\n  reasoning_effort: xhigh\nplugins:\n  enabled:\n    - b\n")
        watch_config()
        ev = events(0)
        assert len(ev) == 1 and ev[0][1] == "Config: reasoning_effort high → xhigh, plugins +b −a", ev
        hs = {h: 10 for h in range(0, 40 * 3600, 3600)}
        d30 = {"hours": {h: {"x": 1.0 if h < 20 * 3600 else 2.0} for h in hs}, "hsteps": hs}
        assert per_step(d30, 0, 20 * 3600) == 0.1 and per_step(d30, 20 * 3600, 40 * 3600) == 0.2 and per_step(d30, 0, 3600) is None
        save_settings({**SET, "currency": "EUR"})
        assert load_settings()["currency"] == "EUR" and (DATA / "settings.json").stat().st_mode & 0o777 == 0o600
        SET["currency"] = "USD"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def post(origin, body=b"section=look&theme=dark&action=save"):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/settings", body, {"Origin": origin})
            try:
                return urllib.request.urlopen(req, timeout=30).status
            except urllib.error.HTTPError as err:
                return err.code
        assert post("http://evil.example") == 403 and SET["theme"] == "system"
        here = f"http://127.0.0.1:{srv.server_port}"
        assert post(here) == 200 and load_settings()["theme"] == "dark"   # 303 to the page
        # Each sub-page saves only its own section: a checkbox missing from its form must not switch off another page's
        flat = [k for ks in SECTIONS.values() for k in ks]
        assert sorted(flat) == sorted(DEFAULTS), set(flat) ^ set(DEFAULTS)   # every key in exactly one section
        every = {"theme": "light", "five_min": "99", "quiet_from": "01:00", "push": "off", "tg_thread": "5", "pet": "duck",
                 "ntfy_server": "https://x.example", "url": "https://y.example", "extra_steps": "5", "ntfy_topic": "t2"}   # a bit of every page
        keep = dict(SET)
        for sec, keys in SECTIONS.items():
            SET.update({k: True for k, d in DEFAULTS.items() if isinstance(d, bool)}, push="own", guard_jobs=["j"])
            snap = json.loads(json.dumps(SET))
            assert post(here, urlencode({**every, "section": sec, "action": "save"}).encode()) == 200, sec
            moved = {k for k in DEFAULTS if SET[k] != snap[k]}
            assert moved and moved <= set(keys), (sec, moved)
            assert urllib.request.urlopen(f"{here}/settings/{sec}?lang=de", timeout=30).status == 200
        assert post(here, b"theme=dark&action=save") == 200 and SET["theme"] == "light"   # no section: nothing saved
        try:
            urllib.request.urlopen(f"{here}/settings/nope", timeout=30)
            raise AssertionError("/settings/nope")
        except urllib.error.HTTPError as err:
            assert err.code == 404
        SET.clear(); SET.update(keep); save_settings(dict(SET))
        r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/settings", b"section=alerts&quiet_from=25:00&action=save",
                                                          {"Origin": f"http://127.0.0.1:{srv.server_port}"}), timeout=30)
        assert "/settings/alerts?" in r.url and r.url.endswith("bad=quiet_from") and 'role="alert"' in r.read().decode(), r.url
        def auto(body, origin=here):   # what static/app.js sends when a field changes
            req = urllib.request.Request(f"{here}/settings", body, {"Origin": origin, "X-Usagecast-Auto": "1"})
            try:
                r = urllib.request.urlopen(req, timeout=30)
                return r.status, r.headers["Content-Type"], json.loads(r.read())
            except urllib.error.HTTPError as err:
                return err.code, "", None
        SET.update(alert_five=True, alert_week=True, theme="light", quiet_from="22:00", alert_digest=True)
        st, ct, j = auto(b"section=alerts&alert_five=on&quiet_from=23:15&theme=dark")   # alert_week and alert_digest missing = off here
        assert (st, ct, j["ok"], j["bad"]) == (200, "application/json", True, []) and j["num"] == {k: SET[k] for k in RANGES if k in SECTIONS["alerts"]}, (st, ct, j)
        assert SET["quiet_from"] == "23:15" and SET["alert_five"] and not SET["alert_week"] and not SET["alert_digest"] and SET["theme"] == "light"   # only the section's keys
        assert auto(b"section=alerts&quiet_from=25:00&five_min=9999")[2] == {"ok": True, "bad": ["quiet_from"], "num": {"five_min": 240, "chat_pct": SET["chat_pct"], "spike_pct": SET["spike_pct"]}} and SET["quiet_from"] == "23:15" and SET["five_min"] == 240
        assert auto(b"section=alerts&quiet_from=")[2]["bad"] == ["quiet_from"] and SET["quiet_from"] == "23:15"   # a cleared time is refused too
        assert auto(b"section=notify&ntfy_topic=a+b")[2]["bad"] == ["ntfy_topic"]
        before = dict(SET)
        assert auto(b"theme=dark&quiet_from=01:00")[:2] == (200, "application/json") and SET == before   # no section: ok false, nothing saved
        assert auto(b"section=nope&theme=dark")[2]["ok"] is False and SET == before
        assert auto(b"section=look&theme=dark", "http://evil.example")[0] == 403 and SET == before
        pg = page_settings({}, "alerts")
        assert 'data-saved="Saved"' in pg and "data-bad=" in pg and "data-fail=" in pg and 'class="sr"' in pg
        SET.clear(); SET.update(keep); save_settings(dict(SET))
        def via_cf(tok):   # a request as cloudflared forwards it
            h = {"Cf-Connecting-Ip": "203.0.113.9", **({"Authorization": "Bearer " + tok} if tok else {})}
            try:
                return urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/health", headers=h), timeout=30).status
            except urllib.error.HTTPError as err:
                return err.code
        global PROXY_TOKEN
        pt0, PROXY_TOKEN = PROXY_TOKEN, ""
        assert via_cf("") == 403 and via_cf("x") == 403   # no token set: nothing gets in through the tunnel
        PROXY_TOKEN = "pt"
        assert via_cf("") == 403 and via_cf("ptx") == 403 and via_cf("pt") == 200
        PROXY_TOKEN = pt0
        def ingest(body, tok):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/ingest", body, {"Authorization": "Bearer " + tok} if tok else {})
            try:
                return urllib.request.urlopen(req, timeout=30).status
            except urllib.error.HTTPError as err:
                return err.code
        good = json.dumps({"host": "mac-1", "since": 7200, "hours": [[7205, "claude-opus-5-5", 10, 300, 1000, 50, 100]]}).encode()
        assert ingest(good, "tk") == 401 and parse_ingest(good)[0] == "mac-1"   # no token created yet
        SET["ingest_token"] = "tk"
        assert ingest(good, "") == 401 and ingest(good, "tkx") == 401 and ingest(b"{", "tk") == 400
        assert ingest(json.dumps({"host": "../x", "hours": []}).encode(), "tk") == 400
        assert ingest(json.dumps({"host": "a", "hours": [[1, "m", -1, 0, 0, 0]]}).encode(), "tk") == 400
        assert ingest(good, "tk") == 200 and ingest(good, "tk") == 200 and list(ingest_hosts()) == ["mac-1"]
        pr = price("claude-opus-5-5")
        want = 10 * pr[0] + 200 * pr[1] + 100 * pr[2] + 1000 * pr[3] + 50 * pr[4]
        assert [round(c, 6) for t, c in cc_calls(0) if t == 7200] == [round(want, 6)], cc_calls(0)
        assert urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/cc-report.py", timeout=30).read().startswith(b"#!/usr/bin/env python3")
        INGEST_SHOWN["at"] = time.time()
        assert "--token tk --install" in page_settings({"done": ["ingest"]}, "advanced") and "--token TOKEN" in page_settings({}, "advanced")
        # Mascot picker: saves a valid choice, keeps the old one on junk, six labelled tiles in every language
        SET["pet"] = "rack"
        assert post(here, b"section=look&pet=duck&action=save") == 200 and SET["pet"] == "duck"
        assert post(here, b"section=look&pet=xyz&action=save") == 200 and SET["pet"] == "duck"
        for lg in LOC:
            _req.lang = lg
            pk = page_settings({}, "look")
            assert pk.count('name="pet"') == 6 and 'name="pet" value="duck" checked' in pk, lg
            names = [tr("set.pet." + k) for k in ("off", *PETS)]
            assert len(set(names)) == 6 and all(f"<b>{n}</b>" in pk for n in names), (lg, names)
        _req.lang, SET["pet"] = "en", DEFAULTS["pet"]
        srv.shutdown()
        LAST_PUSH.update(at=time.time(), ok=False, reply="<x>")
        real_k = real + sum(row_cost("claude-opus-5-5", 10, 4000, 1000, 200, 300))   # plus the subagent of the test chat
        assert abs(sum(day_costs().values()) - real_k) < 1e-9, (day_costs(), real_k)   # the calendar loses nothing
        cal = calendar({(datetime.now().date() - timedelta(days=1)).isoformat(): 2.0})
        assert cal.count('class="h4" title') == 1 and 2 <= cal.count("<i ") <= 14, cal[:200]   # a young install starts at its first week
        assert calendar({"2000-01-03": 1.0, datetime.now().date().isoformat(): 1.0}).count("<i ") >= 365 + 5   # at most a year
        (HERMES / "cron").mkdir()
        (HERMES / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "j1", "name": "Daily report", "state": "paused", "repeat": {"times": None}},
            {"id": "j2", "name": "Once", "state": "scheduled", "repeat": {"times": 1}}]}))
        (DATA / "guard.json").write_text(json.dumps({"paused": ["j1"]}))
        assert [j["id"] for j in cron_list()] == ["j1"] and "Daily report" in guard_note() and clean({"gj_j1": "on", "gj_j2": "on"}, SET)["guard_jobs"] == ["j1"]
        tp = [t for _, t, _ in tips(dict(total=1.0, ttl_save=0, comp={}, uses={}, ctx={}, where={"cron:Daily report": 0.5, "cron:Once": 0.5}), {})]
        assert tp == [tr("tip.cron", name="Daily report")], tp   # no "run it less often" for one-shot jobs
        x1 = dict(total=1.0, ttl_save=0, comp={}, uses={}, ctx={}, where={"cron:Daily report": 0.5})
        at = all_tips(dict(x1, parts=[("", None, x1), ("a", None, dict(x1))]))
        assert len(at) == 1 and at[0][0] == 0.5 and at[0][2].count("<strong>") == 2, at   # one card for both agents
        xt = dict(total=1.0, ttl_save=0, comp={"schema": 0.5}, uses={"tool:a": 1}, ctx={}, where={})
        assert [t for _, t, _ in tips(xt, {"tools": {"a": 1, "b": 1}})] == [tr("tip.unused")]
        assert not tips(xt, {"tools": {"a": 1, "b": 1}}, also={"b"}), "b ran this month: not unused"
        for sec in ("", *SECTIONS):
            ids = re.findall(r'\bid="([^"]+)"', page_settings({}, sec))
            assert len(ids) == len(set(ids)), (sec, sorted(i for i in ids if ids.count(i) > 1))
        # Links of the one-page settings: the list page sends /settings#push and the like on to their sub-page
        assert set(OLD_ANCHORS.values()) <= set(SECTIONS) and all(f"{a}:{b}" in page_settings({}) for a, b in OLD_ANCHORS.items())
        assert "/settings#" not in page_overview("7") and 'href="/settings/notify"' in page_overview("7")
        assert long_line([50_000] * 9) == LONG_CTX and long_line([50_000] * 20) == LONG_CTX   # too few tasks / small tasks
        assert long_line(list(range(10_000, 160_001, 10_000))) == 130_000 and long_line([900_000] * 12) == 2 * LONG_CTX
        _req.lang = "en"
        two = dict(total=10.0, comp={"break": 1.0}, ctx={})
        a1, a2 = dict(two, ctx={120_000: 2.0}), dict(two, ctx={120_000: 2.0, 160_000: 2.0})
        LONG.update(a=150_000)
        lim, STATE["limits"] = STATE["limits"], None
        both = dict(total=15.0, comp={"break": 2.0}, ctx={}, parts=[("", None, a1), ("a", None, a2)])
        tb = shared_tips(both)
        assert [t for _, t, _ in tb].count(tr("tip.break")) == 1 and tb[1][0] == 2.0 and "100,000–150,000" in tb[1][2], tb   # one card each
        STATE["limits"] = {"seven_day": {"utilization": 85, "resets_at": datetime.fromtimestamp(time.time() + 86400).isoformat()}}
        assert week_tight() and shared_tips(both)[1][0] == 3.0   # a tight week keeps everyone at 100k
        STATE["limits"] = lim
        LONG.clear()
        refresh_bots()   # no tokens in this home: nothing fetched, nothing named
        assert read_json(DATA / "bots.json")["names"] == {} and bot_label(":telegram") == "Hermes · Telegram"
        (DATA / "bots.json").write_text(json.dumps({"at": time.time(), "names": {"": {"telegram": "Robo"}}}))
        assert (bot_label(":telegram"), bot_label(":cron"), bot_label("w:discord")) == ("Robo · Telegram", "Robo · Cron jobs", "w · Discord")
        leftover = re.compile(r"\b(?:chat|th|set|cal|ovl|ev|brk|ov|lim|det|ses|hist|heat|nav|kpi|seg|src|proj|cron|period|since|fmt|num|tip|comp|label|alert|until|sub)\.[a-z_]")
        for code in LOC:
            _req.lang, _req.url = code, "/?p=7"
            SET["ntfy_topic"] = "usagecast-test" if code != "en" else ""   # English: not set up yet, the others: subscribe steps
            cp = f"{datetime.now().date() - timedelta(days=1)}..{datetime.now().date()}"
            for page in [*(f(p) for f in (page_overview, page_history, page_details, page_projects) for p in [*PERIODS, cp]),
                         page_sessions("7", {}), page_sessions("7", {"view": ["cron"]}), page_sessions("7", {"proj": ["shop"]}),
                         page_session("s", "7"), page_settings({}), *(page_settings({"done": [d]}, x) for x in SECTIONS for d in ("save", "test")),
                         page_settings({"bad": ["quiet_from,url"]}, "alerts")]:
                text = re.sub(r"<[^>]+>", " ", page)
                assert not leftover.search(text) and "{" not in text, (code, leftover.search(text), text[:300])
                assert page.count('<div class="app">') == 1, "static/app.js swaps this element on page switches"
        # Subagents: the chat lists them with their real cost, the subagent links back, the list shows both ways
        _req.lang = "en"
        kc = money(sum(row_cost("claude-opus-5-5", 10, 4000, 1000, 200, cache_ttl())))
        ps, pk, pl = page_session("s", "7"), page_session("k", "7"), page_sessions("7", {})
        assert "Subagent: check" in ps and 'href="/s/k?p=7"' in ps and kc in ps and "with subagents" in ps and "Subagents (delegate_<wbr>task)" in ps, ps
        assert '<summary>Full task</summary><p class="why">check &lt;all&gt; of it</p>' in pk and "Full task" not in ps, pk
        assert 'started by <a href="/s/s?p=7">Test</a>' in pk and "with subagents" not in pk, pk
        assert f"subagents: 1 · {kc}" in pl and 'started by <a href="/s/s?p=7">' in pl, pl
        # Bots & chats: the overview folds the chats out per bot, the list filters by bot and chat, old src= links still work
        lim0, STATE["limits"] = STATE["limits"], {"seven_day": {"utilization": 10, "resets_at": iso(time.time() + 3.5 * 86400)}}
        for k in PETS:   # calm renders the calm and the work drawing: their clipPath ids must not clash on the page
            SET["pet"] = k
            ids = re.findall(r'\bid="([^"]+)"', po := page_overview("7"))
            assert 'data-st="calm"' in po and len(ids) == len(set(ids)), (k, sorted(i for i in ids if ids.count(i) > 1))
        STATE["limits"], SET["pet"] = lim0, DEFAULTS["pet"]
        po, shop = page_overview("7"), link("/sessions", p="7", bot=":telegram", chat="telegram:42:7")
        assert 'class="chats"' in po and f'<a href="{shop}"><span>Shop</span>' in po and ">Robo · Subagents<" in po, po   # one DM: no context
        assert ">Robo · Cron jobs<" in po and link("/sessions", p="7", bot=":cron", chat="cron:Daily report") in po
        pf = page_sessions("7", {"bot": [":telegram"], "chat": ["telegram:42:7"]})
        assert "Only chat <b>Shop</b>" in pf and ">Test</a>" in pf and "Robo · Telegram · Shop" in pf and ">Subagent: check…</a>" not in pf, pf
        pc_ = page_sessions("7", {"chat": ["telegram:42:7"]})
        assert ">Test</a>" in pc_ and ">Subagent: check…</a>" in pc_ and "Daily report" not in pc_   # the subagents of that chat too
        ps_ = page_sessions("7", {"src": ["cron"]})
        assert "Daily report" in ps_ and ">Test</a>" not in ps_ and "Daily report" in page_sessions("7", {"view": ["cron"], "bot": [":cron"]})
        assert "Robo · Telegram" in ps and ">Shop</a>" in ps
    assert "&lt;x&gt;" in page_settings({"done": ["test"]}, "notify") and 'data-theme="dark"' in page_settings({})
    # Extra credits: a drop in the counter starts a new month, steps come once per month and only the highest
    assert extra_months([{"t": i, "extra": v} for i, v in enumerate([None, 1.0, 2.5, 2.5, 0.0, 0.5, 3.0])]) == [[1, 2.5], [4, 3.0]]
    Lx = {"extra_usage": {"is_enabled": True, "used_credits": 2100, "monthly_limit": 2500, "decimal_places": 2, "currency": "EUR"}}
    _req.lang = "en"
    sx, tn = {**DEFAULTS, "extra_steps": "10, 20", "alert_extra": False}, 2e9
    m, st = alerts(Lx, [], {"extra_used": 2100}, tn, sx)
    assert [t for t, _, _ in m] == ["extra@20"] and m[0][1] == "Extra credits passed 20 EUR" and alerts(Lx, [], {**st, "extra@20": tn}, tn, sx)[0] == [], m
    Lx["extra_usage"]["used_credits"] = 100
    m, st = alerts(Lx, [], {"extra_used": 2100, "extra@20": tn - 20 * 86400}, tn, sx)
    assert m == [] and "extra@20" not in st and st["extra_used"] == 100, st
    Lx["extra_usage"]["used_credits"] = 1200
    assert [t for t, _, _ in alerts(Lx, [], st, tn, sx)[0]] == ["extra@10"] and alerts(Lx, [], st, tn, {**sx, "extra_steps": ""})[0] == []
    assert clean({"extra_steps": "10, 20.5"}, DEFAULTS)["extra_steps"] == "10, 20.5" and clean({"extra_steps": "ten"}, DEFAULTS)["extra_steps"] == ""
    RATE["k"] = 0.5
    assert overrun_usd(120) == 40 and overrun_usd(90) is None and overrun_usd(None) is None
    RATE.clear()
    # Budget guard: marked active jobs pause when the week gets tight, only its own come back, unknown limits change nothing
    tg = 2e9
    Lg = lambda wk, five, frac: {"seven_day": {"utilization": wk, "resets_at": iso(tg + (1 - frac) * 7 * 86400)},
                                 "five_hour": {"utilization": five, "resets_at": iso(tg + 3600)}}
    gs, jobs = {**DEFAULTS, "guard": True, "guard_jobs": ["a", "b", "u"]}, {"a": "scheduled", "b": "scheduled", "u": "paused", "c": "scheduled"}
    assert guard_plan(Lg(50, 10, 0.5), tg, jobs, {}, gs) == ([], []) and guard_plan(Lg(60, 10, 0.5), tg, jobs, {}, gs) == (["a", "b"], [])
    assert guard_plan(Lg(12, 10, 0.1), tg, jobs, {}, gs) == ([], []) and guard_plan(Lg(40, 85, 0.5), tg, jobs, {}, gs)[0] == ["a", "b"]
    assert guard_plan(Lg(90, 0, 0.95), tg, jobs, {}, gs)[0] == ["a", "b"] and guard_plan(Lg(60, 10, 0.5), tg, jobs, {}, {**gs, "guard": False}) == ([], [])
    held = {"a": "paused", "b": "paused", "u": "paused", "c": "scheduled"}
    assert guard_plan(Lg(50, 10, 0.5), tg, held, {"paused": ["a", "b"]}, gs) == ([], ["a", "b"])   # room again, u stays the user's
    assert guard_plan(Lg(60, 10, 0.5), tg, held, {"paused": ["a", "b"]}, {**gs, "guard_jobs": ["a"]}) == ([], ["b"])
    assert guard_plan(Lg(60, 10, 0.5), tg, held, {"paused": ["a"]}, {**gs, "guard": False}) == ([], ["a"])
    assert guard_plan(None, tg, held, {"paused": ["a"]}, gs) == ([], []) and guard_plan(Lg(50, 10, 0.5), tg, jobs, {"paused": ["a"]}, gs) == ([], [])
    # Several accounts: guard pauses only when every available one is tight; alerts per account, tags prefixed, label in the title
    A = lambda i, lab, L, av=True: {"id": i, "label": lab, "limits": L, "avail": av, "primary": i == "w"}
    pool = [A("w", "work", Lg(60, 10, 0.5)), A("p", "personal", Lg(60, 10, 0.5))]
    assert guard_pool(pool, pool[0]["limits"], tg, jobs, {}, gs) == (["a", "b"], [])
    pool[1]["limits"] = Lg(50, 10, 0.5)
    assert guard_pool(pool, pool[0]["limits"], tg, jobs, {}, gs) == ([], []), "one account free: no pause"
    pool[1].update(limits=Lg(50, 10, 0.5), avail=False)
    pool[0]["limits"] = Lg(60, 10, 0.5)
    assert guard_pool(pool, pool[0]["limits"], tg, jobs, {}, gs) == (["a", "b"], []), "unavailable account does not count"
    assert guard_pool(pool[:1], pool[0]["limits"], tg, jobs, {}, gs) == guard_plan(pool[0]["limits"], tg, jobs, {}, gs)
    pool[1]["avail"] = True   # primary still tight, the other one free again: the guard's paused jobs come back
    assert guard_pool(pool, pool[0]["limits"], tg, dict(jobs, a="paused"), {"paused": ["a"]}, gs) == ([], ["a"])
    Lh = Lg(95, 95, 0.5)
    pool = [A("w", "work", Lh), A("p", "personal", Lh)]
    nohist, sa = lambda acct=None: [], {**SET, "alert_steps": True}
    m, st = alerts_pool(pool, {}, tg, nohist, sa)
    assert {t[:2] for t, _, _ in m} == {"w:", "p:"} and all(ti.startswith(("work: ", "personal: ")) for _, ti, _ in m), m
    assert all(k[:2] in ("w:", "p:") for k in st), st
    assert alerts_pool(pool, {**st, **{t: tg for t, _, _ in m}}, tg + 60, nohist, sa)[0] == []   # the sent store keeps accounts apart
    m2 = alerts_pool(pool, {t: tg for t, _, _ in m if t[0] == "w"}, tg + 60, nohist, sa)[0]
    assert {t[:2] for t, _, _ in m2} == {"p:"}, m2
    pool[0]["limits"] = None
    assert {t[:2] for t, _, _ in alerts_pool(pool, {}, tg, nohist, sa)[0]} == {"p:"}
    lim0, STATE["limits"] = STATE["limits"], Lh   # one account: tags and texts as before
    assert alerts_pool(pool[:1], {}, tg, nohist, sa) == alerts(Lh, [], {}, tg, sa) and alerts(Lh, [], {}, tg, sa)[0]
    STATE["limits"] = lim0
    # Weekly rhythm: a week that spends half its cost in the first day; the weighted forecast sees through it
    ws0, pat = 50 * 7 * 86400, [10.0] * 24 + [10.0 * 24 / 144] * 144
    hc = {ws0 - 7 * 86400 * i + 3600 * h: c * (1 + 0.1 * i) for i in range(1, 5) for h, c in enumerate(pat)}
    prof, bt = rhythm(hc, ws0, ws0 - 30 * 86400)
    assert prof and abs(sum(prof) - 1) < 1e-9 and abs(sum(prof[:24]) - 0.5) < 1e-9 and bt["wtd"] < 1 < bt["lin"], bt
    assert rhythm(hc, ws0, ws0 - 15 * 86400) == (None, None)   # two weeks are not enough
    FC.update(prof=prof, method="weighted")
    assert abs(week_forecast(50, 24 / 168) - 100) < 1e-6 and abs(week_forecast(60, 0.5) - 60 / (0.5 + 0.5 * 60 / 144)) < 1e-6
    FC["method"] = "linear"
    assert abs(week_forecast(50, 0.5) - 100) < 1e-9
    FC.clear()
    # Turns and the chat/spike watch
    assert split_turns([(5, 1.0), (12, 2.0), (13, 1.0), (30, 4.0)], [0, 10, 20]) == [(0, 5, 1, 1.0), (10, 13, 2, 3.0), (20, 30, 1, 4.0)]
    _req.lang, T0, ws = "en", 1e6, {**DEFAULTS, "chat_pct": 3.0, "spike_pct": 3.0}
    ss = lambda src, cost, costs, last, org=None, st=0: dict(src=src, title="T", cost=cost, last=last, origin=org or src, started=st,
                                                         turns=[(T0 + i, T0 + i, 3, c) for i, c in enumerate(costs)])
    d30w = {"since": 0, "sess": {"a": ss("telegram", 16, [1, 1, 1, 4, 4, 4, 1], T0 + 100), "b": ss("cli", 15, [1, 1, 1, 1, 1, 10], T0 + 100),
                                 "old": ss("telegram", 99, [9, 9, 9, 9], T0 - 1300),
                                 **{f"c{i}": ss("cron", 1.0, [], 0, "cron:job", i) for i in range(3)}, "c9": ss("cron", 10.0, [], T0 + 50, "cron:job", 9)}}
    wm = watch(d30w, T0 + 200, {}, 1.0, ws)
    assert [x[0] for x in wm] == ["chat:a", "spike:b:1000005", "spike:c9"] and "4.0%" in wm[0][2] and "~1.0%" in wm[0][2] and wm[0][3] == "/s/a", wm
    assert "~1.0%" in wm[2][2] and "6 steps" not in wm[1][2] and "3 steps" in wm[1][2], wm
    assert watch(d30w, T0 + 200, {x[0]: 1 for x in wm}, 1.0, ws) == [] and watch(d30w, T0 + 200, {}, None, ws) == []
    assert watch(d30w, T0 + 200, {}, 1.0, {**ws, "alert_chat": False, "alert_spike": False}) == []
    assert clean({"chat_pct": "1,5", "spike_pct": "nan"}, DEFAULTS)["chat_pct"] == 1.5 and clean({"spike_pct": "nan"}, DEFAULTS)["spike_pct"] == 3.0
    db, _ = test_db()
    tu = analyze(db, -1, snap, 300, logs={})["sess"]["s"]["turns"]
    assert [(a, b, n) for a, b, n, _ in tu] == [(0.0, 2.0, 2), (1000.0, 1000.0, 1)], tu
    assert VERSION == re.search(r"^## (\S+)", (ROOT / "CHANGELOG.md").read_text(), re.M)[1], "VERSION and CHANGELOG.md disagree"
    dm_ = demo_db(time.time())
    assert dm_.execute("select count(*) from sessions where source = 'cron'").fetchone()[0] > 10   # --demo data builds
    assert {p for p, _ in chat_index(dm_).values()} == {"telegram", "discord", "cli", "cron"}   # with topics and two bots
    print("selftest ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        selftest()
    elif "--demo" in sys.argv:
        demo()
    elif "--ntfy-test" in sys.argv:
        print("sent" if notify(tr("alert.test"), tr("alert.test.text")) else "not sent: ntfy is not configured or not reachable")
    elif "--snapshot" in sys.argv:
        snapshot()
        sys.stdout.flush()
        os._exit(0)  # AIAgent can leave background threads (MCP) running
    else:
        threading.Thread(target=background, daemon=True).start()
        threading.Thread(target=warm, daemon=True).start()
        threading.Thread(target=net_loop, daemon=True).start()
        print(f"usagecast on http://127.0.0.1:{PORT}", flush=True)
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
