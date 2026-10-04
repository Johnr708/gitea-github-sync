# gitea-github-sync

Keep repos on a self-hosted Gitea and their copies on GitHub in step, with one button in Gitea's Actions tab.
Sync Gitea to GitHub, GitHub to Gitea, or both ways at once.

I built this because my Gitea lives on my home network where GitHub can't reach it. So everything runs on
the Gitea Actions runner, which can reach both. No webhooks, no open ports, nothing exposed to the internet.

[![tests](https://github.com/Johnr708/gitea-github-sync/actions/workflows/test.yml/badge.svg)](https://github.com/Johnr708/gitea-github-sync/actions/workflows/test.yml)

## What it does

For every repo pair on your list it fetches both sides and then:

- pushes a branch only when the other side is behind (a plain fast-forward)
- if both sides have new commits on the same branch, it leaves both alone and tells you which branch, so you
  can merge or rebase and run it again
- copies tags that are missing and reports a tag that points somewhere different on each side
- never deletes a branch or a tag
- creates the repo on the other side as private if it doesn't exist yet, so setting up a new mirror is just
  adding a line to the list

There's a `force` option for the first sync between two repos that started out separately (say GitHub made its
own README commit). It only works one way, so it's always clear which side wins. Do a dry run first.

## Setup

You need Gitea with Actions enabled and at least one runner (the stock `ubuntu-latest` runner image works).

1. Get this project into your Gitea. The easy way: in Gitea click +, New Migration, GitHub, paste this repo's
   URL and give it a name like `git-sync`. Or clone it and push it to an empty Gitea repo.

2. Make two tokens:
   - GitHub: a fine-grained token with access to the repos you sync (or "All repositories") and
     Contents: Read and write. Add Administration: Read and write if it should create missing repos.
     A classic token with the `repo` scope works too.
   - Gitea: Settings, Applications, a token with repository Read and write and user Read
     (plus organization Read and write if some repos live in an org).

3. In the Gitea repo go to Settings, Actions, Secrets and add them as `SYNC_GITHUB_TOKEN` and
   `SYNC_GITEA_TOKEN`. (Gitea won't accept secret names starting with `GITEA_` or `GITHUB_`.)

4. Tell it which repos to sync, either way works:
   - copy `repos.example.txt` to `repos.txt` and edit it, or
   - add an Actions variable called `SYNC_REPOS` (Settings, Actions, Variables) with the same lines in it.
     The variable wins if both exist. Handy if you'd rather not commit your list.

5. Go to the Actions tab and click `sync.yml` in the workflow list on the left. A "Run workflow" button
   shows up above the (empty) list of runs. The form labels each option with its description, e.g.
   "dry_run - only show what would happen". Tick dry_run the first time.

## The options

| Option | What it does |
|---|---|
| direction | `both`, `gitea-to-github` or `github-to-gitea` |
| repo | only this repo from the list, empty = all |
| branches | only these branches, comma separated, empty = all |
| tags | sync tags too (on by default) |
| dry_run | only print what it would do |
| force | overwrite the target where branches diverged (one-way only) |
| create_missing | create the repo on the other side if it's missing |

The run goes red when something needs you, like a diverged branch or a tag that differs. The log says which
repo and which branch.

Want it automatic? Uncomment the `schedule` block in `.gitea/workflows/sync.yml` and it runs every night.

## First sync with a repo GitHub already created

If you made the GitHub repo with a README or license, the two histories have nothing in common and the first
run reports `main: diverged`. Overwrite GitHub's starter commit once:

- direction `gitea-to-github`, branches `main`, force and dry_run ticked. Check the log says
  `main: github FORCED ...`
- run it again with dry_run unticked

After that, normal `both` runs work without force.

## Good to know

- Tokens are handed to git as an HTTP header, so they never end up in a remote URL or in the log.
- If your Gitea uses a certificate from your own CA, the runner's job container has to trust it. Mount the
  host's CA bundle into job containers in the runner's `config.yaml`:
  `container.options: "-v /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro"`
  (and add that path to `valid_volumes`).
- Branch protection on GitHub that blocks force pushes will stop a forced sync. Normal syncs are fine.
- Only Python 3 and git are needed, both are in the standard runner image.
- The workflow uses `actions/checkout`. That works out of the box because Gitea fetches actions from GitHub
  by default (`DEFAULT_ACTIONS_URL = github`). If you changed that, mirror `actions/checkout` to your Gitea.

## Running it outside Actions

    SYNC_GITEA_URL=https://gitea.example.com SYNC_GITEA_TOKEN=... SYNC_GITHUB_TOKEN=... \
      DIRECTION=both DRY_RUN=true python3 sync.py

## Tests

    python3 -m unittest discover -s tests -v

The tests use two local bare repos as stand-ins for Gitea and GitHub, so they need no network or tokens.

## License

MIT
