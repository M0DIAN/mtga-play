"""Replay or follow a local MTGA log, emitting only the player-view projection."""
import argparse
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from arena_log import GameTracker, LogFollower, decision_readiness, load_names, normalized_events, utc_now


def atomic_json(path, value):
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.write('\n')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def run(args):
    names = load_names(args.cards, args.card_db)
    output = args.output.resolve()
    # One owner per output directory; never append to or replace another run.
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / 'run.json', {'schema_version': 1, 'run_id': uuid4().hex,
                                    'mode': args.command, 'created_at': utc_now(),
                                    'source_path': str((args.log or args.events).resolve()), 'self_seat_argument': args.self_seat})
    tracker = GameTracker(args.self_seat, names)
    follower = LogFollower(args.log, args.bootstrap_mib * 1024 * 1024) if args.log else None
    count = 0
    started = time.monotonic()
    last_signature = None
    with (output / 'events.jsonl').open('x', encoding='utf-8', newline='\n') as journal:
        try:
            while True:
                if follower:
                    reset, records = follower.poll()
                    inputs = ((begin, end, index, event) for begin, end, obj in records
                              for index, event in enumerate(normalized_events(obj)))
                else:
                    if args.events.stat().st_size > args.bootstrap_mib * 1024 * 1024:
                        raise ValueError('Fixture exceeds configured read limit')
                    reset = True
                    def fixture_inputs():
                        with args.events.open(encoding='utf-8') as source:
                            for index, line in enumerate(source):
                                event = json.loads(line)
                                yield event.get('source', {}).get('byte_start'), event.get('source', {}).get('byte_end'), index, event
                    inputs = fixture_inputs()
                if reset:
                    tracker.reset()
                    last_signature = None
                for begin, end, index, event in inputs:
                    changes = tracker.process(event)
                    if not changes and event['kind'] == 'client':
                        continue
                    state = tracker.snapshot()
                    signature = json.dumps({'state': state, 'changes': changes}, sort_keys=True)
                    if signature == last_signature:
                        continue
                    last_signature = signature
                    count += 1
                    row = {'seq': count, 'source': {'generation': follower.generation if follower else 1,
                           'byte_start': begin, 'byte_end': end, 'message_index': index if follower else None,
                           'fixture_event_index': index if not follower else None},
                           'event_kind': event['kind'], 'changes': changes, 'state': state}
                    journal.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
                journal.flush()
                document = {'schema_version': 1, 'live': args.command == 'watch', 'checked_at': utc_now(),
                            'caught_up': (follower.offset == args.log.stat().st_size and not follower.framer.depth) if follower else True,
                            'source': {'byte_offset': follower.offset if follower else args.events.stat().st_size,
                                       'generation': follower.generation if follower else 1,
                                       'framing_errors': follower.framer.errors if follower else 0,
                                       'partial_entry': bool(follower.framer.depth) if follower else False},
                            'event_seq': count, 'state': tracker.snapshot()}
                document.update(decision_readiness(document, args.max_age_seconds))
                atomic_json(output / 'state.json', document)
                if args.command == 'replay' or (args.stop_file and args.stop_file.exists()) or (args.duration_seconds and time.monotonic() - started >= args.duration_seconds):
                    break
                time.sleep(args.poll_ms / 1000)
        except KeyboardInterrupt:
            pass
        finally:
            if args.command == 'watch' and 'document' in locals():
                document['live'] = False
                document['stopped_at'] = utc_now()
                document.update(decision_readiness(document, args.max_age_seconds))
                atomic_json(output / 'state.json', document)
    result = {'output': str(output), 'events': count, 'game_state_id': tracker.last_id,
              'coverage': tracker.snapshot()['coverage'], 'live_watcher_stopped': args.command == 'watch'}
    atomic_json(output / 'summary.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('replay', 'watch'):
        cmd = sub.add_parser(name)
        source = cmd.add_mutually_exclusive_group(required=True)
        source.add_argument('--log', type=Path)
        if name == 'replay':
            source.add_argument('--events', type=Path, help='Frozen normalized event JSONL; internal replay input')
        cmd.add_argument('--output', type=Path, required=True, help='New directory owned by this run')
        cmd.add_argument('--self-seat', type=int, choices=[1, 2])
        cmd.add_argument('--cards', type=Path, action='append', default=[])
        cmd.add_argument('--card-db', type=Path, help='Official SQLite catalog, opened read-only for names')
        cmd.add_argument('--bootstrap-mib', type=int, choices=range(1, 65), default=64)
        cmd.add_argument('--poll-ms', type=int, default=200)
        cmd.add_argument('--duration-seconds', type=float, default=0, help='0 follows until interrupted')
        cmd.add_argument('--stop-file', type=Path, help='Exit cleanly after publishing when this file exists')
        cmd.add_argument('--max-age-seconds', type=float, default=5)
    status = sub.add_parser('status')
    status.add_argument('--state', type=Path, required=True)
    status.add_argument('--max-age-seconds', type=float, default=5)
    status.add_argument('--full', action='store_true', help='Include history and all public zones')
    args = parser.parse_args()
    if args.max_age_seconds <= 0:
        parser.error('max-age-seconds must be positive')
    if args.command == 'status':
        document = json.loads(args.state.read_text(encoding='utf-8'))
        document.update(decision_readiness(document, args.max_age_seconds))
        print(json.dumps(document if args.full else brief(document), ensure_ascii=False, indent=2))
        return
    if not 50 <= args.poll_ms <= 5000 or args.duration_seconds < 0 or args.max_age_seconds <= 0:
        parser.error('poll-ms must be 50..5000; duration >=0; max-age >0')
    run(args)


def brief(document):
    state = document['state']
    def cards(kind, own=False):
        result = []
        for zone in state.get('zones', []):
            if zone['type'] == kind and (not own or zone.get('owner') == state.get('self_seat')):
                for card in zone.get('cards', []):
                    result.append({k: v for k, v in card.items() if k in {
                        'instance_id', 'card_id', 'name', 'identity', 'controller', 'power', 'toughness',
                        'tapped', 'summoning_sick', 'attack_state', 'block_state', 'target_instance_ids',
                        'source_card_id', 'object_type', 'ability_id', 'source_name'}})
        return result
    return {**{k: document[k] for k in ('event_seq', 'checked_at', 'live', 'ready_for_decision', 'fallback_reasons')},
            **{k: state[k] for k in ('match_key', 'game_state_id', 'coverage', 'self_seat', 'turn', 'own_turn', 'players', 'pending_request')},
            'hand': cards('ZoneType_Hand', own=True), 'battlefield': cards('ZoneType_Battlefield'),
            'stack': cards('ZoneType_Stack'), 'self_artifact_count_observed': state['self_artifact_count_observed'],
            'available_actions': state['available_actions'], 'last_scry': state['scry_history'][-1:]}


if __name__ == '__main__':
    main()
