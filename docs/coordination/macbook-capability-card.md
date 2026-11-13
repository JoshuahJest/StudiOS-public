# MacBook Capability Card — Proposed Enrollment Input

**Host ID:** `macbook-worker`
**Observed:** 2026-08-29T04:41:56-0500
**Data class:** shared-nonsecret
**Status:** preparation only; not enrolled as a remote worker

## Verified capabilities

- macOS 26.6.2; Apple M5 Max; 128 GiB unified memory.
- Hermes Agent v0.20.5; local profile `default`; MiniMax OAuth configured locally.
- LM Studio installed locally; no qualified local-model benchmark has been completed.
- Tailscale tag-owned identity `tag:thomas-macbook`.
- Scoped Tailnet access only to <nas-hostname> SMB TCP/445.
- 14 <nas-hostname> SMB shares mounted through the direct LAN path; private Tailnet SMB benchmarked as fallback.
- Local no-write envelope validation module tested for host-target and expiry handling.

## Explicitly absent

- No ENKI-II SSH, dashboard, database, Docker, DSM, or general peer access.
- No remote task-control endpoint.
- No shared Hermes state, credentials, memory/session database, browser profile, or raw project data.
- No authorization for external communication, financial operations, deployment, destructive actions, or arbitrary shell execution.

## Candidate first canary

A locally executed, no-write synthetic envelope that is:

- addressed to `macbook-worker`;
- short-lived;
- restricted to an approved local synthetic fixture path;
- `local-only` for model budget;
- evidence/receipt producing;
- manually approved through the existing Dropbox handoff process.
