#!/usr/bin/env python3
"""Unified static + helper HTTP server for the autoencoder demo page.

Static files: served from this script's directory (the demo dir).
Helper API: under /helper/* — same-origin so it works from any client (mobile,
LAN, private window) without CORS dance.

Endpoints (all under /helper):
  GET  /helper/status         →  {"ok": true, "version": "..."}
  GET  /helper/lsusb          →  {"devices": [{bus, device, id, name}, ...]}
  GET  /helper/scale          →  scale reading (on-demand; the serial port is
                                  opened only while the page keeps polling)
  POST /helper/enable-webgpu  →  appends WebGPU prefs to Firefox user.js
  POST /helper/reset-webgpu   →  removes WebGPU prefs
  POST /helper/redEC          →  body = binary file ; → {hash_hex, level} (auto Red1..Red4)
  POST /helper/redDEC         →  body = {"hash_hex": "<2048 chars>"} ; → {hashes_hex[1024], …}
  GET  /helper/files/pick     →  picker natif multi-fichiers ; → {paths, entries}
  POST /helper/refs/mount     →  {paths,label?} ; symlinks dans un tmpdir, no copy → {id,mount_path,entries}
  POST /helper/refs/open      →  {id,path?} ; xdg-open le tmpdir ou un de ses fichiers
  POST /helper/refs/unmount   →  {id} ; supprime les symlinks (fichiers cibles intacts)
  GET  /helper/refs/list?id=  →  {entries} du tmpdir

Listens on 0.0.0.0:49080 (HTTP) and 0.0.0.0:49443 (HTTPS) by default so mobile
devices on the same LAN can hit the page (and the helper endpoints) via the
desktop's IP. Ports are IANA dynamic range — no collision with standard apps.

Run: python3 helper.py
"""
import base64
import json
import os
import platform
import re
import shutil
import socket
import ssl
import struct
import subprocess
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

VERSION = '0.5.38'
PORT = int(os.environ.get('HELPER_PORT', '49080'))
HTTPS_PORT = int(os.environ.get('HELPER_HTTPS_PORT', '49443'))
DEMO_DIR = Path(__file__).resolve().parent
CERT_FILE = DEMO_DIR / 'cert.pem'
KEY_FILE = DEMO_DIR / 'key.pem'

# Embedded local.redlinks.fr cert + key (Let's Encrypt). Used as a last
# resort when the file-on-disk versions aren't reachable — typical after
# auto-update of helper.py to the OS cache dir, which leaves the bundled
# certs unreachable. Renew window: see `openssl x509 -enddate -in cert.pem`.
_EMBEDDED_CERT_B64 = 'LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSURqVENDQXhPZ0F3SUJBZ0lTQm9vR1J2SVZYakRWQk05TjVoMm1oSFlpTUFvR0NDcUdTTTQ5QkFNRE1ESXgKQ3pBSkJnTlZCQVlUQWxWVE1SWXdGQVlEVlFRS0V3MU1aWFFuY3lCRmJtTnllWEIwTVFzd0NRWURWUVFERXdKRgpOekFlRncweU5qQTFNRFV4TXpNMU1EUmFGdzB5TmpBNE1ETXhNek0xTUROYU1Cd3hHakFZQmdOVkJBTVRFV3h2ClkyRnNMbkpsWkd4cGJtdHpMbVp5TUZrd0V3WUhLb1pJemowQ0FRWUlLb1pJemowREFRY0RRZ0FFZjVUWmdDZXUKYUpzcUxlZ3NjZGQ3VlpmZW5FWmhqeHJZazVtTEh0Z1F6bkRucFRINTUwTEJhNFVYeHFJc0ZYcVk2OGxqSkRmegpSYlhPb1Y0MlNNTDdwNk9DQWgwd2dnSVpNQTRHQTFVZER3RUIvd1FFQXdJSGdEQVRCZ05WSFNVRUREQUtCZ2dyCkJnRUZCUWNEQVRBTUJnTlZIUk1CQWY4RUFqQUFNQjBHQTFVZERnUVdCQlNzSXBIYlNSVytOSXRHNlVHbVV0NXMKQVMzS2pqQWZCZ05WSFNNRUdEQVdnQlN1U0o3Y2h4MUVvRy9hb3VWZ2RBUjR3cHdBZ0RBeUJnZ3JCZ0VGQlFjQgpBUVFtTUNRd0lnWUlLd1lCQlFVSE1BS0dGbWgwZEhBNkx5OWxOeTVwTG14bGJtTnlMbTl5Wnk4d0hBWURWUjBSCkJCVXdFNElSYkc5allXd3VjbVZrYkdsdWEzTXVabkl3RXdZRFZSMGdCQXd3Q2pBSUJnWm5nUXdCQWdFd0xRWUQKVlIwZkJDWXdKREFpb0NDZ0hvWWNhSFIwY0RvdkwyVTNMbU11YkdWdVkzSXViM0puTHpNMUxtTnliRENDQVF3RwpDaXNHQVFRQjFua0NCQUlFZ2YwRWdmb0ErQUIzQU1JeGZsZEZHYU5GN244NDNyS1FRZXZId2lGYUlyOS8xYld0CmRwclpEbExOQUFBQm5maVBBS1FBQUFRREFFZ3dSZ0loQU41Yml2R012R0tzdVJrR1ZFWkdPSmsxNG1IdmRoMnEKeEQ5czkvc28wS3BmQWlFQWt4eGFQZmZnVElOaHZFWVVOeG9md3NTa2JqZFdRaWFQRFpFQndwekxVd1lBZlFBYQppNTFyRC82L2diUjVPY2JTTVFxRzF0RUMxUEJHNGhnc25lTmZYaVlsN3dBQUFaMzRqd0R0QUFnQUFBVUFEenY4CllnUURBRVl3UkFJZ2FMODhkL0RPQ1Y4RklleklFYis5YkErWWcxWFpINFZwSTdRMHZpdkp1dDBDSUhBcWNtVEMKSksrRDdwckp2cS9ZMzFSMERpZXZCUTN6RGxISG9ka1IxNk16TUFvR0NDcUdTTTQ5QkFNREEyZ0FNR1VDTUNTTApYbHpieTY1YzY3R1BISDFKV2tKY2RDcG13NjU5TWovVDlXZmM5c3QxalUyck5UZk5XR1RXQTF4MG1oaWlrQUl4CkFMUnJESEVxUmxNc2crcWZuSktHZ2NpdHMydzlKUURqS2JndmgxVnNTV3d5N0QwVE5UOWpKbmgxTFBFYjJ0SXYKN0E9PQotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCi0tLS0tQkVHSU4gQ0VSVElGSUNBVEUtLS0tLQpNSUlFVnpDQ0FqK2dBd0lCQWdJUkFLcDE4ZVlyandvaUNXYlRpNy9VdXFFd0RRWUpLb1pJaHZjTkFRRUxCUUF3ClR6RUxNQWtHQTFVRUJoTUNWVk14S1RBbkJnTlZCQW9USUVsdWRHVnlibVYwSUZObFkzVnlhWFI1SUZKbGMyVmgKY21Ob0lFZHliM1Z3TVJVd0V3WURWUVFERXd4SlUxSkhJRkp2YjNRZ1dERXdIaGNOTWpRd016RXpNREF3TURBdwpXaGNOTWpjd016RXlNak0xT1RVNVdqQXlNUXN3Q1FZRFZRUUdFd0pWVXpFV01CUUdBMVVFQ2hNTlRHVjBKM01nClJXNWpjbmx3ZERFTE1Ba0dBMVVFQXhNQ1JUY3dkakFRQmdjcWhrak9QUUlCQmdVcmdRUUFJZ05pQUFSQjZBU1QKQ0ZoL3ZqY3dETUNnUWVyK1Z0cUVrejdKQU51clp4TFArVTlUQ2Vpb0w2c3A1WjhWUnZSYllrNFAxSU5CbWJlZgpRSEpGSEN4Y1NqS213dHZHQldwbC85cmE4SFcwUURzVWFKVzJxT0pxY2VKMFpWRlQzaGJVSGlmQk0vMmpnZmd3CmdmVXdEZ1lEVlIwUEFRSC9CQVFEQWdHR01CMEdBMVVkSlFRV01CUUdDQ3NHQVFVRkJ3TUNCZ2dyQmdFRkJRY0QKQVRBU0JnTlZIUk1CQWY4RUNEQUdBUUgvQWdFQU1CMEdBMVVkRGdRV0JCU3VTSjdjaHgxRW9HL2FvdVZnZEFSNAp3cHdBZ0RBZkJnTlZIU01FR0RBV2dCUjV0Rm5tZTdibDVBRnpnQWlJeUJwWTl1bWJiakF5QmdnckJnRUZCUWNCCkFRUW1NQ1F3SWdZSUt3WUJCUVVITUFLR0ZtaDBkSEE2THk5NE1TNXBMbXhsYm1OeUxtOXlaeTh3RXdZRFZSMGcKQkF3d0NqQUlCZ1puZ1F3QkFnRXdKd1lEVlIwZkJDQXdIakFjb0JxZ0dJWVdhSFIwY0RvdkwzZ3hMbU11YkdWdQpZM0l1YjNKbkx6QU5CZ2txaGtpRzl3MEJBUXNGQUFPQ0FnRUFqeDY2ZkRkTGs1eXdGbjNDekExdzFxZnlsSFVECmFFZjBRWnBYY0pzZWRkSkdTZmJVVU92Yk5SOU4vUVExNksxbFhsNFZGeWhtR1hEVDVLZGZjcjBSdklJVnJOeEYKaDRscUh0UlJDUDZSQlJzdHFiWjJ6VVJncWFrbi9YaXAwaWFRTDBJZGZIQlpyMzk2Rmdrbm5pUllGY2tLT1JQRwp5TTNRS25kNjZndE1zdDhJNW5rUlFsQWcvSmIrR2MzZWdJdnVHS1dib0UxRzg5TlRzTjlMVEREM1BMajBkVU1yCk9JdXFWakxCOHBFQzZ5azllbnJscnFqWFFna0xFWWhYenE3ZExhZnY1VmtpZzZHbDBudXVxanFmcDBRMWJpMW8KeVZOQWxYZTZhVVh3OTJDY2doQzliTnNLRU8xK001MllZNStvZklYbFMvU0VRYnZWWVlCTFo1eWVpZ2xWNnQzUwpNNkgrdlRHMGFQOVlIekxuL0tWT0h6R1FmWERQN3FNNXRrZis3ZGlaZTdvMmZ3Nk83SXZONmZzUVhFUVFqOFRKClVYSnh2Mi91SmhjdXkvdFNEZ1h3SE04VWszNFdOYlJUN3pHVEdrUVJYMGdzYmpBZWEvallBb1d2MFp2UVJ3cHEKUGU3OUQvaTdDZXA4cVduQSs3QUUvM0IzUy8zZEVFWW1jMGxwZTEzNjZBLzZHRWdrM2t0cjlQRW9RckxDaHM2SQp0dTN3bk5MQjJldUM4SUtHTFFGcEd0T08vMi9oaUFLanlhamFCUDI1dzFqRjBXbDhCYnFuZTN1WjJxMUd5UEZKCllSbVQ3L09YcG1PSC9GVkx0d1MrOG5nMWNBbXBDdWpQd3RlSlpOY0RHMHNGMm4vc2MwK1NRZjQ5ZmR5VUswdHkKK1ZVd0ZqOXRtV3h5Ui9NPQotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCg=='
_EMBEDDED_KEY_B64 = 'LS0tLS1CRUdJTiBQUklWQVRFIEtFWS0tLS0tCk1JR0hBZ0VBTUJNR0J5cUdTTTQ5QWdFR0NDcUdTTTQ5QXdFSEJHMHdhd0lCQVFRZzFsU1V0bURxci9UbnhkZ3QKT3dnTDVnaTZYZ1Z0N05hUzJXdDZNZEREaGF1aFJBTkNBQVIvbE5tQUo2NW9teW90NkN4eDEzdFZsOTZjUm1HUApHdGlUbVlzZTJCRE9jT2VsTWZublFzRnJoUmZHb2l3VmVwanJ5V01rTi9ORnRjNmhYalpJd3Z1bgotLS0tLUVORCBQUklWQVRFIEtFWS0tLS0tCg=='


def _resolve_cert_paths():
    """Find usable cert.pem + key.pem on disk; fall back to materializing
    the embedded copy in a tmp dir. Resolution order:
      1. DEMO_DIR (where helper.py is currently running from)
      2. $REDSTARS_HELPER_BUNDLED_DIR (passed by the Tauri shell if it
         knows the bundle path; absent on auto-update runs)
      3. embedded fallback → /tmp/redstars-helper-certs/
    Returns (cert_path, key_path) or (None, None) if even the embed
    write fails."""
    import tempfile as _tf
    candidates = [DEMO_DIR]
    bundled = os.environ.get('REDSTARS_HELPER_BUNDLED_DIR')
    if bundled:
        candidates.append(Path(bundled))
    for d in candidates:
        c = d / 'cert.pem'
        k = d / 'key.pem'
        if c.is_file() and k.is_file():
            return c, k
    try:
        tmp = Path(_tf.gettempdir()) / 'redstars-helper-certs'
        tmp.mkdir(exist_ok=True)
        c = tmp / 'cert.pem'
        k = tmp / 'key.pem'
        c.write_bytes(base64.b64decode(_EMBEDDED_CERT_B64))
        k.write_bytes(base64.b64decode(_EMBEDDED_KEY_B64))
        try: os.chmod(k, 0o600)
        except OSError: pass
        return c, k
    except Exception as e:
        print(f'  HTTPS: failed to materialize embedded certs: {e}')
        return None, None

# Origins allowed to call /helper/* across origins (HTTPS dashboard reaching
# https://local.redlinks.fr:8443/helper/* needs CORS to consent).
ALLOWED_ORIGINS = {
    'https://dev.redstars.redlinks.fr',
    'https://redstars.redlinks.fr',
    'http://localhost:49080',
    'https://local.redlinks.fr:49443',
    # Legacy — kept for older clients during the migration off 9999/8443.
    'http://localhost:9999',
    'https://local.redlinks.fr:8443',
}

# Codec autoencoder (redEC/redDEC) — chargement paresseux à la 1ʳᵉ requête /helper/redEC ou /redDEC.
# Permet au helper de démarrer même sans torch/numpy installés ; les routes renvoient une erreur
# claire si la dépendance manque.
_CODEC = {'loaded': False, 'redEC_chain': None, 'redDEC': None,
          'encode_file': None, 'decode_file': None, 'backend': None,
          'cascade_err': None, 'err': None}

def _codec_dirs():
    """Dirs that may hold the codec assets. helper.py auto-updates to the OS cache
    dir (only helper.py there), so beyond DEMO_DIR we probe the env hint + the OS
    bundle (rpm/deb productName dir, /opt, AppImage mount, macOS .app)."""
    import glob as _glob
    cands = [DEMO_DIR]
    env = os.environ.get('REDSTARS_HELPER_BUNDLED_DIR')
    if env:
        cands.append(Path(env))
    for pat in (
        '/usr/lib/*/bundled/codec_onnx.py', '/usr/lib/*/resources/bundled/codec_onnx.py',
        '/usr/share/*/bundled/codec_onnx.py', '/opt/*/bundled/codec_onnx.py',
        '/tmp/.mount_*/usr/lib/*/bundled/codec_onnx.py',
        '/Applications/*.app/Contents/Resources/bundled/codec_onnx.py',
        str(Path.home() / 'Applications' / '*.app' / 'Contents' / 'Resources' / 'bundled' / 'codec_onnx.py'),
    ):
        for hit in _glob.glob(pat):
            cands.append(Path(hit).parent)
    return cands

def _ensure_codec():
    if _CODEC['loaded'] or _CODEC['err']:
        return _CODEC['err']
    try:
        import sys as _sys
        for d in _codec_dirs():
            if (d / 'codec_onnx.py').is_file() or (d / 'codec_numpy.py').is_file() or (d / 'redEC.py').is_file():
                if str(d) not in _sys.path:
                    _sys.path.insert(0, str(d))
                break
        # Codec lossless PORTABLE (sidecar de correction → bit-exact) : ONNX (rapide,
        # multi-thread CPU, sans torch) → numpy (secours si pas d'onnxruntime).
        try:
            import codec_onnx as _cf; _CODEC['backend'] = 'onnx'
        except Exception:
            import codec_numpy as _cf; _CODEC['backend'] = 'numpy'
        _CODEC['encode_file'] = _cf.encode_file
        _CODEC['decode_file'] = _cf.decode_file
        # Cascade redEC/redDEC (1 hash ↔ 1024) — OPTIONNELLE, nécessite torch. Si torch
        # absent (machine portable), le blob lossless marche quand même.
        try:
            from redEC import redEC_chain
            from redDEC import redDEC
            _CODEC['redEC_chain'] = redEC_chain
            _CODEC['redDEC'] = redDEC
        except Exception as _te:
            _CODEC['cascade_err'] = f'{type(_te).__name__}: {_te}'
        _CODEC['loaded'] = True
        return None
    except Exception as e:
        _CODEC['err'] = f'{type(e).__name__}: {e}'
        return _CODEC['err']

SCALE_PORT = '/dev/ttyUSB0'
SCALE_BAUD = 9600
# Format observed on cheap CH340-based scales: "WTST    +27.34  g"
SCALE_LINE = re.compile(r'(?P<status>[A-Z]{2,4})\s*(?P<sign>[+-])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>g|kg|lb|oz|ml)?', re.I)


class ScaleReader:
    """On-demand serial scale reader. There is NO 24/7 background polling: the
    serial port is opened lazily on the first /scale request and kept open only
    while the page keeps requesting. A single self-rescheduling idle timer (one
    wake every IDLE_CLOSE_SECS, ONLY while in use) closes the port a few seconds
    after the last read, so the helper goes fully dormant — zero threads, zero
    wakeups — when the scale isn't being used. The web page drives the cadence
    (poll ~1/s while the weighing screen is open).

    Cheap CH340 scales stream weight lines continuously while powered, so each
    read drains the serial buffer and returns the freshest line.
    """
    IDLE_CLOSE_SECS = 5

    def __init__(self, port=SCALE_PORT, baud=SCALE_BAUD):
        self.port = port
        self.baud = baud
        self.lock = threading.Lock()
        self._ser = None
        self._timer = None
        self._last_request = 0.0
        self._last = {
            'connected': False, 'value': None, 'unit': None, 'sign': None,
            'status': None, 'raw': None, 'updated_at': None, 'error': None,
        }

    # --- port lifecycle (caller holds self.lock) -------------------------
    def _close(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def _arm_timer(self):
        t = threading.Timer(self.IDLE_CLOSE_SECS, self._on_timer)
        t.daemon = True
        self._timer = t
        t.start()

    def _on_timer(self):
        # Close if no read happened recently; otherwise keep watching. This is
        # the ONLY recurring wake, and only while the scale is actively in use.
        with self.lock:
            self._timer = None
            if time.time() - self._last_request >= self.IDLE_CLOSE_SECS:
                self._close()
            else:
                self._arm_timer()

    # --- public API ------------------------------------------------------
    # Freshness window for the Android bridge file: no line newer than this ⇒
    # treat the scale as disconnected (unplugged / powered off).
    STALE_MS = 4000

    def _read_from_file(self, path):
        """Android: an app can't open /dev/ttyUSB0, so the native Kotlin bridge
        (UsbScaleBridge, usb-serial-for-android) reads the USB scale and writes the
        freshest line to REDSTARS_SCALE_FILE as '<epoch_ms>\\t<raw>'. Parse it here
        with the SAME SCALE_LINE regex / return shape as the serial path."""
        try:
            with open(path, 'r') as f:
                content = f.read().strip()
        except FileNotFoundError:
            self._last = dict(self._last, connected=False, error='balance bridge: no reading yet')
            return dict(self._last)
        except Exception as e:
            self._last = dict(self._last, connected=False, error=type(e).__name__ + ': ' + str(e))
            return dict(self._last)
        if not content or '\t' not in content:
            self._last = dict(self._last, connected=False, error='balance bridge: no reading yet')
            return dict(self._last)
        ts_str, raw = content.split('\t', 1)
        raw = raw.strip()
        try:
            age_ms = time.time() * 1000.0 - float(ts_str)
        except ValueError:
            age_ms = None
        if age_ms is not None and age_ms > self.STALE_MS:
            self._last = dict(self._last, connected=False, raw=raw, error='scale idle (unplugged?)')
            return dict(self._last)
        m = SCALE_LINE.search(raw)
        if m:
            sign = -1 if m.group('sign') == '-' else 1
            self._last = {
                'connected': True,
                'value': sign * float(m.group('value')),
                'unit': (m.group('unit') or 'g').lower(),
                'sign': m.group('sign'),
                'status': m.group('status'),
                'raw': raw,
                'updated_at': time.time(),
                'error': None,
            }
        else:
            self._last = dict(self._last, connected=True, raw=raw, updated_at=time.time(), error=None)
        return dict(self._last)

    def read(self):
        """Open on demand, drain to the freshest line, return the reading.
        The port auto-closes IDLE_CLOSE_SECS after the last call."""
        _bridge = os.environ.get('REDSTARS_SCALE_FILE')
        if _bridge:
            return self._read_from_file(_bridge)
        with self.lock:
            self._last_request = time.time()
            if self._ser is None:
                try:
                    import serial
                except ImportError:
                    self._last = dict(self._last, connected=False,
                                      error='pyserial not installed (pip install --user pyserial)')
                    return dict(self._last)
                try:
                    self._ser = serial.Serial(self.port, self.baud, timeout=0.4)
                except FileNotFoundError:
                    self._last = dict(self._last, connected=False,
                                      error=f'{self.port} not present (scale unplugged?)')
                    return dict(self._last)
                except PermissionError:
                    self._last = dict(self._last, connected=False,
                                      error=f'{self.port} permission denied (udev rule?)')
                    return dict(self._last)
                except Exception as e:
                    self._last = dict(self._last, connected=False,
                                      error=type(e).__name__ + ': ' + str(e))
                    return dict(self._last)
            ser = self._ser
            try:
                latest_raw = None
                latest_m = None
                # Drain everything buffered so we return the FRESHEST line.
                while ser.in_waiting:
                    raw = ser.readline().decode('ascii', errors='replace').strip()
                    if not raw:
                        continue
                    latest_raw = raw
                    m = SCALE_LINE.search(raw)
                    if m:
                        latest_m = m
                if latest_raw is None:
                    # Nothing buffered yet — one short blocking read.
                    raw = ser.readline().decode('ascii', errors='replace').strip()
                    if raw:
                        latest_raw = raw
                        latest_m = SCALE_LINE.search(raw)
            except Exception as e:
                self._close()
                self._last = dict(self._last, connected=False,
                                  error=type(e).__name__ + ': ' + str(e))
                return dict(self._last)

            if latest_m:
                sign = -1 if latest_m.group('sign') == '-' else 1
                self._last = {
                    'connected': True,
                    'value': sign * float(latest_m.group('value')),
                    'unit': (latest_m.group('unit') or 'g').lower(),
                    'sign': latest_m.group('sign'),
                    'status': latest_m.group('status'),
                    'raw': latest_raw,
                    'updated_at': time.time(),
                    'error': None,
                }
            elif latest_raw is not None:
                # Boot message / blank — keep raw for debug, mark connected.
                self._last = dict(self._last, connected=True, raw=latest_raw,
                                  updated_at=time.time(), error=None)
            else:
                # Port open but no data this round — keep last value.
                self._last = dict(self._last, connected=True, error=None)

            if self._timer is None:
                self._arm_timer()
            return dict(self._last)

    # Back-compat alias (old callers used .get()).
    def get(self):
        return self.read()


SCALE = ScaleReader()


def find_firefox_default_profile():
    """Return the path to the active Firefox profile, or None.

    Reads profiles.ini and prefers the one referenced by [Install*] Default=,
    which is what Firefox actually launches.
    """
    ff_dir = Path.home() / '.mozilla' / 'firefox'
    ini = ff_dir / 'profiles.ini'
    if not ini.exists():
        return None
    text = ini.read_text()
    # Find the Install section's Default first (this wins over Profile.Default=1)
    install_default = re.search(r'\[Install[^\]]+\]\s*\nDefault=(\S+)', text)
    if install_default:
        candidate = ff_dir / install_default.group(1)
        if candidate.is_dir():
            return candidate
    # Fallback: any Profile with Default=1
    for block in re.split(r'\n(?=\[)', text):
        if 'Default=1' in block:
            m = re.search(r'Path=(\S+)', block)
            if m:
                candidate = ff_dir / m.group(1)
                if candidate.is_dir():
                    return candidate
    return None


def parse_lsusb_line(line):
    m = re.match(r'Bus (\d+) Device (\d+): ID (\S+) (.*)', line)
    if not m:
        return None
    return {
        'bus': m.group(1),
        'device': m.group(2),
        'id': m.group(3),
        'name': m.group(4).strip(),
    }


# ─── ISO mount/unmount ────────────────────────────────────────────────
#
# The dashboard asks the helper to wrap a payload (or nothing) in an
# ISO 9660 image and mount it on the host using the OS's native loop
# mechanism — no sudo, no FUSE install required on stock desktops.
# Unmount cleans up and deletes the temp .iso.
#
# Mount mechanism per OS (all userspace):
#   Linux   → udisksctl loop-setup + udisksctl mount   (Polkit, session)
#   macOS   → hdiutil attach
#   Windows → powershell Mount-DiskImage

ISO_CACHE_DIR = Path.home() / '.cache' / 'redstars-helper' / 'iso'


def _save_dir():
    """Dossier où enregistrer les fichiers extraits, accessible à l'utilisateur.
    Android : Android/data/com.redstars.app/files/RedStars (USB / gestionnaire de
    fichiers, sans permission). Desktop : ~/Téléchargements (ou ~/Downloads)."""
    env = os.environ.get('REDSTARS_HELPER_SAVE_DIR')
    if env:
        d = Path(env)
    elif os.environ.get('REDSTARS_HELPER_PLATFORM') == 'android':
        ext = Path('/storage/emulated/0/Android/data/com.redstars.app/files/RedStars')
        try:
            ext.mkdir(parents=True, exist_ok=True)
            t = ext / '.wtest'; t.write_text('x'); t.unlink()   # test d'écriture
            d = ext
        except Exception:
            d = Path(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~')) / 'RedStars'
    else:
        dl = Path(os.path.expanduser('~/Téléchargements'))
        d = (dl if dl.is_dir() else Path(os.path.expanduser('~/Downloads'))) / 'RedStars'
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- Lecteur ISO9660(+Joliet) PUR PYTHON inliné (auto-update sans module séparé) ---
# Liste/extrait les fichiers à la racine d'un ISO make_iso (xorrisofs -R -J),
# sans monter ni binaire externe, en seek (gros ISO ok).
_ISO_SECTOR = 2048

def _iso_find_vds(f):
    pvd = svd = None; sec = 16
    while sec <= 64:
        f.seek(sec * _ISO_SECTOR); vd = f.read(_ISO_SECTOR)
        if len(vd) < 7 or vd[1:6] != b'CD001':
            break
        t = vd[0]
        if t == 1 and pvd is None: pvd = vd
        elif t == 2 and any(e in vd[88:120] for e in (b'%/@', b'%/C', b'%/E')): svd = vd
        elif t == 255: break
        sec += 1
    return pvd, svd

def _iso_root(vd):
    rec = vd[156:190]
    return struct.unpack('<I', rec[2:6])[0], struct.unpack('<I', rec[10:14])[0]

def _iso_records(f, lba, length, joliet):
    f.seek(lba * _ISO_SECTOR); block = f.read(length); out = []; i = 0
    while i < len(block):
        rlen = block[i]
        if rlen == 0:
            i = ((i // _ISO_SECTOR) + 1) * _ISO_SECTOR; continue
        rec = block[i:i + rlen]; i += rlen
        if len(rec) < 33: break
        ext_lba = struct.unpack('<I', rec[2:6])[0]
        dlen = struct.unpack('<I', rec[10:14])[0]
        flags = rec[25]; idlen = rec[32]; ident = rec[33:33 + idlen]
        if flags & 0x02: continue
        if idlen == 1 and ident in (b'\x00', b'\x01'): continue
        name = ident.decode('utf-16-be', 'replace') if joliet else ident.decode('ascii', 'replace')
        if ';' in name: name = name.split(';')[0]
        name = name.rstrip('.')
        if name: out.append({'name': name, 'lba': ext_lba, 'size': dlen})
    return out

def _iso_list(path):
    with open(path, 'rb') as f:
        pvd, svd = _iso_find_vds(f)
        vd = svd or pvd
        if vd is None: return []
        lba, length = _iso_root(vd)
        recs = _iso_records(f, lba, length, joliet=(svd is not None))
    return [{'name': r['name'], 'size': r['size']} for r in recs]

def _iso_extract_to(path, name, dst_path, chunk=1 << 20):
    with open(path, 'rb') as f:
        pvd, svd = _iso_find_vds(f)
        vd = svd or pvd
        if vd is None: raise ValueError('pas un ISO9660')
        lba, length = _iso_root(vd)
        rec = next((r for r in _iso_records(f, lba, length, joliet=(svd is not None)) if r['name'] == name), None)
        if rec is None: raise FileNotFoundError(name)
        Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
        with open(dst_path, 'wb') as o:
            f.seek(rec['lba'] * _ISO_SECTOR); remaining = rec['size']
            while remaining > 0:
                buf = f.read(min(chunk, remaining))
                if not buf: break
                o.write(buf); remaining -= len(buf)
        return rec['size']
MOUNTED = {}  # iso_id → {'iso_path','mount_path','dev','label','created_at'}

# Refs « par référence » : un répertoire temporaire de symlinks vers des fichiers
# du disque, partagé avec l'utilisateur via xdg-open. Pas de copie des données,
# juste des liens symboliques. Cleanup à la demande via /helper/refs/unmount.
REFS_CACHE_DIR = Path.home() / '.cache' / 'redstars-helper' / 'refs'
REFS = {}  # refs_id → {'dir_path','label','sources':[...],'created_at'}

# Jobs longs (décodage chaîne Red3/Red4 : 1M+ appels redDEC). On les
# spawn dans un thread, le client poll /redDEC-job/<id>.
JOBS = {}  # job_id → {kind, status: 'running'|'done'|'failed',
           #           progress: 0..1, started_at, done_at?,
           #           error?, result?: MountInfo}
JOBS_LOCK = threading.Lock()

def _new_job(kind: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            'kind': kind,
            'status': 'running',
            'progress': 0.0,
            'started_at': time.time(),
        }
    return job_id

def _job_set(job_id: str, **kw):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


# --- File-codec blob store (the tested lossless chain: codec.py + sidecar) ----
# Save  : files → make_iso → encode_file → blob (RSN1 + patch trailer) persisted here.
# Restore: blob → decode_file → iso → mount. The blob is the shareable artifact
# (~source size, lossless 1:1 — NOT a tiny hash; no contraction, no dropping).
BLOB_DIR = (Path(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'))
            / 'redstars-helper' / 'blobs')

def _iso_payload_from_paths(abs_paths):
    used, payload, files_meta = set(), {}, []
    for src in abs_paths:
        nm = Path(src).name
        if nm in used:
            base, ext = os.path.splitext(nm); i = 1
            while f'{base}-{i}{ext}' in used: i += 1
            nm = f'{base}-{i}{ext}'
        used.add(nm); payload[nm] = src
        files_meta.append({'name': nm, 'size': os.path.getsize(src)})
    return payload, files_meta

def _codec_encode_worker(job_id, abs_paths, label):
    """Save : fichiers → make_iso → HASH INT8 (latents codec_numpy) stocké helper-side.
    Plus de blob ni de sidecar ni de cascade : le hash int8 EST l'artefact, décodé
    exactement par le réseau infaillible. Hash ≈ taille ISO (1:1, pas de compression)."""
    try:
        import codec_numpy, numpy as _np
        payload, files_meta = _iso_payload_from_paths(abs_paths)
        iso_path = make_iso(label, payload=payload)
        iso = iso_path.read_bytes()
        source_bytes = len(iso)
        pad = (-source_bytes) % codec_numpy.BLOCK
        arr = _np.frombuffer(iso + b'\x00' * pad, _np.uint8)
        _, lat = codec_numpy._enc_blocks(arr)               # latents = le hash int8
        BLOB_DIR.mkdir(parents=True, exist_ok=True)
        hash_id = uuid.uuid4().hex[:16]
        (BLOB_DIR / f'{hash_id}.hash').write_bytes(bytes(lat))
        try: iso_path.unlink(missing_ok=True)
        except Exception: pass
        _job_set(job_id, status='done', progress=1.0, done_at=time.time(), result={
            'hash_id': hash_id,
            'hash_bytes': len(lat),
            'source_bytes': source_bytes,     # taille ISO → pour tronquer au restore
            'n_files': len(files_meta),
            'files': files_meta,
            'label': label,
        })
    except Exception as e:
        _job_set(job_id, status='failed', done_at=time.time(), error=f'{type(e).__name__}: {e}')

def _mount_iso_result(iso_path, iso_id, label):
    """ISO (déjà écrite) → mount + list. Returns the MountInfo-shaped result dict.
    Partagé entre le décode blob (legacy) et le décode hash int8."""
    mount_path, dev, mount_error = None, None, None
    try:
        mount_path, dev = mount_iso(iso_path)
        MOUNTED[iso_id] = {'iso_path': str(iso_path), 'mount_path': mount_path,
                           'dev': dev, 'label': label or 'BUNDLE', 'created_at': time.time()}
    except Exception as e:
        mount_error = f'{type(e).__name__}: {e}'
    result = {'id': iso_id, 'iso_path': str(iso_path), 'mount_path': mount_path,
              'label': label or 'BUNDLE', 'output_size': iso_path.stat().st_size,
              'entries': list_mount(mount_path) if mount_path else []}
    if mount_error: result['mount_error'] = mount_error
    return result


def _hash_to_iso_and_mount(hash_bytes, iso_bytes, label):
    """hash int8 (latents) → décode int8/NPU → ISO (tronquée à iso_bytes) → mount/list.
    Pas de blob ni sidecar : on s'appuie sur le réseau infaillible (round-trip 0)."""
    import codec_numpy, numpy as _np
    raw = _np.asarray(codec_numpy._dec_bytes(bytes(hash_bytes)), _np.uint8).tobytes()
    if iso_bytes and iso_bytes > 0:
        raw = raw[:iso_bytes]
    ISO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    iso_id = uuid.uuid4().hex[:12]
    iso_path = ISO_CACHE_DIR / f'{iso_id}.iso'
    iso_path.write_bytes(raw)
    return _mount_iso_result(iso_path, iso_id, label)


def _decode_blob_and_mount(blob_path, label):
    """decode_file(blob) → iso → mount. Returns the MountInfo-shaped result dict."""
    ISO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    iso_id = uuid.uuid4().hex[:12]
    iso_path = ISO_CACHE_DIR / f'{iso_id}.iso'
    _CODEC['decode_file'](str(blob_path), str(iso_path))
    return _mount_iso_result(iso_path, iso_id, label)

def _codec_restore_worker(job_id, hash_id, iso_bytes, label):
    """hash int8 (par id, stocké helper-side) → décode int8/NPU → ISO → mount."""
    try:
        hp = BLOB_DIR / f'{hash_id}.hash'
        if not hp.is_file():
            _job_set(job_id, status='failed', done_at=time.time(), error=f'no such hash: {hash_id}'); return
        _job_set(job_id, status='done', progress=1.0, done_at=time.time(),
                 result=_hash_to_iso_and_mount(hp.read_bytes(), iso_bytes, label))
    except Exception as e:
        _job_set(job_id, status='failed', done_at=time.time(), error=f'{type(e).__name__}: {e}')

def _codec_restore_data_worker(job_id, hash_bytes, iso_bytes, label):
    """hash int8 collé (venu d'ailleurs) → décode int8 → ISO → mount."""
    try:
        _job_set(job_id, status='done', progress=1.0, done_at=time.time(),
                 result=_hash_to_iso_and_mount(hash_bytes, iso_bytes, label))
    except Exception as e:
        _job_set(job_id, status='failed', done_at=time.time(), error=f'{type(e).__name__}: {e}')

def _redDEC_chain_worker(job_id: str, hash_hex: str, level: int,
                         name: str, target_size):
    """Worker thread pour /redDEC-chain. Sortie 1024^level hashes ×
    1024 octets ≈ 1024^(level+1) octets, truncate à `target_size`."""
    try:
        # Total d'appels redDEC attendus = somme géométrique.
        total_calls = sum(1024**i for i in range(level))  # 1, 1025, ~1M, ~1G
        calls_done = 0

        def report_progress():
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]['progress'] = min(0.99, calls_done / max(1, total_calls))

        # Short-circuit dès qu'on a assez de hashes pour couvrir
        # `bytes_needed` octets utiles à la sortie. Après l'étape `step`,
        # chaque hash dans `current` se développera en 1024^(level-step)
        # octets — donc on a besoin de ceil(bytes_needed / 1024^(level-step))
        # hashes max. Pour un tar de 11 GiB en Red3 :
        #     step 0 (avant 2 itérations restantes) → 11 hashes suffisent
        #     step 1 (avant 1 itération restante)   → 11 264 hashes
        #     step 2 (sortie finale)                → 11 534 336 leaves = 11 GiB
        # Total ~11k appels redDEC au lieu de 1M+, ET on n'écrit JAMAIS
        # plus que bytes_needed octets sur disque.
        bytes_needed = int(target_size) if target_size and int(target_size) > 0 else None

        current = [bytes.fromhex(hash_hex)]
        for step in range(level):
            nxt = []
            mult_after_step = 1024 ** (level - step)  # ce qu'un hash de nxt deviendra
            for h in current:
                decoded = _CODEC['redDEC'](h)  # 1 Mo
                calls_done += 1
                if calls_done % 32 == 0:
                    report_progress()
                for i in range(1024):
                    nxt.append(decoded[i*1024:(i+1)*1024])
                if bytes_needed and len(nxt) * mult_after_step >= bytes_needed:
                    break
            current = nxt

        # On limite current AVANT le join — sinon b''.join(1M hashes) =
        # 1 GiB en RAM même si on truncate après.
        if bytes_needed:
            needed_hashes = -(-bytes_needed // 1024)  # ceil
            current = current[:needed_hashes]
        out_bytes = b''.join(current)
        if bytes_needed:
            out_bytes = out_bytes[:bytes_needed]

        # Cache + mount.
        cache_root = Path(os.environ.get('XDG_CACHE_HOME')
                          or os.path.expanduser('~/.cache')) / 'redstars-helper' / 'decoded'
        cache_root.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', name)[:120] or 'decoded.bin'
        out_path = cache_root / f'{hash_hex[:8]}-{safe}'
        out_path.write_bytes(out_bytes)

        # Les `out_bytes` SONT directement les bytes d'une iso 9660 / UDF
        # (l'encode redEC porte sur l'iso construite côté envoi, pas sur
        # un tar). Le système de fichiers ISO porte sa propre table
        # d'index — on monte la sortie telle quelle et l'utilisateur
        # retrouve ses fichiers à la racine, sans cache local ni sidecar
        # de métadonnées (c.-à-d. ça marche sur n'importe quelle machine
        # qui reçoit juste le hash + le bundle_size).
        ISO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        iso_label = f'BUNDLE-Red{level}'
        iso_id = uuid.uuid4().hex[:12]
        iso_path = ISO_CACHE_DIR / f'{iso_id}.iso'
        shutil.move(str(out_path), str(iso_path))
        mount_path = None
        dev = None
        mount_error = None
        try:
            mount_path, dev = mount_iso(iso_path)
            MOUNTED[iso_id] = {
                'iso_path': str(iso_path),
                'mount_path': mount_path,
                'dev': dev,
                'label': iso_label,
                'created_at': time.time(),
            }
        except Exception as e:
            # Mount KO = la sortie cascade n'est pas une iso valide (codec
            # a perdu des bits sur une entrée non cascade-valide, ou
            # bundle_size faux). On garde quand même le .iso sur disque
            # pour debug et on remonte l'erreur au client — pas de
            # fallback `payload.bin` muet qui ferait croire à un succès.
            mount_error = f'{type(e).__name__}: {e}'
        result = {
            'level': level,
            'output_path': str(iso_path), 'output_size': len(out_bytes),
            'id': iso_id, 'mount_path': mount_path, 'label': iso_label,
            'iso_path': str(iso_path),
            'entries': list_mount(mount_path) if mount_path else [],
        }
        if mount_error:
            result['mount_error'] = mount_error
        _job_set(job_id, status='done', progress=1.0, done_at=time.time(),
                 result=result)
    except Exception as e:
        _job_set(job_id, status='failed', done_at=time.time(),
                 error=f'{type(e).__name__}: {e}')

def _recover_refs_from_disk():
    """Au boot, repeuple REFS depuis les sous-dossiers existants de
    ~/.cache/redstars-helper/refs/<LABEL>-<12hex>. Sans ça, après un
    restart du helper (ou un crash + relaunch via tray), les paths
    d'un mount fait par la session précédente sont rejetés en 403
    'path not under any active /refs/ mount' alors que les symlinks
    existent toujours sur disque. /refs/open et /refs/unmount avec
    l'ID d'un mount pré-restart tombaient aussi en 404."""
    if not REFS_CACHE_DIR.is_dir():
        return
    import re as _re
    pat = _re.compile(r'^(?P<label>.+)-(?P<id>[0-9a-f]{12})$')
    for child in REFS_CACHE_DIR.iterdir():
        if not child.is_dir():
            continue
        m = pat.match(child.name)
        if not m:
            continue
        refs_id = m.group('id')
        if refs_id in REFS:
            continue
        sources = []
        try:
            for entry in child.iterdir():
                try:
                    tgt = os.readlink(str(entry))
                    sources.append(tgt if os.path.isabs(tgt) else str((child / tgt).resolve()))
                except OSError:
                    pass
        except OSError:
            pass
        try:
            created_at = child.stat().st_mtime
        except OSError:
            created_at = time.time()
        REFS[refs_id] = {
            'dir_path': str(child),
            'label': m.group('label'),
            'sources': sources,
            'created_at': created_at,
        }
_recover_refs_from_disk()


def make_iso(label='REDSTARS', payload=None):
    """
    Build an ISO 9660+UDF disc image.

    The `payload` argument is the data-loading hook for the disc:
      - None              → 100% empty FS (just a labeled, navigable disc)
      - bytes / bytearray → one file `payload.bin` at the root
      - dict[str, bytes]  → those named files at the root

    Returns the path to the written .iso. Caller owns its lifecycle.
    """
    ISO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    iso_id = uuid.uuid4().hex[:12]
    iso_path = ISO_CACHE_DIR / f'{iso_id}.iso'
    src_dir = ISO_CACHE_DIR / f'src-{iso_id}'
    src_dir.mkdir()
    try:
        if isinstance(payload, (bytes, bytearray)):
            (src_dir / 'payload.bin').write_bytes(bytes(payload))
        elif isinstance(payload, dict):
            for name, data in payload.items():
                safe = Path(str(name)).name  # strip path components
                if not safe:
                    continue
                dst = src_dir / safe
                # data peut être :
                #   - bytes / bytearray   → write direct
                #   - str / Path qui pointe vers un fichier existant
                #                          → copy stream (films de plusieurs
                #                          Gio sans charger en RAM)
                if isinstance(data, (bytes, bytearray)):
                    dst.write_bytes(bytes(data))
                else:
                    src_path = Path(str(data))
                    if src_path.is_file():
                        shutil.copyfile(src_path, dst)
                    else:
                        # Fallback : on tente bytes()
                        dst.write_bytes(bytes(data))
        # else (None) → src_dir stays empty → empty filesystem

        tool = next((t for t in ('xorrisofs', 'genisoimage', 'mkisofs')
                     if shutil.which(t)), None)
        if not tool:
            raise RuntimeError('No ISO tool (install xorriso / genisoimage / mkisofs).')
        safe_label = (label or 'REDSTARS')[:32].strip() or 'REDSTARS'
        cmd = [tool, '-iso-level', '3', '-R', '-J', '-no-pad',
               '-V', safe_label, '-o', str(iso_path), str(src_dir)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f'{tool} failed: {r.stderr.strip()[:500]}')
        return iso_path
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)


def _run(cmd, check=True):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def mount_iso(iso_path):
    """Mount as the user via the OS-native mechanism. Returns (mount_path, dev)."""
    sysname = platform.system()
    if sysname == 'Linux':
        out = _run(['udisksctl', 'loop-setup', '-f', str(iso_path),
                    '--no-user-interaction']).stdout.strip()
        # "Mapped file file.iso as /dev/loop0."
        dev = out.split()[-1].rstrip('.')
        m = _run(['udisksctl', 'mount', '-b', dev, '--no-user-interaction']).stdout.strip()
        # "Mounted /dev/loop0 at /run/media/user/LABEL"
        mount_path = m.split(' at ', 1)[-1].rstrip('.').strip()
        return mount_path, dev
    if sysname == 'Darwin':
        out = _run(['hdiutil', 'attach', '-nobrowse', str(iso_path)]).stdout.strip()
        # Last non-empty line: "/dev/disk2   Apple_HFS    /Volumes/REDSTARS"
        last = [l for l in out.splitlines() if l.strip()][-1]
        parts = re.split(r'\s{2,}|\t', last.strip())
        dev = parts[0].strip()
        mount_path = parts[-1].strip()
        return mount_path, dev
    if sysname == 'Windows':
        ps = (f"$img = Mount-DiskImage -ImagePath '{iso_path}' -PassThru; "
              "Start-Sleep -Milliseconds 250; "
              "($img | Get-Volume).DriveLetter")
        d = _run(['powershell', '-NoProfile', '-Command', ps]).stdout.strip().splitlines()[-1].strip()
        return f'{d}:\\', str(iso_path)  # dev = iso path (used by Dismount-DiskImage)
    raise RuntimeError(f'Unsupported OS: {sysname}')


def unmount_iso(iso_path, dev):
    sysname = platform.system()
    try:
        if sysname == 'Linux':
            _run(['udisksctl', 'unmount', '-b', dev, '--no-user-interaction'], check=False)
            _run(['udisksctl', 'loop-delete', '-b', dev, '--no-user-interaction'], check=False)
        elif sysname == 'Darwin':
            _run(['hdiutil', 'detach', dev], check=False)
        elif sysname == 'Windows':
            _run(['powershell', '-NoProfile', '-Command',
                  f"Dismount-DiskImage -ImagePath '{iso_path}'"], check=False)
    except Exception:
        pass


# ─── Self-update over HTTP (triggered by /helper/update) ───────────────
#
# The dashboard polls /api/v1/agents/script-latest for the canonical
# helper.py release, then POSTs /helper/update if the running version
# differs. We fetch + verify minisign + atomic-write + execv ourselves —
# same model as the Tauri shell's auto-update poller, just user-initiated
# from the browser.

# Embedded minisign pubkey — matches the `pubkey` in tauri.conf.json's
# updater config, i.e. the same key that signs the .deb/.dmg/.msi
# updates AND the helper.py release assets (release-script.yml in
# redstars-helper). Verification is fail-closed: missing cryptography
# lib, malformed signature, or signature mismatch → REJECT, keep cache.
_HELPER_MINISIGN_PUBKEY_B64 = 'RWSLOkiWKfscZzD9cOda4UFFRyOZJh5lu/lZZ56+oxa152FXiNtvuM/b'


# Pure-Python Ed25519 verification — embedded so helper.py is fully
# self-contained (no `pip install cryptography` ever). Only used by the
# /helper/update path, so a ~1 s verify on CPython is acceptable.
# Reference: ed25519.cr.yp.to/python/ed25519.py (DJB), trimmed to the
# verify path and switched to iterative scalarmult.
import hashlib as _hashlib

_ED_Q = (1 << 255) - 19
_ED_L = (1 << 252) + 27742317777372353535851937790883648493
_ED_D = (-121665 * pow(121666, _ED_Q - 2, _ED_Q)) % _ED_Q
_ED_I = pow(2, (_ED_Q - 1) // 4, _ED_Q)


def _ed_xrecover(y):
    xx = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_Q - 2, _ED_Q)
    x = pow(xx, (_ED_Q + 3) // 8, _ED_Q)
    if (x * x - xx) % _ED_Q != 0:
        x = (x * _ED_I) % _ED_Q
    if x % 2 != 0:
        x = _ED_Q - x
    return x


_ED_BY = 4 * pow(5, _ED_Q - 2, _ED_Q) % _ED_Q
_ED_BX = _ed_xrecover(_ED_BY)
_ED_B = (_ED_BX % _ED_Q, _ED_BY, 1, (_ED_BX * _ED_BY) % _ED_Q)


def _ed_add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _ED_Q
    b = (y1 + x1) * (y2 + x2) % _ED_Q
    c = t1 * 2 * _ED_D * t2 % _ED_Q
    dd = z1 * 2 * z2 % _ED_Q
    e = (b - a) % _ED_Q
    f = (dd - c) % _ED_Q
    g = (dd + c) % _ED_Q
    h = (b + a) % _ED_Q
    return (e * f % _ED_Q, g * h % _ED_Q, f * g % _ED_Q, e * h % _ED_Q)


def _ed_mult(P, e):
    R = (0, 1, 1, 0)  # identity (extended Edwards coords)
    while e > 0:
        if e & 1:
            R = _ed_add(R, P)
        P = _ed_add(P, P)
        e >>= 1
    return R


def _ed_decode(s):
    y = int.from_bytes(s, 'little') & ((1 << 255) - 1)
    x = _ed_xrecover(y)
    if (x & 1) != ((s[31] >> 7) & 1):
        x = _ED_Q - x
    return (x, y, 1, (x * y) % _ED_Q)


def _ed_encode(P):
    x, y, z, _ = P
    zi = pow(z, _ED_Q - 2, _ED_Q)
    x = (x * zi) % _ED_Q
    y = (y * zi) % _ED_Q
    out = bytearray(y.to_bytes(32, 'little'))
    out[31] |= (x & 1) << 7
    return bytes(out)


def ed25519_verify(pub32, sig64, msg):
    """Pure-Python Ed25519 verify. True iff signature is valid."""
    if len(pub32) != 32 or len(sig64) != 64:
        return False
    try:
        R = _ed_decode(sig64[:32])
        A = _ed_decode(pub32)
        s = int.from_bytes(sig64[32:], 'little')
        h = int.from_bytes(_hashlib.sha512(sig64[:32] + pub32 + msg).digest(),
                           'little') % _ED_L
        return _ed_encode(_ed_mult(_ED_B, s)) == _ed_encode(_ed_add(R, _ed_mult(A, h)))
    except Exception:
        return False


def _verify_minisign(content, sig_text):
    """Verify a minisign signature against the embedded pubkey. Handles
    BOTH algos minisign emits — modern `ED` (Ed25519 over BLAKE2b-512
    prehash, what `minisign -S` produces by default since v0.10) and
    legacy `Ed` (Ed25519 over raw bytes). Self-contained."""
    # Pubkey: base64(algo[2] + key_id[8] + ed25519_pub[32])
    pub_raw = base64.b64decode(_HELPER_MINISIGN_PUBKEY_B64)
    if len(pub_raw) < 42:
        return False
    pub32 = pub_raw[10:42]
    # Signature file: first non-comment base64 line decodes to
    # algo[2] + key_id[8] + ed25519_sig[64]
    sig64 = None
    algo = None
    for line in sig_text.splitlines():
        line = line.strip()
        if not line or line.startswith('untrusted comment') or line.startswith('trusted comment'):
            continue
        try:
            raw = base64.b64decode(line)
            if len(raw) >= 74:
                algo = bytes(raw[:2])
                sig64 = raw[10:74]
                break
        except Exception:
            continue
    if sig64 is None:
        return False
    if algo == b'ED':
        # Modern: signature was made over BLAKE2b-512(content)
        msg = _hashlib.blake2b(content, digest_size=64).digest()
    else:
        # Legacy (b'Ed') or unknown — assume raw signature
        msg = content
    return ed25519_verify(pub32, sig64, msg)


def _user_helper_cache_path() -> Path:
    """Chemin où le Tauri shell lit helper.py EN PRIORITÉ (avant le bundled
    root-owned). Cf. src-tauri/src/script_updater.rs::cache_path et
    AppHandle::app_local_data_dir() :
      - Linux   : $XDG_DATA_HOME/<bundle_id>/helper.py
                  ou ~/.local/share/<bundle_id>/helper.py
      - macOS   : ~/Library/Application Support/<bundle_id>/helper.py
      - Windows : %LOCALAPPDATA%\\<bundle_id>\\helper.py
    bundle_id = 'fr.redlinks.redstars-helper' (tauri.conf.json#identifier)."""
    bundle_id = 'fr.redlinks.redstars-helper'
    sysname = platform.system()
    if sysname == 'Linux':
        base = Path(os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share'))
    elif sysname == 'Darwin':
        base = Path.home() / 'Library' / 'Application Support'
    elif sysname == 'Windows':
        base = Path(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/AppData/Local'))
    else:
        base = Path.home() / '.local' / 'share'
    return base / bundle_id / 'helper.py'


def update_self(api_base='https://api.dev.redstars.redlinks.fr'):
    """Pull the latest signed helper.py from the platform, verify, and
    write it into the USER cache that the Tauri shell loads in priority
    (cf. `_user_helper_cache_path`). On NE TOUCHE PAS le bundled root-owned
    `/usr/lib/Redstars Helper/bundled/helper.py` — le shell est codé pour
    préférer le cache user au bundled, donc écrire là suffit. Returns a
    dict describing the outcome (caller respawns via execv if `updated`
    is True ; au prochain restart du shell le nouveau helper.py est chargé)."""
    import urllib.request, hashlib
    try:
        info_url = api_base.rstrip('/') + '/api/v1/agents/script-latest?name=helper.py'
        with urllib.request.urlopen(info_url, timeout=15) as r:
            info = json.loads(r.read())
    except Exception as e:
        return {'updated': False, 'error': f'fetch script-latest: {e}'}
    new_version = info.get('version', '?')
    if new_version == VERSION:
        return {'updated': False, 'version': VERSION, 'reason': 'already up to date'}
    try:
        with urllib.request.urlopen(info['script_url'], timeout=30) as r:
            script_bytes = r.read()
        with urllib.request.urlopen(info['signature_url'], timeout=15) as r:
            sig_text = r.read().decode('utf-8', errors='replace')
    except Exception as e:
        return {'updated': False, 'error': f'fetch release asset: {e}'}
    expected = info.get('sha256', '')
    if expected:
        got = hashlib.sha256(script_bytes).hexdigest()
        if got != expected:
            return {'updated': False, 'error': f'sha256 mismatch: expected {expected[:16]}…, got {got[:16]}…'}
    if not _verify_minisign(script_bytes, sig_text):
        return {'updated': False, 'error': 'minisign verification failed (or cryptography lib missing)'}
    # On VISE le cache user, jamais le bundled root-owned. Le Tauri shell
    # lit cache user > bundled dans script_updater.rs (resolution order).
    # Le helper.py qui tourne là maintenant continue de tourner ; l'update
    # prend effet au prochain restart du shell.
    target = _user_helper_cache_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {'updated': False, 'error': f'mkdir {target.parent}: {e}'}
    if not os.access(target.parent, os.W_OK):
        return {'updated': False, 'error': f'cannot write to {target.parent}'}
    tmp = target.with_suffix('.py.tmp')
    try:
        tmp.write_bytes(script_bytes)
        os.replace(tmp, target)
        # Le shell utilise ce marker pour afficher la version chargée
        # dans le statut + skipper le fetch si déjà à jour au prochain boot.
        (target.parent / 'helper.version').write_text(new_version)
    except Exception as e:
        return {'updated': False, 'error': f'write failed: {e}'}
    return {
        'updated': True,
        'from_version': VERSION,
        'version': new_version,
        'path': str(target),
        'size': len(script_bytes),
    }


def list_mount(mount_path):
    """Lightweight directory listing for the dashboard frame."""
    out = []
    try:
        for name in sorted(os.listdir(mount_path)):
            full = os.path.join(mount_path, name)
            try:
                st = os.stat(full)
                out.append({
                    'name': name,
                    'path': full,
                    'size': st.st_size,
                    'is_dir': os.path.isdir(full),
                })
            except OSError:
                continue
    except FileNotFoundError:
        pass
    return out


class Handler(SimpleHTTPRequestHandler):
    """Same-origin server: static files + /helper/* API endpoints."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def end_headers(self):
        # Allow CORS for /helper/* from the dev/prod dashboards. Same-origin
        # callers (page served by helper itself) get the headers too — no harm.
        origin = self.headers.get('Origin', '')
        if origin in ALLOWED_ORIGINS or self.path.startswith('/helper/'):
            allow = origin if origin in ALLOWED_ORIGINS else '*'
            self.send_header('Access-Control-Allow-Origin', allow)
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            # Private Network Access (W3C PNA spec) : Firefox 142+/Chrome 130+
            # exigent ce header dans la réponse au preflight quand une page
            # publique (https://dev.redstars.redlinks.fr) cible une IP
            # privée (127.0.0.1 via local.redlinks.fr). Sans → Firefox
            # bloque silencieusement avec "Status code: (null)".
            self.send_header('Access-Control-Allow-Private-Network', 'true')
        # Cross-origin isolation for future SharedArrayBuffer / WASM threads.
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
        self.send_header('Cross-Origin-Resource-Policy', 'cross-origin')
        super().end_headers()

    def do_OPTIONS(self):
        # Preflight for the dashboard's cross-origin /helper/* calls.
        self.send_response(204)
        self.end_headers()

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, code, body):
        self.send_response(code)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get('Content-Length', '0') or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    def do_GET(self):
        if not self.path.startswith('/helper/'):
            return super().do_GET()  # static file serve
        split = urlsplit(self.path)
        ep = split.path[len('/helper'):]  # strip prefix → /status, /lsusb, etc.
        query = parse_qs(split.query)
        if ep == '/status':
            self._json(200, {'ok': True, 'version': VERSION})
            return
        if ep == '/codec/bench-single':
            # Latence d'UNE inférence 32×32 : enc (image→hash) et dec (hash→image),
            # moyennée sur n forwards. Isole le coût per-patch (vs le débit en masse).
            err = _ensure_codec()
            if err:
                self._json(500, {'error': f'codec load failed: {err}'}); return
            import time as _t
            import numpy as _np
            try:
                from nn_numpy import enc_forward, dec_forward
                n = max(1, min(2000, int((query.get('n', ['200']) or ['200'])[0])))
                patch = _np.random.randint(0, 256, (1, 32, 32), dtype=_np.uint8)
                z = enc_forward(patch)            # warmup + latent pour le decode
                _ = dec_forward(z)
                t0 = _t.time()
                for _ in range(n):
                    enc_forward(patch)
                enc_ms = (_t.time() - t0) * 1000.0 / n
                t0 = _t.time()
                for _ in range(n):
                    dec_forward(z)
                dec_ms = (_t.time() - t0) * 1000.0 / n
                self._json(200, {
                    'n': n, 'backend': _CODEC.get('backend'),
                    'enc_ms_per_patch': round(enc_ms, 3),
                    'dec_ms_per_patch': round(dec_ms, 3),
                })
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/codec/bench-parallel':
            # Décode n patchs en SÉRIE vs en T THREADS → mesure le speedup réel
            # (les threads ne scalent que si numpy libère le GIL pendant l'einsum).
            err = _ensure_codec()
            if err:
                self._json(500, {'error': f'codec load failed: {err}'}); return
            import time as _t
            import os as _os
            import numpy as _np
            from concurrent.futures import ThreadPoolExecutor
            try:
                from nn_numpy import dec_forward
                n = max(64, min(8192, int((query.get('n', ['2048']) or ['2048'])[0])))
                T = max(1, min(16, int((query.get('threads', [str(_os.cpu_count() or 4)]) or ['4'])[0])))
                z = _np.random.randint(0, 2, (n, 8, 32, 32)).astype(_np.uint8)
                dec_forward(z[:16])                                # warmup
                t0 = _t.time()
                for i in range(0, n, 256):
                    dec_forward(z[i:i + 256])
                ser = _t.time() - t0
                cs = (n + T - 1) // T
                chunks = [z[i:i + cs] for i in range(0, n, cs)]
                t0 = _t.time()
                with ThreadPoolExecutor(max_workers=T) as ex:
                    list(ex.map(dec_forward, chunks))
                par = _t.time() - t0
                mb = n * 1024 / 1e6
                self._json(200, {
                    'n': n, 'threads': T, 'cores': _os.cpu_count(),
                    'serial_mbps': round(mb / ser, 2),
                    'parallel_mbps': round(mb / par, 2),
                    'speedup': round(ser / par, 2),
                })
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/codec/gpu-bench':
            # Bench de l'inférence GPU NATIVE (TFLite via la classe Kotlin CodecGpu,
            # interop Chaquopy). Android uniquement.
            if os.environ.get('REDSTARS_HELPER_PLATFORM') != 'android':
                self._json(200, {'skip': 'desktop — pas de CodecGpu natif'}); return
            try:
                from com.redstars.app import CodecGpu
                n = max(64, min(8192, int((query.get('n', ['2048']) or ['2048'])[0])))
                self._json(200, {
                    'status': str(CodecGpu.status()),
                    'result': str(CodecGpu.selfTest(n)),
                })
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/codec/int8-bench':
            # Compare FLOAT vs INT8 du décode codec sur le NPU (CodecGpu.benchInt8).
            if os.environ.get('REDSTARS_HELPER_PLATFORM') != 'android':
                self._json(200, {'skip': 'desktop — pas de CodecGpu natif'}); return
            try:
                from com.redstars.app import CodecGpu
                n = max(64, min(8192, int((query.get('n', ['2048']) or ['2048'])[0])))
                self._json(200, {
                    'status': str(CodecGpu.status()),
                    'result': str(CodecGpu.benchInt8(n)),
                })
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/codec/list-savedir':
            # Liste les fichiers du dossier RedStars (pour picker sans navigateur sur mobile).
            try:
                d = _save_dir()
                files = sorted(({'name': f.name, 'bytes': f.stat().st_size}
                                for f in d.iterdir() if f.is_file()), key=lambda x: x['name'])
                self._json(200, {'dir': str(d), 'files': files})
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/redDEC-job':
            # GET /redDEC-job?id=<job_id> — poll endpoint pour /redDEC-chain.
            job_id = (query.get('id', ['']) or [''])[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job is None:
                    self._json(404, {'error': 'unknown job id'}); return
                snapshot = dict(job)
            self._json(200, snapshot)
            return
        if ep == '/codec/blob':
            # GET /codec/blob?id=<blob_id> — récupère le blob encodé pour le tester
            # ailleurs. base64 si petit (≤256 Ko → copiable/QR), sinon taille + chemin.
            blob_id = (query.get('id', ['']) or [''])[0]
            if not re.fullmatch(r'[0-9a-f]{8,32}', blob_id):
                self._json(400, {'error': 'id required (hex)'}); return
            bp = BLOB_DIR / f'{blob_id}.rsn'
            if not bp.is_file():
                self._json(404, {'error': f'no such blob: {blob_id}'}); return
            sz = bp.stat().st_size
            # ≤8 Mo → base64 copiable (clipboard). Au-delà → trop gros, on rend
            # juste le chemin (le blob est un fichier à copier tel quel). QR jamais
            # possible : une ISO fait ≥376 Ko, un QR plafonne ~3 Ko.
            if sz > 8 * 1024 * 1024:
                self._json(200, {'ok': True, 'blob_id': blob_id, 'blob_bytes': sz,
                                 'too_big': True, 'path': str(bp)}); return
            import base64 as _b64
            self._json(200, {'ok': True, 'blob_id': blob_id, 'blob_bytes': sz,
                             'blob_b64': _b64.b64encode(bp.read_bytes()).decode('ascii')}); return
        if ep == '/disk':
            # Espace dispo sur la partition qui hébergera les sorties.
            # ?path=<…> ou défaut = le cache redstars-helper (= là où
            # /redDEC-chain et /refs/ écrivent).
            target = (query.get('path', ['']) or [''])[0]
            if not target:
                target = str(Path(os.environ.get('XDG_CACHE_HOME')
                                  or os.path.expanduser('~/.cache')) / 'redstars-helper')
            try:
                Path(target).mkdir(parents=True, exist_ok=True)
                usage = shutil.disk_usage(target)
                self._json(200, {
                    'ok': True, 'path': target,
                    'total': usage.total, 'free': usage.free, 'used': usage.used,
                })
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/scale':
            state = SCALE.read()
            if state.get('updated_at'):
                state['age_ms'] = int((time.time() - state['updated_at']) * 1000)
            self._json(200, state)
            return
        if ep == '/lsusb':
            try:
                out = subprocess.run(['lsusb'], capture_output=True, text=True, timeout=2)
                if out.returncode != 0:
                    self._json(500, {'error': 'lsusb exit ' + str(out.returncode), 'stderr': out.stderr})
                    return
                devices = [d for d in (parse_lsusb_line(l) for l in out.stdout.splitlines()) if d]
                self._json(200, {'devices': devices, 'count': len(devices)})
            except FileNotFoundError:
                self._json(500, {'error': 'lsusb not installed'})
            except Exception as e:
                self._json(500, {'error': type(e).__name__ + ': ' + str(e)})
            return
        if ep == '/iso/list':
            iso_id = (query.get('id', ['']) or [''])[0]
            info = MOUNTED.get(iso_id)
            if not info:
                self._json(404, {'error': 'unknown id'})
                return
            self._json(200, {
                'mount_path': info['mount_path'],
                'label': info['label'],
                'entries': list_mount(info['mount_path']),
            })
            return

        if ep == '/files/pick':
            # Picker natif multi-fichiers OU multi-dossiers via zenity/kdialog.
            # Renvoie la liste des paths choisis sur le disque (sans copier
            # les données).
            #   ?mode=files (défaut)  → fichiers multi
            #   ?mode=dirs            → dossiers multi (kdialog : 1 seul)
            mode = (query.get('mode', ['files']) or ['files'])[0].lower()
            if mode == 'dirs':
                tools = [
                    ['zenity', '--file-selection', '--directory', '--multiple', '--separator=\n'],
                    ['kdialog', '--getexistingdirectory', os.path.expanduser('~')],
                ]
            else:
                mode = 'files'
                tools = [
                    ['zenity', '--file-selection', '--multiple', '--separator=\n'],
                    ['kdialog', '--multiple', '--getopenfilename', os.path.expanduser('~')],
                ]
            paths = None
            err = None
            for cmd in tools:
                if shutil.which(cmd[0]) is None:
                    continue
                try:
                    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    if out.returncode != 0:
                        # User cancelled — return empty list, not an error
                        paths = []
                        break
                    sep = '\n' if cmd[0] == 'zenity' else ' '
                    paths = [p for p in out.stdout.strip().split(sep) if p]
                    break
                except subprocess.TimeoutExpired:
                    err = f'{cmd[0]} timeout'
                except Exception as e:
                    err = f'{type(e).__name__}: {e}'
            if paths is None:
                self._json(500, {'error': err or 'no native picker (install zenity or kdialog)'})
                return
            # enrichir avec taille + nom pour l'UI
            entries = []
            for p in paths:
                pth = Path(p)
                try:
                    st = pth.stat()
                    entries.append({'name': pth.name, 'path': str(pth), 'size': st.st_size, 'is_dir': pth.is_dir()})
                except OSError:
                    entries.append({'name': pth.name, 'path': str(pth), 'size': 0, 'is_dir': False, 'missing': True})
            self._json(200, {'paths': paths, 'entries': entries})
            return

        if ep == '/refs/list':
            refs_id = (query.get('id', ['']) or [''])[0]
            info = REFS.get(refs_id)
            if not info:
                self._json(404, {'error': 'unknown id'}); return
            self._json(200, {
                'id': refs_id,
                'mount_path': info['dir_path'],
                'label': info['label'],
                'entries': list_mount(info['dir_path']),
            })
            return

        self._json(404, {'error': 'unknown helper endpoint'})

    def do_HEAD(self):
        if not self.path.startswith('/helper/'):
            return super().do_HEAD()
        # Helper endpoints don't really do HEAD, just say OK.
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        if not self.path.startswith('/helper/'):
            self._json(404, {'error': 'POST only allowed under /helper/*'})
            return
        # Even POST endpoints can take a `?path=…` style query (e.g.
        # /helper/redEC?path=…). do_GET parses it the same way ; we
        # mirror it here so handlers can read `query[...]` uniformly.
        split = urlsplit(self.path)
        ep    = split.path[len('/helper'):]
        query = parse_qs(split.query)
        if ep == '/codec/encode-hash':
            # data brute -> hash (latents purs, SANS blob/sidecar). Padding zéro à 1024.
            try:
                import codec_numpy, numpy as _np
                n = int(self.headers.get('Content-Length', '0') or 0)
                raw = self.rfile.read(n) if n > 0 else b''
                pad = (-len(raw)) % codec_numpy.BLOCK
                arr = _np.frombuffer(raw + b'\x00' * pad, _np.uint8)
                _, lat = codec_numpy._enc_blocks(arr)
                self._raw(200, bytes(lat))
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/codec/decode-hash':
            # hash (latents purs) -> data brute (N*1024). Décode via INT8/NPU si routé.
            try:
                import codec_numpy, numpy as _np
                n = int(self.headers.get('Content-Length', '0') or 0)
                lat = self.rfile.read(n) if n > 0 else b''
                out = codec_numpy._dec_bytes(lat)
                self._raw(200, _np.asarray(out, _np.uint8).tobytes())
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/codec/encode-hash-save':
            # data brute (body) -> hash (latents) enregistré sur l'appareil. ?name=
            try:
                import codec_numpy, numpy as _np
                name = (query.get('name', ['fichier.hash']) or ['fichier.hash'])[0]
                n = int(self.headers.get('Content-Length', '0') or 0)
                raw = self.rfile.read(n) if n > 0 else b''
                pad = (-len(raw)) % codec_numpy.BLOCK
                arr = _np.frombuffer(raw + b'\x00' * pad, _np.uint8)
                _, lat = codec_numpy._enc_blocks(arr)
                p = _save_dir() / os.path.basename(name)
                p.write_bytes(bytes(lat))
                self._json(200, {'saved_path': str(p), 'in_bytes': len(raw), 'hash_bytes': len(lat)})
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/codec/decode-hash-save':
            # hash (latents, body) -> data décodée (INT8/NPU) enregistrée sur l'appareil. ?name=
            try:
                import codec_numpy, numpy as _np
                name = (query.get('name', ['fichier.bin']) or ['fichier.bin'])[0]
                n = int(self.headers.get('Content-Length', '0') or 0)
                lat = self.rfile.read(n) if n > 0 else b''
                out = _np.asarray(codec_numpy._dec_bytes(lat), _np.uint8).tobytes()
                p = _save_dir() / os.path.basename(name)
                p.write_bytes(out)
                self._json(200, {'saved_path': str(p), 'hash_bytes': len(lat), 'out_bytes': len(out)})
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/codec/encode-file-hash':
            # {name} d'un fichier du dossier RedStars -> hash (latents) au même endroit.
            try:
                import codec_numpy, numpy as _np
                name = os.path.basename(self._read_json().get('name', ''))
                src = _save_dir() / name
                if not name or not src.is_file():
                    self._json(404, {'error': f'introuvable: {name}'}); return
                raw = src.read_bytes(); pad = (-len(raw)) % codec_numpy.BLOCK
                arr = _np.frombuffer(raw + b'\x00' * pad, _np.uint8)
                _, lat = codec_numpy._enc_blocks(arr)
                p = _save_dir() / (name + '.hash'); p.write_bytes(bytes(lat))
                self._json(200, {'saved_path': str(p), 'name': name + '.hash', 'in_bytes': len(raw), 'hash_bytes': len(lat)})
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/codec/decode-file-hash':
            # {name} d'un fichier hash du dossier RedStars -> data décodée (INT8/NPU) au même endroit.
            try:
                import codec_numpy, numpy as _np
                name = os.path.basename(self._read_json().get('name', ''))
                src = _save_dir() / name
                if not name or not src.is_file():
                    self._json(404, {'error': f'introuvable: {name}'}); return
                lat = src.read_bytes()
                out = _np.asarray(codec_numpy._dec_bytes(lat), _np.uint8).tobytes()
                outname = name[:-5] if name.endswith('.hash') else name + '.decoded'
                p = _save_dir() / outname; p.write_bytes(out)
                self._json(200, {'saved_path': str(p), 'name': outname, 'hash_bytes': len(lat), 'out_bytes': len(out)})
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return
        if ep == '/enable-webgpu':
            profile = find_firefox_default_profile()
            if profile is None:
                self._json(500, {'error': 'No Firefox profile found at ~/.mozilla/firefox'})
                return
            user_js = profile / 'user.js'
            content = user_js.read_text() if user_js.exists() else ''
            additions = []
            for pref in ('dom.webgpu.enabled', 'dom.webgpu.unsafe.enabled'):
                if pref not in content:
                    additions.append(f'user_pref("{pref}", true);')
            if additions:
                header = '' if content.endswith('\n') or not content else '\n'
                user_js.write_text(content + header + '\n'.join(additions) + '\n')
            self._json(200, {
                'ok': True,
                'profile': str(profile),
                'user_js': str(user_js),
                'wrote': len(additions),
                'restart_required': True,
                'message': 'Restart Firefox for changes to take effect.' if additions
                           else 'WebGPU prefs already present in user.js.',
            })
            return

        if ep == '/reset-webgpu':
            # Strip our prefs from user.js AND force them back to default in
            # prefs.js so the next Firefox launch doesn't see cached `true`.
            profile = find_firefox_default_profile()
            if profile is None:
                self._json(500, {'error': 'No Firefox profile found at ~/.mozilla/firefox'})
                return
            actions = []
            user_js = profile / 'user.js'
            if user_js.exists():
                kept = []
                for line in user_js.read_text().splitlines():
                    if 'dom.webgpu.enabled' in line or 'dom.webgpu.unsafe.enabled' in line:
                        continue
                    if line.startswith('//') and 'WebGPU' in line:
                        continue
                    kept.append(line)
                new_content = '\n'.join(kept).strip()
                if new_content:
                    user_js.write_text(new_content + '\n')
                else:
                    user_js.unlink()
                actions.append('cleaned user.js')
            # prefs.js is rewritten by Firefox at shutdown — best-effort patch.
            # We rewrite the lines while Firefox is closed; if it's running we
            # warn the caller.
            prefs_js = profile / 'prefs.js'
            if prefs_js.exists():
                lines = prefs_js.read_text().splitlines()
                kept = [l for l in lines if 'dom.webgpu.enabled' not in l and 'dom.webgpu.unsafe.enabled' not in l]
                if len(kept) != len(lines):
                    prefs_js.write_text('\n'.join(kept) + '\n')
                    actions.append('patched prefs.js (will only stick if Firefox is closed)')
            self._json(200, {
                'ok': True,
                'profile': str(profile),
                'user_js': str(user_js),
                'action': ', '.join(actions) or 'nothing to do',
                'message': 'Close Firefox first, then reopen, for the reset to apply cleanly.',
            })
            return

        if ep == '/update':
            body = self._read_json()
            api_base = body.get('api_base') or 'https://api.dev.redstars.redlinks.fr'
            try:
                result = update_self(api_base)
            except Exception as e:
                self._json(500, {'error': type(e).__name__ + ': ' + str(e)})
                return
            self._json(200, result)
            # If we replaced our own file, exec the same interpreter on the
            # same path so we come back up with the new code. Brief sleep so
            # the response gets flushed first.
            if result.get('updated'):
                import sys as _sys
                def _restart():
                    time.sleep(0.5)
                    try:
                        os.execv(_sys.executable, [_sys.executable, str(Path(__file__).resolve())])
                    except Exception as e:
                        print(f'[update] execv failed: {e}')
                threading.Thread(target=_restart, daemon=True).start()
            return

        if ep == '/iso/mount':
            body = self._read_json()
            label = (body.get('label') or 'REDSTARS')
            payload = None
            # Two body shapes accepted:
            #   {"files": {"name.ext": "<base64>", ...}}   → multi-file ISO
            #   {"payload": "<base64>"}                    → single payload.bin
            # `files` wins if both are present.
            if isinstance(body.get('files'), dict) and body['files']:
                try:
                    payload = {name: base64.b64decode(b64)
                               for name, b64 in body['files'].items()}
                except Exception as e:
                    self._json(400, {'error': f'bad base64 in files: {e}'})
                    return
            elif body.get('payload'):
                try:
                    payload = base64.b64decode(body['payload'])
                except Exception as e:
                    self._json(400, {'error': f'bad base64 payload: {e}'})
                    return
            try:
                iso_path = make_iso(label, payload)
                mount_path, dev = mount_iso(iso_path)
            except Exception as e:
                self._json(500, {'error': type(e).__name__ + ': ' + str(e)})
                return
            iso_id = uuid.uuid4().hex[:12]
            MOUNTED[iso_id] = {
                'iso_path': str(iso_path),
                'mount_path': mount_path,
                'dev': dev,
                'label': label,
                'created_at': time.time(),
            }
            self._json(200, {
                'id': iso_id,
                'mount_path': mount_path,
                'label': label,
                'entries': list_mount(mount_path),
            })
            return

        if ep == '/iso/unmount':
            body = self._read_json()
            iso_id = body.get('id')
            info = MOUNTED.pop(iso_id, None)
            if not info:
                self._json(404, {'error': 'unknown id'})
                return
            unmount_iso(info['iso_path'], info['dev'])
            try:
                os.remove(info['iso_path'])
            except OSError:
                pass
            self._json(200, {'ok': True, 'id': iso_id})
            return

        if ep == '/iso/open':
            body = self._read_json()
            path = body.get('path', '')
            # Path must sit under a currently-mounted iso (we never open
            # arbitrary host paths on behalf of the dashboard).
            if not any(path.startswith(info['mount_path']) for info in MOUNTED.values()):
                self._json(403, {'error': 'path not in a mounted iso'})
                return
            sysname = platform.system()
            try:
                if sysname == 'Linux':
                    subprocess.Popen(['xdg-open', path])
                elif sysname == 'Darwin':
                    subprocess.Popen(['open', path])
                elif sysname == 'Windows':
                    os.startfile(path)  # type: ignore[attr-defined]
                else:
                    self._json(500, {'error': f'open unsupported on {sysname}'})
                    return
                self._json(200, {'ok': True})
            except Exception as e:
                self._json(500, {'error': type(e).__name__ + ': ' + str(e)})
            return

        if ep == '/redEC':
            err = _ensure_codec()
            if err:
                self._json(500, {'error': f'codec load failed: {err}', 'hint': 'pip install torch numpy'}); return
            # Two modes :
            #   - ?path=<absolute path>  → encode an existing file on disk.
            #     The path MUST sit under one of the active /refs/ mount
            #     dirs (same guard as /refs/open) so the browser can't
            #     point us at /etc/anything via a malicious POST.
            #   - body : raw binary, content-type application/octet-stream.
            #     Used when the file lives only in the browser.
            query_path = (query.get('path', ['']) or [''])[0]
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                in_path  = Path(td) / 'in.bin'
                out_path = Path(td) / 'out.bin'
                if query_path:
                    src = os.path.normpath(os.path.abspath(query_path))
                    allowed = False
                    for info in REFS.values():
                        mount = os.path.normpath(os.path.abspath(info['dir_path']))
                        if src == mount or src.startswith(mount + os.sep):
                            allowed = True; break
                    if not allowed:
                        self._json(403, {'error': 'path not under any active /refs/ mount'}); return
                    if not os.path.isfile(src):
                        self._json(404, {'error': f'no such file: {src}'}); return
                    in_path.write_bytes(Path(src).read_bytes())
                else:
                    n = int(self.headers.get('Content-Length', '0') or 0)
                    if n <= 0:
                        self._json(400, {'error': 'empty body and no ?path= — POST raw binary or use ?path=<refs-file>'}); return
                    in_path.write_bytes(self.rfile.read(n))
                try:
                    level, h, in_size = _CODEC['redEC_chain'](in_path, out_path)
                    self._json(200, {
                        'ok': True,
                        'level': f'Red{level}',
                        'n_chain_steps': level,
                        'input_bytes': in_size,
                        'output_hash_hex': h.hex(),
                        'output_bytes': len(h),
                    })
                except Exception as e:
                    self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return

        if ep == '/redEC/bundle':
            # Bundle plusieurs fichiers en UN seul hash. C'est le mode
            # canonique pour le "disque virtuel" : tout le payload (1 MiB
            # pour Red1, 1 GiB pour Red2, …) est encodé en une seule chaîne
            # redEC, donc UN seul hash partageable via QR. La sortie
            # redDEC-chain (modifiée pour détecter `tarfile.is_tarfile`)
            # ré-extrait les fichiers à leurs noms d'origine.
            #
            # body : {"paths": ["<abs>/a", "<abs>/b", …], "label"?: "…"}
            #        Chaque path doit être sous un /refs/ actif (même garde
            #        que /redEC), pour empêcher le browser d'aller tarrer
            #        /etc/* via un POST malicieux.
            err = _ensure_codec()
            if err:
                self._json(500, {'error': f'codec load failed: {err}', 'hint': 'pip install torch numpy'}); return
            body  = self._read_json()
            paths = body.get('paths') or []
            if not paths:
                self._json(400, {'error': 'paths required'}); return
            abs_paths = []
            for p in paths:
                src = os.path.normpath(os.path.abspath(p))
                allowed = False
                for info in REFS.values():
                    mount = os.path.normpath(os.path.abspath(info['dir_path']))
                    if src == mount or src.startswith(mount + os.sep):
                        allowed = True; break
                if not allowed:
                    self._json(403, {'error': f'path not under any active /refs/ mount: {p}'}); return
                if not os.path.isfile(src):
                    self._json(404, {'error': f'no such file: {src}'}); return
                abs_paths.append(src)
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                out_path = Path(td) / 'out.bin'
                # On construit l'iso d'ABORD avec le dict {nom: src_path}
                # — make_iso stream-copy chaque fichier (pas de RAM bloat
                # pour les gros films). Les bytes de cette iso SONT ce
                # qu'on passe à redEC_chain : pas de tar intermédiaire.
                # Au décode l'arbre du système de fichiers ISO 9660 / UDF
                # sert d'index — n'importe quelle machine qui reçoit le
                # hash + le bundle_size peut remonter l'iso et retrouver
                # les fichiers d'origine sans cache ni sidecar local.
                iso_label = (body.get('label') or 'REDSTARS')[:32]
                iso_payload = {}
                used = set()
                files_meta = []
                for src in abs_paths:
                    nm = Path(src).name
                    if nm in used:
                        base, ext = os.path.splitext(nm)
                        i = 1
                        while f'{base}-{i}{ext}' in used:
                            i += 1
                        nm = f'{base}-{i}{ext}'
                    used.add(nm)
                    iso_payload[nm] = src
                    files_meta.append({'name': nm, 'size': os.path.getsize(src)})
                try:
                    iso_path = make_iso(iso_label, payload=iso_payload)
                except Exception as e:
                    self._json(500, {'error': f'make_iso failed: {type(e).__name__}: {e}'}); return
                bundle_size = iso_path.stat().st_size
                try:
                    level, h, _ = _CODEC['redEC_chain'](iso_path, out_path)
                except Exception as e:
                    self._json(500, {'error': f'{type(e).__name__}: {e}'}); return
                iso_id = uuid.uuid4().hex[:12]
                iso_info = None
                try:
                    mount_path, dev = mount_iso(iso_path)
                    MOUNTED[iso_id] = {
                        'iso_path': str(iso_path),
                        'mount_path': mount_path,
                        'dev': dev,
                        'label': iso_label,
                        'created_at': time.time(),
                    }
                    iso_info = {
                        'iso_id': iso_id,
                        'mount_path': mount_path,
                        'label': iso_label,
                        'entries': list_mount(mount_path),
                    }
                except Exception as e:
                    # ISO mount best-effort côté envoi — si udisksctl manque
                    # ou échoue on garde quand même le hash redEC pour le
                    # partage. La sortie reste réceptionnable côté décode.
                    iso_info = {'error': f'iso mount failed: {type(e).__name__}: {e}'}
                self._json(200, {
                    'ok': True,
                    'level': f'Red{level}',
                    'n_chain_steps': level,
                    'output_hash_hex': h.hex(),
                    'output_bytes': len(h),
                    'bundle_bytes': bundle_size,
                    'n_files': len(files_meta),
                    'files': files_meta,
                    'iso': iso_info,
                })
            return

        if ep == '/codec/encode':
            # Save : files → make_iso → HASH INT8 (codec_numpy, sans torch). Async job.
            body  = self._read_json()
            paths = body.get('paths') or []
            label = (body.get('label') or 'REDSTARS')[:32]
            if not paths:
                self._json(400, {'error': 'paths required'}); return
            abs_paths = []
            for p in paths:
                src = os.path.normpath(os.path.abspath(p))
                allowed = any(
                    src == os.path.normpath(os.path.abspath(info['dir_path'])) or
                    src.startswith(os.path.normpath(os.path.abspath(info['dir_path'])) + os.sep)
                    for info in REFS.values())
                if not allowed:
                    self._json(403, {'error': f'path not under any active /refs/ mount: {p}'}); return
                if not os.path.isfile(src):
                    self._json(404, {'error': f'no such file: {src}'}); return
                abs_paths.append(src)
            job_id = _new_job('codec-encode')
            threading.Thread(target=_codec_encode_worker, args=(job_id, abs_paths, label), daemon=True).start()
            self._json(200, {'job_id': job_id}); return

        if ep == '/codec/restore':
            # Remonter : hash int8 (par id) → décode int8 → iso → mount. Async job.
            body      = self._read_json()
            hash_id   = (body.get('hash_id') or '').strip()
            iso_bytes = int(body.get('iso_bytes') or 0)
            label     = (body.get('label') or 'BUNDLE')[:32]
            if not re.fullmatch(r'[0-9a-f]{8,32}', hash_id):
                self._json(400, {'error': 'hash_id required (hex)'}); return
            job_id = _new_job('codec-restore')
            threading.Thread(target=_codec_restore_worker, args=(job_id, hash_id, iso_bytes, label), daemon=True).start()
            self._json(200, {'job_id': job_id}); return

        if ep == '/codec/restore-data':
            # Remonter un hash int8 COLLÉ (hex, encodé ailleurs). Async job.
            body  = self._read_json()
            hex_  = (body.get('hash_hex') or '').strip().lower().replace(' ', '')
            iso_bytes = int(body.get('iso_bytes') or 0)
            label = (body.get('label') or 'BUNDLE')[:32]
            if not re.fullmatch(r'[0-9a-f]+', hex_) or len(hex_) % 2048 != 0:
                self._json(400, {'error': 'hash_hex invalide (hex, multiple de 2048)'}); return
            job_id = _new_job('codec-restore-data')
            threading.Thread(target=_codec_restore_data_worker, args=(job_id, bytes.fromhex(hex_), iso_bytes, label), daemon=True).start()
            self._json(200, {'job_id': job_id}); return

        if ep == '/codec/iso-list':
            # Liste les fichiers à la racine d'un ISO DÉJÀ décodé (sans le monter).
            # Mobile : après restore (mount KO), on affiche le contenu sur la page.
            body = self._read_json()
            iso_id = (body.get('id') or '').strip()
            if not re.fullmatch(r'[0-9a-f]{8,32}', iso_id):
                self._json(400, {'error': 'id requis (hex)'}); return
            iso_path = ISO_CACHE_DIR / f'{iso_id}.iso'
            if not iso_path.is_file():
                self._json(404, {'error': 'iso introuvable (expiré ?)'}); return
            try:
                self._json(200, {'id': iso_id, 'files': _iso_list(str(iso_path))})
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return

        if ep == '/codec/save-file':
            # Extrait UN fichier de l'ISO décodé vers le dossier accessible
            # (sans app externe). Renvoie le chemin enregistré.
            body = self._read_json()
            iso_id = (body.get('id') or '').strip()
            name   = Path(str(body.get('name') or '')).name      # anti path-traversal
            if not re.fullmatch(r'[0-9a-f]{8,32}', iso_id) or not name:
                self._json(400, {'error': 'id (hex) + name requis'}); return
            iso_path = ISO_CACHE_DIR / f'{iso_id}.iso'
            if not iso_path.is_file():
                self._json(404, {'error': 'iso introuvable (expiré ?)'}); return
            try:
                dst = _save_dir() / name
                size = _iso_extract_to(str(iso_path), name, str(dst))
                self._json(200, {'ok': True, 'saved_path': str(dst), 'size': size})
            except FileNotFoundError:
                self._json(404, {'error': f'fichier absent de l\'iso: {name}'})
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return

        if ep == '/redDEC':
            err = _ensure_codec()
            if err:
                self._json(500, {'error': f'codec load failed: {err}', 'hint': 'pip install torch numpy'}); return
            body = self._read_json()
            hash_hex = (body.get('hash_hex') or '').strip().lower()
            if len(hash_hex) != 2048 or any(c not in '0123456789abcdef' for c in hash_hex):
                self._json(400, {'error': 'hash_hex must be exactly 2048 hex chars (= 8192 bits = 1 BYTEA)'}); return
            try:
                out_bytes  = _CODEC['redDEC'](bytes.fromhex(hash_hex))
                n_hashes   = len(out_bytes) // 1024
                hashes_hex = [out_bytes[i*1024:(i+1)*1024].hex() for i in range(n_hashes)]
                n_distinct = len(set(hashes_hex))
                self._json(200, {
                    'ok': True,
                    'input_hash_hex': hash_hex,
                    'output_bytes': len(out_bytes),
                    'n_hashes': n_hashes,
                    'n_distinct': n_distinct,
                    'hashes_hex': hashes_hex,
                })
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return

        if ep == '/redDEC-chain':
            # POST → spawn un job thread, retourne {job_id} immédiatement.
            # Le client poll /redDEC-job?id=<job_id> pour progress + résultat.
            # Tous les niveaux sont async (cohérent) — Red1 finit en ~1 s,
            # Red2 en minutes, Red3/4 en heures.
            err = _ensure_codec()
            if err:
                self._json(500, {'error': f'codec load failed: {err}', 'hint': 'pip install torch numpy'}); return
            body = self._read_json()
            hash_hex = (body.get('hash_hex') or '').strip().lower()
            level    = int(body.get('level') or 1)
            name     = (body.get('name') or 'decoded.bin').strip() or 'decoded.bin'
            target_size = body.get('size')
            if len(hash_hex) != 2048 or any(c not in '0123456789abcdef' for c in hash_hex):
                self._json(400, {'error': 'hash_hex must be exactly 2048 hex chars'}); return
            if level < 1 or level > 4:
                self._json(400, {'error': 'level must be 1..4'}); return
            # Pré-check disque AVANT de lancer le thread — on doit pouvoir
            # écrire 1024^(level+1) octets (Red1 = 1 Mio … Red4 = 1 Pio).
            expected_out = 1024 ** (level + 1)
            cache_root = Path(os.environ.get('XDG_CACHE_HOME')
                              or os.path.expanduser('~/.cache')) / 'redstars-helper' / 'decoded'
            cache_root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(cache_root).free
            if target_size and int(target_size) > 0:
                # Si on connaît la vraie sortie utile, on check ça (Red3
                # d'un 11 GiB tar n'a besoin que de 11 GiB libre).
                needed = int(target_size)
            else:
                needed = expected_out
            if free < int(needed * 1.1):
                self._json(507, {
                    'error': 'not enough free disk',
                    'expected_output_bytes': needed,
                    'free_bytes': free,
                    'path': str(cache_root),
                }); return
            # Spawn worker thread, retourne le job_id au caller.
            job_id = _new_job('redDEC-chain')
            t = threading.Thread(
                target=_redDEC_chain_worker,
                args=(job_id, hash_hex, level, name, target_size),
                daemon=True,
                name=f'redDEC-{job_id[:8]}',
            )
            t.start()
            self._json(202, {'ok': True, 'job_id': job_id, 'level': level})
            return


        if ep == '/refs/mount':
            body  = self._read_json()
            paths = body.get('paths') or []
            label = (body.get('label') or 'REDSTARS').strip() or 'REDSTARS'
            if not paths:
                self._json(400, {'error': 'paths required'}); return
            # valider que chaque path existe AVANT de créer quoi que ce soit
            missing = [p for p in paths if not Path(p).exists()]
            if missing:
                self._json(400, {'error': 'path not found', 'missing': missing}); return
            REFS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            refs_id = uuid.uuid4().hex[:12]
            dir_path = REFS_CACHE_DIR / f'{label}-{refs_id}'
            try:
                dir_path.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                self._json(500, {'error': 'tmp dir collision'}); return
            entries = []
            for p in paths:
                src = Path(p).resolve()
                # éviter les collisions de nom : suffixer si nécessaire
                base = src.name
                target = dir_path / base
                i = 1
                while target.exists():
                    stem = src.stem; ext = src.suffix
                    target = dir_path / f'{stem}-{i}{ext}'
                    i += 1
                try:
                    target.symlink_to(src)
                    st = src.stat()
                    entries.append({
                        'name': target.name, 'path': str(target),
                        'target': str(src), 'size': st.st_size,
                        'is_dir': src.is_dir(),
                    })
                except OSError as e:
                    self._json(500, {'error': f'symlink failed: {e}', 'path': str(src)}); return
            REFS[refs_id] = {
                'dir_path': str(dir_path),
                'label': label,
                'sources': [str(Path(p).resolve()) for p in paths],
                'created_at': time.time(),
            }
            self._json(200, {
                'id': refs_id,
                'mount_path': str(dir_path),
                'label': label,
                'entries': entries,
            })
            return

        if ep == '/refs/add':
            # Ajoute des symlinks à un mount /refs/ existant — pour pouvoir
            # cumuler plusieurs sélections (fichiers + dossiers) tant que
            # le user n'a pas cliqué Clore. La sortie est juste la liste
            # des entrées ajoutées (le caller les append dans son state).
            body = self._read_json()
            refs_id   = body.get('id', '')
            new_paths = body.get('paths') or []
            info = REFS.get(refs_id)
            if not info:
                self._json(404, {'error': 'unknown id'}); return
            if not new_paths:
                self._json(400, {'error': 'paths required'}); return
            dir_path = Path(info['dir_path'])
            if not dir_path.is_dir():
                self._json(500, {'error': 'mount dir disappeared'}); return
            added = []
            for p in new_paths:
                src = Path(p)
                if not src.exists():
                    added.append({
                        'name': src.name, 'path': str(dir_path / src.name),
                        'target': str(src), 'size': 0, 'is_dir': False, 'missing': True,
                    })
                    continue
                src_res = src.resolve()
                # Évite les collisions de nom comme dans /refs/mount.
                base   = src_res.name
                target = dir_path / base
                i = 1
                while target.exists():
                    stem = src_res.stem; ext = src_res.suffix
                    target = dir_path / f'{stem}-{i}{ext}'
                    i += 1
                try:
                    target.symlink_to(src_res)
                    st = src_res.stat()
                    added.append({
                        'name': target.name, 'path': str(target),
                        'target': str(src_res), 'size': st.st_size,
                        'is_dir': src_res.is_dir(),
                    })
                except OSError as e:
                    self._json(500, {'error': f'symlink failed: {e}', 'path': str(src_res)}); return
            info['sources'].extend(str(Path(p).resolve()) for p in new_paths)
            self._json(200, {'ok': True, 'added': added})
            return

        if ep == '/refs/open':
            body = self._read_json()
            refs_id = body.get('id', '')
            info = REFS.get(refs_id)
            if not info:
                # Fallback : depuis 0.5.18 le decode et le bundle save
                # construisent une vraie iso via make_iso → l'entry vit
                # dans MOUNTED, pas REFS. Aligne la forme attendue par la
                # suite (dir_path).
                m = MOUNTED.get(refs_id)
                if m:
                    info = {'dir_path': m['mount_path'], 'label': m.get('label', '')}
                else:
                    self._json(404, {'error': 'unknown id'}); return
            target = body.get('path') or info['dir_path']
            # Path must sit under the refs dir we created. We use absolute() +
            # normpath, NOT resolve() — the refs are symlinks pointing OUTSIDE
            # the tmpdir (their whole purpose). resolve() would follow them
            # and the check would always fail. We trust the symlinks we wrote
            # at mount time; what we're guarding against is the caller asking
            # to open `.../../../etc/passwd` literally, which normpath catches.
            mount_norm  = os.path.normpath(os.path.abspath(info['dir_path']))
            target_norm = os.path.normpath(os.path.abspath(target))
            if target_norm != mount_norm and not target_norm.startswith(mount_norm + os.sep):
                self._json(403, {'error': 'path not in refs mount'}); return
            # Pick the right opener. Symlinks to a directory must land
            # in a file manager, NOT xdg-open — VSCode/VSCodium and
            # other editors register themselves as inode/directory
            # handlers ("Open Folder…"), which hijacks the mount and
            # the user never sees their files in a real explorer. For
            # plain files we keep xdg-open: the user's MIME defaults
            # are their choice (e.g. VSCodium for text/plain is fine).
            # Pick the opener. For DIRECTORIES we MUST bypass xdg-open
            # because editors (VSCodium, VSCode, Cursor, IntelliJ, …)
            # register themselves as `inode/directory` handlers via
            # their .desktop file ("Open Folder…"), and on many distros
            # they end up as the default. xdg-open then hands the mount
            # to the editor and the user never sees it in a real file
            # manager. For plain FILES we keep xdg-open : the user's
            # MIME defaults are their choice.
            #
            # FILE_MANAGERS_LINUX is intentionally large : every major
            # FM (GTK/Qt/KDE/MATE/Cinnamon/XFCE/LXQt/UKUI/Deepin/…)
            # ships a single well-known binary, we try them in order
            # and pick the first present. Order matters : a user who
            # has both Thunar and Nautilus installed gets the one that
            # ships with their session DE (XDG_CURRENT_DESKTOP hint).
            FILE_MANAGERS_LINUX = [
                'thunar', 'nautilus', 'dolphin', 'nemo', 'caja',
                'pcmanfm-qt', 'pcmanfm', 'peony', 'konqueror',
                'krusader', 'spacefm', 'index.fm', 'qtfm',
                'dde-file-manager', 'cosmic-files', 'nautilus-desktop',
                'gnome-files', 'Files',
            ]
            sysname = platform.system()
            try:
                is_dir = os.path.isdir(target)  # follows symlinks
                opener_label = 'xdg'
                if sysname == 'Linux' or sysname.endswith('BSD'):
                    cmd = None
                    if is_dir:
                        # Bias toward the FM bundled with the current
                        # session DE so a Thunar+Nautilus box on XFCE
                        # gets Thunar, on GNOME gets Nautilus, etc.
                        dt = (os.environ.get('XDG_CURRENT_DESKTOP', '') + ':'
                              + os.environ.get('XDG_SESSION_DESKTOP', '')).lower()
                        de_map = {'xfce': 'thunar', 'gnome': 'nautilus',
                                  'kde': 'dolphin', 'plasma': 'dolphin',
                                  'mate': 'caja', 'cinnamon': 'nemo',
                                  'lxqt': 'pcmanfm-qt', 'lxde': 'pcmanfm',
                                  'ukui': 'peony', 'deepin': 'dde-file-manager',
                                  'cosmic': 'cosmic-files'}
                        preferred = next((fm for tok, fm in de_map.items() if tok in dt), None)
                        order = ([preferred] if preferred else []) + \
                                [f for f in FILE_MANAGERS_LINUX if f != preferred]
                        for fm in order:
                            if shutil.which(fm):
                                cmd = [fm, target]
                                opener_label = f'fm:{fm}'
                                break
                    if cmd is None:
                        cmd = ['xdg-open', target]
                    subprocess.Popen(cmd)
                elif sysname == 'Darwin':
                    # macOS `open <dir>` opens Finder on that path.
                    # No editor hijack issue : Finder always wins for
                    # bare `open` on directories.
                    subprocess.Popen(['open', target])
                    opener_label = 'finder' if is_dir else 'xdg'
                elif sysname == 'Windows':
                    # Windows `explorer.exe <dir>` opens File Explorer.
                    # os.startfile on a directory normally does the
                    # same but explicit explorer is more predictable.
                    if is_dir:
                        subprocess.Popen(['explorer', target])
                        opener_label = 'explorer'
                    else:
                        os.startfile(target)  # type: ignore[attr-defined]
                else:
                    self._json(500, {'error': f'open unsupported on {sysname}'}); return
                self._json(200, {'ok': True, 'path': target, 'opener': opener_label})
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
            return

        if ep == '/refs/unmount':
            body = self._read_json()
            refs_id = body.get('id', '')
            info = REFS.pop(refs_id, None)
            if not info:
                self._json(404, {'error': 'unknown id'}); return
            try:
                # supprimer les symlinks puis le répertoire ; ne touche pas aux fichiers cibles
                d = Path(info['dir_path'])
                if d.exists():
                    for child in d.iterdir():
                        try: child.unlink()
                        except OSError: pass
                    d.rmdir()
            except OSError as e:
                self._json(500, {'error': f'unmount failed: {e}', 'id': refs_id, 'dir': info['dir_path']}); return
            self._json(200, {'ok': True, 'id': refs_id})
            return

        self._json(404, {'error': 'unknown helper endpoint'})

    def log_message(self, fmt, *args):
        # Silent — uncomment to debug
        pass


def _serve_thread(server, label):
    print(f'  {label}: ready')
    try:
        # poll_interval=30 (default 0.5): serve_forever wakes only to re-check
        # an internal shutdown flag we never set (the helper exits via signal /
        # process kill, not server.shutdown()). A long interval keeps the agent
        # dormant — a few wakeups per minute instead of ~2/s — when idle.
        server.serve_forever(poll_interval=30)
    except Exception as e:
        print(f'  {label} crashed: {e}')


def _route_int8_if_safe():
    """Android : route le décode codec vers le modèle INT8 (NPU) via CodecGpu,
    mais UNIQUEMENT si l'int8 est bit-exact vs float sur CE device (sinon perte
    silencieuse car le sidecar est calculé pour le float). Vérifié au démarrage
    par un petit benchInt8 (mismatch=0). Sinon le décode reste sur numpy."""
    if os.environ.get('REDSTARS_HELPER_PLATFORM') != 'android':
        return
    try:
        import numpy as _np
        import codec_numpy
        from com.redstars.app import CodecGpu
        st = str(CodecGpu.status())
        if 'error' in st or 'int8=none' in st or 'int8=absent' in st:
            print(f'  [int8] indisponible ({st}) — decode reste numpy'); return
        bench = str(CodecGpu.benchInt8(64))
        if 'mismatch=0/' not in bench:
            print(f'  [int8] NON bit-exact sur ce device — decode reste numpy ({bench[:90]})'); return
        _orig = codec_numpy._dec_bytes
        def _dec_int8(lat_bytes):
            try:
                return _np.frombuffer(bytes(CodecGpu.decodeI8(bytes(lat_bytes))), _np.uint8)
            except Exception:
                return _orig(lat_bytes)
        codec_numpy._dec_bytes = _dec_int8
        print(f'  [int8] decode route vers INT8/NPU (bit-exact OK) — {st} | {bench[:90]}')
        # Encode int8 : même garantie (int8-enc == float-enc bit-exact, vérifié). On
        # route _enc_blocks vers CodecGpu.encodeI8 (le conv tourne sur NPU) et on
        # reconstruit z par unpackbits pour que le sidecar de correction (calculé dans
        # encode()/encode_file() à partir de z) reste rigoureusement identique.
        enc_bench = str(CodecGpu.benchEncodeInt8(64))
        if 'enc_int8_vs_float_mismatch=0/' in enc_bench:
            _orig_enc = codec_numpy._enc_blocks
            def _enc_int8(raw):
                try:
                    lat = bytes(CodecGpu.encodeI8(raw.tobytes()))
                    b = len(raw) // 1024
                    z = _np.unpackbits(_np.frombuffer(lat, _np.uint8).reshape(b, 1024),
                                       axis=1).reshape(b, 8, 32, 32)
                    return z, lat
                except Exception:
                    return _orig_enc(raw)
            codec_numpy._enc_blocks = _enc_int8
            print(f'  [int8] encode route vers INT8/NPU (bit-exact OK) — {enc_bench[:90]}')
            # Le sidecar (dans encode()/encode_file()) appelle dec_forward(z) DIRECTEMENT
            # (pas _dec_bytes) → ce decode de verif restait sur CPU et dominait le cout de
            # l'encode. On le route AUSSI vers le NPU (decode deja prouve bit-exact) →
            # encode 100% NPU (les 2 passes : enc + decode-sidecar), ~2x plus rapide.
            _orig_dfwd = codec_numpy.dec_forward
            def _dfwd_int8(z):
                try:
                    lat = _np.packbits(z.reshape(len(z), -1), axis=1).tobytes()
                    return _np.frombuffer(bytes(CodecGpu.decodeI8(lat)),
                                          _np.uint8).reshape(len(z), 32, 32)
                except Exception:
                    return _orig_dfwd(z)
            codec_numpy.dec_forward = _dfwd_int8
            print('  [int8] sidecar dec_forward route aussi vers INT8/NPU (encode 100% NPU)')
        else:
            print(f'  [int8] encode NON bit-exact — encode reste numpy ({enc_bench[:90]})')
    except Exception as e:
        print(f'  [int8] routage echoue ({type(e).__name__}: {e}) — decode/encode reste numpy')


def main():
    # SO_REUSEADDR : sur Android, l'OS garde le socket 30-120 s en TIME_WAIT
    # après un crash du process. Sans REUSEADDR, le redémarrage de l'app
    # (auto-update du script_updater ou retour foreground) → EADDRINUSE
    # → startupError → MobileBlocker. Idem desktop si le user relance le
    # tray avant la fin du TIME_WAIT.
    HTTPServer.allow_reuse_address = True
    # HTTP server on :8080 — page + helper API, same-origin path.
    http_srv = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'redstars-helper {VERSION}')
    print(f'  static files from {DEMO_DIR}')
    _route_int8_if_safe()   # Android : décode codec via INT8/NPU si bit-exact

    print(f'  HTTP  http://0.0.0.0:{PORT}/  +  /helper/*')

    # HTTPS server on :8443 with the local.redlinks.fr cert — required for
    # HTTPS dashboards (dev.redstars.redlinks.fr) to reach /helper/* without
    # mixed-content blocks. local.redlinks.fr is a public DNS A record
    # pointing at 127.0.0.1; the cert is from Let's Encrypt DNS-01.
    https_thread = None
    cert_path, key_path = _resolve_cert_paths()
    if cert_path and key_path and cert_path.is_file() and key_path.is_file():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        # Bind 127.0.0.1 only — the cert is for local.redlinks.fr which always
        # resolves to 127.0.0.1, and we don't want to expose the helper to LAN
        # over HTTPS (the cert/key would let any LAN box impersonate us).
        https_srv = HTTPServer(('127.0.0.1', HTTPS_PORT), Handler)
        https_srv.socket = ctx.wrap_socket(https_srv.socket, server_side=True)
        print(f'  HTTPS https://local.redlinks.fr:{HTTPS_PORT}/  +  /helper/*')
        https_thread = threading.Thread(target=_serve_thread, args=(https_srv, 'HTTPS'), daemon=True)
        https_thread.start()
    else:
        print('  HTTPS: skipped — no cert.pem/key.pem on disk and embedded fallback failed')
    try:
        http_srv.serve_forever(poll_interval=30)  # see _serve_thread: stay dormant when idle
    except KeyboardInterrupt:
        print('\nbye')


if __name__ == '__main__':
    main()
