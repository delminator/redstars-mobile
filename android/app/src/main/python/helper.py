#!/usr/bin/env python3
"""Unified static + helper HTTP server for the autoencoder demo page.

Static files: served from this script's directory (the demo dir).
Helper API: under /helper/* — same-origin so it works from any client (mobile,
LAN, private window) without CORS dance.

Endpoints (all under /helper):
  GET  /helper/status         →  {"ok": true, "version": "..."}
  GET  /helper/lsusb          →  {"devices": [{bus, device, id, name}, ...]}
  GET  /helper/scale          →  latest scale reading (cached)
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
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

VERSION = '0.5.10'
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
_CODEC = {'loaded': False, 'redEC_chain': None, 'redDEC': None, 'err': None}

def _ensure_codec():
    if _CODEC['loaded'] or _CODEC['err']:
        return _CODEC['err']
    try:
        import sys as _sys
        if str(DEMO_DIR) not in _sys.path:
            _sys.path.insert(0, str(DEMO_DIR))
        from redEC import redEC_chain
        from redDEC import redDEC
        _CODEC['redEC_chain'] = redEC_chain
        _CODEC['redDEC'] = redDEC
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
    """Background thread reading the scale's serial output and caching the
    latest stable value. /scale endpoint just returns the cache.

    Auto-reconnects if the serial port disappears (unplug/replug).
    """
    def __init__(self, port=SCALE_PORT, baud=SCALE_BAUD):
        self.port = port
        self.baud = baud
        self.lock = threading.Lock()
        self.state = {
            'connected': False, 'value': None, 'unit': None, 'sign': None,
            'status': None, 'raw': None, 'updated_at': None, 'error': None,
        }
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def get(self):
        with self.lock:
            return dict(self.state)

    def _run(self):
        try:
            import serial
        except ImportError:
            self._set(error='pyserial not installed (pip install --user pyserial)')
            return
        while not self._stop.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=1) as ser:
                    self._set(connected=True, error=None)
                    while not self._stop.is_set():
                        raw = ser.readline().decode('ascii', errors='replace').strip()
                        if not raw:
                            continue
                        m = SCALE_LINE.search(raw)
                        if m:
                            sign = -1 if m.group('sign') == '-' else 1
                            self._set(
                                connected=True,
                                value=sign * float(m.group('value')),
                                unit=(m.group('unit') or 'g').lower(),
                                sign=m.group('sign'),
                                status=m.group('status'),
                                raw=raw,
                                updated_at=time.time(),
                                error=None,
                            )
                        else:
                            # Boot messages, blank lines, etc — keep raw for debug
                            self._set(raw=raw, updated_at=time.time(), error=None)
            except FileNotFoundError:
                self._set(connected=False, error=f'{self.port} not present (scale unplugged?)')
                time.sleep(2)
            except PermissionError:
                self._set(connected=False, error=f'{self.port} permission denied (udev rule?)')
                time.sleep(5)
            except Exception as e:
                self._set(connected=False, error=type(e).__name__ + ': ' + str(e))
                time.sleep(2)


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
MOUNTED = {}  # iso_id → {'iso_path','mount_path','dev','label','created_at'}

# Refs « par référence » : un répertoire temporaire de symlinks vers des fichiers
# du disque, partagé avec l'utilisateur via xdg-open. Pas de copie des données,
# juste des liens symboliques. Cleanup à la demande via /helper/refs/unmount.
REFS_CACHE_DIR = Path.home() / '.cache' / 'redstars-helper' / 'refs'
REFS = {}  # refs_id → {'dir_path','label','sources':[...],'created_at'}


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
                (src_dir / safe).write_bytes(bytes(data))
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


def update_self(api_base='https://api.dev.redstars.redlinks.fr'):
    """Pull the latest signed helper.py from the platform, verify, and
    write it next to the running script. Returns a dict describing the
    outcome (caller respawns via execv if `updated` is True)."""
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
    target = Path(__file__).resolve()
    if not os.access(target.parent, os.W_OK):
        return {'updated': False, 'error': f'cannot write to {target.parent}'}
    tmp = target.with_suffix('.py.tmp')
    try:
        tmp.write_bytes(script_bytes)
        os.replace(tmp, target)
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
            state = SCALE.get()
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
            # Chaîne `level` applications de redDEC pour reconstruire le
            # fichier d'origine. Niveau N → 1 + 1024 + … + 1024^(N-1)
            # appels redDEC, sortie 1024^N octets (ré-tronquée par
            # `size` si fournie). On écrit le résultat dans
            # ~/.cache/redstars-helper/decoded/, on le monte direct via
            # un /refs/, et on renvoie le mount au caller — un seul
            # round-trip côté browser.
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
            if level > 2:
                # ~1 M appels neuronaux pour Red3, ~1 G pour Red4. Hors
                # de portée d'un serveur HTTP synchrone ; on refuse
                # explicitement plutôt que de bloquer pendant des heures.
                self._json(501, {
                    'error': f'Red{level} unsupported pour l\'instant',
                    'detail': f'{1024**(level-1)} appels redDEC nécessaires — '
                              'faisable mais demande un job worker dédié, pas '
                              'une requête HTTP. Red1/Red2 OK.',
                }); return
            try:
                # Pré-check : la sortie après `level` étapes redDEC contient
                # 1024^level hashes × 1024 octets = 1024^(level+1) octets.
                # Red1 = 1 Mio, Red2 = 1 Gio, Red3 = 1 Tio, Red4 = 1 Pio.
                # Si on n'a clairement pas la place sur le disque cible,
                # on refuse avant de lancer 1k+ appels neuronaux.
                expected_out = 1024 ** (level + 1)
                cache_root = Path(os.environ.get('XDG_CACHE_HOME')
                                  or os.path.expanduser('~/.cache')) / 'redstars-helper' / 'decoded'
                cache_root.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(cache_root).free
                # Marge de 10 % pour ne pas saturer la partition.
                if free < int(expected_out * 1.1):
                    self._json(507, {
                        'error': 'not enough free disk',
                        'expected_output_bytes': expected_out,
                        'free_bytes': free,
                        'path': str(cache_root),
                    }); return
                # Chaîne redDEC `level` fois. À chaque étape on découpe
                # le 1 Mo de sortie en 1024 hashes filles.
                current = [bytes.fromhex(hash_hex)]
                for step in range(level):
                    nxt = []
                    for h in current:
                        decoded = _CODEC['redDEC'](h)  # 1 Mo
                        for i in range(1024):
                            nxt.append(decoded[i*1024:(i+1)*1024])
                    current = nxt
                out_bytes = b''.join(current)
                if target_size and int(target_size) > 0:
                    out_bytes = out_bytes[:int(target_size)]

                # Cache + mount.
                cache_root = Path(os.environ.get('XDG_CACHE_HOME')
                                  or os.path.expanduser('~/.cache')) / 'redstars-helper' / 'decoded'
                cache_root.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r'[^A-Za-z0-9._-]', '_', name)[:120] or 'decoded.bin'
                out_path = cache_root / f'{hash_hex[:8]}-{safe}'
                out_path.write_bytes(out_bytes)

                # Monte le fichier décodé via le mécanisme /refs/ pour que
                # le caller récupère un MountInfo prêt à brancher.
                REFS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                refs_id  = uuid.uuid4().hex[:12]
                label    = f'DECODED-Red{level}'
                dir_path = REFS_CACHE_DIR / f'{label}-{refs_id}'
                dir_path.mkdir(parents=True, exist_ok=False)
                link_target = dir_path / out_path.name
                link_target.symlink_to(out_path)
                REFS[refs_id] = {
                    'dir_path': str(dir_path),
                    'label': label,
                    'sources': [str(out_path)],
                    'created_at': time.time(),
                }
                self._json(200, {
                    'ok': True,
                    'level': level,
                    'output_path': str(out_path),
                    'output_size': len(out_bytes),
                    'id': refs_id,
                    'mount_path': str(dir_path),
                    'label': label,
                    'entries': [{
                        'name': link_target.name,
                        'path': str(link_target),
                        'target': str(out_path),
                        'size': len(out_bytes),
                        'is_dir': False,
                    }],
                })
            except Exception as e:
                self._json(500, {'error': f'{type(e).__name__}: {e}'})
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
        server.serve_forever()
    except Exception as e:
        print(f'  {label} crashed: {e}')


def main():
    # HTTP server on :8080 — page + helper API, same-origin path.
    http_srv = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'redstars-helper {VERSION}')
    print(f'  static files from {DEMO_DIR}')

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
        http_srv.serve_forever()
    except KeyboardInterrupt:
        print('\nbye')


if __name__ == '__main__':
    main()
