"""Example Flint plugin — type `uuid` to get a fresh UUID (Enter copies)."""
import uuid


def query(q):
    if q.strip().lower() != "uuid":
        return []
    u = str(uuid.uuid4())
    return [{"title": u, "subtitle": "UUID v4 — Enter copies", "icon": "🆔",
             "copy_text": u}]
