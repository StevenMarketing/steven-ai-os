# Pull request

> Opened by the Hermes worker from an approved `hermes-ready` issue.
> **A human merges this. The worker never merges, deploys or publishes.**

## What and why

<!-- one short paragraph; link the issue -->

Closes #

## Change summary

| File | Change |
|---|---|
|  |  |

## Verification

| Check | Result |
|---|---|
| Full test suite | <!-- e.g. 559 passed / 0 failed --> |
| New tests added | <!-- path, or "none needed because ..." --> |
| Manual check | <!-- what you actually ran / clicked --> |

## Guard rails (all must be true)

- [ ] Branch only — `main` was never pushed to, nothing was force-pushed
- [ ] No secret, token, credential or real customer data in the diff
- [ ] No protected path touched (`auth`, `authz`, `security`, workflows, `.env`, guard scripts)
- [ ] No deploy, no Google API call, no review published
- [ ] AI cost recorded on the issue

## Rollback

<!-- how to undo: revert this commit / drop the branch; any migration note -->

## Reviewer notes

<!-- anything you want the human to look at first, or a reason to reject -->
