import assert from 'node:assert/strict';
import { test } from 'node:test';
import { inflateSync, deflateSync, crc32 } from 'node:zlib';
import { cropPNG, cropCurrentFrame } from '../scripts/frame_crop.mjs';

// Valid Screenshot.url PNG, four distinct RGBA columns repeated on two rows.
const url = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAQAAAACCAYAAAB/qH1jAAAAF0lEQVR4nGP4z8DwHwwZ/v//D2MhCwAAIBAR7/vVKbUAAAAASUVORK5CYII=';
function fixture() {
  const arena = { controllerId: 'local-test', generation: 3,
    state: { screenshots: [{ id: 'actual-url-shape', url, width: 2, height: 1, originX: 100, originY: 200 }] } };
  const request = { controllerId: arena.controllerId, generation: 3,
    screenshotId: 'actual-url-shape', coordinateSpace: 'screenshot', rect: { x: 0, y: 0, width: 0.5, height: 1 } };
  return { arena, request };
}
function scanlines(url) {
  const image = Buffer.from(url.split(',')[1], 'base64'), parts = [];
  for (let at = 8; at < image.length;) {
    const size = image.readUInt32BE(at), type = image.toString('ascii', at + 4, at + 8);
    if (type === 'IDAT') parts.push(image.subarray(at + 8, at + 8 + size));
    at += size + 12;
  }
  return [...inflateSync(Buffer.concat(parts))];
}

test('real PNG regions produce distinct exact pixels with explicit logical/encoded mapping and full-frame proof', async () => {
  const { arena, request } = fixture();
  const red = await cropCurrentFrame(arena, request);
  const green = await cropCurrentFrame(arena, { ...request, rect: { ...request.rect, x: 0.5 } });
  assert.deepEqual(scanlines(red.image.url), [0, 255, 0, 0, 255, 0, 255, 0, 0, 255]);
  assert.deepEqual(scanlines(green.image.url), [0, 0, 255, 0, 255, 0, 0, 255, 0, 255]);
  assert.equal(red.full_frame, arena.state);
  assert.deepEqual(red.source, { controllerId: 'local-test', generation: 3, screenshotId: 'actual-url-shape' });
  assert.deepEqual(red.pixelRect, { x: 0, y: 0, width: 1, height: 2 });
  assert.equal(red.mapping.scaleX, 2);
  assert.equal(red.mapping.scaleY, 2);
  assert.deepEqual(red.mapping.screenOrigin, { x: 100, y: 200 });
  assert.match(red.mapping.windowMapping, /requires existing controller calibration/);
  assert.match(red.authorization, /no new observation or input binding/);
});

test('ROI rejects stale identity, missing dimensions and all invalid logical bounds before rendering', async () => {
  const { arena, request } = fixture();
  let calls = 0;
  const renderer = () => { calls++; throw new Error('must not render'); };
  for (const patch of [{ controllerId: 'other' }, { generation: 2 }, { generation: 3.1 }, { screenshotId: 'old' },
    { coordinateSpace: 'window' }, ...[
      { x: -1 }, { y: -1 }, { width: 0 }, { height: -1 }, { x: Infinity }, { x: NaN },
      { width: 3 }, { height: 2 }, { x: 1.75, width: 0.5 }, { y: 0.75, height: 0.5 },
    ].map(part => ({ rect: { ...request.rect, ...part } }))]) {
    await assert.rejects(cropCurrentFrame(arena, { ...request, ...patch }, renderer));
  }
  delete arena.state.screenshots[0].width;
  await assert.rejects(cropCurrentFrame(arena, request, renderer), /dimensions/);
  assert.equal(calls, 0);
});

test('ROI checks exact source again after a trusted async renderer and passes only image data/rectangle', async () => {
  for (const mutate of [a => { a.generation++; }, a => { a.state = null; }, a => { a.state = structuredClone(a.state); },
    a => { a.state.screenshots[0].url += 'changed'; }, a => { a.state.screenshots[0].width = 1; }]) {
    const { arena, request } = fixture();
    await assert.rejects(cropCurrentFrame(arena, { ...request, encodedSize: { width: 4, height: 2 } }, async data => {
      assert.deepEqual(Object.keys(data).sort(), ['pixelRect', 'url']);
      const image = cropPNG(data);
      await Promise.resolve(); mutate(arena);
      return image;
    }), /changed during rendering/);
  }
});

test('caller renderer requires explicit encoded dimensions and its output bounds are checked', async () => {
  const { arena, request } = fixture();
  const renderer = data => cropPNG(data);
  await assert.rejects(cropCurrentFrame(arena, request, renderer), /Encoded image dimensions/);
  const mapped = { ...request, encodedSize: { width: 4, height: 2 } };
  const result = await cropCurrentFrame(arena, mapped, renderer);
  assert.equal(result.image.width, 1);
  await assert.rejects(cropCurrentFrame(arena, mapped, () => ({ url, width: 4, height: 2 })), /requested pixel dimensions/);
  await assert.rejects(cropCurrentFrame(arena, mapped, () => ({ url: 'https://example.invalid/image', width: 1, height: 2 })), /crop image/);
});

test('async ROI rendering preserves the requested rectangle and encoded mapping', async () => {
  const { arena, request } = fixture();
  request.encodedSize = { width: 4, height: 2 };
  const result = await cropCurrentFrame(arena, request, async data => {
    const image = cropPNG(data);
    await Promise.resolve();
    request.rect.x = 0.5;
    request.encodedSize.width = 8;
    return image;
  });
  assert.deepEqual(result.rect, { x: 0, y: 0, width: 0.5, height: 1 });
  assert.deepEqual(result.pixelRect, { x: 0, y: 0, width: 1, height: 2 });
  assert.equal(result.mapping.encodedWidth, 4);
  assert.equal(result.mapping.scaleX, 2);
  assert.deepEqual(scanlines(result.image.url), [0, 255, 0, 0, 255, 0, 255, 0, 0, 255]);
});

test('PNG renderer rejects malformed, unsupported and out-of-bounds byte sources', () => {
  const pixelRect = { x: 0, y: 0, width: 1, height: 1 };
  for (const bad of ['data:image/jpeg;base64,AAAA', 'data:image/png;base64,AAAA', url.slice(0, -4), url.replace('F0lE', 'G0lE')]) {
    assert.throws(() => cropPNG({ url: bad, pixelRect }));
  }
  for (const change of [{ x: -1 }, { width: 0 }, { x: 4 }, { y: 2 }, { width: 1.5 }, { height: Infinity }]) {
    assert.throws(() => cropPNG({ url, pixelRect: { ...pixelRect, ...change } }), /outside PNG/);
  }
});

test('PNG row filters 0 through 4 preserve the exact cropped RGBA region', () => {
  const chunk = (type, data) => {
    const result = Buffer.alloc(data.length + 12); result.writeUInt32BE(data.length);
    result.write(type, 4); data.copy(result, 8); result.writeUInt32BE(crc32(result.subarray(4, -4)), result.length - 4);
    return result;
  };
  const rows = [Buffer.from([15, 35, 55, 255, 75, 95, 115, 255]), Buffer.from([20, 40, 60, 255, 80, 100, 120, 255])];
  for (let filter = 0; filter <= 4; filter++) {
    const raw = [];
    for (let row = 0; row < rows.length; row++) {
      raw.push(filter);
      for (let col = 0; col < rows[row].length; col++) {
        const a = col >= 4 ? rows[row][col - 4] : 0, b = row ? rows[row - 1][col] : 0;
        const c = row && col >= 4 ? rows[row - 1][col - 4] : 0;
        const p = a + b - c, da = Math.abs(p - a), db = Math.abs(p - b), dc = Math.abs(p - c);
        const predictor = [0, a, b, Math.floor((a + b) / 2), da <= db && da <= dc ? a : db <= dc ? b : c][filter];
        raw.push((rows[row][col] - predictor) & 255);
      }
    }
    const header = Buffer.from([0, 0, 0, 2, 0, 0, 0, 2, 8, 6, 0, 0, 0]);
    const image = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header),
      chunk('IDAT', deflateSync(Buffer.from(raw))), chunk('IEND', Buffer.alloc(0))]);
    const crop = cropPNG({ url: 'data:image/png;base64,' + image.toString('base64'), pixelRect: { x: 1, y: 0, width: 1, height: 2 } });
    assert.deepEqual(scanlines(crop.url), [0, 75, 95, 115, 255, 0, 80, 100, 120, 255]);
  }
});
