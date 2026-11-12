# Security

manyeyes runs AI reviewers on source code and holds login tokens, so security reports matter.

## Report a vulnerability

Do not open a public issue. Use GitHub's private reporting instead: the **Security** tab of this repository, then **Report a vulnerability**. You get an answer within a week.

Useful to include: the version (`manyeyes --version`), the mode (CLI or server), the steps to reproduce, and what an attacker gains.

## Scope

In scope: escape from the reviewer jail (`engine/jail.py`), a key or token that reaches a reviewer, authentication or authorization bypass on the server, and access to another user's jobs.

Known and documented, so out of scope: a prompt-injected reviewer can send the code under review to its own provider and can write a misleading review. See the security model in [docs/design.md](docs/design.md#10-security-model).

## Supported versions

Only the latest release gets fixes.
