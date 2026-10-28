# GitHub publish plan for `WHanY-7/IssuePilot`

This is an operator plan, not an executed publication. Never substitute `WHanY-7/IssuePilot-Sandbox`; that repository is reserved for controlled comment-write tests.

## 1. Verify owner and remote state

1. The owner selected the MIT License for project-authored source. Verify `LICENSE` and `THIRD_PARTY_NOTICES.md` remain in the audited file list.
2. In GitHub, verify whether `WHanY-7/IssuePilot` already exists, who owns it, its default branch, branch protection, visibility, and whether it contains unrelated history. Do not overwrite or force-push an existing repository.
3. Confirm that the four synthetic examples and README summary are acceptable for publication.

## 2. Create a clean D-drive staging tree

Use the current project directory only if every gate passes and stage files with the explicit `ALLOW` list from `release/release_file_manifest.txt`; never use `git add .` or `git add -A`. A separate clean D-drive staging directory remains the safer fallback when the remote contains existing history.

After copying, verify from the staging root in PowerShell:

```powershell
Get-ChildItem -Recurse -Force
rg -n "ghp_|github_pat_|sk-|Authorization:\s*Bearer|D:\\\\|C:\\\\Users" .
python -m unittest discover -s tests -v
python -m scripts.similarity_mock_demo
$env:ISSUEPILOT_ENV_FILE = "docker.env.example"
docker compose config --quiet
```

Review every reported match manually. Variable names and documented placeholders are acceptable; real values, local absolute paths, raw responses, and denied files are not.

## 3. Choose the safe Git path based on remote state

If `WHanY-7/IssuePilot` already exists, clone it to a separate empty D-drive directory, inspect its history and remotes, and merge/copy the curated staging files without deleting unrelated content. Use normal commits and a review branch; never use `--force`.

If it does not exist, the owner creates it through an approved GitHub UI/account workflow. Only then initialize the clean staging directory, add exactly `https://github.com/WHanY-7/IssuePilot.git` as `origin`, and verify with `git remote -v`. Do not use `gh repo create` for this delivery.

Before the first commit:

```powershell
git status --short
git add --dry-run .
git diff --cached --name-only
```

Compare the staged file list byte-for-byte with `release/release_file_manifest.txt`. Run a secret scanner approved by the owner. Inspect the staged diff; then create a normal signed/reviewable commit according to the owner's policy.

## 4. Publish without destructive operations

Push a review branch first when branch protection/workflow permits. Open a pull request into the verified default branch. Do not force-push, rewrite history, publish `.env`, or add the Sandbox repository as the project remote.

This Codex delivery does not execute `git init`, `git commit`, `git push`, remote creation, or GitHub API operations.

## 5. Post-publication verification

On GitHub, verify:

- repository path is exactly `WHanY-7/IssuePilot`;
- LICENSE/visibility match the owner's decision;
- only manifest files are visible;
- `.env`, `docker.env`, databases, real datasets, logs, backups, gold labels, raw responses, and ledgers are absent;
- README Mermaid renders and all relative links work;
- default branch CI runs the offline test suite without credentials;
- synthetic Mock demo works with no API key;
- Docker build uses only `data/issues_demo.json`;
- GitHub writeback remains disabled without explicit sandbox settings;
- no release text claims production accuracy, duplicate-detection accuracy, high concurrency, or multi-host SQLite support.

If a forbidden file or credential is found after publication, stop distribution, rotate affected credentials first, preserve evidence, and coordinate history remediation. Do not assume a later `.gitignore` change removes data from history.
