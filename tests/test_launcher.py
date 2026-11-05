from __future__ import annotations

import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import main as project
from agents.speaker_agent import main as speaker
from shared.runtime.lock import InstanceLock
from shared.runtime.supervisor import AgentProcess, ProjectSupervisor
from test_agent import settings


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_environment_example_never_overwrites_existing_config(self):
        (self.root / ".env.example").write_text("EXAMPLE=value\n", encoding="utf-8")
        project.ensure_env_file(self.root)
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), "EXAMPLE=value\n")
        (self.root / ".env").write_text("EXISTING=keep\n", encoding="utf-8")
        project.ensure_env_file(self.root)
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), "EXISTING=keep\n")

    def test_instance_lock_blocks_duplicate_and_is_released(self):
        path = self.root / "instance.lock"
        with InstanceLock(path):
            with self.assertRaisesRegex(RuntimeError, "已有实例"):
                with InstanceLock(path):
                    self.fail("duplicate lock acquired")
        with InstanceLock(path):
            pass

    def test_check_failure_does_not_start_any_agent(self):
        python = project.project_python(self.root)
        python.parent.mkdir(parents=True)
        python.touch()
        (self.root / ".env").write_text("", encoding="utf-8")
        with patch.object(project, "ROOT", self.root), patch.object(project, "check_agents", return_value=False), \
             patch.object(project, "ProjectSupervisor") as supervisor:
            self.assertEqual(project.main([]), 2)
            supervisor.assert_not_called()

    def test_check_is_offline_and_does_not_install_or_create_env(self):
        with patch.object(project, "ROOT", self.root), patch.object(project, "setup_environment") as setup, \
             patch.object(project, "ProjectSupervisor") as supervisor:
            self.assertEqual(project.main(["--check"]), 2)
            setup.assert_not_called()
            supervisor.assert_not_called()
        self.assertFalse((self.root / ".env").exists())
        self.assertFalse((self.root / "runtime").exists())

    def test_default_entrypoint_starts_registered_agents_with_project_python(self):
        python = project.project_python(self.root)
        python.parent.mkdir(parents=True)
        python.touch()
        (self.root / ".env").write_text("EXISTING=keep\n", encoding="utf-8")
        with patch.object(project, "ROOT", self.root), patch.object(project, "check_agents", return_value=True), \
             patch.object(project, "ProjectSupervisor") as supervisor, patch.object(project, "setup_environment") as setup:
            supervisor.return_value.run.return_value = 0
            self.assertEqual(project.main([]), 0)
            setup.assert_not_called()
            supervisor.assert_called_once_with(self.root, python, project.AGENTS)
            supervisor.return_value.run.assert_called_once()

    def test_project_uses_venv_and_root_for_all_preflight_checks(self):
        agents = (AgentProcess("one", "agents.one.main"), AgentProcess("two", "agents.two.main"))
        with patch.object(project.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertTrue(project.check_agents(self.root, Path("test-python"), agents))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ["test-python", "-m", "agents.two.main", "--check"])
        self.assertEqual(run.call_args.kwargs["cwd"], self.root)

    def test_speaker_check_does_not_construct_any_network_client(self):
        config = settings(self.root)
        with patch.object(speaker, "load_dotenv"), patch.object(speaker.Settings, "from_env", return_value=config), \
             patch.object(speaker, "FeishuGateway") as gateway, patch.object(speaker, "create_socket_client") as socket:
            self.assertEqual(speaker.main(["--check"]), 0)
            gateway.assert_not_called()
            socket.assert_not_called()
        self.assertFalse((self.root / "speaker.sqlite3").exists())

    def test_speaker_rejects_placeholder_credentials_before_network(self):
        config = replace(settings(self.root), app_id="cli_xxx")
        with patch.object(speaker, "load_dotenv"), patch.object(speaker.Settings, "from_env", return_value=config), \
             patch.object(speaker, "FeishuGateway") as gateway:
            self.assertEqual(speaker.main(["--check"]), 2)
            gateway.assert_not_called()

    def test_speaker_stops_worker_when_connection_is_interrupted(self):
        config = replace(settings(self.root), enable_polling=False)
        agent = Mock()
        connection = Mock()
        connection.start.side_effect = KeyboardInterrupt
        with patch.object(speaker, "FeishuGateway"), patch.object(speaker, "DemandAgent", return_value=agent), \
             patch.object(speaker, "register_handlers"), patch.object(speaker, "create_socket_client", return_value=connection):
            with self.assertRaises(KeyboardInterrupt):
                speaker.run_agent(config)
        agent.stop.assert_called_once()


class SupervisorTests(unittest.TestCase):
    def test_keyboard_interrupt_stops_owned_processes(self):
        child = Mock()
        child.poll.return_value = None
        supervisor = ProjectSupervisor(Path("."), Path(sys.executable), (AgentProcess("one", "fake_one"),))
        with patch("shared.runtime.supervisor.subprocess.Popen", return_value=child), \
             patch("shared.runtime.supervisor.time.sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(supervisor.run(), 0)
        child.send_signal.assert_called_once()
        child.wait.assert_called_once()
        self.assertEqual(supervisor.processes, [])

    def test_partial_startup_failure_cleans_up_already_started_child(self):
        child = Mock()
        child.poll.return_value = None
        supervisor = ProjectSupervisor(Path("."), Path(sys.executable), (
            AgentProcess("one", "fake_one"), AgentProcess("two", "fake_two"),
        ))
        with patch("shared.runtime.supervisor.subprocess.Popen", side_effect=[child, OSError("spawn failed")]):
            with self.assertRaises(OSError):
                supervisor.run()
        child.send_signal.assert_called_once()
        child.wait.assert_called_once()

    def test_stuck_child_is_killed_after_grace_period(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("fixture", 0), 0]
        supervisor = ProjectSupervisor(Path("."), Path(sys.executable), (), stop_timeout=0)
        supervisor.processes.append((AgentProcess("one", "fake_one"), child))
        supervisor.stop()
        child.kill.assert_called_once()
        self.assertEqual(supervisor.processes, [])

    def test_real_local_child_failure_stops_sibling_without_external_services(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fixture_fail.py").write_text("import time\ntime.sleep(0.2)\nraise SystemExit(7)\n", encoding="utf-8")
            (root / "fixture_wait.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            supervisor = ProjectSupervisor(root, Path(sys.executable), (
                AgentProcess("failure", "fixture_fail"), AgentProcess("sibling", "fixture_wait"),
            ), stop_timeout=0.5)
            processes: list[subprocess.Popen] = []
            original = subprocess.Popen

            def spawn(*args, **kwargs):
                process = original(*args, **kwargs)
                processes.append(process)
                return process

            try:
                with patch("shared.runtime.supervisor.subprocess.Popen", side_effect=spawn):
                    self.assertEqual(supervisor.run(), 7)
                self.assertEqual(len(processes), 2)
                self.assertTrue(all(process.poll() is not None for process in processes))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    unittest.main()
