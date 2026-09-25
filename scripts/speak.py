#!/usr/bin/env python3
"""Read Claude's replies aloud. Python 3.9+, standard library only.

  speak.py                      speak the last reply (newest transcript)
  speak.py --session ID         …of one session (Claude Code's session id)
  speak.py --project PATH       …scoped to one workspace's sessions
  speak.py --transcript FILE    …from one specific session transcript
  speak.py --previous           …the reply before the latest prompt
  speak.py --text "…"           speak the given text
  speak.py --stdin              speak the text arriving on stdin
  speak.py --stop               stop playback
  speak.py --detach             re-launch detached, return immediately
  speak.py --turn ID            run as part of a slash-command turn in session ID
  speak.py --hook               Stop / UserPromptSubmit hook mode
  speak.py --status             show current configuration
  speak.py --auto on|off        toggle read-every-reply
  speak.py --set-voice V [--lang ru|en]   default voice, or one per language
  speak.py --print              show what would be spoken (and by which voice)

Design notes, learned the hard way before this was a plugin:

* CHUNKED, RAMPED PLAYBACK. Cloud TTS has per-request input limits, and
  synthesis time scales with input length — nothing plays until the first
  chunk exists, so the first chunk's size IS the time-to-first-sound
  (measured: 3.3s at 260 chars vs 10.7s at 1800, same voice). Chunks ramp
  ~260 → ~1000 → 1800 at sentence boundaries; chunk i+1 synthesises while
  chunk i plays, so later chunks cost no waiting and there are no gaps.
* THE ORCHESTRATOR IS THE PROCESS YOU STOP. This process stays alive for the
  whole playback, records its pid, and SIGTERM takes down the current player
  with it. Starting a new reading stops the old one first.
* SPEECHIFY'S WAV NEEDS ITS HEADER FIXED. It arrives with the RIFF/data sizes
  set to 0xFFFFFFFF (a streaming placeholder); some players then play the
  header bytes as audio — it sounds like a burst of static.
* HOOK MODE MUST BE HARMLESS. It takes the reply text Claude Code hands the
  Stop hook (never guesses), spawns itself detached, and exits 0 no matter
  what — a TTS failure must never break Claude's own flow.
* ONE VOICE PER LANGUAGE. A reply that mixes Russian and English is split
  into runs by alphabet, and each run is read by a voice of its language.
* FAILURES ARE WRITTEN DOWN. A detached reading has no terminal; what goes
  wrong lands in read-aloud.log, and --status / --detach report it.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import time
import threading
import urllib.request
import wave

# --------------------------------------------------------------------------- paths

HOME = pathlib.Path.home()
PROJECTS = HOME / ".claude" / "projects"
PIDFILE = pathlib.Path(tempfile.gettempdir()) / "claude-read-aloud.pid"
# Scratch audio is named per process. Two readings overlap for a moment by
# design — the one being stopped is still winding down while its replacement
# synthesises — and one shared pair of filenames means the dying player reads
# a file the new one is rewriting. That is heard as garble, not as a handover.
PARTS = [pathlib.Path(tempfile.gettempdir()) / f"claude-read-aloud-{os.getpid()}-{i}.wav"
         for i in (0, 1)]
# What the Windows system voice is asked to read, one file per reader.
JOB = pathlib.Path(tempfile.gettempdir()) / f"claude-read-aloud-{os.getpid()}-job.json"
SCRATCH_GLOBS = ("claude-read-aloud-*-[01].wav", "claude-read-aloud-*-job.json",
                 "claude-read-aloud-handoff-*", "claude-read-aloud-quiet-*")

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"


def config_path() -> pathlib.Path:
    if IS_WIN:
        base = pathlib.Path(os.environ.get("APPDATA", HOME))
    else:
        base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
    return base / "claude-read-aloud" / "config.json"


def data_dir() -> pathlib.Path:
    if IS_WIN:
        base = pathlib.Path(os.environ.get("LOCALAPPDATA", HOME))
    else:
        base = pathlib.Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share"))
    return base / "claude-read-aloud"


def log_path() -> pathlib.Path:
    return data_dir() / "read-aloud.log"


def log(msg: str) -> None:
    """Record a failure where --status and --detach can find it. Never raises."""
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 256_000:     # keep it small
            p.write_bytes(p.read_bytes()[-64_000:])
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def last_error() -> tuple[str, float] | None:
    """(last line of the log, its age in seconds), or None."""
    try:
        p = log_path()
        lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()]
        return (lines[-1], time.time() - p.stat().st_mtime) if lines else None
    except OSError:
        return None


DEFAULTS = {
    "provider": "system",   # system | speechify | elevenlabs | openai | command
    "voice": "",            # provider-specific voice name/id; "" = default
    "voices": {},           # system voice per language, e.g. {"ru": "Microsoft Irina
                            # Desktop"}; unset languages pick an installed voice
    "stop_on_prompt": True,  # sending a new prompt stops that session's reading
    "speed": 1.0,
    "auto_read": False,     # Stop hook reads every reply when true
    "max_chars": 12000,     # hard cap; ~14 minutes of speech
    "chunk_chars": 1800,    # per-request size, under every provider's limit
    "first_chars": 260,     # first chunk is tiny: its size is the latency
    "model": "",            # openai only; default tts-1
    "command": "",          # custom engine: template with {text} and/or {out}
    "api_keys": {},         # optional; env vars take precedence
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(config_path().read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    if not IS_WIN:
        # It may hold API keys (api_keys): owner-only, like ~/.netrc. On
        # Windows %APPDATA% is already private to the user.
        os.chmod(p, 0o600)


def api_key(cfg: dict, provider: str) -> str:
    key = os.environ.get(f"{provider.upper()}_API_KEY") \
        or cfg.get("api_keys", {}).get(provider, "")
    if not key:
        sys.exit(f"{provider} needs an API key: set {provider.upper()}_API_KEY "
                 f"or api_keys.{provider} in {config_path()}")
    return key

# ----------------------------------------------------------------- transcript → text


def valid_session(value) -> str:
    """A Claude Code session id, or "" for anything that does not look like one
    (it becomes part of file names and glob patterns)."""
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) \
        else ""


def project_dir_name(project: str) -> str:
    """The ~/.claude/projects folder Claude Code keeps a project's sessions in.

    Claude Code replaces every non-alphanumeric character of the path with '-':
    /home/me/app → -home-me-app, C:\\Users\\me\\app → C--Users-me-app. A Git
    Bash path (/c/Users/me/app) names the same Windows folder, so it is turned
    back into C:/Users/me/app first.
    """
    m = re.match(r"^/([A-Za-z])(/.*)?$", project) if IS_WIN else None
    if m:
        project = f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return re.sub(r"[^A-Za-z0-9]", "-", project.rstrip("/\\"))


def find_transcript(session: str = "", project: str | None = None) -> pathlib.Path | None:
    """The transcript to read: the session's own when its id is known, else the
    newest in the project, else the newest anywhere.

    Newest-anywhere is a last resort: it reads whichever session wrote last,
    the wrong one whenever two are running.
    """
    newest = lambda files: max(files, key=lambda p: p.stat().st_mtime) if files else None
    if session:
        found = newest(list(PROJECTS.glob(f"*/{session}.jsonl")))
        if found:
            return found
    if project:
        found = newest(list((PROJECTS / project_dir_name(project)).glob("*.jsonl")))
        if found:
            return found
    return newest(list(PROJECTS.glob("*/*.jsonl")))


def is_prompt(e: dict) -> bool:
    """A message the person sent (a slash command counts), as opposed to a tool
    result or text Claude Code injected on its own (isMeta)."""
    if e.get("type") != "user" or e.get("isMeta"):
        return False
    c = e.get("message", {}).get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        kinds = {b.get("type") for b in c if isinstance(b, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


def last_reply(path: pathlib.Path, previous: bool = False) -> str:
    """The final assistant text in a session transcript.

    previous=True: the reply as it stood when the latest prompt arrived. /speak
    runs as a turn of its own, and whatever Claude says during that turn is not
    what the person asked to hear.
    """
    text = answered = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if previous and is_prompt(e):
            answered = text
            continue
        if e.get("type") != "assistant":
            continue
        blocks = e.get("message", {}).get("content", [])
        if not isinstance(blocks, list):
            continue
        joined = "".join(b.get("text", "") for b in blocks
                         if isinstance(b, dict) and b.get("type") == "text")
        if joined.strip():
            text = joined
    return answered if previous else text

# ------------------------------------------------------------- markdown → speech

CYRILLIC = re.compile(r"[\u0400-\u04FF]")
LATIN = re.compile(r"[A-Za-z\u00C0-\u024F]")
# Pictographs, dingbats and their joiners: a voice reads 🔊 as "speaker high
# volume". Arrows become a pause instead.
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]")
TABLE_ROW = re.compile(r"^\s*\|(.*)\|\s*$")
TABLE_RULE = re.compile(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*(:?-{2,}:?\s*)?$")
RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")

PHRASES = {
    "en": {"code": "code omitted", "link": "link",
           "more": "… I'll stop there — the rest is on screen."},
    "ru": {"code": "код пропущен", "link": "ссылка",
           "more": "… На этом остановлюсь — остальное на экране."},
}


def text_lang(s: str) -> str | None:
    """"ru" or "en" by which alphabet more words of s are in; None without words.

    Two voices cover a Russian/English reply: Cyrillic goes to the Russian
    voice, anything in Latin letters to the other. Russian technical prose is
    full of English names ("Запусти npm install и затем npm run build"), while
    English prose almost never holds two Russian words. So two Russian words
    make a sentence Russian; otherwise the majority of words decides, a tie
    going to Russian.
    """
    words = re.findall(r"[^\W\d_]+", s)
    cyr = sum(1 for w in words if CYRILLIC.search(w))
    lat = sum(1 for w in words if not CYRILLIC.search(w) and LATIN.search(w))
    if not cyr and not lat:
        return None
    return "ru" if cyr >= 2 or cyr >= lat else "en"


def code_words(code: str) -> str:
    """Inline code as it should sound: speak_direct() → "speak direct"."""
    return re.sub(r"\(\)", "", code).replace("_", " ")


def spoken_form(md: str, max_chars: int) -> str:
    """Markdown reads terribly aloud: keep the prose, say what was left out.

    Every line ends in punctuation, so headings, list items and table rows
    get a pause instead of running into the next line.
    """
    lang = text_lang(re.sub(r"```.*?(```|\Z)", "", md, flags=re.S)) or "en"
    say = PHRASES[lang]
    s = md.replace("\r\n", "\n")
    # Fenced code, including a fence the reply left open.
    s = re.sub(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)",
               f"\n{say['code']}.\n", s, flags=re.S | re.M)
    s = re.sub(r"```.*?```", f" {say['code']} ", s, flags=re.S)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)                   # images
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)                # links: keep the words
    s = re.sub(r"<https?://[^>\s]+>", f" {say['link']} ", s)
    s = re.sub(r"https?://[^\s)>\]]+", f" {say['link']} ", s)
    s = re.sub(r"</?[A-Za-z][^>\n]*>", " ", s)                    # HTML tags
    s = re.sub(r"`([^`\n]+)`", lambda m: code_words(m.group(1)), s)
    s = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"\2", s)        # bold
    s = re.sub(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", s)
    s = re.sub(r"(?<!\w)_(?=[^\s_])([^_\n]+?)(?<=\S)_(?!\w)", r"\1", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    s = re.sub(r"(?<=\w)_(?=\w)", " ", s)                         # snake_case
    s = EMOJI.sub("", s)
    s = re.sub(r"\s*[→⇒⟶]\s*", " — ", s)

    lines = []
    for line in s.split("\n"):
        if TABLE_RULE.match(line) or RULE.match(line):
            continue
        row = TABLE_ROW.match(line)
        if row:                                                   # a row is a sentence
            line = ", ".join(c.strip() for c in row.group(1).split("|") if c.strip())
        line = re.sub(r"^\s*#{1,6}\s+", "", line)                 # heading
        line = re.sub(r"^\s*(>\s*)+", "", line)                   # quote
        line = re.sub(r"^\s*[-*+]\s+(\[[ xX]\]\s+)?", "", line)   # bullet, task box
        line = line.strip()
        if line:
            lines.append(line if line[-1] in ".!?…:;," else line + ".")
    s = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if len(s) > max_chars:
        cut = s[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        s = (cut[:end + 1] if end > max_chars // 2 else cut) + " " + say["more"]
    return s


def segments(text: str) -> list[tuple[str, str]]:
    """Split spoken text into runs of one language: [("ru", "…"), ("en", "…")].

    A sentence without letters ("2.", "—") joins its neighbour's run. A
    period followed by a lowercase word is an abbreviation ("см. файл",
    "e.g. this"), not the end of a sentence.
    """
    sents = [x for x in re.split(r"(?<=[.!?;:…]) +(?![a-zа-яё])", text) if x.strip()]
    langs = [text_lang(x) for x in sents]
    known = [l for l in langs if l]
    last = known[0] if known else "en"
    runs: list[list[str]] = []
    for sent, lang in zip(sents, langs):
        lang = lang or last
        last = lang
        if runs and runs[-1][0] == lang:
            runs[-1][1] += " " + sent
        else:
            runs.append([lang, sent])
    return [(lang, t) for lang, t in runs]

# ------------------------------------------------------------------------ chunking


def chunk_text(s: str, first: int, full: int) -> list[str]:
    """Split at sentence boundaries; sizes ramp first → 4×first → full."""
    def limits():
        yield min(first, full)
        yield min(4 * first, full)
        while True:
            yield full

    gen = limits()
    limit = next(gen)
    parts: list[str] = []
    cur = ""
    for sent in re.split(r"(?<=[.!?;:]) +", s):
        while len(sent) > limit:                # pathological unbroken run
            take = limit - len(cur) - 1 if cur else limit
            if cur and take < 40:               # no room left in this chunk
                parts.append(cur)
                cur = ""
                limit = next(gen)
                continue
            head, sent = sent[:take], sent[take:]
            cur = f"{cur} {head}".strip()
            parts.append(cur)
            cur = ""
            limit = next(gen)
        if cur and len(cur) + len(sent) + 1 > limit:
            parts.append(cur)
            cur = sent
            limit = next(gen)
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        parts.append(cur)
    return parts

# ------------------------------------------------------------------- audio plumbing


def fix_riff_sizes(b: bytes) -> bytes:
    """Repair streaming WAV headers whose size fields are 0xFFFFFFFF.

    Speechify (and some streaming encoders) emit RIFF/data chunk sizes as the
    placeholder 0xFFFFFFFF; players that trust the header then misread the
    file — heard as a burst of static. Sizes are recomputed from actual length.
    """
    if len(b) < 44 or b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        return b
    out = bytearray(b)
    struct.pack_into("<I", out, 4, len(out) - 8)
    off = 12
    while off + 8 <= len(out):
        cid = bytes(out[off:off + 4])
        size = struct.unpack_from("<I", out, off + 4)[0]
        actual_rest = len(out) - off - 8
        if cid == b"data":
            if size == 0xFFFFFFFF or size > actual_rest:
                struct.pack_into("<I", out, off + 4, actual_rest)
            break
        if size == 0xFFFFFFFF or size > actual_rest:
            break
        off += 8 + size + (size & 1)
    return bytes(out)


def wrap_pcm(pcm: bytes, out: pathlib.Path, rate: int = 22050) -> None:
    """Wrap raw 16-bit mono PCM in a WAV container (ElevenLabs pcm_* output)."""
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _which(*names: str) -> str | None:
    from shutil import which
    for n in names:
        if which(n):
            return n
    return None


# Windows gives every console app it spawns a console window of its own, so
# each detached run, each powershell player and the kokoro runner would flash a
# black box on screen. CREATE_NO_WINDOW suppresses it — and is *ignored* when
# combined with DETACHED_PROCESS, so it must replace that flag, not join it.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def quiet_win() -> dict:
    """Popen kwargs that keep Windows from flashing a console window."""
    return {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}


def powershell(script: str, **values: str) -> tuple[list[str], dict]:
    """A PowerShell command whose inputs arrive as DATA, never as code.

    Each value is handed over in an environment variable ($env:CRA_<NAME>) and
    the script only ever reads it from there. Splicing text into the -Command
    string instead means quoting it for PowerShell, and doubling ' is not
    enough: PowerShell also closes a single-quoted string on the typographic
    quotes ‘ ’ ‚ ‛, which Claude's replies are full of. One ’ in a reply
    ended the string and ran whatever followed as a command.
    """
    env = dict(os.environ)
    for name, value in values.items():
        env[f"CRA_{name.upper()}"] = value
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env


def player_cmd(wav: pathlib.Path) -> tuple[list[str], dict | None]:
    """Command (and environment, or None to inherit) that plays one WAV file."""
    if IS_MAC:
        return ["afplay", str(wav)], None
    if IS_WIN:
        return powershell("(New-Object Media.SoundPlayer $env:CRA_WAV).PlaySync()",
                          wav=str(wav))
    p = _which("aplay", "paplay", "ffplay")
    if not p:
        sys.exit("no audio player found (need aplay, paplay, or ffplay)")
    if p == "ffplay":
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(wav)], None
    return ([p, "-q", str(wav)] if p == "aplay" else [p, str(wav)]), None

# ---------------------------------------------------------------------- providers


def not_an_option(text: str) -> str:
    """A reply starting with "-" would be parsed as an option (say -o FILE…);
    a leading space keeps it an operand and is not heard."""
    return " " + text if text.startswith("-") else text


def command_argv(template: str, text: str = "", out: str = "") -> list[str]:
    """argv for the custom "command" engine, {text} and {out} filled in.

    Each placeholder lands inside one argv entry and no shell is involved, so
    the reply cannot add arguments or commands, with one exception: on Windows
    a .bat/.cmd program is run through cmd.exe, which re-parses the whole line,
    and a reply containing & or | would run commands. Those are refused.
    """
    argv = shlex.split(template)
    if not argv:
        sys.exit(f'provider "command" has an empty command template in {config_path()}')
    prog = pathlib.PureWindowsPath(argv[0])
    if IS_WIN and (prog.suffix.lower() in (".bat", ".cmd")
                   or prog.name.lower() in ("cmd", "cmd.exe")):
        sys.exit("a .bat/.cmd file cannot be the command engine on Windows: cmd.exe "
                 "would run parts of the reply as commands. Point \"command\" at "
                 "the real program (an .exe, or python with a script).")
    text = not_an_option(text)
    return [a.replace("{text}", text).replace("{out}", out) for a in argv]


def http_json(url: str, payload: dict, headers: dict) -> bytes:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def make_synth(cfg: dict):
    """Return synth(text, out_path) writing a playable WAV, or None when the
    provider speaks directly (system voices, self-playing custom commands)."""
    provider = cfg["provider"]

    if provider == "speechify":
        import base64
        key = api_key(cfg, "speechify")

        def synth(text: str, out: pathlib.Path) -> None:
            raw = http_json(
                "https://api.sws.speechify.com/v1/audio/speech",
                {"input": text, "voice_id": cfg["voice"] or "oliver",
                 "audio_format": "wav"},
                {"Authorization": f"Bearer {key}"})
            audio = base64.b64decode(json.loads(raw)["audio_data"])
            out.write_bytes(fix_riff_sizes(audio))
        return synth

    if provider == "elevenlabs":
        key = api_key(cfg, "elevenlabs")
        voice = cfg["voice"] or "21m00Tcm4TlvDq8ikWAM"      # Rachel (premade)

        def synth(text: str, out: pathlib.Path) -> None:
            pcm = http_json(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
                "?output_format=pcm_22050",
                {"text": text},
                {"xi-api-key": key})
            wrap_pcm(pcm, out)
        return synth

    if provider == "openai":
        key = api_key(cfg, "openai")

        def synth(text: str, out: pathlib.Path) -> None:
            audio = http_json(
                "https://api.openai.com/v1/audio/speech",
                {"model": cfg["model"] or "tts-1",
                 "voice": cfg["voice"] or "alloy",
                 "input": text, "response_format": "wav",
                 "speed": cfg["speed"] or 1.0},
                {"Authorization": f"Bearer {key}"})
            out.write_bytes(fix_riff_sizes(audio))
        return synth

    if provider == "kokoro":
        vp, model, voices_bin, runner = kokoro_paths()
        if not (vp.exists() and model.exists() and voices_bin.exists() and runner.exists()):
            sys.exit("kokoro is not set up — run /read-aloud:voice-setup "
                     "(or: speak.py --setup kokoro)")

        def synth(text: str, out: pathlib.Path) -> None:
            # One long-lived runner keeps the 300MB model loaded across chunks;
            # spawning per chunk would reload it every time.
            global _kokoro_runner
            if _kokoro_runner is None or _kokoro_runner.poll() is not None:
                _kokoro_runner = subprocess.Popen(
                    [str(vp), str(runner), str(model), str(voices_bin)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                    **quiet_win())
            v = cfg["voice"] or "am_michael"
            req = {"text": text, "voice": v, "lang": kokoro_lang(v),
                   "speed": float(cfg["speed"] or 1.0), "out": str(out)}
            _kokoro_runner.stdin.write(json.dumps(req) + "\n")
            _kokoro_runner.stdin.flush()
            line = _kokoro_runner.stdout.readline()
            if not line.startswith("ok"):
                raise RuntimeError(f"kokoro runner: {line.strip() or 'died'}")
        return synth

    if provider == "command":
        template = cfg["command"]
        if not template:
            sys.exit(f'provider "command" needs a command template in {config_path()}')
        command_argv(template)                   # reject an unsafe template up front
        if "{out}" not in template:
            return None                          # self-playing command

        def synth(text: str, out: pathlib.Path) -> None:
            subprocess.run(command_argv(template, text=text, out=str(out)),
                           capture_output=True, check=True)
        return synth

    return None                                  # system voices speak directly


# One PowerShell process reads every run, switching voice per language.
# Choice per run: the voice configured for that language (voices.ru), else
# the default voice if it speaks the language, else the first installed voice
# that does, else the default voice anyway. Text arrives through a UTF-8 job
# file, never through the script. CRA_DRY prints the choice instead.
WIN_SPEAK = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = {rate}
$job = [IO.File]::ReadAllText($env:CRA_JOB, [Text.Encoding]::UTF8) | ConvertFrom-Json
$all = @($s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo })
function Pick([string]$lang) {
  $want = $job.voices.$lang
  if ($want -and ($all | Where-Object { $_.Name -eq $want })) { return $want }
  $mine = $all | Where-Object { $_.Name -eq $job.voice } | Select-Object -First 1
  if ($mine -and $mine.Culture.TwoLetterISOLanguageName -eq $lang) { return $mine.Name }
  $any = $all | Where-Object { $_.Culture.TwoLetterISOLanguageName -eq $lang } | Select-Object -First 1
  if ($any) { return $any.Name }
  if ($mine) { return $mine.Name }
  return ''
}
if ($env:CRA_DRY) { [Console]::OutputEncoding = [Text.Encoding]::UTF8 }
foreach ($seg in $job.segments) {
  $name = Pick $seg.lang
  if ($env:CRA_DRY) { [Console]::Out.WriteLine($name + "`t" + $seg.text); continue }
  if ($name) { $s.SelectVoice($name) }
  $s.Speak($seg.text)
}
"""

_mac_voices: list[tuple[str, str]] | None = None


def mac_voices() -> list[tuple[str, str]]:
    """[(name, locale)] from `say -v ?`, read once per run."""
    global _mac_voices
    if _mac_voices is None:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
        _mac_voices = [(m.group(1).strip(), m.group(2)) for m in
                       (re.match(r"^(.*?)\s{2,}([a-z]{2}_\w+)", ln) for ln in out.splitlines())
                       if m]
    return _mac_voices


def mac_voice_for(lang: str, cfg: dict) -> str:
    """Same choice as on Windows (see WIN_SPEAK), from macOS's voices."""
    want = (cfg.get("voices") or {}).get(lang)
    if want:
        return want
    listed = mac_voices()
    mine = next((loc for name, loc in listed if name == cfg["voice"]), None)
    if mine and mine.startswith(lang):
        return cfg["voice"]
    return next((name for name, loc in listed if loc.startswith(lang + "_")),
                cfg["voice"] or "")


def system_speech(segs: list[tuple[str, str]], cfg: dict) -> list[tuple[list[str], dict | None]]:
    """Commands that read segs aloud with the system voices, one voice per language.
    Run them in order; each returns when its speech ends."""
    speed = float(cfg["speed"] or 1.0)
    if IS_WIN:
        JOB.write_text(json.dumps({
            "voice": str(cfg["voice"] or ""), "voices": cfg.get("voices") or {},
            "segments": [{"lang": lang, "text": t} for lang, t in segs],
        }, ensure_ascii=False), encoding="utf-8")
        rate = max(-10, min(10, int((speed - 1.0) * 10)))
        return [powershell(WIN_SPEAK.replace("{rate}", str(rate)), job=str(JOB))]
    cmds = []
    for lang, t in segs:
        if IS_MAC:
            cmd = ["say"]
            voice = mac_voice_for(lang, cfg)
            if voice:
                cmd += ["-v", voice]
            if abs(speed - 1.0) > 0.01:
                cmd += ["-r", str(int(190 * speed))]
        else:
            if not _which("spd-say"):
                sys.exit("no system voice found — install speech-dispatcher, "
                         "or pick a cloud provider (see --status)")
            cmd = ["spd-say", "-w", "-l", lang]
            voice = (cfg.get("voices") or {}).get(lang) or (cfg["voice"] if lang == "en" else "")
            if voice:
                cmd += ["-y", voice]
            if abs(speed - 1.0) > 0.01:
                cmd += ["-r", str(max(-100, min(100, int((speed - 1.0) * 100))))]
        cmds.append((cmd + [not_an_option(t)], None))
    return cmds


def run_players(cmds: list[tuple[list[str], dict | None]]) -> None:
    """Run speaking/playing commands one after another. Their stderr is ours,
    so in a detached reading it lands in the log."""
    global _player
    for cmd, env in cmds:
        _player = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, **quiet_win())
        code = _player.wait()
        if code != 0:
            raise RuntimeError(f"{pathlib.Path(cmd[0]).name} exited with code {code}")


def speak_direct(text: str, cfg: dict) -> None:
    """System TTS (and self-playing custom commands): no files, near-zero latency."""
    if cfg["provider"] == "command":
        cmds = [(command_argv(cfg["command"], text=text), None)]
    else:
        cmds = system_speech(segments(text), cfg)
    claim()
    try:
        run_players(cmds)
    finally:
        release()

# ------------------------------------------------------------------ kokoro (local)

_kokoro_runner: subprocess.Popen | None = None

KOKORO_URLS = {
    "kokoro-v1.0.onnx":
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin":
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/voices-v1.0.bin",
}
# SHA-256 of each release file. The model is loaded and run by the runner, so
# a file swapped on the server (or in transit) must be refused, not used.
KOKORO_SHA256 = {
    "kokoro-v1.0.onnx":
        "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin":
        "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
# Exact versions, wheels only: an unpinned install takes whatever is newest on
# PyPI the day it runs, and a source build runs the package's own setup code.
KOKORO_PACKAGES = ["kokoro-onnx==0.5.0", "soundfile==0.14.0"]


def sha256_of(path: pathlib.Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()

# Written to disk at setup; runs inside the plugin's own venv where
# kokoro-onnx and soundfile exist. The plugin itself stays stdlib-only.
KOKORO_RUNNER = '''import json, sys
from kokoro_onnx import Kokoro
import soundfile as sf
k = Kokoro(sys.argv[1], sys.argv[2])
for line in sys.stdin:
    try:
        q = json.loads(line)
        # Default keeps an older on-disk runner working with a newer speak.py.
        s, r = k.create(q["text"], voice=q["voice"], speed=q["speed"],
                        lang=q.get("lang", "en-us"))
        sf.write(q["out"], s, r)
        print("ok", flush=True)
    except Exception as e:
        print("err " + str(e).replace(chr(10), " "), flush=True)
'''


def kokoro_paths() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    d = data_dir()
    vp = d / "venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
    return vp, d / "kokoro" / "kokoro-v1.0.onnx", d / "kokoro" / "voices-v1.0.bin", \
        d / "kokoro_runner.py"


def setup_kokoro() -> int:
    """One-time install of the free local neural voice (~340MB, no account)."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    vp, model, voices_bin, runner = kokoro_paths()

    if not vp.exists():
        if not (3, 10) <= sys.version_info[:2] <= (3, 13):
            sys.exit(f"kokoro-onnx needs Python 3.10–3.13, and this is "
                     f"{sys.version.split()[0]}. Run this setup with one of those, "
                     f"e.g.  py -3.12 speak.py --setup kokoro")
        print("creating a private virtualenv…", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(d / "venv")], check=True)
    print("installing kokoro-onnx (a few minutes on first run)…", flush=True)
    subprocess.run([str(vp), "-m", "pip", "install", "--quiet", "--only-binary=:all:",
                    *KOKORO_PACKAGES], check=True)

    (d / "kokoro").mkdir(exist_ok=True)
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        dest = d / "kokoro" / name
        if dest.exists() and sha256_of(dest) == KOKORO_SHA256[name]:
            print(f"{name}: already present, checksum ok", flush=True)
            continue
        print(f"downloading {name}…", flush=True)
        tmp = dest.with_suffix(".part")
        with urllib.request.urlopen(KOKORO_URLS[name], timeout=60) as r, \
                open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                got += len(buf)
                if total and got % (50 << 20) < (1 << 20):
                    print(f"  {got >> 20} / {total >> 20} MB", flush=True)
        digest = sha256_of(tmp)
        if digest != KOKORO_SHA256[name]:
            tmp.unlink(missing_ok=True)
            sys.exit(f"{name}: checksum mismatch, refusing to use it "
                     f"(expected {KOKORO_SHA256[name]}, got {digest})")
        tmp.replace(dest)
        print(f"  {name}: {dest.stat().st_size >> 20} MB", flush=True)

    runner.write_text(KOKORO_RUNNER, encoding="utf-8")

    cfg = load_config()
    if cfg["provider"] == "system":
        cfg["provider"] = "kokoro"
        if str(cfg["voice"]) not in KOKORO_VOICES:
            cfg["voice"] = "am_michael"
        save_config(cfg)
        print("\nKokoro installed — provider set to kokoro (voice am_michael).")
    else:
        # Someone who deliberately configured a cloud voice keeps it.
        print(f"\nKokoro installed. Provider left as {cfg['provider']} — switch by "
              f"setting \"provider\": \"kokoro\" in {config_path()}")
    print("54 voices (am_michael, am_puck, af_heart, bf_emma, …) — set \"voice\" in the config.")
    return 0

# ----------------------------------------------------------------------- playback


_player: subprocess.Popen | None = None


def process_start(pid: int) -> str | None:
    """When process `pid` started, as an opaque string; None if it is not running.

    A pid alone does not name a process: once a reader crashes without
    releasing the pidfile, the operating system hands its number to the next
    process that starts (Windows does so within seconds), and "stop the
    reading" would then kill that one: an editor, a browser, anything. The
    pidfile therefore records the start time too, and stop() kills only when
    both still match.
    """
    try:
        if IS_WIN:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + \
                [ctypes.POINTER(wintypes.FILETIME)] * 4
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) \
                        or code.value != 259:                # STILL_ACTIVE
                    return None
                times = [wintypes.FILETIME() for _ in range(4)]
                if not k32.GetProcessTimes(handle, *map(ctypes.byref, times)):
                    return None
                return str(times[0].dwHighDateTime << 32 | times[0].dwLowDateTime)
            finally:
                k32.CloseHandle(handle)
        stat = pathlib.Path(f"/proc/{pid}/stat")
        if stat.exists():                        # Linux: field 22, after "(comm)"
            return stat.read_text().rsplit(")", 1)[1].split()[19]
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
        return out or None
    except (OSError, IndexError, AttributeError):
        return None


def kill_reader(pid: int) -> None:
    """End a reader and everything it started.

    On Windows os.kill() is TerminateProcess: the reader's SIGTERM handler
    never runs, so the player it spawned would talk on. Kill the whole tree.
    """
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, **quiet_win())
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass                                     # already gone


# The session this reading belongs to ("" when started outside one, e.g. from
# VS Code). A new prompt stops its own session's reading, no one else's.
_session = ""


def read_pidfile() -> tuple[int, str, str] | None:
    """(pid, start time, session) of the recorded reader, or None.

    The file reads "PID START SESSION", "-" standing for an empty field; an
    older version wrote only "PID".
    """
    try:
        fields = PIDFILE.read_text().split()
        fields = [("" if f == "-" else f) for f in fields] + ["", ""]
        return int(fields[0]), fields[1], fields[2]
    except (OSError, ValueError, IndexError):
        return None


def stop(only_session: str | None = None) -> None:
    """Stop whoever holds the reading. Never this process — see claim().

    only_session: stop it only if that session started it.
    """
    if not PIDFILE.exists():
        return
    rec = read_pidfile()
    if rec and rec[0] == os.getpid():
        return
    if only_session is not None and (not rec or rec[2] != only_session):
        return
    # A record without a start time comes from an older version; it cannot be
    # verified, so it is dropped rather than trusted.
    if rec and rec[1] and process_start(rec[0]) == rec[1]:
        kill_reader(rec[0])
    PIDFILE.unlink(missing_ok=True)


def claim() -> None:
    """Become the one reader: stop the current reading, then take the pidfile.

    Claimed when a run STARTS, not when its audio does. A reading spends its
    first seconds loading a model or waiting on an API, and a second click
    landing in that window used to find an empty pidfile, stop nothing, and
    leave two voices talking over each other.
    """
    stop()
    PIDFILE.write_text(f"{os.getpid()} {process_start(os.getpid()) or '-'} {_session or '-'}")
    signal.signal(signal.SIGTERM, _on_term)
    _sweep_parts()


def release() -> None:
    """Hand back the pidfile if it is still ours, and take our scratch files with us."""
    rec = read_pidfile()
    if rec and rec[0] == os.getpid():
        PIDFILE.unlink(missing_ok=True)
    for part in (*PARTS, JOB):
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


def mark_own_turn(session: str) -> None:
    """Note that the current turn of `session` is one of this plugin's commands.

    Its reply ("🔇 stopped", "auto-read on") is not something to read aloud,
    and reading it would also cut off the reading /speak just started. The
    Stop hook of the same turn finds this note and skips that one reply.
    """
    try:
        (pathlib.Path(tempfile.gettempdir()) / f"claude-read-aloud-quiet-{session}").touch()
    except OSError:
        pass


def is_own_turn(session: str) -> bool:
    """Consume the note mark_own_turn() left, if any (and not a stale one)."""
    p = pathlib.Path(tempfile.gettempdir()) / f"claude-read-aloud-quiet-{session}"
    try:
        fresh = time.time() - p.stat().st_mtime < 600
        p.unlink()
        return fresh
    except OSError:
        return False


def _on_term(_sig=None, _frm=None):
    if _player and _player.poll() is None:
        _player.terminate()
    if _kokoro_runner and _kokoro_runner.poll() is None:
        _kokoro_runner.terminate()
    release()
    os._exit(0)


def _sweep_parts() -> None:
    """Bin scratch files a crashed reading left behind — an hour is long past."""
    cutoff = time.time() - 3600
    try:
        for pattern in SCRATCH_GLOBS:
            for part in pathlib.Path(tempfile.gettempdir()).glob(pattern):
                if part.stat().st_mtime < cutoff:
                    part.unlink(missing_ok=True)
    except OSError:
        pass


def play_chunked(text: str, cfg: dict, synth, own_langs: set[str] | None = None) -> None:
    """Gapless chunked playback: synthesise the next chunk while one plays.

    own_langs: the languages synth can voice (None: all of them). Runs in any
    other language are read by the system voice for that language: Kokoro has
    no Russian voice, the system usually does.
    """
    claim()                                      # never overlap two readings
    try:
        first, full = int(cfg["first_chars"]), int(cfg["chunk_chars"])
        if own_langs is None:
            chunks = [("", c) for c in chunk_text(text, first, full)]
        else:
            chunks = [(lang, c) for lang, run in segments(text)
                      for c in chunk_text(run, first, full)]
        own = [i for i, (lang, _) in enumerate(chunks) if own_langs is None or lang in own_langs]
        # Own chunks alternate between the two scratch files, so the one being
        # written is never the one being played.
        slot = {i: PARTS[k % 2] for k, i in enumerate(own)}

        def render(i: int) -> None:
            # Delete first: if synthesis fails, nothing stale is left to play
            # (the file still held the chunk from two steps back).
            slot[i].unlink(missing_ok=True)
            synth(chunks[i][1], slot[i])

        def prefetch(i: int | None):
            if i is None:
                return None
            errors: list[Exception] = []

            def work():
                try:
                    render(i)
                except Exception as e:           # noqa: BLE001 — reported below
                    errors.append(e)
            t = threading.Thread(target=work, daemon=True)
            t.start()
            return t, errors

        after = lambda i: next((j for j in own if j > i), None)
        ahead = prefetch(own[0] if own else None)
        for i, (lang, chunk) in enumerate(chunks):
            if i not in slot:
                run_players(system_speech([(lang, chunk)], cfg))
                continue
            thread, errors = ahead
            thread.join()
            if errors:                           # one retry; a second failure ends it
                log(f"chunk {i + 1} of {len(chunks)} failed ({errors[0]}), retrying")
                render(i)
            ahead = prefetch(after(i))
            run_players([player_cmd(slot[i])])
    finally:
        release()                                # a failed request must not strand it

# --------------------------------------------------------------------------- modes


def detach(argv: list[str]) -> subprocess.Popen:
    """Re-launch this script detached so the caller returns immediately.

    Nobody watches a detached reading, so its stderr goes to the log: a wrong
    key or a missing voice is then findable instead of plain silence.
    """
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        kwargs["stderr"] = open(log_path(), "a", encoding="utf-8")
    except OSError:
        kwargs["stderr"] = subprocess.DEVNULL
    if IS_WIN:
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([sys.executable, os.path.abspath(__file__), *argv], **kwargs)


def handoff(text: str) -> str:
    """Put text in a scratch file for a detached reader (--text-file), which
    deletes it. A command line is too short for a long reply, and on Windows
    it is re-quoted on the way through."""
    fd, path = tempfile.mkstemp(prefix="claude-read-aloud-handoff-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# The plugin's own commands, typed as /read-aloud:speak or, when unambiguous, /speak.
OWN_COMMANDS = ("speak", "speak-stop", "speak-auto", "speak-status", "voice", "voice-setup")


def hook_mode() -> int:
    """Hook entry: exit fast and NEVER fail — Claude's flow comes first.

    Stop: read the reply just finished, if auto-read is on. The text comes
    from the hook input (last_assistant_message), not the transcript file,
    which Claude Code writes asynchronously and can still hold the previous
    reply.
    UserPromptSubmit: a new prompt stops that session's reading.
    """
    try:
        event = json.loads(sys.stdin.read() or "{}")
        cfg = load_config()
        session = valid_session(event.get("session_id"))
        if event.get("hook_event_name") == "UserPromptSubmit":
            word = str(event.get("prompt") or "").strip().split(" ", 1)[0]
            own = word.startswith("/read-aloud:") or word.lstrip("/") in OWN_COMMANDS
            if session and cfg.get("stop_on_prompt", True) and not own:
                stop(only_session=session)
            return 0
        if session and is_own_turn(session):
            return 0
        if not cfg.get("auto_read"):
            return 0
        extra = ["--session", session] if session else []
        text = event.get("last_assistant_message")
        if isinstance(text, str) and text.strip():
            detach(["--text-file", handoff(text), *extra])
            return 0
        path = event.get("transcript_path", "")    # older Claude Code
        if path and pathlib.Path(path).exists():
            detach(["--transcript", path, *extra])
    except Exception as e:                        # noqa: BLE001 — deliberate
        log(f"hook: {e}")
    return 0


# Kokoro's voice roster is fixed per model release, and the model cannot be
# asked for it cheaply — listing must not load 300MB. English voices first.
KOKORO_VOICES = [
    "am_michael", "am_puck", "af_heart", "af_bella", "af_nova", "af_sarah",
    "af_sky", "af_alloy", "af_aoede", "af_jessica", "af_kore", "af_nicole",
    "af_river", "am_adam", "am_echo", "am_eric", "am_fenrir",
    "am_liam", "am_onyx", "am_santa",
    "bf_emma", "bf_alice", "bf_isabella", "bf_lily",
    "bm_george", "bm_daniel", "bm_fable", "bm_lewis",
    # The model ships 54 voices in 9 languages; the bundled voices bin already
    # contains all of them, so none of these cost an extra download.
    "ef_dora", "em_alex", "em_santa",
    "ff_siwis",
    "if_sara", "im_nicola",
    "pf_dora", "pm_alex", "pm_santa",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
]

# A voice's first letter encodes its language, and phonemization must follow the
# voice: a Spanish voice fed English phonemes reads Spanish with an English
# accent. Each code below was verified against a voice of that language.
#
# Every entry carries a display label, American English included: three voices
# share the name "santa" and two each share "dora"/"alex"/"alpha" across
# languages, so an unlabelled default would read as ambiguous, not merely terse.
KOKORO_LANGS = {
    "a": ("en-us", "American"),  "b": ("en-gb", "British"),
    "e": ("es", "Spanish"),      "f": ("fr-fr", "French"),
    "h": ("hi", "Hindi"),        "i": ("it", "Italian"),
    "j": ("ja", "Japanese"),     "p": ("pt-br", "Brazilian Portuguese"),
    "z": ("cmn", "Mandarin"),
}


def kokoro_lang(voice: str) -> str:
    """espeak language code for a Kokoro voice id, by its prefix letter."""
    return KOKORO_LANGS.get((voice or "a")[:1], ("en-us", ""))[0]


def kokoro_label(voice: str) -> str:
    """Display name for a Kokoro voice id: bare name plus its language."""
    lang = KOKORO_LANGS.get(voice[0], ("", ""))[1]
    return voice.split("_", 1)[1] + (f" ({lang})" if lang else "")


OPENAI_VOICES = ["alloy", "ash", "coral", "echo", "fable", "nova", "onyx",
                 "sage", "shimmer"]


def list_voices(cfg: dict) -> int:
    """Print {provider, voices:[{id,label}]} as JSON for pickers to consume."""
    provider = cfg["provider"]
    voices: list[dict] = []

    if provider == "kokoro":
        voices = [{"id": v, "label": kokoro_label(v)} for v in KOKORO_VOICES]
    elif provider == "openai":
        voices = [{"id": v, "label": v} for v in OPENAI_VOICES]
    elif provider == "speechify":
        key = api_key(cfg, "speechify")
        cursor, pages = "", 0
        while pages < 12:
            pages += 1
            url = ("https://api.sws.speechify.com/v1/voices?limit=100"
                   + (f"&cursor={cursor}" if cursor else ""))
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                page = json.loads(r.read())
            for v in page.get("voices", []):
                if str(v.get("locale", "")).startswith("en"):
                    voices.append({"id": v["id"],
                                   "label": f"{v.get('display_name', v['id'])} "
                                            f"({v.get('locale', '')}, {v.get('gender', '?')})"})
            cursor = page.get("next_cursor") or ""
            if not page.get("has_more") or not cursor:
                break
    elif provider == "elevenlabs":
        key = api_key(cfg, "elevenlabs")
        req = urllib.request.Request("https://api.elevenlabs.io/v1/voices",
                                     headers={"xi-api-key": key})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        voices = [{"id": v["voice_id"], "label": v.get("name", v["voice_id"])}
                  for v in data.get("voices", [])]
    elif provider == "system":
        if IS_MAC:
            out = subprocess.run(["say", "-v", "?"], capture_output=True,
                                 text=True).stdout
            for line in out.splitlines():
                m = re.match(r"^(.*?)\s{2,}([a-z]{2}_\w+)", line)
                if m:
                    voices.append({"id": m.group(1).strip(),
                                   "label": f"{m.group(1).strip()} ({m.group(2)})",
                                   "lang": m.group(2)[:2]})
        elif IS_WIN:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -AssemblyName System.Speech;"
                 "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                 ".GetInstalledVoices()|ForEach-Object{"
                 "$_.VoiceInfo.Name + '|' + $_.VoiceInfo.Culture.Name}"],
                capture_output=True, text=True).stdout
            for line in out.splitlines():
                name, _, culture = line.strip().partition("|")
                if name:
                    voices.append({"id": name, "label": f"{name} ({culture})",
                                   "lang": culture.split("-")[0]})
        else:
            out = subprocess.run(["spd-say", "-L"], capture_output=True,
                                 text=True).stdout
            for line in out.splitlines()[1:]:
                # Rows are "NAME LANGUAGE VARIANT" but NAME can contain spaces
                # ("English (Great Britain)+Adam en-gb Adam"), so split from
                # the right. spd-say lists every variant in every language —
                # ~15,000 rows; the two languages read here only, or the
                # picker is unusable.
                parts = line.rsplit(None, 2)
                if len(parts) == 3 and parts[1].lower()[:2] in ("en", "ru"):
                    voices.append({"id": parts[0].strip(),
                                   "label": f"{parts[0].strip()} ({parts[1]})",
                                   "lang": parts[1].lower()[:2]})
    else:
        print(f"provider {provider} has no listable voices", file=sys.stderr)
        return 1

    print(json.dumps({"provider": provider, "voices": voices}, indent=1))
    return 0


def show_status(cfg: dict) -> None:
    keys = {p: ("env" if os.environ.get(f"{p.upper()}_API_KEY")
                else "config" if cfg.get("api_keys", {}).get(p) else "—")
            for p in ("speechify", "elevenlabs", "openai")}
    print(f"config    : {config_path()}")
    print(f"provider  : {cfg['provider']}"
          + (f"  (voice: {cfg['voice']})" if cfg["voice"] else "  (default voice)"))
    per_lang = cfg.get("voices") or {}
    print(f"voices    : " + "  ".join(f"{lang}: {per_lang.get(lang) or 'auto'}"
                                      for lang in ("ru", "en"))
          + "   (system voice per language)")
    print(f"speed     : {cfg['speed']}")
    print(f"auto_read : {'on — every reply is spoken' if cfg['auto_read'] else 'off'}")
    print(f"new prompt: {'stops the reading' if cfg.get('stop_on_prompt', True) else 'reading continues'}"
          f"  (stop_on_prompt)")
    err = last_error()
    if err:
        age = err[1]
        when = (f"{int(age // 60)} min ago" if age < 3600 else
                f"{int(age // 3600)} h ago" if age < 86400 else f"{int(age // 86400)} days ago")
        print(f"last error: ({when}) {err[0]}")
        print(f"log       : {log_path()}")
    print(f"api keys  : " + "  ".join(f"{p}:{s}" for p, s in keys.items()))
    if "config" in keys.values():
        print("warning   : API keys are stored in plain text in the config file. "
              "Prefer environment variables (e.g. OPENAI_API_KEY) and remove "
              "them from api_keys.")
    if cfg["provider"] in ("speechify", "elevenlabs", "openai"):
        print(f"privacy   : replies are sent to {cfg['provider']} to be voiced. "
              f"The system and kokoro providers never leave this machine.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--text")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--transcript")
    ap.add_argument("--project")
    ap.add_argument("--detach", action="store_true")
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--setup", choices=["kokoro"],
                    help="one-time install of the free local neural voice")
    ap.add_argument("--voice",
                    help="use this voice for this run only (audition)")
    ap.add_argument("--list-voices", action="store_true", dest="list_voices",
                    help="print the current provider's voices as JSON")
    ap.add_argument("--set-voice", dest="set_voice",
                    help="save this voice to the config")
    ap.add_argument("--set-provider", dest="set_provider",
                    choices=["system", "kokoro", "speechify", "elevenlabs",
                             "openai", "command"])
    ap.add_argument("--set-speed", dest="set_speed", type=float)
    ap.add_argument("--get-config", action="store_true", dest="get_config",
                    help="print effective config as JSON (for UI panels)")
    ap.add_argument("--auto", choices=["on", "off"])
    ap.add_argument("--print", action="store_true", dest="print_only")
    ap.add_argument("--session", default="",
                    help="Claude Code session id: read its transcript, tag the reading")
    ap.add_argument("--turn", default="",
                    help="like --session, and this run is part of a slash-command "
                         "turn whose own reply must not be read aloud")
    ap.add_argument("--previous", action="store_true",
                    help="read the reply before the latest prompt")
    ap.add_argument("--text-file", dest="text_file",
                    help="speak the text in this file, then delete it")
    ap.add_argument("--lang", choices=["ru", "en"],
                    help="with --set-voice: the language that voice is for")
    args = ap.parse_args()

    # Windows consoles default to a legacy code page that cannot print emoji
    # (or, piped, mangles Cyrillic for whoever reads it). Never crash on output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace",
                               **({} if stream.isatty() else {"encoding": "utf-8"}))
        except (AttributeError, ValueError):
            pass

    global _session
    turn = valid_session(args.turn)
    if turn:
        mark_own_turn(turn)
    _session = valid_session(args.session) or turn

    if args.hook:
        return hook_mode()
    if args.stop:
        stop()
        return 0

    if args.setup:
        return setup_kokoro()

    cfg = load_config()
    if args.get_config:
        vp, model, vb, runner = kokoro_paths()
        print(json.dumps({
            "provider": cfg["provider"], "voice": cfg["voice"],
            "speed": cfg["speed"], "auto_read": bool(cfg["auto_read"]),
            "kokoro_ready": all(p.exists() for p in (vp, model, vb, runner)),
            "keys": {p: bool(os.environ.get(f"{p.upper()}_API_KEY")
                             or cfg.get("api_keys", {}).get(p))
                     for p in ("speechify", "elevenlabs", "openai")},
        }))
        return 0
    if args.set_provider:
        if args.set_provider != cfg["provider"]:
            # A voice id belongs to ONE provider — keeping the old one hands
            # e.g. Kokoro a Speechify id and every reading errors. Reset to
            # empty; each provider has a good built-in default.
            cfg["voice"] = ""
        cfg["provider"] = args.set_provider
        save_config(cfg)
        print(f"provider set to {args.set_provider} (voice: default)")
        return 0
    if args.set_speed is not None:
        cfg["speed"] = max(0.5, min(2.0, args.set_speed))
        save_config(cfg)
        print(f"speed set to {cfg['speed']}")
        return 0
    if args.list_voices:
        return list_voices(cfg)
    if args.set_voice and args.lang:
        per_lang = dict(cfg.get("voices") or {})
        if args.set_voice == "auto":
            per_lang.pop(args.lang, None)
        else:
            per_lang[args.lang] = args.set_voice
        cfg["voices"] = per_lang
        save_config(cfg)
        print(f"{args.lang} voice set to {args.set_voice}")
        return 0
    if args.set_voice:
        cfg["voice"] = args.set_voice
        save_config(cfg)
        print(f"voice set to {args.set_voice} ({cfg['provider']})")
        return 0
    if args.voice:
        cfg["voice"] = args.voice          # this run only; nothing saved
        cfg["voices"] = {}                 # an audition must not be overridden
    if args.status:
        show_status(cfg)
        return 0
    if args.auto:
        cfg["auto_read"] = args.auto == "on"
        save_config(cfg)
        print(f"auto-read {'on — every reply will be spoken' if cfg['auto_read'] else 'off'}")
        return 0

    if args.detach:
        # The reader runs on alone: it is no longer part of the command's turn.
        argv = ["--session" if a == "--turn" else a for a in sys.argv[1:] if a != "--detach"]
        reader = detach(argv)
        try:                                     # most failures show up at once
            code = reader.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            code = None
        if code:
            err = last_error()
            print(f"error: {err[0] if err else f'reader exited with code {code}'}")
            print(f"log: {log_path()}")
            return 1
        print("reading aloud…")
        # First impressions: the stock Linux voice is espeak, and it is rough.
        # Tell the user the good free voice is one command away — once there.
        if (not IS_MAC and not IS_WIN and cfg["provider"] == "system"
                and not kokoro_paths()[1].exists()):
            print("note: this is the basic Linux system voice — run "
                  "/read-aloud:voice-setup once for a much better free "
                  "neural voice (~340MB, local, no account).")
        return 0

    if args.stdin:
        raw = sys.stdin.read()
    elif args.text:
        raw = args.text
    elif args.text_file:
        f = pathlib.Path(args.text_file)
        raw = f.read_text(encoding="utf-8")
        f.unlink(missing_ok=True)
    else:
        t = (pathlib.Path(args.transcript) if args.transcript
             else find_transcript(_session, args.project))
        if not t or not t.exists():
            print("no transcript found", file=sys.stderr)
            return 1
        raw = last_reply(t, previous=args.previous)

    text = spoken_form(raw, int(cfg["max_chars"]))
    if not text:
        print("nothing to speak", file=sys.stderr)
        return 1
    if args.print_only:
        return print_plan(text, cfg)

    # Claimed before the engine is built, not after: loading a 300MB model or
    # waiting on a cloud voice is exactly the window a second click lands in.
    claim()
    try:
        synth = make_synth(cfg)
        if synth is None:
            speak_direct(text, cfg)
        else:
            # Kokoro has no Russian voice; cloud voices are multilingual.
            play_chunked(text, cfg, synth, {"en"} if cfg["provider"] == "kokoro" else None)
    finally:
        release()                                # e.g. a missing API key exits here
    return 0


def print_plan(text: str, cfg: dict) -> int:
    """--print: what would be read, run by run, and by which voice."""
    runs = segments(text)
    if cfg["provider"] == "system" and IS_WIN:
        (cmd, env), = system_speech(runs, cfg)
        env["CRA_DRY"] = "1"
        try:
            out = subprocess.run(cmd, env=env, capture_output=True, **quiet_win())
        finally:
            JOB.unlink(missing_ok=True)
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            voice, _, run = line.partition("\t")
            print(f"[{voice or 'default voice'}] {run}")
        return out.returncode
    for lang, run in runs:
        voice = mac_voice_for(lang, cfg) if cfg["provider"] == "system" and IS_MAC else ""
        print(f"[{voice or lang}] {run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
