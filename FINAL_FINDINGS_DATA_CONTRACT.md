# Final findings data contract

The canonical `AppFinding` retains the fixed seed category and these reviewer-facing dimensions:

| Reviewer column | Canonical fields |
|---|---|
| App | seed name |
| Category | seed category |
| What it does | `one_liner` + `one_liner` evidence |
| Auth | `auth_methods` + `auth_methods` evidence |
| Access | `self_serve_status`, `gating_reason` + access evidence |
| API Surface | `api_surface_type`, `api_surface_breadth`, `api_surface_summary` |
| MCP | `mcp_status`, `mcp_notes` + MCP evidence |
| Buildability | `buildability_verdict` |
| Main Blocker / Caveat | product `blocker` only; never research transport/model errors |
| Evidence / Sources | field-labelled links for Description, Auth, Access, API, and MCP |

Each `Evidence` item keeps its field, exact URL, source title when available,
source type, confidence, and verification status. Research status is
`grounded`, `partial`, or `unresolved`; `complete` remains readable only for
historical payload compatibility.

The reviewer projection is read-only. It cannot overwrite findings, passes,
or verification records.
