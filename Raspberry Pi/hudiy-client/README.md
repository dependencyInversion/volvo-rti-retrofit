# hudiy-client

WebSocket/Protobuf client for the [Hudiy](https://github.com/wiboma/hudiy) in-car API
(`Api.proto`), for use in the RTI retrofit's Raspberry Pi.

Implements the WebSocket transport only (default port `44406`): a 12-byte header
(`payload_size`, `message_id`, `flags`, all little-endian `uint32`) followed by the
protobuf-serialized payload, sent as a single binary frame. On connect the client performs
the `HelloRequest`/`HelloResponse` handshake and auto-replies to keepalive pings.

## Install

```bash
poetry install
```

## Regenerating the protobuf stub

`hudiy_client/generated/Api_pb2.py` is committed, so this is only needed after updating
`proto/Api.proto`:

```bash
poetry run python -m grpc_tools.protoc -I proto --python_out=hudiy_client/generated proto/Api.proto
```

## Navigation playground

An interactive example that connects to a running Hudiy instance and sends `KeyEvent`
navigation commands in reaction to single key presses -- no need to type a word and hit
Enter, and it requires a real interactive terminal (not a piped/redirected stdin):

```bash
poetry run python examples/navigation_playground.py --host <hudiy-host> --port 44406
```

Controls: arrow keys navigate (left/right default to scroll -- see below), `Tab`/`Shift+Tab`
also scroll right/left, `Enter`, `b`=back, `h`=home, `n`=skip, `p`=revert, `f`=toggle input
focus, `m`=media menu, `g`=navigation menu, `c`=phone menu, `q` (or Ctrl+C) to disconnect.
`m`/`g`/`c` send `KEY_TYPE_MEDIA_MENU`/`KEY_TYPE_NAVIGATION_MENU`/`KEY_TYPE_PHONE_MENU` --
direct shortcuts to the apps CarPlay/Android Auto's dock usually holds, worth trying if the
dock itself isn't reachable via D-pad/scroll navigation.

Holding a key repeats it at a throttled, rate-limited interval (default 150ms, tune with
`--repeat-interval-ms`) instead of sending one `KeyEvent` per raw OS/keyboard auto-repeat
signal -- important since a real held key can generate dozens of raw repeats per second,
which would otherwise flood the socket. Each send is logged, and releasing the key (or
switching to a different one) prints a summary, e.g.:

```
[KEY] up sent (#1)
[KEY] up sent (#2)
[KEY] up released -- sent 2x, saw 11 raw signal(s) over 0.4s
```

`n`/`p` (skip/revert) send Hudiy's `KEY_TYPE_NEXT_TRACK`/`KEY_TYPE_PREVIOUS_TRACK`. They work
normally while paused (as long as some media source is active) and are only suppressed when
Hudiy reports no media source at all
(`MEDIA_SOURCE_NONE`, tracked via a `MEDIA` status subscription), since Hudiy can hang
handling next/previous track with nothing loaded to skip within. A `[MEDIA]` line is only
printed when the source or play state actually changes, not on every update (Hudiy ticks
`MediaStatus` roughly once a second while something is playing).

`f` sends `KEY_TYPE_TOGGLE_INPUT_FOCUS`, which moves key-input focus between the projected app
(Android Auto/CarPlay) and Hudiy's own overlay.

Plain `KEY_TYPE_LEFT`/`KEY_TYPE_RIGHT` don't appear to do anything at all -- not in Hudiy's own
UI, not in Android Auto, not in CarPlay. `KEY_TYPE_SCROLL_LEFT`/`KEY_TYPE_SCROLL_RIGHT` (what
`main_configuration.md`'s `activeBoundaries` option describes as jumping between input scopes)
is what actually navigates left/right in all three, so that's what the left/right arrow keys
send by default, and `Tab`/`Shift+Tab` are bound to the same two key types as a convenient
alias. This is easily reversible: pass `--left-right-mode key` to make the arrow keys send the
literal (currently useless) directional keys instead, or `--left-right-mode scroll` to be
explicit about the default. The controls banner printed at startup shows which mode is active.

The playground prints the connection status, the handshake result, and `[SENT]`/`[FAIL]`/`[KEY]`
for every command. Pass `-v`/`--verbose` to also log every raw message sent and received on the
wire (message name, id, size) to the console, or `--log-file PATH` to always capture that same
debug-level transcript to disk regardless of `-v` -- handy for sharing a session, especially
once this runs unattended on the Pi with no one watching a terminal.

If something still looks wrong from Hudiy's own side, it's launched via the `hudiy.service`
systemd unit in console/EGL mode, so its own logs can be inspected with `journalctl -u
hudiy.service` on the Pi.
