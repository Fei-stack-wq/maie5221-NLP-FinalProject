import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import psutil

from miniagent.member_processes import managed_member_processes
from miniagent.sandbox import Sandbox


class MemberProcessTest(unittest.TestCase):
    def test_os_inventory_restriction_does_not_erase_the_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(Path(tmp))
            with patch("psutil.process_iter", side_effect=PermissionError()):
                with managed_member_processes(sandbox) as result:
                    pass
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(sandbox.process_env, {})

    def test_detached_server_survives_between_calls_but_not_failed_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(Path(tmp))
            code = "import time; time.sleep(45)"
            with subprocess.Popen([sys.executable, "-I", "-c", code], cwd=tmp) as unrelated:
                try:
                    with self.assertRaisesRegex(RuntimeError, "failed"):
                        with managed_member_processes(sandbox):
                            spawn = ("import subprocess,sys; print(subprocess.Popen("
                                     "[sys.executable,'-I','-c'," + repr(code) + "], start_new_session=True,"
                                     "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)")
                            pid = int(subprocess.check_output([sys.executable, "-I", "-c", spawn], cwd=tmp,
                                                              env={**os.environ, **sandbox.process_env}))
                            self.assertTrue(psutil.pid_exists(pid))
                            raise RuntimeError("failed")
                    self.assertFalse(psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
                    self.assertIsNone(unrelated.poll())
                finally:
                    unrelated.terminate()
