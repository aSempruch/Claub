"""Tests for the file-download MCP server (Stas instance).

Security-critical helpers (path confinement, SSRF blocking, scheme validation,
size capping) live in helpers.py with no module-level side effects, so they can
be imported and unit-tested directly. The httpx download orchestration in
server.py is exercised by a live end-to-end agent test, not here.
"""

import os
import sys

import pytest

# MCP is baked into the image from the repo's mcps/ dir (COPY mcps/ /app/mcps/).
_FILE_DOWNLOAD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "mcps", "file-download"
)
sys.path.insert(0, _FILE_DOWNLOAD_DIR)

from helpers import (  # noqa: E402
    assert_host_allowed,
    is_blocked_ip,
    read_capped,
    resolve_safe_dest,
    validate_url,
)


class TestResolveSafeDest:
    def test_valid_relative_path(self, tmp_path):
        workspace = str(tmp_path)
        assert resolve_safe_dest("img.png", workspace) == str(tmp_path / "img.png")

    def test_subdirectory_path_not_yet_existing(self, tmp_path):
        workspace = str(tmp_path)
        # Parent dir need not exist yet — the server creates it after validation.
        assert resolve_safe_dest("downloads/a/img.png", workspace) == str(
            tmp_path / "downloads" / "a" / "img.png"
        )

    def test_rejects_empty(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            resolve_safe_dest("", str(tmp_path))

    def test_rejects_path_traversal(self, tmp_path):
        with pytest.raises(ValueError, match="escapes"):
            resolve_safe_dest("../../etc/passwd", str(tmp_path))

    def test_rejects_absolute_escape(self, tmp_path):
        with pytest.raises(ValueError, match="escapes"):
            resolve_safe_dest("/etc/passwd", str(tmp_path))

    def test_rejects_git_dir(self, tmp_path):
        with pytest.raises(ValueError, match="protected"):
            resolve_safe_dest(".git/hooks/pre-commit", str(tmp_path))

    def test_rejects_claude_dir(self, tmp_path):
        for p in (".claude/x", ".claude-skills/s.md", ".claude-agents/a.md"):
            with pytest.raises(ValueError, match="protected"):
                resolve_safe_dest(p, str(tmp_path))

    def test_rejects_protected_dir_when_nested(self, tmp_path):
        with pytest.raises(ValueError, match="protected"):
            resolve_safe_dest("sub/.git/config", str(tmp_path))

    def test_rejects_symlink_escape(self, tmp_path):
        workspace = str(tmp_path)
        outside = tmp_path.parent / "outside"
        outside.mkdir()
        link = tmp_path / "escape"
        link.symlink_to(outside)
        with pytest.raises(ValueError, match="escapes"):
            resolve_safe_dest("escape/loot.bin", workspace)

    def test_rejects_existing_directory_target(self, tmp_path):
        (tmp_path / "adir").mkdir()
        with pytest.raises(ValueError, match="directory"):
            resolve_safe_dest("adir", str(tmp_path))


class TestIsBlockedIp:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "::1",
            "10.0.0.1",
            "192.168.1.1",
            "172.16.0.1",
            "169.254.169.254",  # cloud metadata
            "0.0.0.0",
        ],
    )
    def test_blocks_non_public(self, ip):
        assert is_blocked_ip(ip) is True

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
    def test_allows_public(self, ip):
        assert is_blocked_ip(ip) is False


class TestValidateUrl:
    def test_accepts_http(self):
        validate_url("http://example.com/a.png")

    def test_accepts_https(self):
        validate_url("https://example.com/a.png")

    def test_rejects_file_scheme(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_url("file:///etc/passwd")

    def test_rejects_ftp_scheme(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_url("ftp://example.com/a")

    def test_rejects_missing_host(self):
        with pytest.raises(ValueError, match="host"):
            validate_url("http:///nohost")


class TestAssertHostAllowed:
    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "localhost", "10.0.0.1", "169.254.169.254", "host.docker.internal"],
    )
    def test_blocks_internal(self, host):
        with pytest.raises(ValueError, match="blocked"):
            assert_host_allowed(host)

    def test_allows_public_ip_literal(self):
        # Literal public IP resolves offline; must not raise.
        assert_host_allowed("8.8.8.8")


class TestReadCapped:
    def test_under_limit(self):
        assert read_capped([b"abc", b"def"], 10) == b"abcdef"

    def test_at_limit(self):
        assert read_capped([b"abcde", b"fghij"], 10) == b"abcdefghij"

    def test_over_limit_raises(self):
        with pytest.raises(ValueError, match="too large"):
            read_capped([b"a" * 6, b"b" * 6], 10)
