/** Read-only PNG crops of a caller-selected region in the CURRENT full frame.
 * No locator, input, OCR, browser, Buffer global, or third-party dependency.
 */
import { inflateSync, deflateSync } from 'node:zlib';

const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
function decodeURL(url) {
  if (typeof url !== 'string' || !/^data:image\/png;base64,[A-Za-z0-9+/]*={0,2}$/.test(url)) {
    throw new Error('PNG base64 Screenshot.url required; other formats need a caller renderer');
  }
  const base64 = url.slice(url.indexOf(',') + 1);
  if (base64.length % 4 || base64.length > 48 * 1024 * 1024) throw new Error('Invalid or oversized PNG');
  const bytes = new Uint8Array(base64.length / 4 * 3 - (base64.endsWith('==') ? 2 : base64.endsWith('=') ? 1 : 0));
  let value = 0, bits = 0, at = 0;
  for (const char of base64) {
    if (char === '=') break;
    value = (value << 6) | alphabet.indexOf(char); bits += 6;
    if (bits >= 8) { bits -= 8; bytes[at++] = (value >>> bits) & 255; }
  }
  return bytes;
}
function encodeURL(bytes) {
  const chunks = [];
  for (let i = 0; i < bytes.length; i += 3) {
    const value = (bytes[i] << 16) | ((bytes[i + 1] ?? 0) << 8) | (bytes[i + 2] ?? 0);
    chunks.push(alphabet[value >>> 18], alphabet[(value >>> 12) & 63],
      i + 1 < bytes.length ? alphabet[(value >>> 6) & 63] : '=', i + 2 < bytes.length ? alphabet[value & 63] : '=');
  }
  return 'data:image/png;base64,' + chunks.join('');
}
const u32 = (bytes, offset) => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(offset);
const set32 = (bytes, offset, value) => new DataView(bytes.buffer).setUint32(offset, value);
function crc(bytes) {
  let value = 0xffffffff;
  for (const byte of bytes) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0);
  }
  return (value ^ 0xffffffff) >>> 0;
}
function chunk(type, data) {
  const result = new Uint8Array(data.length + 12);
  set32(result, 0, data.length);
  result.set([...type].map(char => char.charCodeAt(0)), 4);
  result.set(data, 8);
  set32(result, result.length - 4, crc(result.subarray(4, result.length - 4)));
  return result;
}
function concat(parts) {
  const bytes = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let offset = 0;
  for (const part of parts) { bytes.set(part, offset); offset += part.length; }
  return bytes;
}
function png(url) {
  const bytes = decodeURL(url);
  if (bytes.length < 33 || [...bytes.subarray(0, 8)].join(',') !== '137,80,78,71,13,10,26,10') throw new Error('Invalid PNG signature');
  const parts = [], ancillary = [];
  let header = null, ended = false;
  for (let offset = 8; offset < bytes.length;) {
    if (offset + 12 > bytes.length) throw new Error('Truncated PNG');
    const length = u32(bytes, offset), end = offset + length + 12;
    if (end > bytes.length) throw new Error('Truncated PNG chunk');
    const type = String.fromCharCode(...bytes.subarray(offset + 4, offset + 8));
    if (crc(bytes.subarray(offset + 4, end - 4)) !== u32(bytes, end - 4)) throw new Error('PNG checksum mismatch');
    const data = bytes.slice(offset + 8, end - 4);
    if (type === 'IHDR') {
      if (header || offset !== 8 || length !== 13) throw new Error('Invalid PNG header');
      header = data;
    } else if (type === 'IDAT') parts.push(data);
    else if (type === 'IEND') { ended = end === bytes.length; break; }
    else if (['PLTE', 'tRNS', 'sRGB', 'gAMA'].includes(type)) ancillary.push(chunk(type, data));
    else if (type[0] === type[0].toUpperCase()) throw new Error('Unsupported critical PNG chunk');
    offset = end;
  }
  if (!header || !ended || !parts.length) throw new Error('Incomplete PNG');
  const width = u32(header, 0), height = u32(header, 4), channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[header[9]];
  if (!width || !height || width * height > 16 * 1024 * 1024 || header[8] !== 8 ||
      !channels || header[10] || header[11] || header[12]) throw new Error('Only bounded, noninterlaced 8-bit PNG supported');
  return { width, height, channels, header, parts, ancillary };
}

// Default trusted renderer. Coordinates here are encoded image pixels.
export function cropPNG({ url, pixelRect }) {
  const source = png(url), { x, y, width, height } = pixelRect;
  if (![x, y, width, height].every(Number.isInteger) || x < 0 || y < 0 || width <= 0 || height <= 0 ||
      x + width > source.width || y + height > source.height) throw new Error('Pixel crop outside PNG');
  const stride = source.width * source.channels, expected = (stride + 1) * source.height;
  const raw = inflateSync(concat(source.parts), { maxOutputLength: expected });
  if (raw.length !== expected) throw new Error('PNG scanline size mismatch');
  const pixels = new Uint8Array(stride * source.height);
  const paeth = (a, b, c) => {
    const p = a + b - c, da = Math.abs(p - a), db = Math.abs(p - b), dc = Math.abs(p - c);
    return da <= db && da <= dc ? a : db <= dc ? b : c;
  };
  for (let row = 0; row < source.height; row++) {
    const filter = raw[row * (stride + 1)];
    if (filter > 4) throw new Error('Invalid PNG row filter');
    for (let col = 0; col < stride; col++) {
      const at = row * stride + col, left = col >= source.channels ? pixels[at - source.channels] : 0;
      const up = row ? pixels[at - stride] : 0, corner = row && col >= source.channels ? pixels[at - stride - source.channels] : 0;
      const predictor = filter === 0 ? 0 : filter === 1 ? left : filter === 2 ? up :
        filter === 3 ? Math.floor((left + up) / 2) : paeth(left, up, corner);
      pixels[at] = (raw[row * (stride + 1) + col + 1] + predictor) & 255;
    }
  }
  const cropStride = width * source.channels, output = new Uint8Array((cropStride + 1) * height);
  for (let row = 0; row < height; row++) {
    const start = (y + row) * stride + x * source.channels;
    output.set(pixels.subarray(start, start + cropStride), row * (cropStride + 1) + 1);
  }
  const header = source.header.slice(); set32(header, 0, width); set32(header, 4, height);
  return { url: encodeURL(concat([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk('IHDR', header), ...source.ancillary, chunk('IDAT', deflateSync(output)), chunk('IEND', new Uint8Array())])), width, height };
}

export async function cropCurrentFrame(arena, request, renderer = cropPNG) {
  const { controllerId, generation, screenshotId, coordinateSpace, rect: requestedRect } = request ?? {};
  const rect = requestedRect && { ...requestedRect };
  const frame = arena.state;
  const current = () => frame && arena.state === frame && arena.controllerId === controllerId && arena.generation === generation;
  if (!Number.isInteger(generation) || !current()) throw new Error('Crop source is not the current controller/frame generation');
  const shot = frame.screenshots?.find(item => item.id === screenshotId);
  if (!shot || coordinateSpace !== 'screenshot' || typeof renderer !== 'function') throw new Error('Explicit current screenshot and screenshot coordinate space required');
  if (![shot.width, shot.height].every(value => Number.isFinite(value) && value > 0) || !rect ||
      ![rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) || rect.x < 0 || rect.y < 0 ||
      rect.width <= 0 || rect.height <= 0 || rect.x + rect.width > shot.width || rect.y + rect.height > shot.height) {
    throw new Error('Logical crop bounds/dimensions invalid');
  }
  // The default PNG path derives encoded dimensions from actual Screenshot.url.
  // Other formats require caller-provided, explicit encoded dimensions and renderer.
  const encoded = renderer === cropPNG ? png(shot.url) : { ...request.encodedSize };
  if (![encoded?.width, encoded?.height].every(value => Number.isInteger(value) && value > 0) ||
      encoded.width * encoded.height > 16 * 1024 * 1024) throw new Error('Encoded image dimensions required');
  const scaleX = encoded.width / shot.width, scaleY = encoded.height / shot.height;
  const sourceIdentity = JSON.stringify([shot.id, shot.url, shot.width, shot.height, shot.originX, shot.originY]);
  const x = Math.floor(rect.x * scaleX), y = Math.floor(rect.y * scaleY);
  const pixelRect = { x, y, width: Math.ceil((rect.x + rect.width) * scaleX) - x,
    height: Math.ceil((rect.y + rect.height) * scaleY) - y };
  if (![pixelRect.x, pixelRect.y, pixelRect.width, pixelRect.height].every(Number.isInteger) ||
      x < 0 || y < 0 || pixelRect.width <= 0 || pixelRect.height <= 0 ||
      x + pixelRect.width > encoded.width || y + pixelRect.height > encoded.height) throw new Error('Mapped crop outside encoded image bounds');
  const image = await renderer({ url: shot.url, pixelRect: { ...pixelRect } });
  if (!current() || frame.screenshots?.find(item => item.id === screenshotId) !== shot ||
      JSON.stringify([shot.id, shot.url, shot.width, shot.height, shot.originX, shot.originY]) !== sourceIdentity) throw new Error('Crop source changed during rendering');
  if (!image || typeof image.url !== 'string' || !image.url.startsWith('data:image/') ||
      image.width !== pixelRect.width || image.height !== pixelRect.height) throw new Error('Renderer must return a crop image of the requested pixel dimensions');
  return { source: { controllerId, generation, screenshotId }, coordinateSpace,
    rect: { ...rect }, pixelRect, mapping: { logicalWidth: shot.width, logicalHeight: shot.height,
      encodedWidth: encoded.width, encodedHeight: encoded.height, scaleX, scaleY,
      screenOrigin: Number.isFinite(shot.originX) && Number.isFinite(shot.originY) ? { x: shot.originX, y: shot.originY } : null,
      windowMapping: 'requires existing controller calibration; screen origin is not calibration' },
    image, full_frame: frame, authorization: 'read_only_crop; inspect full frame; no new observation or input binding' };
}
