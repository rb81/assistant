import unittest

from assistant_agent.admin_commands import HELP


class EmailAdministrationSurfaceTest(unittest.TestCase):
    def test_help_covers_core_admin_workflows(self):
        for command in ("logs", "jobs", "memory", "reminders", "review"):
            self.assertIn("/admin " + command, HELP)


if __name__ == "__main__":
    unittest.main()
