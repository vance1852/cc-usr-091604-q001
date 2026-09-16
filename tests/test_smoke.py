import unittest

from app.schedule import ScheduleDraft, ScheduleService


class ScheduleSmokeTest(unittest.TestCase):
    def test_health_and_draft(self):
        self.assertEqual(ScheduleService().health()["status"], "ok")
        self.assertEqual(ScheduleDraft("春季联赛").name, "春季联赛")


if __name__ == "__main__":
    unittest.main()

