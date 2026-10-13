"""Run isolated account processes sequentially in a freshly shuffled order."""
import json
import os
import random
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

from .accounts import load_accounts
from .checkin import CheckinError, SHANGHAI


def run_batch(args, output, writer, runner=None, rng=None):
    accounts = [a for a in load_accounts(args.accounts) if a["enabled"]]
    if not accounts:
        raise CheckinError("没有启用的账号，请先运行 account-add 并初始化登录。")
    (rng if rng is not None else random.SystemRandom()).shuffle(accounts)
    run_id = datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    directory = args.accounts.resolve().parent / "batch-runs" / run_id
    directory.mkdir(parents=True)
    report = {"status": "running", "run_id": run_id, "command": args.command,
              "order": [a["id"] for a in accounts], "results": [],
              "started_at": datetime.now(SHANGHAI).isoformat()}
    lock_path = args.accounts.resolve().with_suffix(".batch.lock")
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError:
        raise CheckinError("已有批量任务锁，停止重复运行。异常退出后请先确认没有任务运行，再清理 .batch.lock。") from None
    child_env = dict(os.environ)
    for key in ("UESTC_USERNAME", "UESTC_PASSWORD"):
        child_env.pop(key, None)
    main_script = Path(__file__).resolve().parents[1] / "main.py"
    command = args.command.removeprefix("batch-")
    runner = runner or subprocess.run
    try:
        with lock:
            lock.write(str(os.getpid()))
            lock.flush()
            writer(output, report)
            for position, account in enumerate(accounts, 1):
                result_path = directory / (account["id"] + ".json")
                entry = {"account_id": account["id"], "status": "running", "output": str(result_path)}
                report["results"].append(entry)
                writer(output, report)
                print(f"[{position}/{len(accounts)}] {account['id']}: {command}", flush=True)
                argv = [sys.executable, "-X", "utf8", str(main_script), command,
                        "--accounts", str(args.accounts.resolve()), "--account-id", account["id"],
                        "--timeout", str(args.timeout), "--output", str(result_path)]
                if account["auto_login"]:
                    argv.append("--auto-login")
                if command in ("inspect", "checkin"):
                    argv.append("--auto-location")
                    if account["area_index"] is not None:
                        argv += ["--area-index", str(account["area_index"])]
                if args.headless:
                    argv.append("--headless")
                if args.simulate_miniprogram:
                    argv.append("--simulate-miniprogram")
                try:
                    if command == "login" and not account["auto_login"]:
                        raise CheckinError("该账号已关闭自动登录；请在网页中开启或使用手动登录。")
                    completed = runner(argv, env=child_env, check=False)
                    if result_path.exists():
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                    else:
                        result = {"status": "failed", "error": "账号进程没有保存结果，状态未知；未自动重试。"}
                    success = completed.returncode == 0 and result.get("status") == {
                        "checkin": "verified", "inspect": "inspected", "login": "session_saved"}[command]
                    entry.update(status="success" if success else "failed", result=result)
                except KeyboardInterrupt:
                    entry.update(status="unknown", error="用户中断，当前账号结果未知；未自动重试。")
                    report["status"] = "cancelled"
                    break
                except Exception as exc:
                    entry.update(status="failed", error=str(exc) if isinstance(exc, CheckinError) else f"账号执行失败：{type(exc).__name__}；未自动重试。")
                writer(output, report)
            report["success_count"] = sum(e["status"] == "success" for e in report["results"])
            report["failed_count"] = sum(e["status"] == "failed" for e in report["results"])
            report["unprocessed"] = report["order"][len(report["results"]):]
            if report["status"] != "cancelled":
                report["status"] = "completed" if not report["failed_count"] else "partial_failure"
            report["finished_at"] = datetime.now(SHANGHAI).isoformat()
            writer(output, report)
    finally:
        lock_path.unlink(missing_ok=True)
    return report
