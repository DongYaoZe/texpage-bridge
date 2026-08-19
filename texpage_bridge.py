#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "projects.json"
BROKER_SCRIPT = ROOT / "texpage_browser_broker.py"
DEFAULT_PROFILE = Path(
    os.environ.get(
        "TEXPAGE_BRIDGE_PROFILE",
        str(Path.home() / ".texpage-bridge" / "chromium-profile"),
    )
)
DEFAULT_BROKER_HOST = "127.0.0.1"
DEFAULT_BROKER_PORT = 43177


class BridgeError(RuntimeError):
    pass


def run(cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None, timeout: int = 60) -> str:
    cp = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if cp.returncode != 0:
        raise BridgeError(
            f"Command failed ({cp.returncode}): {' '.join(cmd)}\n{cp.stderr.strip()}"
        )
    return cp.stdout.strip()


def load_config(project_name: str) -> dict:
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    common = data.get("_common", {})
    projects = data.get("projects", {})
    if project_name not in projects:
        raise BridgeError(f"Unknown project: {project_name}")
    cfg = dict(common)
    cfg.update(projects[project_name])
    cfg["name"] = project_name
    return cfg


def ensure_local_cache(repo: Path) -> Path:
    cache = repo / ".texpage"
    cache.mkdir(exist_ok=True)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    old = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    if not re.search(r"(?m)^\.texpage/$", old):
        with exclude.open("a", encoding="utf-8") as f:
            if old and not old.endswith("\n"):
                f.write("\n")
            f.write("\n# Local TeXPage cloud-build cache\n.texpage/\n")
    return cache


def snapshot_worktree(repo: Path, message: str) -> str:
    """Create a commit object representing the worktree without touching the real index/branch."""
    fd, idx = tempfile.mkstemp(prefix="texpage-index-")
    os.close(fd)
    try:
        # Git requires a non-existent path (or a valid index), not an empty file.
        os.unlink(idx)
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = idx
        run(["git", "read-tree", "HEAD"], repo, env=env)
        run(["git", "add", "-A", "--", "."], repo, env=env, timeout=120)
        tree = run(["git", "write-tree"], repo, env=env)
        # Make the cloud-build snapshot a root commit.  TeXPage's isolated build branch
        # does not need the local GitHub history; using HEAD as a parent can cause Git
        # to upload the repository's entire unrelated history on the first push.
        cp = subprocess.run(
            ["git", "commit-tree", tree],
            cwd=str(repo),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=message + "\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if cp.returncode != 0:
            raise BridgeError(f"git commit-tree failed: {cp.stderr.strip()}")
        return cp.stdout.strip()
    finally:
        try:
            os.unlink(idx)
        except FileNotFoundError:
            pass


def push_snapshot(repo: Path, remote: str, version: str, commit: str) -> None:
    # Explicitly re-enable TLS verification even if the user's unrelated global Git config disables it.
    # Force host-level, non-interactive credential reuse so parallel agents never race on GCM dialogs.
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    cmd = [
        "git",
        "-c",
        "http.sslVerify=true",
        "-c",
        "credential.useHttpPath=false",
        "push",
        "--force",
        remote,
        f"{commit}:refs/heads/{version}",
    ]

    # TeXPage creates the version through its web API and materializes the
    # corresponding Git ref asynchronously.  A push issued in that small
    # window can be rejected with "cannot lock ref ... reference already
    # exists" even though retrying the same forced update seconds later is
    # valid.  Retry only that narrow, observed race; all other Git errors are
    # still surfaced immediately.
    retry_delays = (0.0, 0.75, 1.5, 3.0)
    for attempt, delay in enumerate(retry_delays):
        if delay:
            time.sleep(delay)
        try:
            run(cmd, repo, env=env, timeout=180)
            return
        except BridgeError as exc:
            text = str(exc).lower()
            transient_ref_race = (
                "cannot lock ref" in text and "reference already exists" in text
            )
            if not transient_ref_race or attempt == len(retry_delays) - 1:
                raise


def git_auth_available(repo: Path, remote: str) -> bool:
    """Check TeXPage Git authentication non-interactively without exposing credentials."""
    try:
        remote_url = run(["git", "remote", "get-url", remote], repo, timeout=10)
    except Exception:
        return False
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    cp = subprocess.run(
        [
            "git",
            "-c",
            "http.sslVerify=true",
            "-c",
            "credential.useHttpPath=false",
            "ls-remote",
            remote_url,
            "HEAD",
        ],
        cwd=str(repo),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=25,
    )
    return cp.returncode == 0


def store_git_credential(password: str, username: str = "git") -> None:
    """Store one host-level TeXPage Git credential through GCM using stdin only."""
    payload = (
        "protocol=https\n"
        "host=git.tex.nju.edu.cn\n"
        f"username={username}\n"
        f"password={password}\n\n"
    )
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    cp = subprocess.run(
        [
            "git",
            "-c",
            "credential.useHttpPath=false",
            "credential",
            "approve",
        ],
        input=payload,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if cp.returncode != 0:
        raise BridgeError(f"Could not store TeXPage Git credential in GCM: {cp.stderr.strip()}")


def download_atomic(url: str, dest: Path) -> int:
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        size = tmp.stat().st_size
        if size <= 0:
            raise BridgeError(f"Downloaded empty artifact: {dest.name}")
        os.replace(tmp, dest)
        return size
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def broker_endpoint(cfg: dict) -> tuple[str, int]:
    return (
        str(cfg.get("broker_host", DEFAULT_BROKER_HOST)),
        int(cfg.get("broker_port", DEFAULT_BROKER_PORT)),
    )


def broker_request(cfg: dict, payload: dict, timeout_s: int = 10) -> dict:
    host, port = broker_endpoint(cfg)
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=min(timeout_s, 10)) as sock:
            sock.settimeout(timeout_s)
            sock.sendall(raw)
            buf = bytearray()
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
    except OSError as exc:
        raise BridgeError(f"TeXPage browser broker unavailable: {exc}") from exc
    if not buf:
        raise BridgeError("TeXPage browser broker returned no response")
    result = json.loads(buf.decode("utf-8"))
    if not result.get("ok"):
        raise BridgeError(result.get("error", "TeXPage browser broker failed"))
    return result


def broker_alive(cfg: dict) -> bool:
    try:
        broker_request(cfg, {"action": "ping"}, timeout_s=2)
        return True
    except Exception:
        return False


def ensure_broker(cfg: dict) -> None:
    if broker_alive(cfg):
        return
    profile = Path(os.environ.get("TEXPAGE_PROFILE", cfg.get("profile", DEFAULT_PROFILE)))
    host, port = broker_endpoint(cfg)
    log_path = ROOT / "broker.log"
    log = open(log_path, "ab", buffering=0)
    # On Windows use pythonw for the long-lived broker so the scheduler never
    # leaves a visible Python console/window on the user's desktop.  Keep the
    # exact same venv/interpreter environment as the short-lived CLI.
    broker_python = sys.executable
    if os.name == "nt":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.is_file():
            broker_python = str(pythonw)
    cmd = [
        broker_python,
        str(BROKER_SCRIPT),
        "--host",
        host,
        "--port",
        str(port),
        "--profile",
        str(profile),
        "--workers",
        str(int(cfg.get("browser_workers", 4))),
    ]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": log,
        "cwd": str(ROOT),
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    deadline = time.time() + 20
    while time.time() < deadline:
        if broker_alive(cfg):
            return
        time.sleep(0.25)
    raise BridgeError(f"Could not start TeXPage browser broker; see {log_path}")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextmanager
def config_update_lock(timeout_s: int = 30):
    lock_dir = ROOT / ".config.lock"
    deadline = time.time() + timeout_s
    while True:
        try:
            lock_dir.mkdir()
            (lock_dir / "owner.json").write_text(
                json.dumps({"pid": os.getpid(), "started_at": time.time()}, indent=2) + "\n",
                encoding="utf-8",
            )
            break
        except FileExistsError:
            try:
                owner_file = lock_dir / "owner.json"
                owner = json.loads(owner_file.read_text(encoding="utf-8")) if owner_file.exists() else {}
                owner_pid = int(owner.get("pid", 0))
                age = time.time() - lock_dir.stat().st_mtime
                if (owner_pid > 0 and not pid_alive(owner_pid)) or age > 300:
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
            except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError):
                pass
            if time.time() >= deadline:
                raise BridgeError("Timed out waiting to update projects.json")
            time.sleep(0.2)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


@contextmanager
def project_version_lock(cfg: dict, timeout_s: int = 60):
    """Keep one project's reserve-version + config update transaction ordered."""
    repo = Path(cfg["repo"]).resolve()
    cache = ensure_local_cache(repo)
    lock_dir = cache / "version.lock"
    deadline = time.time() + timeout_s
    while True:
        try:
            lock_dir.mkdir()
            (lock_dir / "owner.json").write_text(
                json.dumps({"pid": os.getpid(), "started_at": time.time()}, indent=2) + "\n",
                encoding="utf-8",
            )
            break
        except FileExistsError:
            try:
                owner_file = lock_dir / "owner.json"
                owner = json.loads(owner_file.read_text(encoding="utf-8")) if owner_file.exists() else {}
                owner_pid = int(owner.get("pid", 0))
                age = time.time() - lock_dir.stat().st_mtime
                if (owner_pid > 0 and not pid_alive(owner_pid)) or age > 600:
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
            except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError):
                pass
            if time.time() >= deadline:
                raise BridgeError(f"Timed out waiting to reserve a version for {cfg['name']}")
            time.sleep(0.2)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


@contextmanager
def project_build_lock(cache: Path, timeout_s: int):
    """Serialize one project while allowing different projects to build concurrently."""
    lock_dir = cache / "build.lock"
    deadline = time.time() + timeout_s
    while True:
        try:
            lock_dir.mkdir()
            (lock_dir / "owner.json").write_text(
                json.dumps({"pid": os.getpid(), "started_at": time.time()}, indent=2) + "\n",
                encoding="utf-8",
            )
            break
        except FileExistsError:
            try:
                owner_file = lock_dir / "owner.json"
                owner = json.loads(owner_file.read_text(encoding="utf-8")) if owner_file.exists() else {}
                owner_pid = int(owner.get("pid", 0))
                owner_alive = pid_alive(owner_pid)
                age = time.time() - lock_dir.stat().st_mtime
                if (owner_pid > 0 and not owner_alive) or age > max(1800, timeout_s * 2):
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
            except FileNotFoundError:
                continue
            except (ValueError, json.JSONDecodeError, OSError):
                pass
            if time.time() >= deadline:
                raise BridgeError(f"Timed out waiting for another build of {cache.parent.name}")
            time.sleep(0.5)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def cloud_compile(cfg: dict, cache: Path, commit: str, timeout_s: int) -> dict:
    ensure_broker(cfg)
    project_key = cfg["project_key"]
    version_no = cfg["version_no"]
    version = cfg["version"]
    started_at = time.time()
    broker_result = broker_request(
        cfg,
        {
            "action": "compile",
            "project": cfg["name"],
            "project_key": project_key,
            "version": version,
            "version_no": version_no,
            "timeout_s": timeout_s,
        },
        # A caller may be behind several other projects in the broker's
        # global TeXPage UI queue.  Do not let the client-side socket timeout
        # turn healthy queueing into a false build failure.
        timeout_s=max(timeout_s + 60, int(cfg.get("broker_compile_wait_s", 3600))),
    )
    pdf_size = download_atomic(broker_result["pdf_url"], cache / "latest.pdf")
    log_size = download_atomic(broker_result["log_url"], cache / "latest.log")
    log_raw = (cache / "latest.log").read_bytes()
    if log_raw.startswith(b"\x1f\x8b"):
        try:
            log_raw = gzip.decompress(log_raw)
        except OSError:
            pass
    log_text = log_raw.decode("utf-8", errors="replace")
    bang_errors = len(re.findall(r"(?m)^! ", log_text))
    fileline_errors = len(re.findall(r"(?m)^.*?:\d+: LaTeX Error:", log_text))
    tex_errors = bang_errors + fileline_errors
    warnings = len(re.findall(r"(?i)warning", log_text))
    return {
        "project": cfg["name"],
        "project_key": project_key,
        "version": version,
        "version_no": version_no,
        "snapshot_commit": commit,
        "status": "success",
        "elapsed_seconds": round(time.time() - started_at, 1),
        "broker_elapsed_seconds": broker_result.get("elapsed_seconds"),
        "pdf_bytes": pdf_size,
        "log_bytes": log_size,
        "tex_errors": tex_errors,
        "warning_mentions": warnings,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

def cmd_build(cfg: dict, timeout_s: int, no_push: bool) -> int:
    repo = Path(cfg["repo"]).resolve()
    if not (repo / ".git").exists():
        raise BridgeError(f"Not a Git repository: {repo}")
    cache = ensure_local_cache(repo)
    # Same-project builds are serialized locally as well.  The holder may
    # itself be waiting in the global browser queue, so use a queue-aware
    # upper bound instead of only one compile duration.
    with project_build_lock(cache, max(timeout_s + 180, int(cfg.get("broker_compile_wait_s", 3600)))):
        if no_push:
            commit = run(["git", "rev-parse", "HEAD"], repo)
        else:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            commit = snapshot_worktree(repo, f"TeXPage cloud-build snapshot {stamp}")
            push_snapshot(repo, cfg.get("git_remote", "texpage"), cfg["version"], commit)
            # Give the TeXPage Web↔Git synchronizer a moment to ingest the new branch tip.
            time.sleep(2.0)

        print(f"TeXPage build: {cfg['name']} @ {cfg['version']}", flush=True)
        print(f"Snapshot: {commit[:12]}", flush=True)
        result = cloud_compile(cfg, cache, commit, timeout_s)
        (cache / "build.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("BUILD SUCCESS", flush=True)
        print(f"PDF: {cache / 'latest.pdf'} ({result['pdf_bytes'] / 1024 / 1024:.1f} MiB)", flush=True)
        print(f"LOG: {cache / 'latest.log'} ({result['log_bytes'] / 1024:.1f} KiB)", flush=True)
        print(f"TeX ! errors: {result['tex_errors']}; warning mentions: {result['warning_mentions']}", flush=True)
    return 0


TERMINAL_REQUEST_STATES = {"success", "failed", "interrupted"}


def _request_record(cfg: dict, request_id: str) -> dict:
    result = broker_request(
        cfg,
        {
            "action": "request_status",
            "request_id": request_id,
            "project": cfg["name"],
        },
        timeout_s=10,
    )
    return dict(result["request"])


def _print_request_result(record: dict) -> int:
    status = str(record.get("status") or "unknown")
    request_id = str(record.get("request_id") or "?")
    if status == "success":
        result = dict(record.get("result") or {})
        print(f"REQUEST SUCCESS: {request_id}")
        print(f"TeXPage build: {record.get('project')} @ {record.get('version')}")
        print(f"Snapshot: {str(record.get('snapshot_commit') or '')[:12]}")
        if result.get("pdf_path"):
            print(
                f"PDF: {result['pdf_path']} "
                f"({int(result.get('pdf_bytes', 0)) / 1024 / 1024:.1f} MiB)"
            )
        if result.get("log_path"):
            print(
                f"LOG: {result['log_path']} "
                f"({int(result.get('log_bytes', 0)) / 1024:.1f} KiB)"
            )
        print(
            f"TeX ! errors: {result.get('tex_errors', '?')}; "
            f"warning mentions: {result.get('warning_mentions', '?')}"
        )
        return 0
    print(
        f"REQUEST FAILED: {request_id}: {record.get('error') or status}",
        file=sys.stderr,
    )
    return 2


def cmd_submit(
    cfg: dict,
    timeout_s: int,
    no_push: bool,
    wait: bool,
    new_version: bool = False,
) -> int:
    """Submit a build to the central service; optionally wait for its result."""
    ensure_broker(cfg)
    submitted = broker_request(
        cfg,
        {
            "action": "submit_build",
            # Intentionally submit only the allow-listed project alias and
            # non-sensitive execution options. The service loads repo paths,
            # TeXPage keys, versions, credentials, and browser state itself.
            "project": cfg["name"],
            "timeout_s": timeout_s,
            "no_push": no_push,
            "new_version": new_version,
        },
        timeout_s=max(180, timeout_s),
    )
    record = dict(submitted["request"])
    request_id = str(record["request_id"])
    if record.get("status") in {"failed", "interrupted"}:
        return _print_request_result(record)
    print(
        f"REQUEST SUBMITTED: {request_id} "
        f"{record.get('project')} @ {record.get('version')} "
        f"snapshot={str(record.get('snapshot_commit') or '')[:12]}",
        flush=True,
    )
    if not wait:
        return 0

    last_status = None
    deadline = time.time() + max(timeout_s + 600, int(cfg.get("broker_compile_wait_s", 3600)))
    while time.time() < deadline:
        record = _request_record(cfg, request_id)
        status = str(record.get("status") or "unknown")
        if status != last_status:
            print(f"REQUEST {request_id}: {status}", flush=True)
            last_status = status
        if status in TERMINAL_REQUEST_STATES:
            return _print_request_result(record)
        time.sleep(1.0)
    raise BridgeError(f"Timed out waiting for central TeXPage request {request_id}")


def cmd_request(cfg: dict, request_id: str) -> int:
    ensure_broker(cfg)
    record = _request_record(cfg, request_id)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record.get("status") != "failed" else 2


def cmd_requests(cfg: dict, limit: int) -> int:
    ensure_broker(cfg)
    result = broker_request(
        cfg,
        {
            "action": "list_requests",
            "project": cfg["name"],
            "limit": limit,
        },
        timeout_s=10,
    )
    records = list(result.get("requests", []))
    if not records:
        print("No central TeXPage requests for this project yet.")
        return 0
    for record in records:
        print(
            f"{record.get('request_id')}\t{record.get('status')}\t"
            f"{record.get('version')}\t{str(record.get('snapshot_commit') or '')[:12]}\t"
            f"{record.get('updated_at') or record.get('created_at') or ''}"
        )
    return 0


def cmd_status(cfg: dict) -> int:
    repo = Path(cfg["repo"]).resolve()
    status_file = repo / ".texpage" / "build.json"
    if not status_file.exists():
        print("No local TeXPage build record yet.")
        return 1
    print(status_file.read_text(encoding="utf-8"))
    return 0


def cmd_versions(cfg: dict) -> int:
    ensure_broker(cfg)
    result = broker_request(
        cfg,
        {"action": "versions", "project_key": cfg["project_key"]},
        timeout_s=20,
    )
    versions = result.get("versions", [])
    if not versions:
        print("No TeXPage versions found.")
        return 1
    for item in versions:
        marker = " *default" if item.get("is_default") else ""
        source = f" <- {item.get('from_version')}" if item.get("from_version") else ""
        print(f"{item.get('version')}\t{item.get('version_no')}{source}{marker}")
    return 0


def update_project_version_config(project_name: str, version: str, version_no: str) -> None:
    """Atomically update only one project's selected TeXPage version."""
    with config_update_lock():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        projects = data.get("projects", {})
        if project_name not in projects:
            raise BridgeError(f"Unknown project in projects.json: {project_name}")
        projects[project_name]["version"] = version
        projects[project_name]["version_no"] = version_no
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)


def cmd_set_version(cfg: dict, project_name: str, version: str, version_no: str) -> int:
    ensure_broker(cfg)
    remote = broker_request(
        cfg,
        {"action": "versions", "project_key": cfg["project_key"]},
        timeout_s=20,
    )
    exact = [
        x for x in remote.get("versions", [])
        if x.get("version") == version and x.get("version_no") == version_no
    ]
    if not exact:
        raise BridgeError(
            f"Refusing config update: TeXPage does not report exact pair {version} / {version_no}"
        )
    update_project_version_config(project_name, version, version_no)
    print(f"Updated {project_name}: {version} / {version_no}")
    return 0


def cmd_reserve_version(cfg: dict, project_name: str, dry_run: bool = False) -> int:
    """Create the next live TeXPage version and atomically select it in projects.json."""
    ensure_broker(cfg)
    with project_version_lock(cfg):
        created = broker_request(
            cfg,
            {
                "action": "reserve_next_version",
                "project": project_name,
                "project_key": cfg["project_key"],
                "dry_run": dry_run,
            },
            timeout_s=45,
        )
        version = str(created["version"])
        if dry_run:
            print(
                f"Would reserve {project_name}: {version} "
                f"<- {created.get('from_version') or '?'} "
                f"({created.get('from_version_no') or '?'})"
            )
            return 0
        version_no = str(created["version_no"])
        update_project_version_config(project_name, version, version_no)
    print(
        f"Reserved {project_name}: {version} / {version_no} "
        f"<- {created.get('from_version') or '?'}"
    )
    return 0


def cmd_broker(cfg: dict, action: str) -> int:
    if action == "start":
        ensure_broker(cfg)
        result = broker_request(cfg, {"action": "status"}, timeout_s=3)
    elif action == "stop":
        if not broker_alive(cfg):
            print("TeXPage browser broker is not running.")
            return 0
        result = broker_request(cfg, {"action": "shutdown"}, timeout_s=3)
    else:
        if not broker_alive(cfg):
            print("TeXPage browser broker is not running.")
            return 1
        result = broker_request(cfg, {"action": "status"}, timeout_s=3)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NJU TeXPage cloud compile bridge")
    parser.add_argument("project", help="Project key from projects.json")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser(
        "build",
        help="Submit to the central TeXPage service and wait for PDF/log",
    )
    b.add_argument("--timeout", type=int, default=240, help="Compile timeout in seconds")
    b.add_argument("--no-push", action="store_true", help="Compile current TeXPage version without Git push")
    s = sub.add_parser(
        "submit",
        help="Freeze the current worktree and submit an asynchronous central build request",
    )
    s.add_argument("--timeout", type=int, default=240, help="Compile timeout in seconds")
    s.add_argument("--no-push", action="store_true", help="Compile current TeXPage version without Git push")
    p = sub.add_parser(
        "publish",
        help="Formal publish: freeze source, reserve next TeXPage version, push, compile, and wait",
    )
    p.add_argument("--timeout", type=int, default=240, help="Compile timeout in seconds")
    rq = sub.add_parser("request", help="Show one central build request by request id")
    rq.add_argument("request_id")
    rqs = sub.add_parser("requests", help="List recent central build requests for this project")
    rqs.add_argument("--limit", type=int, default=20)
    sub.add_parser("status", help="Show last successful local build record")
    sub.add_parser("versions", help="List live TeXPage versions for this project")
    rv = sub.add_parser(
        "reserve-version",
        help="Atomically create and select the next unused TeXPage vMAJOR.MINOR version",
    )
    rv.add_argument("--dry-run", action="store_true", help="Show the next version without creating it")
    sv = sub.add_parser("set-version", help="Atomically select an existing TeXPage version")
    sv.add_argument("version")
    sv.add_argument("version_no")
    br = sub.add_parser("broker", help="Manage the shared one-browser multi-tab broker")
    br.add_argument("action", choices=["start", "status", "stop"])
    args = parser.parse_args()

    try:
        cfg = load_config(args.project)
        if args.command == "build":
            return cmd_submit(cfg, args.timeout, args.no_push, wait=True)
        if args.command == "submit":
            return cmd_submit(cfg, args.timeout, args.no_push, wait=False)
        if args.command == "publish":
            return cmd_submit(cfg, args.timeout, no_push=False, wait=True, new_version=True)
        if args.command == "request":
            return cmd_request(cfg, args.request_id)
        if args.command == "requests":
            return cmd_requests(cfg, args.limit)
        if args.command == "versions":
            return cmd_versions(cfg)
        if args.command == "reserve-version":
            return cmd_reserve_version(cfg, args.project, args.dry_run)
        if args.command == "set-version":
            return cmd_set_version(cfg, args.project, args.version, args.version_no)
        if args.command == "broker":
            return cmd_broker(cfg, args.action)
        return cmd_status(cfg)
    except (BridgeError, subprocess.TimeoutExpired) as e:
        print(f"BUILD FAILED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
