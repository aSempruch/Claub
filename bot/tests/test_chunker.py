from claude_assistant.chunker import chunk_message

LIMIT = 2000


def test_short_message() -> None:
    assert chunk_message("hello", LIMIT) == ["hello"]


def test_exact_limit() -> None:
    msg = "a" * LIMIT
    assert chunk_message(msg, LIMIT) == [msg]


def test_splits_on_newline() -> None:
    line = "a" * 999
    msg = f"{line}\n{line}\n{line}"  # 3 lines, total > 2000
    chunks = chunk_message(msg, LIMIT)
    assert len(chunks) == 2
    for c in chunks:
        assert len(c) <= LIMIT


def test_long_line_force_split() -> None:
    msg = "a" * 3000  # single line longer than limit
    chunks = chunk_message(msg, LIMIT)
    assert len(chunks) == 2
    assert len(chunks[0]) == LIMIT
    assert len(chunks[1]) == 1000


def test_empty_message() -> None:
    assert chunk_message("", LIMIT) == [""]
