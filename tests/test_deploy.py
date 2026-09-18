"""部署入口集成测试：临时 Git 仓库，不连接 VPS，也不操作服务。"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("flock"), "部署入口需要 Linux flock")
class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote"
        self.checkout = self.root / "checkout"
        self.marker = self.root / "deployed"
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
        self.env.pop("SSH_ORIGINAL_COMMAND", None)
        self.git("init", "-q", "-b", "main", str(self.remote))
        (self.remote / "deploy").mkdir()
        # 记录部署开始与结束时的 HEAD，模拟运行中不能被另一个部署改写的脚本。
        (self.remote / "deploy/deploy.sh").write_text(
            '#!/bin/bash\nset -euo pipefail\n'
            'cd "$(dirname "$0")/.."\n'
            f'mkdir "{self.root / "active"}"\n'
            f"trap 'rmdir \"{self.root / 'active'}\"' EXIT\n"
            f'git rev-parse HEAD >> "{self.marker}"\n'
            'sleep 0.2\n'
            f'git rev-parse HEAD >> "{self.marker}"\n'
        )
        self.git("-C", str(self.remote), "add", ".")
        self.git("-C", str(self.remote), "commit", "-qm", "first")
        self.old = self.git("-C", str(self.remote), "rev-parse", "HEAD")
        self.git("clone", "-q", str(self.remote), str(self.checkout))
        (self.remote / "version").write_text("new")
        self.git("-C", str(self.remote), "add", ".")
        self.git("-C", str(self.remote), "commit", "-qm", "second")
        self.latest = self.git("-C", str(self.remote), "rev-parse", "HEAD")
        source = Path(__file__).resolve().parent.parent / "deploy/xueqiu-deploy"
        self.entry = self.root / "entry"
        self.entry.write_text(source.read_text().replace(
            "SRC=/opt/xueqiu-mcp/.src", f'SRC="{self.checkout}"'
        ).replace("/run/lock/xueqiu-deploy.lock", str(self.root / "deploy.lock")))

    def git(self, *args):
        return subprocess.check_output(["git", *args], env=self.env, stderr=subprocess.PIPE, text=True).strip()

    def deploy(self, *args, original=None):
        env = dict(self.env)
        if original is not None:
            env["SSH_ORIGINAL_COMMAND"] = original
        return subprocess.run(["bash", str(self.entry), *args], env=env, text=True, capture_output=True)

    def test_deploys_exact_current_main(self):
        result = self.deploy(self.latest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marker.read_text().splitlines(), [self.latest] * 2)

    def test_stale_ci_result_does_not_deploy_or_reset_checkout(self):
        result = self.deploy(self.old)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("跳过过期提交", result.stdout)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.git("-C", str(self.checkout), "rev-parse", "HEAD"), self.old)

    def test_ssh_forced_command_accepts_sha(self):
        result = self.deploy(original=self.latest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marker.read_text().splitlines(), [self.latest] * 2)

    def test_rejects_shell_commands_and_options(self):
        for original in ("id", "main", "--help", self.latest + "; id", "$(id)"):
            with self.subTest(original=original):
                result = self.deploy(original=original)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.marker.exists())

    def test_concurrent_runs_do_not_overlap(self):
        first = subprocess.Popen(["bash", str(self.entry), self.latest], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(["bash", str(self.entry), self.latest], env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for process in (first, second):
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(self.marker.read_text().splitlines(), [self.latest] * 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
