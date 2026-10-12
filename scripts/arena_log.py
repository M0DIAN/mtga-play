"""Read-only MTGA JSON framing and player-view state reduction (no UI input).

Raw objects stay internal. snapshot() is the only decision-facing projection:
hidden zones are counts, private identities require the current viewing seat.
"""
from collections import deque
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp_utc(value):
    try:
        number = int(value)
        micros = (number - 621355968000000000) // 10 if number > 10**16 else number * 1000
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


class JsonFramer:
    """Frame whole JSON objects across chunks, UTF-8, braces and log prefixes."""
    def __init__(self, offset=0, max_entry_bytes=4 * 1024 * 1024):
        self.offset = offset
        self.max_entry_bytes = max_entry_bytes
        self.buffer = bytearray()
        self.depth = 0
        self.quoted = self.escaped = False
        self.start = 0
        self.errors = 0

    def feed(self, chunk):
        records = []
        for byte in chunk:
            position = self.offset
            self.offset += 1
            if not self.depth:
                if byte != 123:
                    continue
                self.start, self.depth = position, 1
                self.buffer = bytearray([byte])
                self.quoted = self.escaped = False
                continue
            self.buffer.append(byte)
            if len(self.buffer) > self.max_entry_bytes:
                self.errors += 1
                self.buffer.clear()
                self.depth = 0
                continue
            if self.quoted:
                if self.escaped:
                    self.escaped = False
                elif byte == 92:
                    self.escaped = True
                elif byte == 34:
                    self.quoted = False
            elif byte == 34:
                self.quoted = True
            elif byte in (123, 91):
                self.depth += 1
            elif byte in (125, 93):
                self.depth -= 1
                if not self.depth:
                    try:
                        value = json.loads(self.buffer)
                        if isinstance(value, dict):
                            records.append((self.start, self.offset, value))
                    except (ValueError, UnicodeError):
                        self.errors += 1
                    self.buffer.clear()
        return records


def normalized_events(obj):
    """Whitelist envelopes, removing account identifiers and transactions."""
    stamp = timestamp_utc(obj.get('timestamp'))
    if 'matchGameRoomStateChangedEvent' in obj:
        info = obj['matchGameRoomStateChangedEvent'].get('gameRoomInfo', {})
        config = info.get('gameRoomConfig', {})
        yield {'kind': 'room', 'timestamp': stamp, 'room': {
            'match_key': hashlib.sha256(str(config.get('matchId', '')).encode()).hexdigest()[:16],
            'state': info.get('stateType'), 'result': info.get('finalMatchResult')}}
    for message in obj.get('greToClientEvent', {}).get('greToClientMessages', []):
        yield {'kind': 'gre', 'timestamp': stamp, 'message': message}
    payload = obj.get('payload', obj)
    if isinstance(payload, dict) and payload.get('type', '').startswith('ClientMessageType_'):
        if payload['type'] != 'ClientMessageType_UIMessage':
            yield {'kind': 'client', 'timestamp': stamp, 'message': payload}


def details(annotation):
    return {item.get('key'): item.get('valueInt32', item.get('valueString', []))
            for item in annotation.get('details', [])}


class GameTracker:
    def __init__(self, self_seat=None, card_names=None):
        self.configured_seat = self_seat
        self.card_names = card_names if card_names is not None else {}
        self.reset()

    def reset(self, match_key=None):
        self.match_key = match_key
        self.self_seat = self.configured_seat
        self.room_state = None
        self.game_info, self.players, self.turn = {}, {}, {}
        self.objects, self.zones, self.targets = {}, {}, {}
        self.object_versions, self.zone_versions = {}, {}
        self.last_id = None
        self.have_full = False
        self.gaps = []
        self.actions = []
        self.pending = None
        self.groups = {}
        self.scry = deque(maxlen=32)
        self.draws = deque(maxlen=32)
        self.seen_annotations = set()
        self.client_seen, self.client_order = set(), deque()
        self.last_timestamp = None
        self.own_turn = 0
        self.last_own_global_turn = None
        self.event_count = 0

    def zone_for(self, instance):
        obj = self.objects.get(instance, {})
        return self.zones.get(obj.get('zoneId'), {})

    def identity_visible(self, instance, looked=False):
        obj = self.objects.get(instance, {})
        zone = self.zone_for(instance)
        kind = zone.get('type')
        # Limbo contains obsolete IDs and must never be a visible battlefield.
        if kind in {'ZoneType_Limbo', 'ZoneType_Suppressed', 'ZoneType_Sideboard'}:
            return False
        if obj.get('visibility') == 'Visibility_Public' and kind not in {'ZoneType_Library', 'ZoneType_Hand'}:
            return True
        if self.self_seat is None:
            return False
        viewer = self.self_seat in obj.get('viewers', [])
        if kind == 'ZoneType_Hand' and zone.get('ownerSeatId') == self.self_seat:
            return viewer or obj.get('visibility') == 'Visibility_Public'
        if looked and (viewer or obj.get('visibility') == 'Visibility_Public'):
            return True
        return False

    def card(self, instance, looked=False):
        obj = self.objects.get(instance, {})
        result = {'instance_id': instance}
        if not self.identity_visible(instance, looked):
            result['identity'] = 'unknown'
            return result
        is_ability = obj.get('type') == 'GameObjectType_Ability'
        # Ability grpIds use a different namespace and can collide with card IDs.
        card_id = None if is_ability else obj.get('grpId')
        result.update({'card_id': card_id, 'name': self.card_names.get(card_id) if card_id else None,
                       'object_type': obj.get('type'), 'owner': obj.get('ownerSeatId'),
                       'controller': obj.get('controllerSeatId'), 'types': obj.get('cardTypes', []),
                       'tapped': obj.get('isTapped', False), 'summoning_sick': obj.get('hasSummoningSickness', False),
                       'ability_ids': [a['grpId'] for a in obj.get('uniqueAbilities', []) if 'grpId' in a],
                       'observed_at_state_id': self.object_versions.get(instance)})
        if is_ability:
            result['ability_id'] = obj.get('grpId')
            source_id = obj.get('objectSourceGrpId')
            result['source_name'] = self.card_names.get(source_id) if source_id else None
        for raw, target in [('power', 'power'), ('toughness', 'toughness')]:
            if raw in obj:
                result[target] = obj[raw].get('value', 0)
        for raw, target in [('attackState', 'attack_state'), ('blockState', 'block_state'),
                            ('objectSourceGrpId', 'source_card_id'), ('parentId', 'parent_id')]:
            if raw in obj:
                result[target] = obj[raw]
        if instance in self.targets:
            result['target_instance_ids'] = self.targets[instance]
        return result

    def prompt(self, message):
        req_key = next((key for key in message if key.endswith('Req')), None)
        request = message.get(req_key, {}) if req_key else {}
        prompt = {'type': message.get('type'), 'msg_id': message.get('msgId'),
                  'game_state_id': message.get('gameStateId'), 'request_key': req_key}
        for field in ('context', 'groupType', 'groupSpecs', 'minSel', 'maxSel', 'idType', 'listType', 'sourceId'):
            if field in request:
                prompt[field] = copy.deepcopy(request[field])
        ids = request.get('instanceIds', request.get('ids', request.get('itemsSought', [])))
        if req_key == 'payCostsReq':
            selection = request.get('effectCostReq', {}).get('costSelection', {})
            ids = selection.get('ids', [])
            prompt['cost_selection'] = {k: copy.deepcopy(selection[k]) for k in
                ('minSel', 'maxSel', 'context', 'optionContext', 'idType', 'weights') if k in selection}
        may_look = req_key in {'groupReq', 'searchReq'} or (
            req_key == 'selectNReq' and request.get('idType') == 'IdType_InstanceId')
        prompt['cards'] = [self.card(i, looked=may_look) for i in ids]
        if req_key == 'selectNReq' and may_look and 'unfilteredIds' in request:
            # Show legally viewed but ineligible cards too; viewers still gate identity.
            prompt['unfiltered_cards'] = [self.card(i, looked=True) for i in request['unfilteredIds']]
        if req_key == 'searchReq':
            prompt['max_find'] = request.get('maxFind', 0)
            prompt['allow_fail_to_find'] = request.get('allowFailToFind')
        if req_key == 'declareAttackersReq':
            prompt['legal_attackers'] = [{'card': self.card(a['attackerInstanceId']),
                'recipients': copy.deepcopy(a.get('legalDamageRecipients', []))}
                for a in request.get('qualifiedAttackers', request.get('attackers', []))]
            prompt['can_submit'] = request.get('canSubmitAttackers', False)
        prompt['supported'] = req_key in {'groupReq', 'actionsAvailableReq', 'selectTargetsReq',
            'selectNReq', 'payCostsReq', 'declareAttackersReq', 'mulliganReq', 'searchReq', 'orderReq', 'intermissionReq'}
        if req_key == 'selectTargetsReq':
            prompt['targets'] = [{'index': t.get('targetIdx'), 'min': t.get('minTargets', 0),
                                  'max': t.get('maxTargets'), 'options': [
                                      {'card': self.card(x.get('targetInstanceId')), 'action': x.get('legalAction')}
                                      for x in t.get('targets', [])]} for t in request.get('targets', [])]
        return prompt

    def apply_state(self, gs):
        state_id = gs.get('gameStateId')
        if state_id is None:
            return [{'type': 'unsupported_state', 'reason': 'missing_gameStateId'}]
        if state_id == self.last_id:
            return []  # Duplicated transport envelope, never replay old changes.
        full = gs.get('type') == 'GameStateType_Full'
        if not full and self.last_id is not None and state_id < self.last_id:
            self.gaps.append({'kind': 'out_of_order', 'last': self.last_id, 'received': state_id})
            return [{'type': 'state_rejected', 'game_state_id': state_id}]
        changes = []
        if full:
            self.objects, self.zones, self.players, self.targets = {}, {}, {}, {}
            self.object_versions, self.zone_versions = {}, {}
            self.pending, self.groups = None, {}
            self.gaps, self.seen_annotations = [], set()
            self.scry.clear()
            self.draws.clear()
            self.client_seen.clear()
            self.client_order.clear()
            kinds = {z.get('type') for z in gs.get('zones', [])}
            seats = {p.get('systemSeatNumber') for p in gs.get('players', [])}
            self.have_full = (bool(gs.get('gameInfo')) and len(seats) >= 2 and
                              {'ZoneType_Battlefield', 'ZoneType_Stack', 'ZoneType_Hand'} <= kinds)
            if not self.have_full:
                self.gaps.append({'kind': 'incomplete_full_schema', 'received': state_id})
            self.turn, self.game_info = {}, {}
            self.own_turn = 0 if gs.get('turnInfo', {}).get('turnNumber', 0) <= 1 else None
            self.last_own_global_turn = None
        elif not self.have_full or gs.get('prevGameStateId') != self.last_id:
            gap = {'kind': 'missing_baseline' if not self.have_full else 'state_gap',
                   'last': self.last_id, 'expected_previous': gs.get('prevGameStateId'), 'received': state_id}
            self.gaps.append(gap)
            changes.append({'type': 'coverage_gap', **gap})
        self.last_id = state_id
        old_battle = {i for z in self.zones.values() if z.get('type') == 'ZoneType_Battlefield'
                      for i in z.get('objectInstanceIds', [])}
        for instance in gs.get('diffDeletedInstanceIds', []):
            self.objects.pop(instance, None)
            self.object_versions.pop(instance, None)
            self.targets.pop(instance, None)
            for zone in self.zones.values():
                if instance in zone.get('objectInstanceIds', []):
                    zone['objectInstanceIds'].remove(instance)
        for zone_id in gs.get('diffDeletedZoneIds', []):
            self.zones.pop(zone_id, None)
        # GRE diffs contain replacements of the listed entity, not field patches.
        # Omitted objectInstanceIds means an EMPTY zone; omitted isTapped means false.
        for zone in gs.get('zones', []):
            self.zones[zone['zoneId']] = copy.deepcopy(zone)
            self.zone_versions[zone['zoneId']] = state_id
        for obj in gs.get('gameObjects', []):
            self.objects[obj['instanceId']] = copy.deepcopy(obj)
            self.object_versions[obj['instanceId']] = state_id
        for player in gs.get('players', []):
            self.players[player['systemSeatNumber']] = copy.deepcopy(player)
        if 'turnInfo' in gs:
            self.turn = copy.deepcopy(gs['turnInfo'])
            number = self.turn.get('turnNumber')
            if number is not None and self.turn.get('activePlayer') == self.self_seat and number != self.last_own_global_turn:
                if self.own_turn is not None:
                    self.own_turn += 1
                self.last_own_global_turn = number
        if 'gameInfo' in gs:
            self.game_info = copy.deepcopy(gs['gameInfo'])
            self.game_info.pop('matchID', None)
        self.actions = [copy.deepcopy(a['action']) for a in gs.get('actions', []) if a.get('seatId') == self.self_seat]
        new_battle = {i for z in self.zones.values() if z.get('type') == 'ZoneType_Battlefield'
                      for i in z.get('objectInstanceIds', [])}
        if old_battle != new_battle:
            changes.append({'type': 'battlefield_change', 'entered': [self.card(i) for i in sorted(new_battle - old_battle)],
                            'left_instance_ids': sorted(old_battle - new_battle)})
        for annotation in gs.get('annotations', []) + gs.get('persistentAnnotations', []):
            key = annotation.get('id')
            if key is not None and key in self.seen_annotations:
                continue
            if key is not None:
                self.seen_annotations.add(key)
            kinds, data = annotation.get('type', []), details(annotation)
            if 'AnnotationType_TargetSpec' in kinds:
                self.targets[annotation.get('affectorId')] = annotation.get('affectedIds', [])
            if 'AnnotationType_Scry' in kinds:
                pending = next((g for g in reversed(list(self.groups.values()))
                                if g.get('sourceId') == annotation.get('affectorId')), None)
                known = {c['instance_id']: c for c in (pending or {}).get('cards', [])}
                result = {'type': 'scry_confirmed', 'server_annotation_id': key, 'game_state_id': state_id,
                          'top': [known.get(i, self.card(i, looked=True)) for i in data.get('topIds', [])],
                          'bottom': [known.get(i, self.card(i, looked=True)) for i in data.get('bottomIds', [])],
                          'client_groups': (pending or {}).get('submitted_groups'), 'timestamp': self.last_timestamp,
                          'confirmation': 'server_annotation'}
                self.scry.append(result)
                changes.append(copy.deepcopy(result))
            if 'AnnotationType_ZoneTransfer' in kinds and 'Draw' in data.get('category', []):
                for instance in annotation.get('affectedIds', []):
                    card = self.card(instance)
                    if self.objects.get(instance, {}).get('ownerSeatId') == self.self_seat:
                        draw = {'type': 'self_draw', 'game_state_id': state_id, 'card': card}
                        self.draws.append(draw)
                        changes.append(copy.deepcopy(draw))
        return changes

    def process(self, event):
        if event['kind'] == 'room':
            room = event['room']
            if room['match_key'] != self.match_key:
                self.reset(room['match_key'])
            self.room_state = room['state']
            self.last_timestamp = event.get('timestamp') or self.last_timestamp
            self.event_count += 1
            return [{'type': 'room', 'state': self.room_state}]
        message = event['message']
        recipients = message.get('systemSeatIds', [])
        if event['kind'] == 'gre' and len(recipients) == 1 and self.self_seat is not None and recipients[0] != self.self_seat:
            raise ValueError('Viewing seat disagrees with GRE recipient; refusing private-state projection')
        if self.self_seat is None and event['kind'] == 'gre' and len(message.get('systemSeatIds', [])) == 1:
            self.self_seat = message['systemSeatIds'][0]
        self.last_timestamp = event.get('timestamp') or self.last_timestamp
        self.event_count += 1
        if event['kind'] == 'client':
            key = json.dumps(message, sort_keys=True, separators=(',', ':'))
            if key in self.client_seen:
                return []
            self.client_seen.add(key)
            self.client_order.append(key)
            if len(self.client_order) > 2048:
                self.client_seen.remove(self.client_order.popleft())
            req = self.groups.get(message.get('respId'))
            changes = []
            if 'groupResp' in message and req:
                known = {c['instance_id']: c for c in req.get('cards', [])}
                groups = [{'zone': g.get('zoneType'), 'subzone': g.get('subZoneType'),
                           'cards': [known.get(i, {'instance_id': i, 'identity': 'unknown'}) for i in g.get('ids', [])]}
                          for g in message['groupResp'].get('groups', [])]
                req['submitted_groups'] = groups
                changes.append({'type': 'group_submitted', 'context': req.get('context'), 'groups': groups,
                                'resp_id': message.get('respId'), 'confirmation': 'client_submission_only'})
            if self.pending and self.pending.get('msg_id') == message.get('respId'):
                self.pending = None
            return changes
        changes = self.apply_state(message['gameStateMessage']) if 'gameStateMessage' in message else []
        if message.get('type', '').endswith('Req') and self.self_seat in message.get('systemSeatIds', []):
            self.pending = self.prompt(message)
            if 'groupReq' in message:
                self.groups[message['msgId']] = copy.deepcopy(self.pending)
            if 'actionsAvailableReq' in message:
                self.actions = message['actionsAvailableReq'].get('actions', [])
            changes.append({'type': 'request', 'request': copy.deepcopy(self.pending)})
        return changes

    def snapshot(self):
        zones = []
        for zone_id, zone in sorted(self.zones.items()):
            kind = zone.get('type')
            if kind in {'ZoneType_Limbo', 'ZoneType_Suppressed', 'ZoneType_Sideboard'}:
                continue
            ids = zone.get('objectInstanceIds', [])
            private = kind in {'ZoneType_Library', 'ZoneType_Hand'} and not (
                kind == 'ZoneType_Hand' and zone.get('ownerSeatId') == self.self_seat)
            item = {'zone_id': zone_id, 'type': kind, 'owner': zone.get('ownerSeatId'), 'count': len(ids),
                    'observed_at_state_id': self.zone_versions.get(zone_id)}
            if not private:
                item['cards'] = [self.card(i) for i in ids]
            zones.append(item)
        battlefield = [c for z in zones if z['type'] == 'ZoneType_Battlefield' for c in z.get('cards', [])]
        own = [c for c in battlefield if c.get('controller') == self.self_seat]
        artifact_count = sum('CardType_Artifact' in c.get('types', []) for c in own)
        actions = []
        for action in self.actions:
            item = {k: copy.deepcopy(action[k]) for k in ('actionType', 'instanceId', 'abilityGrpId', 'manaCost') if k in action}
            if 'instanceId' in action:
                item['card'] = self.card(action['instanceId'])
            actions.append(item)
        return {'schema_version': 1, 'match_key': self.match_key, 'self_seat': self.self_seat,
                'room_state': self.room_state, 'game_state_id': self.last_id, 'source_event_time': self.last_timestamp,
                'coverage': {'full_baseline': self.have_full, 'continuous': self.have_full and not self.gaps,
                             'gaps': self.gaps[-8:], 'scope': 'player_visible_projection; partial after a gap'},
                'turn': copy.deepcopy(self.turn), 'own_turn': self.own_turn,
                'players': [{'seat': seat, 'life': p.get('lifeTotal', 0), 'team': p.get('teamId')}
                            for seat, p in sorted(self.players.items())],
                'zones': zones, 'self_artifact_count_observed': artifact_count,
                'self_artifact_count_complete': self.have_full and not self.gaps and all('identity' not in c for c in battlefield),
                'available_actions': actions, 'pending_request': ({**copy.deepcopy(self.pending),
                    'state_current': self.pending.get('game_state_id') == self.last_id} if self.pending else None),
                'scry_history': copy.deepcopy(list(self.scry)), 'recent_self_draws': copy.deepcopy(list(self.draws)),
                'game_info': copy.deepcopy(self.game_info)}


class LogFollower:
    """Poll only appended bytes; resets on truncation/replacement, bounded bootstrap."""
    def __init__(self, path, max_bootstrap_bytes=64 * 1024 * 1024):
        self.path = Path(path)
        self.limit = max_bootstrap_bytes
        self.identity = None
        self.offset = 0
        self.framer = JsonFramer()
        self.generation = 0
        self.prefix = b''
        self.anchor = b''
        self.anchor_start = 0

    def poll(self):
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_ctime_ns)
        # Windows creation time is stable; Unix ctime changes on append, so use inode.
        identity = identity[:2] if stat.st_ino else identity
        reset = self.identity != identity or stat.st_size < self.offset
        if not reset and self.offset:
            with self.path.open('rb') as check:
                if self.prefix and check.read(len(self.prefix)) != self.prefix:
                    reset = True
                check.seek(self.anchor_start)
                if self.anchor and check.read(len(self.anchor)) != self.anchor:
                    reset = True
        if reset:
            self.identity = identity
            self.offset = max(0, stat.st_size - self.limit)
            self.framer = JsonFramer(self.offset)
            self.generation += 1
            with self.path.open('rb') as check:
                self.prefix = check.read(min(256, stat.st_size))
        result = []
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            # Read to the observed size, not an unbounded file that keeps growing.
            remaining = stat.st_size - self.offset
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    break
                result.extend(self.framer.feed(chunk))
                self.offset += len(chunk)
                remaining -= len(chunk)
            self.anchor_start = max(0, self.offset - 64)
            stream.seek(self.anchor_start)
            self.anchor = stream.read(self.offset - self.anchor_start)
        return reset, result


class CardNames(dict):
    def __init__(self, database=None):
        super().__init__()
        self.db = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) if database else None

    def get(self, card_id, default=None):
        if card_id not in self and self.db and card_id:
            row = self.db.execute('SELECT Loc FROM Localizations_enUS WHERE LocId = '
                                  '(SELECT TitleId FROM Cards WHERE GrpId = ?) '
                                  'ORDER BY CASE WHEN Formatted = 1 THEN 0 ELSE COALESCE(Formatted,999) END LIMIT 1',
                                  (card_id,)).fetchone()
            self[card_id] = row[0] if row else None
        return super().get(card_id, default)


def load_names(paths, database=None):
    names = CardNames(database)
    for path in paths:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            names[row['card_id']] = row.get('name_en')
    return names


def decision_readiness(document, max_age_seconds=5, now=None):
    """A live heartbeat does not make old game events current."""
    now = time.time() if now is None else now
    state = document.get('state', {})
    reasons = []
    for field in ('checked_at',):
        try:
            age = now - datetime.fromisoformat(document[field]).timestamp()
            if age < -1 or age > max_age_seconds:
                reasons.append('watcher_heartbeat_stale')
        except (KeyError, ValueError, TypeError):
            reasons.append('watcher_heartbeat_missing')
    try:
        age = now - datetime.fromisoformat(state['source_event_time']).timestamp()
        if age < -1 or age > max_age_seconds:
            reasons.append('game_event_stale')
    except (KeyError, ValueError, TypeError):
        reasons.append('game_event_time_missing')
    if not document.get('live'):
        reasons.append('watcher_stopped' if document.get('stopped_at') else 'offline_replay')
    if not document.get('caught_up'):
        reasons.append('source_not_caught_up')
    if not state.get('coverage', {}).get('continuous'):
        reasons.append('incomplete_state')
    if state.get('self_seat') is None:
        reasons.append('viewing_seat_unknown')
    if state.get('pending_request') and not state['pending_request'].get('state_current'):
        reasons.append('pending_request_stale')
    if state.get('pending_request') and state['pending_request'].get('supported') is False:
        reasons.append('request_semantics_unsupported')
    if state.get('room_state') != 'MatchGameRoomStateType_Playing' or state.get('game_info', {}).get('stage') == 'GameStage_GameOver':
        reasons.append('game_not_playing')
    return {'ready_for_decision': not reasons, 'fallback_reasons': reasons}
