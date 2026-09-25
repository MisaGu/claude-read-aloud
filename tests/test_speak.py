"""Regression tests for speak.py. Standard library only:

    python -m unittest discover -s tests
"""
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "speak.py"
spec = importlib.util.spec_from_file_location("speak", SCRIPT)
speak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(speak)

# Replies that used to break out of the PowerShell string: ASCII and
# typographic single quotes (PowerShell closes a '…' string on ‘ ’ ‚ ‛ too),
# plus the other characters PowerShell treats specially.
HOSTILE = [
    "hello'); Write-Output INJECTED; ('",
    "hello\u2019); Write-Output INJECTED; (\u2019",
    "hello\u2018); Write-Output INJECTED; (\u2018",
    "hello\u201a); Write-Output INJECTED; (\u201b",
    'say "$(Write-Output INJECTED)" and `$env:USERNAME`',
    "Привет — don\u2019t stop; 100% & | < > ^",
]


def installed_windows_voices() -> dict:
    """{name: two-letter language} of the System.Speech voices here."""
    if os.name != "nt":
        return {}
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Add-Type -AssemblyName System.Speech;"
         "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices()"
         "|ForEach-Object{$_.VoiceInfo.Name+'|'+$_.VoiceInfo.Culture.TwoLetterISOLanguageName}"],
        capture_output=True, text=True).stdout
    return dict(line.strip().split("|", 1) for line in out.splitlines() if "|" in line)


def dry_run(runs, cfg) -> list[tuple[str, str]]:
    """[(voice, text)] the Windows system voice would read, without speaking."""
    (cmd, env), = speak.system_speech(runs, cfg)
    env["CRA_DRY"] = "1"
    out = subprocess.run(cmd, env=env, capture_output=True)
    return [tuple(line.split("\t", 1)) for line in
            out.stdout.decode("utf-8").splitlines()]


class Scratch(unittest.TestCase):
    """Keeps the pidfile, job file and log out of the real temp/data dirs."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        for name, value in (("PIDFILE", self.tmp / "reader.pid"),
                            ("JOB", self.tmp / "job.json"),
                            ("PROJECTS", self.tmp / "projects")):
            p = mock.patch.object(speak, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(speak, "data_dir", lambda: self.tmp / "data")
        p.start()
        self.addCleanup(p.stop)


# ------------------------------------------------------------------ security


class PowerShellQuoting(Scratch):
    def test_text_never_enters_the_script(self):
        with mock.patch.object(speak, "IS_WIN", True), mock.patch.object(speak, "IS_MAC", False):
            for text in HOSTILE:
                cfg = dict(speak.DEFAULTS, voice="x'); evil; ('")
                (cmd, env), = speak.system_speech([("en", text)], cfg)
                self.assertNotIn(text, " ".join(cmd))
                self.assertNotIn("evil", " ".join(cmd))
                job = json.loads(speak.JOB.read_text(encoding="utf-8"))
                self.assertEqual(job["segments"][0]["text"], text)
                self.assertEqual(env["CRA_JOB"], str(speak.JOB))

    @unittest.skipUnless(os.name == "nt", "needs Windows PowerShell")
    def test_powershell_receives_text_verbatim(self):
        runs = [("en", text) for text in HOSTILE]
        self.assertEqual([t for _, t in dry_run(runs, dict(speak.DEFAULTS))], HOSTILE)

    @unittest.skipUnless(os.name == "nt", "needs Windows PowerShell")
    def test_player_path_is_data(self):
        cmd, env = speak.player_cmd(pathlib.Path("C:/x/it\u2019s.wav"))
        self.assertNotIn("it\u2019s", " ".join(cmd))
        self.assertEqual(env["CRA_WAV"], str(pathlib.Path("C:/x/it\u2019s.wav")))


class OptionLikeReplies(Scratch):
    def test_reply_starting_with_dash_stays_an_operand(self):
        text = "-o /tmp/owned.aiff hello"
        with mock.patch.object(speak, "IS_MAC", True), mock.patch.object(speak, "IS_WIN", False), \
                mock.patch.object(speak, "mac_voices", lambda: []):
            (cmd, _), = speak.system_speech([("en", text)], dict(speak.DEFAULTS))
        self.assertEqual(cmd[-1], " " + text)
        self.assertEqual(speak.command_argv("engine --text {text}", text=text)[-1], " " + text)


class CommandProvider(unittest.TestCase):
    def test_placeholders_stay_single_arguments(self):
        argv = speak.command_argv("engine --say {text} --out {out}",
                                  text="a b; rm -rf ~", out="o.wav")
        self.assertEqual(argv, ["engine", "--say", "a b; rm -rf ~", "--out", "o.wav"])

    def test_batch_files_refused_on_windows(self):
        with mock.patch.object(speak, "IS_WIN", True):
            for template in ("tts.bat {text}", "C:/x/TTS.CMD {text}", "cmd /c say {text}"):
                with self.assertRaises(SystemExit):
                    speak.command_argv(template)
            speak.command_argv("piper.exe --text {text}")      # still allowed


class Pidfile(Scratch):
    def setUp(self):
        super().setUp()
        # A stand-in for "some other program" that ended up with a stale pid.
        self.bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.bystander.wait)
        self.addCleanup(self.bystander.kill)

    def alive(self) -> bool:
        time.sleep(0.5)
        return self.bystander.poll() is None

    def record(self, start=None, session="-"):
        start = start or speak.process_start(self.bystander.pid)
        speak.PIDFILE.write_text(f"{self.bystander.pid} {start} {session}")

    def test_start_time_is_stable_and_distinct(self):
        pid = self.bystander.pid
        self.assertIsNotNone(speak.process_start(pid))
        self.assertEqual(speak.process_start(pid), speak.process_start(pid))
        self.assertNotEqual(speak.process_start(pid), speak.process_start(os.getpid()))

    def test_stale_record_does_not_kill_a_reused_pid(self):
        self.record(start="12345")
        speak.stop()
        self.assertTrue(self.alive())
        self.assertFalse(speak.PIDFILE.exists())

    def test_old_format_record_is_not_trusted(self):
        speak.PIDFILE.write_text(str(self.bystander.pid))
        speak.stop()
        self.assertTrue(self.alive())

    def test_matching_record_is_stopped(self):
        self.record()
        speak.stop()
        self.assertFalse(self.alive())

    def test_prompt_stops_only_its_own_sessions_reading(self):
        self.record(session="session-A")
        speak.stop(only_session="session-B")
        self.assertTrue(self.alive())
        speak.stop(only_session="session-A")
        self.assertFalse(self.alive())

    def test_failed_reading_releases_the_pidfile(self):
        def broken_synth(text, out):
            raise OSError("network down")

        with mock.patch.object(speak.signal, "signal"):
            with self.assertRaises(OSError):
                speak.play_chunked("Hello there.", dict(speak.DEFAULTS), broken_synth)
        self.assertFalse(speak.PIDFILE.exists())


# ----------------------------------------------------------------- playback


class ChunkedPlayback(Scratch):
    """play_chunked with a fake synth (writes the chunk's text into the file)
    and a fake player (records what the file held when it was played)."""

    def play(self, text, synth, own_langs=None, size=5):
        played = []

        def fake_run(cmds):
            for cmd, _ in cmds:
                played.append(pathlib.Path(cmd[1]).read_text(encoding="utf-8")
                              if cmd[0] == "play" else f"system:{cmd[1]}")

        cfg = dict(speak.DEFAULTS, first_chars=size, chunk_chars=size)
        with mock.patch.object(speak, "player_cmd", lambda p: (["play", str(p)], None)), \
                mock.patch.object(speak, "run_players", fake_run), \
                mock.patch.object(speak, "system_speech",
                                  lambda runs, c: [(["say", runs[0][1]], None)]), \
                mock.patch.object(speak.signal, "signal"):
            speak.play_chunked(text, cfg, synth, own_langs)
        return played

    def test_failed_chunk_is_retried_not_replaced_by_stale_audio(self):
        failures = {"Two."}

        def synth(text, out):
            if text in failures:
                failures.discard(text)
                raise OSError("network blip")
            out.write_text(text, encoding="utf-8")

        self.assertEqual(self.play("One. Two. Six.", synth), ["One.", "Two.", "Six."])
        self.assertIn("retrying", (speak.log_path()).read_text(encoding="utf-8"))

    def test_a_chunk_that_keeps_failing_ends_the_reading(self):
        def synth(text, out):
            if text == "Two.":
                raise OSError("down")
            out.write_text(text, encoding="utf-8")

        with self.assertRaises(OSError):
            self.play("One. Two. Six.", synth)

    def test_kokoro_hands_russian_to_the_system_voice(self):
        def synth(text, out):
            out.write_text(text, encoding="utf-8")

        played = self.play("Hello. Привет, как дела? Bye.", synth, own_langs={"en"}, size=20)
        self.assertEqual(played, ["Hello.", "system:Привет, как дела?", "Bye."])

    def test_long_sentence_with_a_small_chunk_size_terminates(self):
        self.assertEqual(speak.chunk_text("Supercalifragilistic.", 5, 5),
                         ["Super", "calif", "ragil", "istic", "."])


# ------------------------------------------------------- text and languages


class SpokenForm(unittest.TestCase):
    def spoken(self, md):
        return speak.spoken_form(md, 12000)

    def test_tables_are_read_row_by_row(self):
        md = "| Claim | Verdict |\n|---|:---:|\n| Supports Russian | ✅ True |"
        self.assertEqual(self.spoken(md), "Claim, Verdict. Supports Russian, True.")

    def test_identifiers_and_symbols_survive(self):
        s = self.spoken("Call `speak_direct()` on snake_case_name. Use C# or 2*3 > 5.")
        self.assertEqual(s, "Call speak direct on snake case name. Use C# or 2*3 > 5.")

    def test_emphasis_is_unwrapped(self):
        self.assertEqual(self.spoken("A *really* **bold** _move_."), "A really bold move.")

    def test_lines_get_pauses(self):
        s = self.spoken("## Summary\n- First item\n- Second item\n1. Third")
        self.assertEqual(s, "Summary. First item. Second item. 1. Third.")

    def test_emoji_and_arrows(self):
        self.assertEqual(self.spoken("🔊 Done → next ✅"), "Done — next.")

    def test_open_code_fence_is_not_read(self):
        s = self.spoken("Here:\n```python\nprint('secret')\n")
        self.assertNotIn("secret", s)
        self.assertIn("code omitted", s)

    def test_russian_reply_gets_russian_phrases(self):
        s = self.spoken("Вот код:\n```\nx = 1\n```\nСмотри https://example.com/x тут.")
        self.assertEqual(s, "Вот код: код пропущен. Смотри ссылка тут.")


class Languages(unittest.TestCase):
    def test_text_lang(self):
        cases = {
            "The fix is ready.": "en",
            "Готово!": "ru",
            "Запусти npm install и затем npm run build.": "ru",
            "Исправление в speak direct, см. файл config.json.": "ru",
            "The word Привет means hello.": "en",
            "2.": None,
        }
        for text, lang in cases.items():
            self.assertEqual(speak.text_lang(text), lang, text)

    def test_segments(self):
        text = "Итог. Смотри см. файл. The fix is ready, e.g. now. 2. Готово!"
        self.assertEqual(speak.segments(text), [
            ("ru", "Итог. Смотри см. файл."),
            ("en", "The fix is ready, e.g. now. 2."),
            ("ru", "Готово!"),
        ])

    @unittest.skipUnless({"ru", "en"} <= set(installed_windows_voices().values()),
                         "needs a Russian and an English Windows voice")
    def test_each_language_gets_a_voice_of_that_language(self):
        voices = installed_windows_voices()
        chosen = dry_run([("ru", "Привет."), ("en", "Hello."), ("ru", "Пока.")],
                         dict(speak.DEFAULTS))
        self.assertEqual([voices[name] for name, _ in chosen], ["ru", "en", "ru"])

    @unittest.skipUnless(os.name == "nt", "needs Windows PowerShell")
    def test_configured_voice_per_language_wins(self):
        en = [n for n, lang in installed_windows_voices().items() if lang == "en"]
        if len(en) < 2:
            self.skipTest("needs two English voices")
        cfg = dict(speak.DEFAULTS, voices={"en": en[-1]})
        self.assertEqual(dry_run([("en", "Hello.")], cfg)[0][0], en[-1])


# ------------------------------------------------------------ transcripts


def transcript(*entries) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)


def said(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class Transcripts(Scratch):
    def test_project_dir_name(self):
        with mock.patch.object(speak, "IS_WIN", True):
            for path in (r"C:\Users\me\app", "/c/Users/me/app", "C:/Users/me/app"):
                self.assertEqual(speak.project_dir_name(path), "C--Users-me-app", path)
        with mock.patch.object(speak, "IS_WIN", False):
            self.assertEqual(speak.project_dir_name("/home/me/app"), "-home-me-app")

    def test_session_transcript_wins_over_newer_ones(self):
        mine = speak.PROJECTS / "C--x" / "session-1.jsonl"
        other = speak.PROJECTS / "C--y" / "session-2.jsonl"
        for f in (mine, other):
            f.parent.mkdir(parents=True)
            f.write_text("")
        os.utime(mine, (1, 1))
        self.assertEqual(speak.find_transcript("session-1"), mine)
        self.assertEqual(speak.find_transcript(""), other)

    def test_speak_reads_the_reply_before_its_own_turn(self):
        f = self.tmp / "t.jsonl"
        f.write_text(transcript(
            {"type": "user", "message": {"content": "question"}},
            said("The answer."),
            {"type": "user", "message": {"content":
                "<command-message>read-aloud:speak</command-message>"
                "<command-name>/read-aloud:speak</command-name>"}},
            {"type": "user", "isMeta": True, "message": {"content": [
                {"type": "text", "text": "Run exactly this one bash command"}]}},
            said("Let me run that."),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
            said("🔊 reading aloud"),
        ), encoding="utf-8")
        self.assertEqual(speak.last_reply(f, previous=True), "The answer.")
        self.assertEqual(speak.last_reply(f), "🔊 reading aloud")


# -------------------------------------------------------------------- hooks


class Hooks(Scratch):
    def hook(self, event, auto_read=True):
        launched = []
        cfg = dict(speak.DEFAULTS, auto_read=auto_read)
        with mock.patch.object(speak, "detach", lambda argv: launched.append(argv)), \
                mock.patch.object(speak, "load_config", lambda: cfg), \
                mock.patch.object(speak.sys, "stdin", io.StringIO(json.dumps(event))):
            self.assertEqual(speak.hook_mode(), 0)
        return launched

    def test_stop_reads_the_reply_from_the_hook_input(self):
        launched = self.hook({"hook_event_name": "Stop", "session_id": "s1",
                              "transcript_path": "stale.jsonl",
                              "last_assistant_message": "Fresh reply."})
        argv, = launched
        self.assertEqual(argv[0], "--text-file")
        self.assertEqual(pathlib.Path(argv[1]).read_text(encoding="utf-8"), "Fresh reply.")
        self.assertEqual(argv[2:], ["--session", "s1"])
        pathlib.Path(argv[1]).unlink()

    def test_auto_read_off_reads_nothing(self):
        self.assertEqual(self.hook({"hook_event_name": "Stop", "session_id": "s1",
                                    "last_assistant_message": "x"}, auto_read=False), [])

    def test_own_command_turn_is_not_read_aloud(self):
        stop_event = {"hook_event_name": "Stop", "session_id": "s2",
                      "last_assistant_message": "🔇 stopped"}
        speak.mark_own_turn("s2")
        self.assertEqual(self.hook(stop_event), [])
        launched = self.hook(dict(stop_event, last_assistant_message="A real reply."))
        self.assertEqual(len(launched), 1)          # the note was used up
        pathlib.Path(launched[0][1]).unlink()

    def test_new_prompt_stops_its_sessions_reading(self):
        calls = []
        with mock.patch.object(speak, "stop", lambda only_session=None: calls.append(only_session)):
            for prompt in ("next question", "/read-aloud:speak", "/speak-stop", "/voice ru"):
                self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s3",
                           "prompt": prompt})
        self.assertEqual(calls, ["s3"])

    def test_garbage_input_never_fails(self):
        with mock.patch.object(speak.sys, "stdin", io.StringIO("not json")):
            self.assertEqual(speak.hook_mode(), 0)


# ------------------------------------------------------------ end to end


class CommandLine(unittest.TestCase):
    def run_script(self, *args):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, LOCALAPPDATA=home, APPDATA=home, XDG_DATA_HOME=home,
                       XDG_CONFIG_HOME=home)
            return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                                  capture_output=True, timeout=60)

    def test_print_with_emoji_does_not_crash(self):
        out = self.run_script("--print", "--text", "🔊 Готово! Done.")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Готово", out.stdout.decode("utf-8"))

    def test_detach_reports_a_reader_that_fails(self):
        out = self.run_script("--detach", "--transcript", "does-not-exist.jsonl")
        self.assertEqual(out.returncode, 1)
        self.assertIn("error: no transcript found", out.stdout.decode("utf-8"))


class KokoroDownload(unittest.TestCase):
    def test_sha256_of(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "f"
            p.write_bytes(b"abc")
            self.assertEqual(
                speak.sha256_of(p),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_every_download_has_a_checksum(self):
        self.assertEqual(set(speak.KOKORO_URLS), set(speak.KOKORO_SHA256))
        self.assertTrue(all("==" in p for p in speak.KOKORO_PACKAGES))


if __name__ == "__main__":
    unittest.main()
