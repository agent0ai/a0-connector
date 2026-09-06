// Local release-signing preparation. Never installs, notarizes, or activates trust.
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { constants, copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
function run(program, args, options = {}) {
  return execFileSync(program, args, { cwd: root, encoding: 'utf8', timeout: 60_000,
    maxBuffer: 4 * 1024 * 1024, ...options });
}
function fingerprint() {
  const entries = [];
  function visit(relative) {
    const path = join(root, relative), info = lstatSync(path);
    assert(!info.isSymbolicLink(), 'Source symlinks are not permitted');
    if (info.isDirectory()) for (const name of readdirSync(path).sort()) visit(`${relative}/${name}`);
    else {
      assert(info.isFile());
      entries.push([relative, sha256(readFileSync(path))]);
    }
  }
  for (const name of ['Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'browser-registry-v1.json', 'release-policy-v1.json', 'src', 'scripts']) visit(name);
  return sha256(JSON.stringify(entries));
}

function main(args) {
  const options = {};
  for (let i = 0; i < args.length; i += 2) {
    assert(['--output', '--identity', '--team-id', '--architectures', '--cargo'].includes(args[i])
      && !options[args[i]] && args[i + 1], 'Supply output, identity SHA-1, team-id, architectures and cargo');
    options[args[i]] = args[i + 1];
  }
  assert.equal(process.platform, 'darwin', 'Run on the Mac signing host');
  const output = options['--output'], cargo = options['--cargo'];
  const identity = options['--identity'], team = options['--team-id'];
  assert(isAbsolute(output ?? '') && isAbsolute(cargo ?? ''));
  assert(!existsSync(output), 'Choose a new output directory; signed artifacts are never overwritten');
  assert(/^[A-F0-9]{40}$/.test(identity) && /^[A-Z0-9]{10}$/.test(team));
  assert(['arm64', 'universal'].includes(options['--architectures']));
  const identities = run('/usr/bin/security', ['find-identity', '-v', '-p', 'codesigning']);
  assert(identities.split('\n').some(line => line.includes(`${identity} "Developer ID Application:`)
    && line.includes(`(${team})"`)), 'Exact Developer ID Application signing identity is unavailable');
  const targets = options['--architectures'] === 'universal'
    ? ['aarch64-apple-darwin', 'x86_64-apple-darwin'] : ['aarch64-apple-darwin'];
  const installed = run(join(dirname(cargo), 'rustup'), ['target', 'list', '--installed']).trim().split('\n');
  assert(targets.every(target => installed.includes(target)), 'Required Rust targets are not installed; no toolchains are installed automatically');
  const source = fingerprint();
  mkdirSync(output, { mode: 0o700 });
  const binaries = [];
  for (const target of targets) {
    // Empty default features: never re-label a development companion as production.
    run(cargo, ['build', '--locked', '--offline', '--release', '--no-default-features', '--target', target],
      { stdio: 'inherit', timeout: 240_000 });
    const binary = join(root, 'target', target, 'release/a0-browser-bridge');
    assert(lstatSync(binary).isFile() && !lstatSync(binary).isSymbolicLink());
    binaries.push(binary);
  }
  assert.equal(fingerprint(), source, 'Source changed during build; discard incomplete output');
  const binary = join(output, 'a0-browser-bridge');
  if (binaries.length === 1) copyFileSync(binaries[0], binary, constants.COPYFILE_EXCL);
  else run('/usr/bin/lipo', ['-create', ...binaries, '-output', binary]);
  const architectures = run('/usr/bin/lipo', ['-archs', binary]).trim().split(/\s+/).sort();
  assert.deepEqual(architectures, targets.length === 1 ? ['arm64'] : ['arm64', 'x86_64']);
  const unsignedHash = sha256(readFileSync(binary));
  run('/usr/bin/codesign', ['--force', '--sign', identity, '--identifier', 'io.agentzero.browser_bridge',
    '--options', 'runtime', '--timestamp', binary], { stdio: 'inherit' });
  run('/usr/bin/codesign', ['--verify', '--strict', '--all-architectures', '--verbose=2', binary], { stdio: 'inherit' });
  // codesign display uses stderr; keep the exact bounded public evidence beside the candidate.
  const evidence = [];
  for (const architecture of architectures) {
    const display = spawnSync('/usr/bin/codesign', ['--display', '--verbose=4', '--arch', architecture, binary],
      { encoding: 'utf8', timeout: 30_000, maxBuffer: 64 * 1024 });
    assert(!display.error && display.status === 0, 'Unable to inspect the signed slice');
    const lines = display.stderr.split('\n');
    assert(lines.includes(`TeamIdentifier=${team}`));
    assert(lines.includes('Identifier=io.agentzero.browser_bridge'));
    assert(lines.some(line => line.startsWith('Authority=Developer ID Application:')));
    assert(lines.some(line => line.startsWith('Timestamp=') && line.length > 10), 'Secure signing timestamp missing');
    assert(lines.some(line => /flags=.*\bruntime\b/.test(line)), 'Hardened runtime missing');
    evidence.push({ architecture, display: display.stderr });
  }
  const metadata = JSON.parse(run(binary, ['metadata', '--json'], { timeout: 8_000, maxBuffer: 4096 }));
  assert.equal(metadata.contract, 'a0.browser-bridge.release-metadata.v1');
  assert.equal(metadata.channel, 'stable');
  assert.equal(metadata.native_host, 'io.agentzero.browser_bridge');
  assert.equal(metadata.platform, 'macos');
  const signedHash = sha256(readFileSync(binary));
  const name = `a0-browser-bridge-${metadata.companion_version}-macos-${options['--architectures']}-signed-candidate.zip`;
  const archive = join(output, name);
  run('/usr/bin/zip', ['-X', '-q', archive, 'a0-browser-bridge'], { cwd: output });
  assert.equal(run('/usr/bin/unzip', ['-Z1', archive]).trim(), 'a0-browser-bridge');
  assert.equal(sha256(readFileSync(binary)), signedHash);
  const archiveHash = sha256(readFileSync(archive));
  writeFileSync(join(output, 'SIGNING.json'), JSON.stringify({
    contract: 'a0.browser-bridge.macos-signing-candidate.v1', source_sha256: source,
    recipe_sha256: sha256(readFileSync(fileURLToPath(import.meta.url))),
    rust_toolchain: run(join(dirname(cargo), 'rustc'), ['--version']).trim(),
    cargo_version: run(cargo, ['--version']).trim(), metadata, architectures,
    identity_sha1: identity, team_id: team, unsigned_sha256: unsignedHash,
    signed_sha256: signedHash, archive: name, archive_sha256: archiveHash, signature_evidence: evidence,
    notarization: 'not_submitted', universal_release: architectures.length === 2,
    production_ready: false, installed: false,
  }, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  writeFileSync(join(output, 'SHA256SUMS'), `${signedHash}  a0-browser-bridge\n${archiveHash}  ${name}\n`, { flag: 'wx' });
  writeFileSync(join(output, 'README.txt'), 'Signed macOS candidate, not a production installation.\n\nDeveloper ID signing and hardened runtime were verified. This ZIP has not been submitted for notarization. It contains no installer or production release catalog. Production trust registries and browser admission remain unchanged. Do not replace an installed development companion with it. An arm64-only candidate does not satisfy the universal macOS release contract.\n', { flag: 'wx' });
  console.log(JSON.stringify({ archive, sha256: archiveHash, signed: true, notarized: false, production_ready: false }));
}

try { main(process.argv.slice(2)); }
catch (error) { console.error(`Mac signing preparation stopped: ${error.message}`); process.exitCode = 1; }
