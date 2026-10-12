import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import run_offline
from temp_support import test_directory


class OfflineReportTests(unittest.TestCase):
    def test_file_write_denial_still_returns_original_blocking_evidence(self):
        with test_directory() as parent:
            source = Path(parent) / 'source'
            source.mkdir()
            (source / 'VERSION').write_text('test-version', encoding='utf-8')
            output = Path(parent) / 'new-output'
            original_open = Path.open

            def deny_output_writes(path, mode='r', *args, **kwargs):
                if path.parent == output and ('w' in mode or 'a' in mode):
                    raise PermissionError(13, 'simulated file writes denied after mkdir', str(path))
                return original_open(path, mode, *args, **kwargs)

            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(run_offline, 'SOURCE', source), \
                    patch.object(Path, 'open', deny_output_writes), \
                    patch('sys.argv', ['run_offline.py', '--suite', 'python', '--output', str(output)]), \
                    patch.object(run_offline.subprocess, 'run') as execute, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = run_offline.main()

            execute.assert_not_called()
            self.assertEqual(code, 2)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report['status'], 'BLOCKED')
            self.assertEqual(report['tests'], [])
            self.assertEqual(len(report['preflight']), 1)
            self.assertIn('PermissionError', report['preflight'][0]['error'])
            self.assertIn('preflight-python.stdout.txt', report['preflight'][0]['error'])
            self.assertIn('PermissionError', report['report_write_error'])
            self.assertIn('offline-report.json', report['report_write_error'])
            self.assertEqual(report['cleanup']['status'], 'PASS')
            self.assertEqual(list(output.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
