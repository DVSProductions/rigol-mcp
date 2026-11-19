# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Tools no longer look hung when the scope cannot be reached. On 2026-11-19
  `scope_info` failed twice after 10 s with only "timed out", while a fresh
  socket to the scope answered `*IDN?` at once. The real cause: that Claude
  session's `rigol` server had been started on 2026-11-16 with
  `RIGOL_HOST=192.168.33.36`; the scope moved to a static 192.168.33.250 on
  2026-11-17 and `RIGOL_HOST` was edited then, but a running server keeps the
  environment it was started with. Every call waited out the 10 s connect
  timeout against the old address (read back from the server process's
  environment; the old code reproduces the same "timed out" after 10.0 s). No
  connection to the scope was involved. Connecting now has its own short
  timeout (`RIGOL_CONNECT_TIMEOUT`, default 3 s), and the error names the
  address, `RIGOL_HOST`, and that the server must be reconnected after a change.
- A scope blocked by another client no longer makes every query wait out the
  reply timeout. Measured on a DS1104Z Plus (00.04.05.SP2): an idle second
  client does not block it, but a client that stops reading in the middle of a
  reply blocks every other connection (accepted, never answered) until it is
  killed, which frees the scope at once. Each call now checks `*IDN?` on its
  connection first and fails within 3 s with "port 5555 busy or scope not
  answering", naming the processes on this PC with a live connection to it.
- Settings sent at the end of a call could be lost: the scope drops commands
  it has not read yet when the connection closes (a trailing `:RUN` after 18
  settings was lost every time). A call that ends on a setting now waits for
  `*OPC?` before closing and fails loudly if the scope does not confirm.
- `mcp[cli]` is pinned below 2 (`>=1.26,<2`): mcp 2.0 renamed FastMCP to
  MCPServer, so a fresh install, and CI, could no longer import the server.
- A query the scope never answers (a typo, a setting sent with `?`) now fails
  naming the command and pointing at `:SYSTem:ERRor?`, instead of "timed out".

### Added
- `poll`: measurements of one or more channels every `interval_ms` for
  `seconds` (at most 1200), with PC timestamps, on one connection. The CSV is
  written to `RIGOL_OUT_DIR` row by row; optional `trig_status` column;
  `stop_after_stop_s` ends a single-shot recording a few seconds after the
  scope reports STOP. It only reads (never `:RUN`, `:STOP` or a setting),
  reports progress every 5 s, and stops early when the client cancels.
- `scpi` takes a list of commands and runs them in order on one connection,
  returning each reply, or `OK` for a setting. One string works as before.
- `get_waveform` takes several channels (`"CH1,CH4"` or a list) and reads them
  from one acquisition: one stop, one resume (`:RUN`, or `:SINGle` for an armed
  single shot) when the scope was running; `screenshot` saves the screen as PNG
  beside the CSVs; `name` sets the file names. One channel works as before.
- `timeout_s` on `scpi`, `poll`, `get_waveform` and `get_screenshot`.
- `RIGOL_CONNECT_TIMEOUT` environment variable.
- Tool tests in `tests/` against a fake SCPI server (no scope needed), run by
  CI; `test` extra in `pyproject.toml`.

### Changed
- Calls of one server never overlap: a call that arrives while a `poll` runs
  fails at once, saying so, instead of opening a second connection.
- README: tool table, "One client at a time" and "Long calls" sections, and
  new gotchas (dropped settings, measurements need acquired data, per-source
  trigger level). Docs and code are plain ASCII.

## [0.1.0] - 2026-07-02

### Added
- Initial release of the Rigol DS1000Z / MSO1000Z MCP server.
- Tools: `scope_info`, `get_screenshot`, `get_stats`, `get_waveform`,
  `plot_waveform`, `run_control`, `set_channel`, `set_timebase`,
  `set_trigger`, `set_acquire`, and a raw `scpi` escape hatch.
- Raw SCPI-over-TCP transport (port 5555) with IEEE 488.2 block reads: no
  VISA, UltraSigma, or drivers required.
- Chunked deep-memory (RAW) capture with automatic reassembly and retry.
- `main()` console entry point and `rigol-mcp` script.
- Packaging (`pyproject.toml`), MIT license, CI (ruff + multi-version import
  smoke test).

[Unreleased]: https://github.com/DVSProductions/rigol-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DVSProductions/rigol-mcp/releases/tag/v0.1.0
