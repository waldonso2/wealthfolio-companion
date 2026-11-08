"""Installs Scalable's official CLI (``sc``) from its GitHub release, verified.

Only Scalable's own release assets are used: the checksum list
``sc-<tag>-SHA256SUMS`` must carry a valid minisign signature of Scalable's
release key (pinned below, as published in the CLI's README), and the tarball
must match its checksum. Anything else aborts before a file is written. The
binary goes to ``<dir>/sc``; an installed binary of the same version is kept.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import platform
import re
import subprocess
import tarfile
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from brokersync import ratelimit

REPO = "https://github.com/ScalableCapital/scalable-cli"
# Scalable Capital's minisign release signing key (README of scalable-cli).
PUBLIC_KEY = "RWRKuuSASIzbSYpuU5gdXeTkXirJBl5+XVXLP6E60hBUUKZ5HPIGjV8b"
ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


# GitHub's release pages answer 5xx now and then; a read is safe to repeat.
RETRY = (429, 500, 502, 503, 504)


def _get(http: httpx.Client, url: str, *, follow_redirects: bool) -> httpx.Response:
    return ratelimit.with_backoff(lambda: http.get(url, follow_redirects=follow_redirects), statuses=RETRY)


class InstallError(Exception):
    pass


def verify_minisign(message: bytes, signature: str, public_key: str | None = None) -> str:
    """Check a minisign signature (Scalable's release key by default); returns its trusted comment."""
    pk = base64.b64decode(public_key or PUBLIC_KEY)
    if len(pk) != 42 or pk[:2] != b"Ed":
        raise InstallError("Invalid minisign public key.")
    lines = [x.strip() for x in signature.strip().splitlines()]
    if len(lines) != 4 or not lines[2].startswith("trusted comment: "):
        raise InstallError("Invalid minisign signature file.")
    sig = base64.b64decode(lines[1])
    if len(sig) != 74 or sig[:2] not in (b"Ed", b"ED") or sig[2:10] != pk[2:10]:
        raise InstallError("The signature is not from Scalable's release key.")
    key = Ed25519PublicKey.from_public_bytes(pk[10:])
    signed = hashlib.blake2b(message, digest_size=64).digest() if sig[:2] == b"ED" else message
    trusted = lines[2][len("trusted comment: "):]
    try:
        key.verify(sig[10:], signed)
        key.verify(base64.b64decode(lines[3]), sig[10:] + trusted.encode())
    except InvalidSignature as e:
        raise InstallError("Invalid signature of the release checksums.") from e
    return trusted


def latest_tag(http: httpx.Client) -> str:
    r = _get(http, f"{REPO}/releases/latest", follow_redirects=False)
    m = re.search(r"/releases/tag/(v[\w.\-]+)$", r.headers.get("location", ""))
    if not m:
        raise InstallError(f"Latest Scalable CLI release not found (HTTP {r.status_code}).")
    return m.group(1)


def installed_version(binary: Path) -> str | None:
    if not binary.exists():
        return None
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", out)
    return f"v{m.group(1)}" if m else None


def install(directory: Path, *, http: httpx.Client | None = None, machine: str | None = None) -> tuple[str, bool]:
    """Install the latest ``sc`` into ``directory``; returns (version, whether it was newly installed)."""
    arch = ARCH.get((machine or platform.machine()).lower())
    if not arch:
        raise InstallError(f"Scalable CLI has no Linux build for {machine or platform.machine()}.")
    http = http or httpx.Client(timeout=120)
    binary = Path(directory) / "sc"
    tag = latest_tag(http)
    if installed_version(binary) == tag:
        return tag, False

    def get(name: str) -> bytes:
        r = _get(http, f"{REPO}/releases/download/{tag}/{name}", follow_redirects=True)
        if r.status_code != 200:
            raise InstallError(f"Download of {name} failed (HTTP {r.status_code}).")
        return r.content

    sums_name = f"sc-{tag}-SHA256SUMS"
    sums = get(sums_name)
    trusted = verify_minisign(sums, get(f"{sums_name}.minisig").decode())
    if sums_name not in trusted:
        raise InstallError("The signature belongs to another release.")
    asset = f"sc-{tag}-linux-{arch}-gnu.tar.gz"
    expected = next((line.split()[0] for line in sums.decode().splitlines() if line.split()[1:] == [asset]), None)
    if not expected:
        raise InstallError(f"{asset} is not in the signed checksums.")
    tarball = get(asset)
    if hashlib.sha256(tarball).hexdigest() != expected:
        raise InstallError(f"Checksum of {asset} doesn't match.")
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        member = tar.getmember(f"sc-{tag}-linux-{arch}-gnu/sc")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise InstallError(f"{asset} holds no file sc.")
        data = extracted.read()
    Path(directory).mkdir(parents=True, exist_ok=True)
    tmp = binary.with_suffix(".new")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o755)  # noqa: S103 - a program, verified above; root-owned directory
    os.replace(tmp, binary)
    return tag, True
