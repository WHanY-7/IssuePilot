# IssuePilot release checklist

Target: `WHanY-7/IssuePilot`  
Forbidden destination: `WHanY-7/IssuePilot-Sandbox`

| Gate | Status | Evidence / required action |
|---|---|---|
| Curated allowlist exists | PASS | `release/release_file_manifest.txt`; unlisted files are denied. |
| Real `.env` excluded | PASS | `.gitignore` and `.dockerignore` exclude `.env`/`.env.*`; only `.env.example` is allowed. |
| Environment examples contain placeholders only | PASS | Automated deployment test checks auth/Qwen placeholders and empty GitHub settings. |
| Credentials absent from candidate files | PASS | Candidate-only pattern scan found no token, Authorization value, or private credential file. |
| SQLite/runtime state excluded | PASS | Global database ignore rules; only synthetic JSON data is allowed. |
| Logs, backups, audit data, caches, venv excluded | PASS | Git/Docker ignores and DENY manifest entries cover them. |
| Raw real evaluation artifacts excluded | PASS | Real Issue text, gold labels, request ledgers, responses, predictions, and result directories are denied. |
| Dataset redistribution reviewed | PASS | Only four project-authored synthetic examples are allowed; the 36-Issue corpus is denied. |
| Docker build context excludes sensitive/local state | PASS | `.dockerignore` allows only `data/issues_demo.json`; static tests pass. |
| Docker Compose static validation | PASS | `docker compose config --quiet` passed using `docker.env.example`; no runtime rebuild was needed. |
| Fresh dependency installation | PASS | A new D-drive Python 3.12 venv installed `requirements.txt` from the configured public package index and imported API/Worker/retrieval modules. Temp files were removed. |
| Mock demo without credentials | PASS | Synthetic input produced candidates/evidence, required human review, 0 Qwen calls, 0 GitHub writes. |
| Automated regression suite | PASS | Final run: 49 passed, 0 failed. |
| Enhancement defaults off | PASS | `ISSUEPILOT_SIMILARITY_ENABLED` defaults to `false`; covered by test. |
| GitHub writeback remains human-gated and sandbox-only | PASS | Existing review/writeback tests pass; target constant remains `WHanY-7/IssuePilot-Sandbox`. |
| README commands and architecture match source | PASS | Windows, Compose, API docs, Mock demo, task states, retrieval limits, and security boundaries checked. |
| Known limitations disclosed | PASS | README lists all five limitations and evaluation denominators/caveats. |
| Local Git history sensitive-file scan | NOT_CHECKED | The current project directory and its parent are not Git repositories; there is no local history or remote to inspect. |
| Target repository existence, ownership, branch protection | NOT_CHECKED | Must be verified by the owner before any clone/init/push. Do not assume the remote exists. |
| LICENSE selected and authorized | PASS | Owner selected MIT; `LICENSE` uses copyright 2026 WHanY-7 and `THIRD_PARTY_NOTICES.md` keeps dependency licenses separate. |
| Post-publication GitHub content verification | NOT_CHECKED | Perform only after the owner publishes the curated staging tree. |

## Release decision

The curated code and documentation are technically ready for the final GitHub account and remote-state gates. The MIT license decision is resolved. Publication remains blocked until the authenticated account is WHanY-7 and the target repository is verified safe. No Git history has been rewritten and no remote operation had been performed when this checklist entry was updated.
