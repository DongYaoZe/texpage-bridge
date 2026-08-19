#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 43177
DEFAULT_PROFILE = Path(
    os.environ.get(
        "TEXPAGE_BRIDGE_PROFILE",
        str(Path.home() / ".texpage-bridge" / "chromium-profile"),
    )
)
RESULT_RE = re.compile(r"/CompileResult/.+?/output\.(pdf|log)(?:\?|$)", re.I)
REQUEST_DIR = Path(__file__).resolve().parent / "requests"
TERMINAL_REQUEST_STATES = {"success", "failed", "interrupted"}


def newest_signed_url(events: list[tuple[float, str, str]], ext: str) -> str | None:
    candidates = [(ts, url) for ts, kind, url in events if kind == ext]
    if not candidates:
        return None

    def key(item: tuple[float, str]):
        ts, url = item
        q = parse_qs(urlparse(url).query)
        signed = q.get("X-Amz-Date", [""])[0]
        return signed, ts

    return max(candidates, key=key)[1]


async def visible_text_exists(page, text: str) -> bool:
    loc = page.get_by_text(text, exact=True)
    try:
        count = min(await loc.count(), 6)
        for i in range(count):
            if await loc.nth(i).is_visible():
                return True
    except Exception:
        return False
    return False


async def click_exact_text(page, text: str) -> None:
    loc = page.get_by_text(text, exact=True)
    count = min(await loc.count(), 10)
    for i in range(count):
        item = loc.nth(i)
        if await item.is_visible():
            await item.click(timeout=5000)
            return
    raise RuntimeError(f"Could not find clickable text: {text}")


def hide_windows_from_taskbar(process_id: int) -> int:
    """Keep this Chromium headful/visible while removing its top-level windows from the taskbar."""
    if os.name != "nt" or process_id <= 0:
        return 0

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        get_window_long = user32.GetWindowLongPtrW
        set_window_long = user32.SetWindowLongPtrW
        get_window_long.restype = ctypes.c_longlong
        set_window_long.restype = ctypes.c_longlong
    else:
        get_window_long = user32.GetWindowLongW
        set_window_long = user32.SetWindowLongW
        get_window_long.restype = ctypes.c_long
        set_window_long.restype = ctypes.c_long

    get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]

    hidden = 0
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def enum_window(hwnd, _lparam):
        nonlocal hidden
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != process_id or not user32.IsWindowVisible(hwnd):
            return True
        old_style = int(get_window_long(hwnd, GWL_EXSTYLE))
        new_style = (old_style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        if new_style != old_style:
            # Windows' taskbar does not always refresh an already-visible
            # window's extended style. Briefly hide/show without activation;
            # the worker remains a genuine visible headful Chromium window.
            user32.ShowWindow(hwnd, SW_HIDE)
            set_window_long(hwnd, GWL_EXSTYLE, new_style)
            user32.SetWindowPos(
                hwnd,
                None,
                0,
                0,
                0,
                0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        hidden += 1
        return True

    user32.EnumWindows(enum_window, 0)
    return hidden


class BrowserBroker:
    def __init__(self, profile: Path, worker_count: int = 4):
        self.profile = profile
        self.worker_count = max(1, int(worker_count))
        self.playwright = None
        self.context = None
        self.browser_pid = 0
        self.project_locks: dict[str, asyncio.Lock] = {}
        self.service_project_locks: dict[str, asyncio.Lock] = {}
        self.version_request_locks: dict[str, asyncio.Lock] = {}
        self.service_slots = asyncio.Semaphore(self.worker_count)
        self.request_jobs: dict[str, dict] = {}
        self.request_tasks: dict[str, asyncio.Task] = {}
        # Build requests are centrally queued, then assigned to a fixed pool of
        # real top-level Chromium windows.  Agents never own browser pages.
        # Different workers can compile concurrently; one project is still
        # serialized by project_locks so two snapshots cannot race its branch.
        self.workers: list[dict] = []
        self.available_workers: asyncio.Queue = asyncio.Queue()
        self.ui_waiting: list[dict] = []
        self.ui_active: dict[int, dict] = {}
        self.ui_sequence = 0
        self.active: dict[str, dict] = {}
        self.started_at = time.time()

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _request_path(self, request_id: str) -> Path:
        return REQUEST_DIR / f"{request_id}.json"

    def _persist_request(self, record: dict) -> None:
        REQUEST_DIR.mkdir(parents=True, exist_ok=True)
        path = self._request_path(str(record["request_id"]))
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _update_request(self, request_id: str, **changes) -> dict:
        record = self.request_jobs[request_id]
        record.update(changes)
        record["updated_at"] = self._iso_now()
        self._persist_request(record)
        return record

    def _recover_request_records(self) -> None:
        REQUEST_DIR.mkdir(parents=True, exist_ok=True)
        for path in REQUEST_DIR.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            request_id = str(record.get("request_id") or path.stem)
            record["request_id"] = request_id
            if record.get("status") not in TERMINAL_REQUEST_STATES:
                record["status"] = "interrupted"
                record["error"] = "Central TeXPage service restarted before this request completed."
                record["completed_at"] = self._iso_now()
                record["updated_at"] = record["completed_at"]
                try:
                    self._persist_request(record)
                except Exception:
                    pass
            self.request_jobs[request_id] = record

    async def submit_build(self, req: dict) -> dict:
        """Freeze a source snapshot, enqueue the rest of the build, and return a request id."""
        import texpage_bridge as bridge

        name = str(req["project"])
        timeout_s = int(req.get("timeout_s", 240))
        no_push = bool(req.get("no_push", False))
        new_version = bool(req.get("new_version", False))
        if no_push and new_version:
            raise RuntimeError("A publish request cannot combine new_version with no_push")
        cfg = bridge.load_config(name)
        repo = Path(cfg["repo"]).resolve()
        if not (repo / ".git").exists():
            raise RuntimeError(f"Not a Git repository: {repo}")
        cache = bridge.ensure_local_cache(repo)
        request_id = f"tp-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        record = {
            "request_id": request_id,
            "project": name,
            "status": "snapshotting",
            "version": str(cfg["version"]),
            "version_no": str(cfg["version_no"]),
            "no_push": no_push,
            "new_version": new_version,
            "created_at": self._iso_now(),
            "updated_at": self._iso_now(),
        }
        self.request_jobs[request_id] = record
        self._persist_request(record)
        try:
            if no_push:
                commit = await asyncio.to_thread(
                    bridge.run, ["git", "rev-parse", "HEAD"], repo
                )
            else:
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                commit = await asyncio.to_thread(
                    bridge.snapshot_worktree,
                    repo,
                    f"TeXPage service snapshot {stamp}",
                )
        except Exception as exc:
            self._update_request(
                request_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                completed_at=self._iso_now(),
            )
            return self._public_request(self.request_jobs[request_id])

        if new_version:
            version_lock = self.version_request_locks.setdefault(name, asyncio.Lock())
            try:
                async with version_lock:
                    self._update_request(
                        request_id,
                        status="reserving_version",
                        snapshot_commit=str(commit),
                    )
                    created = await self.reserve_next_version(
                        {
                            "project": name,
                            "project_key": cfg["project_key"],
                            "dry_run": False,
                        }
                    )
                    cfg["version"] = str(created["version"])
                    cfg["version_no"] = str(created["version_no"])
                    await asyncio.to_thread(
                        bridge.update_project_version_config,
                        name,
                        cfg["version"],
                        cfg["version_no"],
                    )
                    self._update_request(
                        request_id,
                        version=cfg["version"],
                        version_no=cfg["version_no"],
                        from_version=created.get("from_version"),
                    )
            except Exception as exc:
                self._update_request(
                    request_id,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    completed_at=self._iso_now(),
                )
                return self._public_request(self.request_jobs[request_id])

        # Freeze both the snapshot and target TeXPage version at submission time.
        # Later config edits cannot silently redirect an already submitted request.
        self._update_request(
            request_id,
            status="queued",
            snapshot_commit=str(commit),
            queued_at=self._iso_now(),
        )
        task = asyncio.create_task(
            self._run_build_request(
                request_id=request_id,
                cfg=dict(cfg),
                cache=cache,
                commit=str(commit),
                timeout_s=timeout_s,
                no_push=no_push,
            )
        )
        self.request_tasks[request_id] = task
        task.add_done_callback(lambda _t, rid=request_id: self.request_tasks.pop(rid, None))
        return self._public_request(self.request_jobs[request_id])

    @staticmethod
    def _find_token_secret(value):
        """Find a token-like string in an API response without ever logging it."""
        if isinstance(value, dict):
            preferred = ("token", "gitToken", "accessToken", "password", "secret")
            for key in preferred:
                candidate = value.get(key)
                if isinstance(candidate, str) and len(candidate) >= 8:
                    return candidate
            for key, candidate in value.items():
                if any(word in str(key).lower() for word in ("token", "password", "secret")):
                    if isinstance(candidate, str) and len(candidate) >= 8:
                        return candidate
            for candidate in value.values():
                found = BrowserBroker._find_token_secret(candidate)
                if found:
                    return found
        elif isinstance(value, list):
            for candidate in value:
                found = BrowserBroker._find_token_secret(candidate)
                if found:
                    return found
        return None

    async def ensure_git_credential(self, cfg: dict) -> dict:
        """Repair TeXPage Git auth centrally using the logged-in web session if needed."""
        import texpage_bridge as bridge

        repo = Path(cfg["repo"]).resolve()
        remote = str(cfg.get("git_remote", "texpage"))
        if await asyncio.to_thread(bridge.git_auth_available, repo, remote):
            return {"ok": True, "repaired": False}

        response = await self.context.request.post(
            "https://tex.nju.edu.cn/api/git/token",
            timeout=20000,
        )
        if not response.ok:
            raise RuntimeError(f"TeXPage Git token creation failed: HTTP {response.status}")
        data = await response.json()
        if data.get("status", {}).get("code") != 1:
            raise RuntimeError(f"TeXPage Git token creation returned: {data.get('status')}")
        token = self._find_token_secret(data.get("result"))
        if not token:
            result = data.get("result")
            fields = sorted(result.keys()) if isinstance(result, dict) else [type(result).__name__]
            raise RuntimeError(f"TeXPage Git token API returned no usable token field; fields={fields}")

        # The secret travels only through broker memory and git-credential stdin.
        # It is never placed on a command line, persisted in request JSON, or
        # returned over the broker socket.
        await asyncio.to_thread(bridge.store_git_credential, token, "git")
        token = None
        if not await asyncio.to_thread(bridge.git_auth_available, repo, remote):
            raise RuntimeError("TeXPage Git credential was stored but non-interactive authentication still failed")
        return {"ok": True, "repaired": True}

    async def git_auth_ensure(self, req: dict) -> dict:
        import texpage_bridge as bridge

        cfg = bridge.load_config(str(req["project"]))
        result = await self.ensure_git_credential(cfg)
        return {
            "ok": True,
            "project": cfg["name"],
            "repaired": bool(result.get("repaired")),
        }

    async def _run_build_request(
        self,
        *,
        request_id: str,
        cfg: dict,
        cache: Path,
        commit: str,
        timeout_s: int,
        no_push: bool,
    ) -> None:
        import gzip
        import texpage_bridge as bridge

        name = str(cfg["name"])
        repo = Path(cfg["repo"]).resolve()
        service_lock = self.service_project_locks.setdefault(name, asyncio.Lock())
        started = time.time()
        try:
            async with service_lock:
                async with self.service_slots:
                    self._update_request(
                        request_id,
                        status="running",
                        started_at=self._iso_now(),
                    )
                    if not no_push:
                        self._update_request(request_id, status="authenticating_git")
                        auth = await self.ensure_git_credential(cfg)
                        if auth.get("repaired"):
                            self._update_request(request_id, credential_repaired=True)
                        self._update_request(request_id, status="pushing")
                        await asyncio.to_thread(
                            bridge.push_snapshot,
                            repo,
                            cfg.get("git_remote", "texpage"),
                            str(cfg["version"]),
                            commit,
                        )
                        await asyncio.sleep(2.0)

                    self._update_request(request_id, status="compiling")
                    broker_result = await self.compile(
                        {
                            "project": name,
                            "project_key": cfg["project_key"],
                            "version": cfg["version"],
                            "version_no": cfg["version_no"],
                            "timeout_s": timeout_s,
                        }
                    )

                    # Signed result URLs never leave the central service.  Only
                    # sanitized paths and QA metrics are persisted/returned.
                    self._update_request(request_id, status="downloading")
                    pdf_size = await asyncio.to_thread(
                        bridge.download_atomic,
                        broker_result["pdf_url"],
                        cache / "latest.pdf",
                    )
                    log_size = await asyncio.to_thread(
                        bridge.download_atomic,
                        broker_result["log_url"],
                        cache / "latest.log",
                    )
                    log_raw = await asyncio.to_thread((cache / "latest.log").read_bytes)
                    if log_raw.startswith(b"\x1f\x8b"):
                        try:
                            log_raw = gzip.decompress(log_raw)
                        except OSError:
                            pass
                    log_text = log_raw.decode("utf-8", errors="replace")
                    tex_errors = len(re.findall(r"(?m)^! ", log_text)) + len(
                        re.findall(r"(?m)^.*?:\d+: LaTeX Error:", log_text)
                    )
                    warnings = len(re.findall(r"(?i)warning", log_text))
                    completed_at = self._iso_now()
                    result = {
                        "request_id": request_id,
                        "project": name,
                        "project_key": cfg["project_key"],
                        "version": cfg["version"],
                        "version_no": cfg["version_no"],
                        "snapshot_commit": commit,
                        "status": "success",
                        "elapsed_seconds": round(time.time() - started, 1),
                        "broker_elapsed_seconds": broker_result.get("elapsed_seconds"),
                        "pdf_path": str(cache / "latest.pdf"),
                        "log_path": str(cache / "latest.log"),
                        "build_json_path": str(cache / "build.json"),
                        "pdf_bytes": pdf_size,
                        "log_bytes": log_size,
                        "tex_errors": tex_errors,
                        "warning_mentions": warnings,
                        "completed_at": completed_at,
                    }
                    build_tmp = cache / "build.json.tmp"
                    build_tmp.write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    os.replace(build_tmp, cache / "build.json")
                    self._update_request(
                        request_id,
                        status="success",
                        completed_at=completed_at,
                        result=result,
                    )
        except Exception as exc:
            self._update_request(
                request_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                completed_at=self._iso_now(),
            )

    @staticmethod
    def _public_request(record: dict) -> dict:
        public = dict(record)
        public.pop("version_no", None)
        result = public.get("result")
        if isinstance(result, dict):
            result = dict(result)
            result.pop("project_key", None)
            result.pop("version_no", None)
            public["result"] = result
        return public

    def request_status(self, req: dict) -> dict:
        request_id = str(req["request_id"])
        requested_project = str(req.get("project") or "")
        record = self.request_jobs.get(request_id)
        if record is None:
            path = self._request_path(request_id)
            if not path.exists():
                raise RuntimeError(f"Unknown TeXPage request: {request_id}")
            record = json.loads(path.read_text(encoding="utf-8"))
            self.request_jobs[request_id] = record
        if requested_project and str(record.get("project")) != requested_project:
            raise RuntimeError("Request does not belong to the requested project alias")
        return {"ok": True, "request": self._public_request(record)}

    def list_requests(self, req: dict) -> dict:
        project = str(req.get("project") or "")
        limit = max(1, min(int(req.get("limit", 20)), 100))
        records: list[dict] = []
        REQUEST_DIR.mkdir(parents=True, exist_ok=True)
        paths = sorted(
            REQUEST_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for path in paths:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if project and str(record.get("project")) != project:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return {"ok": True, "requests": [self._public_request(x) for x in records]}

    async def acquire_worker(self, project: str, action: str) -> tuple[dict, dict]:
        self.ui_sequence += 1
        ticket = {
            "id": self.ui_sequence,
            "project": project,
            "action": action,
            "queued_at": time.time(),
        }
        self.ui_waiting.append(ticket)
        try:
            worker = await self.available_workers.get()
        except BaseException:
            if ticket in self.ui_waiting:
                self.ui_waiting.remove(ticket)
            raise
        if ticket in self.ui_waiting:
            self.ui_waiting.remove(ticket)
        ticket["started_at"] = time.time()
        ticket["wait_seconds"] = round(ticket["started_at"] - ticket["queued_at"], 1)
        ticket["worker_id"] = worker["id"]
        self.ui_active[worker["id"]] = ticket
        return worker, ticket

    async def release_worker(self, worker: dict, ticket: dict) -> None:
        self.ui_active.pop(worker["id"], None)
        await self.available_workers.put(worker)

    async def create_top_level_page(self, control_page=None):
        """Create a real Chromium window, not another tab in an existing window."""
        if control_page is None or control_page.is_closed():
            pages = [p for p in self.context.pages if not p.is_closed()]
            if not pages:
                return await self.context.new_page()
            control_page = pages[0]
        before = list(self.context.pages)
        session = await self.context.new_cdp_session(control_page)
        result = await session.send(
            "Target.createTarget",
            {"url": "about:blank", "newWindow": True, "background": True},
        )
        target_id = result.get("targetId")
        deadline = time.time() + 5
        page = None
        while time.time() < deadline:
            for candidate in self.context.pages:
                if candidate not in before and not candidate.is_closed():
                    page = candidate
                    break
            if page is not None:
                break
            await asyncio.sleep(0.05)
        if page is None:
            raise RuntimeError("Chromium created a worker target but no page appeared")
        # Park worker windows far off-screen. They stay genuinely headful for
        # NJU's security layer, but should not steal the user's foreground.
        try:
            worker_session = await self.context.new_cdp_session(page)
            window = await worker_session.send(
                "Browser.getWindowForTarget", {"targetId": target_id}
            )
            await worker_session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window["windowId"],
                    "bounds": {
                        "left": -32000,
                        "top": -32000,
                        "width": 1280,
                        "height": 820,
                    },
                },
            )
        except Exception:
            pass
        if self.browser_pid:
            try:
                await asyncio.to_thread(hide_windows_from_taskbar, self.browser_pid)
            except Exception:
                pass
        return page

    async def start(self) -> None:
        self.profile.mkdir(parents=True, exist_ok=True)
        self._recover_request_records()
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.profile),
            headless=False,
            viewport={"width": 1280, "height": 820},
            # NJU's security layer rejects true headless Chromium.  Keep a
            # genuine headful process, but park its single window off-screen so
            # automation cannot keep stealing the user's foreground desktop.
            args=[
                "--start-minimized",
                "--window-position=-32000,-32000",
                "--window-size=1280,820",
            ],
        )
        try:
            browser = self.context.browser
            if browser is not None:
                browser_session = await browser.new_browser_cdp_session()
                process_info = await browser_session.send("SystemInfo.getProcessInfo")
                browser_item = next(
                    (item for item in process_info.get("processInfo", []) if item.get("type") == "browser"),
                    None,
                )
                if browser_item is not None:
                    self.browser_pid = int(browser_item.get("id") or 0)
        except Exception:
            self.browser_pid = 0
        pages = list(self.context.pages)
        base = pages[0] if pages else await self.context.new_page()
        for extra in pages[1:]:
            try:
                await extra.close()
            except Exception:
                pass
        self.workers = [{"id": 1, "page": base}]
        for worker_id in range(2, self.worker_count + 1):
            page = await self.create_top_level_page(base)
            self.workers.append({"id": worker_id, "page": page})
        if self.browser_pid:
            try:
                await asyncio.to_thread(hide_windows_from_taskbar, self.browser_pid)
            except Exception:
                pass
        for worker in self.workers:
            await self.available_workers.put(worker)

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()
        if self.playwright is not None:
            await self.playwright.stop()

    async def ensure_worker_page(self, worker: dict):
        page = worker.get("page")
        if page is None or page.is_closed():
            page = await self.create_top_level_page()
            worker["page"] = page
        return page

    async def park_worker_page(self, worker: dict) -> None:
        """Detach editor/socket state before returning a window to the pool."""
        page = worker.get("page")
        if page is None or page.is_closed():
            return
        try:
            await page.goto("about:blank", wait_until="commit", timeout=5000)
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
            worker["page"] = None

    async def compile(self, req: dict) -> dict:
        name = str(req["project"])
        lock = self.project_locks.setdefault(name, asyncio.Lock())
        async with lock:
            project_key = str(req["project_key"])
            version = str(req["version"])
            version_no = str(req["version_no"])
            timeout_s = int(req.get("timeout_s", 240))
            url = f"https://tex.nju.edu.cn/project/user/{project_key}/{version_no}"
            events: list[tuple[float, str, str]] = []
            started_at = time.time()

            worker, ticket = await self.acquire_worker(name, "compile")
            page = await self.ensure_worker_page(worker)
            self.active[name] = {
                "version": version,
                "version_no": version_no,
                "started_at": started_at,
                "ui_started_at": ticket["started_at"],
                "queue_wait_seconds": ticket["wait_seconds"],
                "worker_id": worker["id"],
                "url": url,
            }

            def on_response(resp):
                m = RESULT_RE.search(resp.url)
                if m:
                    events.append((time.time(), m.group(1).lower(), resp.url))

            page.on("response", on_response)
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response is None or response.status >= 400:
                    raise RuntimeError(
                        f"TeXPage project page failed to load: {getattr(response, 'status', None)}"
                    )
                # A newly-created version is not guaranteed to render its
                # version label as visible page text.  Older code treated
                # ``version not in body`` as a login failure, which produced
                # false positives on otherwise healthy project pages (the
                # exact project/version URL, file tree and compile controls
                # were all available).  Validate the authenticated editor by
                # its stable routing + UI instead.
                editor_deadline = time.time() + 10
                compile_visible = False
                while time.time() < editor_deadline:
                    compile_visible = await visible_text_exists(page, "编译")
                    if compile_visible:
                        break
                    await page.wait_for_timeout(250)
                title = await page.title()
                current_url = page.url
                exact_editor_route = (
                    project_key in current_url and version_no in current_url
                )
                if not exact_editor_route or "TeXPage" not in title or not compile_visible:
                    raise RuntimeError(
                        "TeXPage login/session is unavailable or NJU security verification was triggered. "
                        "Stop the broker, open the saved browser profile once and log in, then retry."
                    )

                # A Git push may already have triggered compilation. Join it if so;
                # otherwise explicitly trigger a build on this project's own tab.
                if await visible_text_exists(page, "停止"):
                    events.clear()
                else:
                    events.clear()
                    await click_exact_text(page, "编译")
                    deadline = time.time() + 15
                    while time.time() < deadline and not await visible_text_exists(page, "停止"):
                        await page.wait_for_timeout(250)

                deadline = time.time() + timeout_s
                while time.time() < deadline:
                    if await visible_text_exists(page, "编译") and not await visible_text_exists(page, "停止"):
                        break
                    await page.wait_for_timeout(500)
                else:
                    raise RuntimeError(f"TeXPage compile timed out after {timeout_s}s")

                # Do not trust artifact URLs observed while compilation is still
                # transitioning.  TeXPage can briefly serve the previous PDF or
                # finish the compile without re-requesting the result URLs, which
                # caused both stale downloads and false "URLs not observed"
                # failures.  Once the compile control has returned to idle, clear
                # all earlier events and reload the exact version route.  The
                # finalized editor reload requests the current output.pdf/log;
                # retry the reload a few times to tolerate object-store propagation.
                pdf_url = None
                log_url = None
                refresh_errors: list[str] = []
                artifact_recovery_deadline = time.time() + 120
                refresh_attempt = 0
                while time.time() < artifact_recovery_deadline:
                    refresh_attempt += 1
                    events.clear()
                    try:
                        refreshed = await page.reload(
                            wait_until="domcontentloaded", timeout=30000
                        )
                        if refreshed is None or refreshed.status >= 400:
                            raise RuntimeError(
                                f"TeXPage artifact refresh failed: {getattr(refreshed, 'status', None)}"
                            )
                        observation_deadline = min(
                            time.time() + 6, artifact_recovery_deadline
                        )
                        while time.time() < observation_deadline:
                            pdf_url = newest_signed_url(events, "pdf")
                            log_url = newest_signed_url(events, "log")
                            if pdf_url and log_url:
                                break
                            await page.wait_for_timeout(250)

                        if not log_url:
                            try:
                                await click_exact_text(page, "日志")
                                await page.wait_for_timeout(1000)
                            except Exception:
                                pass
                            log_url = newest_signed_url(events, "log")
                        pdf_url = newest_signed_url(events, "pdf")
                        if pdf_url and log_url:
                            break
                    except Exception as exc:
                        refresh_errors.append(f"{type(exc).__name__}: {exc}")

                    # TeXPage can report the compiler idle well before the
                    # finalized PDF/log objects become visible.  Production
                    # observations have shown delays around one minute.  Re-enter
                    # the exact version route at a modest cadence rather than
                    # spinning or allocating another formal version.
                    remaining = artifact_recovery_deadline - time.time()
                    if remaining > 0:
                        await page.wait_for_timeout(int(min(8.0, remaining) * 1000))

                if not pdf_url or not log_url:
                    detail = "; ".join(refresh_errors[-2:])
                    suffix = f"; refresh errors: {detail}" if detail else ""
                    raise RuntimeError(
                        "Compilation finished, but fresh signed PDF/log result URLs were not observed"
                        + suffix
                    )

                return {
                    "ok": True,
                    "project": name,
                    "elapsed_seconds": round(time.time() - started_at, 1),
                    "pdf_url": pdf_url,
                    "log_url": log_url,
                }
            finally:
                self.active.pop(name, None)
                try:
                    try:
                        page.remove_listener("response", on_response)
                    except Exception:
                        pass
                    await self.park_worker_page(worker)
                finally:
                    await self.release_worker(worker, ticket)

    async def versions(self, req: dict) -> dict:
        project_key = str(req["project_key"])
        url = f"https://tex.nju.edu.cn/api/project/version?projectKey={project_key}"
        response = await self.context.request.get(url, timeout=15000)
        if not response.ok:
            raise RuntimeError(f"TeXPage version API failed: HTTP {response.status}")
        data = await response.json()
        if data.get("status", {}).get("code") != 1:
            raise RuntimeError(f"TeXPage version API returned: {data.get('status')}")
        versions = []
        for item in data.get("result", []):
            versions.append(
                {
                    "version": item.get("versionName"),
                    "version_no": item.get("versionNo"),
                    "is_default": bool(item.get("isDefault")),
                    "from_version": item.get("fromVersionName"),
                    "created_at": item.get("createAt"),
                }
            )
        return {"ok": True, "project_key": project_key, "versions": versions}

    async def reserve_next_version(self, req: dict) -> dict:
        """Atomically choose and create the next vMAJOR.MINOR for one project."""
        name = str(req["project"])
        project_key = str(req["project_key"])
        lock = self.project_locks.setdefault(name, asyncio.Lock())
        async with lock:
            url = f"https://tex.nju.edu.cn/api/project/version?projectKey={project_key}"
            response = await self.context.request.get(url, timeout=15000)
            if not response.ok:
                raise RuntimeError(f"TeXPage version API failed: HTTP {response.status}")
            data = await response.json()
            if data.get("status", {}).get("code") != 1:
                raise RuntimeError(f"TeXPage version API returned: {data.get('status')}")
            items = list(data.get("result", []))

            parsed: list[tuple[int, int, dict]] = []
            for item in items:
                m = re.fullmatch(r"v(\d+)\.(\d+)", str(item.get("versionName", "")))
                if m:
                    parsed.append((int(m.group(1)), int(m.group(2)), item))

            if parsed:
                major, minor, source = max(parsed, key=lambda x: (x[0], x[1]))
                new_version = f"v{major}.{minor + 1}"
            else:
                source = next((x for x in items if x.get("isDefault")), None)
                if source is None and items:
                    source = items[-1]
                if source is None:
                    raise RuntimeError("No source TeXPage version exists")
                new_version = "v1.0"

            existing_names = {str(x.get("versionName")) for x in items}
            while new_version in existing_names:
                m = re.fullmatch(r"v(\d+)\.(\d+)", new_version)
                if not m:
                    raise RuntimeError(f"Could not advance version name: {new_version}")
                new_version = f"v{int(m.group(1))}.{int(m.group(2)) + 1}"

            if req.get("dry_run"):
                return {
                    "ok": True,
                    "project": name,
                    "dry_run": True,
                    "version": new_version,
                    "version_no": None,
                    "from_version": source.get("versionName"),
                    "from_version_no": source.get("versionNo"),
                }

            create = await self.context.request.post(
                f"https://tex.nju.edu.cn/api/project/version?t={int(time.time() * 1000)}",
                data={
                    "newVersionName": new_version,
                    "projectKey": project_key,
                    "versionNo": source.get("versionNo"),
                },
                timeout=30000,
            )
            created_data = await create.json()
            status = created_data.get("status", {})
            if not create.ok or status.get("code") != 1:
                raise RuntimeError(
                    f"TeXPage create version failed: {status.get('code')} {status.get('message')}"
                )

            confirm = await self.context.request.get(url, timeout=15000)
            confirm_data = await confirm.json()
            created = next(
                (
                    x
                    for x in confirm_data.get("result", [])
                    if x.get("versionName") == new_version
                ),
                None,
            )
            if not created:
                raise RuntimeError(
                    f"TeXPage reported success but {new_version} was not visible afterward"
                )
            return {
                "ok": True,
                "project": name,
                "version": created.get("versionName"),
                "version_no": created.get("versionNo"),
                "from_version": created.get("fromVersionName"),
                "from_version_no": created.get("fromVersionNo"),
                "created_at": created.get("createAt"),
            }

    async def delete_version(self, req: dict) -> dict:
        """Delete exactly one non-default version after verifying its name and UUID."""
        name = str(req.get("project") or req["project_key"])
        project_key = str(req["project_key"])
        version = str(req["version"])
        version_no = str(req["version_no"])
        lock = self.project_locks.setdefault(name, asyncio.Lock())
        async with lock:
            live = await self.versions({"project_key": project_key})
            match = next(
                (
                    item
                    for item in live.get("versions", [])
                    if item.get("version") == version
                    and item.get("version_no") == version_no
                ),
                None,
            )
            if not match:
                raise RuntimeError(
                    f"Refusing delete: exact version pair not found: {version}/{version_no}"
                )
            if match.get("is_default"):
                raise RuntimeError("Refusing to delete the default TeXPage version")
            response = await self.context.request.delete(
                "https://tex.nju.edu.cn/api/project/version",
                params={"projectKey": project_key, "versionNo": version_no},
                timeout=20000,
            )
            data = await response.json()
            if not response.ok or data.get("status", {}).get("code") != 1:
                raise RuntimeError(f"TeXPage delete version failed: {data.get('status')}")
            confirm = await self.versions({"project_key": project_key})
            if any(
                item.get("version") == version or item.get("version_no") == version_no
                for item in confirm.get("versions", [])
            ):
                raise RuntimeError("TeXPage version still visible after delete")
            return {
                "ok": True,
                "project": name,
                "version": version,
                "version_no": version_no,
                "deleted": True,
            }

    async def upload_version(self, req: dict) -> dict:
        project_key = str(req["project_key"])
        from_version_no = str(req["from_version_no"])
        from_version = str(req["from_version"])
        version = str(req["version"])
        zip_path = Path(str(req["zip_path"])).resolve()
        if not zip_path.is_file():
            raise RuntimeError(f"Upload ZIP not found: {zip_path}")

        before = await self.versions({"project_key": project_key})
        if any(item.get("version") == version for item in before.get("versions", [])):
            raise RuntimeError(f"Refusing to overwrite existing TeXPage version: {version}")

        url = f"https://tex.nju.edu.cn/project/user/{project_key}/{from_version_no}"
        name = str(req.get("project") or project_key)
        worker, ticket = await self.acquire_worker(name, "upload_version")
        page = await self.ensure_worker_page(worker)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response is None or response.status >= 400:
                raise RuntimeError(
                    f"TeXPage project page failed to load: {getattr(response, 'status', None)}"
                )
            await page.wait_for_timeout(1800)
            body = await page.text_content("body") or ""
            if "TeXPage" not in await page.title():
                raise RuntimeError("TeXPage login/session is unavailable")

            # Open the current version selector and choose New Version.
            await click_exact_text(page, from_version)
            await page.wait_for_timeout(300)
            await click_exact_text(page, "新建版本")
            await page.wait_for_timeout(350)

            upload_tabs = page.get_by_text("上传版本", exact=True)
            tab_clicked = False
            for i in range(min(await upload_tabs.count(), 6)):
                item = upload_tabs.nth(i)
                if await item.is_visible():
                    await item.click(timeout=5000)
                    tab_clicked = True
                    break
            if not tab_clicked:
                raise RuntimeError("Could not open Upload Version tab")
            await page.wait_for_timeout(350)

            text_inputs = page.locator('input[type="text"]')
            name_input = None
            for i in range(await text_inputs.count()):
                item = text_inputs.nth(i)
                if await item.is_visible():
                    name_input = item
            if name_input is None:
                raise RuntimeError("Visible version-name input not found")
            await name_input.fill(version)

            file_inputs = page.locator('input[type="file"]')
            file_input = None
            for i in range(await file_inputs.count()):
                item = file_inputs.nth(i)
                active = await item.evaluate(
                    "e => { const p=e.closest('.ant-tabs-tabpane'); "
                    "return !p || p.classList.contains('ant-tabs-tabpane-active'); }"
                )
                if active:
                    file_input = item
            if file_input is None:
                raise RuntimeError("Upload ZIP input not found")
            await file_input.set_input_files(str(zip_path))

            # Selecting the file only starts TeXPage's asynchronous object-storage
            # upload. The form is valid only after its custom uploader's onSuccess
            # callback stores uploadFileKey; at that point the UI replaces the
            # progress text with the original ZIP filename. Waiting a fixed 700 ms
            # races this upload for normal multi-megabyte projects.
            upload_deadline = time.time() + 120
            while time.time() < upload_deadline:
                if await visible_text_exists(page, zip_path.name):
                    break
                await page.wait_for_timeout(500)
            else:
                body = await page.text_content("body") or ""
                raise RuntimeError(
                    "TeXPage ZIP upload did not finish within 120s; "
                    f"filename never became visible: {zip_path.name}; "
                    f"page_tail={body[-500:]}"
                )

            submits = page.locator('button[type="submit"]')
            submit = None
            for i in range(await submits.count()):
                item = submits.nth(i)
                if await item.is_visible() and (await item.inner_text()).strip() == "上传版本":
                    submit = item
            if submit is None:
                raise RuntimeError("Upload Version submit button not found")
            await submit.click(timeout=15000)

            deadline = time.time() + 120
            while time.time() < deadline:
                await page.wait_for_timeout(800)
                live = await self.versions({"project_key": project_key})
                matches = [x for x in live.get("versions", []) if x.get("version") == version]
                if matches:
                    item = matches[0]
                    return {
                        "ok": True,
                        "project_key": project_key,
                        "version": item.get("version"),
                        "version_no": item.get("version_no"),
                        "created_at": item.get("created_at"),
                        "zip_path": str(zip_path),
                    }
            raise RuntimeError(f"Uploaded version did not appear in TeXPage API: {version}")
        finally:
            try:
                await self.park_worker_page(worker)
            finally:
                await self.release_worker(worker, ticket)

    async def project_scripts(self, req: dict) -> dict:
        project_key = str(req["project_key"])
        version_no = str(req["version_no"])
        name = str(req.get("project") or project_key)
        worker, ticket = await self.acquire_worker(name, "project_scripts")
        page = await self.ensure_worker_page(worker)
        try:
            url = f"https://tex.nju.edu.cn/project/user/{project_key}/{version_no}"
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response is None or response.status >= 400:
                raise RuntimeError(f"Project page failed: HTTP {getattr(response, 'status', None)}")
            await page.wait_for_timeout(1500)
            scripts = await page.locator("script[src]").evaluate_all(
                "els => els.map(e => e.src).filter(Boolean)"
            )
            return {"ok": True, "scripts": sorted(set(scripts))}
        finally:
            try:
                await self.park_worker_page(worker)
            finally:
                await self.release_worker(worker, ticket)

    async def project_git_info(self, req: dict) -> dict:
        project_key = str(req["project_key"])
        response = await self.context.request.get(
            "https://tex.nju.edu.cn/api/project/git",
            params={"projectKey": project_key},
            timeout=15000,
        )
        if not response.ok:
            raise RuntimeError(f"TeXPage project Git API failed: HTTP {response.status}")
        data = await response.json()

        def scrub(value):
            if isinstance(value, dict):
                out = {}
                for key, item in value.items():
                    lowered = str(key).lower()
                    if any(word in lowered for word in ("token", "password", "secret", "credential")):
                        out[key] = "<redacted>" if item not in (None, "", [], {}) else item
                    else:
                        out[key] = scrub(item)
                return out
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value

        return {"ok": True, "project_key": project_key, "response": scrub(data)}

    async def ensure_project_git(self, req: dict) -> dict:
        project_key = str(req["project_key"])
        current = await self.context.request.get(
            "https://tex.nju.edu.cn/api/project/git",
            params={"projectKey": project_key},
            timeout=15000,
        )
        if not current.ok:
            raise RuntimeError(f"TeXPage project Git API failed: HTTP {current.status}")
        current_data = await current.json()
        if current_data.get("status", {}).get("code") != 1:
            raise RuntimeError(f"TeXPage project Git API returned: {current_data.get('status')}")
        created = False
        if not current_data.get("result"):
            # Initializing Git for older TeXPage projects can take substantially
            # longer than ordinary API calls.  A 20 s timeout can leave legacy
            # projects permanently unable to enter the bridge's Git workflow.
            response = await self.context.request.post(
                "https://tex.nju.edu.cn/api/project/git",
                data={"projectKey": project_key},
                timeout=90000,
            )
            data = await response.json()
            if not response.ok or data.get("status", {}).get("code") != 1:
                raise RuntimeError(f"TeXPage create Git repository failed: {data.get('status')}")
            created = True

        confirmed = await self.context.request.get(
            "https://tex.nju.edu.cn/api/project/git",
            params={"projectKey": project_key},
            timeout=15000,
        )
        confirmed_data = await confirmed.json()
        result = confirmed_data.get("result")
        if not confirmed.ok or confirmed_data.get("status", {}).get("code") != 1 or not result:
            raise RuntimeError("TeXPage Git repository was not visible after initialization")

        def scrub(value):
            if isinstance(value, dict):
                out = {}
                for key, item in value.items():
                    lowered = str(key).lower()
                    if any(word in lowered for word in ("token", "password", "secret", "credential")):
                        out[key] = "<redacted>" if item not in (None, "", [], {}) else item
                    else:
                        out[key] = scrub(item)
                return out
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value

        return {
            "ok": True,
            "project_key": project_key,
            "created": created,
            "git": scrub(result),
        }

    def status(self) -> dict:
        now = time.time()
        queued = []
        for item in self.ui_waiting:
            queued.append(
                {
                    "id": item["id"],
                    "project": item["project"],
                    "action": item["action"],
                    "queued_seconds": round(now - item["queued_at"], 1),
                }
            )
        ui_active = []
        for worker_id, item in sorted(self.ui_active.items()):
            ui_active.append(
                {
                    "worker_id": worker_id,
                    "id": item["id"],
                    "project": item["project"],
                    "action": item["action"],
                    "running_seconds": round(now - item["started_at"], 1),
                    "wait_seconds": item["wait_seconds"],
                }
            )
        request_counts: dict[str, int] = {}
        for record in self.request_jobs.values():
            state = str(record.get("status") or "unknown")
            request_counts[state] = request_counts.get(state, 0) + 1
        return {
            "ok": True,
            "pid": os.getpid(),
            "uptime_seconds": round(now - self.started_at, 1),
            "ui_mode": "fifo_top_level_window_pool",
            "worker_capacity": self.worker_count,
            "workers_busy": len(self.ui_active),
            "workers_free": max(0, self.worker_count - len(self.ui_active)),
            "ui_active": ui_active,
            "ui_queue": queued,
            "ui_queue_depth": len(queued),
            "active": self.active,
            "browser_pages": len(self.context.pages) if self.context is not None else 0,
            "request_counts": request_counts,
        }


async def serve(host: str, port: int, profile: Path, workers: int) -> None:
    broker = BrowserBroker(profile, worker_count=workers)
    await broker.start()
    stop_event = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            req = json.loads(raw.decode("utf-8"))
            action = req.get("action")
            if action == "ping":
                result = broker.status()
            elif action == "status":
                result = broker.status()
            elif action == "versions":
                result = await broker.versions(req)
            elif action == "reserve_next_version":
                result = await broker.reserve_next_version(req)
            elif action == "delete_version":
                result = await broker.delete_version(req)
            elif action == "upload_version":
                result = await broker.upload_version(req)
            elif action == "project_scripts":
                result = await broker.project_scripts(req)
            elif action == "project_git_info":
                result = await broker.project_git_info(req)
            elif action == "ensure_project_git":
                result = await broker.ensure_project_git(req)
            elif action == "git_auth_ensure":
                result = await broker.git_auth_ensure(req)
            elif action == "submit_build":
                result = {"ok": True, "request": await broker.submit_build(req)}
            elif action == "request_status":
                result = broker.request_status(req)
            elif action == "list_requests":
                result = broker.list_requests(req)
            elif action == "compile":
                result = await broker.compile(req)
            elif action == "shutdown":
                result = {"ok": True, "message": "shutting down"}
                stop_event.set()
            else:
                result = {"ok": False, "error": f"Unknown action: {action}"}
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionError, OSError):
            # A short-lived CLI probe may disconnect after receiving enough data.
            # Never let that tear down the long-lived broker.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(handle, host, port)
    except OSError as exc:
        # Another broker won a simultaneous startup race. Exit quietly.
        await broker.close()
        if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in {48, 98, 10048}:
            return
        raise

    async with server:
        await stop_event.wait()
        server.close()
        await server.wait_closed()
    await broker.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Queued multi-window broker for NJU TeXPage")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE))
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    try:
        asyncio.run(serve(args.host, args.port, Path(args.profile), args.workers))
        return 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
