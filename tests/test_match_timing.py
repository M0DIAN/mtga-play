from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from match_timing import analyze_events


def event(seq, seconds, kind, **fields):
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return {'seq': seq, 'session_id': 's', 'timestamp': timestamp.isoformat(), 'type': kind, **fields}


class TimingTests(unittest.TestCase):
    def test_boundaries_exclude_navigation_and_postgame_review(self):
        events = [event(1, 0, 'decision', stage='navigation'), event(2, 5, 'match_start'),
                  event(3, 8, 'decision'), event(4, 8.5, 'execution', decision_seq=3),
                  event(5, 20, 'game_result', result='win'), event(6, 100, 'correction')]
        report = analyze_events(events)
        self.assertEqual(report['elapsed_s'], 15)
        self.assertEqual(report['decision_count'], 1)
        self.assertEqual(report['decision_to_execution']['total_s'], .5)
        self.assertEqual(report['outside_decision_execution_intervals_s'], 14.5)
        self.assertEqual(report['boundary']['end_seq'], 5)
        self.assertTrue(report['result_recorded'])
        self.assertEqual(report['instrumented']['execution.input_ms']['count'], 0)
        self.assertIsNone(report['instrumented']['execution.input_ms']['total_s'])
        self.assertIsNone(report['instrumented']['execution.input_ms']['median_s'])

    def test_instrumented_metrics_stay_separate_and_unpaired_input_is_reported(self):
        events = [event(1, 0, 'match_start'),
                  event(2, 3, 'decision', turn=1, timing={'since_capture_ms': 2500}),
                  event(3, 3.03, 'execution', decision_seq=2,
                        timing={'input_ms': 20, 'capture_ms': 5, 'action_cycle_ms': 25}),
                  event(4, 4, 'observation', timing={'capture_ms': 7}),
                  event(5, 5, 'decision', decision_kind='ui_recovery')]
        report = analyze_events(events)
        self.assertFalse(report['result_recorded'])
        self.assertEqual(report['instrumented']['decision.since_capture_ms']['total_s'], 2.5)
        self.assertEqual(report['instrumented']['execution.input_ms']['total_s'], .02)
        self.assertEqual(report['instrumented']['execution.capture_ms']['total_s'], .005)
        self.assertEqual(report['instrumented']['observation.capture_ms']['total_s'], .007)
        self.assertEqual(report['standalone_observation_count'], 1)
        self.assertEqual(report['own_turns_observed'], [1])
        self.assertEqual(report['ui_recovery_decision_count'], 1)
        self.assertIn('5', report['warnings'][0])

    def test_legacy_start_can_be_selected_explicitly(self):
        events = [event(1, 0, 'decision', stage='navigation'),
                  event(2, .6, 'execution', decision_seq=1),
                  event(3, 10, 'decision', stage='gameplay'),
                  event(4, 10.5, 'execution', decision_seq=3), event(5, 15, 'game_result', result='win')]
        self.assertEqual(analyze_events(events, start_seq=1)['elapsed_s'], 15)
        self.assertEqual(analyze_events(events)['elapsed_s'], 5)
        with self.assertRaises(ValueError):
            analyze_events(events, start_seq=99)
        with self.assertRaises(ValueError):
            analyze_events(events, start_seq=3, end_seq=2)

    def test_duplicate_execution_or_mixed_sessions_cannot_double_count_time(self):
        events = [event(1, 0, 'decision'), event(2, 1, 'execution', decision_seq=1),
                  event(3, 2, 'execution', decision_seq=1)]
        with self.assertRaises(ValueError):
            analyze_events(events)
        events[-1] = event(3, 2, 'game_result', session_id='other')
        with self.assertRaises(ValueError):
            analyze_events(events)

    def test_reversed_timestamps_and_invalid_metrics_are_refused(self):
        with self.assertRaises(ValueError):
            analyze_events([event(1, 2, 'decision'), event(2, 1, 'game_result')])
        for value in [-1, float('inf'), float('nan')]:
            with self.assertRaises(ValueError):
                analyze_events([event(1, 0, 'decision', timing={'since_capture_ms': value})])


if __name__ == '__main__':
    unittest.main()
