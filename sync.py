#!/usr/bin/env python3
"""Sync branches and tags between Gitea and GitHub for every repo pair in SYNC_REPOS or repos.txt.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")
GITHUB_GIT = os.environ.get("GITHUB_GIT", "https://github.com")
GITEA_URL = os.environ.get("SYNC_GITEA_URL", "").rstrip("/")
TOKENS = {"gitea": os.environ.get("SYNC_GITEA_TOKEN", ""), "github": os.environ.get("SYNC_GITHUB_TOKEN", "")}
DIRECTION = os.environ.get("DIRECTION", "both")
ONLY = os.environ.get("REPO", "").strip()
BRANCHES = [b.strip() for b in os.environ.get("BRANCHES", "").split(",") if b.strip()]
flag = lambda name, default: (os.environ.get(name, "").strip() or default).lower() in ("1", "true", "yes")
TAGS, FORCE, DRY = flag("TAGS", "true"), flag("FORCE", "false"), flag("DRY_RUN", "false")
CREATE = flag("CREATE_MISSING", "true")
SKIP_API = flag("SKIP_API", "false")  # tests: plain local/remote URLs, no repo checks


def git(*args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[-400:]}")
    return r


def api(side, method, path, body=None):
    if side == "github":
        url, auth = GITHUB_API + path, "Bearer " + TOKENS["github"]
        extra = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    else:
        url, auth = f"{GITEA_URL}/api/v1{path}", "token " + TOKENS["gitea"]
        extra = {"Accept": "application/json"}
    headers = {"Authorization": auth, "User-Agent": "git-sync", **extra}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {}


_me = {}


def whoami(side):
    if side not in _me:
        code, data = api(side, "GET", "/user")
        if code != 200:
            raise RuntimeError(f"{side} token rejected (HTTP {code}) - check the secret")
        _me[side] = data["login"]
    return _me[side]


def ensure_repo(side, full, description):
    """True if the repo exists (or was created), False if it's missing and we may not create it."""
    if SKIP_API:
        return True
    code, _ = api(side, "GET", f"/repos/{full}")
    if code == 200:
        return True
    if code != 404:
        raise RuntimeError(f"{side} {full}: HTTP {code}")
    if not CREATE or DRY:
        print(f"  {side}:{full} does not exist" + (" (would create it)" if CREATE else ""))
        return False
    owner, name = full.split("/", 1)
    body = {"name": name, "private": True, "description": description}
    path = "/user/repos" if owner.lower() == whoami(side).lower() else f"/orgs/{owner}/repos"
    code, _ = api(side, "POST", path, body)
    if code not in (200, 201):
        raise RuntimeError(f"could not create {side}:{full} (HTTP {code})")
    print(f"  created {side}:{full} (private)")
    return True


def auth_header(side):
    user = "x-access-token" if side == "github" else "git-sync"
    return "AUTHORIZATION: basic " + base64.b64encode(f"{user}:{TOKENS[side]}".encode()).decode()


def remote_url(side, full):
    if SKIP_API:  # tests pass full URLs/paths in repos.txt
        return full
    return f"{GITEA_URL}/{full}.git" if side == "gitea" else f"{GITHUB_GIT}/{full}.git"


def refs(work, prefix):
    out = git("for-each-ref", "--format=%(refname) %(objectname)", prefix, cwd=work).stdout
    return {line.split()[0][len(prefix):]: line.split()[1] for line in out.splitlines() if line.strip()}


def is_ancestor(work, a, b):
    return git("merge-base", "--is-ancestor", a, b, cwd=work, check=False).returncode == 0


def sync_pair(gitea_full, github_full):
    print(f"\n== {gitea_full}  <->  {github_full}")
    names = {"gitea": gitea_full, "github": github_full}
    sources = {"gitea-to-github": ["gitea"], "github-to-gitea": ["github"], "both": ["gitea", "github"]}[DIRECTION]
    problems, actions = [], []
    exists = {}
    for side in ("gitea", "github"):
        if side in sources:
            code = 200 if SKIP_API else api(side, "GET", f"/repos/{names[side]}")[0]
            exists[side] = code == 200
            if code not in (200, 404):
                raise RuntimeError(f"{side} {names[side]}: HTTP {code}")
        else:
            exists[side] = ensure_repo(side, names[side], f"Mirror of {names['gitea' if side == 'github' else 'github']}")
    if DIRECTION == "both":  # a repo that only exists on one side gets created on the other
        for side in ("gitea", "github"):
            other = "github" if side == "gitea" else "gitea"
            if not exists[side] and exists[other]:
                exists[side] = ensure_repo(side, names[side], f"Mirror of {names[other]}")
    if not any(exists[s] for s in sources):
        problems.append("source repo does not exist")
        return actions, problems

    with tempfile.TemporaryDirectory(prefix="git-sync-") as work:
        git("init", "-q", "--bare", cwd=work)
        for side in ("gitea", "github"):
            if not exists[side]:
                continue
            url = remote_url(side, names[side])
            git("remote", "add", side, url, cwd=work)
            if not SKIP_API:
                base = url.split("/", 3)
                git("config", f"http.{base[0]}//{base[2]}/.extraheader", auth_header(side), cwd=work)
            specs = [f"+refs/heads/*:refs/sync/{side}/heads/*"]
            if TAGS:
                specs.append(f"+refs/tags/*:refs/sync/{side}/tags/*")
            git("fetch", "-q", "--no-tags", side, *specs, cwd=work)
        heads = {s: refs(work, f"refs/sync/{s}/heads/") for s in ("gitea", "github")}
        tags = {s: refs(work, f"refs/sync/{s}/tags/") for s in ("gitea", "github")}

        pushes = {"gitea": [], "github": []}  # target -> list of (refspec, lease or None)
        branch_names = sorted(set(heads["gitea"]) | set(heads["github"]))
        if BRANCHES:
            branch_names = [b for b in branch_names if b in BRANCHES]
        for br in branch_names:
            g, h = heads["gitea"].get(br), heads["github"].get(br)
            if g == h:
                continue
            pairs = [("gitea", "github")] if DIRECTION == "gitea-to-github" else \
                    [("github", "gitea")] if DIRECTION == "github-to-gitea" else [("gitea", "github"), ("github", "gitea")]
            done = False
            for src, dst in pairs:
                s, d = heads[src].get(br), heads[dst].get(br)
                if not s or not exists[dst]:
                    continue
                if d is None:
                    pushes[dst].append((f"{s}:refs/heads/{br}", None)); actions.append(f"{br}: new on {dst}"); done = True
                elif is_ancestor(work, d, s):
                    pushes[dst].append((f"{s}:refs/heads/{br}", None)); actions.append(f"{br}: {dst} fast-forward {d[:8]}..{s[:8]}"); done = True
                elif is_ancestor(work, s, d):
                    if DIRECTION != "both":
                        actions.append(f"{br}: {dst} is ahead of {src}, left alone")
                    done = done or DIRECTION != "both"
                elif FORCE and DIRECTION != "both":
                    pushes[dst].append((f"+{s}:refs/heads/{br}", d)); actions.append(f"{br}: {dst} FORCED {d[:8]} -> {s[:8]}"); done = True
                else:
                    problems.append(f"{br}: diverged ({src} {s[:8]}, {dst} {d[:8]}) - merge or rebase on one side, then run again")
                    done = True
                if done:
                    break

        if TAGS:
            for src, dst in (("gitea", "github"), ("github", "gitea")):
                if (DIRECTION == "gitea-to-github" and src != "gitea") or (DIRECTION == "github-to-gitea" and src != "github"):
                    continue
                if not exists[dst]:
                    continue
                for tag, sha in tags[src].items():
                    other = tags[dst].get(tag)
                    if other is None:
                        pushes[dst].append((f"{sha}:refs/tags/{tag}", None)); actions.append(f"tag {tag}: new on {dst}")
                        tags[dst][tag] = sha
                    elif other != sha:
                        msg = f"tag {tag}: differs between gitea and github, left alone"
                        if msg not in problems:
                            problems.append(msg)

        for dst, specs in pushes.items():
            if not specs:
                continue
            if DRY:
                continue
            args = ["push", "-q", "--porcelain", dst]
            for spec, lease in specs:
                if lease:
                    br = spec.split("refs/heads/")[1]
                    args.insert(2, f"--force-with-lease=refs/heads/{br}:{lease}")
                args.append(spec)
            r = git(*args, cwd=work, check=False)
            if r.returncode:
                # keep the server's reason (remote: ...) and the rejected refs, not just git's last line
                lines = [l.strip() for l in (r.stdout + r.stderr).splitlines()
                         if l.strip().startswith(("remote:", "!", "error:")) and l.strip() != "remote:"]
                problems.append(f"push to {dst} failed: " + (" | ".join(lines)[-600:] or r.stderr.strip()[-300:]))
    return actions, problems


def main():
    if DIRECTION not in ("gitea-to-github", "github-to-gitea", "both"):
        sys.exit(f"unknown DIRECTION {DIRECTION}")
    if FORCE and DIRECTION == "both":
        sys.exit("FORCE only works with a one-way direction (it has to know which side wins)")
    if not SKIP_API and not (GITEA_URL and TOKENS["gitea"] and TOKENS["github"]):
        sys.exit("SYNC_GITEA_URL, SYNC_GITEA_TOKEN and SYNC_GITHUB_TOKEN must be set (repo secrets SYNC_GITEA_TOKEN / SYNC_GITHUB_TOKEN)")
    # The list comes from the SYNC_REPOS variable if it's set, otherwise from repos.txt.
    listing = os.environ.get("SYNC_REPOS", "").strip()
    source = "SYNC_REPOS"
    if not listing:
        source = os.environ.get("REPOS_FILE", "repos.txt")
        if not os.path.exists(source):
            sys.exit("no repos to sync: set the SYNC_REPOS variable or create repos.txt (see repos.example.txt)")
        listing = open(source).read()
    pairs = []
    for line in listing.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            parts = line.split()
            if len(parts) != 2:
                sys.exit(f"{source}: expected '<gitea owner/repo> <github owner/repo>', got: {line}")
            pairs.append(parts)
    if not pairs:
        sys.exit(f"{source} has no repo pairs in it")
    if ONLY:
        pairs = [p for p in pairs if ONLY in (p[0], p[1], p[0].split("/")[-1], p[1].split("/")[-1])]
        if not pairs:
            sys.exit(f"{ONLY} is not in repos.txt")
    print(f"direction={DIRECTION} branches={','.join(BRANCHES) or 'all'} tags={TAGS} force={FORCE} "
          f"dry_run={DRY} create_missing={CREATE} repos={len(pairs)}")
    failed = 0
    for g, h in pairs:
        try:
            actions, problems = sync_pair(g, h)
        except RuntimeError as e:
            actions, problems = [], [str(e)]
        for a in actions:
            print(("  would push  " if DRY and ("new on" in a or "fast-forward" in a or "FORCED" in a) else "  ") + a)
        for p in problems:
            print("  PROBLEM  " + p)
        if not actions and not problems:
            print("  already in sync")
        failed += bool(problems)
    print(f"\n{len(pairs) - failed}/{len(pairs)} repos OK" + (" (dry run, nothing pushed)" if DRY else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
