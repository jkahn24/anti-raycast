# Anti-Raycast plugins

Drop a `.py` file in this folder exposing one function:

```python
def query(q: str) -> list[dict]:
    """Called on every keystroke of a plain (non-prefixed) query.
    Return [] fast when the query isn't yours."""
    return [{
        "title":     "What the row says",       # required
        "subtitle":  "smaller grey text",       # optional
        "icon":      "🔌",                       # optional emoji
        "on_enter":  lambda: ...,                # optional callable on Enter
        "copy_text": "copied to clipboard",     # optional, Enter copies
    }]
```

Rules:
- Loaded once at worker spawn (`> restart` to reload after edits).
- Max 3 rows per plugin per query.
- Exceptions are swallowed — a broken plugin can't take the palette down.
- Keep `query()` fast (<10ms): it runs synchronously on every keystroke.
  For slow work, return a row whose `on_enter` does the slow part.
- Files starting with `_` are skipped.

Example: `uuid_gen.py` — type `uuid`, Enter copies a fresh UUID v4.
