"""Unit tests for the chat-host-shell dispatch wrapper in claude_runner.

The wrapper has to (a) prefix the original argv with ``docker exec -i -w
/home/felix portfolio-chat-host-shell``, (b) propagate HOME via ``-e`` so the
account-router's HOME swap reaches the in-container claude, and (c) leave
the original argv intact at the tail so cancellation / signal propagation
behave the same.
"""
from __future__ import annotations

import claude_runner


def test_wrap_for_host_shell_no_home() -> None:
    args = ["claude", "--session-id", "abc", "-p", "hi"]
    out = claude_runner._wrap_for_host_shell(args, home=None)
    assert out[:5] == ["docker", "exec", "-i", "-w", "/home/felix"]
    # No HOME -e pair when home is None.
    assert "HOME=" not in " ".join(out)
    # Container name comes right before the original argv.
    assert out[-len(args) - 1] == "portfolio-chat-host-shell"
    assert out[-len(args):] == args


def test_wrap_for_host_shell_with_home() -> None:
    args = ["claude", "-p", "hi"]
    home = "/opt/wizerith/claude-accounts/account-2"
    out = claude_runner._wrap_for_host_shell(args, home=home)
    # `-e HOME=<path>` appears as a contiguous pair before the container name.
    idx = out.index("-e")
    assert out[idx + 1] == f"HOME={home}"
    # XDG_RUNTIME_DIR is also propagated for systemctl --user.
    assert any(s == "XDG_RUNTIME_DIR=/run/user/1000" for s in out)
    # Original argv preserved at the tail.
    assert out[-len(args):] == args


def test_wrap_for_host_shell_uses_correct_container_name() -> None:
    out = claude_runner._wrap_for_host_shell(["claude"], home=None)
    assert "portfolio-chat-host-shell" in out
    assert claude_runner._HOST_SHELL_CONTAINER == "portfolio-chat-host-shell"
