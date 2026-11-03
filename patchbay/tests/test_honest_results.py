"""
Tests for Joshua's EglantinePS5 run, where the AI said "I have installed EglantinePS5 onto your PS5. The build
completed successfully ... copied to /user/EglantinePS5.self" after hitting the step limit with the build still
failing, and nothing built or sent anywhere. Also: it picked /user on the PS5 without looking up where the app goes.
- "make 2>&1 | tail" in a sandbox fails when make fails (pipefail), and "|| echo" can't hide a failed build either.
- Claims of built / copied / installed that PatchBay didn't see happen are taken out of the AI's words.
- A step-limit or loop-guard stop starts with "Not finished", the last failed step, and what didn't happen.
- Install locations need a source (a project file or a page that was read, which mentions the folder), shown on
  the card; a Mac command that copies to a named device folder needs one too.
- FFmpeg's "C compiler test failed" reads ffbuild/config.log and installs what it names in one go.
Nothing here contacts a real device.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import json
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import proof  # noqa: E402
import sandbox  # noqa: E402
import server  # noqa: E402
from tests.test_safety import FakeChat, device_cfg  # noqa: E402
from tests.test_mac_vs_device_paths import NoDevice  # noqa: E402

FALSE_ANSWER = ("I have installed EglantinePS5 onto your PS5. The build completed successfully in the Linux sandbox, "
                "and the file was copied to `/user/EglantinePS5.self` on your console (192.168.50.203). You can now "
                "launch it from your Homebrew Menu.")


class Chat(FakeChat):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.timing, self.chat_id = [], ""


class ClaimCheck(unittest.TestCase):
    def setUp(self):
        self.st = Chat({"device_name": "Ps5"})
        proof.question_started(self.st)

    def test_the_false_answer_is_taken_out(self):
        fixed, kinds = proof.correct(FALSE_ANSWER, self.st)
        self.assertEqual(kinds, ["built", "installed"])
        self.assertNotIn("/user/EglantinePS5.self", fixed)
        self.assertNotIn("You can now launch", fixed)
        self.assertIn("Correction from PatchBay", fixed)
        self.assertIn("nothing was installed or copied to your device", fixed)

    def test_plans_questions_and_failures_are_not_claims(self):
        for text in ("Once it builds, I'll copy it to your PS5.", "Should I install it on the PS5?",
                     "The build failed: C compiler test failed.", "I didn't copy anything to the console.",
                     "I installed clang and binutils in the sandbox.", "Next: copy it to /data/homebrew.",
                     "I installed make, git and rsync."):
            self.assertEqual(proof.correct(text, self.st), (text, []), text)

    def test_claims_with_proof_stay(self):
        proof.record(self.st, "built", "sandbox EglantinePS5: dist/EglantinePS5.self")
        proof.record(self.st, "installed", "/data/homebrew/EglantinePS5/eboot.bin on Ps5")
        text = "The build completed. I copied it to your PS5."
        self.assertEqual(proof.correct(text, self.st), (text, []))

    def test_old_proof_only_counts_for_the_same_thing(self):
        self.st.proof = [{"kind": "built", "what": "project ftpd: ftpd.nro", "time": time.time() - 600}]
        self.assertEqual(proof.correct("ftpd built successfully.", self.st)[1], [])
        self.assertEqual(proof.correct("EglantinePS5 built successfully.", self.st)[1], ["built"])

    def test_dotted_names_and_addresses_do_not_split_sentences(self):
        self.assertEqual(proof.claims("I copied ftpd.nro to /switch/ftpd.nro."), ["installed"])
        self.assertEqual(proof.claims("Sent it to 192.168.50.203."), ["installed"])


def step_call(name="sandbox_run", **args):
    return {"tool_calls": [{"function": {"name": name, "arguments": args or {"command": "make package"}}}]}


class ChatLoopStops(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        tmp = Path(self._t.name)
        (tmp / "dev").mkdir()
        self.cfg = device_cfg(tmp / "dev", tmp / "data")
        self.st = Chat(self.cfg)
        self.events = []

    def tearDown(self):
        self._t.cleanup()

    def run_loop(self, answers, handlers):
        with mock.patch.object(server, "ask_model", side_effect=answers), \
                mock.patch.object(server, "second_opinion", return_value=""), \
                mock.patch.object(server, "claude_ready", return_value=False):
            server._chat_loop(self.st, "install eglantineps5 onto my ps5", self.events.append, self.cfg, "system",
                              [{"type": "function", "function": {"name": n}} for n in handlers], handlers)

    def test_step_limit_says_not_finished_and_what_failed(self):
        n = iter(range(1000))

        def handler(st, emit, a):
            i = next(n)
            if i == 3:
                return ("In the Linux sandbox EglantinePS5, make package failed (exit code 1). First error: C compiler "
                        "test failed.")
            return "Step %d: installed package number %d of many, all different %s" % (i, i, "x" * i)

        def ask(cfg, messages, tools=None, emit=None, model=None):
            return {"content": FALSE_ANSWER} if tools == [] else step_call(command="step %d" % len(messages))
        self.run_loop(ask, {"sandbox_run": handler})
        answer = [e for e in self.events if e["type"] == "answer"][-1]["text"]
        self.assertTrue(answer.startswith("**Not finished.**"), answer)
        self.assertIn("I stopped after", answer)
        self.assertIn("C compiler test failed", answer)
        self.assertIn("nothing was installed or copied to your device", answer.lower())
        self.assertNotIn("/user/EglantinePS5.self", answer)
        self.assertNotIn("You can now launch", answer)
        self.assertNotIn("/user/EglantinePS5.self", self.st.messages[-1]["content"])

    def test_final_answer_claim_is_corrected_and_remembered_corrected(self):
        self.run_loop([{"content": FALSE_ANSWER}], {})
        answer = [e for e in self.events if e["type"] == "answer"][-1]["text"]
        self.assertIn("Correction from PatchBay", answer)
        self.assertNotIn("installed EglantinePS5 onto your PS5", answer)
        self.assertEqual(self.st.messages[-1]["content"], answer)

    def test_true_claim_after_a_real_install_stays(self):
        def install(st, emit, a):
            proof.record(st, "installed", "/data/homebrew/EglantinePS5/eboot.bin on Dev")
            return "Done: change #4. /data/homebrew/EglantinePS5/eboot.bin on Dev was written."
        self.run_loop([step_call("fake_install", x=1), {"content": "I copied it to your Dev device."}],
                      {"fake_install": install})
        answer = [e for e in self.events if e["type"] == "answer"][-1]["text"]
        self.assertEqual(answer, "I copied it to your Dev device.")

    def test_loop_guard_stop_says_not_finished_and_last_failure(self):
        self.run_loop([step_call()] * 5, {"sandbox_run": lambda st, emit, a: "Error: the build broke: ranlib missing"})
        stop = next(e for e in self.events if e["type"] == "choices")
        self.assertTrue(stop["text"].startswith("**Not finished.**"))
        self.assertIn("The last step that failed", stop["text"])
        self.assertIn("ranlib missing", stop["text"])


class HiddenFailures(unittest.TestCase):
    def test_or_echo_cannot_hide_a_failed_make(self):
        out = "CC libavutil/x.o\nmake: *** [Makefile:40: package] Error 1\n"
        self.assertIn("Error 1", server.hidden_failure("make package 2>&1 | tail -80 || echo done", 0, out))
        self.assertIn("C compiler test failed", server.hidden_failure(
            "bash tools/build-package.sh release || true", 0, "C compiler test failed.\n"))

    def test_reading_a_log_is_not_a_failure(self):
        self.assertEqual(server.hidden_failure("cat ffbuild/config.log | tail -40", 0, "C compiler test failed"), "")
        self.assertEqual(server.hidden_failure("make", 0, "all good\n"), "")
        self.assertEqual(server.hidden_failure("make", 2, "make: *** Error 2"), "")   # already a failure

    @unittest.skipUnless(shutil.which("dash") and shutil.which("bash"), "needs dash and bash")
    def test_sandbox_commands_run_with_pipefail(self):
        r = subprocess.run(["dash", "-lc", sandbox.PIPEFAIL_SH, "sh", "false | tail -n 1"], capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        r = subprocess.run(["dash", "-lc", sandbox.PIPEFAIL_SH, "sh", "echo hi | tail -n 1"], capture_output=True,
                           text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "hi"))

    def test_sandbox_run_marks_a_hidden_failure_as_failed(self):
        st = Chat({"device_name": "Ps5"})
        s = {"name": "Eg", "kind": "linux", "network": True, "output": "", "memory_gb": 8}
        with mock.patch.object(server, "sandbox_name", return_value="Eg"), \
                mock.patch.object(sandbox, "load", return_value=s), \
                mock.patch.object(sandbox, "preflight", return_value={"brew": [], "colima": None, "stuck": ""}), \
                mock.patch.object(sandbox, "run", return_value=(0, "make: *** [Makefile:9: all] Error 1\n")), \
                mock.patch.object(server, "sandbox_hints", return_value=""):
            out = server.t_sandbox_run(st, lambda e: None, {"command": "make 2>&1 | tail -5 || echo"})
        self.assertTrue(server.failed(out), out)
        self.assertIn("hid it", out)
        self.assertEqual(proof.seen(st, "built"), [])


class InstallLocation(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        tmp = Path(self._t.name).resolve()
        self.proj = tmp / "EglantinePS5"
        (self.proj / "dist").mkdir(parents=True)
        (self.proj / "README.md").write_text("Install: copy the app folder to /data/homebrew/ on the PS5.\n")
        (self.proj / "NOTES.md").write_text("Nothing about where it goes.\n")
        (self.proj / "dist" / "EglantinePS5.self").write_bytes(b"\x7fELF" + b"\0" * 60)
        self.cfg = {"device_name": "Ps5", "protocol": "ftp", "host": "192.0.2.1", "ports": [2121],
                    "timeout_seconds": 1, "data_folder": str(tmp / "data"), "allow_writes": True, "projects": []}
        self.config = tmp / "config.json"
        self.config.write_text(json.dumps(self.cfg))
        self.st = Chat(self.cfg)
        self.st.config_path = str(self.config)
        self.events = []
        self.patches = [mock.patch.object(server, "access_cfg", lambda: {"data_folder": str(tmp / "data")}),
                        mock.patch.object(server.core, "Device", NoDevice),
                        mock.patch.object(server, "linux_sandboxes_for", return_value=[]),
                        mock.patch.object(server, "checked_build", side_effect=lambda st, cfg, pr, emit, **k: pr)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._t.cleanup()

    def set_project(self, **more):
        a = dict({"name": "EglantinePS5", "folder": str(self.proj), "build": "make",
                  "output": "dist/EglantinePS5.self"}, **more)
        out = server.HANDLERS["set_project"](self.st, self.events.append, a)
        self.cfg["projects"] = json.loads(self.config.read_text())["projects"]
        return out

    def test_a_guessed_location_is_refused(self):
        with self.assertRaises(server.ToolError) as e:
            self.set_project(device_path="/user/EglantinePS5.self")
        self.assertIn("Don't guess", str(e.exception))
        self.assertIn("web_search", str(e.exception))
        with self.assertRaises(server.ToolError) as e:       # a source that doesn't say so
            self.set_project(device_path="/user/EglantinePS5.self", device_path_source="NOTES.md")
        self.assertIn("doesn't mention", str(e.exception))
        with self.assertRaises(server.ToolError):             # the README says /data/homebrew, not /user
            self.set_project(device_path="/user/EglantinePS5.self", device_path_source="README.md")

    def test_a_project_file_that_says_so_is_a_source(self):
        out = self.set_project(device_path="/data/homebrew/EglantinePS5/eboot.bin", device_path_source="README.md")
        self.assertIn("(from README.md)", out)
        self.set_project(device_path="/data/homebrew/EglantinePS5/eboot.bin")     # same place: source kept
        self.assertEqual(self.cfg["projects"][0]["device_path_source"], "README.md")

    def test_a_web_page_must_be_read_first(self):
        url = "https://example.org/ps5-homebrew-install"
        with self.assertRaises(server.ToolError) as e:
            self.set_project(device_path="/data/homebrew/EglantinePS5/eboot.bin", device_path_source=url)
        self.assertIn("read_web_page", str(e.exception))
        server.note_pages(self.st, "Homebrew apps live in /data/homebrew/<name>/ with an eboot.bin", url)
        self.set_project(device_path="/data/homebrew/EglantinePS5/eboot.bin", device_path_source=url)
        self.assertEqual(self.cfg["projects"][0]["device_path_source"], url)

    def test_the_device_is_a_source_only_when_the_file_was_seen_there(self):
        with self.assertRaises(server.ToolError):
            self.set_project(device_path="/data/homebrew/EglantinePS5/eboot.bin", device_path_source="device")
        self.st.messages.append({"role": "tool", "content": "Found /data/homebrew/EglantinePS5/eboot.bin (2 MB)"})
        self.set_project(device_path="/data/homebrew/EglantinePS5/eboot.bin", device_path_source="device")

    def test_install_needs_a_source_and_shows_it_on_the_card(self):
        self.cfg["projects"] = [{"name": "EglantinePS5", "folder": str(self.proj), "build": "make",
                                 "output": "dist/EglantinePS5.self", "device_path": "/user/EglantinePS5.self"}]
        with self.assertRaises(server.ToolError) as e:
            server.HANDLERS["propose_install"](self.st, self.events.append, {"project": "EglantinePS5"})
        self.assertIn("Don't guess", str(e.exception))
        self.assertEqual(self.st.pending, {})
        self.set_project(device_path="/data/homebrew/EglantinePS5/eboot.bin", device_path_source="README.md")
        with mock.patch.object(server.core, "check_bytes", return_value=[]):
            server.HANDLERS["propose_install"](self.st, self.events.append, {"project": "EglantinePS5"})
        card = next(e for e in self.events if e.get("type") == "confirm")
        self.assertIn("Install location: /data/homebrew/EglantinePS5/eboot.bin", card["diff"])
        self.assertIn("Where that comes from: README.md", card["diff"])

    def test_mac_command_copying_to_a_device_folder_needs_a_source(self):
        self.set_project()
        cmd = "curl -T dist/EglantinePS5.self ftp://192.0.2.1:2121/user/EglantinePS5.self"
        with self.assertRaises(server.ToolError) as e:
            server.HANDLERS["propose_command"](self.st, self.events.append, {"project": "EglantinePS5",
                                                                            "command": cmd})
        self.assertIn("/user/EglantinePS5.self", str(e.exception))
        self.assertEqual(self.st.pending, {})
        self.assertEqual(server.remote_folder_in("make install"), "")


class FfmpegConfigLog(unittest.TestCase):
    def test_reads_config_log_and_adds_every_missing_program(self):
        s = {"name": "Eg", "kind": "linux", "network": True, "memory_gb": 8, "setup_command": "apt-get install -y make"}
        log = ("== /work/ffmpeg/ffbuild/config.log\nsh: 1: ranlib: not found\nsh: 1: nasm: not found\n"
               "C compiler test failed.")
        saved = {}
        with mock.patch.object(sandbox, "run", return_value=(0, log)) as run, \
                mock.patch.object(sandbox, "set_recipe", side_effect=lambda n, r: saved.update(r)), \
                mock.patch.object(server, "script_needs", return_value=([], "")):
            tips = server.sandbox_hints(s, 1, "C compiler test failed.\nIf you think configure made a mistake",
                                        "build")
        self.assertIn("ffbuild/config.log", run.call_args[0][1])
        self.assertIn("ffbuild/config.log", tips)
        self.assertIn("binutils", saved.get("setup_command", ""))
        self.assertIn("nasm", saved.get("setup_command", ""))


if __name__ == "__main__":
    unittest.main()
