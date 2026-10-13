import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import statusline as sl


class InstallerRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name) / "Claude's config & tools"
        self.dir.mkdir()

    def install(self, *flags):
        with mock.patch("builtins.print"):
            return sl.install_main(["install", "--claude-dir", str(self.dir),
                                    "--icons", "none", *flags])

    def test_custom_path_renders_and_uses_its_own_config_directory(self):
        self.assertEqual(self.install("--no-test"), 0)
        command = json.loads((self.dir / "settings.json").read_text())["statusLine"]["command"]
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.dir.parent / "other"), STATUSLINE_LIVE_USAGE="0")
        result = subprocess.run(command, shell=True, input=json.dumps(sl.SAMPLE_PAYLOAD),
                                capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Test", result.stdout)
        probe = command.replace("statusline.main()", "print(statusline.CLAUDE_DIR)")
        result = subprocess.run(probe, shell=True, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(os.path.abspath(result.stdout.strip()), str(self.dir))

    def test_failed_update_restores_settings_script_and_install_record(self):
        settings = self.dir / "settings.json"
        settings.write_text(json.dumps({"statusLine": {"command": "original"}}))
        self.assertEqual(self.install("--no-test"), 0)
        script = self.dir / "statusline.py"
        script.write_bytes(b"# previous working installation\n")
        paths = [settings, script, self.dir / sl.INSTALL_RECORD]
        before = {path: path.read_bytes() for path in paths}
        with mock.patch.object(sl, "_smoke_test", return_value=(False, "failed update")):
            self.assertEqual(self.install(), 1)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_failed_first_install_removes_new_settings_script_and_record(self):
        with mock.patch.object(sl, "_smoke_test", return_value=(False, "failed install")):
            self.assertEqual(self.install(), 1)
        for name in ("settings.json", "statusline.py", sl.INSTALL_RECORD):
            self.assertFalse((self.dir / name).exists(), name)

    def test_backups_do_not_overwrite_each_other_within_one_second(self):
        settings = self.dir / "settings.json"
        settings.write_text('{"model":"first"}')
        with mock.patch.object(sl.time, "strftime", return_value="20261013-120000"):
            self.assertEqual(self.install("--no-test"), 0)
            settings.write_text('{"model":"second"}')
            self.assertEqual(self.install("--no-test"), 0)
        backups = list((self.dir / "backups").glob("statusline-*/settings.json"))
        self.assertEqual(len(backups), 2)
        self.assertEqual({json.loads(path.read_text())["model"] for path in backups}, {"first", "second"})


if __name__ == "__main__":
    unittest.main()
