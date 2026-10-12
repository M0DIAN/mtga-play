import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from arena_log import GameTracker, JsonFramer, LogFollower, decision_readiness, normalized_events, timestamp_utc


def zone(zone_id, kind, ids=None, owner=None):
    result = {'zoneId': zone_id, 'type': 'ZoneType_' + kind, 'visibility': 'Visibility_Public'}
    if ids is not None:
        result['objectInstanceIds'] = ids
    if owner is not None:
        result['ownerSeatId'] = owner
    return result


def card(instance, grp, zone_id, owner=1, **fields):
    return {'instanceId': instance, 'grpId': grp, 'zoneId': zone_id, 'ownerSeatId': owner,
            'controllerSeatId': owner, 'visibility': 'Visibility_Public', 'type': 'GameObjectType_Card', **fields}


def send(tracker, gs):
    return tracker.process({'kind': 'gre', 'message': {'type': 'GREMessageType_GameStateMessage',
                           'systemSeatIds': [1], 'gameStateMessage': gs}})


class FramingTests(unittest.TestCase):
    def test_partial_utf8_quoted_braces_and_multiline_json(self):
        data = ('prefix\n' + json.dumps({'text': '牌 { \\" }', 'items': [{'n': 1}]}, ensure_ascii=False) + '\nother\n' +
                json.dumps({'greToClientEvent': {'greToClientMessages': []}}, indent=2)).encode()
        expected = JsonFramer().feed(data)
        for size in (1, 2, 7, 61):
            framer = JsonFramer()
            actual = []
            for start in range(0, len(data), size):
                actual += framer.feed(data[start:start + size])
            self.assertEqual(actual, expected)
            self.assertEqual(framer.errors, 0)

    def test_partial_entry_never_emitted_and_size_bound(self):
        framer = JsonFramer(max_entry_bytes=20)
        self.assertEqual(framer.feed(b'{"text":"not yet'), [])
        self.assertEqual(framer.feed(b' more than twenty bytes'), [])
        self.assertEqual(framer.errors, 1)

    def test_envelopes_drop_credentials_and_duplicate_nested_parsing(self):
        obj = {'transactionId': 'SECRET', 'accessToken': 'SECRET', 'timestamp': '1791771000000',
               'greToClientEvent': {'greToClientMessages': [{'type': 'GREMessageType_GroupReq'}]}}
        rows = list(normalized_events(obj))
        self.assertEqual(len(rows), 1)
        self.assertNotIn('SECRET', json.dumps(rows))
        self.assertEqual(timestamp_utc('639273678062733394'), '2026-10-12T02:10:06.273339+00:00')


class ReducerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = GameTracker(1)
        send(self.tracker, {'type': 'GameStateType_Full', 'gameStateId': 1,
             'gameInfo': {'gameNumber': 1}, 'players': [{'systemSeatNumber': 1, 'lifeTotal': 20}, {'systemSeatNumber': 2, 'lifeTotal': 20}],
             'zones': [zone(28, 'Battlefield', [10]), zone(27, 'Stack', []), zone(31, 'Hand', [], 1)],
             'gameObjects': [card(10, 90694, 28, cardTypes=['CardType_Artifact', 'CardType_Creature'],
                                  power={'value': 8}, toughness={'value': 8}, isTapped=True)]})

    def test_entity_replacement_clears_empty_zone_and_tapped_default(self):
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 2, 'prevGameStateId': 1,
             'zones': [zone(27, 'Stack')], 'gameObjects': [card(10, 90694, 28, power={}, toughness={})]})
        self.assertFalse(self.tracker.card(10)['tapped'])
        self.assertEqual(self.tracker.card(10)['power'], 0)
        self.assertEqual(self.tracker.zones[27].get('objectInstanceIds', []), [])

    def test_live_ability_collision_and_resolution_look_visibility(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/live_selection.json').read_text(encoding='utf-8'))
        tracker = GameTracker(1, {100685: 'Splinter, the Mentor', 90667: 'Simulacrum Synthesizer'})
        for event in fixture['events']:
            tracker.process(event)
        ability = tracker.card(306)
        self.assertIsNone(ability['card_id'])
        self.assertIsNone(ability['name'])
        self.assertEqual(ability['ability_id'], 100685)
        self.assertEqual(ability['source_card_id'], 90667)
        self.assertEqual(ability['source_name'], 'Simulacrum Synthesizer')
        prompt = tracker.snapshot()['pending_request']
        self.assertEqual([c['card_id'] for c in prompt['cards']], [94899, 94899, 87141, 96601])
        self.assertEqual(len(prompt['unfiltered_cards']), 7)
        self.assertTrue(all('cards' not in z for z in tracker.snapshot()['zones'] if z['type'] == 'ZoneType_Library'))
        tracker.objects[175]['viewers'] = [2]
        self.assertEqual(tracker.prompt(fixture['events'][-1]['message'])['cards'][0]['identity'], 'unknown')
        self.assertNotIn('card_id', tracker.card(175))

    def test_deleted_objects_and_zone_replacement_do_not_make_ghosts(self):
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 2, 'prevGameStateId': 1,
             'diffDeletedInstanceIds': [10]})
        self.assertNotIn(10, self.tracker.objects)
        self.assertEqual(self.tracker.zones[28]['objectInstanceIds'], [])

    def test_hidden_identity_is_never_reused_after_public_to_hidden(self):
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 2, 'prevGameStateId': 1,
             'zones': [zone(28, 'Battlefield'), zone(35, 'Hand', [10], 2), zone(32, 'Library', [20], 1)],
             'gameObjects': [card(10, 75494, 35, owner=2, visibility='Visibility_Private', viewers=[2]),
                             card(20, 999999, 32, visibility='Visibility_Private', viewers=[1])]})
        snapshot = self.tracker.snapshot()
        self.assertEqual(self.tracker.card(10), {'instance_id': 10, 'identity': 'unknown'})
        self.assertNotIn('999999', json.dumps(snapshot))
        self.assertNotIn('75494', json.dumps(snapshot))
        for z in snapshot['zones']:
            if z['type'] == 'ZoneType_Library' or (z['type'] == 'ZoneType_Hand' and z['owner'] != 1):
                self.assertNotIn('cards', z)

    def test_private_library_only_revealed_for_current_look_request(self):
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 2, 'prevGameStateId': 1,
             'zones': [zone(32, 'Library', [20], 1)],
             'gameObjects': [card(20, 96594, 32, visibility='Visibility_Private', viewers=[1])]})
        self.assertEqual(self.tracker.card(20)['identity'], 'unknown')
        self.tracker.process({'kind': 'gre', 'message': {'type': 'GREMessageType_GroupReq',
            'systemSeatIds': [1], 'msgId': 7, 'gameStateId': 2,
            'groupReq': {'context': 'GroupingContext_Scry', 'instanceIds': [20], 'sourceId': 30}}})
        self.assertEqual(self.tracker.snapshot()['pending_request']['cards'][0]['card_id'], 96594)

    def test_client_submission_not_server_confirmation_and_deduplicates(self):
        self.tracker.process({'kind': 'gre', 'message': {'type': 'GREMessageType_GroupReq',
            'systemSeatIds': [1], 'msgId': 7, 'gameStateId': 1,
            'groupReq': {'context': 'GroupingContext_Scry', 'instanceIds': [10], 'sourceId': 30}}})
        client = {'kind': 'client', 'message': {'type': 'ClientMessageType_GroupResp', 'respId': 7,
                  'groupResp': {'groups': [{'ids': [10], 'zoneType': 'ZoneType_Library', 'subZoneType': 'SubZoneType_Top'}]}}}
        self.assertEqual(self.tracker.process(client)[0]['confirmation'], 'client_submission_only')
        self.assertEqual(list(self.tracker.scry), [])
        self.assertEqual(self.tracker.process(client), [])
        annotation = {'id': 44, 'affectorId': 30, 'type': ['AnnotationType_Scry'],
                      'details': [{'key': 'topIds', 'valueInt32': [10]}, {'key': 'bottomIds'}]}
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 2, 'prevGameStateId': 1, 'annotations': [annotation]})
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 3, 'prevGameStateId': 2, 'persistentAnnotations': [annotation]})
        self.assertEqual(len(self.tracker.scry), 1)
        self.assertEqual(self.tracker.scry[0]['bottom'], [])

    def test_gap_stays_partial_until_full_baseline_and_out_of_order_refused(self):
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 3, 'prevGameStateId': 2})
        self.assertFalse(self.tracker.snapshot()['coverage']['continuous'])
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 2, 'prevGameStateId': 1})
        self.assertEqual(self.tracker.last_id, 3)
        send(self.tracker, {'type': 'GameStateType_Full', 'gameStateId': 4, 'gameInfo': {'gameNumber': 1},
             'players': [{'systemSeatNumber': 1}, {'systemSeatNumber': 2}],
             'zones': [zone(28, 'Battlefield'), zone(27, 'Stack'), zone(31, 'Hand', [], 1)]})
        self.assertTrue(self.tracker.snapshot()['coverage']['continuous'])

    def test_incomplete_full_schema_cannot_claim_complete_state(self):
        send(self.tracker, {'type': 'GameStateType_Full', 'gameStateId': 4})
        self.assertFalse(self.tracker.snapshot()['coverage']['continuous'])
        self.assertEqual(self.tracker.snapshot()['coverage']['gaps'][0]['kind'], 'incomplete_full_schema')

    def test_turn_replacement_does_not_leave_combat_step_in_main(self):
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 2, 'prevGameStateId': 1,
             'turnInfo': {'phase': 'Phase_Combat', 'step': 'Step_CombatDamage', 'activePlayer': 1, 'turnNumber': 2}})
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 3, 'prevGameStateId': 2,
             'turnInfo': {'phase': 'Phase_Main2', 'activePlayer': 1, 'turnNumber': 2}})
        self.assertNotIn('step', self.tracker.snapshot()['turn'])
        self.assertEqual(self.tracker.own_turn, 1)

    def test_wrong_viewing_seat_rejected_before_private_state(self):
        with self.assertRaisesRegex(ValueError, 'Viewing seat'):
            self.tracker.process({'kind': 'gre', 'message': {'type': 'GREMessageType_GameStateMessage', 'systemSeatIds': [2]}})

    def test_targets_and_stack_do_not_vanish_until_zone_update(self):
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 2, 'prevGameStateId': 1,
             'zones': [zone(27, 'Stack', [30])], 'gameObjects': [card(30, 75494, 27, owner=2)],
             'persistentAnnotations': [{'id': 1, 'type': ['AnnotationType_TargetSpec'], 'affectorId': 30, 'affectedIds': [10]}]})
        send(self.tracker, {'type': 'GameStateType_Diff', 'gameStateId': 3, 'prevGameStateId': 2, 'turnInfo': {'priorityPlayer': 1}})
        self.assertEqual(self.tracker.card(30)['target_instance_ids'], [10])
        self.assertEqual(self.tracker.zones[27]['objectInstanceIds'], [30])


class FollowerTests(unittest.TestCase):
    def test_append_partial_completion_truncate_and_rotation(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('MTGA_TEST_TMP')) as tmp:
            path = Path(tmp) / 'Player.log'
            path.write_bytes(b'prefix {"a":')
            follower = LogFollower(path)
            reset, records = follower.poll()
            self.assertTrue(reset)
            self.assertEqual(records, [])
            with path.open('ab') as stream:
                stream.write(b'1}\n{"b":2}')
            reset, records = follower.poll()
            self.assertFalse(reset)
            self.assertEqual([r[2] for r in records], [{'a': 1}, {'b': 2}])
            self.assertEqual(follower.poll()[1], [])
            # Truncate and regrow past previous offset between polls, same inode.
            path.write_bytes(b'changed prefix ' + b'{"c":3}' + b' ' * 40)
            self.assertTrue(follower.poll()[0])
            replacement = Path(tmp) / 'new.log'
            replacement.write_bytes(b'{"d":4}')
            os.replace(replacement, path)
            reset, records = follower.poll()
            self.assertTrue(reset)
            self.assertEqual(records[0][2], {'d': 4})


class ReadinessTests(unittest.TestCase):
    def test_fresh_heartbeat_does_not_hide_old_event_or_missing_state(self):
        now = datetime.now(timezone.utc)
        doc = {'live': True, 'checked_at': now.isoformat(), 'caught_up': True,
               'state': {'source_event_time': now.isoformat(), 'self_seat': 1,
                         'coverage': {'continuous': True}, 'room_state': 'MatchGameRoomStateType_Playing'}}
        self.assertTrue(decision_readiness(doc, now=now.timestamp())['ready_for_decision'])
        stale = copy.deepcopy(doc)
        stale['state']['source_event_time'] = '2020-01-01T00:00:00+00:00'
        self.assertIn('game_event_stale', decision_readiness(stale)['fallback_reasons'])
        doc['state']['coverage']['continuous'] = False
        self.assertIn('incomplete_state', decision_readiness(doc)['fallback_reasons'])


class CLIWatchTests(unittest.TestCase):
    def test_real_watcher_observes_append_partial_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('MTGA_TEST_TMP')) as tmp:
            root = Path(tmp)
            log = root / 'Player.log'
            def envelope(state_id, full=False):
                return json.dumps({'timestamp': str(int(time.time() * 1000)), 'greToClientEvent': {
                    'greToClientMessages': [{'type': 'GREMessageType_GameStateMessage', 'systemSeatIds': [1],
                    'gameStateMessage': {'type': 'GameStateType_Full' if full else 'GameStateType_Diff',
                    'gameStateId': state_id, 'prevGameStateId': state_id - 1,
                    **({'gameInfo': {'gameNumber': 1}, 'players': [{'systemSeatNumber': 1}, {'systemSeatNumber': 2}],
                        'zones': [zone(28, 'Battlefield'), zone(27, 'Stack'), zone(31, 'Hand', [], 1)]} if full else {})}}]}}).encode()
            room = json.dumps({'timestamp': str(int(time.time() * 1000)), 'matchGameRoomStateChangedEvent': {
                'gameRoomInfo': {'gameRoomConfig': {'matchId': 'test-only'}, 'stateType': 'MatchGameRoomStateType_Playing'}}}).encode()
            log.write_bytes(room + b'\n' + envelope(1, True))
            output = root / 'watch'
            proc = subprocess.Popen([sys.executable, '-B', '-X', 'utf8',
                str(Path(__file__).resolve().parents[1] / 'scripts/track_state.py'), 'watch', '--log', str(log),
                '--output', str(output), '--poll-ms', '50', '--duration-seconds', '10', '--self-seat', '1',
                '--stop-file', str(root / 'stop')],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            def await_state(predicate):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    path = output / 'state.json'
                    if path.exists():
                        doc = json.loads(path.read_text(encoding='utf-8'))
                        if predicate(doc):
                            return doc
                    time.sleep(.02)
                self.fail('Watcher did not publish expected state')
            try:
                self.assertTrue(await_state(lambda d: d['state']['game_state_id'] == 1)['ready_for_decision'])
                data = envelope(2)
                with log.open('ab') as stream:
                    stream.write(b'\n' + data[:40])
                partial = await_state(lambda d: d['source']['partial_entry'])
                self.assertFalse(partial['ready_for_decision'])
                with log.open('ab') as stream:
                    stream.write(data[40:])
                updated = await_state(lambda d: d['state']['game_state_id'] == 2)
                self.assertTrue(updated['ready_for_decision'])
                (root / 'stop').write_text('stop', encoding='utf-8')
                stdout, stderr = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, stdout + stderr)
                stopped = json.loads((output / 'state.json').read_text(encoding='utf-8'))
                self.assertFalse(stopped['live'])
                self.assertFalse(stopped['ready_for_decision'])
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
