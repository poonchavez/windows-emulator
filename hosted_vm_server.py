#!/usr/bin/env python3
"""Local proof-of-concept host for browser-streamed QEMU Windows VMs.

Only bind this development server to localhost. It is not an authenticated,
multi-tenant public hosting service.
"""

import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from select import select
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
IMAGE_ROOT = Path(os.environ.get("WINDOWS_VM_IMAGE_ROOT", ROOT / "vm-images")).expanduser().resolve()
PORT = int(os.environ.get("WINDOWS_VM_PORT", "8090"))
MAX_SESSIONS = max(1, int(os.environ.get("WINDOWS_VM_MAX_SESSIONS", "2")))
IDLE_SECONDS = int(os.environ.get("WINDOWS_VM_IDLE_SECONDS", str(3 * 60 * 60)))
QEMU = os.environ.get("QEMU_BIN") or shutil.which("qemu-system-x86_64")
QEMU_IMG = os.environ.get("QEMU_IMG_BIN") or shutil.which("qemu-img")
SWTPM = os.environ.get("SWTPM_BIN") or shutil.which("swtpm")

PROFILES = {
    "xp": {"name": "Windows XP", "memory": "1024", "cores": "1", "uefi": False},
    "windows-vista": {"name": "Windows Vista", "memory": "2048", "cores": "2", "uefi": True},
    "windows-7": {"name": "Windows 7", "memory": "2048", "cores": "2", "uefi": True},
    "windows-8": {"name": "Windows 8", "memory": "2048", "cores": "2", "uefi": True},
    "windows-8-1": {"name": "Windows 8.1", "memory": "4096", "cores": "2", "uefi": True},
}

sessions = {}
sessions_lock = threading.Lock()


def find_ovmf():
    configured_code = os.environ.get("OVMF_CODE")
    configured_vars = os.environ.get("OVMF_VARS")
    if configured_code and configured_vars:
        code, variables = Path(configured_code), Path(configured_vars)
        if code.is_file() and variables.is_file():
            return code, variables

    roots = [
        Path("/opt/homebrew/share/qemu"), Path("/usr/local/share/qemu"), Path("/opt/local/share/qemu"),
        Path("/usr/share/OVMF"), Path("/usr/share/ovmf"), Path("/usr/share/edk2/ovmf"),
        Path("/usr/share/edk2/x64"), Path("/usr/share/edk2-ovmf/x64"), Path("/usr/share/qemu"),
    ]
    names = [
        ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"),
        ("OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd"),
        ("OVMF_CODE.fd", "OVMF_VARS.fd"),
        ("edk2-x86_64-code.fd", "edk2-i386-vars.fd"),
    ]
    for root in roots:
        for code_name, vars_name in names:
            code, variables = root / code_name, root / vars_name
            if code.is_file() and variables.is_file():
                return code, variables
    return None


def status_data():
    qemu_ok = bool(QEMU and Path(QEMU).is_file())
    qemu_img_ok = bool(QEMU_IMG and Path(QEMU_IMG).is_file())
    ovmf = find_ovmf()
    result = []
    for key, profile in PROFILES.items():
        disk = IMAGE_ROOT / key / "disk.qcow2"
        ready = qemu_ok and qemu_img_ok and disk.is_file() and (not profile["uefi"] or ovmf is not None)
        result.append({
            "id": key,
            "name": profile["name"],
            "ready": ready,
            "reason": "Ready" if ready else missing_reason(profile, disk, qemu_ok, qemu_img_ok, ovmf),
        })
    return {
        "versions": result,
        "host": {
            "qemu": qemu_ok,
            "qemuImg": qemu_img_ok,
            "swtpm": bool(SWTPM and Path(SWTPM).is_file()),
            "ovmf": ovmf is not None,
            "maxSessions": MAX_SESSIONS,
            "activeSessions": len(sessions),
        },
    }


def missing_reason(profile, disk, qemu_ok, qemu_img_ok, ovmf):
    if not qemu_ok:
        return "Install QEMU on the host"
    if not qemu_img_ok:
        return "Install qemu-img on the host"
    if profile["uefi"] and ovmf is None:
        return "Install OVMF/EDK II firmware on the host"
    if not disk.is_file():
        return "Preinstall a licensed VM image on the host"
    return "Unavailable"


def reserve_ports():
    for display in range(30, 200):
        vnc_port = 5900 + display
        websocket_port = 5700 + display
        vnc = socket.socket()
        websocket = socket.socket()
        try:
            vnc.bind(("127.0.0.1", vnc_port))
            websocket.bind(("127.0.0.1", websocket_port))
            return display, vnc_port, websocket_port
        except OSError:
            pass
        finally:
            vnc.close()
            websocket.close()
    raise RuntimeError("No free local QEMU display ports are available")


def host_acceleration():
    system = platform.system().lower()
    if system == "darwin":
        return "pc,accel=hvf"
    if system == "linux" and Path("/dev/kvm").exists():
        return "pc,accel=kvm"
    if system == "windows":
        return "pc,accel=whpx"
    return "pc"


def start_session(profile_id):
    if profile_id not in PROFILES:
        raise ValueError("Unknown Windows version")
    profile = PROFILES[profile_id]
    base_disk = IMAGE_ROOT / profile_id / "disk.qcow2"
    if not base_disk.is_file():
        raise FileNotFoundError("This Windows version has no preinstalled host image yet")
    if not QEMU or not Path(QEMU).is_file() or not QEMU_IMG or not Path(QEMU_IMG).is_file():
        raise RuntimeError("QEMU and qemu-img must be installed on the VM host")

    uefi_pair = find_ovmf() if profile["uefi"] else None
    if profile["uefi"] and uefi_pair is None:
        raise RuntimeError("OVMF/EDK II UEFI firmware is required for this Windows version")

    session_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    work_dir = Path(tempfile.mkdtemp(prefix="windows-vm-"))
    overlay = work_dir / "session.qcow2"
    tpm_process = None
    qemu_process = None
    try:
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base_disk), str(overlay)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        display, vnc_port, websocket_port = reserve_ports()
        command = [
            QEMU, "-name", profile["name"], "-machine", host_acceleration(),
            "-m", profile["memory"], "-smp", profile["cores"], "-cpu", "qemu64",
            "-display", "none", "-vga", "std", "-device", "usb-tablet",
            "-netdev", "user,id=net0", "-device", "rtl8139,netdev=net0",
            "-drive", f"file={overlay},format=qcow2,if=ide",
            "-vnc", f"127.0.0.1:{display},websocket=127.0.0.1:{websocket_port}",
        ]

        if uefi_pair:
            code, vars_template = uefi_pair
            vars_copy = work_dir / "uefi-vars.fd"
            shutil.copyfile(vars_template, vars_copy)
            command += [
                "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                "-drive", f"if=pflash,format=raw,file={vars_copy}",
            ]

        if SWTPM and Path(SWTPM).is_file():
            tpm_state = work_dir / "tpm-state"
            tpm_state.mkdir()
            tpm_socket = work_dir / "swtpm.sock"
            tpm_log = work_dir / "swtpm.log"
            tpm_log_handle = tpm_log.open("ab")
            tpm_process = subprocess.Popen(
                [SWTPM, "socket", "--tpmstate", f"dir={tpm_state}", "--ctrl", f"type=unixio,path={tpm_socket}",
                 "--tpm2", "--log", f"file={tpm_log},level=20"],
                stdout=tpm_log_handle, stderr=subprocess.STDOUT,
            )
            deadline = time.time() + 5
            while not tpm_socket.exists() and tpm_process.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            tpm_log_handle.close()
            if not tpm_socket.exists():
                raise RuntimeError("swtpm could not create its session socket")
            command += [
                "-chardev", f"socket,id=chrtpm,path={tpm_socket}",
                "-tpmdev", "emulator,id=tpm0,chardev=chrtpm", "-device", "tpm-tis,tpmdev=tpm0",
            ]

        qemu_log = (work_dir / "qemu.log").open("ab")
        qemu_process = subprocess.Popen(command, stdout=qemu_log, stderr=subprocess.STDOUT)
        qemu_log.close()
        time.sleep(0.35)
        if qemu_process.poll() is not None:
            detail = (work_dir / "qemu.log").read_text(errors="replace")[-1200:]
            raise RuntimeError("QEMU exited during startup. " + detail)

        entry = {
            "id": session_id, "token": token, "profile": profile_id, "work_dir": work_dir,
            "qemu": qemu_process, "tpm": tpm_process, "websocket_port": websocket_port,
            "created": time.time(), "last_used": time.time(),
        }
        with sessions_lock:
            if len(sessions) >= MAX_SESSIONS:
                raise RuntimeError("The host has reached its active VM session limit")
            sessions[session_id] = entry
        return {"id": session_id, "name": profile["name"], "token": token}
    except Exception:
        stop_process(qemu_process)
        stop_process(tpm_process)
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def stop_process(process):
    if not process:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def stop_session(session_id):
    with sessions_lock:
        entry = sessions.pop(session_id, None)
    if not entry:
        return False
    stop_process(entry["qemu"])
    stop_process(entry["tpm"])
    shutil.rmtree(entry["work_dir"], ignore_errors=True)
    return True


def expire_idle_sessions():
    while True:
        time.sleep(60)
        cutoff = time.time() - IDLE_SECONDS
        with sessions_lock:
            expired = [sid for sid, entry in sessions.items() if entry["last_used"] < cutoff]
        for sid in expired:
            stop_session(sid)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print("[hosted-vm] " + fmt % args)

    def send_json(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/versions":
            return self.send_json(status_data())
        if self.path.startswith("/ws/"):
            return self.proxy_websocket(self.path[len("/ws/"):])
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/sessions":
            return self.send_json({"error": "Not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 2048:
                return self.send_json({"error": "Request too large"}, 413)
            payload = json.loads(self.rfile.read(length) or b"{}")
            profile_id = payload.get("version", "")
            with sessions_lock:
                if len(sessions) >= MAX_SESSIONS:
                    return self.send_json({"error": "The host has no free VM slots right now"}, 429)
            result = start_session(profile_id)
            self.send_json(result, 201)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc) or "Invalid request"}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 503)

    def do_DELETE(self):
        session_id = self.path.removeprefix("/api/sessions/")
        if not session_id or "/" in session_id:
            return self.send_json({"error": "Not found"}, 404)
        if stop_session(session_id):
            return self.send_json({"stopped": True})
        return self.send_json({"error": "Session not found"}, 404)

    def proxy_websocket(self, token):
        if self.headers.get("Upgrade", "").lower() != "websocket":
            return self.send_json({"error": "WebSocket upgrade required"}, 400)
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self.send_json({"error": "Cross-origin connections are not allowed"}, 403)
        with sessions_lock:
            entry = next((item for item in sessions.values() if secrets.compare_digest(item["token"], token)), None)
            if entry:
                entry["last_used"] = time.time()
        if not entry:
            return self.send_json({"error": "Session not found"}, 404)

        upstream = None
        try:
            upstream = socket.create_connection(("127.0.0.1", entry["websocket_port"]), timeout=5)
            headers = [
                "GET / HTTP/1.1",
                f"Host: 127.0.0.1:{entry['websocket_port']}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {self.headers.get('Sec-WebSocket-Key', '')}",
                f"Sec-WebSocket-Version: {self.headers.get('Sec-WebSocket-Version', '13')}",
            ]
            for name in ("Sec-WebSocket-Protocol", "Origin"):
                value = self.headers.get(name)
                if value:
                    headers.append(f"{name}: {value}")
            upstream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
            upstream.settimeout(5)
            response = bytearray()
            while b"\r\n\r\n" not in response and len(response) < 16384:
                chunk = upstream.recv(1)
                if not chunk:
                    raise ConnectionError("QEMU closed the WebSocket handshake")
                response.extend(chunk)
            self.connection.sendall(response)
            if not response.startswith(b"HTTP/1.1 101"):
                return
            self.connection.setblocking(False)
            upstream.setblocking(False)
            sockets = [self.connection, upstream]
            while True:
                readable, _, exceptional = select(sockets, [], sockets, 120)
                if exceptional or not readable:
                    break
                for source in readable:
                    target = upstream if source is self.connection else self.connection
                    data = source.recv(65536)
                    if not data:
                        return
                    target.sendall(data)
        except (OSError, ConnectionError):
            pass
        finally:
            if upstream:
                upstream.close()


class HostedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def main():
    threading.Thread(target=expire_idle_sessions, daemon=True).start()
    server = HostedHTTPServer((os.environ.get("WINDOWS_VM_BIND", "127.0.0.1"), PORT), Handler)
    print(f"Windows VM host ready at http://{server.server_address[0]}:{server.server_address[1]}")
    print(f"Preinstalled qcow2 images: {IMAGE_ROOT}")
    print("This development server binds to localhost and is not configured for public hosting.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with sessions_lock:
            active = list(sessions)
        for session_id in active:
            stop_session(session_id)


if __name__ == "__main__":
    main()
