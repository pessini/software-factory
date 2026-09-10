import os
import sys
from pathlib import Path

import pytest

from software_factory.processes import CleanupUncertain, finish_process, start_process, stop_group


async def test_failed_process_inspection_never_signals_a_group(tmp_path: Path, monkeypatch) -> None:
    fake_ps = tmp_path / "ps"
    fake_ps.write_text(
        '#!/bin/sh\nif [ "$1" = "-p" ]; then echo "inspection denied" >&2; exit 1; else echo "999999 S"; fi\n'
    )
    fake_ps.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    signals = []
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(CleanupUncertain, match="inspect"):
        await stop_group(999999, "saved identity")
    assert not signals


async def test_normal_cleanup_refuses_a_changed_process_identity(
    tmp_path: Path, monkeypatch
) -> None:
    process, process_id = await start_process(
        [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path
    )
    try:
        fake_ps = tmp_path / "ps"
        fake_ps.write_text(
            f'#!/bin/sh\nif [ "$1" = "-p" ]; then echo "different start time"; else echo "{process.pid} S"; fi\n'
        )
        fake_ps.chmod(0o755)
        with monkeypatch.context() as patched:
            patched.setenv("PATH", str(tmp_path))
            signals = []
            patched.setattr(os, "killpg", lambda *args: signals.append(args))
            with pytest.raises(CleanupUncertain, match="identity changed"):
                await finish_process(process, process_id)
            assert not signals
    finally:
        await finish_process(process, process_id)
