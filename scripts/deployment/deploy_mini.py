#!/usr/bin/env python3
"""2026-10-12 Codex: one explicit, fail-closed Mini PAPER deployment transaction.

See docs/operations/deploy-mini.md. Default is read-only planning. Parameters
never invoke PM2 or move Git HEAD. Releases require maintenance approval and
120 seconds of verification before saving/unlocking. LIVE continues to use
deployment/deploy_release.sh with its existing broker/certification gates.
No broker imports, orders, state rewrites, flattening, stash, or auto rollback.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from core.futures_profiles import validate_paper_profile, write_paper_profile


class DeployError(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise DeployError(reason)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def one(rows, name):
    matches = [r for r in rows if r.get("name") == name]
    require(len(matches) == 1, "AMBIGUOUS_APP:" + name)
    return matches[0]


def brief(row):
    env = row["pm2_env"]
    return {"pid": row["pid"], "status": env["status"], "cwd": env["pm_cwd"],
            "sha": env.get("LRC_RELEASE_SHA"), "restarts": env.get("restart_time", 0)}


def verify_app(row, root, sha, runtime):
    env = row["pm2_env"]
    require(env.get("status") == "online" and row.get("pid", 0) > 0, "APP_NOT_ONLINE")
    require(Path(env["pm_cwd"]).resolve() == root.resolve(), "APP_CWD_MISMATCH")
    require(env.get("LRC_RELEASE_SHA") == sha, "APP_SHA_MISMATCH:" + row["name"])
    require(Path(env.get("TRADING_CANONICAL_ROOT", "")).resolve() == root.resolve(),
            "APP_CANONICAL_ROOT_MISMATCH")
    require(Path(env.get("TRADING_RUNTIME_DIR", "")).resolve() == runtime.resolve(),
            "APP_RUNTIME_ROOT_MISMATCH")


def updated_width(body, width):
    require(not isinstance(width, bool) and isinstance(width, (int, float))
            and math.isfinite(width) and width > 0, "INVALID_WIDTH")
    cfg = validate_paper_profile(body)
    # Deliberately narrow: this command changes only the requested quote-width.
    require(isinstance(cfg.get("mts", {}).get("params"), dict), "MISSING_MTS_PARAMS")
    cfg["mts"]["params"]["max_spread_width"] = width
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False).encode()


def execute(args, ops):
    require(args.mode == "paper", "LIVE_USE_EXISTING_CERTIFIED_DEPLOY_RELEASE")
    if args.apply and args.operation == "release":
        require(args.maintenance_approved, "MAINTENANCE_APPROVAL_REQUIRED")
    ops.preflight()
    if not args.apply:
        ops.plan()
        return
    ops.lock()
    ops.backup()
    if args.operation == "release":
        if getattr(ops, "resume_verify_only", False) is not True:
            ops.stop()
            ops.install()
            ops.start()
    else:
        ops.publish_params()
    ops.observe()
    if args.operation == "release":
        ops.save()
    ops.unlock()
    ops.complete()


class MiniDeployment:
    def __init__(self, args):
        self.args = args
        self.site = read_json(args.site)
        for key in ("mts_root", "dashboard_root", "runtime_dir", "python_bin", "pm2_bin",
                    "pm2_home", "launchd_plist"):
            require(Path(self.site[key]).is_absolute(), "SITE_PATH_MUST_BE_ABSOLUTE:" + key)
        self.roots = {"trading-system": Path(self.site["mts_root"]).resolve(),
                      "dashboard": Path(self.site["dashboard_root"]).resolve()}
        # 2026-10-12 Codex: explicit deployment scope, not a dirty-source bypass.
        self.engine_only = bool(getattr(args, "engine_only", False))
        require(not self.engine_only or (args.operation == "release"
                and self.roots["trading-system"] != self.roots["dashboard"]),
                "ENGINE_ONLY_REQUIRES_SEPARATE_ROOTS")
        self.managed = ("trading-system",) if self.engine_only else tuple(self.roots)
        self.runtime = Path(self.site["runtime_dir"]).resolve()
        require(all(self.runtime != r and r not in self.runtime.parents for r in self.roots.values()),
                "RUNTIME_MUST_BE_OUTSIDE_SOURCE")
        self.profile = self.runtime / "config/futures.paper.yaml"
        self.entry_lock = self.roots["trading-system"] / "data/maintenance_entry_lock.flag"
        self.token = "codex-" + uuid.uuid4().hex
        self.lock_token = self.token
        self.resumed_from = None
        self.continued_from = None
        self.resume_verify_only = False
        self.resume_startup_verified = False
        self.resume_failed_at = None
        self.out = self.runtime / "logs/deployments" / self.token
        self.stage = "init"
        self.owns_lock = False
        self.env = dict(os.environ, PM2_HOME=self.site["pm2_home"],
                        TRADING_RUNTIME_DIR=str(self.runtime), AGENT_NAME="codex")

    def command(self, argv, *, root=None, env=None, body=None):
        result = subprocess.run(argv, cwd=root, env=env or self.env, input=body,
                                capture_output=True, timeout=180)
        if result.returncode:
            # Do not print process environments or command output (may contain credentials).
            raise DeployError("COMMAND_FAILED:" + Path(argv[0]).name)
        return result.stdout

    def git(self, root, *args):
        return self.command(["git", "-C", str(root), *args]).decode().strip()

    def apps(self):
        return json.loads(self.command([self.site["pm2_bin"], "jlist"]))

    def pm2(self, *args, env=None, root=None):
        return self.command([self.site["pm2_bin"], *args], env=env, root=root)

    def context(self):
        ctx = read_json(self.runtime / "execution_context.json")
        require(ctx.get("requested_mode") == "paper" and ctx.get("effective_mode") == "paper_active"
                and ctx.get("live_order_allowed") is False and ctx.get("state_namespace") == "paper",
                "NOT_ACTIVE_PAPER")
        return ctx

    def record(self, name, value):
        with (self.out / name).open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())

    def preserved_dashboard_data(self):
        # 2026-10-12 Codex: only existing generated CSV differences are allowed;
        # source, staged edits, deletions and concurrent data changes fail closed.
        root = self.roots["dashboard"]
        require(not self.git(root, "diff", "--cached", "--name-only"), "STAGED_DASHBOARD_SOURCE")
        paths = self.git(root, "diff", "HEAD", "--name-only").splitlines()
        require(not self.git(root, "diff", "HEAD", "--name-only", "--diff-filter=D"),
                "DELETED_DASHBOARD_DATA")
        require(all(p.startswith("data/taifex_raw/") and p.endswith(".csv") for p in paths),
                "DIRTY_DASHBOARD_SOURCE")
        return {p: digest((root / p).read_bytes()) for p in paths}

    def preflight(self):
        self.stage = "preflight"
        require(self.runtime.is_dir(), "RUNTIME_MISSING")
        resume_id = getattr(self.args, "resume_failed_transaction", None)
        resume_started_id = getattr(self.args, "resume_start_failed_transaction", None)
        require(not (resume_id and resume_started_id), "RESUME_OPTIONS_MUTUALLY_EXCLUSIVE")
        if resume_id is None and resume_started_id is None:
            require(not self.entry_lock.exists(), "EXISTING_ENTRY_LOCK_PRESERVED")
        else:
            require(self.args.apply and self.args.operation == "release" and self.engine_only,
                    "RESUME_REQUIRES_APPLIED_ENGINE_ONLY_RELEASE")
        self.heads = {}
        self.targets = {}
        # Identity is checked BEFORE contacting any PM2 daemon.
        for name, root in self.roots.items():
            target = read_json(root / ".deployment-target")
            require(target.get("deployment_id") == "mini" and target.get("host_role") == "runtime-primary"
                    and target.get("instance_id") and "paper" in target.get("execution_modes", [])
                    and Path(target.get("repo_path", "")).resolve() == root,
                    "DEPLOYMENT_TARGET_MISMATCH:" + name)
            self.targets[name] = target
        if resume_id is not None:
            self.validate_resume_lock(resume_id)
        elif resume_started_id is not None:
            self.validate_started_failure(resume_started_id)
        self.before = self.apps()
        for name, root in self.roots.items():
            if self.engine_only and name == "dashboard":
                self.dashboard_data = self.preserved_dashboard_data()
            else:
                require(not self.git(root, "status", "--porcelain", "--untracked-files=no"),
                        "DIRTY_TRACKED_SOURCE:" + name)
            untracked = self.git(root, "ls-files", "--others", "--exclude-standard").splitlines()
            require(all(p.startswith(("data/", "logs/")) for p in untracked), "UNTRACKED_SOURCE:" + name)
            self.heads[name] = self.git(root, "rev-parse", "HEAD")
            verify_app(one(self.before, name), root, self.heads[name], self.runtime)
        require("live_runtime" in self.targets["trading-system"].get("allowed_operations", []),
                "ENGINE_RUNTIME_CAPABILITY_MISSING")
        require(one(self.before, "trading-system")["pm2_env"].get("TRADING_FUTURES_MODE") == "paper",
                "ENGINE_MODE_MISMATCH")
        self.old_context = self.context()
        baseline = self.roots["trading-system"] / "config/futures.yaml"
        active = self.profile if self.profile.exists() else baseline
        require(self.old_context["config_hash"] == digest(active.read_bytes()), "CURRENT_CONFIG_HASH_MISMATCH")
        self.old_body = active.read_bytes()
        self.ticker = validate_paper_profile(self.old_body)["ticker"]
        require(all(t.get("ticker", self.ticker) == self.ticker for t in self.targets.values()),
                "TARGET_PRODUCT_MISMATCH")
        self.shas = dict(self.heads)
        if self.args.operation == "params":
            require(self.profile.exists(), "INSTALL_SHARED_PROFILE_WITH_RELEASE_FIRST")
            self.body = updated_width(self.old_body, self.args.max_spread_width)
        else:
            for name, sha in (("trading-system", self.args.mts_sha), ("dashboard", self.args.dashboard_sha)):
                require(isinstance(sha, str) and re.fullmatch("[0-9a-f]{40}", sha), "FULL_CANDIDATE_SHA_REQUIRED")
                require(not self.engine_only or name != "dashboard" or sha == self.heads[name],
                        "ENGINE_ONLY_DASHBOARD_SHA_MUST_STAY")
                root = self.roots[name]
                require(self.git(root, "rev-parse", sha + "^{commit}") == sha, "CANDIDATE_NOT_AVAILABLE")
                self.git(root, "merge-base", "--is-ancestor", self.heads[name], sha)
                self.git(root, "cat-file", "-e", sha + ":core/futures_profiles.py")
                self.shas[name] = sha
            if self.resume_verify_only:
                require(self.heads["trading-system"] == self.shas["trading-system"],
                        "RESUME_STARTED_SOURCE_NOT_AT_CANDIDATE")
                row = one(self.before, "trading-system")
                require(row["pm2_env"].get("LRC_RELEASE_SHA") == self.shas["trading-system"],
                        "RESUME_STARTED_PROCESS_NOT_AT_CANDIDATE")
            dash_root = self.roots["dashboard"]
            dash_cfg = self.command(["git", "-C", str(dash_root), "show",
                                     self.shas["dashboard"] + ":config/futures.yaml"])
            validate_paper_profile(dash_cfg, ticker=self.ticker)
            if self.roots["trading-system"] == self.roots["dashboard"]:
                require(self.shas["trading-system"] == self.shas["dashboard"], "SAME_ROOT_DIFFERENT_SHA")
            for row in self.before:
                if row["name"] not in self.roots:
                    for name, root in self.roots.items():
                        require(Path(row["pm2_env"]["pm_cwd"]).resolve() != root
                                or self.shas[name] == self.heads[name], "OTHER_APP_SHARES_CHANGED_SOURCE")
            root = self.roots["trading-system"]
            sha = self.shas["trading-system"]
            freeze = self.git(root, "show", sha + ":PHASE1_FINAL_FREEZE.md")
            match = re.search(r"^frozen_tree_hash:\s*([0-9a-f]{64})$", freeze, re.M)
            excluded = {"PHASE1_RC_CANDIDATE.md", "PHASE2_DEPLOYMENT_MANIFEST.md", "PHASE1_FINAL_FREEZE.md"}
            tree = "\n".join(ln for ln in self.git(root, "ls-tree", "-r", sha).splitlines()
                             if ln.split("\t")[-1] not in excluded)
            require(match and match[1] == digest(tree.encode()), "CANDIDATE_FREEZE_STALE")
            self.body = self.command(["git", "-C", str(root), "show", sha + ":config/futures.yaml"])
            candidate = validate_paper_profile(self.body, ticker=self.ticker)
            if self.profile.exists():
                old_cfg = validate_paper_profile(self.old_body, ticker=self.ticker)
                committed = yaml.safe_load(baseline.read_bytes())
                old_width = old_cfg["mts"]["params"]["max_spread_width"]
                old_cfg["mts"]["params"]["max_spread_width"] = committed["mts"]["params"]["max_spread_width"]
                require(old_cfg == committed, "OTHER_RUNTIME_PARAMETERS_REQUIRE_REVIEW")
                candidate["mts"]["params"]["max_spread_width"] = old_width
                self.body = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False).encode()
            # Validate the candidate canonical ecosystem BEFORE stopping any process.
            ecosystem = self.command(["git", "-C", str(root), "show", sha + ":ecosystem.config.js"])
            js = ("const M=require('module');const p=require('path');"
                  "const f=p.join(process.cwd(),'ecosystem.config.js');"
                  "const m=new M(f);m.filename=f;m.paths=M._nodeModulePaths(process.cwd());"
                  "m._compile(require('fs').readFileSync(0,'utf8'),f);"
                  "const a=m.exports.apps.filter(a=>a.name==='trading-system');"
                  "if(a.length!==1||a[0].cwd!==process.cwd())process.exit(2);")
            self.command(["node", "-e", js], root=root, env=self.app_env("trading-system"), body=ecosystem)
        self.before_protected = {r["name"]: brief(r) for r in self.before if r["name"] not in self.managed}
        self.initial_ledger = self.ledger_snapshot()
        self.initial_state = read_json(self.runtime / "paper/data/state/mts_position_state.json")
        self.check_position(self.initial_state, self.initial_ledger)
        self.check_single_engine(one(self.before, "trading-system")["pid"])
        dash_args = one(self.before, "dashboard")["pm2_env"].get("args", [])
        words = shlex.split(dash_args) if isinstance(dash_args, str) else dash_args
        port = next((w.split("=", 1)[1] for w in words if w.startswith("--server.port=")), None)
        if port is None and "--server.port" in words:
            port = words[words.index("--server.port") + 1]
        require(port and str(port).isdigit(), "DASHBOARD_PORT_UNKNOWN")
        self.health_url = "http://127.0.0.1:" + str(port) + "/_stcore/health"
        self.log = Path(one(self.before, "trading-system")["pm2_env"]["pm_out_log_path"])
        self.log_inode, self.log_offset = self.log.stat().st_ino, self.log.stat().st_size

    def validate_resume_lock(self, transaction_id):
        """Adopt only our exact retained observe-failure lock; never unlock it here."""
        require(isinstance(transaction_id, str)
                and re.fullmatch(r"codex-[0-9a-f]{32}", transaction_id),
                "RESUME_TRANSACTION_ID_INVALID")
        require(self.entry_lock.is_file() and not self.entry_lock.is_symlink()
                and self.entry_lock.read_text(encoding="utf-8") == transaction_id,
                "RESUME_ENTRY_LOCK_OWNER_MISMATCH")
        deployment_root = (self.runtime / "logs/deployments").resolve()
        receipt = deployment_root / transaction_id
        require(not receipt.is_symlink() and receipt.resolve().parent == deployment_root,
                "RESUME_RECEIPT_PATH_INVALID")
        failed_path = receipt / "failed.json"
        before_path = receipt / "before.json"
        require(failed_path.is_file() and not failed_path.is_symlink()
                and before_path.is_file() and not before_path.is_symlink(),
                "RESUME_RECEIPT_INCOMPLETE")
        require(not (receipt / "complete.json").exists()
                and not (receipt / "observed.json").exists(),
                "RESUME_RECEIPT_ALREADY_COMPLETED")
        failed = read_json(failed_path)
        before = read_json(before_path)
        require(failed.get("stage") == "observe" and failed.get("entry_lock_retained") is True,
                "RESUME_FAILURE_STAGE_NOT_ADOPTABLE")
        require(before.get("operation") == "release" and before.get("mode") == "paper"
                and before.get("managed_apps") == ["trading-system"],
                "RESUME_PRIOR_SCOPE_MISMATCH")
        prior_heads = before.get("heads")
        prior_pm2 = before.get("pm2")
        require(isinstance(prior_heads, dict) and set(prior_heads) == set(self.roots)
                and isinstance(prior_pm2, dict) and set(self.roots).issubset(prior_pm2),
                "RESUME_PRIOR_RECEIPT_ROOTS_MISMATCH")
        old_targets = before.get("deployment_targets")
        require(isinstance(old_targets, dict) and set(old_targets) == set(self.targets),
                "RESUME_PRIOR_TARGETS_MISMATCH")
        for name, root in self.roots.items():
            prior_app = prior_pm2[name]
            require(isinstance(prior_app, dict)
                    and Path(prior_app.get("cwd", "")).resolve() == root
                    and prior_app.get("sha") == prior_heads[name]
                    and prior_app.get("status") == "online"
                    and isinstance(prior_app.get("pid"), int)
                    and prior_app["pid"] > 0,
                    "RESUME_PRIOR_PROCESS_IDENTITY_MISMATCH:" + name)
            prior = old_targets[name]
            current = self.targets[name]
            require(prior.get("deployment_id") == "mini"
                    and prior.get("host_role") == "runtime-primary"
                    and prior.get("instance_id") == current.get("instance_id")
                    and Path(prior.get("repo_path", "")).resolve() == root,
                    "RESUME_TARGET_IDENTITY_MISMATCH:" + name)
        self.resumed_from = transaction_id
        self.lock_token = transaction_id

    def validate_started_failure(self, transaction_id):
        """Continue verification only for an exact retained-lock start failure."""
        require(isinstance(transaction_id, str)
                and re.fullmatch(r"codex-[0-9a-f]{32}", transaction_id),
                "RESUME_START_TRANSACTION_ID_INVALID")
        deployment_root = (self.runtime / "logs/deployments").resolve()
        receipt = deployment_root / transaction_id
        require(not receipt.is_symlink() and receipt.resolve().parent == deployment_root,
                "RESUME_START_RECEIPT_PATH_INVALID")
        failed_path, before_path = receipt / "failed.json", receipt / "before.json"
        require(failed_path.is_file() and not failed_path.is_symlink()
                and before_path.is_file() and not before_path.is_symlink(),
                "RESUME_START_RECEIPT_INCOMPLETE")
        require(not (receipt / "complete.json").exists()
                and not (receipt / "observed.json").exists(),
                "RESUME_START_RECEIPT_ALREADY_COMPLETED")
        failed, before = read_json(failed_path), read_json(before_path)
        parent_id = failed.get("resumed_from")
        require(failed.get("stage") == "start" and failed.get("entry_lock_retained") is True
                and isinstance(parent_id, str), "RESUME_FAILURE_STAGE_NOT_START")
        self.validate_resume_lock(parent_id)
        require(before.get("operation") == "release" and before.get("mode") == "paper"
                and before.get("managed_apps") == ["trading-system"],
                "RESUME_START_SCOPE_MISMATCH")
        before_heads = before.get("heads")
        before_pm2 = before.get("pm2")
        before_targets = before.get("deployment_targets")
        require(isinstance(before_heads, dict) and set(before_heads) == set(self.roots)
                and isinstance(before_pm2, dict) and set(self.roots).issubset(before_pm2)
                and isinstance(before_targets, dict) and set(before_targets) == set(self.targets),
                "RESUME_START_RECEIPT_ROOTS_MISMATCH")
        for name, root in self.roots.items():
            prior_app = before_pm2[name]
            require(isinstance(prior_app, dict)
                    and Path(prior_app.get("cwd", "")).resolve() == root
                    and prior_app.get("sha") == before_heads[name]
                    and prior_app.get("status") == "online"
                    and isinstance(prior_app.get("pid"), int) and prior_app["pid"] > 0,
                    "RESUME_START_PRIOR_PROCESS_IDENTITY_MISMATCH:" + name)
            prior = before_targets[name]
            current = self.targets[name]
            require(prior.get("deployment_id") == "mini"
                    and prior.get("host_role") == "runtime-primary"
                    and prior.get("instance_id") == current.get("instance_id")
                    and Path(prior.get("repo_path", "")).resolve() == root,
                    "RESUME_START_TARGET_IDENTITY_MISMATCH:" + name)
        self.resume_verify_only = True
        self.continued_from = transaction_id
        self.resume_failed_at = failed_path.stat().st_mtime

    def plan(self):
        print(json.dumps({"status": "PLAN_ONLY_NOT_DEPLOYED", "operation": self.args.operation,
                          "mode": "paper", "roots": self.roots, "before": self.heads,
                          "after": self.shas, "managed_apps": self.managed,
                          "config_hash": digest(self.body)}, default=str))

    def app_env(self, name):
        old = one(self.before, name)["pm2_env"]
        env = dict(self.env, NODE_ENV="production", TRADING_FUTURES_MODE="paper",
                   TRADING_PYTHON_BIN=self.site["python_bin"], PYTHONPATH=str(self.roots[name]),
                   TRADING_CANONICAL_ROOT=str(self.roots[name]), LRC_RELEASE_SHA=self.shas[name])
        for key in ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY", "SHIOAJI_CA_PATH", "SHIOAJI_CA_PASSWD", "TRADING_RUNTIME_ID"):
            if name == "trading-system":
                require(isinstance(old.get(key), str) and old[key], "MISSING_START_ENV:" + key)
            if old.get(key):
                env[key] = old[key]
        return env

    def lock(self):
        self.stage = "lock"
        self.entry_lock.parent.mkdir(parents=True, exist_ok=True)
        if self.resumed_from is not None:
            require(self.entry_lock.is_file() and not self.entry_lock.is_symlink()
                    and self.entry_lock.read_text(encoding="utf-8") == self.lock_token,
                    "RESUME_ENTRY_LOCK_OWNER_CHANGED")
        else:
            with self.entry_lock.open("x") as handle:
                handle.write(self.lock_token)
                handle.flush()
                os.fsync(handle.fileno())
        self.owns_lock = True
        self.out.mkdir(parents=True, mode=0o700)

    def backup(self):
        self.stage = "backup"
        # 2026-10-12 Codex: fail if another operator changed ownership since preflight.
        rows = self.apps()
        require({r["name"]: brief(r) for r in rows} == {r["name"]: brief(r) for r in self.before},
                "PROCESS_CHANGED_AFTER_PREFLIGHT")
        require(self.context()["config_hash"] == self.old_context["config_hash"], "CONTEXT_CHANGED_AFTER_PREFLIGHT")
        # Runtime state is read for evidence only; never restored/rewritten here.
        self.record("before.json", {"heads": self.heads, "pm2": {r["name"]: brief(r) for r in self.before},
                                    "position": self.initial_state, "ledger": self.initial_ledger,
                                    "config_hash": digest(self.old_body), "operation": self.args.operation,
                                    "hostname": socket.gethostname(), "deployment_targets": self.targets,
                                    "managed_apps": self.managed,
                                    "resumed_from": self.resumed_from,
                                    "preserved_dashboard_data": getattr(self, "dashboard_data", {}),
                                    "ticker": self.ticker, "mode": "paper"})
        (self.out / "paper-profile.before.yaml").write_bytes(self.old_body)

    def protected(self, rows):
        require({r["name"]: brief(r) for r in rows if r["name"] not in self.managed}
                == self.before_protected, "UNRELATED_PM2_APP_CHANGED")

    def stop(self):
        self.stage = "stop"
        for name in ("dashboard", "trading-system"):
            if name in self.managed:
                self.pm2("stop", name)
        deadline = time.monotonic() + 40
        while True:
            rows = self.apps()
            gone = all(one(rows, n)["pm2_env"]["status"] == "stopped" and not one(rows, n)["pid"] for n in self.managed)
            old_alive = []
            for name in self.managed:
                try:
                    os.kill(one(self.before, name)["pid"], 0)
                    old_alive.append(name)
                except ProcessLookupError:
                    pass
            if gone and not old_alive:
                break
            require(time.monotonic() < deadline, "OLD_PROCESS_DID_NOT_STOP")
            time.sleep(1)
        self.protected(rows)
        self.stopped_at = time.monotonic()

    def install(self):
        self.stage = "install"
        for root in {self.roots[n] for n in self.managed}:
            name = next(n for n, r in self.roots.items() if r == root)
            require(self.git(root, "rev-parse", "HEAD") == self.heads[name], "HEAD_DRIFT")
            require(not self.git(root, "status", "--porcelain", "--untracked-files=no"), "SOURCE_DRIFT")
            self.git(root, "merge", "--ff-only", self.shas[name])
        write_paper_profile(self.profile, self.body, ticker=self.ticker, runtime_dir=self.runtime)

    def start(self):
        self.stage = "start"
        # Shared broker market-data library needs a quiet interval after shutdown.
        time.sleep(max(0, 15 - (time.monotonic() - self.stopped_at)))
        self.pm2("delete", "trading-system")
        self.pm2("start", "ecosystem.config.js", "--only", "trading-system",
                 root=self.roots["trading-system"], env=self.app_env("trading-system"))
        if "dashboard" in self.managed:
            self.pm2("restart", "dashboard", "--update-env", env=self.app_env("dashboard"))

    def publish_params(self):
        self.stage = "publish_params"
        require(self.profile.read_bytes() == self.old_body, "PROFILE_CONCURRENT_EDIT")
        write_paper_profile(self.profile, self.body, ticker=self.ticker, runtime_dir=self.runtime)

    def ledger_snapshot(self):
        from dataclasses import asdict
        from strategies.futures.mts_ledger_authority import project_fills
        path = self.runtime / "paper/logs/mts_trade_fills.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        # Never mix live/legacy rows into PAPER restoration evidence.
        require(all(isinstance(r, dict) and r.get("mode") == "paper" for r in rows), "NON_PAPER_LEDGER_ROW")
        projected = project_fills(rows, source="PAPER")
        value = asdict(projected)
        value["status"] = projected.status.value
        require(value["status"] in ("FLAT", "OPEN", "SINGLE_LEG") and not value["anomalies"],
                "PAPER_LEDGER_NOT_UNAMBIGUOUS")
        return value

    @staticmethod
    def check_position(state, ledger):
        if ledger["status"] == "FLAT":
            require(state.get("has_position") is False, "PAPER_FLAT_STATE_DIVERGENCE")
            return
        require(state.get("has_position") is True and state.get("trade_id") == ledger["trade_id"],
                "PAPER_TRADE_STATE_DIVERGENCE")
        for leg in ("near", "far"):
            if ledger[leg + "_qty"]:
                require(state.get(leg + "_side") == ledger[leg + "_side"], "PAPER_SIDE_DIVERGENCE")
                require(state.get(leg + "_entry") == ledger[leg + "_entry"], "PAPER_ENTRY_DIVERGENCE")

    def check_single_engine(self, pid):
        lines = self.command(["/bin/ps", "-axo", "pid=,args="]).decode().splitlines()
        engines = []
        for line in lines:
            fields = line.strip().split(None, 1)
            if len(fields) != 2:
                continue
            try:
                words = shlex.split(fields[1])
            except ValueError:
                continue
            if (words and Path(words[0]).name.lower().startswith(("python", "taskpolicy"))
                    and any(Path(word).name == "main.py" for word in words)):
                engines.append(int(fields[0]))
        require(engines == [pid], "DUPLICATE_OR_MISSING_ENGINE")

    def verify(self):
        rows = self.apps()
        self.protected(rows)
        for name, root in self.roots.items():
            verify_app(one(rows, name), root, self.shas[name], self.runtime)
            require(self.git(root, "rev-parse", "HEAD") == self.shas[name], "SOURCE_SHA_DRIFT")
            if self.engine_only and name == "dashboard":
                require(self.preserved_dashboard_data() == self.dashboard_data, "PRESERVED_DASHBOARD_DATA_CHANGED")
            else:
                require(not self.git(root, "status", "--porcelain", "--untracked-files=no"), "DIRTY_DEPLOYED_SOURCE")
        pid = one(rows, "trading-system")["pid"]
        self.check_single_engine(pid)
        args = one(rows, "trading-system")["pm2_env"].get("args", [])
        words = shlex.split(args) if isinstance(args, str) else args
        require(words == ["-c", "background", self.site["python_bin"], "main.py", "--config", "futures"],
                "ENGINE_INTERPRETER_OR_ARGS_MISMATCH")
        ctx = self.context()
        require(ctx["config_hash"] == digest(self.body) == digest(self.profile.read_bytes()), "APPLIED_CONFIG_HASH_MISMATCH")
        state_path = self.runtime / "paper/data/state/mts_position_state.json"
        state, ledger = read_json(state_path), self.ledger_snapshot()
        self.check_position(state, ledger)
        require(state.get("heartbeat_pid") == pid or state.get("writer_identity") == "mts_spread:" + str(pid),
                "STATE_WRITER_MISMATCH")
        from core.date_utils import is_taifex_futures_market_open
        market_open = is_taifex_futures_market_open()
        if market_open:
            require(time.time() - state_path.stat().st_mtime < 15, "STALE_OPEN_SESSION_STATE")
        safety = read_json(self.runtime / "data/state/trading_safety_state.json")
        require(safety.get("pid") == pid and safety.get("entry_allowed") is True
                and safety.get("account_health") == "HEALTHY", "PAPER_SAFETY_NOT_READY")
        with urllib.request.urlopen(self.health_url, timeout=5) as response:
            require(response.status == 200, "DASHBOARD_HTTP_NOT_READY")
        # Exercise the deployed dashboard's real classifier, not just HTTP liveness.
        source = self.roots["dashboard"] / "ui/dashboard.py"
        nodes = [n for n in ast.parse(source.read_text()).body if isinstance(n, ast.FunctionDef)
                 and n.name in ("load_active_runtime_truth", "summarize_execution_context")]
        require(len(nodes) == 2, "DASHBOARD_CLASSIFIER_MISSING")
        ns = {}
        prior = os.environ.get("TRADING_RUNTIME_DIR")
        os.environ["TRADING_RUNTIME_DIR"] = str(self.runtime)
        try:
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)
            truth = ns["load_active_runtime_truth"](self.roots["dashboard"])
        finally:
            if prior is None:
                os.environ.pop("TRADING_RUNTIME_DIR", None)
            else:
                os.environ["TRADING_RUNTIME_DIR"] = prior
        require(truth.get("is_paper_runtime") and not truth.get("warning"), "DASHBOARD_PROFILE_NOT_READY")
        if self.args.operation == "params":
            require(all(brief(one(rows, name)) == brief(one(self.before, name)) for name in self.roots),
                    "PARAMETERS_RESTARTED_PROCESS")
        else:
            engine = one(rows, "trading-system")
            if self.resume_verify_only:
                started_at = engine["pm2_env"].get("pm_uptime")
                require(isinstance(started_at, (int, float))
                        and started_at / 1000 >= self.resume_failed_at - 2
                        and self.log.stat().st_mtime >= self.resume_failed_at - 2,
                        "RESUME_ENGINE_NOT_STARTED_AFTER_FAILURE")
            if self.resume_verify_only and self.resume_startup_verified:
                body = ""
            else:
                with self.log.open("rb") as handle:
                    if self.resume_verify_only:
                        # A healthy long-running PAPER process can produce enough
                        # output to push its startup acknowledgements out of a
                        # fixed tail window. Scan the log for an acknowledgement
                        # block whose nearby ISO timestamp matches this exact
                        # post-failure PM2 process start instead of accepting an
                        # arbitrary historical startup marker.
                        handle.seek(0)
                    elif self.log.stat().st_ino == self.log_inode:
                        handle.seek(self.log_offset)
                    body = handle.read().decode(errors="replace")
            if self.resume_verify_only:
                if not self.resume_startup_verified:
                    lines = body.splitlines()
                    started_epoch = started_at / 1000
                    current_startup = None
                    for index, line in enumerate(lines):
                        if "PAPER_STARTUP_READY: local simulation" not in line:
                            continue
                        window = lines[index:index + 256]
                        acks = {(bool(far), kind) for far, kind in re.findall(
                            r"Subscribed (far-month )?" + re.escape(self.ticker)
                            + r" (tick|bidask):", "\n".join(window))}
                        if len(acks) != 4:
                            continue
                        timestamps = re.findall(
                            r"20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
                            r"(?:Z|[+-]\d{2}:\d{2})?", "\n".join(window))
                        for value in timestamps:
                            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(tzinfo=timezone.utc)
                            event_epoch = parsed.timestamp()
                            if started_epoch - 10 <= event_epoch <= started_epoch + 180:
                                current_startup = window
                                break
                        if current_startup is not None:
                            break
                    require(current_startup is not None, "PAPER_STARTUP_ACK_MISSING")
                    require("Traceback (most recent call last)" not in "\n".join(current_startup),
                            "STARTUP_TRACEBACK")
                    self.resume_startup_verified = True
            else:
                require("PAPER_STARTUP_READY: local simulation" in body, "PAPER_STARTUP_ACK_MISSING")
                acks = {(bool(far), kind) for far, kind in re.findall(
                    r"Subscribed (far-month )?" + re.escape(self.ticker) + r" (tick|bidask):", body)}
                require(len(acks) == 4, "FOUR_MARKET_DATA_SUBSCRIPTIONS_MISSING")
                require("Traceback (most recent call last)" not in body, "STARTUP_TRACEBACK")
        return {"apps": {n: brief(one(rows, n)) for n in self.roots}, "ledger": ledger,
                "config_hash": ctx["config_hash"], "mode": "paper_active", "market_open": market_open,
                "time": time.time()}

    def observe(self):
        self.stage = "observe"
        deadline = time.monotonic() + 150
        while True:
            try:
                first = self.verify()
                break
            except (DeployError, OSError, ValueError):
                require(time.monotonic() < deadline, "STARTUP_OR_CONFIG_APPLY_TIMEOUT")
                time.sleep(3)
        # Parameters are verified in-place; releases require a full stable window.
        duration = 120 if self.args.operation == "release" else 10
        started = time.monotonic()
        samples = [first]
        while time.monotonic() - started < duration:
            time.sleep(5)
            sample = self.verify()
            require(sample["apps"] == first["apps"], "PROCESS_CHANGED_DURING_OBSERVATION")
            samples.append(sample)
            print("[OBSERVE] verified PAPER; elapsed=" + str(int(time.monotonic() - started)), flush=True)
        self.record("observed.json", {"samples": samples, "stable_seconds": time.monotonic() - started})

    def save(self):
        self.stage = "save"
        self.verify()
        self.pm2("save")
        rows = read_json(Path(self.site["pm2_home"]) / "dump.pm2")
        for name, root in self.roots.items():
            env = one(rows, name)
            require(env.get("LRC_RELEASE_SHA") == self.shas[name] and env.get("pm_cwd") == str(root),
                    "PM2_DUMP_IDENTITY_MISMATCH")
            require(env.get("TRADING_CANONICAL_ROOT") == str(root)
                    and env.get("TRADING_RUNTIME_DIR") == str(self.runtime), "PM2_DUMP_ROOT_MISMATCH")
            if name == "trading-system":
                require(env.get("TRADING_FUTURES_MODE") == "paper", "PM2_DUMP_MODE_MISMATCH")
        with Path(self.site["launchd_plist"]).open("rb") as handle:
            daemon = plistlib.load(handle)
        require(daemon.get("EnvironmentVariables", {}).get("PM2_HOME") == self.site["pm2_home"],
                "LAUNCHD_PM2_HOME_MISMATCH")

    def unlock(self):
        self.stage = "unlock"
        require(self.owns_lock and self.entry_lock.read_text() == self.lock_token, "ENTRY_LOCK_OWNER_CHANGED")
        self.entry_lock.unlink()
        self.owns_lock = False

    def complete(self):
        self.stage = "complete"
        value = {"status": "VERIFIED", "operation": self.args.operation, "heads": self.shas,
                 "managed_apps": self.managed,
                 "resumed_from": self.resumed_from,
                 "continued_from": self.continued_from,
                 "config_hash": digest(self.body), "paper_only": True, "broker_queries": 0,
                 "manual_orders": 0, "manual_state_or_ledger_edits": False,
                 "completed_at": datetime.now(timezone.utc).isoformat()}
        self.record("complete.json", value)
        print(json.dumps(value))

    def failed(self):
        if self.out.exists():
            self.record("failed.json", {"stage": self.stage, "entry_lock_retained": self.owns_lock,
                                        "resumed_from": self.resumed_from,
                                        "continued_from": self.continued_from,
                                        "recovery": "Review before.json; no automatic state rewrite or restart."})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True, help="Mini-local JSON paths; never credentials")
    parser.add_argument("--mode", choices=("paper", "live"), default="paper")
    parser.add_argument("--apply", action="store_true", help="without this, preflight/plan only")
    sub = parser.add_subparsers(dest="operation", required=True)
    params = sub.add_parser("params")
    params.add_argument("--max-spread-width", type=float, required=True)
    release = sub.add_parser("release")
    release.add_argument("--mts-sha", required=True)
    release.add_argument("--dashboard-sha", required=True)
    release.add_argument("--maintenance-approved", action="store_true")
    release.add_argument("--engine-only", action="store_true",
                         help="preserve dashboard process/source; dashboard SHA must equal current HEAD")
    release.add_argument("--resume-failed-transaction", metavar="CODEX_TXID",
                         help="adopt only the matching retained lock from a failed observe-stage PAPER release")
    release.add_argument("--resume-start-failed-transaction", metavar="CODEX_TXID",
                         help="verify/save/unlock an online exact-candidate engine after a retained start-stage failure; never restarts it")
    args = parser.parse_args(argv)
    ops = None
    try:
        os.umask(0o077)
        ops = MiniDeployment(args)
        execute(args, ops)
        return 0
    except (DeployError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        if ops is not None:
            ops.failed()
        # Do not echo generic exception text: it can contain private subprocess data.
        reason = str(exc) if isinstance(exc, DeployError) else type(exc).__name__
        detail = ("; stage=" + ops.stage + "; evidence=" + str(ops.out)
                  if ops is not None else "")
        print("[FAIL] " + reason + detail + "; no completion declared", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
