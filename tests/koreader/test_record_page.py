"""Run the Lua record-page spec under pytest.

The spec stubs the KOReader modules and drives the page with a state file it
writes itself, so this test never touches a Kindle, Bluetooth, or the control
socket. Lua runs through lupa; the module skips when lupa is not installed.
"""

import pathlib

import pytest

lupa = pytest.importorskip("lupa")

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = ROOT / "tests" / "koreader" / "voicebridge_spec.lua"
PLUGIN = ROOT / "koreader" / "voicebridge.koplugin" / "main.lua"


def test_record_page_state_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEBRIDGE_PLUGIN", str(PLUGIN))
    monkeypatch.setenv("VOICEBRIDGE_STATE", str(tmp_path / "session.state"))

    lua = lupa.LuaRuntime()
    lua.execute(SPEC.read_text(encoding="utf-8"))
