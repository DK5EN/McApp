#!/usr/bin/env node
// Pre-compress the built webapp for Caddy's `file_server { precompressed br gzip }`.
//
// Usage: node scripts/precompress_webapp.mjs <directory>
//
// For every text-like file of at least 1024 bytes, writes <file>.br (brotli q11)
// and <file>.gz (gzip level 9), keeping a sidecar only when it is at least 5 percent
// smaller than the source. Each sidecar's mtime is set equal to its source's so a
// sidecar is never newer than what it mirrors. Idempotent: existing .br/.gz files are
// never compressed again, and a second run rewrites the same bytes.
//
// .wasm is deliberately NOT in the list: ~107 MB of raw WASM would add about 70 MB to
// every tarball. Backlog item B16 handles WASM separately.
//
// Node stdlib only. Exits non-zero on any read/write error.

import { readdirSync, readFileSync, writeFileSync, statSync, utimesSync, rmSync } from 'node:fs';
import { join, extname, basename } from 'node:path';
import zlib from 'node:zlib';

const EXTENSIONS = new Set([
  '.js', '.mjs', '.css', '.html', '.json', '.svg', '.webmanifest', '.txt', '.map',
]);
const MIN_BYTES = 1024;
const MIN_SAVING = 0.05;

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (entry.isFile()) yield full;
  }
}

function writeSidecar(path, data, source) {
  writeFileSync(path, data);
  // Floor to whole milliseconds: passing Date values lets Node round UP, which
  // could land the sidecar a hair NEWER than its source.
  const mtimeSec = Math.floor(source.mtimeMs) / 1000;
  utimesSync(path, mtimeSec, mtimeSec);
}

function main() {
  const root = process.argv[2];
  if (!root) {
    console.error('usage: precompress_webapp.mjs <directory>');
    return 2;
  }
  let files = 0;
  let rawBytes = 0;
  let brBytes = 0;
  for (const file of walk(root)) {
    const name = basename(file);
    const ext = extname(name).toLowerCase();
    if (ext === '.br' || ext === '.gz') continue;
    // AppleDouble / Finder litter must never get a sidecar.
    if (name.startsWith('._') || name === '.DS_Store') continue;
    if (!EXTENSIONS.has(ext)) continue;
    const source = statSync(file);
    if (source.size < MIN_BYTES) continue;
    const raw = readFileSync(file);
    const br = zlib.brotliCompressSync(raw, {
      params: {
        [zlib.constants.BROTLI_PARAM_QUALITY]: 11,
        [zlib.constants.BROTLI_PARAM_SIZE_HINT]: raw.length,
      },
    });
    const gz = zlib.gzipSync(raw, { level: 9 });
    const limit = raw.length * (1 - MIN_SAVING);
    // A stale sidecar from an earlier run that no longer qualifies must go, or
    // Caddy would serve outdated bytes.
    if (br.length <= limit) writeSidecar(`${file}.br`, br, source);
    else rmSync(`${file}.br`, { force: true });
    if (gz.length <= limit) writeSidecar(`${file}.gz`, gz, source);
    else rmSync(`${file}.gz`, { force: true });
    files += 1;
    rawBytes += raw.length;
    brBytes += br.length <= limit ? br.length : raw.length;
  }
  console.log(`precompress: ${files} files, ${rawBytes} raw bytes, ${brBytes} br bytes`);
  return 0;
}

try {
  process.exitCode = main();
} catch (err) {
  console.error(`precompress: ${err && err.message ? err.message : err}`);
  process.exitCode = 1;
}
