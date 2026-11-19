# Extensions

Fields in `extensions` that the PublicDataToolkit standard doesn't model yet (STANDARD.md §4.4).

| Key | What it holds | Source | Why it's an extension |
|---|---|---|---|
| `gss.modules` | The GSS module a variable belongs to, e.g. `Core`, `ISSP` | gssrdoc `module_df` | A module could be a group (`kind: module`), but in the slice every variable is `Core`, so a group would say nothing. Revisit when the slice includes topical modules |
