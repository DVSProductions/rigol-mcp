# Rigol DS1054Z MCP server

[![CI](https://github.com/DVSProductions/rigol-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/DVSProductions/rigol-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-server-orange.svg)](https://modelcontextprotocol.io)

Drive a Rigol DS1054Z (or any DS1000Z / MSO1000Z series scope) from Claude Code
over Ethernet: no UltraSigma, no NI-VISA, no drivers. The scope is an LXI
instrument that accepts plain SCPI on a raw TCP socket (port **5555**); this
server wraps that as MCP tools.

## Tools

| Tool | What it does |
|------|--------------|
| `scope_info` | `*IDN?` + timebase, sample rate, memory depth, trigger, per-channel state |
| `get_screenshot` | live display as a **PNG** (Claude can see the trace/menus) |
| `get_stats` | measurements once: Vpp, Vmax/min/avg/rms, freq, period, width, duty, rise/fall |
| `poll` | measurements of one or more channels every `interval_ms` for `seconds`, with PC timestamps, on one connection; CSV written row by row; optional trigger-status column and single-shot stop; only reads, never sends `:RUN` or a setting |
| `get_waveform` | traces of one or more channels (NORM screen ~1200 pts, or RAW deep memory) -> stats + one CSV per channel; several channels come from one acquisition (one stop, one resume), optionally with the screen saved as PNG |
| `plot_waveform` | capture + render a PNG plot (needs `matplotlib`) |
| `run_control` | run / stop / single / force / autoscale |
| `set_channel` | per-channel scale / offset / coupling / display / probe |
| `set_timebase` | timebase scale + offset |
| `set_trigger` | edge source / level / slope / sweep |
| `set_acquire` | memory depth |
| `scpi` | raw SCPI: one command, or a list run in order on one connection (replies in order, `OK` for settings) |

A few calls, as Claude would make them:

```text
scpi(command=[":CHAN1:SCAL?", ":CHAN3:DISP ON", ":MEAS:ITEM? VAVG,CHAN3"])
poll(channels="CH3,CH4", seconds=10, interval_ms=500)
poll(channels="CH4", items="VAVG,VMAX", seconds=600, interval_ms=250, stop_after_stop_s=2)
get_waveform(channel=["CH1", "CH4"], screenshot=true, name="fs-open")
```

The second `poll` is for a single shot you armed yourself (`run_control single`):
it adds a `trig_status` column and ends 2 s after the scope reports `STOP`.
`get_waveform` with several channels writes `fs-open-CHAN1.csv`,
`fs-open-CHAN4.csv` and `fs-open.png` to `RIGOL_OUT_DIR`.

## One client at a time

Use the scope from one client at a time: one Claude session's `rigol` server,
or one script. What a DS1104Z Plus (firmware 00.04.05.SP2) does, measured:

- It accepts a second TCP connection on 5555, and answers it while the first
  client is idle.
- It works through one request at a time. While a client has stopped reading
  in the middle of a reply (a screenshot, a waveform block), every other
  connection is accepted but gets no answer. Killing that client frees the
  scope at once.

What this server does about it:

- Every call opens one connection, checks that the scope answers `*IDN?` on it,
  and closes it again. A blocked port fails within `RIGOL_CONNECT_TIMEOUT`
  (3 s) with *"port 5555 busy or scope not answering"*, and the message names
  the processes on this PC that hold a live connection to the scope.
- Calls of this server never overlap: a call that arrives while a `poll` runs
  fails at once with *"this server is still using the scope for a poll"*.
- A running MCP server keeps the `RIGOL_HOST` it was started with. After you
  change it (or the scope's address changes), reconnect the server (`/mcp` in
  Claude Code, or a new session); until then the tools fail within 3 s with
  *"no connection to <old address>:5555 ... Check RIGOL_HOST"*.

## Long calls

`poll` runs up to 1200 s per call. Claude Code aborts an MCP call that sends no
response or progress for 30 minutes (observed: 1800 s); `poll` reports progress
every 5 s, stops within one interval when the client cancels the call, and
writes its CSV row by row, so the file is complete up to the last row even if
the client gave up. Split longer recordings into several calls.

## Setup

Clone the repo, then either install the dependencies directly:

```sh
pip install "mcp[cli]<2"    # required (2.x renamed the FastMCP API)
pip install matplotlib      # optional, only for plot_waveform
```

...or install the package itself (adds a `rigol-mcp` console script):

```sh
pip install .               # or:  pip install ".[plot]"  to include matplotlib
```

Find the scope's IP on the unit: **Utility > IO Setting > LAN Conf**
(this one is `192.168.178.96`). Sanity-check the link with a browser at
`http://<scope-ip>`: it serves an LXI welcome page. A static IP saves you from
chasing DHCP moves.

## Register with Claude Code

```sh
claude mcp add --env RIGOL_HOST=192.168.178.96 --scope user rigol -- python D:/ryzzenDL/rigol-mcp/rigol_mcp.py
```

> The `--env` flag must have another option (here `--scope user`) between it and
> the server name, or the CLI mis-parses the name. If `python` isn't on PATH,
> use the full `python.exe` path or `-- cmd /c python D:/...`.

If you `pip install`ed the package, use the console script instead of a path:

```sh
claude mcp add --env RIGOL_HOST=192.168.178.96 --scope user rigol -- rigol-mcp
```

Or add it to a `.mcp.json` (project scope); see [`.mcp.example.json`](.mcp.example.json):

```json
{
  "mcpServers": {
    "rigol": {
      "type": "stdio",
      "command": "python",
      "args": ["D:/ryzzenDL/rigol-mcp/rigol_mcp.py"],
      "env": { "RIGOL_HOST": "192.168.178.96" }
    }
  }
}
```

Then `claude mcp list` to confirm it connects, and ask Claude things like
*"screenshot the scope"*, *"what's the frequency on channel 1?"*, or
*"capture CH1 deep memory and plot it"*.

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `RIGOL_HOST` | `192.168.178.96` | scope IP (read once, when the server starts) |
| `RIGOL_PORT` | `5555` | raw SCPI socket port |
| `RIGOL_TIMEOUT` | `10` | reply timeout (seconds); tools with `timeout_s` override it per call |
| `RIGOL_CONNECT_TIMEOUT` | `3` | seconds to connect and get the scope's answer to `*IDN?` |
| `RIGOL_OUT_DIR` | `./captures` | where `get_waveform` and `poll` write CSVs and PNGs |

## Notes / gotchas

- **RAW captures stop the scope.** Internal memory is only readable in STOP
  state; the deep-memory read also chunks at 250k points (a hard per-read limit)
  and reassembles, handled for you.
- Asking for the whole multi-megapoint buffer in one shot makes the scope reply
  *"Memory lack in waveform reading!"*; always chunk (this server does).
- VXI-11 (`pyvisa`/`python-vxi11`) also works but is slower and flakier on old
  firmware; the raw socket used here is the robust path.
- **The scope drops commands it has not read yet when a connection closes.**
  Measured: a `:RUN` at the end of 18 settings was lost every time while the
  scope was still applying them. So a call that ends on a setting waits for
  `*OPC?` before it closes, and fails loudly if the scope does not confirm.
- **Measurements need acquired data.** The scope answers `9.9E37` (`null` in
  `get_stats`, an empty cell in `poll`) for a channel that is off, a value it
  cannot measure, and a channel with nothing acquired: in NORM sweep with no
  trigger (status `WAIT`), a channel you just switched on reads nothing. Use
  AUTO sweep to read levels.
- **`get_stats` and `poll` change the display.** On the DS1000Z, *querying*
  `:MEAS:ITEM? X` also *adds X to the on-screen measurement bar*; there is no
  pure-read variant. Reading measurements reshuffles the scope's displayed
  measurement list (cosmetic).
- **The edge trigger level is stored per source.** `:TRIG:EDG:LEV?` reads the
  level of the current source; switching the source back brings that source's
  own level back. Setting a level while another source is selected changes
  that source's stored level, which a later switch to it will show.
- **Changing a channel's `scale` also moves its `offset`.** The scope keeps the
  trace's division position, so V/div and offset-in-volts track together; pass
  `offset` explicitly alongside `scale` if you need an exact vertical position.
- **`set_acquire AUTO` only reverts to AUTO mode while the scope is running.**
  Sent while stopped it's ignored and `:ACQ:MDEP?` keeps the last fixed depth.
- **`RTIMe`/`FTIMe` return `9.9E37` (-> `null`) on ringy/overshooting edges**:
  the scope's built-in rise/fall measurement gives up when the signal crosses the
  90% threshold multiple times. Compute 10-90% from `get_waveform` samples instead.
- **First `plot_waveform` call can take 2-3 minutes** on a fresh machine while
  matplotlib builds its font cache (one-time, looks like a hang). Subsequent
  calls are fast. To avoid it, pre-warm once: `python -c "import matplotlib.pyplot"`.
- **`scpi` query detection:** a command is treated as a query if it contains `?`
  (queries may carry args after the `?`, e.g. `:MEAS:ITEM? VPP,CHAN1`). A query
  the scope does not answer (a typo, a setting sent with `?`) fails after the
  reply timeout naming the command; read `:SYSTem:ERRor?` to see why.

## Contributing

Issues and PRs welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). CI lints with
[ruff](https://docs.astral.sh/ruff/), runs an import smoke test, and runs the
tool tests in `tests/` against a fake scope on Python 3.10 to 3.13 (no scope
required).

## License

[MIT](LICENSE) (c) Valentino Saitz
