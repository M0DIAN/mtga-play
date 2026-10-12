/** Real filesystem/coordinator check using synthetic state and no UI backend. */
import { mkdtemp, writeFile, readFile, rm, stat } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { createArenaController } from './arena_ui.mjs';
import { createMatchSession } from './match_session.mjs';
import { cropPNG, cropCurrentFrame } from './frame_crop.mjs';

export async function runReplPreflight({ tempRoot } = {}) {
  const report = { schema_version: 1, status: 'failed', scope: 'synthetic_no_input',
    live_game_baseline_verified: false, checks: {}, backend_calls: [],
    temporary_directory: null, cleanup: { created: false, removed: false } };
  const sessions = [];
  const check = (name, condition) => {
    report.checks[name] = Boolean(condition);
    if (!condition) throw new Error('REPL preflight check failed: ' + name);
  };
  try {
    if (typeof tempRoot !== 'string' || !isAbsolute(tempRoot)) {
      throw new Error('Explicit absolute tempRoot required; use an existing writable scratch directory');
    }
    // Never fall back to system Temp, touch Player.log, or reuse an old run.
    const directory = await mkdtemp(join(tempRoot, 'mtga-repl-preflight-'));
    report.temporary_directory = directory;
    report.cleanup.created = true;
    const logStatePath = join(directory, 'synthetic-player-state.json');
    const journalPath = join(directory, 'synthetic-actions.jsonl');
    const sessionId = 'preflight:' + directory;
    const now = Date.now(), stamp = new Date(now).toISOString();
    const document = { schema_version: 1, live: true, checked_at: stamp, caught_up: true,
      source: { generation: 1 }, event_seq: 1,
      state: { schema_version: 1, match_key: 'synthetic-preflight-match', self_seat: 1,
        game_state_id: 1, source_event_time: stamp, room_state: 'MatchGameRoomStateType_Playing',
        coverage: { full_baseline: true, continuous: true, gaps: [] },
        turn: { decisionPlayer: 1, activePlayer: 1, turnNumber: 1 },
        pending_request: { type: 'GREMessageType_ActionsAvailableReq', msg_id: 1,
          game_state_id: 1, state_current: true, supported: true },
        zones: [], game_info: { stage: 'GameStage_Playing' } } };
    await writeFile(logStatePath, JSON.stringify(document), 'utf8');
    // Every method fails closed. No real window or computer API is accepted.
    const backend = new Proxy({}, { get(_target, method) {
      return async () => {
        report.backend_calls.push(String(method));
        throw new Error('REPL preflight denies all backend calls: ' + String(method));
      };
    } });
    const arena = await createArenaController(backend, { sessionId,
      window: { app: 'synthetic-preflight-only', id: 1 }, journalPath, logStatePath,
      requireExpectedRequest: true, wallNow: () => now });
    check('strict_controller_created', arena.requiresExpectedRequest === true && arena.sessionId === sessionId);
    const config = { sessionId, logStatePath, wallNow: () => now, presentation: 'compact' };
    const create = async (controller = arena, options = config) => {
      const session = await createMatchSession(controller, options);
      sessions.push(session);
      return session;
    };
    const rejected = async (work, expected) => {
      try { await work(); } catch (error) { return expected.test(String(error)); }
      return false;
    };
    check('strict_controller_identity_guard', await rejected(
      () => create({ ...arena, requiresExpectedRequest: false }), /strict arena controller/));
    const session = await create();
    check('session_created', session.sessionId === sessionId);
    check('duplicate_session_owner_guard', await rejected(() => create(), /active REPL owner/));
    const otherId = sessionId + ':other';
    check('duplicate_source_owner_guard', await rejected(
      () => create({ ...arena, sessionId: otherId }, { ...config, sessionId: otherId }), /active REPL owner/));
    const packet = await session.next({ timeoutMs: 0 });
    check('next_decision', packet.status === 'needs_decision' && typeof packet.token === 'string');
    check('compact_packet', packet.presentation === 'compact' && !!packet.state.card_catalog);
    const detail = await session.details(packet.token, { path: ['pending_request'], limit: 64 });
    check('bounded_detail', detail.status === 'detail' && detail.json.length <= 64 && detail.next_offset === 64);
    check('no_automatic_frame_binding', (await session.bind(packet.token,
      { generation: 0, note: 'synthetic preflight has no observed image' })).reason === 'frame_not_for_decision');
    check('confirm_next_requires_effect_review', (await session.confirm({ decisionSeq: 1, generation: 0,
      status: 'confirmed', note: 'synthetic preflight has no observed effect', next: true })).reason === 'effect_review_mismatch');
    check('refresh_evidence_surface', arena.refreshEvidence === null);
    // Pure read aid with real, valid Screenshot.url bytes. This is not a real
    // controller observation and never bypasses readiness or grants input.
    const url = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAQAAAACCAYAAAB/qH1jAAAAF0lEQVR4nGP4z8DwHwwZ/v//D2MhCwAAIBAR7/vVKbUAAAAASUVORK5CYII=';
    const source = { controllerId: 'synthetic-crop', generation: 1,
      state: { screenshots: [{ id: 'png', url, width: 2, height: 1, originX: 10, originY: 20 }] } };
    const cropRequest = { controllerId: source.controllerId, generation: 1, screenshotId: 'png',
      coordinateSpace: 'screenshot', rect: { x: 0, y: 0, width: 0.5, height: 1 } };
    const crop = await cropCurrentFrame(source, cropRequest);
    const other = await cropCurrentFrame(source, { ...cropRequest, rect: { ...cropRequest.rect, x: 0.5 } });
    check('current_png_crop', crop.image.width === 1 && crop.image.height === 2 &&
      crop.image.url !== other.image.url && crop.mapping.scaleX === 2 && crop.full_frame === source.state);
    check('crop_stale_guard', await rejected(() => cropCurrentFrame(source, { ...cropRequest, generation: 2 }), /current controller/));
    check('crop_bounds_guard', await rejected(() => cropCurrentFrame(source,
      { ...cropRequest, rect: { ...cropRequest.rect, x: 2 } }), /bounds/));
    check('png_renderer_export', typeof cropPNG === 'function');
    check('cancel_invalidates_token', (await session.cancel('synthetic preflight')).reason === 'cancelled'
      && session.token === null);
    const renewed = await session.next({ timeoutMs: 0 });
    check('next_after_cancel', renewed.status === 'needs_decision' && renewed.token !== packet.token);
    document.state.coverage.continuous = false;
    document.state.coverage.gaps = ['synthetic-preflight-gap'];
    await writeFile(logStatePath, JSON.stringify(document), 'utf8');
    const blocked = await session.next({ timeoutMs: 0 });
    check('readiness_guard', blocked.status === 'blocked' && blocked.reasons.includes('incomplete_state')
      && session.token === null);
    check('close', (await session.close('synthetic preflight')).reason === 'session_closed');
    check('closed_session_guard', (await session.next({ timeoutMs: 0 })).reason === 'session_closed');
    document.state.coverage.continuous = true;
    document.state.coverage.gaps = [];
    await writeFile(logStatePath, JSON.stringify(document), 'utf8');
    const reacquired = await create();
    check('ownership_reacquired', (await reacquired.next({ timeoutMs: 0 })).status === 'needs_decision');
    await reacquired.close('synthetic preflight reacquisition complete');
    const rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
    report.journal_event_types = rows.map(row => row.type);
    check('journal_lifecycle', rows.filter(row => row.type === 'session_started').length === 2
      && rows.filter(row => row.type === 'session_closed').length === 2
      && rows.some(row => row.type === 'session_cancelled'));
    check('no_backend_calls', report.backend_calls.length === 0);
    report.status = 'passed';
  } catch (error) {
    report.error = String(error);
  } finally {
    const errors = [];
    for (const session of sessions) {
      try { await session.close('synthetic preflight cleanup'); } catch (error) { errors.push(String(error)); }
    }
    if (report.cleanup.created) {
      try {
        // This exact path was returned by our own mkdtemp, never caller data.
        await rm(report.temporary_directory, { recursive: true });
        try { await stat(report.temporary_directory); throw new Error('Temporary directory still exists'); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
        report.cleanup.removed = true;
      } catch (error) { errors.push(String(error)); }
    }
    if (errors.length) { report.cleanup.errors = errors; report.status = 'failed'; }
    if (report.backend_calls.length) report.status = 'failed';
  }
  return report;
}
