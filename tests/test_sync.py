"""Tests for sync.py against two local bare repos standing in for Gitea and GitHub (no network, no tokens).

    python3 -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import tempfile
import unittest

SYNC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sync.py")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.gitea, self.github, self.work = (os.path.join(d, n) for n in ("gitea.git", "github.git", "work"))
        for bare in (self.gitea, self.github):
            git("init", "-q", "--bare", bare, cwd=d)
        git("init", "-q", "-b", "main", self.work, cwd=d)
        for k, v in (("user.email", "t@example.com"), ("user.name", "test"), ("core.autocrlf", "false")):
            git("config", k, v, cwd=self.work)
        git("remote", "add", "gitea", self.gitea, cwd=self.work)
        git("remote", "add", "github", self.github, cwd=self.work)
        self.commit("a")
        git("tag", "v1", cwd=self.work)
        git("push", "-q", "gitea", "main", "v1", cwd=self.work)
        self.repos = os.path.join(d, "repos.txt")
        with open(self.repos, "w") as f:
            f.write(f"{self.gitea} {self.github}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, text):
        with open(os.path.join(self.work, "f.txt"), "w") as f:
            f.write(text + "\n")
        git("add", "f.txt", cwd=self.work)
        git("commit", "-q", "-m", text, cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def run_sync(self, **env):
        e = {**os.environ, "SKIP_API": "true", "REPOS_FILE": self.repos, "SYNC_REPOS": "", **env}
        return subprocess.run([sys.executable, SYNC], cwd=self.tmp.name, env=e, capture_output=True, text=True)

    def sha(self, repo, ref):
        r = subprocess.run(["git", "--git-dir", repo, "rev-parse", "--verify", "-q", ref], capture_output=True, text=True)
        return r.stdout.strip() or None

    def test_dry_run_pushes_nothing(self):
        r = self.run_sync(DIRECTION="both", DRY_RUN="true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would push", r.stdout)
        self.assertIsNone(self.sha(self.github, "main"))

    def test_empty_side_gets_everything(self):
        r = self.run_sync(DIRECTION="both")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.sha(self.github, "main"), self.sha(self.gitea, "main"))
        self.assertEqual(self.sha(self.github, "v1"), self.sha(self.gitea, "v1"))

    def test_fast_forward_from_github(self):
        self.run_sync(DIRECTION="both")
        new = self.commit("b")
        git("push", "-q", "github", "main", cwd=self.work)
        r = self.run_sync(DIRECTION="both")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.sha(self.gitea, "main"), new)

    def test_in_sync_is_a_no_op(self):
        self.run_sync(DIRECTION="both")
        r = self.run_sync(DIRECTION="both")
        self.assertEqual(r.returncode, 0)
        self.assertIn("already in sync", r.stdout)

    def test_diverged_is_reported_and_left_alone(self):
        self.run_sync(DIRECTION="both")
        g = self.commit("on gitea")
        git("push", "-q", "gitea", "main", cwd=self.work)
        git("reset", "-q", "--hard", "HEAD~1", cwd=self.work)
        h = self.commit("on github")
        git("push", "-q", "github", "main", cwd=self.work)
        r = self.run_sync(DIRECTION="both")
        self.assertEqual(r.returncode, 1)
        self.assertIn("diverged", r.stdout)
        self.assertEqual((self.sha(self.gitea, "main"), self.sha(self.github, "main")), (g, h))

    def test_force_refused_for_both(self):
        r = self.run_sync(DIRECTION="both", FORCE="true")
        self.assertEqual(r.returncode, 1)
        self.assertIn("one-way", r.stdout + r.stderr)

    def test_force_one_way_overwrites_target(self):
        self.run_sync(DIRECTION="both")
        git("reset", "-q", "--hard", "HEAD", cwd=self.work)
        other = self.commit("github only")
        git("push", "-q", "github", "main", cwd=self.work)
        git("reset", "-q", "--hard", "HEAD~1", cwd=self.work)
        mine = self.commit("gitea only")
        git("push", "-q", "gitea", "main", cwd=self.work)
        self.assertNotEqual(other, mine)
        r = self.run_sync(DIRECTION="gitea-to-github", FORCE="true")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.sha(self.github, "main"), mine)

    def test_differing_tag_is_left_alone(self):
        self.run_sync(DIRECTION="both")
        git("tag", "v2", cwd=self.work)
        git("push", "-q", "github", "v2", cwd=self.work)
        self.commit("c")
        git("tag", "-f", "v2", cwd=self.work)
        git("push", "-q", "gitea", "main", "v2", cwd=self.work)
        before = self.sha(self.github, "v2")
        r = self.run_sync(DIRECTION="both")
        self.assertEqual(r.returncode, 1)
        self.assertIn("tag v2", r.stdout)
        self.assertEqual(self.sha(self.github, "v2"), before)

    def test_branch_filter_and_tags_off(self):
        self.run_sync(DIRECTION="both")
        git("checkout", "-q", "-b", "dev", cwd=self.work)
        self.commit("dev")
        git("tag", "v3", cwd=self.work)
        git("push", "-q", "gitea", "dev", "v3", cwd=self.work)
        r = self.run_sync(DIRECTION="gitea-to-github", BRANCHES="main", TAGS="false")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIsNone(self.sha(self.github, "dev"))
        self.assertIsNone(self.sha(self.github, "v3"))

    def test_never_deletes(self):
        self.run_sync(DIRECTION="both")
        git("push", "-q", "gitea", "--delete", "v1", cwd=self.work)
        r = self.run_sync(DIRECTION="gitea-to-github")
        self.assertEqual(r.returncode, 0)
        self.assertIsNotNone(self.sha(self.github, "v1"))

    def test_list_from_sync_repos_variable(self):
        os.remove(self.repos)
        r = self.run_sync(DIRECTION="both", SYNC_REPOS=f"# comment\n{self.gitea} {self.github}\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sha(self.github, "main"), self.sha(self.gitea, "main"))

    def test_missing_list_is_a_clear_error(self):
        os.remove(self.repos)
        r = self.run_sync(DIRECTION="both")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SYNC_REPOS", r.stdout + r.stderr)

    def test_unknown_repo_filter(self):
        r = self.run_sync(DIRECTION="both", REPO="nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nope", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
