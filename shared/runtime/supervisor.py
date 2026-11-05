from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentProcess:
    name: str
    module: str


class ProjectSupervisor:
    def __init__(self, root: Path, python: Path, agents: tuple[AgentProcess, ...], *, stop_timeout: float = 10) -> None:
        self.root = root
        self.python = python
        self.agents = agents
        self.stop_timeout = stop_timeout
        self.processes: list[tuple[AgentProcess, subprocess.Popen]] = []

    def run(self) -> int:
        try:
            for agent in self.agents:
                process = subprocess.Popen(
                    [str(self.python), "-u", "-m", agent.module], cwd=self.root,
                    env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
                self.processes.append((agent, process))
                LOGGER.info("已启动 %s（PID %s），日志显示在当前窗口", agent.name, process.pid)
            while self.processes:
                for agent, process in self.processes:
                    code = process.poll()
                    if code is not None:
                        LOGGER.error("%s 已退出（退出码 %s），正在停止其他 Agent", agent.name, code)
                        return code if code > 0 else 1
                time.sleep(0.2)
            return 0
        except KeyboardInterrupt:
            LOGGER.info("收到停止指令，正在关闭所有 Agent")
            return 0
        finally:
            self.stop()

    def stop(self) -> None:
        running = [(agent, process) for agent, process in self.processes if process.poll() is None]
        for agent, process in running:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            except OSError:
                LOGGER.warning("无法通知 %s 正常退出，将在超时后终止", agent.name)
        deadline = time.monotonic() + self.stop_timeout
        for agent, process in running:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                LOGGER.warning("%s 未及时退出，正在终止进程", agent.name)
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait()
        self.processes.clear()
