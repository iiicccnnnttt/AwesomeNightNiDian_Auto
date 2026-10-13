"""Persistent daily scheduler. Claims are saved before starting a subprocess."""
from __future__ import annotations

import copy
import json
import logging
import os
import random
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")
DEFAULT = {"enabled": False, "start": "21:00", "end": "23:00"}


def validate(value):
    if set(value) != set(DEFAULT) or type(value["enabled"]) is not bool:
        raise ValueError("定时配置须包含 enabled、start、end。")
    for key in ("start", "end"):
        if not isinstance(value[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value[key]):
            raise ValueError("时间须为 HH:mm。")
    if value["start"] >= value["end"]:
        raise ValueError("结束时间须晚于开始时间，不支持跨午夜。")
    return dict(value)


class DailyScheduler:
    def __init__(self, path: Path, controller, clock=None, rng=None):
        self.path, self.controller = path, controller
        self.clock = clock or (lambda: datetime.now(ZONE))
        self.rng = rng or random.SystemRandom()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.process = None
        self.active_day = None
        self.error = None
        self.leader = None
        self.data = {"config": dict(DEFAULT), "days": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            validate(self.data["config"])
            if not isinstance(self.data["days"], dict):
                raise ValueError("定时状态无效，须恢复原文件，禁止自动重置。")
        # Uncertain runs are never automatically resubmitted after a restart.
        changed = False
        for day in self.data["days"].values():
            if day["status"] in ("claimed", "running"):
                day["status"] = "interrupted"
                changed = True
        self.recovered = changed

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as file:
            json.dump(self.data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, self.path)
        if os.name != "nt":
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def configure(self, value):
        config = validate(value)
        with self.lock:
            previous = copy.deepcopy(self.data)
            self.data["config"] = config
            # Preserve all attempted days, but resample unclaimed plans.
            if config != previous["config"]:
                self.data["days"] = {k: v for k, v in self.data["days"].items()
                                     if v["status"] not in ("pending", "waiting")}
            try:
                self._save()
            except Exception:
                self.data = previous
                raise
        self.tick()

    def snapshot(self):
        with self.lock:
            now = self.clock().astimezone(ZONE)
            today = self.data["days"].get(now.date().isoformat())
            future = [v for v in self.data["days"].values() if v["status"] in ("pending", "waiting")]
            return {"config": dict(self.data["config"]), "timezone": ZONE.key,
                    "today": copy.deepcopy(today),
                    "next_run": min((v["planned_at"] for v in future), default=None),
                    "error": self.error,
                    "history": copy.deepcopy(list(self.data["days"].values())[-7:])}

    def _plan(self, date):
        key = date.isoformat()
        if key in self.data["days"]:
            return self.data["days"][key]
        config = self.data["config"]
        start = datetime.fromisoformat(key + "T" + config["start"]).replace(tzinfo=ZONE)
        end = datetime.fromisoformat(key + "T" + config["end"]).replace(tzinfo=ZONE)
        planned = start + timedelta(seconds=self.rng.randrange(int((end - start).total_seconds())))
        day = {"date": key, "planned_at": planned.isoformat(), "end_at": end.isoformat(), "status": "pending"}
        self.data["days"][key] = day
        self._save()
        return day

    def tick(self):
        with self.lock:
            if self.recovered:
                self._save()
                self.recovered = False
            now = self.clock().astimezone(ZONE)
            if self.process is not None:
                code = self.process.poll()
                if code is not None:
                    day = self.data["days"][self.active_day]
                    day.update(status="completed" if code == 0 else "failed", exit_code=code,
                               finished_at=now.isoformat())
                    self._save()
                    self.process = None
            for day in self.data["days"].values():
                if day["status"] in ("pending", "waiting") and now >= datetime.fromisoformat(day["end_at"]):
                    day["status"] = "missed"
                    self._save()
            if not self.data["config"]["enabled"]:
                return
            day = self._plan(now.date())
            if now >= datetime.fromisoformat(day["end_at"]):
                if day["status"] == "pending":
                    day["status"] = "missed"
                    self._save()
                self._plan(now.date() + timedelta(days=1))
                return
            if day["status"] not in ("pending", "waiting"):
                self._plan(now.date() + timedelta(days=1))
                return
            if now < datetime.fromisoformat(day["planned_at"]):
                return

            def claim():
                actual = self.clock().astimezone(ZONE)
                if actual >= datetime.fromisoformat(day["end_at"]):
                    raise RuntimeError("定时窗口已结束。")
                day.update(status="claimed", started_at=actual.isoformat())
                self._save()

            try:
                self.process = self.controller.start_scheduled(claim)
            except RuntimeError:
                if day["status"] == "claimed":
                    day["status"] = "failed"
                    day["error"] = "浏览器任务启动失败，请检查服务日志。"
                else:
                    day["status"] = "waiting"
            except ValueError:
                day["status"] = "failed"
                day["error"] = "没有可运行账号或账号配置无效。"
            else:
                self.active_day = day["date"]
                day["status"] = "running"
            self._save()

    def start(self):
        # Docker replicas sharing a data volume must not both schedule jobs.
        if os.name != "nt":
            import fcntl
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.leader = self.path.with_suffix(".daemon.lock").open("a")
            fcntl.flock(self.leader, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.thread = threading.Thread(target=self._loop, name="daily-checkin", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.error = None
            except Exception:
                self.error = "定时守护异常，请检查容器日志和持久化状态文件。"
                logging.exception("Daily scheduler tick failed")
            self.stop_event.wait(1)

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
        if self.leader is not None:
            self.leader.close()
