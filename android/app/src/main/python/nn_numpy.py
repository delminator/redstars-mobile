# Pure-numpy port du forward pass de redEC/redDEC.
#
# Cible : Android via Chaquopy, où PyTorch n'a pas de wheel. Le réseau
# fait ~27k params (4 Conv2d + GELU + sigmoid), donc on l'implémente
# sans framework. Numpy stride_tricks gère l'unfold des Conv 3×3 et
# einsum fait la multiplication. À CPU sur tel modeste : ~30 ms par
# batch de 1024 frames 32×32. Acceptable pour Red1.
#
# Le module charge les poids depuis weights_lat.npz / weights_img.npz
# au premier appel. Les shapes correspondent EXACTEMENT à torch :
#   - eN/dN.weight (Cout, Cin, K, K) avec K=3 pour {e,d}1, K=1 pour {e,d}2
#   - eN/dN.bias   (Cout,)
# Convertis depuis .pth par scripts/export_weights.py.

import numpy as np
from numpy.lib.stride_tricks import as_strided
from pathlib import Path

ROOT = Path(__file__).parent
_WEIGHTS = {}  # key 'lat' or 'img' → {layer_name: ndarray}


def _load_weights(kind: str) -> dict:
    """`kind` = 'lat' ou 'img'."""
    if kind not in _WEIGHTS:
        path = ROOT / f'weights_{kind}.npz'
        z = np.load(path)
        _WEIGHTS[kind] = {k: z[k] for k in z.files}
        z.close()
    return _WEIGHTS[kind]


def _conv2d_3x3_p1(x: np.ndarray, weight: np.ndarray, bias: np.ndarray) -> np.ndarray:
    """Conv2d kernel 3×3, padding=1, stride=1. Préserve H×W.
       x: (B, Cin, H, W) float32 ; weight: (Cout, Cin, 3, 3) ; bias: (Cout,)."""
    # Padding zero d'une bande de 1 pixel autour
    xp = np.pad(x, ((0, 0), (0, 0), (1, 1), (1, 1)))
    B, C, Hp, Wp = xp.shape
    H, W = Hp - 2, Wp - 2
    # Vue (B, Cin, H, W, 3, 3) sur xp sans copie, en jouant avec les strides.
    sB, sC, sH, sW = xp.strides
    patches = as_strided(
        xp,
        shape=(B, C, H, W, 3, 3),
        strides=(sB, sC, sH, sW, sH, sW),
        writeable=False,
    )
    # einsum : pour chaque (b, h, w), somme sur (cin, i, j) de patch * weight
    out = np.einsum('bchwij,ocij->bohw', patches, weight, optimize=True)
    out = out + bias[None, :, None, None]
    return out


def _conv2d_1x1(x: np.ndarray, weight: np.ndarray, bias: np.ndarray) -> np.ndarray:
    """Conv2d 1×1 = matmul sur la dim channels."""
    # weight: (Cout, Cin, 1, 1) → (Cout, Cin)
    w = weight[:, :, 0, 0]
    # einsum: x (B,Cin,H,W) * w (Cout,Cin) → (B,Cout,H,W)
    out = np.einsum('bchw,oc->bohw', x, w, optimize=True)
    out = out + bias[None, :, None, None]
    return out


# GELU — approximation "tanh" officielle torch (approximate='tanh').
# Sur les conv outputs réelles du réseau redEC, on a vérifié que
# l'argmax final est IDENTIQUE à torch erf-GELU (0/262144 pixels
# diff) — l'approx est suffisamment fidèle pour ne pas perturber la
# discretisation. Évite scipy.special.erf qui est ~10× plus lent.
_GELU_C  = np.float32(0.7978845608028654)   # sqrt(2/π)
_GELU_C2 = np.float32(0.044715)
def _gelu(x: np.ndarray) -> np.ndarray:
    return 0.5 * x * (1.0 + np.tanh(_GELU_C * (x + _GELU_C2 * x * x * x)))


def _sigmoid(x: np.ndarray) -> np.ndarray:
    # Stable : pour x grand positif → 1/(1+exp(-x)) sans overflow.
    out = np.empty_like(x)
    pos = x >= 0
    neg = ~pos
    out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
    ex = np.exp(x[neg])
    out[neg] = ex / (1.0 + ex)
    return out


def to_bits(idx: np.ndarray) -> np.ndarray:
    """idx (B, H, W) uint8 [0..255] → bits (B, 8, H, W) float32 [0|1].
       MSB → bit channel 0, LSB → channel 7 (cf. _BITS=torch.arange(8) en
       train_elim.py : (idx >> _BITS) & 1)."""
    # Forme (B, 1, H, W) ; broadcast avec bits (1, 8, 1, 1).
    bits = (np.arange(8, dtype=np.int64))[None, :, None, None]
    out = ((idx.astype(np.int64)[:, None, :, :] >> bits) & 1).astype(np.float32)
    return out


def dec_forward(z: np.ndarray) -> np.ndarray:
    """LATENT decode : (B, 8, 32, 32) bits → (B, 32, 32) palette indices.
       Pipeline torch : d2(gelu(d1(z))).argmax(1)
       Note : ce sont les poids du modèle `lat` (CK_LAT) qui sont utilisés
       pour les couches d1/d2 ici — c'est le contrat redEC/redDEC."""
    w = _load_weights('lat')
    z = z.astype(np.float32)
    h = _conv2d_3x3_p1(z, w['d1.weight'], w['d1.bias'])
    h = _gelu(h)
    o = _conv2d_1x1(h, w['d2.weight'], w['d2.bias'])  # (B, 256, 32, 32)
    return o.argmax(axis=1).astype(np.uint8)


def enc_forward(idx: np.ndarray) -> np.ndarray:
    """IMG encode : (B, 32, 32) palette indices → (B, 8, 32, 32) bits.
       Pipeline torch : round(sigmoid(e2(gelu(e1(to_bits(idx)))))).clamp(0,1)
       Poids du modèle `img` (CK_IMG) pour e1/e2."""
    w = _load_weights('img')
    x = to_bits(idx)
    h = _conv2d_3x3_p1(x, w['e1.weight'], w['e1.bias'])
    h = _gelu(h)
    o = _conv2d_1x1(h, w['e2.weight'], w['e2.bias'])
    o = _sigmoid(o)
    return np.round(np.clip(o, 0.0, 1.0)).astype(np.uint8)
