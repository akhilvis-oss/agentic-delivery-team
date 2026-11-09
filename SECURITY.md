# Security policy

## Reporting a vulnerability
Please **don't open a public issue** for security problems. Use GitHub's private reporting instead: on this repository, go to **Security → Report a vulnerability**. Include what you found, how to reproduce it, and the version or commit affected.

We aim to acknowledge reports within a week. This is a small, volunteer-maintained project, so fixes are best effort.

## Scope
Reports that matter most here:
- ways a pull request could bypass or weaken the trusted review gate;
- ways an agent could make the task runner commit, push or run something outside its validated rules;
- leaks of credentials, tokens or private paths through receipts, logs or GitHub comments.

## Supported versions
Only the latest release receives fixes while the project is at version 0.

## Using the kit safely
- Run agents and the task runner under a dedicated operating-system user with access only to the project.
- Give every automated identity the least GitHub permission it needs, and never use the owner's token for automation.
- Keep credentials out of repositories; the kit's private-content check helps for public repositories but is not a secret scanner.
