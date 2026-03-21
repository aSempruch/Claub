from __future__ import annotations


def chunk_message(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # Try to split on a newline within the limit
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            # No newline found, force split at limit
            split_at = limit
        else:
            split_at += 1  # include the newline in current chunk

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    return chunks
