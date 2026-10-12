"""Run existing offline suites in a new external evidence directory; no game input."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


SOURCE = Path(__file__).resolve().parents[1]
PYTHON_PROBE = '''
from pathlib import Path
import sys
from tempfile import mkdtemp
directory = Path(mkdtemp(dir=sys.argv[1], prefix='probe-python-'))
try:
    path = Path(directory) / 'Player.log'
    path.write_bytes(b'probe')
    with path.open('ab') as stream:
        stream.write(b' appended')
    assert path.read_bytes() == b'probe appended'
    renamed = path.with_name('renamed.log')
    path.replace(renamed)
    renamed.unlink()
finally:
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()
print('create/write/append/read/rename/delete/cleanup: PASS')
'''
NODE_PROBE = '''
const fs = require('node:fs');
const path = require('node:path');
const directory = fs.mkdtempSync(path.join(process.argv[1], 'probe-node-'));
try {
  const file = path.join(directory, 'Player.log');
  fs.writeFileSync(file, 'probe');
  fs.appendFileSync(file, ' appended');
  if (fs.readFileSync(file, 'utf8') !== 'probe appended') throw new Error('readback mismatch');
  const renamed = path.join(directory, 'renamed.log');
  fs.renameSync(file, renamed);
  fs.unlinkSync(renamed);
} finally {
  for (const name of fs.readdirSync(directory)) fs.unlinkSync(path.join(directory, name));
  fs.rmdirSync(directory);
}
console.log('create/write/append/read/rename/delete/cleanup: PASS');
'''


def command_record(name, argv, output, environment, timeout):
    record = {'name': name, 'argv': argv, 'cwd': str(SOURCE),
              'started_at': datetime.now(timezone.utc).isoformat(),
              'stdout_path': str(output / (name + '.stdout.txt')),
              'stderr_path': str(output / (name + '.stderr.txt')),
              'exit_code': None, 'error': None}
    started = time.monotonic()
    try:
        with Path(record['stdout_path']).open('wb') as stdout, Path(record['stderr_path']).open('wb') as stderr:
            completed = subprocess.run(argv, cwd=SOURCE, env=environment, stdout=stdout,
                                       stderr=stderr, timeout=timeout, check=False)
            record['exit_code'] = completed.returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        record['error'] = f'{type(error).__name__}: {error}'
    record['elapsed_s'] = round(time.monotonic() - started, 6)
    if name in ('python', 'javascript') or record['exit_code'] != 0:
        for key in ('stdout_path', 'stderr_path'):
            path = Path(record[key])
            if path.is_file():
                print(path.read_text(encoding='utf-8', errors='replace'), file=sys.stderr)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory outside the skill/source tree')
    parser.add_argument('--node', default='node', help='Existing Node executable; no installation or fallback')
    parser.add_argument('--suite', choices=('all', 'python', 'javascript'), default='all')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(SOURCE):
        parser.error('--output must be outside the skill/source tree')
    report = {'schema_version': 1, 'version': (SOURCE / 'VERSION').read_text().strip(),
              'status': 'BLOCKED', 'source_directory': str(SOURCE),
              'output_directory': str(output), 'suite': args.suite,
              'preflight': [], 'tests': [], 'error': None}
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        report['error'] = f'{type(error).__name__}: {error}'
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    temporary = output / 'test-tmp'
    environment = dict(os.environ, MTGA_TEST_TMP=str(temporary), MTGA_PYTHON=sys.executable,
                       PYTHONDONTWRITEBYTECODE='1')
    report['test_environment'] = {key: environment[key] for key in
                                  ('MTGA_TEST_TMP', 'MTGA_PYTHON', 'PYTHONDONTWRITEBYTECODE')}
    try:
        temporary.mkdir()
        probes = [('preflight-python', [sys.executable, '-B', '-X', 'utf8', '-c', PYTHON_PROBE, str(temporary)])]
        if args.suite != 'python':
            probes.append(('preflight-javascript', [args.node, '-e', NODE_PROBE, str(temporary)]))
        for name, argv in probes:
            check = command_record(name, argv, output, environment, 30)
            report['preflight'].append(check)
            if check['exit_code'] != 0:
                break
        else:
            suites = []
            if args.suite != 'javascript':
                suites.append(('python', [sys.executable, '-B', '-X', 'utf8', '-m', 'unittest',
                                         'discover', '-s', 'tests', '-p', 'test_*.py', '-v']))
            if args.suite != 'python':
                suites.append(('javascript', [args.node, '--test', 'tests/test_arena_ui.mjs',
                                             'tests/test_state_contract.mjs', 'tests/test_match_session.mjs',
                                             'tests/test_temp_support.mjs']))
            for name, argv in suites:
                report['tests'].append(command_record(name, argv, output, environment, 300))
            report['status'] = 'PASS' if all(test['exit_code'] == 0 for test in report['tests']) else 'FAIL'
    except OSError as error:
        report['error'] = f'{type(error).__name__}: {error}'
    try:
        remaining = sorted(str(path.relative_to(temporary)) for path in temporary.rglob('*'))
        if remaining:
            report['cleanup'] = {'status': 'INCOMPLETE', 'remaining_paths': remaining,
                                 'note': 'Preserved for diagnosis; no shared or historical directories removed.'}
        else:
            temporary.rmdir()
            report['cleanup'] = {'status': 'PASS', 'remaining_paths': []}
    except OSError as error:
        report['cleanup'] = {'status': 'BLOCKED', 'remaining_paths': None,
                             'error': f'{type(error).__name__}: {error}'}
    if report['status'] == 'PASS' and report['cleanup']['status'] != 'PASS':
        report['status'] = 'FAIL'
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    try:
        (output / 'offline-report.json').write_text(text, encoding='utf-8')
    except OSError as error:
        report['status_before_report_write'] = report['status']
        report['status'] = 'BLOCKED'
        report['report_write_error'] = f'{type(error).__name__}: {error}'
        text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    print(text, end='')
    return {'PASS': 0, 'FAIL': 1, 'BLOCKED': 2}[report['status']]


if __name__ == '__main__':
    raise SystemExit(main())
