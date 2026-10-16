# Agent Git Identity — Instructions for Parallel Agents

> Read this if you are an AI agent committing to this repository
> (Gemini CLI, Antigravity, or any other tool).

## Why

Multiple agents commit to this repo under the same OS user (`myllin`), so
`git author` cannot distinguish who made which change. A `commit-msg` hook
now **requires** every commit to carry an agent identity.

## What you MUST do before every commit

```bash
export AGENT_NAME=gemini        # <-- your identity (see allowed list below)
```

Then commit normally:

```bash
git commit -m "your message"
```

The hook will append `Provenance: committed-by=gemini` to the message
automatically.

## Allowed identities

| AGENT_NAME   | Who                            |
|--------------|--------------------------------|
| `hermes`     | Hermes Agent (Nous Research)   |
| `gemini`     | Gemini CLI                     |
| `antigravity`| Antigravity CLI                |
| `human`      | Manual operator                |

## If you forget

```
ERROR: AGENT_NAME not set — refusing commit without agent provenance.
       Export your identity first, e.g.:  export AGENT_NAME=gemini
       (Your message is preserved in .git/COMMIT_EDITMSG)
```

Your commit message is kept in `.git/COMMIT_EDITMSG` — export AGENT_NAME and
retry (`git commit` again reuses it).

## How to set it persistently for your session

At the start of each working session (or in your tool's environment setup):

```bash
export AGENT_NAME=gemini
```

If your environment doesn't persist shell exports, set it inline per command:

```bash
AGENT_NAME=gemini git commit -m "..."
```

## Reinstalling the hook (if missing)

The hook lives in `.git/hooks/commit-msg` and is NOT tracked by git.
After a fresh clone or worktree, install it from the repo:

```bash
bash scripts/install_commit_msg_hook.sh
```

The hook source is `scripts/commit-msg.hook`.

## Audit

Anyone can see who committed what:

```bash
git log --format='%h %s%n%b' | grep Provenance
```
