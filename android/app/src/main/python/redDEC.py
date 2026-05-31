"""redDEC — port numpy/Android. Même API publique que la version
torch desktop (`from redDEC import redDEC ; redDEC(hash_bytes) -> bytes`).
Le backend neural passe par nn_numpy au lieu de PyTorch.
"""
import sys
import numpy as np
from pathlib import Path

from nn_numpy import dec_forward as _dec, enc_forward as _enc


def _cascade_snapshots(case_in: np.ndarray, n_states: int = 1024, N: int = 32):
    """Identique à la version desktop (pure numpy déjà)."""
    g = case_in.astype(np.int64).copy()
    states = np.zeros((n_states, N * N), dtype=np.uint8)
    states[0] = case_in.astype(np.uint8)
    for r in range(1, n_states):
        k = r - 1
        s = k % N
        offset = k % 256
        if k & 1:
            pcs = np.arange(N // 2)
            c1 = (2 * pcs + s) % N
            c2 = (2 * pcs + 1 + s) % N
            rows = np.arange(N)
            idx1 = (rows[:, None] * N + c1[None, :]).flatten()
            idx2 = (rows[:, None] * N + c2[None, :]).flatten()
        else:
            pcs = np.arange(N // 2)
            r1 = (2 * pcs + s) % N
            r2 = (2 * pcs + 1 + s) % N
            cols = np.arange(N)
            idx1 = (r1[:, None] * N + cols[None, :]).flatten()
            idx2 = (r2[:, None] * N + cols[None, :]).flatten()
        v1 = g[idx1]
        v2 = g[idx2]
        AA = (v1 >> 4) & 0xF
        BB = v1 & 0xF
        AB = (v2 >> 4) & 0xF
        BA = v2 & 0xF
        g[idx1] = ((((AA ^ AB ^ BA) << 4) | (BB ^ AB ^ BA)) + offset) & 0xFF
        g[idx2] = (((AB << 4) | BA) + offset) & 0xFF
        states[r] = g.astype(np.uint8)
    return states


def redDEC(hash_bytes) -> bytes:
    """1 hash (1024 o) → 1 Mo (1024 hashes concaténés)."""
    arr = np.frombuffer(bytes(hash_bytes), np.uint8)[:1024]
    if arr.size != 1024:
        raise ValueError(f"redDEC besoin de 1024 octets en entrée, reçu {arr.size}")
    lat = np.unpackbits(arr).reshape(8, 32, 32)
    img32 = _dec(lat[None])[0]
    states = _cascade_snapshots(img32.flatten(), n_states=1024, N=32)
    cases = states.reshape(1024, 32, 32)
    lats = _enc(cases)
    hashes = np.packbits(lats.reshape(1024, -1), axis=1)
    return hashes.tobytes()


if __name__ == "__main__":
    inp = sys.argv[1] if len(sys.argv) > 1 else "redEC_out.bin"
    out = sys.argv[2] if len(sys.argv) > 2 else "redDEC_out.bin"
    h = Path(inp).read_bytes()[:1024]
    result = redDEC(h)
    Path(out).write_bytes(result)
    print(f"redDEC: {len(h)} o → {len(result)} o ({out})")
