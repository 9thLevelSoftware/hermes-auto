# Codebase Search Protocol

How Legion commands retrieve from this map. Consumers are `/legion:plan`, `/legion:build`, `/legion:review`, and `/legion:map --query`.

## Dataset

| File | Shape |
|---|---|
| `index.jsonl` | One JSON object per line, 213 chunks |
| `symbols.json` | `{generated_at, by_file: {path: [{name, kind, line, doc}]}, import_edges: {path: [module]}}` |

### Chunk schema

```json
{"id":"py:src/hermes_auto/gateway/schemas.py",
 "path":"src/hermes_auto/gateway/schemas.py",
 "lines":[1,279],
 "kind":"module",
 "summary":"Schema discovery, validation, and registry construction.",
 "symbols":["load_schemas","validate","build_validator"],
 "imports":["jsonschema","referencing"]}
```

`kind` is one of `module`, `class`, `function`, `json-schema`, `doc`.
`id` prefixes: `py:` module, `sym:` symbol, `schema:` JSON Schema, `doc:` markdown.

## Retrieval

1. **Normalize the query** into keywords, path hints, symbol hints, and domain hints. "how are model cards validated" → keywords `model card validate`, path hint `data/schema/routing`, symbol hint `build_validator`.
2. **Grep `index.jsonl`** on `summary` and `symbols` for keywords; filter by `path` when a hint exists.
3. **Grep `symbols.json`** when the query names an identifier — it maps symbol to file and line directly.
4. **Rank**: exact symbol match > path-hint match > summary keyword > `kind` affinity. Return at most 5.
5. **Report** each hit as id, path, line range, kind, summary, and why it matched.
6. **Always emit a "Read next" list** of source paths with line ranges.

## The rule that matters

**Never answer from the index alone when source evidence is required.**

Chunk summaries are compressed and generated at map time. They go stale between the map and the change. Any consumer about to modify code, assert a behavior, or write a plan task must read the source file the chunk points at.

The index tells you *where to look*. It is not evidence.

## Freshness

`CODEBASE.md` carries `generated_at`, `analyzed_commit`, `source_file_count`, and `source_fingerprint`. The map is `stale` when age exceeds 30 days or the fingerprint differs from the current tree.

This map was generated at commit `3c9312f`, with Phase 1 complete and **Phase 2 planned but not built**. Phase 2 adds a full HTTP service under `gateway/`, so the fingerprint will diverge as soon as `/legion:build` runs. Re-run `/legion:map --refresh` after Phase 2 executes.

## Known blind spots

- **Reserved subpackages read as real.** Eight directories under `src/hermes_auto/` are one-line docstrings. A chunk exists for each; its `summary` names the phase that fills it. Do not infer capability from a path.
- **Schema chunks carry `title` and `description`, not constraints.** Whether a field is bounded, patterned, or nullable is only in the file. Cost, capability, and identifier constraints have all been review findings — read the schema.
- **No cross-file call graph.** `import_edges` records module-level imports only. There is no caller/callee index.
- **Test coverage is not mapped.** `tests/` chunks exist, but nothing links a test to the symbol it exercises.
