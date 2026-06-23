#!/usr/bin/env python3
"""Codec per-bloc LOSSLESS GARANTI — numpy pur (sans torch), pour le helper.

Inférence via nn_numpy (poids weights_*.npz) → déployable sans torch.

Le réseau n'est PAS bijectif à 100 % sur des octets arbitraires (~1 octet faux
par Gio). On garantit donc le bit-exact par un SIDECAR DE CORRECTION : à
l'encode on décode le blob et on note les rares octets qui diffèrent dans une
petite table (offset, octet) ; au décode on applique ces patches → bit-exact
pour N'IMPORTE quel fichier. Surcoût : ~9 o par octet corrigé (négligeable).

Format du blob :
    MAGIC(4) | orig_len u64 BE(8) | [latents : ceil(n/1024)·1024 o] | trailer
    trailer = n_patches u32 BE(4) | n_patches × ( offset u64 BE(8) | octet(1) )

API :
    encode(data) -> bytes            decode(blob) -> bytes
    encode_file(in,out) -> (n, n_patches)   decode_file(in,out) -> n
"""
import os
import numpy as np
from nn_numpy import enc_forward, dec_forward

BLOCK = 1024
MAGIC = b"RSN1"
HEADER = 12                      # MAGIC(4) + orig_len u64(8)


def _enc_blocks(raw):
    """raw uint8 (multiple de BLOCK) -> (z bits (b,8,32,32), octets latents packés)."""
    z = enc_forward(raw.reshape(-1, 32, 32))
    return z, np.packbits(z.reshape(len(z), -1), axis=1).tobytes()


def _dec_bytes(lat_bytes):
    """octets latents (multiple de BLOCK) -> raw uint8 reconstruit (b*1024,)."""
    lat = np.frombuffer(lat_bytes, np.uint8).reshape(-1, BLOCK)
    z = np.unpackbits(lat, axis=1).reshape(-1, 8, 32, 32)
    return dec_forward(z).reshape(-1)


def _patch_trailer(patches):
    out = bytearray(len(patches).to_bytes(4, "big"))
    for off, b in patches:
        out += off.to_bytes(8, "big") + bytes([b])
    return bytes(out)


# ----------------------------- mémoire (petits fichiers) -----------------------------

def encode(data: bytes) -> bytes:
    orig = np.frombuffer(data, np.uint8)
    n = len(orig)
    pad = (-n) % BLOCK
    raw = np.concatenate([orig, np.zeros(pad, np.uint8)]) if pad else orig
    z, lat = _enc_blocks(raw)
    back = dec_forward(z).reshape(-1)[:n]
    mism = np.nonzero(back != orig)[0]
    patches = [(int(o), int(orig[o])) for o in mism]
    return MAGIC + n.to_bytes(8, "big") + lat + _patch_trailer(patches)


def decode(blob: bytes) -> bytes:
    assert blob[:4] == MAGIC, "format inconnu"
    n = int.from_bytes(blob[4:HEADER], "big")
    nblocks = (n + BLOCK - 1) // BLOCK
    body = blob[HEADER:HEADER + nblocks * BLOCK]
    out = bytearray(_dec_bytes(body)[:n].tobytes())
    trailer = blob[HEADER + nblocks * BLOCK:]
    npatch = int.from_bytes(trailer[:4], "big")
    pos = 4
    for _ in range(npatch):
        off = int.from_bytes(trailer[pos:pos + 8], "big")
        out[off] = trailer[pos + 8]
        pos += 9
    return bytes(out)


# ----------------------------- fichier→fichier chunké (gros fichiers) -----------------------------

def encode_file(in_path, out_path, batch: int = 512):
    """Encode streaming + sidecar. Renvoie (n_in, n_patches)."""
    n = os.path.getsize(in_path)
    patches = []
    goff = 0
    with open(in_path, "rb") as fi, open(out_path, "wb") as fo:
        fo.write(MAGIC + n.to_bytes(8, "big"))
        while True:
            chunk = fi.read(BLOCK * batch)
            if not chunk:
                break
            orig = np.frombuffer(chunk, np.uint8)
            clen = len(orig)
            pad = (-clen) % BLOCK
            raw = np.concatenate([orig, np.zeros(pad, np.uint8)]) if pad else orig
            z, lat = _enc_blocks(raw)
            fo.write(lat)
            back = dec_forward(z).reshape(-1)[:clen]
            for idx in np.nonzero(back != orig)[0]:
                patches.append((goff + int(idx), int(orig[idx])))
            goff += clen
        fo.write(_patch_trailer(patches))
    return n, len(patches)


def decode_file(in_path, out_path, batch: int = 512):
    """Decode streaming + application du sidecar. Renvoie n (longueur d'origine)."""
    with open(in_path, "rb") as fi:
        head = fi.read(HEADER)
        assert head[:4] == MAGIC, "format inconnu"
        n = int.from_bytes(head[4:HEADER], "big")
        nblocks = (n + BLOCK - 1) // BLOCK
        body_size = nblocks * BLOCK
        read = written = 0
        with open(out_path, "wb") as fo:
            while read < body_size:
                want = min(BLOCK * batch, body_size - read)
                chunk = fi.read(want)
                if not chunk:
                    break
                read += len(chunk)
                raw = _dec_bytes(chunk).tobytes()
                rem = n - written
                if len(raw) > rem:
                    raw = raw[:rem]
                fo.write(raw)
                written += len(raw)
        trailer = fi.read()
    npatch = int.from_bytes(trailer[:4], "big")
    if npatch:
        pos = 4
        with open(out_path, "r+b") as f:
            for _ in range(npatch):
                off = int.from_bytes(trailer[pos:pos + 8], "big")
                f.seek(off)
                f.write(bytes([trailer[pos + 8]]))
                pos += 9
    return n


if __name__ == "__main__":
    for sz in (1024, 4096, 100000):
        data = os.urandom(sz)
        blob = encode(data)
        back = decode(blob)
        print(f"{sz:7d} o -> blob {len(blob)} o -> {'LOSSLESS' if back == data else 'PERTE'}")
