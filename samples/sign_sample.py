"""Sign the sample package's sealed batches with Sample Co's demo key, in TrustEvidence's signature format.

    python samples/sign_sample.py

TrustEvidence signs each sealed batch statement when its evidence signer is switched on (Ed25519, or ES256 in Azure
Key Vault). The sample package was exported with signing off; re-exporting it signed would give every evidence record a
new id and invalidate the saved evaluation runs, so this script adds the signatures afterwards, exactly as an export
with an Ed25519 signer would carry them:

* each batch: "signature" (base64 Ed25519 over the statement's UTF-8 bytes), "signature_alg", "signing_key_id";
* proofs.json "keys": the public key and its SHA-256 fingerprint;
* manifest.json and SHA256SUMS.txt updated for the new proofs.json, and manifest.sig.json (a signature over the
  manifest).

The evidence records do not change. Sample Co is fictional: the private key is generated once, kept in the git-ignored
file .sample-signing-seed, and never published; its public key is published in samples/sample-co-signing-key.json, as a
real company would publish its key on its website. Development only: needs the `cryptography` package.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "samples" / "sample-co-evidence-package.zip"
SEED_FILE = ROOT / ".sample-signing-seed"
PUBLIC_KEY_FILE = ROOT / "samples" / "sample-co-signing-key.json"


def _key() -> ed25519.Ed25519PrivateKey:
    if SEED_FILE.exists():
        seed = base64.b64decode(SEED_FILE.read_text().strip())
    else:
        seed = os.urandom(32)
        SEED_FILE.write_text(base64.b64encode(seed).decode() + "\n")
    return ed25519.Ed25519PrivateKey.from_private_bytes(seed)


def main() -> None:
    private = _key()
    raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_id = "ed25519:" + hashlib.sha256(raw).hexdigest()[:16]  # TrustEvidence's key id format
    key_info = {"alg": "Ed25519", "key_id": key_id, "public_key": base64.b64encode(raw).decode(),
                "fingerprint_sha256": hashlib.sha256(raw).hexdigest()}

    with zipfile.ZipFile(PACKAGE) as z:
        infos = {i.filename: i for i in z.infolist()}
        files = {name: z.read(name) for name in infos}
    proofs = json.loads(files["proofs.json"])
    for b in proofs["batches"]:
        b["signature"] = base64.b64encode(private.sign(b["statement"].encode("utf-8"))).decode()
        b["signature_alg"], b["signing_key_id"] = "Ed25519", key_id
    proofs["keys"] = [key_info]
    files["proofs.json"] = json.dumps(proofs, indent=2, sort_keys=True).encode("utf-8")

    manifest = json.loads(files["manifest.json"])
    manifest["files"]["proofs.json"] = {"sha256": hashlib.sha256(files["proofs.json"]).hexdigest(), "bytes": len(files["proofs.json"])}
    files["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    files["SHA256SUMS.txt"] = ("".join(f"{v['sha256']}  {k}\n" for k, v in manifest["files"].items())
                               + f"{hashlib.sha256(files['manifest.json']).hexdigest()}  manifest.json\n").encode("utf-8")
    files["manifest.sig.json"] = json.dumps({"signed_file": "manifest.json", "alg": "Ed25519", "key_id": key_id,
                                             "signature": base64.b64encode(private.sign(files["manifest.json"])).decode()},
                                            indent=2, sort_keys=True).encode("utf-8")

    stamp = infos["manifest.json"].date_time
    with zipfile.ZipFile(PACKAGE, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=infos[name].date_time if name in infos else stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[name])

    PUBLIC_KEY_FILE.write_text(json.dumps({
        "owner": "Sample Co (fictional)", **key_info,
        "note": "The public key Sample Co signs its sealed evidence batches with. A real company publishes this (or "
                "just its fingerprint) on its website or trust page, so customers can check proof packs against it.",
    }, indent=2) + "\n", encoding="utf-8")
    print(f"signed {len(proofs['batches'])} batch(es) with {key_id}; fingerprint {key_info['fingerprint_sha256']}")


if __name__ == "__main__":
    main()
