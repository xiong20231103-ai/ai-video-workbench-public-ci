import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import upload_service


class UploadServiceTests(unittest.TestCase):
    def test_pid_check_never_signals_the_process(self):
        # os.kill(pid, 0) terminates the target on Windows.
        with patch("os.kill", side_effect=AssertionError("os.kill must not be used")):
            self.assertFalse(upload_service._process_exists(2_147_483_647))
            self.assertTrue(upload_service._process_exists(upload_service.os.getpid()))

    def test_live_pid_of_another_program_is_a_stale_file(self):
        with patch.object(upload_service, "pid_is_alive", return_value=True):
            with patch.object(upload_service, "process_image_name", return_value=r"C:\Windows\explorer.exe"):
                self.assertFalse(upload_service._process_exists(6472))
            with patch.object(upload_service, "process_image_name",
                              return_value=r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable\python_runtime\python.exe"):
                self.assertTrue(upload_service._process_exists(6472))
            with patch.object(upload_service, "process_image_name", return_value=""):
                self.assertTrue(upload_service._process_exists(6472))

    def test_stale_pid_is_replaced_and_released(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_file = Path(temp_dir) / "upload_service.pid"
            pid_file.write_text("6472", encoding="ascii")
            with patch.object(upload_service, "PID_FILE", pid_file), patch.object(
                upload_service, "_acquire_windows_mutex", return_value=True
            ), patch.object(upload_service, "_release_windows_mutex"), patch.object(
                upload_service, "_process_exists", return_value=False
            ):
                self.assertTrue(upload_service.acquire_pid_file())
                self.assertEqual(
                    str(upload_service.os.getpid()),
                    pid_file.read_text(encoding="ascii"),
                )
                upload_service.release_pid_file()
                self.assertFalse(pid_file.exists())


if __name__ == "__main__":
    unittest.main()
