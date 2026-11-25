# Support

Thank you for using **codebase-atlas**!

## How to Get Help

### 1. Documentation

- [README.md](README.md) / [README_ja.md](README_ja.md): overview and quick start
- [docs/how-it-works.md](docs/how-it-works.md): what happens, what is checked, what to do when the agent stops (Japanese)
- [docs/graphify-integration.md](docs/graphify-integration.md): the graphify cross-check (`G-GRAPHIFY`)
- [docs/development-workflow.md](docs/development-workflow.md): contributing, quality gate, Auto-Pilot merge

### 2. Frequently Asked Questions

**Q: Do I need a database, a vector store or a server?**
A: No. The workflow is bash, git and Markdown. graphify writes one `graph.json` file locally; nothing runs as a service.

**Q: graphify is not available in my environment. Can I still use codebase-atlas?**
A: Yes. Run `init.sh` with `--code-graph-tool none` (or set `CODE_GRAPH_TOOL=none` in
`spec-extraction/00-ingestion/gate-config.env` and record the reason in an ADR). The `G-GRAPHIFY` gate is then skipped
and the workflow behaves like reverse-spec-flow.

**Q: Does graphify modify the source code I analyze?**
A: No. `atlas_graph.py build` refuses an output directory inside the target and checks that the target's git state is
unchanged after the run. `gates.sh` also detects any change to the target (`G-SOURCE-INTACT`).

**Q: Is my source code sent anywhere?**
A: graphify's `--code-only` mode parses locally. Your AI tool, however, reads the code: follow its terms of use and your
organization's rules (see [SECURITY.md](SECURITY.md)).

### 3. Reporting Bugs or Requesting Features

- [Open a Bug Report](https://github.com/sun-flat-yamada/codebase-atlas/issues/new?template=bug_report.yml)
- [Open a Feature Request](https://github.com/sun-flat-yamada/codebase-atlas/issues/new?template=feature_request.yml)

If this project helps you, you can support it via
[Buy Me A Coffee](https://buymeacoffee.com/sun.flat.yamada).
