"""Execute the documented Git handoff only against disposable local repositories."""
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile
import unittest


class CommissioningCommandsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="crab-handoff-test-")
        cls.root = Path(cls.temporary.name)
        # Exported index/object/common-dir/config variables must never redirect
        # fixture writes into a developer's real repository or object store.
        cls.environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("GIT_")}
        cls.environment.update(GIT_CONFIG_GLOBAL=os.devnull,
                               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        cls.remote, cls.source = cls.root / "remote.git", cls.root / "source"
        cls.git(cls.root, "init", "--bare", "--initial-branch=main", str(cls.remote))
        cls.git(cls.root, "init", "--initial-branch=main", str(cls.source))
        cls.git(cls.source, "remote", "add", "origin", str(cls.remote))
        for name, value in ((".gitignore", "*.local\n"), ("version", "base\n"),
                            ("removed.local", "preserved base content\n")):
            (cls.source / name).write_text(value)
            cls.git(cls.source, "add", "-f", "--", name)
        cls.git(cls.source, "commit", "-m", "base fixture")
        cls.base = cls.git(cls.source, "rev-parse", "HEAD").stdout.strip()
        cls.git(cls.source, "rm", "--", "removed.local")
        for name in ("version", "collision", "ignored.local"):
            (cls.source / name).write_text("release\n")
            cls.git(cls.source, "add", "-f", "--", name)
        cls.git(cls.source, "commit", "-m", "release fixture")
        cls.release = cls.git(cls.source, "rev-parse", "HEAD").stdout.strip()
        (cls.source / "version").write_text("newer main\n")
        cls.git(cls.source, "add", "--", "version")
        cls.git(cls.source, "commit", "-m", "future fixture")
        cls.git(cls.source, "push", "origin", "main")
        document = (Path(__file__).resolve().parents[1] / "docs/commissioning.md").read_text()
        blocks = re.findall(r"```bash\n(.*?)\n```", document, re.S)
        cls.update = next(x for x in blocks if "git merge --ff-only" in x)
        cls.rollback = next(x for x in blocks if "git checkout --detach" in x)
        cls.update = cls.update[cls.update.index("(\n"):]
        cls.rollback = cls.rollback[cls.rollback.index("(\n"):]

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def git(cls, cwd, *arguments):
        return subprocess.run(["git", "-c", "user.name=Crab fixture", "-c",
                               "user.email=fixture@example.invalid", *arguments],
                              cwd=cwd, env=cls.environment, text=True,
                              capture_output=True, timeout=10, check=True)

    def checkout(self, revision):
        target = self.root / self._testMethodName
        self.git(self.root, "clone", "--no-checkout", str(self.remote), str(target))
        self.git(target, "checkout", "-B", "main", revision)
        return target

    def run_block(self, target, block, **settings):
        return subprocess.run(["bash", "-c", block], cwd=target,
                              env=dict(self.environment, **settings), text=True,
                              capture_output=True, timeout=10)

    def test_update_pins_release_even_when_remote_main_is_newer(self):
        target = self.checkout(self.base)
        result = self.run_block(target, self.update, RELEASE_SHA=self.release)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(target, "rev-parse", "HEAD").stdout.strip(), self.release)

    def test_fixture_ignores_inherited_git_redirection(self):
        with tempfile.TemporaryDirectory(prefix="crab-git-canary-") as directory:
            canary = Path(directory) / "owner-index"
            canary.write_text("must remain untouched\n")
            environment = dict(os.environ, GIT_INDEX_FILE=str(canary),
                               GIT_COMMON_DIR=directory, GIT_OBJECT_DIRECTORY=directory,
                               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.worktree",
                               GIT_CONFIG_VALUE_0=directory)
            target = "tests.test_commissioning_commands.CommissioningCommandsTests.test_update_pins_release_even_when_remote_main_is_newer"
            result = subprocess.run([sys.executable, "-m", "unittest", target],
                                    cwd=Path(__file__).resolve().parents[1], env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(canary.read_text(), "must remain untouched\n")

    def test_update_preserves_tracked_edits(self):
        target = self.checkout(self.base)
        (target / "version").write_text("owner edit\n")
        result = self.run_block(target, self.update, RELEASE_SHA=self.release)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((target / "version").read_text(), "owner edit\n")

    def test_update_rejects_divergence(self):
        target = self.checkout(self.base)
        (target / "local").write_text("local commit\n")
        self.git(target, "add", "--", "local")
        self.git(target, "commit", "-m", "owner fixture")
        before = self.git(target, "rev-parse", "HEAD").stdout
        result = self.run_block(target, self.update, RELEASE_SHA=self.release)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(target, "rev-parse", "HEAD").stdout, before)

    def assert_update_collision(self, name):
        target = self.checkout(self.base)
        (target / name).write_text("private owner file\n")
        result = self.run_block(target, self.update, RELEASE_SHA=self.release)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((target / name).read_text(), "private owner file\n")
        self.assertEqual(self.git(target, "rev-parse", "HEAD").stdout.strip(), self.base)

    def test_update_preserves_untracked_collision(self):
        self.assert_update_collision("collision")

    def test_update_preserves_ignored_collision(self):
        self.assert_update_collision("ignored.local")

    def test_update_rejects_malformed_pin(self):
        target = self.checkout(self.base)
        result = self.run_block(target, self.update, RELEASE_SHA="main")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(target, "rev-parse", "HEAD").stdout.strip(), self.base)

    def test_rollback_preserves_main_branch_and_detaches_at_prior_commit(self):
        target = self.checkout(self.release)
        result = self.run_block(target, self.rollback, ROLLBACK_SHA=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(target, "rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual(self.git(target, "rev-parse", "main").stdout.strip(), self.release)
        self.assertEqual(self.git(target, "branch", "--show-current").stdout.strip(), "")

    def test_rollback_preserves_tracked_edits(self):
        target = self.checkout(self.release)
        (target / "version").write_text("owner edit\n")
        result = self.run_block(target, self.rollback, ROLLBACK_SHA=self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((target / "version").read_text(), "owner edit\n")

    def test_rollback_preserves_ignored_collision(self):
        target = self.checkout(self.release)
        (target / "removed.local").write_text("private owner file\n")
        result = self.run_block(target, self.rollback, ROLLBACK_SHA=self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((target / "removed.local").read_text(), "private owner file\n")
        self.assertEqual(self.git(target, "rev-parse", "HEAD").stdout.strip(), self.release)
