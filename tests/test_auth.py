"""Token storage, precedence and the setup / doctor / logout commands.

conftest's _isolated_home points HOME, XDG_CONFIG_HOME and APPDATA at a
per-test directory and clears REMARKABLE_TOKEN, so nothing here can touch
the real token.
"""

import base64
import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import rm_mcp.api as api_mod
from rm_mcp import auth, cli, client_config


def jwt(exp: float, sub: str = "x") -> str:
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp, 'sub': sub})}.sig"


FILE_TOKEN = json.dumps({"devicetoken": jwt(2e9, "file"), "usertoken": ""})
ENV_TOKEN = json.dumps({"devicetoken": jwt(2e9, "env"), "usertoken": ""})
LEGACY_TOKEN = json.dumps({"devicetoken": jwt(2e9, "legacy"), "usertoken": ""})


def write_file_token(data: str = FILE_TOKEN) -> Path:
    path = auth.token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def write_legacy(data: str = LEGACY_TOKEN) -> Path:
    path = Path.home() / ".rmapi"
    path.write_text(data)
    return path


# =============================================================================
# Precedence
# =============================================================================


def test_no_token_anywhere():
    assert auth.find_token() is None
    assert api_mod.get_rmapi() is None


def test_our_file_beats_env_and_legacy(monkeypatch):
    write_file_token()
    write_legacy()
    monkeypatch.setenv("REMARKABLE_TOKEN", ENV_TOKEN)
    source = auth.find_token()
    assert source.kind == "file" and source.data == FILE_TOKEN
    assert api_mod.get_rmapi().device_token == jwt(2e9, "file")


def test_env_beats_legacy(monkeypatch):
    write_legacy()
    monkeypatch.setenv("REMARKABLE_TOKEN", ENV_TOKEN)
    assert auth.find_token().kind == "env"
    assert api_mod.get_rmapi().device_token == jwt(2e9, "env")


def test_legacy_is_the_last_resort_and_never_written(monkeypatch):
    legacy = write_legacy()
    before = legacy.read_bytes()
    monkeypatch.setenv("REMARKABLE_TOKEN", "   ")  # blank counts as unset
    assert auth.find_token().kind == "legacy"
    client = api_mod.get_rmapi()
    assert client.device_token == jwt(2e9, "legacy")
    assert client.on_token_renewed is None
    assert legacy.read_bytes() == before


def test_rerunning_setup_replaces_the_token_the_running_server_uses():
    """The bug behind issue #6: a new login must win over what the server had."""
    write_file_token()
    first = api_mod.get_rmapi()
    assert api_mod.get_rmapi() is first
    time.sleep(0.01)
    auth.save_token({"devicetoken": jwt(2e9, "new"), "usertoken": ""})
    second = api_mod.get_rmapi()
    assert second is not first and second.device_token == jwt(2e9, "new")


def test_register_writes_our_file_not_rmapi():
    response = Mock(status_code=200, text="device-token-abc")
    with patch("requests.post", return_value=response) as post:
        path = api_mod.register_and_get_token("code1234")
    assert Path(path) == auth.token_path()
    assert json.loads(Path(path).read_text())["devicetoken"] == "device-token-abc"
    assert not (Path.home() / ".rmapi").exists()
    assert post.call_args.kwargs["json"]["deviceDesc"] == auth.device_desc()


@pytest.mark.parametrize(
    "platform,desc",
    [("darwin", "desktop-macos"), ("win32", "desktop-windows"), ("linux", "desktop-linux")],
)
def test_device_desc_names_the_real_os(platform, desc):
    assert auth.device_desc(platform) == desc


# =============================================================================
# The file: location, permissions, user token
# =============================================================================


def test_config_dir_per_platform():
    home = Path("/h")
    assert auth.config_dir("linux", {}, home) == Path("/h/.config/rm-mcp")
    assert auth.config_dir("linux", {"XDG_CONFIG_HOME": "/x"}, home) == Path("/x/rm-mcp")
    assert auth.config_dir("linux", {"XDG_CONFIG_HOME": "rel"}, home) == Path("/h/.config/rm-mcp")
    assert auth.config_dir("darwin", {}, home) == Path("/h/Library/Application Support/rm-mcp")
    assert auth.config_dir("win32", {"APPDATA": "C:/Users/u/AppData/Roaming"}, home) == Path(
        "C:/Users/u/AppData/Roaming/rm-mcp"
    )
    assert auth.config_dir("win32", {}, home) == Path("/h/AppData/Roaming/rm-mcp")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_token_file_is_private():
    path = auth.save_token({"devicetoken": "d", "usertoken": ""})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not auth.loose_permissions(path)
    os.chmod(path, 0o644)
    assert auth.loose_permissions(path)


def test_save_replaces_and_leaves_no_temp_files():
    auth.save_token({"devicetoken": "a", "usertoken": ""})
    auth.save_token({"devicetoken": "b", "usertoken": ""})
    assert json.loads(auth.token_path().read_text())["devicetoken"] == "b"
    assert [p.name for p in auth.token_path().parent.iterdir()] == ["token.json"]


def test_renewed_user_token_is_saved_to_our_file():
    write_file_token()
    client = api_mod.get_rmapi()
    fresh = jwt(time.time() + 86400, "user")
    client._session.post = Mock(return_value=Mock(status_code=200, text=fresh))
    client.renew_token()
    assert json.loads(auth.token_path().read_text())["usertoken"] == fresh
    # Our own write does not make the server think setup replaced the file.
    assert api_mod.get_rmapi() is client


def test_a_saved_user_token_is_reused_until_it_nearly_expires():
    fresh = jwt(time.time() + 3600)
    stale = jwt(time.time() + 60)
    assert auth.usable_user_token(fresh) == fresh
    assert auth.usable_user_token(stale) == ""
    assert auth.usable_user_token("not-a-jwt") == ""
    write_file_token(json.dumps({"devicetoken": jwt(2e9), "usertoken": fresh}))
    assert api_mod.get_rmapi().user_token == fresh


def test_logout_removes_only_our_file(capsys):
    write_file_token()
    legacy = write_legacy()
    cli.main(["logout"])
    assert not auth.token_path().exists() and legacy.exists()
    assert "Removed" in capsys.readouterr().out


# =============================================================================
# Claude Desktop config
# =============================================================================


UVX = "/opt/bin/uvx"


def test_desktop_config_is_created(tmp_path):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    result = client_config.configure_desktop(path, UVX)
    assert result.status == "written" and result.backup is None
    assert json.loads(path.read_text()) == {
        "mcpServers": {"remarkable": {"command": UVX, "args": ["rm-mcp@latest"]}}
    }


def test_desktop_config_keeps_other_servers_and_writes_a_backup(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    original = {
        "mcpServers": {
            "other": {"command": "x"},
            "remarkable": {
                "command": "uvx",
                "args": ["rm-mcp"],
                "env": {"REMARKABLE_TOKEN": "old", "REMARKABLE_ROOT_PATH": "/Work"},
            },
        },
        "theme": "dark",
    }
    path.write_text(json.dumps(original))
    result = client_config.configure_desktop(path, UVX)
    assert result.status == "written"
    assert json.loads(result.backup.read_text()) == original
    written = json.loads(path.read_text())
    assert written["theme"] == "dark" and written["mcpServers"]["other"] == {"command": "x"}
    assert written["mcpServers"]["remarkable"] == {
        "command": UVX,
        "args": ["rm-mcp@latest"],
        "env": {"REMARKABLE_ROOT_PATH": "/Work"},
    }
    assert client_config.configure_desktop(path, UVX).status == "unchanged"


@pytest.mark.parametrize("body", ['{"mcpServers": {', "[1, 2]", '{"mcpServers": []}'])
def test_invalid_desktop_config_is_left_untouched(tmp_path, body):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir()
    path.write_text(body)
    result = client_config.configure_desktop(path, UVX)
    assert result.status == "invalid" and result.reason
    assert path.read_text() == body
    assert sorted(p.name for p in path.parent.iterdir()) == ["claude_desktop_config.json"]


def test_desktop_config_path_per_platform():
    home = Path("/h")
    assert client_config.desktop_config_path("darwin", {}, home) == Path(
        "/h/Library/Application Support/Claude/claude_desktop_config.json"
    )
    assert client_config.desktop_config_path(
        "win32", {"APPDATA": "C:/Users/u/AppData/Roaming"}, home
    ) == Path("C:/Users/u/AppData/Roaming/Claude/claude_desktop_config.json")


def test_windows_commands_use_windows_quoting():
    argv = ["C:\\Program Files\\uv\\uvx.exe", "rm-mcp@latest"]
    assert (
        client_config.display_command(argv, "win32")
        == '"C:\\Program Files\\uv\\uvx.exe" rm-mcp@latest'
    )
    assert client_config.display_command(["/a b/uvx", "x"], "linux") == "'/a b/uvx' x"
    # The Claude Code command carries no token, so it reads the same in every shell.
    add = client_config.claude_code_add_argv()
    assert client_config.display_command(add, "win32") == client_config.display_command(
        add, "linux"
    )
    assert not any("TOKEN" in part for part in add)


def test_windows_desktop_snippet_escapes_the_uvx_path():
    uvx = "C:\\Users\\me\\.local\\bin\\uvx.exe"
    assert json.loads(client_config.snippet(uvx))["mcpServers"]["remarkable"]["command"] == uvx


def test_desktop_findings_flag_a_relative_command_and_a_stale_token(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            {"mcpServers": {"remarkable": {"command": "uvx", "env": {"REMARKABLE_TOKEN": "t"}}}}
        )
    )
    findings = client_config.desktop_findings(path)
    assert len(findings) == 2 and "t" not in [f.split()[-1] for f in findings]


# =============================================================================
# setup and doctor
# =============================================================================


def _no_clients(monkeypatch, tmp_path):
    monkeypatch.setattr(client_config, "claude_code_status", lambda: "missing")
    monkeypatch.setattr(client_config, "uvx_path", lambda: UVX)
    desktop = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(client_config, "desktop_config_path", lambda: desktop)
    return desktop


def test_setup_registers_and_configures_desktop(monkeypatch, tmp_path, capsys):
    desktop = _no_clients(monkeypatch, tmp_path)
    desktop.parent.mkdir()
    response = Mock(status_code=200, text="device-token-abc")
    with patch("requests.post", return_value=response), patch("webbrowser.open") as browser:
        cli.main(["setup", "--code", "abc", "--yes"])
    browser.assert_not_called()
    assert json.loads(auth.token_path().read_text())["devicetoken"] == "device-token-abc"
    assert json.loads(desktop.read_text())["mcpServers"]["remarkable"]["command"] == UVX
    out = capsys.readouterr().out
    assert "device-token-abc" not in out
    assert "claude mcp add -s user remarkable -- uvx rm-mcp@latest" in out


def test_setup_prompts_and_declining_writes_nothing(monkeypatch, tmp_path, capsys):
    desktop = _no_clients(monkeypatch, tmp_path)
    desktop.parent.mkdir()
    answers = iter(["abc", "n"])
    response = Mock(status_code=200, text="tok")
    with patch("requests.post", return_value=response), patch("webbrowser.open"):
        cli._handle_setup(input_fn=lambda prompt: next(answers))
    assert auth.token_path().exists() and not desktop.exists()
    assert '"mcpServers"' in capsys.readouterr().out


def test_setup_cancelled(monkeypatch):
    def interrupt(prompt):
        raise KeyboardInterrupt

    with patch("webbrowser.open"), pytest.raises(SystemExit) as exc:
        cli._handle_setup(input_fn=interrupt)
    assert exc.value.code == 0 and not auth.token_path().exists()


def test_setup_alias_still_works(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "_handle_setup", lambda *a, **k: called.setdefault("yes", True))
    cli.main(["--setup"])
    assert called


def test_claude_code_add_runs_without_a_shell(monkeypatch):
    run = Mock(return_value=Mock(returncode=0))
    client_config.add_to_claude_code(run=run, which=lambda name: "/bin/claude")
    argv = run.call_args.args[0]
    assert argv == ["/bin/claude", "mcp", "add", "-s", "user", "remarkable", "--"] + [
        "uvx",
        "rm-mcp@latest",
    ]
    assert "shell" not in run.call_args.kwargs


def test_doctor_without_a_token(monkeypatch, tmp_path, capsys):
    _no_clients(monkeypatch, tmp_path)
    assert cli._handle_doctor() == 1
    out = capsys.readouterr().out
    assert "No reMarkable token" in out and "rm-mcp@latest setup" in out


def test_doctor_with_a_token_offline(monkeypatch, tmp_path, capsys):
    _no_clients(monkeypatch, tmp_path)
    auth.save_token({"devicetoken": jwt(2e9), "usertoken": ""})
    assert cli._handle_doctor(online=False) == 0
    out = capsys.readouterr().out
    assert "token file" in out and jwt(2e9) not in out


# =============================================================================
# One-time code (Claude Desktop bundle)
# =============================================================================


def test_one_time_code_is_redeemed_once_when_no_token_exists(monkeypatch):
    monkeypatch.setenv("REMARKABLE_ONE_TIME_CODE", "abcdefgh")
    response = Mock(status_code=200, text="device-from-code")
    with patch("requests.post", return_value=response) as post:
        client = api_mod.get_rmapi()
        assert client.device_token == "device-from-code"
        monkeypatch.setattr(api_mod, "_client_singleton", None)
        monkeypatch.setattr(api_mod, "_client_source", None)
        assert api_mod.get_rmapi().device_token == "device-from-code"
    assert post.call_count == 1
    assert auth.find_token().kind == "file"


def test_one_time_code_is_ignored_when_a_token_exists(monkeypatch):
    write_file_token()
    monkeypatch.setenv("REMARKABLE_ONE_TIME_CODE", "abcdefgh")
    with patch("requests.post") as post:
        api_mod.get_rmapi()
    post.assert_not_called()


def test_a_used_code_says_how_to_get_a_new_one(monkeypatch):
    monkeypatch.setenv("REMARKABLE_ONE_TIME_CODE", "abcdefgh")
    with patch("requests.post", return_value=Mock(status_code=400, text="")):
        with pytest.raises(RuntimeError, match="Codes work once"):
            api_mod.get_rmapi()


def test_setup_while_running_on_an_env_token_switches_to_the_file(monkeypatch):
    monkeypatch.setenv("REMARKABLE_TOKEN", ENV_TOKEN)
    first = api_mod.get_rmapi()
    assert first.device_token == jwt(2e9, "env")
    assert api_mod.get_rmapi() is first
    auth.save_token({"devicetoken": jwt(2e9, "new"), "usertoken": ""})
    assert api_mod.get_rmapi().device_token == jwt(2e9, "new")
