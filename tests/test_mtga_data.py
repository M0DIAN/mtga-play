import json
import sqlite3
from pathlib import Path
import sys
import unittest
from temp_support import test_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from mtga_data import (artifact_stats, build_training, eligible, normalize_cards,
                       parse_decks, read_log_tail, select_deck, state_issues,
                       validate_session, write_json, write_jsonl)
from export_cards import read_db


def deck_line(identity='a', name='测试', quantity=4, request_string=True):
    request = {'Summary': {'DeckId': identity, 'Name': name,
                          'Attributes': [{'name': 'Format', 'value': 'Standard'}]},
               'Deck': {'MainDeck': [{'cardId': 90667, 'quantity': quantity}], 'Sideboard': []}}
    outer = {'request': json.dumps(request, ensure_ascii=False) if request_string else request}
    return 'DeckUpsertDeckV3 ' + json.dumps(outer, ensure_ascii=False)


def reviewed(status='confirmed'):
    return {'sample_id': 's_001', 'session_id': 's', 'turn': 6, 'phase': 'main',
            'observation': {'self_life': 8, 'opponent_life': 20},
            'chosen_action': {'type': 'cast_spell', 'card_id': 90667},
            'reason_summary': '建立引擎。', 'verified_result': '已观察到进场。',
            'evidence': {'effect_status': status, 'action_event_sequences': [1]},
            'quality': {'label': 'reviewed_self_play_candidate', 'expert_gold': False},
            'training_inclusion': True, 'game_outcome': 'win'}


class DataTests(unittest.TestCase):
    def test_latest_record_per_id_and_unicode_name(self):
        decks, failures = parse_decks('\n'.join([deck_line(), 'DeckUpsertDeckV3 broken', deck_line(quantity=3, request_string=False)]))
        self.assertEqual(failures, 1)
        self.assertEqual(select_deck(decks, name='测试')['main_deck'][0]['quantity'], 3)

    def test_same_name_needs_id(self):
        decks, _ = parse_decks(deck_line('a') + '\n' + deck_line('b'))
        with self.assertRaises(ValueError):
            select_deck(decks, name='测试')
        self.assertEqual(select_deck(decks, deck_id='b')['deck_id'], 'b')

    def test_quantities_are_not_booleans_or_negative(self):
        for quantity in [True, 0, -1, 1.2]:
            with self.assertRaises(ValueError):
                normalize_cards([{'cardId': 1, 'quantity': quantity}])
        self.assertEqual(normalize_cards([{'cardId': 1, 'quantity': 2}, {'cardId': 1, 'quantity': 3}])[0]['quantity'], 5)

    def test_bounded_tail_skips_partial_line(self):
        with test_directory() as directory:
            path = Path(directory) / 'Player.log'
            path.write_bytes(b'x' * (2 * 1024 * 1024) + b'\n' + deck_line().encode('utf-8'))
            text = read_log_tail(path, 1)
            self.assertLessEqual(len(text.encode('utf-8')), 1024 * 1024)
            self.assertEqual(len(parse_decks(text)[0]), 1)
            with self.assertRaises(ValueError):
                read_log_tail(path, 1000)

    def test_count_reduction_invalidates_ten_damage(self):
        token = {'name': 'Construct', 'stats_basis': {'kind': 'artifact_count'}}
        self.assertEqual(artifact_stats(token, 10), (10, 10))
        self.assertEqual(artifact_stats(token, 9), (9, 9))
        self.assertLess(artifact_stats(token, 9)[0], 10)

    def test_only_explicit_formula_and_modifiers(self):
        self.assertIsNone(artifact_stats({'name': 'Construct'}, 10))
        item = {'stats_basis': {'kind': 'artifact_count', 'power_modifier': 2, 'toughness_modifier': 1}}
        self.assertEqual(artifact_stats(item, 9), (11, 10))

    def test_complete_stacks_count_once(self):
        observation = {'artifact_count': 4, 'self_battlefield': {'artifact_list_complete': True,
                       'artifacts': [{'name': 'Blade', 'quantity': 2}, {'name': 'Construct', 'quantity': 2}]}}
        self.assertEqual(state_issues(observation), [])
        observation['artifact_count'] = 5
        self.assertTrue(state_issues(observation))

    def test_failure_and_uncertainty_do_not_become_positive(self):
        records = [reviewed('confirmed'), reviewed('failed'), reviewed('uncertain')]
        rows, metadata, skipped = build_training(records, 's')
        self.assertEqual((len(rows), len(metadata), len(skipped)), (1, 1, 2))
        self.assertNotIn('game_outcome', json.loads(rows[0]['messages'][1]['content']))
        self.assertEqual(metadata[0]['game_outcome'], 'win')

    def test_legacy_is_opt_in_and_not_failure_override(self):
        item = reviewed()
        del item['evidence']['effect_status']
        self.assertFalse(eligible(item))
        self.assertTrue(eligible(item, True))
        item['evidence']['effect_status'] = 'failed'
        self.assertFalse(eligible(item, True))

    def test_success_without_postgame_review_is_not_positive(self):
        item = reviewed()
        item['quality'] = {}
        self.assertFalse(eligible(item))

    def test_malformed_visible_state_is_reported(self):
        self.assertTrue(state_issues({'self_battlefield': []}))
        self.assertTrue(state_issues({'artifact_count': -1}))
        self.assertTrue(state_issues({'self_battlefield': {'artifacts': ['unknown']}}))

    def test_nested_future_field_rejected(self):
        item = reviewed()
        item['observation']['nested'] = {'future_draw': 'Island'}
        with self.assertRaises(ValueError):
            build_training([item], 's')

    def test_nonexistent_evidence_rejected_by_session_validation(self):
        with test_directory() as directory:
            root = Path(directory)
            write_json(root / 'session.json', {'session_id': 's', 'status': 'preparing', 'main_deck': [], 'main_deck_count': 0})
            write_jsonl(root / 'actions.jsonl', [])
            write_jsonl(root / 'decision_records.jsonl', [reviewed()])
            self.assertFalse(validate_session(root)['passed'])

    def test_writing_existing_file_refused(self):
        with test_directory() as directory:
            path = Path(directory) / 'output.jsonl'
            write_jsonl(path, [{'original': True}])
            with self.assertRaises(FileExistsError):
                write_jsonl(path, [{'overwrite': True}])
            self.assertEqual(json.loads(path.read_text()), {'original': True})

    def test_catalog_connection_refuses_database_writes(self):
        with test_directory() as directory:
            path = Path(directory) / 'cards.mtga'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE example (value INTEGER)')
                db.execute('INSERT INTO example VALUES (7)')
            db.close()  # SQLite transaction contexts do not close Windows handles.
            db = read_db(path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute('UPDATE example SET value=9')
                self.assertEqual(db.execute('SELECT value FROM example').fetchone()[0], 7)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
