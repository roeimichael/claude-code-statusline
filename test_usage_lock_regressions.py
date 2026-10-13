import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

import statusline as sl


WORKER = '''
import pathlib, sys, time
import statusline as sl
sl.USAGE_LOCK = sys.argv[1]
mode = sys.argv[2]
if mode == "hold":
    gate = sl._usage_gate()
    assert gate is not None
    print("ready", flush=True)
    sys.stdin.readline()
    gate.close()
elif mode == "take":
    print("ready", flush=True)
    sys.stdin.readline()
    print(sl._take_lock(time.time(), sys.argv[3]), flush=True)
'''


class UsageLockConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "usage.lock"
        self.old_path = sl.USAGE_LOCK
        sl.USAGE_LOCK = str(self.path)
        self.addCleanup(setattr, sl, "USAGE_LOCK", self.old_path)

    def worker(self, mode, token=""):
        child = subprocess.Popen([sys.executable, "-c", WORKER, str(self.path), mode, token],
                                 cwd=pathlib.Path(sl.__file__).parent,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.finish, child)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child

    @staticmethod
    def finish(child):
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    def test_held_gate_prevents_takeover_and_token_checked_release(self):
        self.path.write_text("winner", encoding="utf-8")
        os.utime(self.path, (time.time() - 120, time.time() - 120))
        child = self.worker("hold")
        self.assertFalse(sl._take_lock(time.time(), "loser"))
        sl._release_lock("winner")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "winner")
        child.communicate("release\n", timeout=10)
        sl._release_lock("loser")
        self.assertTrue(self.path.exists())
        sl._release_lock("winner")
        self.assertFalse(self.path.exists())
        self.assertTrue(pathlib.Path(str(self.path) + ".guard").exists())

    def test_concurrent_stale_takeover_has_one_winner(self):
        self.path.write_text("dead", encoding="utf-8")
        os.utime(self.path, (time.time() - 120, time.time() - 120))
        children = [self.worker("take", str(i)) for i in range(6)]
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [child.communicate(timeout=10)[0].strip() for child in children]
        self.assertEqual(results.count("True"), 1, results)
        self.assertNotEqual(self.path.read_text(encoding="utf-8"), "dead")

    def test_crashed_gate_owner_does_not_block_a_future_refresh(self):
        child = self.worker("hold")
        child.kill()
        child.communicate(timeout=10)
        self.assertTrue(sl._take_lock(time.time(), "next"))


if __name__ == "__main__":
    unittest.main()
