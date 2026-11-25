## Description

Briefly describe the intent of this pull request and what problem it solves.

- Closes #

## Type of Change

- [ ] ✨ New feature (gate, template, tool option)
- [ ] 🐛 Bug fix
- [ ] 📚 Playbook (new or updated technology stack)
- [ ] 🧩 Host adapter (`hosts/`)
- [ ] 📝 Documentation
- [ ] 🧪 Tests / CI

## Key Changes

-

## Quality Gate (Mandatory)

- [ ] `bash selftest.sh` ends with `結果: PASS <n> / FAIL 0`
- [ ] `python3 -m unittest discover -s tests` passes
- [ ] The six coupled places stay consistent (`init.sh`, `AGENTS.md.template`, `pipeline.md`, `playbook.md`, `templates/`, `playbooks/`; see `CLAUDE.md`)
- [ ] No proprietary source code, customer names or secrets are included (fixtures are synthetic)

## Git Ops

- [ ] Branch name follows `<type>/<issue>-<slug>` (checked by the `Branch Name Check` workflow)
- [ ] Commits follow Conventional Commits; the branch is rebased onto the latest `main`
- [ ] Merge strategy: **Rebase & Merge** (`python3 scripts/automerge.py finish <pr> --wait` when Auto-Pilot is on)
