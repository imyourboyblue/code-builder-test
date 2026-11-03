"""
Tests for sandboxes (sandbox.py): secrets stay out, results only go back through copy_out, and undo works.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sandbox  # noqa: E402


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.proj = base / "proj"
        (self.proj / "src").mkdir(parents=True)
        (self.proj / "src" / "main.c").write_text("int main(){return 0;}\n")
        (self.proj / "Makefile").write_text("all:\n\techo hi\n")
        (self.proj / ".env").write_text("TOKEN=secret\n")
        (self.proj / "config.json").write_text("{\"password\": \"x\"}")
        (self.proj / ".ssh").mkdir()
        (self.proj / ".ssh" / "id_rsa").write_text("KEY")
        outside = base / "private.txt"
        outside.write_text("private")
        os.symlink(str(outside), str(self.proj / "leak.txt"))
        os.symlink("src/main.c", str(self.proj / "inside-link.c"))
        self.root = mock.patch.object(sandbox, "ROOT", base / "sandboxes")
        self.root.start()

    def tearDown(self):
        self.root.stop()
        self.tmp.cleanup()

    def test_copy_leaves_out_secrets_and_outside_links(self):
        sb = sandbox.create("proj", str(self.proj), "linux")
        w = sandbox.work(sb)
        self.assertTrue((w / "src" / "main.c").is_file())
        self.assertTrue((w / "inside-link.c").is_symlink())
        for gone in (".env", "config.json", ".ssh", "leak.txt"):
            self.assertFalse(os.path.lexists(w / gone), gone)
        self.assertFalse(sb["network"])          # network starts off

    def test_names_and_paths_are_checked(self):
        with self.assertRaises(sandbox.SandboxError):
            sandbox.create("../evil", str(self.proj))
        sb = sandbox.create("proj", str(self.proj))
        with self.assertRaises(sandbox.SandboxError):
            sandbox.inside_work(sb, "../../etc/passwd")
        with self.assertRaises(sandbox.SandboxError):
            sandbox.plan_copy_out("proj", "src/main.c", "../outside.c")
        with self.assertRaises(sandbox.SandboxError):
            sandbox.create("proj", str(self.proj))           # already there

    def test_changed_copy_out_and_undo(self):
        sb = sandbox.create("proj", str(self.proj))
        w = sandbox.work(sb)
        (w / "src" / "main.c").write_text("int main(){return 1;}\n")
        (w / "app.bin").write_bytes(b"\0\1\2")
        changed = {rel: kind for kind, rel, _ in sandbox.changed_files("proj")}
        self.assertEqual(changed.get(os.path.join("src", "main.c")), "changed")
        self.assertEqual(changed.get("app.bin"), "new")
        # Nothing reaches the real project until copy_out.
        self.assertIn("return 0", (self.proj / "src" / "main.c").read_text())
        change, words = sandbox.plan_copy_out("proj", "src/main.c")
        self.assertIn("backed up", words)
        sandbox.copy_out(change)
        self.assertIn("return 1", (self.proj / "src" / "main.c").read_text())
        n = sandbox.recent_copies()[0]["id"]
        sandbox.undo_copy(n)
        self.assertIn("return 0", (self.proj / "src" / "main.c").read_text())
        change, _ = sandbox.plan_copy_out("proj", "app.bin")
        sandbox.copy_out(change)
        self.assertTrue((self.proj / "app.bin").exists())
        sandbox.undo_copy(sandbox.recent_copies()[0]["id"])
        self.assertFalse((self.proj / "app.bin").exists())

    def test_reset_and_delete_never_touch_project(self):
        sb = sandbox.create("proj", str(self.proj))
        (sandbox.work(sb) / "junk").write_text("x")
        sandbox.reset("proj")
        self.assertFalse((sandbox.work(sb) / "junk").exists())
        sandbox.delete("proj")
        self.assertFalse(sandbox.folder("proj").exists())
        self.assertTrue((self.proj / "src" / "main.c").exists())

    def test_container_command_is_locked_down(self):
        sb = sandbox.create("proj", str(self.proj))
        argv = sandbox.create_args("docker", "docker", sb)
        self.assertIn("none", argv[argv.index("--network") + 1:argv.index("--network") + 2])
        self.assertIn("%s:/work" % sandbox.work(sb), argv)
        self.assertEqual(sum(1 for a in argv if a == "-v"), 1)      # only the copy is visible
        self.assertIn("--memory", argv)
        sb = sandbox.set_options("proj", network=True)
        argv = sandbox.create_args("docker", "docker", sb)
        self.assertEqual(argv[argv.index("--network") + 1], "bridge")
        with mock.patch.object(sandbox, "_offline_network") as off:
            sb = sandbox.set_options("proj", network=False)
            argv = sandbox.create_args("apple", "container", sb)
            off.assert_called_once()
        self.assertEqual(argv[argv.index("--network") + 1], sandbox.OFFLINE_NET)
        with self.assertRaises(sandbox.SandboxError):
            sandbox.set_options("proj", cpus=999)

    def test_mac_profile(self):
        sb = sandbox.create("proj", str(self.proj), "linux")
        sb["kind"] = "mac"
        prof = sandbox.mac_profile(sb)
        self.assertIn("(deny network*)", prof)
        self.assertIn("(deny file-write*)", prof)
        self.assertIn("com.apple.SecurityServer", prof)
        sb["network"] = True
        self.assertNotIn("(deny network*)", sandbox.mac_profile(sb))
        env = sandbox.mac_env(sb, "/usr/bin:/bin")
        self.assertNotIn("TOKEN", " ".join(env))
        self.assertTrue(env["HOME"].startswith(str(sandbox.folder("proj"))))

    def test_run_records_output_and_respects_stop(self):
        sb = sandbox.create("proj", str(self.proj), "linux")
        sb["kind"] = "mac"
        sandbox.save(sb)
        plain = lambda s, cmd: ["/bin/sh", "-c", cmd]
        with mock.patch.object(sandbox, "mac_argv", plain), mock.patch.object(sandbox.Path, "exists",
                                                                              lambda self: True):
            code, out = sandbox.run("proj", "echo hello && ls src")
        self.assertEqual(code, 0)
        self.assertIn("hello", out)
        self.assertIn("main.c", out)
        self.assertEqual(sandbox.load("proj")["last_run"]["code"], 0)
        import threading
        stop = threading.Event()
        stop.set()
        with mock.patch.object(sandbox, "mac_argv", plain), mock.patch.object(sandbox.Path, "exists",
                                                                              lambda self: True):
            code, out = sandbox.run("proj", "sleep 30", stop=stop)
        self.assertIn("you pressed Stop", out)


if __name__ == "__main__":
    unittest.main()
