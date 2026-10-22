# README

A terminal monitor for OpenCode sessions and their direct subagents, showing status and approximate context usage.

## Requirements

- Python 3.9+ and a terminal with POSIX TTY support (macOS or Linux).
- The `opencode` executable on your `PATH`.

## Run

From this repository, run `python3 bin/octop.py`, or use `make install-octop` to create a symlink at `~/.local/bin/octop` (ensure that directory is on your `PATH`). Then run `octop` from the directory whose sessions you want to see.

**Terminal tip:** Do not run OpenCode in a tmux split pane; it becomes unusable. To view OpenCode and octop side by side, use your terminal's built-in split-pane feature instead.

Without a session ID, octop opens a picker of root sessions for the current directory, ordered by most recent update; enter `1` for the newest. Press `t` to show sessions from other directories. If none match the current directory, octop shows all available sessions instead. The picker also supports `e <number>` to export a session as JSON, `i` to import a session JSON file, and `q` to quit.

To skip the picker, pass a session ID: `octop ses_...` (or `octop ID=ses_...`). By default, octop starts a private local OpenCode server; if `OPENCODE_SERVER_PASSWORD` is set, it connects to an existing server at `127.0.0.1:4096` instead. Use `--host` and `--port` to select a different existing server.

## Monitor keys

Press `h` to show or hide the header with column settings and shortcuts. The header is hidden by default. Press `s` for status, `b` for progress bars, `p` for percentage, `t` for token counts, `m` for model, `d` for date, `c` for time, `f` for full titles, or `a` for abbreviated agent labels. These settings last only for the current run. Press `Ctrl-S` to return to the session picker, or `Ctrl+C` to stop.

Use `octop --help` for startup flags, including refresh intervals and column defaults. Context figures are estimates, not exact token counts.

## Tests

Run `make test`.
