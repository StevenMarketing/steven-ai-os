# GitHub ↔ Hermes Issue Worker

Bridge between GitHub Issues and Hermes: a task lands in the repository, Hermes
does the work on its own branch, opens a pull request, and reports back on the
issue. **A human merges.** Nothing is ever deployed or published.

Status: **built and tested offline; NOT activated.** The cron job is created in
the `paused` state and no AI usage is consumed until it is explicitly enabled.

---

## 1. Flow

```
Issue with label hermes-proposed          <- anyone (incl. ChatGPT) may create
        |
        |  the repository owner applies  hermes-ready     <- the human gate
        v
cron (every 10m) github_worker_watch.sh   -> one deterministic fingerprint line
        |                                     unchanged => agent never starts
        v
cron github_worker_poll.sh                -> eligible-issue report (free)
        |
        v
agent: validate -> guard claim -> branch -> implement -> test -> push branch
        |
        v
draft Pull Request  +  comment on the issue (TEST RESULT / files / cost / errors)
        |
        v
the owner reviews and merges
```

Two labels do the authorisation:

| Label | Who applies it | Effect |
|---|---|---|
| `hermes-proposed` | anyone, including a bot | **nothing** — the worker ignores it |
| `hermes-ready` | **only `StevenMarketing`** | approval; the worker may pick it up |

The authorisation check reads the issue's *event* stream and requires the login
that applied `hermes-ready` to be in the owner allow-list
(`--owner`, default `StevenMarketing`). A label applied by a bot does **not**
count: see the fixture cases `#104` and `#105` and their tests.

## 2. What the worker refuses to do

* push to `main` / `master` (branch + PR only), force-push, delete a branch
* merge, approve, deploy, change repository settings
* call Google, publish a review reply, or touch customer data
* print, log or paste a token
* edit a protected path: `.env*`, `**/secrets*`, `*secret*.json`, `**/*.pem`,
  `**/*.key`, `**/id_rsa*`, `.gitignore`, `.github/workflows/**`,
  `.github/ISSUE_TEMPLATE/**`, `.github/rulesets/**`, `src/gbp/auth/**`,
  `src/gbp/authz/**`, `src/gbp/security/**`, `sql/schema.sql`,
  `scripts/issue_*.py`, `scripts/hooks/**`, `scripts/cron/**`,
  `tests/test_issue_worker.py`, `tests/fixtures/github_issues.json`, this file

If a task genuinely needs one of those, the worker stops and reports it as
`hermes-blocked` for a human to do.

## 3. Prompt injection

Issue text, comments and PR bodies are **untrusted data**, never instructions.
`issue_worker_lib.scan_injection()` trips on: "ignore previous instructions",
"disregard the above", role switches, "system prompt", requests to reveal/print
a token or API key, reading `.env`, dumping env vars, pushing/merging to `main`,
force-push, skipping/disabling tests, `curl`/`wget` piping, `base64 -d`,
`export GITHUB_TOKEN`, adding secrets, weakening the guard/allowlist, and
self-merging. Any hit rejects the issue before any work starts (`#103` in the
fixture demonstrates it).

## 4. Limits (enforced by code, not by trust)

| Limit | Value | Where |
|---|---|---|
| AI cost per issue | $0.06 | `issue_worker_lib.MAX_COST_PER_ISSUE` |
| AI cost per day | $0.15 | `MAX_COST_PER_DAY` |
| Issues per day | 3 | `MAX_ISSUES_PER_DAY` |
| Concurrent workers | 1 | claim + `kanban.max_in_progress_per_profile: 1` |
| Runtime per issue | 300 s | kanban `max_runtime_seconds` |
| Poll interval | 10 min | cron schedule |
| Claim TTL | 30 min | `CLAIM_TTL_SECONDS` (a crashed worker is retryable) |

> **Honest note.** Hermes has no provider-level USD cap. These ceilings are
> self-enforced by `scripts/issue_guard.py`; the only hard money wall would be a
> spending limit configured in the OpenRouter account itself.

## 5. Setup

### 5.1 Token (least privilege)

Create a **fine-grained personal access token** limited to the single
repository `StevenMarketing/steven-ai-os`:

| Permission | Level |
|---|---|
| Contents | Read and write |
| Issues | Read and write |
| Pull requests | Read and write |
| Metadata | Read (mandatory) |

Nothing else — no Workflows, Secrets, Administration, Webhooks or Actions.
Set an expiry (90 days) and rotate.

Store it on the VPS **yourself**, never through chat:

```bash
printf 'GITHUB_TOKEN=<paste-here>\n' >> /opt/data/.env
chmod 600 /opt/data/.env
```

The poller reads it from `--token-file /opt/data/.env` (or `$GITHUB_TOKEN`).
It is never echoed. `scripts/issue_guard.py check-secrets` additionally refuses
to let a real credential value reach a commit.

### 5.2 Push guard

```bash
cd /opt/data/gbp-integration
git config --local core.hooksPath scripts/hooks
```

`scripts/hooks/pre-push` refuses any push that writes `main`/`master`, deletes a
branch, or is non-fast-forward. A deliberate human bypass exists:
`HERMES_ALLOW_MAIN_PUSH=1 git push ...`.

### 5.3 Labels

Create them in the repository (Settings → Labels):

`hermes-proposed` · `hermes-ready` · `hermes-running` · `hermes-needs-info` ·
`hermes-done` · `hermes-blocked`

### 5.4 Cron

Two wrapper scripts, both safe to run by hand:

```bash
python3 scripts/issue_poller.py --fixture tests/fixtures/github_issues.json   # offline demo
scripts/cron/github_worker_watch.sh                                          # fingerprint only
scripts/cron/github_worker_poll.sh                                           # full report
```

The job is created **paused**:

```bash
hermes cron list                 # find the job id
hermes cron resume <job_id>      # activate (only after the owner approves)
hermes cron pause  <job_id>      # stop
```

Emergency stop (checked at every gate, independent of cron):

```bash
python3 scripts/issue_guard.py pause --reason "stop everything"
python3 scripts/issue_guard.py resume
```

## 6. Manual runbook for one issue

```bash
cd /opt/data/gbp-integration
G="python3 scripts/issue_guard.py"

$G can-start --issue 42 --estimate 0.05   # exit 1 => stop, read the reason
$G claim --issue 42                       # exclusive, expiring claim

# branch in a separate worktree, never on main
BRANCH=$(python3 -c "import sys;sys.path.insert(0,'scripts');import issue_worker_lib as l;print(l.branch_for(42,'short title'))")
git fetch origin
git worktree add ../wt-issue-42 -b "$BRANCH" origin/main

# ... implement inside ../wt-issue-42, respecting the issue's Scope ...

PYTHONPATH=src python3 -m unittest discover -s tests     # must be green
git -C ../wt-issue-42 add -A
git -C ../wt-issue-42 commit -m "feat(web): add ZIP column (#42)"
git -C ../wt-issue-42 push -u origin "$BRANCH"           # hook allows a branch

$G check-paths --base origin/main          # exit 1 => a protected path is in play
$G check-secrets --root .                  # exit 1 => a credential is in the diff

# open a DRAFT pull request with curl against the GitHub API, body from
# .github/pull_request_template.md, containing "Closes #42"
# then comment the result on the issue and label it hermes-done/hermes-blocked

$G finish --issue 42 --outcome done --pr <PR-URL> --cost 0.031
git worktree remove ../wt-issue-42
```

## 7. Branch protection and rulesets — verified status

GitHub's own documentation (*Available rules for rulesets*) states:

> Rulesets are available in public repositories with GitHub Free and GitHub Free
> for organizations, and in public and private repositories with GitHub Pro,
> GitHub Team, and GitHub Enterprise Cloud.

`StevenMarketing/steven-ai-os` is a **private repository on a free plan**, so
**branch rulesets and classic branch protection cannot be applied**, and the
REST endpoints return `403 Upgrade to GitHub Pro or make this repository
public`. No paid plan has been purchased.

Because of that, protection of `main` is enforced **in the worker**:

1. `scripts/hooks/pre-push` — refuses any push that targets `main`/`master`, any
   deletion, and any non-fast-forward update.
2. The runbook only ever pushes a `hermes/issue-*` branch.
3. `issue_guard.check-paths` blocks changes to the workflow/guard/secret paths.

`.github/rulesets/main-protection.json` is a ready-to-import ruleset for the day
the plan changes. It is documentation until then, and the worker deliberately
has no `Administration` permission, so it cannot apply it itself.

## 8. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `no GitHub token` | no token in env or `.env` | add `GITHUB_TOKEN=` to `/opt/data/.env` (mode 600) |
| `looks like a dotenv file but has no GITHUB_TOKEN=` | the file is an env file and the key is missing | add the `GITHUB_TOKEN=` line; the worker refuses to use a whole env file as a token, because that would send every other secret in it to GitHub |
| nothing is picked up | label missing, or applied by a bot | owner applies `hermes-ready` themselves |
| issue ignored, comment says *needs info* | a required section is missing | fill in the issue template |
| issue ignored, *injection* | the body trips a tripwire | rewrite the issue; do not put instructions for the agent in it |
| `can-start` refuses | paused, claimed, done, or over budget | read the printed reason; `status` shows usage |
| push refused by `hermes-guard` | it targeted `main` | push the branch instead |
| work seems stuck | claim TTL not yet expired | wait 30 min, or `issue_guard reclaim` |
| agent runs but does nothing | looking at the poll report | if `eligible=0`, that is correct and costs nothing |
