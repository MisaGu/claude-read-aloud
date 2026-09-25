"""Regression tests for speak.py's security fixes. Standard library only:

    python -m unittest discover -s tests
"""
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("speak", ROOT / "scripts" / "speak.py")
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


def captured_cmd(text: str, cfg: dict) -> tuple[list, dict]:
    """The command speak_direct would run, without running it."""
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw.get("env")

        def wait(self):
            return 0

    with mock.patch.object(speak.subprocess, "Popen", FakePopen), \
            mock.patch.object(speak, "claim"), mock.patch.object(speak, "release"):
        speak.speak_direct(text, cfg)
    return seen["cmd"], seen["env"]


class PowerShellQuoting(unittest.TestCase):
    def test_text_never_enters_the_script(self):
        with mock.patch.object(speak, "IS_WIN", True), mock.patch.object(speak, "IS_MAC", False):
            for text in HOSTILE:
                cmd, env = captured_cmd(text, dict(speak.DEFAULTS, voice="x'); evil; ('"))
                self.assertNotIn(text, " ".join(cmd))
                self.assertNotIn("evil", " ".join(cmd))
                self.assertEqual(env["CRA_TEXT"], text)

    @unittest.skipUnless(os.name == "nt", "needs Windows PowerShell")
    def test_powershell_receives_text_verbatim(self):
        for text in HOSTILE:
            cmd, env = speak.powershell(
                "[Console]::OutputEncoding = [Text.Encoding]::UTF8;"
                "[Console]::Out.Write($env:CRA_TEXT)", text=text)
            out = subprocess.run(cmd, env=env, capture_output=True).stdout.decode("utf-8")
            self.assertEqual(out, text)


class OptionLikeReplies(unittest.TestCase):
    def test_reply_starting_with_dash_stays_an_operand(self):
        text = "-o /tmp/owned.aiff hello"
        with mock.patch.object(speak, "IS_MAC", True), mock.patch.object(speak, "IS_WIN", False):
            cmd, _ = captured_cmd(text, dict(speak.DEFAULTS))
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


class Pidfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pidfile = mock.patch.object(
            speak, "PIDFILE", pathlib.Path(self.tmp.name) / "reader.pid")
        self.pidfile.start()
        # A stand-in for "some other program" that ended up with a stale pid.
        self.bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.bystander.wait)
        self.addCleanup(self.bystander.kill)
        self.addCleanup(self.pidfile.stop)
        self.addCleanup(self.tmp.cleanup)

    def alive(self) -> bool:
        time.sleep(0.5)
        return self.bystander.poll() is None

    def test_start_time_is_stable_and_distinct(self):
        pid = self.bystander.pid
        self.assertIsNotNone(speak.process_start(pid))
        self.assertEqual(speak.process_start(pid), speak.process_start(pid))
        self.assertNotEqual(speak.process_start(pid), speak.process_start(os.getpid()))

    def test_stale_record_does_not_kill_a_reused_pid(self):
        speak.PIDFILE.write_text(f"{self.bystander.pid} 12345")
        speak.stop()
        self.assertTrue(self.alive())
        self.assertFalse(speak.PIDFILE.exists())

    def test_old_format_record_is_not_trusted(self):
        speak.PIDFILE.write_text(str(self.bystander.pid))
        speak.stop()
        self.assertTrue(self.alive())

    def test_matching_record_is_stopped(self):
        start = speak.process_start(self.bystander.pid)
        speak.PIDFILE.write_text(f"{self.bystander.pid} {start}")
        speak.stop()
        self.assertFalse(self.alive())

    def test_failed_reading_releases_the_pidfile(self):
        def broken_synth(text, out):
            raise OSError("network down")

        with mock.patch.object(speak.signal, "signal"):
            with self.assertRaises(OSError):
                speak.play_chunked("Hello there.", dict(speak.DEFAULTS), broken_synth)
        self.assertFalse(speak.PIDFILE.exists())


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
