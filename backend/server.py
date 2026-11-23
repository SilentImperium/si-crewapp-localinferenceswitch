#!/usr/bin/env python3
"""SI Local Inference Switch - Kiro Crew app backend.

A managed shell-command runner. The dashboard defines named *profiles*
(verbatim shell command + working dir + env + shell mode) and this backend
runs at most one profile at a time as a child in its own process group,
capturing everything it prints. The Kiro Crew gateway reverse-proxies
``/apps/{name}/api/*`` to this server; the UI talks to the gateway, and the
gateway signs every proxied request with an HMAC we verify here before
touching anything.

Endpoints (as seen by the gateway, which prepends /api/):
  GET    /health            open liveness probe (must stay unauthenticated)
  GET    /api/state         profiles + process + last exit + log cursor
  POST   /api/profiles      create profile
  PUT    /api/profiles/<id> update profile
  DELETE /api/profiles/<id> delete profile
  POST   /api/process/start {profile_id, stop_current?}
  POST   /api/process/stop  {grace_ms?}
  GET    /api/logs/tail     ?after=<seq>&limit=<n>
  GET    /api/logs/stream   SSE: seed of lines after ?after= (or Last-Event-ID)
                            then live "line" / "exit" events, 15s heartbeats
  GET    /api/metrics       live inference dashboard data (token rates, per-model
                            ledger, requests, latency, spec decode, memory)

Stdlib only - the gateway spawns this with a scrubbed env and no
requirements.txt, so nothing may need provisioning.

Env set by the gateway at spawn:
  PORT                   loopback port to bind (allocated from 9100-9200)
  KIROCREW_APP_NAME      the app name
  KIROCREW_HOME          the Kiro Crew config directory
  KIROCREW_PROXY_SECRET  shared secret for X-KiroCrew-Proxy verification
"""

import hashlib
import hmac
import json
import math
import os
import queue
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_NAME = os.environ.get("KIROCREW_APP_NAME", "si-crewapp-localinferenceswitch")
APP_VERSION = "0.1.0"
PROXY_SECRET = os.environ.get("KIROCREW_PROXY_SECRET", "")
MAX_SKEW_S = 60
LOG_RING_SIZE = 2000
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_STOP_GRACE_S = 8.0
SHELL_MODES = ("login", "interactive", "plain", "exec")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DATA_DIR = Path(os.environ.get("KIROCREW_HOME", str(Path.home() / ".kiro" / "crew"))) / "apps" / APP_NAME / "data"
LOG_DIR = DATA_DIR / "logs"
STATE_FILE = DATA_DIR / "state.json"
RUN_FILE = DATA_DIR / "run.json"
LOG_FILE = LOG_DIR / "process.log"
LOG_BACKUP = LOG_DIR / "process.log.1"

LOCK = threading.RLock()
RING = deque(maxlen=LOG_RING_SIZE)          # (seq, text)
WATCHERS = []                               # queue.Queue per SSE client
SEQ = 0
RUN = None                                  # managed process record or None
SERVER = None
STATE = {"profiles": [], "last_started_profile_id": None, "last_exit": None}


class ApiError(Exception):
    def __init__(self, status, msg, extra=None):
        super().__init__(msg)
        self.status = status
        self.msg = msg
        self.extra = extra or {}


# ---------------------------------------------------------------------------
# state persistence


def _load_state():
    global STATE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE = {"profiles": [], "last_started_profile_id": None, "last_exit": None}
    if STATE_FILE.exists():
        try:
            d = json.loads(STATE_FILE.read_text())
            if isinstance(d, dict):
                STATE["profiles"] = [p for p in d.get("profiles", []) if isinstance(p, dict)]
                STATE["last_started_profile_id"] = d.get("last_started_profile_id")
                STATE["last_exit"] = d.get("last_exit")
        except Exception:
            print("[si] state.json unreadable; starting fresh", flush=True)


def _save_state():
    """Caller must hold LOCK."""
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(STATE, indent=2))
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# log buffer


def _append_line(text):
    global SEQ
    with LOCK:
        SEQ += 1
        seq = SEQ
        RING.append((seq, text))
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_FILE_MAX_BYTES:
                LOG_FILE.replace(LOG_BACKUP)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass
        for q in list(WATCHERS):
            try:
                q.put_nowait(("line", {"seq": seq, "text": text}))
            except Exception:
                pass


def tail_logs(qs):
    try:
        after = int((qs.get("after") or ["0"])[0])
    except (ValueError, IndexError):
        after = 0
    try:
        limit = int((qs.get("limit") or ["300"])[0])
    except (ValueError, IndexError):
        limit = 300
    limit = max(1, min(limit, 1000))
    with LOCK:
        lines = [list(it) for it in RING if it[0] > after][-limit:]
        seq = SEQ
    return {"lines": lines, "next": seq}


# ---------------------------------------------------------------------------
# /proc helpers (linux)


IS_WINDOWS = os.name == "nt"


def _pid_alive(pid):
    """True when the pid is a live (non-zombie) process (cross-platform)."""
    if not pid:
        return False
    if IS_WINDOWS:
        # On Windows, signal 0 checks for existence (no zombie state to worry
        # about - the OS reaps automatically).
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True
    try:
        data = Path("/proc/%d/stat" % pid).read_text()
    except Exception:
        return False
    try:
        # field 3 (state) is the first token after the ")"-terminated comm
        return data.rsplit(")", 1)[1].split()[0] != "Z"
    except (IndexError, ValueError):
        return os.path.isdir("/proc/%d" % pid)


def _read_stat_field(pid, field):
    """Read field N of /proc/<pid>/stat (comm may contain spaces/parens)."""
    try:
        data = Path("/proc/%d/stat" % pid).read_text()
    except Exception:
        return None
    try:
        tail = data.rsplit(")", 1)[1].split()
        return int(tail[field - 3])
    except (IndexError, ValueError):
        return None


def _iter_pids():
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for entry in entries:
        if entry.isdigit():
            yield int(entry)


def _proc_cmdline(pid):
    try:
        raw = Path("/proc/%d/cmdline" % pid).read_bytes()
    except Exception:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _group_members(pgid):
    """Live (non-zombie) pids currently in process group pgid.

    Zombies keep their pgid until their parent reaps them, so they must
    not count as survivors of a stop.
    """
    if not pgid:
        return []
    out = []
    for pid in _iter_pids():
        try:
            tail = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
        except Exception:
            continue
        if len(tail) >= 3 and tail[0] != "Z":
            try:
                if int(tail[2]) == pgid:
                    out.append(pid)
            except ValueError:
                pass
    return out


def _port_listener_pids(port):
    """Pids holding a LISTEN socket on the given port (127.0.0.1 and ::).

    POSIX only (via /proc); on Windows this returns [] and callers fall
    back to _port_listening().
    """
    if not port:
        return []
    inodes = set()
    port_hex = "%04X" % int(port)
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            data = Path(f).read_text()
        except Exception:
            continue
        for line in data.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":  # 0A == TCP_LISTEN
                continue
            if parts[1].rsplit(":", 1)[-1].upper() == port_hex:
                inodes.add(parts[9])
    if os.environ.get("SI_DEBUG"):
        print("[si-debug] port %s inodes=%s" % (port, sorted(inodes)), flush=True)
    if not inodes:
        return []
    wanted = {"socket:[%s]" % i for i in inodes}
    found = set()
    for pid in _iter_pids():
        try:
            fds = os.listdir("/proc/%d/fd" % pid)
        except Exception:
            continue
        for fd in fds:
            try:
                link = os.readlink("/proc/%d/fd/%s" % (pid, fd))
            except OSError:
                continue
            if link in wanted:
                found.add(pid)
                break
    if os.environ.get("SI_DEBUG"):
        print("[si-debug] port %s listeners=%s" % (port, sorted(found)), flush=True)
    return sorted(found)


_PORT_FLAG_RE = re.compile(r"(?:^|\s)--port[=\s](\d{1,5})(?=\s|$)")


def _port_listening(port):
    """Cross-platform: is something accepting connections on 127.0.0.1:port?

    Works for services inside WSL too - WSL2 forwards loopback ports to the
    Windows side.
    """
    if not port:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        rc = s.connect_ex(("127.0.0.1", int(port)))
    except OSError:
        rc = -1
    finally:
        s.close()
    if os.environ.get("SI_DEBUG"):
        print("[si-debug] _port_listening(%s) -> %s" % (port, rc == 0), flush=True)
    return rc == 0


def _kill_tree(pid):
    """Terminate a Windows process and all its children.

    A plain TerminateProcess on a cmd.exe or wsl.exe launcher leaves the
    children it spawned running, so always kill the tree.
    """
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            timeout=15,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _signal_group(pgid, pid, sig):
    """Signal a process group (POSIX) or the whole process tree (Windows)."""
    if not pid:
        return
    if not IS_WINDOWS:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        # Windows has no process groups; SIGTERM/SIGKILL both map to
        # TerminateProcess, and tree-kill is the only way to reach the
        # children of a shell/launcher wrapper.
        _kill_tree(pid)


def _profile_by_id(profile_id):
    if not profile_id:
        return None
    with LOCK:
        return next((p for p in STATE["profiles"] if p.get("id") == profile_id), None)


def _profile_marker(prof):
    """A command-line token that identifies the profile's service - a model
    file (.ninfer) or a launcher script (.sh) - with shell quotes stripped."""
    for t in str((prof or {}).get("command") or "").split():
        t = t.strip("'\"")
        if t.endswith((".ninfer", ".sh")):
            return t
    return None


_WIN_PROCS_CACHE = {"at": 0.0, "procs": []}


def _windows_processes():
    """[(pid, command_line)] for every process (Windows only).

    The list is cached for 5 s: discovery is polled by the UI every couple
    of seconds, and a full process enumeration takes a couple of seconds.
    """
    now = time.time()
    if _WIN_PROCS_CACHE["procs"] and now - _WIN_PROCS_CACHE["at"] < 5.0:
        return _WIN_PROCS_CACHE["procs"]
    ps = (
        "Get-CimInstance Win32_Process | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    procs = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=20,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout or ""
    except Exception:
        return []
    for line in out.splitlines():
        pid_s, sep, cmd = line.partition("\t")
        if not sep:
            continue
        try:
            procs.append((int(pid_s.strip()), cmd))
        except ValueError:
            pass
    _WIN_PROCS_CACHE["at"] = now
    _WIN_PROCS_CACHE["procs"] = procs
    return procs


def _windows_service_pid(prof):
    """The Windows-side pid that owns a profile's service, or 0 when the
    service is reachable (its port is open) but no launcher process was
    found. For wsl.exe-wrapper profiles this is the wsl.exe launcher."""
    marker = _profile_marker(prof)
    procs = _windows_processes()
    if marker:
        for pid, cmdline in procs:
            if marker in cmdline:
                return pid
    cmd = str((prof or {}).get("command") or "")
    if "wsl.exe" in cmd:
        dm = re.search(r"-d\s+(\S+)", cmd)
        distro = dm.group(1) if dm else None
        for pid, cmdline in procs:
            if "wsl.exe" in cmdline and (distro is None or distro in cmdline):
                return pid
    return 0


def _wsl_cleanup(prof):
    """Kill the service inside the WSL distro for a wsl.exe-wrapper profile.

    The Windows-side wsl.exe launcher can die (or be killed) while the
    service it started keeps running in the distro, so a stop must reach in:
    find the distro and a command-line marker, then kill the marker's whole
    session (script + children) from inside the distro. Returns True when a
    cleanup was attempted.
    """
    cmd = str(prof.get("command") or "") if prof else ""
    if "wsl.exe" not in cmd:
        return False
    dm = re.search(r"-d\s+(\S+)", cmd)
    marker = _profile_marker(prof)
    if not marker:
        return False
    # Kill the session of every process whose command line matches the
    # marker (script + its children), then pkill the marker as a backstop.
    # $$ is excluded so the cleanup does not kill its own shell.
    inner = (
        "m='%s'; for p in $(pgrep -f \"$m\" 2>/dev/null | grep -vw \"$$\"); do "
        "f=/proc/$p/stat; [ -r $f ] || continue; "
        "sid=$(sed 's/^.*) //' $f | awk '{print $4}'); "
        "case $sid in ''|*[!0-9]*) continue;; esac; "
        "kill -- -\"$sid\" 2>/dev/null; done; "
        "pkill -f \"$m\" 2>/dev/null; exit 0" % marker
    )
    argv = ["wsl.exe"]
    if dm:
        argv += ["-d", dm.group(1)]
    argv += ["--", "bash", "-lc", inner]
    try:
        subprocess.run(
            argv, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass
    return True


def _profile_port(prof):
    """A profile's service port: its explicit field, else --port in the command."""
    if not prof:
        return None
    p = prof.get("port")
    if p:
        try:
            return int(p)
        except (TypeError, ValueError):
            return None
    m = _PORT_FLAG_RE.search(str(prof.get("command") or ""))
    return int(m.group(1)) if m else None


def _profile_port_by_id(profile_id):
    if not profile_id:
        return None
    with LOCK:
        prof = next((p for p in STATE["profiles"] if p.get("id") == profile_id), None)
    return _profile_port(prof)


def find_service():
    """Find a live service matching a known profile (started inside or outside
    the app). Returns (pid, profile) or (None, None).

    A port match wins over a command-line marker match, and the last started
    profile is checked first. On Windows a port match proves the service is
    reachable (WSL2 forwards loopback ports to the Windows side) but not
    which pid owns it, so the pid comes from the launcher's command line
    (0 when no launcher process is found - the stop then relies on the
    distro-side cleanup and the port check).
    """
    with LOCK:
        profiles = list(STATE["profiles"])
        last_id = STATE["last_started_profile_id"]
    ordered = [p for p in profiles if p.get("id") == last_id]
    ordered += [p for p in profiles if p.get("id") != last_id]
    for prof in ordered:
        port = _profile_port(prof)
        if not port:
            continue
        if IS_WINDOWS:
            if _port_listening(port):
                return _windows_service_pid(prof), prof
        else:
            pids = _port_listener_pids(port)
            if pids:
                return pids[0], prof
    for prof in ordered:
        if IS_WINDOWS:
            marker = _profile_marker(prof)
            if not marker:
                continue
            for pid, cmdline in _windows_processes():
                if marker in cmdline:
                    return pid, prof
        else:
            marker = next(
                (
                    t
                    for t in str(prof.get("command") or "").split()
                    if t.endswith(".ninfer")
                ),
                None,
            )
            if not marker:
                continue
            for pid in _iter_pids():
                if marker in _proc_cmdline(pid):
                    return pid, prof
    return None, None


# ---------------------------------------------------------------------------
# process lifecycle


def _proc_alive(run):
    if run.get("attached") and run.get("proc") is not None:
        try:
            return run["proc"].poll() is None
        except Exception:
            return False
    return _pid_alive(run["pid"])


def _describe_exit(info):
    if info.get("signal"):
        return "signal %d" % info["signal"]
    if info.get("code") is not None:
        return "code %d" % info["code"]
    return "unknown exit"


def _record_exit(run, code):
    """Caller must hold LOCK."""
    if code is None:
        sig, c = None, None
    elif code < 0:
        sig, c = -code, None
    else:
        sig, c = None, code
    info = {
        "profile_id": run.get("profile_id"),
        "profile_name": run.get("profile_name"),
        "code": c,
        "signal": sig,
        "at": time.time(),
    }
    # A detached process (re-adopted after a backend crash) is not our child,
    # so we can never read its wait status; say so instead of guessing.
    if c is None and sig is None and not run.get("attached"):
        info["detached"] = True
    STATE["last_exit"] = info
    _save_state()
    for q in list(WATCHERS):
        try:
            q.put_nowait(("exit", info))
        except Exception:
            pass
    return info


def _stop_locked(grace_s):
    """Stop the managed process. Caller must hold LOCK.

    Returns (exit_info_or_None, still_running). When the service is still
    alive after SIGKILL + verification, the RUN record is kept (re-pointed at
    a surviving pid when one is known) so a later stop can retry, and
    still_running is True.

    Cross-platform: on Windows the tracked pid is usually a wsl.exe launcher
    (or 0 when only the port is known), so the real work is the distro-side
    cleanup plus the port-free verification.
    """
    global RUN
    run = RUN
    if run is None:
        return None, False
    pid, pgid = run["pid"], run["pgid"]
    port = _profile_port_by_id(run.get("profile_id"))
    prof = _profile_by_id(run.get("profile_id")) or {"command": run.get("command")}
    # For a wsl.exe-wrapper profile the service lives inside the distro:
    # kill it there first (the Windows-side launcher can die while the
    # distro-side service keeps running, and vice versa).
    if _wsl_cleanup(prof):
        time.sleep(0.5)
    _signal_group(pgid, pid, signal.SIGTERM)
    deadline = time.time() + grace_s
    while time.time() < deadline and _proc_alive(run):
        time.sleep(0.1)
    killed = False
    if _proc_alive(run):
        killed = True
        _signal_group(pgid, pid, signal.SIGKILL)
        deadline = time.time() + 3.0
        while time.time() < deadline and _proc_alive(run):
            time.sleep(0.1)
    # Straggler sweep: group members that survived (e.g. a worker that left
    # the group via setsid) are killed individually. No process groups on
    # Windows, so this is a no-op there.
    stragglers = _group_members(pgid)
    if stragglers:
        for sp in stragglers:
            try:
                os.kill(sp, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + 3.0
        while time.time() < deadline and _group_members(pgid):
            time.sleep(0.1)
    code = None
    proc = run.get("proc")
    if run.get("attached") and proc is not None:
        try:
            proc.wait(timeout=3.0)
        except Exception:
            pass
        code = proc.returncode
    # Verify the service is actually gone: group dead AND port free.
    survivors = _group_members(pgid)
    port_busy = bool(port) and _port_listening(port)
    if port and not IS_WINDOWS:
        listeners = _port_listener_pids(port)
        survivors = survivors + [p for p in listeners if p not in survivors]
    if survivors or port_busy:
        if survivors:
            for sp in sorted(set(survivors)):
                try:
                    os.kill(sp, signal.SIGKILL)
                except (ProcessLookupError, PermissionError) as e:
                    if os.environ.get("SI_DEBUG"):
                        print("[si-debug] kill %s failed: %s" % (sp, e), flush=True)
            time.sleep(0.5)
            survivors = _group_members(pgid)
            if port and not IS_WINDOWS:
                listeners = _port_listener_pids(port)
                survivors = survivors + [p for p in listeners if p not in survivors]
        # WSL2 port-proxy entries can lag a moment behind the distro-side
        # listener dying - give the port a short settle window. Re-evaluate
        # after the kill above (the pre-kill value is stale by now).
        port_busy = bool(port) and _port_listening(port)
        if port_busy:
            settle = time.time() + 3.0
            while time.time() < settle and _port_listening(port):
                time.sleep(0.5)
            port_busy = _port_listening(port)
        if survivors or port_busy:
            # Still alive: keep tracking it (detached) rather than claiming
            # success and forgetting a live process.
            run["attached"] = False
            run["proc"] = None
            if survivors:
                run["pid"] = survivors[0]
                run["pgid"] = _read_stat_field(survivors[0], 5) or survivors[0]
                run["start_ticks"] = _read_stat_field(survivors[0], 22)
            _write_run_file(run)
            _append_line(
                "[si] could not stop '%s': service still running%s; keeping it tracked"
                % (
                    run.get("profile_name") or "process",
                    " (pids %s)" % ", ".join(map(str, sorted(set(survivors))))
                    if survivors
                    else " (port %d busy)" % port,
                )
            )
            return None, True
    info = _record_exit(run, code)
    RUN = None
    _delete_run_file()
    _save_state()
    _append_line(
        "[si] stopped '%s' (pid %d)%s"
        % (run.get("profile_name") or "process", pid, " - SIGKILL after grace" if killed else "")
    )
    return info, False


def stop_process(grace_s=DEFAULT_STOP_GRACE_S):
    global RUN
    with LOCK:
        if RUN is None:
            # Nothing tracked, but a matching service may be running outside
            # the app - adopt it so Stop can reach it.
            pid, prof = find_service()
            if pid is None:
                return {"stopped": False, "running": False, "service_found": False}
            RUN = {
                "proc": None,
                "pid": pid,
                "pgid": _read_stat_field(pid, 5) or pid,
                "attached": False,
                "profile_id": prof.get("id"),
                "profile_name": prof.get("name"),
                "command": prof.get("command"),
                "started_at": None,
                "start_ticks": _read_stat_field(pid, 22),
                "readopted": True,
            }
            _write_run_file(RUN)
            _append_line(
                "[si] adopting externally-started service (pid %d, profile '%s') to stop it"
                % (pid, prof.get("name"))
            )
        info, still = _stop_locked(grace_s)
        if still:
            return {
                "stopped": False,
                "running": True,
                "service_found": True,
                "pid": RUN.get("pid"),
                "profile_name": RUN.get("profile_name"),
            }
        return {"stopped": True, "running": False, "exit": info}


def start_process(profile_id, stop_current):
    """Returns (run_record, ApiError-or-None)."""
    global RUN
    with LOCK:
        if RUN is not None:
            if not stop_current:
                return None, ApiError(
                    409,
                    "a process is already running (%s)"
                    % (RUN.get("profile_name") or RUN.get("command") or "pid %d" % RUN["pid"]),
                    {"running_profile_id": RUN.get("profile_id"), "pid": RUN["pid"]},
                )
            info, still = _stop_locked(DEFAULT_STOP_GRACE_S)
            if still:
                return None, ApiError(
                    409,
                    "could not stop the running service (pid %d); it is still alive - "
                    "stop it first, then start again" % (RUN.get("pid") if RUN else 0),
                    {"running_pid": RUN.get("pid") if RUN else None},
                )
        prof = next((p for p in STATE["profiles"] if p.get("id") == profile_id), None)
        if prof is None:
            return None, ApiError(404, "no such profile: %s" % profile_id)
        cwd = os.path.expanduser(prof.get("cwd") or "~")
        if not os.path.isdir(cwd):
            return None, ApiError(400, "working directory does not exist: %s" % cwd)
        cmd = prof["command"]
        shell = prof.get("shell") or "login"
        use_shell = False
        if shell == "exec":
            try:
                argv = shlex.split(cmd)
            except ValueError as e:
                return None, ApiError(400, "cannot parse command for exec mode: %s" % e)
            if not argv:
                return None, ApiError(400, "command is empty")
        elif IS_WINDOWS:
            # No bash on Windows: run the verbatim command through cmd.exe.
            # (The profiles here are wsl.exe wrappers; cmd.exe passes the
            # quoted arguments through to wsl.exe intact.)
            argv = cmd
            use_shell = True
        else:
            if shell == "login":
                argv = ["bash", "-lc", cmd]
            elif shell == "interactive":
                argv = ["bash", "-ilc", cmd]
            else:
                argv = ["sh", "-c", cmd]
        env = dict(os.environ)
        env.update(prof.get("env") or {})
        popen_kw = dict(
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        if IS_WINDOWS:
            popen_kw["shell"] = use_shell
            popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv, **popen_kw)
        except Exception as e:
            return None, ApiError(400, "failed to start process: %s" % e)
        run = {
            "proc": proc,
            "pid": proc.pid,
            "pgid": proc.pid,
            "attached": True,
            "profile_id": prof["id"],
            "profile_name": prof.get("name"),
            "command": cmd,
            "started_at": time.time(),
            "start_ticks": _read_stat_field(proc.pid, 22),
        }
        RUN = run
        STATE["last_started_profile_id"] = prof["id"]
        _save_state()
        _write_run_file(run)
    threading.Thread(target=_reader, args=(run,), daemon=True).start()
    threading.Thread(target=_watcher, args=(run,), daemon=True).start()
    _append_line(
        "[si] started '%s' (pid %d, cwd %s, shell %s): %s"
        % (run["profile_name"], run["pid"], cwd, shell, cmd)
    )
    return run, None


def _reader(run):
    proc = run.get("proc")
    if proc is None or proc.stdout is None:
        return
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\n")
            if text:
                _append_line(text)
    except Exception:
        pass
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass


def _watcher(run):
    global RUN
    proc = run.get("proc")
    if proc is None:
        return
    try:
        proc.wait()
    except Exception:
        pass
    time.sleep(0.2)  # give the reader a moment to drain the pipe
    with LOCK:
        if RUN is not run:
            return  # already finalized (e.g. by stop)
        if run.get("proc") is None:
            return  # detached by a failed stop (kept tracked) - not ours to finalize
        info = _record_exit(run, proc.returncode)
        RUN = None
        _delete_run_file()
    _append_line("[si] '%s' exited: %s" % (run.get("profile_name") or "process", _describe_exit(info)))


def _write_run_file(run):
    try:
        RUN_FILE.write_text(
            json.dumps(
                {
                    "pid": run["pid"],
                    "pgid": run["pgid"],
                    "start_ticks": run.get("start_ticks"),
                    "profile_id": run.get("profile_id"),
                    "profile_name": run.get("profile_name"),
                    "command": run.get("command"),
                    "started_at": run.get("started_at"),
                    "created_by": APP_NAME,
                }
            )
        )
    except Exception:
        pass


def _delete_run_file():
    try:
        RUN_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def reconcile():
    """Re-adopt a managed process still alive after a backend restart, or a
    service matching a profile that was started outside the app."""
    if _reconcile_run_file():
        return
    _reconcile_external()


def _reconcile_run_file():
    """Re-adopt from run.json. Returns True when a live process was adopted."""
    global RUN
    if not RUN_FILE.exists():
        return False
    try:
        data = json.loads(RUN_FILE.read_text())
    except Exception:
        _delete_run_file()
        return False
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        _delete_run_file()
        return False
    if not pid or not _pid_alive(pid):
        _delete_run_file()
        return False
    ticks = _read_stat_field(pid, 22)
    if data.get("start_ticks") not in (None, ticks):
        print("[si] run.json pid %d belongs to another process; discarding" % pid, flush=True)
        _delete_run_file()
        return False
    with LOCK:
        RUN = {
            "proc": None,
            "pid": pid,
            "pgid": _read_stat_field(pid, 5) or pid,
            "attached": False,
            "profile_id": data.get("profile_id"),
            "profile_name": data.get("profile_name"),
            "command": data.get("command"),
            "started_at": data.get("started_at") or time.time(),
            "start_ticks": ticks,
            "readopted": True,
        }
    _append_line(
        "[si] backend restarted; managed process still running (pid %d) - "
        "output not attached, stop still available" % pid
    )
    return True


def _reconcile_external():
    """Adopt a service matching a profile that is running outside the app,
    so the UI shows it and Stop can reach it."""
    global RUN
    pid, prof = find_service()
    if pid is None:
        return
    with LOCK:
        if RUN is not None:
            return
        RUN = {
            "proc": None,
            "pid": pid,
            "pgid": _read_stat_field(pid, 5) or pid,
            "attached": False,
            "profile_id": prof.get("id"),
            "profile_name": prof.get("name"),
            "command": prof.get("command"),
            "started_at": None,
            "start_ticks": _read_stat_field(pid, 22),
            "readopted": True,
        }
    _append_line(
        "[si] service from outside the app is running (pid %d, profile '%s') - "
        "adopted; output not attached, stop available" % (pid, prof.get("name"))
    )


# ---------------------------------------------------------------------------
# state snapshot


def proc_status_locked():
    r = RUN
    if not r:
        return {"running": False}
    return {
        "running": True,
        "attached": r.get("attached", False),
        "readopted": bool(r.get("readopted")),
        "pid": r["pid"],
        "pgid": r["pgid"],
        "profile_id": r.get("profile_id"),
        "profile_name": r.get("profile_name"),
        "command": r.get("command"),
        "started_at": r.get("started_at"),
        "uptime_s": round(time.time() - (r.get("started_at") or time.time()), 1),
    }


_DETECTED_CACHE = {"at": 0.0, "value": None}


def _service_detected():
    """A service matching a profile that runs outside app management.

    Positive results are cached 5 s (the UI polls state every couple of
    seconds); negative results only 1.5 s, so a service that just started
    shows up on the next poll.
    """
    now = time.time()
    ttl = 5.0 if _DETECTED_CACHE["value"] else 1.5
    if _DETECTED_CACHE["at"] and now - _DETECTED_CACHE["at"] < ttl:
        return _DETECTED_CACHE["value"]
    val = None
    with LOCK:
        running = RUN is not None
    if not running:
        pid, prof = find_service()
        if pid is not None:
            val = {
                "pid": pid,
                "profile_id": prof.get("id"),
                "profile_name": prof.get("name"),
            }
    _DETECTED_CACHE["at"] = now
    _DETECTED_CACHE["value"] = val
    return val


def state_snapshot():
    with LOCK:
        s = {
            "profiles": list(STATE["profiles"]),
            "last_started_profile_id": STATE["last_started_profile_id"],
            "last_exit": STATE["last_exit"],
            "process": proc_status_locked(),
            "log_seq": SEQ,
        }
    detected = _service_detected()
    if detected:
        s["service_detected"] = detected
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "now": time.time(),
        **s,
        "log_file": str(LOG_FILE),
    }


# ---------------------------------------------------------------------------
# profiles


def _validate_profile(d, existing_id=None):
    errors = []
    name = str(d.get("name") or "").strip()
    if not name or len(name) > 64:
        errors.append("name must be 1-64 characters")
    command = str(d.get("command") or "").strip()
    if not command or len(command) > 8192:
        errors.append("command must be 1-8192 characters")
    shell = str(d.get("shell") or "login")
    if shell not in SHELL_MODES:
        errors.append("shell must be one of: %s" % ", ".join(SHELL_MODES))
    cwd = str(d.get("cwd") or "~").strip() or "~"
    if len(cwd) > 512:
        errors.append("cwd too long (max 512)")
    port = d.get("port")
    if port in (None, ""):
        port = None
    else:
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = None
            errors.append("port must be an integer")
        else:
            if not 1 <= port <= 65535:
                port = None
                errors.append("port must be 1-65535")
    env = d.get("env") or {}
    if not isinstance(env, dict):
        errors.append("env must be an object of KEY=string")
    else:
        if len(env) > 32:
            errors.append("too many env vars (max 32)")
        for k, v in env.items():
            if not _ENV_KEY_RE.match(str(k)):
                errors.append("bad env key: %s" % k)
            if not isinstance(v, str):
                errors.append("env value must be a string: %s" % k)
    if errors:
        raise ApiError(400, "; ".join(errors))
    with LOCK:
        for p in STATE["profiles"]:
            if p.get("id") != existing_id and str(p.get("name", "")).lower() == name.lower():
                raise ApiError(409, "a profile named '%s' already exists" % name)
    return {
        "name": name,
        "command": command,
        "cwd": os.path.expanduser(cwd),
        "env": {str(k): str(v) for k, v in env.items()},
        "shell": shell,
        "port": port,
    }


def create_profile(d):
    clean = _validate_profile(d)
    import uuid

    slug = re.sub(r"[^a-z0-9]+", "-", clean["name"].lower()).strip("-") or "profile"
    prof = {
        "id": "%s-%s" % (slug[:32], uuid.uuid4().hex[:6]),
        **clean,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with LOCK:
        STATE["profiles"].append(prof)
        _save_state()
    return prof


def update_profile(pid, d):
    clean = _validate_profile(d, existing_id=pid)
    with LOCK:
        prof = next((p for p in STATE["profiles"] if p.get("id") == pid), None)
        if prof is None:
            raise ApiError(404, "no such profile: %s" % pid)
        prof.update(clean)
        prof["updated_at"] = time.time()
        _save_state()
    return prof


def delete_profile(pid):
    with LOCK:
        if RUN is not None and RUN.get("profile_id") == pid:
            raise ApiError(409, "cannot delete the running profile; stop the process first")
        before = len(STATE["profiles"])
        STATE["profiles"] = [p for p in STATE["profiles"] if p.get("id") != pid]
        if len(STATE["profiles"]) == before:
            raise ApiError(404, "no such profile: %s" % pid)
        if STATE["last_started_profile_id"] == pid:
            STATE["last_started_profile_id"] = None
        _save_state()
    return {"ok": True}


# ---------------------------------------------------------------------------
# metrics collector (ninfer /metrics)
#
# A daemon thread polls the running service's Prometheus endpoint
# (http://127.0.0.1:<profile port>/metrics) every few seconds, derives
# per-second token rates from counter deltas, keeps a ~10-minute sparkline
# history, and accumulates a per-model token ledger persisted to
# data/usage.json so "tokens per model" survives backend restarts and
# model switches. GET /api/metrics serves the latest snapshot.

METRICS_POLL_S = 3.0
METRICS_TIMEOUT_S = 2.0
METRICS_HISTORY_MAX = 200  # ~10 min at the 3 s cadence
METRICS_EMA_ALPHA = 0.15
USAGE_FILE = DATA_DIR / "usage.json"

_PROM_SAMPLE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(-?[0-9.eE+]+)\s*$"
)
_PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)=("(?:[^"\\]|\\.)*"|[^,]*)')


def _parse_prom(text):
    """Parse Prometheus text exposition into {name: {labels: value}}.

    Labels are stored as frozensets of (k, v) so they hash; unlabeled
    series use the empty set.
    """
    out = {}
    for line in text.splitlines():
        m = _PROM_SAMPLE_RE.match(line.strip())
        if not m:
            continue
        name, lab_s, val_s = m.groups()
        try:
            val = float(val_s)
        except ValueError:
            continue
        labels = frozenset()
        if lab_s:
            kv = {}
            for lm in _PROM_LABEL_RE.finditer(lab_s[1:-1]):
                k, v = lm.group(1), lm.group(2)
                if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                    v = v[1:-1]
                kv[k] = v
            labels = frozenset(kv.items())
        out.setdefault(name, {})[labels] = val
    return out


def _prom_get(samples, name, **labels):
    """Value of the series carrying exactly the given labels (or None)."""
    d = samples.get(name)
    if not d:
        return None
    want = frozenset(labels.items())
    if want in d:
        return d[want]
    if not want and len(d) == 1:
        return next(iter(d.values()))
    return None


def _prom_sum(samples, name, **labels):
    """Sum over every label set that contains the given labels."""
    d = samples.get(name)
    if not d:
        return 0.0
    want = frozenset(labels.items())
    return sum(v for lab, v in d.items() if want <= lab)


def _prom_first_labels(samples, name):
    """Label dict of the first series of the given name ({} when none)."""
    d = samples.get(name)
    if not d:
        return {}
    for lab in d:
        return dict(lab)
    return {}


def _active_metrics_port():
    """The port of the service the dashboard should watch, or None.

    The managed process wins; otherwise a detected external service
    matching a known profile.
    """
    with LOCK:
        if RUN is not None:
            port = _profile_port_by_id(RUN.get("profile_id"))
            if port:
                return port
    detected = _service_detected()
    if detected:
        return _profile_port_by_id(detected.get("profile_id"))
    return None


METRICS_LOCK = threading.Lock()
METRICS = {
    "ok": False,
    "reason": "no service running",
    "port": None,
    "updated_at": None,
    "stale": False,
    "model": None,
    "engine": None,
    "throughput": None,
    "requests": None,
    "spec_decode": None,
    "memory": None,
    "tokens": None,
}
_RATE = {
    "last_ts": None,
    "last_gen": None,
    "last_prompt": None,
    "ema_decode": None,
    "history": deque(maxlen=METRICS_HISTORY_MAX),  # [ts, decode_tps, total_tps]
}
# Session ledger for the current model run: counters at the moment this
# model (re)started, so session totals = current counters - these.
_RUN_BASELINE = {"model": None, "gen_at_start": 0.0, "prompt_at_start": 0.0, "since": None}
# Previous tick's counters + active model (for rate and ledger deltas).
_PREV = {"gen": None, "prompt": None, "model": None, "ts": None}
# Per-model lifetime ledger (persisted to usage.json).
_USAGE = {"models": {}}


def _load_usage():
    if USAGE_FILE.exists():
        try:
            d = json.loads(USAGE_FILE.read_text())
            models = d.get("models") if isinstance(d, dict) else None
            if isinstance(models, dict):
                _USAGE["models"] = {
                    k: m for k, m in models.items() if isinstance(m, dict)
                }
        except Exception:
            print("[si] usage.json unreadable; starting fresh", flush=True)


def _save_usage():
    """Caller must hold METRICS_LOCK."""
    tmp = USAGE_FILE.with_name(USAGE_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(_USAGE, indent=2))
        tmp.replace(USAGE_FILE)
    except Exception:
        pass


def _metrics_set_bad(reason, port):
    with METRICS_LOCK:
        METRICS.update(ok=False, reason=reason, port=port, stale=True)


def _metrics_tick():
    global METRICS
    port = _active_metrics_port()
    if not port:
        with LOCK:
            running = RUN is not None
        reason = (
            "no known port for the running profile"
            if running
            else "no service running"
        )
        _metrics_set_bad(reason, port)
        return
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/metrics" % port, timeout=METRICS_TIMEOUT_S
        ) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception as e:
        _metrics_set_bad("service unreachable (%s)" % e.__class__.__name__, port)
        return

    samples = _parse_prom(text)
    now = time.time()

    gen = _prom_get(samples, "ninfer_generation_tokens_total") or 0.0
    prompt = _prom_get(samples, "ninfer_prompt_tokens_total") or 0.0
    model_labels = _prom_first_labels(samples, "ninfer_model_info")
    model_name = model_labels.get("model_name")

    # Previous-tick state (for rates and the ledger), captured before update.
    prev_gen = _RATE["last_gen"]
    prev_prompt = _RATE["last_prompt"]
    prev_ts = _RATE["last_ts"]
    prev_model = _PREV["model"]

    # -- rates (counter-delta, reset-safe) ---------------------------------
    decode_tps = total_tps = None
    with METRICS_LOCK:
        if (
            prev_ts
            and prev_gen is not None
            and gen >= prev_gen
            and prompt >= prev_prompt
        ):
            dt = now - prev_ts
            if dt > 0:
                decode_tps = (gen - prev_gen) / dt
                total_tps = (gen + prompt - prev_gen - prev_prompt) / dt
        _RATE["last_ts"] = now
        _RATE["last_gen"] = gen
        _RATE["last_prompt"] = prompt
        if decode_tps is not None:
            ema = _RATE["ema_decode"]
            _RATE["ema_decode"] = (
                decode_tps
                if ema is None
                else ema * (1.0 - METRICS_EMA_ALPHA) + decode_tps * METRICS_EMA_ALPHA
            )
            _RATE["history"].append([now, round(decode_tps, 2), round(total_tps, 2)])

        # -- per-model ledger ---------------------------------------------
        # Attribute this tick's counter delta to the model active at the
        # previous tick; a model switch (or counter reset) re-baselines.
        if model_name:
            entry = _USAGE["models"].get(model_name)
            if entry is None:
                entry = {
                    "model": model_name,
                    "spec_backend": model_labels.get("speculative_backend"),
                    "prompt": 0.0,
                    "generation": 0.0,
                    "first_at": now,
                    "last_at": now,
                }
                _USAGE["models"][model_name] = entry
            if prev_model == model_name and prev_gen is not None:
                d_gen = max(0.0, gen - prev_gen)
                d_prompt = max(0.0, prompt - prev_prompt)
                if d_gen or d_prompt:
                    entry["generation"] += d_gen
                    entry["prompt"] += d_prompt
                    entry["last_at"] = now
                    _save_usage()
            if _RUN_BASELINE["model"] != model_name:
                # New model run: session counters restart from here.
                _RUN_BASELINE.update(
                    model=model_name,
                    gen_at_start=gen,
                    prompt_at_start=prompt,
                    since=now,
                )
    _PREV.update(gen=gen, prompt=prompt, model=model_name, ts=now)

    # -- snapshot pieces -----------------------------------------------------
    start_time = _prom_get(samples, "ninfer_server_start_time_seconds")
    req_ttft_sum = _prom_get(samples, "ninfer_time_to_first_token_seconds_sum") or 0.0
    req_ttft_n = _prom_get(samples, "ninfer_time_to_first_token_seconds_count") or 0.0
    req_dur_sum = _prom_get(samples, "ninfer_request_duration_seconds_sum") or 0.0
    req_dur_n = _prom_get(samples, "ninfer_request_duration_seconds_count") or 0.0
    req_queue_sum = _prom_get(samples, "ninfer_request_queue_seconds_sum") or 0.0
    req_queue_n = _prom_get(samples, "ninfer_request_queue_seconds_count") or 0.0

    spec = None
    spec_draft = _prom_get(samples, "ninfer_spec_decode_draft_tokens_total")
    spec_accepted = _prom_get(samples, "ninfer_spec_decode_accepted_tokens_total")
    if spec_draft is not None or spec_accepted is not None:
        positions = []
        pos_d = {}
        for lab, v in (samples.get("ninfer_spec_decode_position_tokens_total") or {}).items():
            d = dict(lab)
            try:
                pos = int(d.get("position", 0))
            except (TypeError, ValueError):
                pos = 0
            slot = pos_d.setdefault(pos, {"position": pos, "accepted": 0.0, "attempted": 0.0})
            kind = d.get("kind")
            if kind == "accepted":
                slot["accepted"] = v
            elif kind == "attempted":
                slot["attempted"] = v
        for pos in sorted(pos_d):
            slot = pos_d[pos]
            if slot["attempted"] > 0:
                slot["rate"] = round(slot["accepted"] / slot["attempted"], 4)
            positions.append(slot)
        spec = {
            "draft_window": _prom_get(samples, "ninfer_spec_decode_draft_window"),
            "draft_tokens": spec_draft,
            "accepted_tokens": spec_accepted,
            "acceptance": (
                round(spec_accepted / spec_draft, 4)
                if spec_draft and spec_accepted is not None
                else None
            ),
            "positions": positions,
        }

    dev_alloc = _prom_sum(
        samples, "ninfer_memory_allocation_bytes", location="device"
    )
    dev_budget = _prom_sum(samples, "ninfer_memory_budget_bytes")

    with METRICS_LOCK:
        history = list(_RATE["history"])
        ema = _RATE["ema_decode"]
        session_prompt = max(0.0, prompt - _RUN_BASELINE["prompt_at_start"])
        session_gen = max(0.0, gen - _RUN_BASELINE["gen_at_start"])
        by_model = sorted(
            (
                dict(m, total=m.get("prompt", 0.0) + m.get("generation", 0.0))
                for m in _USAGE["models"].values()
            ),
            key=lambda m: -m["total"],
        )
        METRICS.update(
            ok=True,
            reason=None,
            port=port,
            updated_at=now,
            stale=False,
            model={
                "name": model_name,
                "spec_backend": model_labels.get("speculative_backend"),
                "max_context": _prom_get(samples, "ninfer_max_context_tokens"),
            },
            engine={
                "server_start_time": start_time,
                "uptime_s": round(now - start_time, 1) if start_time else None,
            },
            throughput={
                "decode_tps": round(decode_tps, 2) if decode_tps is not None else None,
                "total_tps": round(total_tps, 2) if total_tps is not None else None,
                "decode_tps_avg": round(ema, 2) if ema is not None else None,
                "history": history,
            },
            requests={
                "running": _prom_get(samples, "ninfer_requests_running"),
                "waiting": _prom_get(samples, "ninfer_requests_waiting"),
                "max_concurrency": _prom_get(samples, "ninfer_max_concurrency"),
                "submitted": _prom_get(samples, "ninfer_requests_submitted_total"),
                "completed": _prom_get(samples, "ninfer_requests_total", outcome="completed"),
                "cancelled": _prom_get(samples, "ninfer_requests_total", outcome="cancelled"),
                "failed": _prom_get(samples, "ninfer_requests_total", outcome="failed"),
                "rejected": _prom_get(samples, "ninfer_requests_total", outcome="rejected"),
                "ttft_s": round(req_ttft_sum / req_ttft_n, 3) if req_ttft_n else None,
                "duration_s": round(req_dur_sum / req_dur_n, 3) if req_dur_n else None,
                "queue_s": round(req_queue_sum / req_queue_n, 3) if req_queue_n else None,
            },
            spec_decode=spec,
            memory={
                "device": {
                    "allocated": dev_alloc,
                    "budget": dev_budget,
                    "utilization": round(dev_alloc / dev_budget, 4)
                    if dev_budget
                    else None,
                    "weights": _prom_sum(
                        samples,
                        "ninfer_memory_allocation_bytes",
                        resource="weights",
                        location="device",
                    ),
                    "kv": _prom_sum(
                        samples,
                        "ninfer_memory_allocation_bytes",
                        resource="context",
                        location="device",
                    ),
                },
                "host": {
                    "kv_pinned": _prom_sum(
                        samples,
                        "ninfer_memory_allocation_bytes",
                        resource="context",
                        location="host_pinned",
                    ),
                    "available": _prom_get(samples, "ninfer_system_memory_available_bytes"),
                },
            },
            tokens={
                "prompt": prompt,
                "generation": gen,
                "total": prompt + gen,
                "cached_prompt": _prom_get(samples, "ninfer_prompt_tokens_cached_total"),
                "session": {
                    "model": _RUN_BASELINE["model"],
                    "prompt": session_prompt,
                    "generation": session_gen,
                    "total": session_prompt + session_gen,
                    "since": _RUN_BASELINE["since"],
                },
                "by_model": by_model,
            },
        )


def _metrics_loop():
    _load_usage()
    print("[si] metrics collector started (poll %.1fs)" % METRICS_POLL_S, flush=True)
    while True:
        try:
            _metrics_tick()
        except Exception:
            traceback.print_exc()
        time.sleep(METRICS_POLL_S)


def metrics_snapshot():
    """Latest collector snapshot (deep copy, safe to serialize)."""
    with METRICS_LOCK:
        return json.loads(json.dumps(METRICS))


# ---------------------------------------------------------------------------
# HTTP


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SILocalInferencing/" + APP_VERSION
    timeout = 60

    # -- plumbing ------------------------------------------------------------

    def log_message(self, fmt, *args):
        # keep backend.log quiet: only record errors
        if "%s" in fmt:
            pass
        print("[si-http] %s %s" % (self.address_string(), fmt % args), file=sys.stderr, flush=True)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _json(self, status, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _authorized(self, body):
        if not PROXY_SECRET:
            return False
        header = self.headers.get("X-KiroCrew-Proxy", "")
        parts = header.split(":", 1)
        if len(parts) != 2:
            return False
        ts, sig = parts
        try:
            ts_i = int(ts)
        except ValueError:
            return False
        if abs(time.time() - ts_i) > MAX_SKEW_S:
            return False
        body_hash = hashlib.sha256(body).hexdigest()
        msg = "%s:%s:%s:%s" % (ts, self.command, self.path, body_hash)
        expected = hmac.new(PROXY_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    # -- routing -------------------------------------------------------------

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        body = self._body()
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/health":
                return self._json(
                    200,
                    {"ok": True, "app": APP_NAME, "version": APP_VERSION, "pid": os.getpid()},
                )
            if not path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            if not self._authorized(body):
                return self._json(401, {"error": "proxy authentication failed"})
            api = path[len("/api/"):]
            qs = parse_qs(parsed.query)

            if method == "GET" and api == "state":
                return self._json(200, state_snapshot())
            if method == "POST" and api == "profiles":
                return self._json(201, create_profile(_json_body(body)))
            m = re.fullmatch(r"profiles/([^/]+)", api)
            if m:
                if method == "PUT":
                    return self._json(200, update_profile(m.group(1), _json_body(body)))
                if method == "DELETE":
                    return self._json(200, delete_profile(m.group(1)))
            if method == "POST" and api == "process/start":
                d = _json_body(body)
                run, err = start_process(str(d.get("profile_id") or ""), bool(d.get("stop_current")))
                if err:
                    raise err
                return self._json(
                    202,
                    {"ok": True, "pid": run["pid"], "pgid": run["pgid"], "profile_id": run["profile_id"]},
                )
            if method == "POST" and api == "process/stop":
                d = _json_body(body)
                try:
                    grace = max(1.0, min(30.0, float(d.get("grace_ms", 8000)) / 1000.0))
                except (TypeError, ValueError):
                    grace = DEFAULT_STOP_GRACE_S
                return self._json(200, stop_process(grace))
            if method == "GET" and api == "logs/tail":
                return self._json(200, tail_logs(qs))
            if method == "GET" and api == "metrics":
                return self._json(200, metrics_snapshot())
            if method == "GET" and api == "logs/stream":
                return self._sse(qs)
            return self._json(404, {"error": "no such api: %s %s" % (method, api)})
        except ApiError as e:
            return self._json(e.status, {"error": e.msg, **e.extra})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            traceback.print_exc()
            try:
                self._json(500, {"error": "internal error"})
            except Exception:
                pass

    # -- SSE -----------------------------------------------------------------

    def _sse(self, qs):
        try:
            after = int((qs.get("after") or ["0"])[0])
        except (ValueError, IndexError):
            after = 0
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id:
            try:
                after = max(after, int(last_event_id))
            except ValueError:
                pass
        q = queue.Queue()
        with LOCK:
            seed = [it for it in RING if it[0] > after][-500:]
            WATCHERS.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(b"retry: 500\n\n")
            for seq, text in seed:
                self._sse_event("line", {"seq": seq, "text": text}, seq)
            while True:
                try:
                    kind, payload = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if kind == "line":
                    self._sse_event("line", payload, payload.get("seq"))
                else:
                    self._sse_event("exit", payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with LOCK:
                if q in WATCHERS:
                    WATCHERS.remove(q)
            self.close_connection = True

    def _sse_event(self, event, payload, event_id=None):
        out = ""
        if event_id is not None:
            out += "id: %d\n" % event_id
        out += "event: %s\ndata: %s\n\n" % (event, json.dumps(payload))
        self.wfile.write(out.encode("utf-8"))
        self.wfile.flush()


def _json_body(body):
    if not body:
        return {}
    try:
        d = json.loads(body)
    except Exception:
        raise ApiError(400, "invalid JSON body")
    if not isinstance(d, dict):
        raise ApiError(400, "JSON object expected")
    return d


# ---------------------------------------------------------------------------
# shutdown


def _handle_term(signum, frame):
    threading.Thread(target=_shutdown, daemon=True).start()


def _shutdown():
    # Stop only the process we track - never adopt an externally-started
    # service here: the app dying must not kill a service the user started
    # outside the app.
    try:
        with LOCK:
            if RUN is not None:
                _stop_locked(5.0)
    except Exception:
        pass
    if SERVER is not None:
        try:
            SERVER.shutdown()
        except Exception:
            pass


def main():
    global SERVER
    port = int(os.environ.get("PORT") or "9147")
    _load_state()
    reconcile()
    threading.Thread(target=_metrics_loop, daemon=True).start()
    SERVER = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    SERVER.daemon_threads = True
    print(
        "[si] %s v%s listening on 127.0.0.1:%d (pid %d, data %s)"
        % (APP_NAME, APP_VERSION, port, os.getpid(), DATA_DIR),
        flush=True,
    )
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    try:
        SERVER.serve_forever(poll_interval=0.5)
    finally:
        try:
            with LOCK:
                if RUN is not None:
                    _stop_locked(3.0)
        except Exception:
            pass
        SERVER.server_close()
        print("[si] stopped", flush=True)


if __name__ == "__main__":
    main()
