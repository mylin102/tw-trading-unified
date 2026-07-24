# Rule: Deployment Identity Rule

## 1. Core Mandate

1. Before modifying, running, restarting, or deploying the trading system, read `<repo-root>/.deployment-target`.

2. The deployment identity file is the sole authority for host role. Do not infer Air4 or Mini from hostname, shell prompt, SSH alias, CWD name, PM2 process names, or prior conversation context.

3. If `.deployment-target` is missing, malformed, unsupported, or inconsistent with the resolved repository path, stop all mutating and runtime actions. Read-only diagnostics are allowed.

4. Every execution report must include:
   - `deployment_id`
   - `instance_id`
   - `hostname`
   - `resolved repo root`
   - `git commit`
   - `git dirty status`
   - `ticker`
   - `execution mode`

5. Never copy, commit, sync, or overwrite `.deployment-target` between hosts. It must be listed in `.gitignore`.

6. Before restart or deployment, verify that the target `deployment_id` matches the user-requested host. If the target is not explicitly specified, do not deploy to another host.

7. A process may start only when its ticker and execution mode are permitted by the identity file.

8. If another active instance holds the same instance lock, fail closed with `DUPLICATE_RUNTIME_INSTANCE`.

9. Hostname is diagnostic metadata only. It must never override `.deployment-target`.

10. Any deployment action must report the exact files changed locally and the exact host on which the change occurred.
