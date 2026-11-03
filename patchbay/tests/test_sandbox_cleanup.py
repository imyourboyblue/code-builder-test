"""
Tests for Reset really emptying a sandbox, and for Clean up only touching PatchBay's own Docker things.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sandbox  # noqa: E402


class FakeDocker:
    """Records every docker command and answers the ones clean-up and reset ask."""

    def __init__(self, ps="", images="", dangling="", exists=False, top=""):
        self.calls, self.ps, self.images, self.dangling, self.exists, self.top = [], ps, images, dangling, exists, top

    def __call__(self, argv, timeout=60):
        self.calls.append(argv)
        a = argv[1:]
        if a[:1] == ["info"]:
            return 0, "ok"
        if a[:1] == ["ps"]:
            return 0, self.ps
        if a[:1] == ["images"] and "dangling=true" in a:
            return 0, self.dangling
        if a[:1] == ["images"]:
            return 0, self.images
        if a[:2] == ["container", "inspect"]:
            return (0, "[{}]") if self.exists else (1, "")
        if a[:1] == ["top"]:
            return 0, self.top
        return 0, ""

    def names(self):
        return [" ".join(c) for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.proj = base / "proj"
        self.proj.mkdir()
        (self.proj / "Makefile").write_text("all:\n\techo hi\n")
        self.root = mock.patch.object(sandbox, "ROOT", base / "sandboxes")
        self.root.start()
        self.eng = mock.patch.object(sandbox, "engine", return_value=("docker", "docker"))
        self.eng.start()
        self.col = mock.patch.object(sandbox, "_which", lambda exe: None if exe == "colima" else "/usr/bin/" + exe)
        self.col.start()

    def tearDown(self):
        self.col.stop()
        self.eng.stop()
        self.root.stop()
        self.tmp.cleanup()


class ResetEmptiesForReal(Base):
    def test_reset_kills_and_removes_container_with_volumes_and_old_saves(self):
        sandbox.create("proj", str(self.proj))
        fake = FakeDocker(dangling="sha256:aaa\n")
        with mock.patch.object(sandbox, "_quiet", fake):
            sandbox.reset("proj")
        names = fake.names()
        self.assertIn("docker kill patchbay-sb-proj", names)
        self.assertIn("docker rm -f -v patchbay-sb-proj", names)
        self.assertIn("docker image rm patchbay-sb-proj:saved", names)
        self.assertIn("docker image rm sha256:aaa", names)
        dangling = [c for c in fake.calls if "dangling=true" in c][0]
        self.assertIn("label=patchbay.sandbox=proj", dangling)        # only this sandbox's old saves

    def test_reset_says_so_when_the_container_will_not_go(self):
        sandbox.create("proj", str(self.proj))
        with mock.patch.object(sandbox, "_quiet", FakeDocker(exists=True)):
            with self.assertRaises(sandbox.SandboxError):
                sandbox.reset("proj")

    def test_reset_stops_a_running_command_first(self):
        sandbox.create("proj", str(self.proj))
        ev = sandbox._begin_run("proj")
        ended = []

        def build():          # a build that only ends when it's told to
            ev[0].wait(10)
            ended.append(time.time())
            sandbox._end_run("proj", ev)
        t = threading.Thread(target=build)
        t.start()
        self.assertTrue(sandbox.busy("proj"))
        with mock.patch.object(sandbox, "_quiet", FakeDocker()):
            sandbox.reset("proj")
        t.join(5)
        self.assertTrue(ended)
        self.assertFalse(sandbox.busy("proj"))

    def test_new_containers_and_saves_carry_patchbays_label(self):
        sb = sandbox.create("proj", str(self.proj))
        argv = sandbox.create_args("docker", "docker", sb)
        self.assertEqual(argv[argv.index("--label") + 1], "patchbay.sandbox=proj")
        fake = FakeDocker(exists=True)
        ids = iter(["sha256:old", "sha256:new"])
        with mock.patch.object(sandbox, "_quiet", fake), \
                mock.patch.object(sandbox, "_image_id", lambda exe, ref: next(ids)):
            sandbox._save_and_remove("docker", "docker", sb)
        names = fake.names()
        self.assertTrue(any(n.startswith("docker commit --change LABEL patchbay.sandbox=proj") for n in names))
        self.assertIn("docker image rm sha256:old", names)           # the old save doesn't stay behind nameless


class StopReallyStops(unittest.TestCase):
    def test_cancel_ends_a_long_command(self):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.time()
        code, out = sandbox._stream(["sleep", "30"], tempfile.gettempdir(), dict(os.environ), None, 60, None,
                                    cancel=cancel)
        self.assertLess(time.time() - started, 5)
        self.assertIn("the sandbox was reset or deleted", out)

    def test_container_is_stopped_if_reading_breaks(self):
        killed = []

        def bad_emit(ev):
            raise RuntimeError("broke")
        with self.assertRaises(RuntimeError):
            sandbox._stream(["sh", "-c", "echo hi; sleep 30"], tempfile.gettempdir(), dict(os.environ), bad_emit,
                            60, None, on_kill=lambda: killed.append(1))
        self.assertEqual(killed, [1])


class CleanUpOnlyPatchBays(Base):
    def test_cleanup_leaves_other_docker_things_and_busy_sandboxes_alone(self):
        sandbox.create("proj", str(self.proj))
        sandbox.create("busy", str(self.proj))
        ps = "\n".join([
            '{"Names":"patchbay-sb-proj","State":"running"}',
            '{"Names":"patchbay-sb-busy","State":"running"}',
            '{"Names":"patchbay-sb-gone","State":"exited"}',
            '{"Names":"my-own-postgres","State":"running"}'])        # Docker's name filter is a regex; check anyway
        images = "patchbay-sb-proj:saved\npatchbay-sb-gone:saved\n"
        fake = FakeDocker(ps=ps, images=images, dangling="sha256:bbb\n")
        ev = sandbox._begin_run("busy")
        try:
            with mock.patch.object(sandbox, "_quiet", fake):
                text = sandbox.cleanup()
        finally:
            sandbox._end_run("busy", ev)
        names = fake.names()
        self.assertIn("docker stop -t 3 patchbay-sb-proj", names)
        self.assertNotIn("docker stop -t 3 patchbay-sb-busy", names)
        self.assertIn("docker rm -f -v patchbay-sb-gone", names)
        self.assertIn("docker image rm patchbay-sb-gone:saved", names)
        self.assertNotIn("docker image rm patchbay-sb-proj:saved", names)
        self.assertIn("docker image rm sha256:bbb", names)
        self.assertFalse(any("my-own-postgres" in n for n in names[1:] if not n.startswith("docker ps")))
        self.assertFalse(any("prune" in n for n in names))
        self.assertIn("Nothing else in Docker was touched", text)

    def test_usage_spots_leftovers_and_stray_builds(self):
        sandbox.create("proj", str(self.proj))
        ps = '{"Names":"patchbay-sb-proj","State":"running"}\n{"Names":"patchbay-sb-gone","State":"exited"}'
        top = "PID COMMAND\n1 sleep infinity\n88 make -j8\n90 clang -c x.c\n"
        with mock.patch.object(sandbox, "_quiet", FakeDocker(ps=ps, top=top)):
            u = sandbox.usage()
            text = sandbox.usage_text(u)
        proj = [c for c in u["containers"] if c["name"] == "patchbay-sb-proj"][0]
        self.assertEqual(len(proj["programs"]), 2)
        self.assertIn("still running", text)
        self.assertIn("deleted sandboxes", text)

    def test_stop_colima_refused_while_building(self):
        ev = sandbox._begin_run("proj")
        try:
            with self.assertRaises(sandbox.SandboxError):
                sandbox.stop_colima_now()
        finally:
            sandbox._end_run("proj", ev)


if __name__ == "__main__":
    unittest.main()
