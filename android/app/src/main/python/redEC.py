"""redEC — port numpy/Android. Même API publique que la version
torch desktop (`from redEC import redEC_chain`). Backend nn_numpy.
"""
import sys
import numpy as np
from pathlib import Path

from nn_numpy import dec_forward as _dec, enc_forward as _enc

UNIT = 1024 * 1024  # 1 Mo


def _level_for_size(size: int) -> int:
    if size <= UNIT:                return 1
    if size <= UNIT * 1024:         return 2
    if size <= UNIT * 1024 ** 2:    return 3
    return 4


def redEC_unit_batched(chunks_list):
    """N chunks de 1 Mo → N hashes de 1024 o chacun."""
    N = len(chunks_list)
    first_hashes = np.zeros((N, 1024), dtype=np.uint8)
    for k in range(N):
        first_hashes[k] = np.frombuffer(chunks_list[k][:1024], np.uint8)
    lats = np.unpackbits(first_hashes, axis=1).reshape(N, 8, 32, 32)
    cases = _dec(lats)
    encs = _enc(cases)
    outs = np.packbits(encs.reshape(N, -1), axis=1)
    return [bytes(outs[k]) for k in range(N)]


def _apply_one_step(in_path: Path, out_path: Path, batch_size: int = 32, prog_cb=None, label: str = ""):
    """1 application de redEC : lit le fichier par chunks de 1 Mo, écrit 1 hash (1024 o) par chunk.
       Batch size réduit côté Android (mémoire mobile)."""
    in_size = in_path.stat().st_size
    n_chunks = (in_size + UNIT - 1) // UNIT
    with open(in_path, "rb") as in_f, open(out_path, "wb") as out_f:
        chunk_idx = 0
        while chunk_idx < n_chunks:
            this_n = min(batch_size, n_chunks - chunk_idx)
            batch = []
            for _ in range(this_n):
                chunk = in_f.read(UNIT)
                if len(chunk) < UNIT:
                    chunk = chunk + b'\x00' * (UNIT - len(chunk))
                batch.append(chunk)
            outs = redEC_unit_batched(batch)
            for h in outs:
                out_f.write(h)
            chunk_idx += this_n
            if prog_cb:
                prog_cb(chunk_idx, n_chunks, label)


def redEC_chain(input_path: Path, output_path: Path, work_dir: Path = None, prog_cb=None):
    """Applique redEC en chaîne (1 à 4 étapes selon la taille) jusqu'à obtenir 1 hash de 1024 o."""
    if work_dir is None:
        work_dir = output_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    in_size = input_path.stat().st_size
    level = _level_for_size(in_size)

    current = input_path
    intermediates = []
    for step in range(level):
        is_final = (step == level - 1)
        next_path = output_path if is_final else (work_dir / f"redEC_tmp_step{step+1}.bin")
        if not is_final:
            intermediates.append(next_path)
        # Batch size 4 sur mobile (256 sur desktop) — limite la mémoire pic
        # à ~1 Mo × 4 × 64 ch × 32 × 32 × 4 o ≈ ~100 Mo intermédiaire.
        _apply_one_step(current, next_path, batch_size=4, prog_cb=prog_cb, label=f"step {step+1}/{level}")
        if current != input_path:
            current.unlink(missing_ok=True)
        current = next_path

    final_bytes = output_path.read_bytes()[:1024]
    return level, final_bytes, in_size


if __name__ == "__main__":
    inp = sys.argv[1] if len(sys.argv) > 1 else "input.bin"
    out = sys.argv[2] if len(sys.argv) > 2 else "redEC_out.bin"
    level, h, in_size = redEC_chain(Path(inp), Path(out))
    print(f"redEC: input {in_size:,} o → Red{level} → hash {len(h)} o ({h.hex()[:32]}…)")
