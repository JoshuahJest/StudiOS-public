# ENKI-II and Solaris Unblocker Checklist

This checklist is derived from the reviewed coordination packet. It is a planning/reference document, not remote execution authority.

## ENKI-II — required before remote worker control

1. **Repair the canonical stale/offline revision contract.**
   - Ensure every mutable API operation enforces `expected_revision` transactionally.
   - Prove the real two-device conflict path: offline mutation → current-state change elsewhere → intentional 409/conflict response → human resolution → revisioned Markdown projection.
2. **Fix the Windows timezone test environment.**
   - Make IANA zones such as `UTC` and `America/Chicago` available in the supported runtime.
   - Re-run the clean-archive backend suite and preserve its result.
3. **Build the local-only controller canary.**
   - Durable task/run/attempt/receipt records in the authoritative `studio.sqlite` path.
   - Test queued/claimed/working/cancel-requested/cancelled/expired/failed/verified transitions, duplicates, policy denial, and receipt verification.
4. **Keep control-plane exposure closed.**
   - Do not expose a general dashboard, SSH, raw database, Docker API, Herdr socket, Funnel, exit node, or subnet route as a shortcut.

## Solaris — required before worker enrollment

1. **Resolve or diagnose the Hermes trampoline-spawn failure.**
   - Prove `hermes --version` and a local health check work from the intended worker account.
2. **Respect the GPU thermal gate.**
   - The last reported Quadro RTX 4000 temperature was 80 C.
   - Keep the first canary CPU-only; do not enable GPU work until cooling, telemetry, and operator-reserve policy are reviewed.
3. **Run only the no-write synthetic canary first.**
   - Expiry, cancellation, duplicate, wrong-host, and policy-denial behavior must be receipted before real project work.

## How the operator can expedite safely

- Ask Wanda to create tickets/ADRs for the ENKI revision and timezone gates, including exact failing test names and expected acceptance evidence.
- Provide one sanitized, non-sensitive synthetic fixture for the first canary.
- Decide the reviewed local project/scratch paths allowed to each worker.
- Review the no-write envelope and receipt examples before any endpoint or Tailnet rule beyond current NAS SMB access is proposed.
- Keep the Dropbox handoff surface non-secret and manual until the canary matrix passes.
