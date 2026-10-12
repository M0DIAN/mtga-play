"""Synthetic transport orderings; these are not a replay of a user game."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

from temp_support import test_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from arena_log import GameTracker, LogFollower, decision_readiness, normalized_events


STAMP = '2026-10-12T12:00:00+00:00'


def key(match_id):
    return hashlib.sha256(match_id.encode()).hexdigest()[:16]


def room(match_id, state='Playing'):
    return next(normalized_events({'timestamp': '1791806400000',
        'matchGameRoomStateChangedEvent': {'gameRoomInfo': {
            'gameRoomConfig': {'matchId': match_id},
            'stateType': 'MatchGameRoomStateType_' + state}}}))


def full(match_id, seat, state_id=1, card_id=222222):
    info = {'gameNumber': 1, 'stage': 'GameStage_Play'}
    if match_id is not None:
        info['matchID'] = match_id
    state = {'type': 'GameStateType_Full', 'gameStateId': state_id,
        'gameInfo': info,
        'turnInfo': {'turnNumber': 1, 'decisionPlayer': seat, 'activePlayer': seat},
        'players': [{'systemSeatNumber': 1}, {'systemSeatNumber': 2}],
        'zones': [{'zoneId': 1, 'type': 'ZoneType_Battlefield'},
                  {'zoneId': 2, 'type': 'ZoneType_Stack'},
                  {'zoneId': 3, 'type': 'ZoneType_Hand', 'ownerSeatId': seat,
                   'objectInstanceIds': [10]},
                  {'zoneId': 4, 'type': 'ZoneType_Hand', 'ownerSeatId': 3 - seat,
                   'objectInstanceIds': [20]}],
        'gameObjects': [{'instanceId': 10, 'zoneId': 3, 'grpId': card_id,
                         'type': 'GameObjectType_Card', 'ownerSeatId': seat,
                         'visibility': 'Visibility_Private', 'viewers': [seat]},
                        {'instanceId': 20, 'zoneId': 4, 'grpId': 999999,
                         'type': 'GameObjectType_Card', 'ownerSeatId': 3 - seat,
                         'visibility': 'Visibility_Private', 'viewers': [3 - seat]}]}
    return {'kind': 'gre', 'timestamp': STAMP, 'message': {
        'type': 'GREMessageType_GameStateMessage', 'systemSeatIds': [seat],
        'gameStateMessage': state}}


def request(seat, state_id=1):
    return {'kind': 'gre', 'timestamp': STAMP, 'message': {
        'type': 'GREMessageType_MulliganReq', 'systemSeatIds': [seat],
        'msgId': 9, 'gameStateId': state_id, 'mulliganReq': {}}}


def completed_tracker(configured=None):
    tracker = GameTracker(configured)
    tracker.process(room('match-A'))
    tracker.process(full('match-A', 2, card_id=111111))
    tracker.process(request(2))
    tracker.process(room('match-A', 'MatchCompleted'))
    return tracker


class MatchBoundaryTests(unittest.TestCase):
    def test_gre_first_changes_identity_before_seat_and_state_id_dedup(self):
        for seat in (1, 2):
            with self.subTest(new_seat=seat):
                tracker = completed_tracker()
                # Same state ID is intentional: new matches can reuse it.
                changes = tracker.process(full('match-B', seat))
                self.assertEqual(changes[0]['type'], 'match_changed')
                snapshot = tracker.snapshot()
                self.assertEqual((snapshot['match_key'], snapshot['self_seat'], snapshot['game_state_id']),
                                 (key('match-B'), seat, 1))
                self.assertIsNone(snapshot['room_state'])
                self.assertIsNone(snapshot['pending_request'])
                self.assertTrue(snapshot['coverage']['continuous'])
                serialized = json.dumps(snapshot)
                self.assertNotIn('111111', serialized)
                self.assertNotIn('999999', serialized)
                self.assertNotIn('match-B', serialized)
                self.assertIn('222222', serialized)
                tracker.process(request(seat))
                before_room = tracker.snapshot()
                document = {'live': True, 'caught_up': True, 'checked_at': STAMP,
                            'state': before_room}
                self.assertFalse(decision_readiness(document, now=1791806400)['ready_for_action'])
                tracker.process(room('match-B'))
                after_room = tracker.snapshot()
                for field in ('coverage', 'game_state_id', 'zones', 'pending_request', 'turn'):
                    self.assertEqual(after_room[field], before_room[field])
                document['state'] = after_room
                self.assertTrue(decision_readiness(document, now=1791806400)['ready_for_action'])

    def test_room_first_resets_dynamic_seat_and_waits_for_new_full(self):
        tracker = completed_tracker()
        tracker.process(room('match-B'))
        reset = tracker.snapshot()
        self.assertIsNone(reset['self_seat'])
        self.assertIsNone(reset['game_state_id'])
        self.assertIsNone(reset['pending_request'])
        self.assertEqual(reset['zones'], [])
        self.assertFalse(reset['coverage']['full_baseline'])
        tracker.process(full('match-B', 1))
        self.assertEqual(tracker.self_seat, 1)
        self.assertTrue(tracker.snapshot()['coverage']['continuous'])

    def test_new_identity_in_diff_does_not_inherit_previous_full(self):
        tracker = completed_tracker()
        tracker.process({'kind': 'gre', 'timestamp': STAMP, 'message': {
            'type': 'GREMessageType_GameStateMessage', 'systemSeatIds': [1],
            'gameStateMessage': {'type': 'GameStateType_Diff', 'gameStateId': 1,
                'prevGameStateId': 0, 'gameInfo': {'matchID': 'match-B', 'gameNumber': 1}}}})
        snapshot = tracker.snapshot()
        self.assertEqual((snapshot['match_key'], snapshot['self_seat']), (key('match-B'), 1))
        self.assertEqual(snapshot['zones'], [])
        self.assertFalse(snapshot['coverage']['full_baseline'])
        self.assertFalse(snapshot['coverage']['continuous'])
        self.assertEqual(snapshot['coverage']['gaps'][0]['kind'], 'missing_baseline')

    def test_same_match_unknown_identity_and_fixed_seat_conflicts_still_reject(self):
        cases = [(None, full('match-A', 1)), (None, full(None, 1)),
                 (None, request(1)), (2, full('match-B', 1))]
        for configured, event in cases:
            with self.subTest(configured=configured, message=event['message']['type']):
                tracker = completed_tracker(configured)
                before = tracker.snapshot()
                with self.assertRaisesRegex(ValueError, 'Viewing seat'):
                    tracker.process(event)
                self.assertEqual(tracker.snapshot(), before)
                self.assertNotIn('222222', json.dumps(tracker.snapshot()))
        tracker = GameTracker()
        for invalid in (True, 3, '1'):
            event = full('match-B', 1)
            event['message']['systemSeatIds'] = [invalid]
            with self.assertRaisesRegex(ValueError, 'Invalid GRE viewing seat'):
                tracker.process(event)
            self.assertIsNone(tracker.self_seat)

    def test_non_state_gre_does_not_pin_or_change_viewing_seat(self):
        tracker = GameTracker()
        tracker.process({'kind': 'gre', 'message': {
            'type': 'GREMessageType_ConnectResp', 'systemSeatIds': [2]}})
        self.assertIsNone(tracker.self_seat)
        tracker.process(full('match-A', 1))
        before = tracker.snapshot()
        for kind in ('ConnectResp', 'UIMessage', 'TimerStateMessage', 'SetSettingsResp'):
            tracker.process({'kind': 'gre', 'message': {
                'type': 'GREMessageType_' + kind, 'systemSeatIds': [2]}})
            self.assertEqual(tracker.snapshot(), before)
        # A misleading outer label must not bypass checking an actual payload.
        disguised = full('match-A', 2)
        disguised['message']['type'] = 'GREMessageType_UIMessage'
        with self.assertRaisesRegex(ValueError, 'Viewing seat'):
            tracker.process(disguised)
        tracker.process(room('match-A', 'MatchCompleted'))
        tracker.process({'kind': 'gre', 'message': {
            'type': 'GREMessageType_ConnectResp', 'systemSeatIds': [2]}})
        tracker.process(full('match-B', 2))
        self.assertEqual((tracker.match_key, tracker.self_seat), (key('match-B'), 2))

    def test_missing_room_identity_does_not_reset_or_reopen_old_match(self):
        for invalid in (None, '', '  ', 42):
            with self.subTest(match_id=invalid):
                event = room(invalid)
                self.assertIsNone(event['room']['match_key'])
                tracker = completed_tracker()
                before = tracker.snapshot()
                tracker.process(event)
                after = tracker.snapshot()
                for field in ('match_key', 'self_seat', 'room_state', 'game_state_id', 'zones', 'pending_request'):
                    self.assertEqual(after[field], before[field])
                self.assertFalse(after['coverage']['continuous'])
                self.assertEqual(after['coverage']['gaps'][0]['kind'], 'missing_room_match_identity')
                tracker.process(full('match-B', 1))
                self.assertTrue(tracker.snapshot()['coverage']['continuous'])

    def test_late_previous_identity_cannot_reopen_retired_match(self):
        tracker = completed_tracker()
        tracker.process(full('match-B', 1))
        tracker.process(room('match-B'))
        tracker.process(request(1))
        before = copy.deepcopy(tracker.snapshot())
        for event in (room('match-A', 'MatchCompleted'), full('match-A', 2)):
            changes = tracker.process(event)
            self.assertEqual(changes[0]['type'], 'previous_match_ignored')
            self.assertEqual(tracker.snapshot(), before)
        # A source generation reset must permit reconstruction from older data.
        tracker.reset()
        tracker.process(room('match-A'))
        tracker.process(full('match-A', 2))
        self.assertEqual(tracker.match_key, key('match-A'))
        self.assertTrue(tracker.snapshot()['coverage']['continuous'])

    def test_start_at_end_skips_only_initial_history_then_reads_and_recovers(self):
        with test_directory() as tmp:
            path = Path(tmp) / 'Player.log'
            history = b'prefix {"old":1}\n'
            path.write_bytes(history)
            follower = LogFollower(path, start_at_end=True)
            reset, records = follower.poll()
            self.assertTrue(reset)
            self.assertEqual(records, [])
            self.assertEqual(follower.offset, len(history))
            with path.open('ab') as stream:
                stream.write(b'{"new":2}\n')
            reset, records = follower.poll()
            self.assertFalse(reset)
            self.assertEqual(records, [(len(history), len(history) + 9, {'new': 2})])
            path.write_bytes(b'{"reset":3}')
            reset, records = follower.poll()
            self.assertTrue(reset)
            self.assertEqual(records[0][2], {'reset': 3})


if __name__ == '__main__':
    unittest.main()
