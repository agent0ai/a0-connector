#!/usr/bin/env python3
"""Build and inspect an unpublished Linux payload; never installs or signs it."""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def source_fingerprint():
    entries = []
    for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "browser-registry-v1.json",
                 "release-policy-v1.json", "src", "scripts"):
        start = ROOT / name
        assert not start.is_symlink(), "Source symlinks are not permitted"
        for path in sorted(start.rglob("*") if start.is_dir() else [start]):
            if "__pycache__" in path.parts:
                continue
            assert not path.is_symlink(), "Source symlinks are not permitted"
            if path.is_file():
                entries.append([str(path.relative_to(ROOT)), sha256(path.read_bytes())])
    return sha256(json.dumps(entries, separators=(",", ":")).encode())


def payload(binary):
    assert 0 < len(binary) <= 512 * 1024 * 1024
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            member = tarfile.TarInfo("a0-browser-bridge")
            member.size, member.mode = len(binary), 0o755
            archive.addfile(member, io.BytesIO(binary))
    encoded = output.getvalue()
    with tarfile.open(fileobj=io.BytesIO(encoded), mode="r:gz") as archive:
        members = archive.getmembers()
        assert len(members) == 1 and members[0].isfile()
        assert archive.extractfile(members[0]).read() == binary
    return encoded


def build(cargo, output, target=None):
    assert platform.system() == "Linux", "Run on the target Linux build host"
    host = platform.machine()
    target = target or f"{host}-unknown-linux-gnu"
    arch = target.removesuffix("-unknown-linux-gnu")
    assert arch in {"x86_64", "aarch64"}, "Only x86-64 and ARM64 Linux are supported"
    assert cargo.is_absolute() and cargo.is_file(), "Supply an absolute Cargo path"
    assert output.is_absolute() and not output.exists(), "Choose a new absolute output directory"
    runner = []
    env = dict(os.environ)
    if arch != host:
        assert host == "x86_64" and arch == "aarch64", "Only x86-64 to ARM64 cross-build is supported"
        runner = ["/usr/bin/qemu-aarch64", "-L", "/"]
        env.update(CC_aarch64_unknown_linux_gnu="/usr/bin/aarch64-linux-gnu-gcc",
                   CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER="/usr/bin/aarch64-linux-gnu-gcc",
                   PKG_CONFIG_ALLOW_CROSS="1", PKG_CONFIG_SYSROOT_DIR="/",
                   PKG_CONFIG_LIBDIR="/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig")
        env.pop("PKG_CONFIG_PATH", None)

    def run(args, **kwargs):
        return subprocess.check_output([str(value) for value in args], cwd=ROOT,
                                       timeout=300, text=True, env=env, **kwargs).strip()

    before = source_fingerprint()
    run([cargo, "build", "--locked", "--offline", "--release", "--no-default-features",
         "--target", target, "--target-dir", ROOT / "target"])
    assert source_fingerprint() == before, "Source changed during build"
    executable = ROOT / "target" / target / "release/a0-browser-bridge"
    assert executable.is_file() and not executable.is_symlink()
    binary = executable.read_bytes()
    assert binary[:6] == b"\x7fELF\x02\x01"
    assert int.from_bytes(binary[18:20], "little") == {"x86_64": 62, "aarch64": 183}[arch]
    metadata = json.loads(run([*runner, executable, "metadata", "--json"]))
    assert metadata["channel"] == "stable" and metadata["platform"] == "linux"
    assert metadata["native_host"] == "io.agentzero.browser_bridge"
    self_test = json.loads(run([*runner, executable, "self-test", "--json"]))
    assert self_test["install_ready"] is True
    version = metadata["companion_version"]
    assert re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version)
    symbols = run(["/usr/bin/readelf", "--version-info", executable])
    versions = {match for match in re.findall(r"\bGLIBC_(\d+(?:\.\d+)+)", symbols)}
    assert versions, "Expected a glibc-linked Linux payload"
    minimum_glibc = max(versions, key=lambda value: tuple(map(int, value.split("."))))
    libraries = re.findall(r"\(NEEDED\).*\[([^]]+)\]", run(["/usr/bin/readelf", "--dynamic", executable]))
    archive = payload(binary)
    name = f"a0-browser-bridge-{version}-linux-{arch}-candidate.tar.gz"
    bootstrap = (ROOT / "scripts/install-linux.sh").read_bytes()
    bootstrap_name = f"a0-browser-bridge-{version}-linux-bootstrap-candidate.sh"
    receipt = {
        "contract": "a0.browser-bridge.linux-build-candidate.v1", "metadata": metadata, "architecture": arch,
        "target": target, "self_test_execution": "qemu-user" if runner else "native",
        "source_commit": run(["git", "rev-parse", "HEAD"]), "source_tree_sha256": before,
        "source_dirty": bool(run(["git", "status", "--porcelain", "--untracked-files=normal"])),
        "recipe_sha256": sha256(Path(__file__).read_bytes()),
        "rust_toolchain": run([cargo.parent / "rustc", "--version"]),
        "executable_sha256": sha256(binary), "executable_size": len(binary),
        "payload": name, "payload_sha256": sha256(archive), "payload_size": len(archive),
        "bootstrap": bootstrap_name, "bootstrap_sha256": sha256(bootstrap), "bootstrap_size": len(bootstrap),
        "minimum_glibc": minimum_glibc, "required_shared_libraries": libraries,
        "offline_self_test": self_test, "signed": False, "published": False,
        "installed": False, "production_ready": False,
    }
    output.mkdir(mode=0o700)
    with (output / name).open("xb") as stream:
        stream.write(archive)
    with (output / bootstrap_name).open("xb") as stream:
        stream.write(bootstrap)
    with (output / "BUILD.json").open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    with (output / "README.txt").open("x") as stream:
        stream.write(f"Unpublished Linux {arch} source candidate; requires glibc >= {minimum_glibc}, libdbus and desktop Secret Service.\n"
                     "This archive does not authorize installation or browser control. A new release version, reviewed Linux builder identity/recipe, "
                     "publisher-signed catalog, signed provenance, independent CLI pins and current Core admission policy are still required. "
                     "Never replace existing release artifacts.\n")
    print(json.dumps({"payload": str(output / name), "sha256": receipt["payload_sha256"],
                      "minimum_glibc": minimum_glibc, "production_ready": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", choices=["x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"])
    args = parser.parse_args()
    build(args.cargo, args.output, args.target)
