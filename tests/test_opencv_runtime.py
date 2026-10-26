import unittest
from unittest import mock

from fukua_rpa.vision.opencv_runtime import configure_opencv_threads


class OpenCvRuntimeTests(unittest.TestCase):
    def test_thread_limit_is_bounded_by_requested_value_and_cpu_count(self):
        cv2_module = mock.Mock()
        cv2_module.getNumThreads.return_value = 2
        with mock.patch("fukua_rpa.vision.opencv_runtime.os.cpu_count", return_value=32):
            actual = configure_opencv_threads(cv2_module, 2)

        self.assertEqual(actual, 2)
        cv2_module.setNumThreads.assert_called_once_with(2)

    def test_single_core_machine_never_requests_more_than_one_thread(self):
        cv2_module = mock.Mock()
        cv2_module.getNumThreads.return_value = 1
        with mock.patch("fukua_rpa.vision.opencv_runtime.os.cpu_count", return_value=1):
            actual = configure_opencv_threads(cv2_module, 8)

        self.assertEqual(actual, 1)
        cv2_module.setNumThreads.assert_called_once_with(1)

    def test_thread_limit_is_capped_by_default_opencv_thread_limit(self):
        cv2_module = mock.Mock()
        cv2_module.getNumThreads.return_value = 2
        with mock.patch("fukua_rpa.vision.opencv_runtime.os.cpu_count", return_value=32):
            actual = configure_opencv_threads(cv2_module, 8)

        self.assertEqual(actual, 2)
        cv2_module.setNumThreads.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
