"""What PatchBay started gets stopped when it quits (and only that). Run: python3 -m unittest discover -s tests -t ."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ollamactl  # noqa: E402
import sandbox  # noqa: E402
import services  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(services, "FILE", Path(self.tmp.name) / "started.json")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        services._DONE.clear()
        self.addCleanup(services._DONE.clear)


class ListTest(Base):
    def test_record_and_forget(self):
        services.record("colima", "default", "Colima")
        services.record("sandbox", "patchbay-sb-a", "sandbox a")
        self.assertTrue(services.has("colima"))
        services.forget("sandbox", "patchbay-sb-a")
        self.assertEqual([r["kind"] for r in services.started()], ["colima"])
        self.assertEqual(services.started()[0]["owner"], os.getpid())


class QuitTest(Base):
    def test_stops_only_what_patchbay_started(self):
        services.record("sandbox", "patchbay-sb-a", "sandbox a")
        services.record("colima", "default", "Colima")
        with mock.patch.object(sandbox, "stop_container_named") as stop_c, \
                mock.patch.object(sandbox, "stop_engine", return_value=True) as stop_e:
            line = services.on_quit({}, ollama_close=lambda cfg: "Ollama is stopped.")
        stop_c.assert_called_once_with("patchbay-sb-a")
        stop_e.assert_called_once_with("colima")
        self.assertIn("Ollama", line)
        self.assertIn("sandbox a", line)
        self.assertEqual(services.started(), [])

    def test_nothing_started_means_nothing_stopped(self):
        with mock.patch.object(sandbox, "stop_engine") as stop_e, \
                mock.patch.object(sandbox, "stop_container_named") as stop_c:
            services.on_quit({})
        self.assertFalse(stop_e.called or stop_c.called)

    def test_switch_off_leaves_it_running_and_hands_it_over(self):
        services.record("colima", "default", "Colima")
        with mock.patch.object(sandbox, "stop_engine") as stop_e:
            services.on_quit({"quit_stops_linux_machine": False})
        self.assertFalse(stop_e.called)
        self.assertEqual(services.started(), [])   # so the next start doesn't stop it either

    def test_running_build_is_stopped(self):
        p = subprocess.Popen(["sleep", "30"], start_new_session=True)
        with services.track(p, "a build"):
            services.on_quit({})
            self.assertIsNotNone(p.wait(5))
        self.assertEqual(services.started(), [])


class SweepTest(Base):
    def test_crash_leftovers_are_stopped(self):
        services.record("sandbox", "patchbay-sb-a", "sandbox a")
        rows = services._read()
        rows[0]["owner"] = 999999      # a PatchBay that isn't running any more
        services._write(rows)
        with mock.patch.object(sandbox, "stop_container_named") as stop_c:
            self.assertEqual(services.sweep({}), ["sandbox a"])
        stop_c.assert_called_once()

    def test_restart_after_update_keeps_things(self):
        services.record("sandbox", "patchbay-sb-a", "sandbox a")
        with mock.patch.object(sandbox, "stop_container_named") as stop_c:
            services.sweep({})
        self.assertFalse(stop_c.called)

    def test_reused_pid_is_not_killed(self):
        services._write([{"kind": "process", "key": str(os.getpid()), "what": "old build", "owner": 999999,
                          "cmd": "make -j8 something-else", "pgid": os.getpid()}])
        with mock.patch.object(services, "_kill_group") as kill:
            services.sweep({})
        self.assertFalse(kill.called)

    def test_ollama_is_adopted_not_stopped(self):
        services._write([{"kind": "ollama", "key": "ollama", "what": "Ollama", "owner": 999999}])
        seen = []
        services.sweep({}, ollama_adopt=lambda cfg, row: seen.append(row["kind"]))
        self.assertEqual(seen, ["ollama"])


class OllamaCloseTest(Base):
    def close(self, mode, started, ours=False):
        if ours:
            services.record("ollama", "ollama", "Ollama")
        cfg = {"ollama_url": "http://localhost:11434", "ollama_on_close": mode}
        with mock.patch.object(ollamactl, "here", return_value=True), \
                mock.patch.object(ollamactl, "stop_all", return_value="Ollama is stopped.") as stop, \
                mock.patch.object(ollamactl, "let_go", return_value=["m"]) as let_go, \
                mock.patch.dict(ollamactl.STATE, {"started_by_us": started}):
            ollamactl.on_close(cfg)
        return stop.called, let_go.call_args

    def test_unloads_right_away_when_ollama_was_yours(self):
        stopped, call = self.close("started", False)
        self.assertFalse(stopped)
        self.assertEqual(call, mock.call(mock.ANY, unload=True))

    def test_quits_ollama_patchbay_started_even_after_restart(self):
        self.assertTrue(self.close("started", False, ours=True)[0])

    def test_never_keeps_the_5_minute_timer(self):
        self.assertEqual(self.close("never", True)[1], mock.call(mock.ANY, unload=False))


class StartupModelTest(unittest.TestCase):
    def test_default_and_off(self):
        self.assertEqual(ollamactl.startup_model({}), "qwen36-uncensored-fixed")
        self.assertEqual(ollamactl.startup_model({"ollama_startup_model": ""}), "")

    def test_latest_is_the_same_model(self):
        self.assertTrue(ollamactl.same_model("qwen36-uncensored-fixed:latest", "qwen36-uncensored-fixed"))
        self.assertFalse(ollamactl.same_model("qwen3:8b", "qwen3"))

    def test_missing_model_says_so(self):
        cfg = {"ollama_url": "http://localhost:11434"}
        with mock.patch.object(ollamactl, "installed", return_value=["llama3:latest"]), \
                mock.patch.dict(ollamactl.STATE, {"error": "", "loading": ""}):
            msg = ollamactl.load_model(cfg, "qwen36-uncensored-fixed")
        self.assertIn("isn't one of your models", msg)


if __name__ == "__main__":
    unittest.main()
