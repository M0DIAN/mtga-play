"""Replay or follow a local MTGA log, emitting only the player-view projection."""
import argparse
from collections import deque
import copy
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from arena_log import (GameTracker, LogFollower, decision_readiness, event_metadata,
                       load_names, normalized_events, utc_now)


def atomic_json(path, value):
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    failure = None
    try:
        stream = temp.open('x', encoding='utf-8', newline='\n')
        try:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.write('\n')
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                stream.close()
            except BaseException as error:
                if failure is None:
                    raise
                failure.add_note(f'Temporary JSON stream close also failed: {type(error).__name__}')
        os.replace(temp, path)
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            temp.unlink(missing_ok=True)
        except BaseException as error:
            if failure is None:
                raise
            failure.add_note(f'Temporary JSON cleanup also failed: {type(error).__name__}')


def run(args):
    output = args.output.resolve()
    # One owner per output directory; never append to or replace another run.
    output.mkdir(parents=True, exist_ok=False)
    run_id = uuid4().hex
    tracker = GameTracker(args.self_seat)
    follower = LogFollower(args.log, args.bootstrap_mib * 1024 * 1024,
                           start_at_end=getattr(args, 'start_at_end', False)) if args.log else None
    count = 0
    started = time.monotonic()
    last_signature = None
    # Never snapshot a tracker after process() failed: it may be partly mutated.
    last_state = copy.deepcopy(tracker.snapshot())
    last_source = None
    recent_events = deque(maxlen=16)
    checked_at = utc_now()
    caught_up = False
    fixture_size = 0
    journal = names = None
    first_error = first_traceback = error_report = None
    current_event = current_source = None
    phase = 'run_metadata'

    def source_status():
        return {'byte_offset': follower.offset if follower else fixture_size,
                'generation': follower.generation if follower else 1,
                'framing_errors': follower.framer.errors if follower else 0,
                'partial_entry': bool(follower.framer.depth) if follower else False}

    def last_successful_state():
        return ({**{key: last_state.get(key) for key in ('match_key', 'self_seat', 'game_state_id')},
                 'source': last_source} if last_source is not None else None)

    def record_failure(error, failed_phase):
        nonlocal first_error, first_traceback, error_report
        # Exception text can contain arbitrary payload values. Keep it only in
        # the original stderr traceback, never in player-view JSON diagnostics.
        detail = {'type': type(error).__name__, 'phase': failed_phase,
                  'message': 'Watcher operation failed; see stderr for the original traceback.'}
        if first_error is None:
            first_error, first_traceback = error, error.__traceback__
            error_report = {**detail, 'source': current_source or source_status(),
                            'event': current_event, 'recent_events': list(recent_events)}
        else:
            first_error.add_note(f'Additional failure during {failed_phase}: {type(error).__name__}')
            error_report.setdefault('additional_errors', []).append(detail)

    def state_document(live):
        document = {'schema_version': 1, 'run_id': run_id, 'live': live, 'checked_at': checked_at,
                    'status': 'error' if first_error is not None else
                              ('running' if live else 'stopped' if args.command == 'watch' else 'completed'),
                    'error': error_report, 'caught_up': caught_up and first_error is None,
                    'source': source_status(), 'event_seq': count, 'state': last_state,
                    'last_successful_state': last_successful_state()}
        if args.command == 'watch' and not live:
            document['stopped_at'] = utc_now()
        document.update(decision_readiness(document, args.max_age_seconds))
        return document

    def summary():
        return {'output': str(output), 'events': count, 'game_state_id': last_state['game_state_id'],
                'coverage': last_state['coverage'], 'live_watcher_stopped': args.command == 'watch',
                'run_id': run_id, 'status': 'error' if first_error is not None else
                            ('stopped' if args.command == 'watch' else 'completed'),
                'error': error_report, 'match_key': last_state['match_key'], 'self_seat': last_state['self_seat'],
                'last_successful_state': last_successful_state()}

    try:
        atomic_json(output / 'run.json', {'schema_version': 1, 'run_id': run_id,
                    'mode': args.command, 'created_at': checked_at,
                    'source_path': str((args.log or args.events).resolve()), 'self_seat_argument': args.self_seat,
                    'start_at_end': getattr(args, 'start_at_end', False)})
        phase = 'load_card_names'
        names = load_names(args.cards, args.card_db)
        tracker.card_names = names
        phase = 'open_journal'
        journal = (output / 'events.jsonl').open('x', encoding='utf-8', newline='\n')
        while True:
            phase = 'poll'
            if follower:
                reset, records = follower.poll()
                inputs = ((begin, end, index, event) for begin, end, obj in records
                          for index, event in enumerate(normalized_events(obj)))
            else:
                fixture_size = args.events.stat().st_size
                if fixture_size > args.bootstrap_mib * 1024 * 1024:
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
            processed = False
            phase = 'normalize_event'
            for begin, end, index, event in inputs:
                current_source = {'generation': follower.generation if follower else 1,
                    'byte_start': begin if type(begin) is int and begin >= 0 else None,
                    'byte_end': end if type(end) is int and end >= 0 else None,
                    'message_index': index if follower else None,
                    'fixture_event_index': index if not follower else None}
                phase = 'event_metadata'
                current_event = event_metadata(event)
                phase = 'process_event'
                changes = tracker.process(event)
                phase = 'project_state'
                candidate = copy.deepcopy(tracker.snapshot())
                if changes or event['kind'] != 'client':
                    signature = json.dumps({'state': candidate, 'changes': changes}, sort_keys=True)
                    if signature != last_signature:
                        row = {'seq': count + 1, 'source': current_source,
                               'event_kind': event['kind'], 'changes': changes, 'state': candidate}
                        phase = 'write_journal'
                        journal.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
                        last_signature = signature
                        count += 1
                last_state, last_source = candidate, current_source
                recent_events.append({'event': current_event, 'source': current_source})
                processed = True
                current_event = current_source = None
                phase = 'normalize_event'
            if reset and not processed:
                # A completed empty reset invalidates the old source baseline.
                # If an event failed before this point, keep the accepted state.
                last_state, last_source = copy.deepcopy(tracker.snapshot()), None
            phase = 'flush_journal'
            journal.flush()
            phase = 'source_status'
            caught_up = (follower.offset == args.log.stat().st_size and not follower.framer.depth) if follower else True
            checked_at = utc_now()
            phase = 'publish_state'
            atomic_json(output / 'state.json', state_document(args.command == 'watch'))
            phase = 'watch_control'
            if args.command == 'replay' or (args.stop_file and args.stop_file.exists()) or (args.duration_seconds and time.monotonic() - started >= args.duration_seconds):
                break
            time.sleep(args.poll_ms / 1000)
    except KeyboardInterrupt:
        pass
    except BaseException as error:
        record_failure(error, phase)
    finally:
        if journal is not None:
            try:
                journal.close()
            except BaseException as error:
                record_failure(error, 'close_journal')
        if names is not None and names.db is not None:
            try:
                names.db.close()
            except BaseException as error:
                record_failure(error, 'close_card_names')
        try:
            atomic_json(output / 'state.json', state_document(False))
        except BaseException as error:
            record_failure(error, 'final_state')
        try:
            atomic_json(output / 'summary.json', summary())
        except BaseException as error:
            record_failure(error, 'summary')
            try:
                atomic_json(output / 'state.json', state_document(False))
            except BaseException as state_error:
                record_failure(state_error, 'final_state_after_summary_error')
        try:
            print(json.dumps(summary(), ensure_ascii=False, indent=2))
        except BaseException as error:
            record_failure(error, 'print_summary')
    if first_error is not None:
        raise first_error.with_traceback(first_traceback)


def watcher_health(document, max_age_seconds):
    """A fresh observer can be healthy between games without permitting input."""
    document = document if isinstance(document, dict) else {}
    readiness = decision_readiness(document, max_age_seconds)
    health_reasons = [reason for reason in readiness['fallback_reasons'] if reason in {
        'watcher_heartbeat_missing', 'watcher_heartbeat_stale', 'watcher_stopped',
        'offline_replay', 'source_not_caught_up'}]
    has_error = document.get('error') is not None or document.get('status') == 'error'
    if has_error:
        health_reasons.append('watcher_error')
    state = document.get('state') if isinstance(document.get('state'), dict) else {}
    return {'schema_version': 1, 'status': 'BLOCKED' if health_reasons else 'PASS',
            'health_reasons': health_reasons, 'run_id': document.get('run_id'),
            'checked_at': document.get('checked_at'), 'health_checked_at': utc_now(),
            'live': document.get('live') is True, 'caught_up': document.get('caught_up') is True,
            'has_error': has_error,
            'last_match': {key: state.get(key) for key in ('match_key', 'self_seat', 'game_state_id')},
            'ready_for_action': readiness['ready_for_action'] and not has_error,
            'action_blockers': readiness['action_blockers'] + (['watcher_error'] if has_error else [])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('replay', 'watch'):
        cmd = sub.add_parser(name)
        source = cmd.add_mutually_exclusive_group(required=True)
        source.add_argument('--log', type=Path)
        if name == 'replay':
            source.add_argument('--events', type=Path, help='Frozen normalized event JSONL; internal replay input')
        else:
            cmd.add_argument('--start-at-end', action='store_true',
                             help='Start at the current log end; use before a new match, not to recover a current game')
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
    status_view = status.add_mutually_exclusive_group()
    status_view.add_argument('--full', action='store_true', help='Include history and all public zones')
    status_view.add_argument('--health-only', action='store_true',
                             help='Check watcher liveness, heartbeat, source catch-up and errors; exit 0 or 2')
    args = parser.parse_args()
    if not 0 < args.max_age_seconds < float('inf'):
        parser.error('max-age-seconds must be finite and positive')
    if args.command == 'status':
        if args.health_only:
            try:
                document = json.loads(args.state.read_text(encoding='utf-8'))
                if not isinstance(document, dict):
                    raise ValueError('Watcher state must be a JSON object')
            except (OSError, ValueError) as error:
                result = watcher_health({}, args.max_age_seconds)
                result['health_reasons'].insert(0, 'state_unavailable')
                result['state_read_error'] = {'type': type(error).__name__,
                                              'message': 'Watcher state could not be read.'}
            else:
                result = watcher_health(document, args.max_age_seconds)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['status'] == 'PASS' else 2
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
                    # snapshot() already owns the player-visible whitelist.
                    # Do not silently drop dynamic abilities or card types here.
                    result.append(dict(card))
        return result
    return {**{k: document[k] for k in ('run_id', 'status', 'error', 'last_successful_state') if k in document},
            **{k: document[k] for k in ('event_seq', 'checked_at', 'live', 'caught_up', 'ready_for_decision',
                                      'fallback_reasons', 'ready_for_action', 'action_blockers', 'waiting_on_current_request')},
            **{k: state[k] for k in ('match_key', 'game_state_id', 'coverage', 'self_seat', 'turn', 'own_turn',
                                    'players', 'pending_request', 'room_state', 'game_info', 'source_event_time')},
            'hand': cards('ZoneType_Hand', own=True), 'battlefield': cards('ZoneType_Battlefield'),
            'stack': cards('ZoneType_Stack'), 'self_artifact_count_observed': state['self_artifact_count_observed'],
            'self_artifact_count_complete': state['self_artifact_count_complete'],
            'available_actions': state['available_actions'], 'last_scry': state['scry_history'][-1:]}


if __name__ == '__main__':
    raise SystemExit(main())
