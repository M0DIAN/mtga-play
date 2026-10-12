"""Real watcher subprocess regressions; all logs are synthetic and privately owned."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

from temp_support import test_directory


CLI = Path(__file__).resolve().parents[1] / 'scripts/track_state.py'
MATCH_ID = 'synthetic-match-id-must-only-appear-as-a-hash'
MATCH_KEY = hashlib.sha256(MATCH_ID.encode()).hexdigest()[:16]
PRIVATE_SENTINEL = 'PRIVATE_PAYLOAD_SENTINEL_MUST_NOT_BE_PUBLISHED'


def encoded(value):
    return json.dumps(value, separators=(',', ':')).encode('utf-8')


def message(state_id, *, seat=1, full=False, ended=False):
    state = {'type': 'GameStateType_Full' if full else 'GameStateType_Diff',
             'gameStateId': state_id, 'prevGameStateId': state_id - 1,
             'gameInfo': {'matchID': MATCH_ID, 'gameNumber': 1,
                          'stage': 'GameStage_GameOver' if ended else 'GameStage_Playing'}}
    if full:
        state.update({'players': [{'systemSeatNumber': 1, 'lifeTotal': 20},
                                  {'systemSeatNumber': 2, 'lifeTotal': 20}],
                      'zones': [{'zoneId': 28, 'type': 'ZoneType_Battlefield'},
                                {'zoneId': 27, 'type': 'ZoneType_Stack'},
                                {'zoneId': 31, 'type': 'ZoneType_Hand', 'ownerSeatId': 1},
                                {'zoneId': 35, 'type': 'ZoneType_Hand', 'ownerSeatId': 2,
                                 'objectInstanceIds': [99]}],
                      'gameObjects': [{'instanceId': 99, 'grpId': 987654321, 'zoneId': 35,
                                       'ownerSeatId': 2, 'visibility': 'Visibility_Private',
                                       'viewers': [2], 'privateMarker': PRIVATE_SENTINEL}]})
    return {'type': 'GREMessageType_GameStateMessage', 'systemSeatIds': [seat],
            'msgId': 1000 + state_id, 'gameStateMessage': state,
            'privateMarker': PRIVATE_SENTINEL}


def envelope(messages, *, timestamp=None):
    return {'timestamp': str(int(time.time() * 1000) if timestamp is None else timestamp),
            'accessToken': PRIVATE_SENTINEL, 'deck': {'privateMarker': PRIVATE_SENTINEL},
            'greToClientEvent': {'greToClientMessages': messages}}


def write_baseline(path, *, ended=False):
    stamp = 946684800000 if ended else int(time.time() * 1000)
    room = {'timestamp': str(stamp), 'matchGameRoomStateChangedEvent': {'gameRoomInfo': {
        'gameRoomConfig': {'matchId': MATCH_ID}, 'stateType': 'MatchGameRoomStateType_Playing'}}}
    path.write_bytes(encoded(room) + b'\n' +
                     encoded(envelope([message(1, full=True, ended=ended)], timestamp=stamp)) + b'\n')


class TrackStateCLITests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', '-X', 'utf8', str(CLI), *map(str, args)],
                              capture_output=True, text=True, encoding='utf-8', timeout=10, check=False)

    def start_watch(self, root):
        return subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(CLI), 'watch',
            '--log', str(root / 'Player.log'), '--output', str(root / 'watch'), '--poll-ms', '50',
            '--duration-seconds', '20', '--stop-file', str(root / 'stop')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')

    def await_state(self, proc, output, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            path = output / 'state.json'
            if path.exists():
                document = json.loads(path.read_text(encoding='utf-8'))
                if predicate(document):
                    return document
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=2)
                self.fail(f'Watcher exited before the expected state: {stdout}\n{stderr}')
            time.sleep(.02)
        self.fail('Watcher did not publish the expected state')

    def stop_if_running(self, proc):
        if proc.poll() is None:
            proc.terminate()
        proc.communicate(timeout=5)

    def assert_private_data_absent(self, output, *extra):
        text = ''.join(path.read_text(encoding='utf-8') for path in output.iterdir() if path.is_file())
        text += ''.join(extra)
        self.assertNotIn(PRIVATE_SENTINEL, text)
        self.assertNotIn(MATCH_ID, text)
        self.assertNotIn('987654321', text)

    def assert_failed_outputs(self, output, error_type):
        state = json.loads((output / 'state.json').read_text(encoding='utf-8'))
        summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
        run = json.loads((output / 'run.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'error')
        self.assertFalse(state['live'])
        self.assertFalse(state['caught_up'])
        self.assertFalse(state['ready_for_action'])
        self.assertIn('stopped_at', state)
        self.assertEqual(summary['status'], 'error')
        self.assertEqual(summary['error'], state['error'])
        self.assertEqual(summary['error']['type'], error_type)
        self.assertEqual(summary['last_successful_state'], state['last_successful_state'])
        self.assertEqual(summary['run_id'], state['run_id'])
        self.assertEqual(summary['run_id'], run['run_id'])
        return state, summary

    def test_first_poll_failure_still_writes_final_state_and_summary(self):
        with test_directory() as tmp:
            root = Path(tmp)
            output = root / 'watch'
            result = self.cli('watch', '--log', root / 'missing.log', '--output', output)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('Traceback (most recent call last)', result.stderr)
            self.assertIn('FileNotFoundError', result.stderr)
            state, summary = self.assert_failed_outputs(output, 'FileNotFoundError')
            self.assertEqual(state['event_seq'], 0)
            self.assertIsNone(state['state']['game_state_id'])
            self.assertIsNone(summary['last_successful_state'])
            self.assertEqual(summary['error']['phase'], 'poll')
            self.assertIsNone(summary['error']['event'])
            self.assertEqual(summary['error']['recent_events'], [])
            self.assertEqual(summary['error']['source']['byte_offset'], 0)
            self.assertEqual(json.loads(result.stdout), summary)

    def test_seat_conflict_keeps_last_processed_state_and_bounded_safe_diagnostics(self):
        with test_directory() as tmp:
            root = Path(tmp)
            log, output = root / 'Player.log', root / 'watch'
            write_baseline(log)
            proc = self.start_watch(root)
            try:
                before = self.await_state(proc, output, lambda d: d['state']['game_state_id'] == 1)
                self.assertTrue(before['live'])
                start = log.stat().st_size
                # Accepted events and the conflict arrive in one poll. The last
                # accepted projection must win even before the next heartbeat.
                data = encoded(envelope([message(i) for i in range(2, 20)] + [message(20, seat=2)]))
                with log.open('ab') as stream:
                    stream.write(data + b'\n')
                stdout, stderr = proc.communicate(timeout=8)
                self.assertEqual(proc.returncode, 1, stdout + stderr)
                self.assertIn('ValueError: Viewing seat disagrees with GRE recipient', stderr)
                state, summary = self.assert_failed_outputs(output, 'ValueError')
                self.assertEqual(state['state']['game_state_id'], 19)
                self.assertEqual(summary['last_successful_state']['game_state_id'], 19)
                self.assertEqual(summary['match_key'], MATCH_KEY)
                self.assertEqual(summary['self_seat'], 1)
                diagnostic = summary['error']
                self.assertEqual(diagnostic['phase'], 'process_event')
                self.assertEqual(diagnostic['source'], {'generation': 1, 'byte_start': start,
                    'byte_end': start + len(data), 'message_index': 18, 'fixture_event_index': None})
                self.assertEqual(diagnostic['event']['system_seat_ids'], [2])
                self.assertEqual(diagnostic['event']['game_state_id'], 20)
                self.assertEqual(diagnostic['event']['prev_game_state_id'], 19)
                self.assertEqual(diagnostic['event']['game_match_key'], MATCH_KEY)
                self.assertEqual(diagnostic['event']['message_type'], 'GREMessageType_GameStateMessage')
                self.assertEqual([row['event']['game_state_id'] for row in diagnostic['recent_events']], list(range(4, 20)))
                rows = [json.loads(line) for line in (output / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
                self.assertEqual(rows[-1]['state']['game_state_id'], 19)
                self.assertEqual(summary['events'], len(rows))
                self.assert_private_data_absent(output, stdout, stderr)
            finally:
                self.stop_if_running(proc)

    def test_partly_mutated_tracker_is_not_used_for_final_projection(self):
        with test_directory() as tmp:
            root = Path(tmp)
            log, output = root / 'Player.log', root / 'watch'
            write_baseline(log)
            proc = self.start_watch(root)
            try:
                before = self.await_state(proc, output, lambda d: d['state']['game_state_id'] == 1)
                invalid = message(2)
                # apply_state updates last_id before visiting each zone. The
                # missing zoneId fails after a real partial tracker mutation.
                invalid['gameStateMessage']['zones'] = [
                    {'zoneId': 28, 'type': 'ZoneType_Battlefield', 'objectInstanceIds': [42]},
                    {'type': 'ZoneType_Hand', 'privateMarker': PRIVATE_SENTINEL}]
                with log.open('ab') as stream:
                    stream.write(encoded(envelope([invalid])) + b'\n')
                stdout, stderr = proc.communicate(timeout=8)
                self.assertEqual(proc.returncode, 1, stdout + stderr)
                self.assertIn("KeyError: 'zoneId'", stderr)
                state, summary = self.assert_failed_outputs(output, 'KeyError')
                self.assertEqual(state['state'], before['state'])
                self.assertEqual(summary['game_state_id'], 1)
                self.assertEqual(summary['error']['event']['game_state_id'], 2)
                self.assertEqual(summary['error']['recent_events'][0]['event']['room_match_key'], MATCH_KEY)
                self.assert_private_data_absent(output, stdout, stderr)
            finally:
                self.stop_if_running(proc)

    def test_summary_write_failure_does_not_replace_original_process_error(self):
        with test_directory() as tmp:
            root = Path(tmp)
            log, output = root / 'Player.log', root / 'watch'
            write_baseline(log)
            proc = self.start_watch(root)
            try:
                self.await_state(proc, output, lambda d: d['state']['game_state_id'] == 1)
                # An owned directory at the target path makes summary replace
                # fail without changing permissions or intercepting the CLI.
                (output / 'summary.json').mkdir()
                with log.open('ab') as stream:
                    stream.write(encoded(envelope([message(2, seat=2)])) + b'\n')
                stdout, stderr = proc.communicate(timeout=8)
                self.assertEqual(proc.returncode, 1, stdout + stderr)
                self.assertIn('ValueError: Viewing seat disagrees with GRE recipient', stderr)
                self.assertIn('Additional failure during summary:', stderr)
                result = json.loads(stdout)
                state = json.loads((output / 'state.json').read_text(encoding='utf-8'))
                self.assertEqual(result['error']['type'], 'ValueError')
                self.assertEqual(state['error']['type'], 'ValueError')
                self.assertEqual(state['error']['additional_errors'][0]['phase'], 'summary')
                self.assertFalse(state['live'])
                self.assertFalse(state['caught_up'])
                self.assert_private_data_absent(output, stdout, stderr)
            finally:
                self.stop_if_running(proc)

    def test_health_only_distinguishes_watcher_health_from_old_finished_game(self):
        with test_directory() as tmp:
            root = Path(tmp)
            output = root / 'watch'
            write_baseline(root / 'Player.log', ended=True)
            proc = self.start_watch(root)
            try:
                before = self.await_state(proc, output, lambda d: d['state']['game_state_id'] == 1)
                result = self.cli('status', '--state', output / 'state.json', '--health-only')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                health = json.loads(result.stdout)
                self.assertEqual(health['status'], 'PASS')
                self.assertEqual(health['health_reasons'], [])
                self.assertEqual(health['run_id'], before['run_id'])
                self.assertEqual(health['last_match'], {'match_key': MATCH_KEY, 'self_seat': 1, 'game_state_id': 1})
                self.assertTrue(health['checked_at'])
                self.assertTrue(health['health_checked_at'])
                self.assertFalse(health['ready_for_action'])
                self.assertIn('game_not_playing', health['action_blockers'])
                self.assertIn('game_event_stale', health['action_blockers'])
                self.assertNotIn('zones', result.stdout)
                (root / 'stop').write_text('stop', encoding='utf-8')
                stdout, stderr = proc.communicate(timeout=8)
                self.assertEqual(proc.returncode, 0, stdout + stderr)
                stopped = self.cli('status', '--state', output / 'state.json', '--health-only')
                self.assertEqual(stopped.returncode, 2, stopped.stdout + stopped.stderr)
                self.assertIn('watcher_stopped', json.loads(stopped.stdout)['health_reasons'])
                for patch, expected_reason in [
                    ({'checked_at': '2000-01-01T00:00:00+00:00'}, 'watcher_heartbeat_stale'),
                    ({'caught_up': False}, 'source_not_caught_up'),
                    ({'error': {'type': 'SyntheticFailure'}}, 'watcher_error')]:
                    with self.subTest(reason=expected_reason):
                        document = copy.deepcopy(before)
                        document.update(patch)
                        state_path = root / 'health-state.json'
                        state_path.write_text(json.dumps(document), encoding='utf-8')
                        blocked = self.cli('status', '--state', state_path, '--health-only')
                        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
                        self.assertIn(expected_reason, json.loads(blocked.stdout)['health_reasons'])
                missing = self.cli('status', '--state', root / 'missing-state.json', '--health-only')
                self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
                self.assertIn('state_unavailable', json.loads(missing.stdout)['health_reasons'])
            finally:
                self.stop_if_running(proc)

    def test_start_at_end_is_explicit_and_watch_only(self):
        with test_directory() as tmp:
            root = Path(tmp)
            log, output = root / 'Player.log', root / 'watch'
            write_baseline(log)
            result = self.cli('watch', '--log', log, '--output', output, '--start-at-end',
                              '--poll-ms', '50', '--duration-seconds', '.1')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(json.loads((output / 'run.json').read_text(encoding='utf-8'))['start_at_end'])
            state = json.loads((output / 'state.json').read_text(encoding='utf-8'))
            self.assertIsNone(state['state']['match_key'])
            self.assertEqual(state['event_seq'], 0)
            replay = self.cli('replay', '--log', log, '--output', root / 'replay', '--start-at-end')
            self.assertEqual(replay.returncode, 2)
            self.assertIn('unrecognized arguments: --start-at-end', replay.stderr)
            self.assertFalse((root / 'replay').exists())


if __name__ == '__main__':
    unittest.main()
