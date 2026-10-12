/** Shared observation/action readiness contract for the watcher and UI.
 *
 * Keep behavior aligned with arena_log.decision_readiness. The cross-language
 * fixture covers ordinary waiting, source failure, and request invalidation.
 * Published readiness booleans are deliberately not trusted by this assessor.
 */

const record = value => value !== null && typeof value === 'object' && !Array.isArray(value) ? value : {};
const isId = value => Number.isSafeInteger(value) && value >= 0;

export function assessReadiness(document, { nowMs = Date.now(), maxAgeMs = 5000 } = {}) {
  if (!Number.isFinite(maxAgeMs) || maxAgeMs <= 0) throw new TypeError('maxAgeMs must be finite and positive');
  if (!Number.isFinite(nowMs)) throw new TypeError('nowMs must be finite');
  document = record(document);
  const state = record(document.state);
  const coverage = record(state.coverage);
  const turn = record(state.turn);
  const gameInfo = record(state.game_info);
  const hasPending = state.pending_request !== null && state.pending_request !== undefined;
  const pending = record(state.pending_request);
  const ageOf = value => {
    // Require an explicit offset; a local-time parse changes meaning by host.
    if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? nowMs - parsed : null;
  };
  const heartbeatAge = ageOf(document.checked_at);
  const eventAge = ageOf(state.source_event_time);
  const heartbeatReason = heartbeatAge === null ? 'watcher_heartbeat_missing'
    : heartbeatAge < -1000 || heartbeatAge > maxAgeMs ? 'watcher_heartbeat_stale' : null;
  const live = document.live === true;
  const caughtUp = document.caught_up === true;
  const continuous = coverage.continuous === true;
  const seatKnown = isId(state.self_seat) && [1, 2].includes(state.self_seat);
  const stateKnown = isId(state.game_state_id);
  const identityKnown = isId(pending.msg_id) && isId(pending.game_state_id);
  const current = hasPending && stateKnown && identityKnown && pending.state_current === true
    && pending.game_state_id === state.game_state_id;
  const supported = pending.supported === true;
  const ownDecision = seatKnown && isId(turn.decisionPlayer) && turn.decisionPlayer === state.self_seat;
  const playing = state.room_state === 'MatchGameRoomStateType_Playing' && gameInfo.stage !== 'GameStage_GameOver'
    && !(Array.isArray(gameInfo.results) && gameInfo.results.length > 0);
  const waiting = !heartbeatReason && live && caughtUp && continuous && seatKnown && stateKnown
    && current && supported && ownDecision && playing;
  const eventReason = eventAge === null ? 'game_event_time_missing'
    : eventAge < -1000 || (eventAge > maxAgeMs && !waiting) ? 'game_event_stale' : null;
  const reasons = [heartbeatReason, eventReason,
    live ? null : document.stopped_at ? 'watcher_stopped' : 'offline_replay',
    caughtUp ? null : 'source_not_caught_up',
    continuous ? null : 'incomplete_state',
    seatKnown ? null : 'viewing_seat_unknown',
    stateKnown ? null : 'state_identity_missing',
    hasPending && !current ? 'pending_request_stale' : null,
    hasPending && !supported ? 'request_semantics_unsupported' : null,
    playing ? null : 'game_not_playing'].filter(Boolean);
  const actionBlockers = [...reasons];
  if (!hasPending) actionBlockers.push('pending_request_missing');
  else if (!identityKnown) actionBlockers.push('request_identity_missing');
  if (!ownDecision) actionBlockers.push('not_decision_player');
  return { ready_for_decision: reasons.length === 0, fallback_reasons: reasons,
    ready_for_action: actionBlockers.length === 0, action_blockers: actionBlockers,
    waiting_on_current_request: Boolean(waiting && !eventReason) };
}
