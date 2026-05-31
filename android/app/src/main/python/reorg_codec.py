#!/usr/bin/env python3
"""Réorganisation latent 4D — codec réversible, size-preserving (AUCUNE redondance).
   Cascade 256 étapes : à chaque étape k, on apparie les cases (sens VARIÉ : vertical/horizontal
   + décalage tournant = fonction de k pour étaler au max), on mélange chaque paire par
   xorbidirPair (mélange 2 couleurs : AA'=AA⊕AB⊕BA, BB'=BB⊕AB⊕BA, AB/BA conservés),
   puis on ajoute l'index k (cycle prédictible). Réversible : décodage rejoue k=255→0
   avec sub puis xorbidirPair (involution). 0 index stocké, taille d'entrée = taille de sortie.
   Usage : python3 reorg_codec.py   (auto-test réversibilité + diffusion)"""
import numpy as np

def xorbidir(c1, c2):                       # mélange 2 octets (2 couleurs), involutif
    AA=(c1>>4)&0xF; BB=c1&0xF; AB=(c2>>4)&0xF; BA=c2&0xF
    return ((((AA^AB^BA)<<4)|(BB^AB^BA))&0xFF, ((AB<<4)|BA)&0xFF)

def _pairs(k, N):
    """Appariement de l'étape k : partition VALIDE (chaque case dans 1 paire), sens varié."""
    s = k % N
    out=[]
    if k & 1:                               # étape impaire : paires HORIZONTALES (décalage colonne s)
        for row in range(N):
            for pc in range(N//2):
                c1=(2*pc+s)%N; c2=(2*pc+1+s)%N
                out.append((row*N+c1, row*N+c2))
    else:                                   # étape paire : paires VERTICALES (décalage ligne s)
        for col in range(N):
            for pr in range(N//2):
                r1=(2*pr+s)%N; r2=(2*pr+1+s)%N
                out.append((r1*N+col, r2*N+col))
    return out

def reorg_encode(grid, N, rounds=256):
    g=grid.astype(np.int64).copy()
    for k in range(rounds):
        for i1,i2 in _pairs(k,N):
            a,b=xorbidir(int(g[i1]),int(g[i2])); g[i1]=(a+k)&0xFF; g[i2]=(b+k)&0xFF
    return g.astype(np.uint8)

def reorg_decode(grid, N, rounds=256):
    g=grid.astype(np.int64).copy()
    for k in range(rounds-1,-1,-1):
        for i1,i2 in _pairs(k,N):
            a=(int(g[i1])-k)&0xFF; b=(int(g[i2])-k)&0xFF
            x,y=xorbidir(a,b); g[i1]=x; g[i2]=y
    return g.astype(np.uint8)

if __name__=="__main__":
    N=32; rng=np.random.default_rng(0); fails=0; avals=[]
    for t in range(5):
        src=rng.integers(0,256,N*N,dtype=np.uint8)
        enc=reorg_encode(src,N); dec=reorg_decode(enc,N)
        ok=bool((dec==src).all()); fails+=0 if ok else 1
        # avalanche : 1 bit flippé en entrée -> combien d'octets changent en sortie
        src2=src.copy(); src2[0]^=1; enc2=reorg_encode(src2,N)
        avals.append(int((enc!=enc2).sum()))
        print(f"essai {t}: réversible={ok} ; avalanche (1 bit -> octets changés) = {avals[-1]}/{N*N}")
    print(f"=> {'RÉVERSIBLE 5/5' if fails==0 else 'ÉCHEC '+str(fails)+'/5'} ; avalanche moyenne {sum(avals)//len(avals)}/{N*N}")
