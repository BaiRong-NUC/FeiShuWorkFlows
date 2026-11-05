"""Project controller. Run this file directly with Python."""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from shared.runtime.lock import InstanceLock
from shared.runtime.signals import interrupt_signals
from shared.runtime.supervisor import AgentProcess, ProjectSupervisor


ROOT = Path(__file__).resolve().parent
# Register only implemented Agents. Each module supports --check and Ctrl+C.
AGENTS = (AgentProcess("speaker", "agents.speaker_agent.main"),)
LOGGER = logging.getLogger(__name__)


def project_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def setup_environment(root: Path) -> int:
    python = project_python(root)
    if not python.is_file():
        LOGGER.info("创建项目虚拟环境 .venv")
        subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], cwd=root, check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(root / "requirements.txt")], cwd=root, check=True)
    ensure_env_file(root)
    LOGGER.info("依赖安装完成，已保留现有配置")
    return 0


def ensure_env_file(root: Path) -> None:
    env_file = root / ".env"
    if not env_file.exists():
        shutil.copyfile(root / ".env.example", env_file)
        LOGGER.info("已创建 .env，请填写飞书配置和 DeepSeek Key 后再次启动")


def check_agents(root: Path, python: Path, agents: tuple[AgentProcess, ...]) -> bool:
    for agent in agents:
        LOGGER.info("检查 %s 的依赖、配置和模板", agent.name)
        result = subprocess.run(
            [str(python), "-m", agent.module, "--check"], cwd=root,
            env={**os.environ, "PYTHONUTF8": "1"}, check=False,
        )
        if result.returncode:
            LOGGER.error("%s 检查失败，项目尚未启动；缺少依赖时请运行 --setup", agent.name)
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="飞书多 Agent 项目统一控制入口")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true", help="安装运行依赖，缺少 .env 时创建配置示例")
    mode.add_argument("--check", action="store_true", help="仅检查依赖、配置和模板，不连接飞书或大模型")
    mode.add_argument("--list", action="store_true", help="列出已实现并注册的 Agent")
    mode.add_argument("--discover", action="store_true", help="只读发现台账文件中的所有表，不启动业务处理")
    mode.add_argument("--login", action="store_true", help="授权个人飞书账号")
    mode.add_argument("--check-user", action="store_true", help="只读检查个人飞书接入，不发送消息或写入台账")
    parser.add_argument("--agents", nargs="+", choices=[agent.name for agent in AGENTS], help="只启动指定的 Agent")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.version_info < (3, 11):
        LOGGER.error("需要 Python 3.11 或更新版本")
        return 2
    if args.list:
        for agent in AGENTS:
            print(f"{agent.name}: {agent.module}")
        return 0
    selected = tuple(agent for agent in AGENTS if not args.agents or agent.name in args.agents)
    python = project_python(ROOT)
    try:
        if args.check or args.discover or args.login or args.check_user:
            if not python.is_file():
                LOGGER.error("缺少 .venv，请先运行 python main.py --setup")
                return 2
            if args.discover or args.login or args.check_user:
                action = "--login" if args.login else "--check-user" if args.check_user else "--discover"
                return subprocess.run([str(python), "-m", "agents.speaker_agent.main", action],
                                      cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1"}, check=False).returncode
            if not check_agents(ROOT, python, selected):
                return 2
            LOGGER.info("本地检查通过；未验证真实账号权限或网络连接")
            return 0
        with interrupt_signals(), InstanceLock(ROOT / "runtime" / "project.lock"):
            if args.setup or not python.is_file():
                setup_environment(ROOT)
                if args.setup:
                    return 0
            ensure_env_file(ROOT)
            if not check_agents(ROOT, python, selected):
                return 2
            LOGGER.info("启动项目：%s；按 Ctrl+C 停止整个项目", ", ".join(agent.name for agent in selected))
            return ProjectSupervisor(ROOT, python, selected).run()
    except KeyboardInterrupt:
        LOGGER.info("已取消")
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        LOGGER.error("启动失败：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
