// Publisher-side packaging only. No installation, credentials, or network writes.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { constants, copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const run = (file, args, extra = {}) => execFileSync(file, args, {
  cwd: root, encoding: 'utf8', timeout: 60_000, maxBuffer: 128 * 1024, ...extra,
});
function regular(path) {
  assert(isAbsolute(path), 'Absolute input required');
  const stat = lstatSync(path);
  assert(stat.isFile() && !stat.isSymbolicLink() && stat.nlink === 1, 'Regular single-link input required');
  return stat;
}
function payload(bytes) {
  // Closed one-entry USTAR profile; never shell out to platform-specific tar defaults.
  assert(bytes.length > 0 && bytes.length <= 512 * 1024 * 1024);
  const header = Buffer.alloc(512);
  const octal = (offset, width, value) => {
    const text = value.toString(8).padStart(width - 1, '0') + '\0';
    assert.equal(text.length, width); header.write(text, offset, width, 'ascii');
  };
  header.write('a0-browser-bridge', 0, 'ascii');
  octal(100, 8, 0o755); octal(108, 8, 0); octal(116, 8, 0);
  octal(124, 12, bytes.length); octal(136, 12, 0);
  header.fill(32, 148, 156); header[156] = 48;
  header.write('ustar\0', 257, 'ascii'); header.write('00', 263, 'ascii');
  const sum = header.reduce((total, byte) => total + byte, 0);
  header.write(sum.toString(8).padStart(6, '0') + '\0 ', 148, 8, 'ascii');
  return gzipSync(Buffer.concat([header, bytes,
    Buffer.alloc((512 - bytes.length % 512) % 512 + 1024)]), { level: 9 });
}
function main(args) {
  const opts = {};
  for (let i = 0; i < args.length; i += 2) {
    assert(['--candidate', '--output', '--identity', '--team-id', '--icon', '--notices'].includes(args[i])
      && !opts[args[i]] && args[i + 1], 'Invalid or duplicate argument');
    opts[args[i]] = args[i + 1];
  }
  assert.equal(process.platform, 'darwin');
  const input = opts['--candidate'], output = opts['--output'];
  assert(isAbsolute(input ?? '') && isAbsolute(output ?? '') && !existsSync(output));
  const identity = opts['--identity'], team = opts['--team-id'];
  assert(/^[A-F0-9]{40}$/.test(identity) && /^[A-Z0-9]{10}$/.test(team));
  const binary = join(input, 'a0-browser-bridge'); regular(binary);
  const receipt = JSON.parse(readFileSync(join(input, 'SIGNING.json'), 'utf8'));
  const bytes = readFileSync(binary);
  assert.equal(hash(bytes), receipt.signed_sha256);
  assert.deepEqual(receipt.architectures, ['arm64', 'x86_64']);
  assert.equal(receipt.metadata.channel, 'stable');
  assert.equal(receipt.team_id, team); assert.equal(receipt.identity_sha1, identity);
  const version = receipt.metadata.companion_version;
  assert(/^\d+\.\d+\.\d+$/.test(version));
  run('/usr/bin/codesign', ['--verify', '--strict', '--all-architectures', binary]);
  const installerSource = join(root, 'scripts/macos-installer.swift'); regular(installerSource);
  regular(opts['--notices']);
  const notices = readFileSync(opts['--notices']);
  assert(notices.length > 0 && notices.length < 8 * 1024 * 1024);
  const sourceHash = hash(readFileSync(installerSource));
  mkdirSync(output, { mode: 0o700 });
  const stage = join(output, 'disk-image'); mkdirSync(stage, { mode: 0o700 });
  const app = join(stage, 'Agent Zero Browser Setup.app');
  const contents = join(app, 'Contents'), macos = join(contents, 'MacOS'), resources = join(contents, 'Resources');
  mkdirSync(macos, { recursive: true }); mkdirSync(resources);
  copyFileSync(binary, join(resources, 'a0-browser-bridge'), constants.COPYFILE_EXCL);
  writeFileSync(join(resources, 'THIRD-PARTY-NOTICES.txt'), notices, { flag: 'wx' });
  writeFileSync(join(stage, 'THIRD-PARTY-NOTICES.txt'), notices, { flag: 'wx' });
  const slices = ['arm64', 'x86_64'].map(arch => {
    const slice = join(output, `setup-${arch}`);
    run('/usr/bin/xcrun', ['swiftc', '-O', '-target', `${arch}-apple-macosx13.0`,
      installerSource, '-o', slice], { timeout: 60_000 });
    return slice;
  });
  assert.equal(hash(readFileSync(installerSource)), sourceHash, 'Installer source changed during build');
  const launcher = join(macos, 'AgentZeroBrowserSetup');
  run('/usr/bin/lipo', ['-create', ...slices, '-output', launcher]);
  const plist = { CFBundleName: 'Agent Zero Browser Setup', CFBundleDisplayName: 'Agent Zero Browser Setup',
    CFBundleIdentifier: 'io.agentzero.browser_bridge.setup', CFBundleExecutable: 'AgentZeroBrowserSetup',
    CFBundlePackageType: 'APPL', CFBundleVersion: version, CFBundleShortVersionString: version,
    LSMinimumSystemVersion: '13.0', NSHighResolutionCapable: true };
  if (opts['--icon']) {
    regular(opts['--icon']); assert(opts['--icon'].endsWith('.icns'));
    copyFileSync(opts['--icon'], join(resources, 'AgentZero.icns'), constants.COPYFILE_EXCL);
    plist.CFBundleIconFile = 'AgentZero';
  }
  const plistPath = join(contents, 'Info.plist');
  writeFileSync(plistPath, JSON.stringify(plist), { flag: 'wx' });
  run('/usr/bin/plutil', ['-convert', 'xml1', plistPath]);
  run('/usr/bin/codesign', ['--sign', identity, '--options', 'runtime', '--timestamp', app]);
  run('/usr/bin/codesign', ['--verify', '--strict', '--deep', '--all-architectures', app]);
  assert.equal(hash(readFileSync(join(resources, 'a0-browser-bridge'))), receipt.signed_sha256,
    'Packaging must not re-sign or change the companion');
  writeFileSync(join(stage, 'Read me.txt'), 'Agent Zero Browser Setup\n\nOpen Agent Zero Browser Setup, then choose Install. No administrator password is needed. Run this on the computer where Chrome runs, even when Agent Zero runs in Docker. The installer verifies the signed release online and registers the companion for your user only. It does not change existing development pairing. After installation, pair the production extension once in its Settings.\n', { flag: 'wx' });
  const name = `a0-browser-bridge-${version}-macos-universal2.dmg`;
  const dmg = join(output, name);
  run('/usr/bin/hdiutil', ['create', '-quiet', '-volname', 'Agent Zero Browser Setup',
    '-srcfolder', stage, '-format', 'UDZO', dmg], { timeout: 60_000 });
  run('/usr/bin/codesign', ['--sign', identity, '--timestamp', dmg]);
  const archiveName = `a0-browser-bridge-${version}-macos-universal2.tar.gz`;
  const archive = payload(bytes);
  writeFileSync(join(output, archiveName), archive, { flag: 'wx' });
  writeFileSync(join(output, 'PACKAGING.json'), JSON.stringify({
    contract: 'a0.browser-bridge.macos-packaging.v1', version,
    executable_sha256: receipt.signed_sha256, executable_size: bytes.length,
    installer_source_sha256: sourceHash, notices_sha256: hash(notices), payload: archiveName,
    payload_sha256: hash(archive), payload_size: archive.length,
    installer: name, installer_pre_notary_sha256: hash(readFileSync(dmg)),
    notarization: 'not_submitted', published: false, installed: false,
  }, null, 2) + '\n', { flag: 'wx' });
  console.log(JSON.stringify({ installer: dmg, payload: join(output, archiveName), notarized: false }));
}
try { main(process.argv.slice(2)); }
catch (error) { console.error(`Mac packaging stopped: ${error.message}`); process.exitCode = 1; }
