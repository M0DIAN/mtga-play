"""Exercise the no-input path only; live success is recorded in match evidence."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'timed_drag.ps1'
POWERSHELL = shutil.which('powershell.exe')


@unittest.skipUnless(POWERSHELL, 'Windows PowerShell is required for the no-input plan')
class TimedDragPlanTests(unittest.TestCase):
    def run_plan(self, *args):
        return subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
                               *map(str, args)], capture_output=True, text=True, timeout=20)

    def test_linear_500ms_plan_has_exact_endpoints(self):
        result = self.run_plan('-FromX', 1100, '-FromY', 420, '-ToX', 580, '-ToY', 480,
                               '-DurationMs', 500)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertIs(plan['executed'], False)
        points = plan['path']
        self.assertEqual(len(points), 33)
        self.assertEqual(points[0], {'time_ms': 0, 'x': 1100, 'y': 420})
        self.assertEqual(points[-1], {'time_ms': 500, 'x': 580, 'y': 480})
        for i, point in enumerate(points):
            self.assertAlmostEqual(point['time_ms'], 500 * i / 32, delta=.001)
            self.assertLessEqual(abs(point['x'] - (1100 - 520 * i / 32)), .5)
            self.assertLessEqual(abs(point['y'] - (420 + 60 * i / 32)), .5)

    def test_invalid_duration_rejected_without_input(self):
        result = self.run_plan('-FromX', 1100, '-FromY', 420, '-ToX', 580, '-ToY', 480,
                               '-DurationMs', 0)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('250', result.stderr)
        self.assertIn('ParameterArgumentValidationError', result.stderr)

    def test_outside_client_rejected_without_input(self):
        result = self.run_plan('-FromX', 1920, '-FromY', 420, '-ToX', 580, '-ToY', 480)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Point outside MTGA client', result.stderr)


if __name__ == '__main__':
    unittest.main()
