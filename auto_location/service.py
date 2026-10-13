"""Local dashboard and persistent daily batch scheduler."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import select
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

from .accounts import load_accounts, add_account, update_account, account_by_id, remove_account
from .checkin import CheckinError
from .browser_passwords import BrowserPasswords
from .vault import CredentialVault, remove_browser_logins
from .scheduler import DailyScheduler


class JobController:
    def __init__(self, accounts: Path, simulate_miniprogram=True, monitor_available=None, monitor_port=6080, vault=None):
        self.accounts = accounts.resolve()
        self.output = self.accounts.parent / "batch-result.json"
        self.log_path = self.accounts.parent / "service-job.log"
        self.simulate = simulate_miniprogram
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.process = None
        self.log = None
        self.kind = None
        self.account_id = None
        self.monitored = False
        self.finishing = False
        self.monitor_available = (os.environ.get("AUTO_LOCATION_GUI") == "1" and bool(os.environ.get("DISPLAY"))) if monitor_available is None else monitor_available
        self.job_output = self.output
        self.monitor_port = monitor_port
        self.setup_lock = None
        self.auth_state = self.accounts.parent / "web-auth-state.json"
        self.vault = vault or CredentialVault(self.accounts.parent / "credentials")
        self.scheduler = DailyScheduler(self.accounts.parent / "schedule.json", self)

    def save_credentials(self, value):
        if set(value) != {"id", "username", "password"}:
            raise ValueError("凭据请求格式无效。")
        with self.lock:
            self._idle()
            account = account_by_id(self.accounts, value["id"])
            self.vault.save(account["id"], value["username"], value["password"])
            remove_browser_logins(account["profile"])

    def _release_setup_lock(self):
        if self.setup_lock is not None:
            self.setup_lock.close()
            self.setup_lock = None
            self.accounts.with_suffix(".batch.lock").unlink(missing_ok=True)

    def _poll(self):
        code = self.process.poll() if self.process is not None else None
        running = self.process is not None and code is None
        if not running and self.log is not None:
            self.log.close()
            self.log = None
        if not running and self.process is not None and getattr(self.process, "stdin", None) is not None:
            self.process.stdin.close()
        if not running:
            self._release_setup_lock()
        return running, code

    def _idle(self):
        if self._poll()[0]:
            raise RuntimeError("已有任务运行，请等待完成。")
        if self.accounts.with_suffix(".batch.lock").exists():
            raise RuntimeError("存在批量任务锁，请先确认其他任务或上次异常退出的状态。")

    def configure(self, value):
        with self.lock:
            self._idle()
            if set(value) == {"id"}:
                add_account(self.accounts, value["id"])
            elif set(value) == {"id", "enabled", "auto_login", "area_index"}:
                update_account(self.accounts, value["id"], value["enabled"], value["auto_login"], value["area_index"])
            else:
                raise ValueError("账号配置字段无效。")

    def delete_account(self, value):
        if set(value) != {'id'}:
            raise ValueError('删除请求仅允许指定账号别名。')
        with self.lock:
            self._idle()
            account = account_by_id(self.accounts, value['id'])
            lock_path = self.accounts.with_suffix('.batch.lock')
            try:
                batch_lock = lock_path.open('x', encoding='utf-8')
            except FileExistsError:
                raise RuntimeError('存在其他浏览器任务，请等待完成。') from None
            staged = []
            trash = None
            committed = False
            try:
                root = self.accounts.parent.resolve()
                profile = root / 'profiles' / account['id']
                # Resolve before moving/deleting. Imported profiles, symlinks
                # and Windows junctions must never lead outside managed data.
                managed = (profile.parent.resolve() == root / 'profiles'
                           and profile.resolve() == profile.absolute()
                           and account['profile'] == profile.resolve())
                trash = Path(tempfile.mkdtemp(prefix='.delete-', dir=root))
                if trash.resolve().parent != root:
                    raise RuntimeError('删除暂存目录不在账号数据目录内。')
                sources = [self.vault._path(account['id'])]
                if managed:
                    sources.append(profile)
                elif (profile.parent.resolve() == root / 'profiles' and
                      (profile.is_symlink() or getattr(profile, 'is_junction', lambda: False)())):
                    # Remove only the managed directory entry, preserving its
                    # external target and preventing reuse when re-adding ID.
                    sources.append(profile)
                for source in sources:
                    if source.exists() or source.is_symlink():
                        target = trash / str(len(staged))
                        source.rename(target)
                        staged.append((source, target))
                remove_account(self.accounts, account['id'])
                committed = True
                preserved = not managed and account['profile'].exists()
                cleanup_pending = False
                try:
                    shutil.rmtree(trash)
                except OSError:
                    cleanup_pending = True
                trash = None
                return {'status': 'account_deleted', 'profile_preserved': preserved,
                        'cleanup_pending': cleanup_pending}
            except Exception:
                # A move may fail before registry commit; restore every move.
                if not committed:
                    for source, target in reversed(staged):
                        if target.exists() or target.is_symlink():
                            target.rename(source)
                raise
            finally:
                try:
                    if trash is not None and trash.exists() and not any(trash.iterdir()):
                        trash.rmdir()
                finally:
                    batch_lock.close()
                    lock_path.unlink(missing_ok=True)

    def _launch(self, command, output, kind, monitored=False, account_id=None, interactive=False):
        env = dict(os.environ)
        for key in ("UESTC_USERNAME", "UESTC_PASSWORD"):
            env.pop(key, None)
        env["AUTO_LOCATION_VAULT_KEY_FILE"] = str(self.vault.key_path.resolve())
        self.accounts.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("w", encoding="utf-8")
        output.unlink(missing_ok=True)
        output.with_name(output.stem + ".error.json").unlink(missing_ok=True)
        try:
            self.process = subprocess.Popen(command, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                            stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                                            start_new_session=os.name != "nt")
            self.kind, self.monitored, self.account_id = kind, monitored, account_id
            self.job_output, self.finishing = output, False
        except Exception:
            self.log.close()
            self.log = None
            self.process = None
            raise RuntimeError("无法启动浏览器进程，请检查容器日志。") from None

    def start(self, kind, monitor=False):
        if kind not in ("checkin", "inspect", "login"):
            raise ValueError("不支持的批量操作。")
        with self.lock:
            self._idle()
            if type(monitor) is not bool or (monitor and not self.monitor_available):
                raise ValueError("浏览器监视不可用，请使用启用 GUI 的容器。")
            if not any(a["enabled"] for a in load_accounts(self.accounts)):
                raise ValueError("没有启用的账号，请先在容器中初始化账号。")
            command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve().parents[1] / "main.py"),
                       "batch-" + kind, "--accounts", str(self.accounts), "--output", str(self.output)]
            if not monitor:
                command.append("--headless")
            if kind == "checkin" and self.simulate:
                command.append("--simulate-miniprogram")
            self._launch(command, self.output, kind, monitor)

    def start_scheduled(self, claim):
        with self.lock:
            self._idle()
            if not any(a["enabled"] for a in load_accounts(self.accounts)):
                raise ValueError("没有启用的账号。")
            claim()
            self.start("checkin", monitor=False)
            return self.process

    def setup(self, ident, kind, monitor=None):
        if kind != "test-login":
            raise ValueError("安全模式禁止通过网页打开密码管理器或交互式登录。")
        with self.lock:
            self._idle()
            monitor = self.monitor_available if monitor is None else monitor
            if type(monitor) is not bool or (monitor and not self.monitor_available):
                raise ValueError("GUI 登录调试不可用。")
            account = account_by_id(self.accounts, ident)
            output = self.accounts.parent / "setup-result.json"
            command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve().parents[1] / "main.py"),
                       "login", "--accounts", str(self.accounts),
                       "--account-id", account["id"], "--output", str(output)]
            command.append("--auto-login")
            if monitor:
                command.extend(["--web-auth", str(self.auth_state), "--timeout", "600"])
            else:
                command.append("--headless")
            self.auth_state.unlink(missing_ok=True)
            self.accounts.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.setup_lock = self.accounts.with_suffix(".batch.lock").open("x", encoding="utf-8")
            except FileExistsError:
                raise RuntimeError("存在其他浏览器任务，请等待完成。") from None
            try:
                self.setup_lock.write(str(os.getpid()))
                self.setup_lock.flush()
                self._launch(command, output, kind, monitor, account["id"], monitor)
            except Exception:
                self._release_setup_lock()
                raise

    def finish_setup(self):
        self.auth_action({"action": "cancel"})

    def auth_action(self, value):
        action = value.get('action')
        if action not in ('cancel', 'continue', 'control'):
            raise ValueError('验证操作无效。')
        if action == 'control':
            if set(value) not in ({'action', 'id'}, {'action', 'id', 'value'}):
                raise ValueError('验证操作格式无效。')
            if not isinstance(value.get('id'), str) or len(value['id']) > 80:
                raise ValueError('验证控件无效。')
            if 'value' in value and not (type(value['value']) is bool or
                    isinstance(value['value'], str) and 0 < len(value['value']) <= 128):
                raise ValueError('验证码格式无效。')
        elif set(value) != {'action'}:
            raise ValueError('验证操作格式无效。')
        with self.lock:
            if not self._poll()[0] or self.kind != 'test-login' or not self.monitored:
                raise RuntimeError('当前没有 GUI 登录验证任务。')
            try:
                self.process.stdin.write((json.dumps(value) + '\n').encode())
                self.process.stdin.flush()
            except (OSError, ValueError):
                raise RuntimeError('登录验证任务已结束。') from None

    def status(self):
        with self.lock:
            running, code = self._poll()
            try:
                accounts = []
                for a in load_accounts(self.accounts):
                    try:
                        count = int(self.vault.configured(a["id"]))
                        legacy = bool(BrowserPasswords(a["profile"]).saved_count())
                        credential_error = None
                    except Exception:
                        count, legacy, credential_error = None, False, "无法读取凭据状态"
                    accounts.append({k: a[k] for k in ("id", "enabled", "auto_login", "area_index")}
                                    | {"saved_credentials": count, "legacy_credentials": legacy,
                                       "credential_error": credential_error})
                account_error = None
            except Exception:
                accounts, account_error = [], "账号清单格式无效。"
            report = None
            verification = None
            if running and self.kind == 'test-login' and self.monitored:
                try:
                    verification = json.loads(self.auth_state.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    pass
            for path in (self.job_output, self.job_output.with_name(self.job_output.stem + ".error.json")):
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                    break
                except (FileNotFoundError, ValueError):
                    continue  # The CLI may currently be writing its checkpoint.
            return {"running": running, "exit_code": code, "operation": self.kind,
                    "account_id": self.account_id, "monitored": self.monitored,
                    "monitor_available": self.monitor_available,
                    "verification": verification,
                    "awaiting_finish": False,
                    "accounts": accounts, "account_error": account_error, "report": report}

    def close(self):
        self.scheduler.close()
        with self.lock:
            if self._poll()[0]:
                # Linux batch and account children share this process group.
                if os.name != "nt":
                    os.killpg(self.process.pid, signal.SIGTERM)
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(self.process.pid, signal.SIGKILL)
                    else:
                        self.process.kill()
                    self.process.wait(timeout=5)
            self._release_setup_lock()
            if self.log is not None:
                self.log.close()
                self.log = None


def handler_for(controller):
    class Handler(BaseHTTPRequestHandler):
        def monitor_authorized(self):
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                value = cookie.get("monitor_session")
                return bool(value) and secrets.compare_digest(value.value, controller.token)
            except Exception:
                return False

        def proxy_monitor(self):
            if not controller.monitor_available:
                self.reply(503, {"error": "浏览器监视未启用。"})
                return
            if not self.monitor_authorized():
                self.reply(403, {"error": "请先打开服务首页。"})
                return
            target = self.path.removeprefix("/monitor")
            # Fixed loopback backend: never accept a user-supplied target.
            if self.headers.get("Upgrade", "").lower() == "websocket":
                origin = urlsplit(self.headers.get("Origin", ""))
                if urlsplit(target).path != "/websockify" or origin.netloc != self.headers.get("Host"):
                    self.reply(403, {"error": "监视连接来源无效。"})
                    return
                try:
                    with socket.create_connection(("127.0.0.1", controller.monitor_port), timeout=5) as upstream:
                        request = ["GET /websockify HTTP/1.1", "Host: 127.0.0.1:6080", "Connection: Upgrade", "Upgrade: websocket"]
                        for name in ("Sec-WebSocket-Key", "Sec-WebSocket-Version", "Sec-WebSocket-Protocol"):
                            if self.headers.get(name):
                                request.append(f"{name}: {self.headers[name]}")
                        upstream.sendall(("\r\n".join(request) + "\r\n\r\n").encode("latin-1"))
                        self.wfile.flush()
                        self.close_connection = True
                        self.connection.settimeout(10)
                        upstream.settimeout(10)
                        while True:
                            ready, _, _ = select.select([upstream, self.connection], [], [], 30)
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                (self.connection if source is upstream else upstream).sendall(data)
                except OSError:
                    self.close_connection = True
                return
            upstream = http.client.HTTPConnection("127.0.0.1", controller.monitor_port, timeout=10)
            try:
                upstream.request("GET", target)
                response = upstream.getresponse()
                body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.getheader("Content-Type", "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "SAMEORIGIN")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.reply(502, {"error": "浏览器监视连接失败。"})
            finally:
                upstream.close()

        def reply(self, code, payload, content_type="application/json; charset=utf-8"):
            body = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if self.path == "/" and code == 200:
                self.send_header("Set-Cookie", f"monitor_session={controller.token}; HttpOnly; SameSite=Strict; Path=/monitor/")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/monitor/"):
                self.proxy_monitor()
            elif self.path == "/healthz":
                self.reply(200, {"status": "ok"})
            elif self.path == "/api/status":
                status = controller.status()
                status["schedule"] = controller.scheduler.snapshot()
                self.reply(200, status)
            elif self.path == "/":
                html = (Path(__file__).with_name("service.html").read_text(encoding="utf-8")
                        .replace("__RUN_TOKEN__", controller.token))
                self.reply(200, html, "text/html; charset=utf-8")
            else:
                self.reply(404, {"error": "页面不存在。"})

        def do_POST(self):
            if self.path not in ("/api/schedule", "/api/run", "/api/accounts", "/api/accounts/delete", "/api/credentials", "/api/setup", "/api/setup/finish", "/api/auth/action"):
                self.reply(404, {"error": "操作不存在。"})
                return
            if not secrets.compare_digest(self.headers.get("X-Run-Token", ""), controller.token):
                self.reply(403, {"error": "请从服务首页触发操作。"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 20000:
                    raise ValueError("请求长度无效。")
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict):
                    raise ValueError("请求格式无效。")
                if self.path == "/api/schedule":
                    controller.scheduler.configure(value)
                    self.reply(200, {"status": "schedule_saved"})
                    return
                elif self.path == "/api/run":
                    if set(value) not in ({"operation"}, {"operation", "monitor"}):
                        raise ValueError("请求格式无效。")
                    controller.start(value["operation"], value.get("monitor", False))
                elif self.path == "/api/accounts":
                    controller.configure(value)
                elif self.path == '/api/accounts/delete':
                    result = controller.delete_account(value)
                    self.reply(200, result)
                    return
                elif self.path == "/api/credentials":
                    controller.save_credentials(value)
                elif self.path == "/api/setup":
                    if set(value) not in ({"id", "operation"}, {"id", "operation", "monitor"}):
                        raise ValueError("请求格式无效。")
                    controller.setup(value["id"], value["operation"], value.get('monitor'))
                elif self.path == '/api/auth/action':
                    controller.auth_action(value)
                else:
                    if value:
                        raise ValueError("请求格式无效。")
                    controller.finish_setup()
                self.reply(200 if self.path == "/api/credentials" else 202,
                           {"status": "credentials_saved" if self.path == "/api/credentials" else "started"})
            except CheckinError as exc:
                self.reply(400, {"error": str(exc)})
            except RuntimeError as exc:
                self.reply(409, {"error": str(exc)})
            except (ValueError, TypeError, KeyError) as exc:
                self.reply(400, {"error": str(exc) if isinstance(exc, ValueError) else "请求格式无效。"})
            except Exception:
                self.reply(400, {"error": "无法读取账号配置，请检查容器内账号清单。"})

        def log_message(self, format, *args):
            # Do not log headers, tokens or request bodies.
            if not self.path.startswith("/monitor/"):
                print("HTTP", self.command, urlsplit(self.path).path, flush=True)
    return Handler


def main():
    parser = argparse.ArgumentParser(description="容器批量打卡控制台")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--accounts", type=Path, default=Path(".state/accounts.json"))
    parser.add_argument("--no-simulate-miniprogram", action="store_true")
    args = parser.parse_args()
    controller = JobController(args.accounts, not args.no_simulate_miniprogram)
    server = ThreadingHTTPServer((args.host, args.port), handler_for(controller))
    def stop(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    try:
        controller.scheduler.start()
        print(f"批量控制台已监听 {args.host}:{args.port}，定时守护已启动。", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        controller.close()


if __name__ == "__main__":
    main()
