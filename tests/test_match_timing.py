from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from match_timing import analyze_events, distribution


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

    def test_fallback_capture_is_one_input_refresh_and_navigation_is_excluded(self):
        for identity in [{}, {'controller_id': 'controller-a'}]:
            with self.subTest(identity=identity):
                events = [
                    event(1, 0, 'observation', generation=1, timing={'capture_ms': 2}, **identity),
                    event(2, 1, 'decision', stage='navigation', **identity),
                    event(3, 2, 'execution', decision_seq=2, generation=2,
                          execution='sent_and_refreshed', timing={'capture_ms': 10}, **identity),
                    event(4, 3, 'match_start'),
                    event(5, 4, 'observation', generation=3, screenshots=[{'id': 'initial'}],
                          timing={'capture_ms': 30}, **identity),
                    event(6, 5, 'decision', generation=3, **identity),
                    event(7, 6, 'observation', generation=4, screenshots=[{'id': 'fallback'}],
                          timing={'capture_ms': 20}, **identity),
                    event(8, 6.1, 'execution', decision_seq=6, generation=4,
                          request_binding_mode='visual_fallback', capture_already_counted=True,
                          timing={'input_ms': 8, 'capture_ms': 20, 'action_cycle_ms': 35}, **identity),
                    event(9, 7, 'decision', generation=4, **identity),
                    event(10, 8, 'execution', decision_seq=9, generation=5,
                          execution='sent_and_refreshed',
                          timing={'input_ms': 10, 'capture_ms': 11, 'action_cycle_ms': 22}, **identity),
                    event(11, 9, 'observation', observation='Board unchanged'),
                    event(12, 10, 'game_result', result='win'),
                ]
                report = analyze_events(events)
                captures = report['captures']
                self.assertEqual(captures['unique_count'], 3)
                self.assertEqual(captures['input_refresh_count'], 2)
                self.assertEqual(captures['extra_observe_count'], 1)
                self.assertEqual(captures['duplicate_record_count'], 1)
                self.assertEqual(captures['coverage']['status'], 'complete')
                self.assertEqual(captures['capture_timing']['all']['total_s'], .061)
                self.assertEqual(captures['capture_timing']['input_refresh']['count'], 2)
                self.assertEqual(captures['capture_timing']['extra_observe']['total_s'], .03)
                self.assertEqual(report['raw_observation_event_count'], 3)
                self.assertEqual(report['standalone_observation_count'], report['raw_observation_event_count'])
                self.assertEqual(analyze_events(events, start_seq=1)['captures']['unique_count'], 5)

    def test_controller_restarts_are_distinct_and_legacy_collisions_stay_unknown(self):
        events = [event(1, 0, 'match_start'),
                  event(2, 1, 'observation', controller_id='first', generation=1),
                  event(3, 2, 'observation', controller_id='second', generation=1)]
        self.assertEqual(analyze_events(events)['captures']['unique_count'], 2)
        for e in events:
            e.pop('controller_id', None)
        captures = analyze_events(events)['captures']
        self.assertIsNone(captures['unique_count'])
        self.assertIsNone(captures['extra_observe_count'])
        self.assertEqual(captures['coverage']['ambiguous_generation_seqs'], [2, 3])
        # A later explicit game boundary safely scopes legacy generation reuse.
        events.insert(2, event(3, 1.5, 'game_start'))
        events[-1]['seq'] = 4
        self.assertEqual(analyze_events(events)['captures']['unique_count'], 2)

    def test_text_notes_and_uncertain_inputs_without_screenshots_are_not_captures(self):
        events = [event(1, 0, 'match_start'),
                  event(2, 1, 'observation', observation='Opponent thinking'),
                  event(3, 2, 'decision'),
                  event(4, 3, 'execution', decision_seq=3, execution='uncertain', generation=8,
                        timing={'input_ms': 10, 'action_cycle_ms': 20}),
                  event(5, 4, 'observation', generation=9, screenshots=[], timing={'capture_ms': 5})]
        captures = analyze_events(events)['captures']
        self.assertEqual(captures['unique_count'], 0)
        self.assertEqual(captures['input_refresh_count'], 0)
        self.assertEqual(captures['coverage']['text_observation_count'], 1)
        self.assertEqual(captures['coverage']['uncertain_without_capture_seqs'], [4])
        self.assertEqual(captures['coverage']['empty_screenshot_seqs'], [5])
        self.assertIsNone(captures['capture_timing']['all']['total_s'])

    def test_incomplete_legacy_capture_identity_and_missing_timings_are_explicit(self):
        events = [event(1, 0, 'match_start'),
                  event(2, 1, 'observation', generation=1),
                  event(3, 2, 'decision'),
                  event(4, 3, 'execution', decision_seq=3, generation=2, timing={'capture_ms': 5}),
                  event(5, 4, 'observation', timing={'capture_ms': 7}),
                  event(6, 5, 'observation', screenshots=[{'id': 'legacy-no-generation'}])]
        report = analyze_events(events)
        captures = report['captures']
        self.assertIsNone(captures['unique_count'])
        self.assertEqual(captures['confirmed_counts']['unique_count'], 2)
        self.assertEqual(captures['coverage']['status'], 'partial')
        self.assertEqual(captures['coverage']['missing_capture_evidence_seqs'], [5])
        self.assertEqual(captures['coverage']['missing_capture_identity_seqs'], [6])
        self.assertEqual(captures['capture_timing']['all']['eligible_count'], 2)
        self.assertEqual(captures['capture_timing']['all']['missing_count'], 1)
        self.assertEqual(captures['capture_timing']['all']['coverage_fraction'], .5)
        # Old raw measurement keys retain known durations, not fabricated zeroes.
        self.assertEqual(report['instrumented']['observation.capture_ms']['total_s'], .007)
        self.assertEqual(report['instrumented']['observation.capture_ms']['missing_count'], 2)
        self.assertIsNone(report['instrumented']['execution.input_ms']['total_s'])
        self.assertEqual(report['instrumented']['execution.input_ms']['missing_count'], 1)

    def test_outside_boundary_or_orphan_fallback_cannot_create_a_capture(self):
        events = [event(1, 0, 'decision', stage='navigation'),
                  event(2, 1, 'observation', controller_id='a', generation=1),
                  event(3, 2, 'match_start'),
                  event(4, 3, 'execution', controller_id='a', decision_seq=1,
                        generation=1, capture_already_counted=True)]
        captures = analyze_events(events)['captures']
        self.assertEqual(captures['unique_count'], 0)
        self.assertEqual(captures['coverage']['capture_outside_boundary_seqs'], [4])
        events[-1]['generation'] = 2
        captures = analyze_events(events)['captures']
        self.assertIsNone(captures['unique_count'])
        self.assertEqual(captures['coverage']['unresolved_reference_seqs'], [4])

    def test_p95_is_nearest_rank_and_retains_sample_count(self):
        result = distribution(list(range(1, 21)), eligible_count=21)
        self.assertEqual(result['p95_s'], 19)
        self.assertEqual(result['max_s'], 20)
        self.assertEqual(result['count'], 20)
        self.assertEqual(result['missing_count'], 1)
        self.assertEqual(distribution([1, 2, 3])['p95_s'], 3)
        self.assertIsNone(distribution([])['p95_s'])


if __name__ == '__main__':
    unittest.main()
