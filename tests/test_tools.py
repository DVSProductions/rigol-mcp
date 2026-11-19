"""Tool tests against the FakeScope in conftest.py: no scope needed."""

import asyncio
import csv
import os
import socket
import threading
import time

import pytest

import rigol_mcp
from rigol_mcp import ScopeError

SETTINGS = (":RUN", ":STOP", ":SING")


def run(coro):
    return asyncio.run(coro)


def queries_only(log):
    """True when every command sent was a query (poll must never change anything)."""
    return all("?" in c for c in log)


# --------------------------------------------------------------------------- #
# scpi
# --------------------------------------------------------------------------- #
def test_scpi_single_forms_unchanged(fake):
    assert rigol_mcp.scpi("*IDN?").startswith("RIGOL TECHNOLOGIES")
    assert rigol_mcp.scpi(":CHAN3:DISP ON") == "OK (write sent)"
    assert fake.log[-2:] == [":CHAN3:DISP ON", "*OPC?"]  # confirmed before closing


def test_scpi_several_commands_on_one_connection_in_order(fake):
    fake.replies[":CHAN1:SCAL?"] = "5.000000e+00"
    fake.meas[("VAVG", "CHAN3")] = "1.234500e-02"
    out = rigol_mcp.scpi([":CHAN1:SCAL?", ":CHAN3:DISP ON", ":MEAS:ITEM? VAVG,CHAN3"])
    lines = out.splitlines()
    assert lines[0].split() == [":CHAN1:SCAL?", "5.000000e+00"]
    assert lines[1].split() == [":CHAN3:DISP", "ON", "OK"]
    assert lines[2].split() == [":MEAS:ITEM?", "VAVG,CHAN3", "1.234500e-02"]
    # one connection: one *IDN? check, then the three commands in order
    assert fake.log == ["*IDN?", ":CHAN1:SCAL?", ":CHAN3:DISP ON", ":MEAS:ITEM? VAVG,CHAN3"]


def test_scpi_unanswered_query_names_it_and_keeps_earlier_replies(fake):
    fake.replies[":CHAN1:SCAL?"] = "5.000000e+00"
    fake.no_reply.add(":FOO?")
    t0 = time.monotonic()
    with pytest.raises(ScopeError) as e:
        rigol_mcp.scpi([":CHAN1:SCAL?", ":FOO?", "*IDN?"], timeout_s=0.5)
    assert time.monotonic() - t0 < 3
    msg = str(e.value)
    assert "command 2 of 3" in msg and "no reply to ':FOO?'" in msg
    assert ":CHAN1:SCAL?  5.000000e+00" in msg


# --------------------------------------------------------------------------- #
# fail fast: busy port, nothing at the address
# --------------------------------------------------------------------------- #
def test_busy_port_fails_fast_with_a_clear_message(fake):
    # The fake, like a scope stuck on a stalled client, serves only this connection.
    holder = socket.create_connection(("127.0.0.1", fake.port))
    try:
        time.sleep(0.2)
        listed = rigol_mcp._local_holders("127.0.0.1", fake.port)
        t0 = time.monotonic()
        with pytest.raises(ScopeError) as e:
            rigol_mcp.scope_info()
        took = time.monotonic() - t0
    finally:
        holder.close()
    msg = str(e.value)
    assert took < 5
    assert f"127.0.0.1:{fake.port}" in msg
    assert "busy or scope not answering" in msg
    assert "one client at a time" in msg
    if listed is not None:  # this PC's connection list was readable: the holder is named
        assert f"pid {os.getpid()} " in msg
    # the next call works again once the other client is gone
    time.sleep(0.3)
    assert "FAKE0001" in rigol_mcp.scope_info()


def test_no_answer_at_the_address_names_rigol_host(fake, monkeypatch):
    def unreachable(addr, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(rigol_mcp.socket, "create_connection", unreachable)
    with pytest.raises(ScopeError) as e:
        rigol_mcp.scpi("*IDN?")
    msg = str(e.value)
    assert "no connection to 127.0.0.1:" in msg
    assert "RIGOL_HOST" in msg and "reconnect the MCP client" in msg


def test_failed_call_frees_the_server_for_the_next(fake):
    fake.mute = True
    with pytest.raises(ScopeError):
        rigol_mcp.scpi("*IDN?")
    fake.mute = False
    time.sleep(0.3)
    assert rigol_mcp.scpi("*IDN?").startswith("RIGOL")


# --------------------------------------------------------------------------- #
# settings are confirmed before the connection closes
# --------------------------------------------------------------------------- #
def test_settings_are_confirmed_before_closing(fake):
    # The scope drops commands it has not read yet when the connection closes,
    # so a call that ends on a write waits for *OPC? first.
    rigol_mcp.set_channel("CH3", display=True, offset=-4.0)
    assert fake.log == ["*IDN?", ":CHANnel3:OFFSet -4.0", ":CHANnel3:DISPlay ON", "*OPC?"]


def test_unconfirmed_settings_fail_loudly(fake):
    fake.no_reply.add("*OPC?")
    with pytest.raises(ScopeError, match="did not confirm the commands sent"):
        rigol_mcp.set_timebase(scale=0.005)


def test_a_call_ending_on_a_query_needs_no_confirmation(fake):
    rigol_mcp.run_control("run")
    assert fake.log == ["*IDN?", ":RUN", ":TRIGger:STATus?"]


# --------------------------------------------------------------------------- #
# poll
# --------------------------------------------------------------------------- #
def test_poll_short_writes_csv_and_sends_only_queries(fake, tmp_path):
    fake.meas[("VAVG", "CHAN3")] = "1.234500e-02"
    fake.meas[("VAVG", "CHAN4")] = "5.050000e+00"
    out = run(rigol_mcp.poll(channels="CH3,CH4", seconds=0.6, interval_ms=100, name="p1"))
    path = tmp_path / "p1.csv"
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["time", "t_s", "CHAN3_VAVG", "CHAN4_VAVG"]
    assert 4 <= len(rows) - 1 <= 7
    assert all(r[2:] == ["0.012345", "5.05"] for r in rows[1:])
    assert all(len(r[0]) == 12 and r[0][2] == ":" for r in rows[1:])  # HH:MM:SS.fff
    assert "ended: time up" in out and str(path) in out
    assert "CHAN4_VAVG: min 5.05  max 5.05" in out
    assert queries_only(fake.log)


def test_poll_marks_invalid_readings_empty(fake, tmp_path):
    fake.meas[("VAVG", "CHAN1")] = "9.9E37"
    out = run(rigol_mcp.poll(channels=["CH1"], seconds=0.3, interval_ms=100, name="p2"))
    with open(tmp_path / "p2.csv", newline="") as f:
        rows = list(csv.reader(f))[1:]
    assert rows and all(r[2] == "" for r in rows)
    assert "no valid reading" in out


def test_poll_single_shot_stops_after_stop(fake, tmp_path):
    fake.status = ["WAIT", "WAIT", "STOP"]
    fake.meas[("VMAX", "CHAN4")] = "5.0e+00"
    t0 = time.monotonic()
    out = run(
        rigol_mcp.poll(
            channels="CH4", items="VAVG,VMAX", seconds=30, interval_ms=50,
            stop_after_stop_s=0.3, name="p3",
        )
    )
    assert time.monotonic() - t0 < 3
    assert "the scope reported STOP, then 0.3 s" in out
    with open(tmp_path / "p3.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["time", "t_s", "trig_status", "CHAN4_VAVG", "CHAN4_VMAX"]
    assert [r[2] for r in rows[1:4]] == ["WAIT", "WAIT", "STOP"]
    assert queries_only(fake.log)
    assert not any(c.startswith(SETTINGS) for c in fake.log)


def test_poll_cancelled_keeps_the_csv_and_frees_the_scope(fake, tmp_path):
    fake.meas[("VAVG", "CHAN1")] = "1.0"

    async def start_then_cancel():
        task = asyncio.create_task(
            rigol_mcp.poll(channels="CH1", seconds=60, interval_ms=50, name="p4")
        )
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(start_then_cancel())
    assert rigol_mcp._scope_lock.acquire(timeout=3)  # the worker let go of the scope
    rigol_mcp._scope_lock.release()
    with open(tmp_path / "p4.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert len(rows) > 3


def test_a_call_during_a_poll_fails_fast_instead_of_queueing(fake, monkeypatch):
    monkeypatch.setattr(rigol_mcp, "LOCK_WAIT_S", 0.2)
    fake.meas[("VAVG", "CHAN1")] = "1.0"
    result = {}

    def poller():
        result["out"] = run(rigol_mcp.poll(channels="CH1", seconds=1.0, interval_ms=100))

    th = threading.Thread(target=poller)
    th.start()
    time.sleep(0.4)
    with pytest.raises(ScopeError) as e:
        rigol_mcp.scpi("*IDN?")
    th.join()
    assert "still using the scope for a poll" in str(e.value)
    assert "ended: time up" in result["out"]


def test_poll_rejects_runs_longer_than_the_cap(fake):
    with pytest.raises(ValueError, match="1200"):
        run(rigol_mcp.poll(seconds=5000))


# --------------------------------------------------------------------------- #
# get_waveform
# --------------------------------------------------------------------------- #
def _two_channels(fake):
    # CH1: 0.1 V per count around 128; CH4: 0.02 V per count, other origin
    fake.preambles["CHAN1"] = "0,2,1200,1,1.000000e-03,-6.000000e-01,0,1.0e-01,0,128"
    fake.preambles["CHAN4"] = "0,2,1200,1,1.000000e-03,-6.000000e-01,0,2.0e-02,10,118"
    fake.waves["CHAN1"] = bytes([128, 138, 148, 118] * 300)
    fake.waves["CHAN4"] = bytes([128, 129, 130, 131] * 300)


def test_get_waveform_several_channels_one_stop_one_run(fake, tmp_path):
    _two_channels(fake)
    fake.status = ["WAIT"]
    res = rigol_mcp.get_waveform(channel=["CH1", "CH4"], screenshot=True, name="w1")
    ch1, ch4 = res["channels"]
    assert (ch1["source"], ch4["source"]) == ("CHANnel1", "CHANnel4")
    assert ch1["samples"] == ch4["samples"] == 1200
    assert ch1["v_min"] == pytest.approx(-1.0) and ch1["v_max"] == pytest.approx(2.0)
    assert ch4["v_min"] == pytest.approx(0.0) and ch4["v_max"] == pytest.approx(0.06)
    with open(tmp_path / "w1-CHAN1.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["time_s", "volts"]
    assert float(rows[1][0]) == pytest.approx(-0.6) and float(rows[2][1]) == pytest.approx(1.0)
    assert (tmp_path / "w1-CHAN4.csv").exists()
    assert (tmp_path / "w1.png").read_bytes() == fake.png
    assert res["screenshot_path"] == str(tmp_path / "w1.png")
    assert res["resumed_with"] == ":RUN"
    assert fake.log.count(":STOP") == 1 and fake.log.count(":RUN") == 1
    assert fake.log.index(":STOP") < fake.log.index(":DISPlay:DATA? ON,OFF,PNG")
    assert fake.log[-2:] == [":RUN", "*OPC?"]


def test_get_waveform_leaves_a_stopped_scope_stopped(fake):
    _two_channels(fake)
    fake.status = ["STOP"]
    res = rigol_mcp.get_waveform(channel="CH1,CH4", save_csv=False)
    assert res["resumed_with"] is None
    assert ":STOP" not in fake.log and ":RUN" not in fake.log


def test_get_waveform_rearms_an_armed_single_shot(fake):
    _two_channels(fake)
    fake.status = ["WAIT"]
    fake.sweep = "SING"
    res = rigol_mcp.get_waveform(channel="CH1,CH4", save_csv=False)
    assert res["resumed_with"] == ":SINGle"
    assert fake.log[-2:] == [":SINGle", "*OPC?"]


def test_get_waveform_single_channel_form_unchanged(fake, tmp_path):
    _two_channels(fake)
    res = rigol_mcp.get_waveform("CH4", preview_points=10)
    assert res["source"] == "CHANnel4" and res["samples"] == 1200
    assert len(res["preview_volts"]) == 10
    assert res["csv_path"].startswith(str(tmp_path))
    assert ":STOP" not in fake.log and ":RUN" not in fake.log  # NORM reads a running scope


def test_channel_lists_accept_the_usual_spellings():
    assert rigol_mcp._chan_list("CH3,CH4") == ["CHANnel3", "CHANnel4"]
    assert rigol_mcp._chan_list(["chan1", "4", "CH1"]) == ["CHANnel1", "CHANnel4"]
    assert rigol_mcp._chan_list('["CH2", "MATH"]') == ["CHANnel2", "MATH"]
