"""Installing Scalable's CLI: only a release signed with Scalable's key and matching its checksum."""

import base64
import hashlib
import io
import tarfile
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from brokersync import sc_install
from brokersync.sc_install import InstallError, install, verify_minisign

HERE = Path(__file__).parent / "fixtures" / "scalable"


def test_scalables_real_release_signature_verifies():
    sums = (HERE / "sc-v1.1.0-SHA256SUMS").read_bytes()
    sig = (HERE / "sc-v1.1.0-SHA256SUMS.minisig").read_text()
    assert verify_minisign(sums, sig) == "Scalable CLI sc-v1.1.0-SHA256SUMS"
    with pytest.raises(InstallError, match="Invalid signature"):
        verify_minisign(sums.replace(b"64aa3b", b"000000"), sig)


class Release:
    """A release signed with a test key, served like GitHub."""

    def __init__(self, tag="v9.9.9"):
        self.key = Ed25519PrivateKey.generate()
        self.keyid = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        raw = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public = base64.b64encode(b"Ed" + self.keyid + raw).decode()
        self.tag = tag
        self.files: dict[str, bytes] = {}
        script = f"#!/bin/sh\necho 'sc {tag[1:]}'\n".encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(f"sc-{tag}-linux-x86_64-gnu/sc")
            info.size, info.mode = len(script), 0o755
            tar.addfile(info, io.BytesIO(script))
        asset = f"sc-{tag}-linux-x86_64-gnu.tar.gz"
        self.files[asset] = buf.getvalue()
        sums = f"{hashlib.sha256(self.files[asset]).hexdigest()}  {asset}\n".encode()
        self.files[f"sc-{tag}-SHA256SUMS"] = sums
        self.files[f"sc-{tag}-SHA256SUMS.minisig"] = self.sign(sums, f"Scalable CLI sc-{tag}-SHA256SUMS")

    def sign(self, message: bytes, trusted: str) -> bytes:
        sig = self.key.sign(hashlib.blake2b(message, digest_size=64).digest())
        glob = self.key.sign(sig + trusted.encode())
        return (f"untrusted comment: test\n{base64.b64encode(b'ED' + self.keyid + sig).decode()}\n"
                f"trusted comment: {trusted}\n{base64.b64encode(glob).decode()}\n").encode()

    def client(self, fail: int = 0) -> httpx.Client:
        calls = {}

        def handle(request: httpx.Request) -> httpx.Response:
            calls[request.url.path] = calls.get(request.url.path, 0) + 1
            if calls[request.url.path] <= fail:
                return httpx.Response(504)
            if request.url.path.endswith("/releases/latest"):
                return httpx.Response(302, headers={
                    "location": f"https://github.com/ScalableCapital/scalable-cli/releases/tag/{self.tag}"})
            name = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, content=self.files[name]) if name in self.files else httpx.Response(404)
        return httpx.Client(transport=httpx.MockTransport(handle))


def test_install_verifies_and_keeps_an_up_to_date_binary(tmp_path, monkeypatch):
    release = Release()
    monkeypatch.setattr(sc_install, "PUBLIC_KEY", release.public)
    assert install(tmp_path, http=release.client(), machine="x86_64") == ("v9.9.9", True)
    assert (tmp_path / "sc").stat().st_mode & 0o111
    assert install(tmp_path, http=release.client(), machine="x86_64") == ("v9.9.9", False)


@pytest.mark.parametrize("tamper", ["tarball", "signature", "other_key"])
def test_a_tampered_release_installs_nothing(tmp_path, monkeypatch, tamper):
    release = Release()
    monkeypatch.setattr(sc_install, "PUBLIC_KEY", release.public)
    if tamper == "tarball":
        release.files["sc-v9.9.9-linux-x86_64-gnu.tar.gz"] += b"x"
    elif tamper == "signature":
        release.files["sc-v9.9.9-SHA256SUMS"] += b"0" * 64 + b"  evil\n"
    else:
        monkeypatch.setattr(sc_install, "PUBLIC_KEY", Release().public)
    with pytest.raises(InstallError):
        install(tmp_path, http=release.client(), machine="x86_64")
    assert not (tmp_path / "sc").exists()


def test_no_build_for_the_architecture(tmp_path):
    with pytest.raises(InstallError, match="no Linux build"):
        install(tmp_path, http=Release().client(), machine="riscv64")


def test_gateway_errors_of_github_are_tried_again(tmp_path, monkeypatch, _no_rate_limit_waits):
    release = Release()
    monkeypatch.setattr(sc_install, "PUBLIC_KEY", release.public)
    assert install(tmp_path, http=release.client(fail=2), machine="x86_64") == ("v9.9.9", True)
    assert _no_rate_limit_waits
    with pytest.raises(InstallError, match="HTTP 504"):
        install(tmp_path / "other", http=release.client(fail=9), machine="x86_64")
