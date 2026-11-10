# GO Field Notes — Aider worker prototype

**Prototype only — do not install or run unattended.**

This repository contains unvalidated Home Assistant add-on scaffolding for an isolated Aider worker. It is not yet a working autonomous agent. The startup script deliberately stays idle.

## Intended boundaries
- No Home Assistant API, Supervisor API, Docker API, host networking, or host configuration mounts.
- Add-on-private workspace only.
- No production credentials, deployment access, or automatic repository changes.
- Manual startup; no remote shell or job interface in this prototype.

## Blocking work before installation
1. Verify Home Assistant add-on build compatibility and dependency versions in a disposable environment.
2. Implement a bounded job interface running as a non-root user, with strict timeouts and resource limits.
3. Verify model support, Google API quotas, credential handling and redacted logs.
4. Add read-only checkout and test against a disposable GitHub repository before enabling branch/PR writes.
5. Verify isolation and GitHub Actions checks. Never give the worker production deployment secrets.

The current Dockerfile and metadata are **unvalidated scaffolding**, not installation instructions. Do not install until build and security verification are completed.

Keep API keys out of GitHub, chats, issues and commits.
