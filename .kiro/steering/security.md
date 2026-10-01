# SentinelNow — Security Rules (always enforce)

This is a security tool. The following rules are non-negotiable and apply to every file.

## Secrets & credentials
- NEVER log, print, or echo raw credentials, tokens, API keys, or ServiceNow instance passwords.
- Read ServiceNow credentials only from environment variables (e.g. `SN_INSTANCE`, `SN_USER`, `SN_PASSWORD`) or a `.env` file that is gitignored.
- When logging an action that involved a secret, reference it by name (`SN_PASSWORD`) never by value.

Example — correct:
```python
logger.info("authenticated to instance %s as %s", instance_host, user)  # no password
```
Example — forbidden:
```python
logger.info("auth with password %s", password)  # NEVER do this
```

## Safety-by-default for ServiceNow writes
- All write actions MUST pass `policy_check` before `guarded_write` executes them.
- Destructive or bulk operations default to DENY unless an explicit allow rule matches.
- `preview_change` must never mutate the instance — read-only, dry-run only.
- Every executed action MUST produce an entry via `audit_log` (who / what / why / result).
- The kill switch, when engaged, blocks ALL writes regardless of other policy.

## Data handling
- Treat all ServiceNow record data as potentially sensitive. Do not write record contents to logs beyond IDs and the fields needed for the audit entry.
- Use synthetic/mock data for tests and demos. Never commit real instance data.

This ensures the gateway cannot leak secrets or perform an un-audited, un-approved write.
