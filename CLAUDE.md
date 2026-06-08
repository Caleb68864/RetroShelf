<!-- logic-dev-graph:start -->
## Code graph — query first

This project has a whole-project code graph at the **project root**
(`<repo root>/graphify-out/graph.json`), built by logic-dev-kit. The graph
tools auto-anchor to the project root, so you can call them from any
subdirectory without passing a target path. Before searching the codebase,
query the graph instead of grepping:

- Call `query_graph` (the MCP tool) before `Grep`, `Glob`, or a broad `Read`
  when you are looking for a symbol, definition, caller, or where something is
  used. The graph answers from a compact, structured index instead of scanning
  the whole repo.
- **If the `query_graph` MCP tool is not available this session** (e.g. the MCP
  server didn't surface it — check your tool list), use the CLI front door via
  `Bash` instead — it is always available and behaves identically:
  `python -m logic_dev_kit.graph_cli query "<symbol>" --text-fallback`
  (also `neighbors`, `path`, `impact`, `stats` subcommands). With
  `--text-fallback` the query returns graph hits first, then automatically falls
  back to ripgrep — or a pure-Python text walk when `rg` isn't installed — for
  content the graph doesn't index. So the CLI alone is graph-first *with* grep
  fallback in one call; reach for raw `Grep` only when it returns nothing.
- Use `get_neighbors` and `shortest_path` to follow call/import relationships.
- Only fall back to `Grep`/`Glob` when the graph returns no hits (a graph miss
  means the symbol genuinely isn't indexed — grep is the correct fallback then).
- `build_project_graph` refreshes the graph. The graph also lazily refreshes on
  query when `project_graph.auto_refresh` is `lazy`, so it reflects uncommitted
  edits without a manual rebuild.

Prefer the graph: it is faster and far more token-efficient than a repo-wide
grep.
<!-- logic-dev-graph:end -->
