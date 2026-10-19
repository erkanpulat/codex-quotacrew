import sys
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.continuity.scan import thread_revision
from codex_account_manager.core.errors import AppServerError, ConversationHistoryTooLargeError
from tests.integration.test_limit_continuation import _thread


@pytest.mark.skipif(sys.platform != "win32", reason="Windows extended filesystem paths")
@pytest.mark.parametrize("extended", [False, True])
async def test_history_containment_and_size_with_windows_aliases(tmp_paths, extended):
    from dataclasses import replace

    home = tmp_paths.shared_codex_home
    directory = home / "sessions"
    directory.mkdir()
    history = directory / "test.jsonl"
    history.write_bytes(b"test\n")
    prefix = "\\\\?\\" if extended else ""
    server = CodexAppServer(home)
    server._request_with_discovery_retry = AsyncMock(
        return_value={"thread": {"id": "thread", "path": prefix + str(history)}}
    )
    await server.require_bounded_desktop_history("thread")
    revision = thread_revision(replace(_thread("thread", 1), path=prefix + str(history)))
    assert revision[-1] == (5, history.stat().st_mtime_ns)
    with history.open("r+b") as stream:
        stream.truncate(256 * 1024 * 1024 + 1)
    with pytest.raises(ConversationHistoryTooLargeError):
        await server.require_bounded_desktop_history("thread")
    outside = home / "sessions-other" / "test.jsonl"
    outside.parent.mkdir()
    outside.write_bytes(b"test\n")
    server._request_with_discovery_retry.return_value["thread"]["path"] = prefix + str(outside)
    with pytest.raises(AppServerError, match="outside"):
        await server.require_bounded_desktop_history("thread")
