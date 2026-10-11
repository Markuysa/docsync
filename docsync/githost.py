"""Git access: LocalGitHost works with repositories on disk (demo, tests), GitLabHost clones, pushes,
opens and merges merge requests through the GitLab API."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import httpx


def git(repo: Path | str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def ensure_identity(repo: Path) -> None:
    if not git(repo, "config", "user.email", check=False):
        git(repo, "config", "user.email", "docsync@local")
        git(repo, "config", "user.name", "DocSync")


class LocalGitHost:
    """Repositories live on disk. service_repos: project_path -> Path."""

    def __init__(self, model_repo: Path, service_repos: dict[str, Path], default_branch: str = "main"):
        self.model_repo = Path(model_repo)
        self.service_repos = service_repos
        self.default_branch = default_branch

    def service_repo(self, project_path: str, head_sha: str) -> Path:
        if project_path not in self.service_repos:
            raise RuntimeError(f"Unknown repository {project_path}; set DOCSYNC_SERVICE_REPOS")
        return self.service_repos[project_path]

    def model_checkout(self) -> Path:
        ensure_identity(self.model_repo)
        git(self.model_repo, "checkout", "-q", self.default_branch)
        return self.model_repo

    def model_head(self) -> str:
        return git(self.model_repo, "rev-parse", "HEAD")

    def propose(self, branch: str, message: str, title: str, description: str) -> dict:
        """Commits the model working tree into `branch` and returns an "MR" description."""
        repo = self.model_repo
        git(repo, "checkout", "-q", "-B", branch)
        git(repo, "add", "-A")
        if git(repo, "status", "--porcelain"):
            git(repo, "commit", "-q", "-m", message)
        sha = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", self.default_branch)
        return {"branch": branch, "sha": sha, "url": f"local://{repo}#{branch}", "iid": None, "title": title}

    def merge(self, branch: str) -> str:
        repo = self.model_repo
        git(repo, "checkout", "-q", self.default_branch)
        git(repo, "merge", "-q", "--no-ff", "-m", f"docsync: merge {branch}", branch)
        git(repo, "branch", "-D", branch)
        return git(repo, "rev-parse", "HEAD")

    def note_source_mr(self, project_path: str, mr_iid: int | None, text: str) -> None:
        return None  # nowhere to post in local mode

    def discard_working_tree(self) -> None:
        git(self.model_repo, "checkout", "-q", "--", ".")
        git(self.model_repo, "clean", "-qfd")


class GitLabHost:
    def __init__(self, url: str, token: str, model_project: str, clones_dir: Path, default_branch: str = "main"):
        self.url = url.rstrip("/")
        self.token = token
        self.model_project = model_project
        self.clones_dir = Path(clones_dir)
        self.default_branch = default_branch
        self.http = httpx.Client(base_url=f"{self.url}/api/v4", headers={"PRIVATE-TOKEN": token}, timeout=60)

    def _check(self, r: httpx.Response) -> dict | list:
        if r.status_code >= 300:
            raise RuntimeError(f"GitLab {r.request.method} {r.request.url} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    def _clone_url(self, project_path: str) -> str:
        host = self.url.split("://", 1)[1]
        return f"https://oauth2:{self.token}@{host}/{project_path}.git"

    def _sync(self, project_path: str) -> Path:
        dest = self.clones_dir / project_path.replace("/", "__")
        if dest.exists():
            git(dest, "fetch", "-q", "--all", "--prune")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", self._clone_url(project_path), str(dest)], check=True)
        ensure_identity(dest)
        return dest

    def service_repo(self, project_path: str, head_sha: str) -> Path:
        repo = self._sync(project_path)
        git(repo, "cat-file", "-e", head_sha)
        return repo

    def model_checkout(self) -> Path:
        repo = self._sync(self.model_project)
        git(repo, "checkout", "-q", self.default_branch)
        git(repo, "reset", "-q", "--hard", f"origin/{self.default_branch}")
        return repo

    @property
    def model_repo(self) -> Path:
        return self.clones_dir / self.model_project.replace("/", "__")

    def model_head(self) -> str:
        return git(self.model_repo, "rev-parse", "HEAD")

    def propose(self, branch: str, message: str, title: str, description: str) -> dict:
        repo = self.model_repo
        git(repo, "checkout", "-q", "-B", branch)
        git(repo, "add", "-A")
        if git(repo, "status", "--porcelain"):
            git(repo, "commit", "-q", "-m", message)
        git(repo, "push", "-q", "-f", "origin", branch)
        sha = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", self.default_branch)
        pid = quote(self.model_project, safe="")
        existing = self._check(self.http.get(f"/projects/{pid}/merge_requests", params={"source_branch": branch, "state": "opened"}))
        if existing:
            mr = existing[0]
        else:
            mr = self._check(self.http.post(f"/projects/{pid}/merge_requests", json={
                "source_branch": branch, "target_branch": self.default_branch, "title": title,
                "description": description, "labels": "docsync", "remove_source_branch": True, "squash": True}))
        return {"branch": branch, "sha": sha, "url": mr["web_url"], "iid": mr["iid"], "title": title}

    def merge(self, branch: str) -> str:
        pid = quote(self.model_project, safe="")
        mrs = self._check(self.http.get(f"/projects/{pid}/merge_requests", params={"source_branch": branch, "state": "opened"}))
        if not mrs:
            raise RuntimeError(f"No open MR found for branch {branch}")
        mr = self._check(self.http.put(f"/projects/{pid}/merge_requests/{mrs[0]['iid']}/merge",
                                       json={"should_remove_source_branch": True, "squash": True}))
        self.model_checkout()
        return mr.get("merge_commit_sha") or mr.get("sha") or self.model_head()

    def note_source_mr(self, project_path: str, mr_iid: int | None, text: str) -> None:
        if not mr_iid:
            return
        pid = quote(project_path, safe="")
        self.http.post(f"/projects/{pid}/merge_requests/{mr_iid}/notes", json={"body": text})

    def merge_request_for_commit(self, project_path: str, sha: str) -> dict | None:
        pid = quote(project_path, safe="")
        mrs = self._check(self.http.get(f"/projects/{pid}/repository/commits/{sha}/merge_requests"))
        merged = [m for m in mrs if m.get("state") == "merged"]
        return merged[0] if merged else None

    def discard_working_tree(self) -> None:
        git(self.model_repo, "checkout", "-q", "--", ".")
        git(self.model_repo, "clean", "-qfd")


def make_host(s):
    if s.git_mode == "gitlab":
        if not s.gitlab_token:
            raise RuntimeError("DOCSYNC_GIT_MODE=gitlab requires GITLAB_TOKEN")
        return GitLabHost(s.gitlab_url, s.gitlab_token, s.model_project, s.clones_dir, s.model_default_branch)
    return LocalGitHost(s.model_repo_path, s.service_repos, s.model_default_branch)


def remove_tree(p: Path) -> None:
    shutil.rmtree(p, ignore_errors=True)
