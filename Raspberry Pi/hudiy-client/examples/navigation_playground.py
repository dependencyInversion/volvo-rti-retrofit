"""Interactive playground for sending Hudiy navigation KeyEvents over WebSocket.

Reacts to single key presses -- no need to type a word and hit Enter. Holding a
key repeats it at a throttled, rate-limited interval instead of flooding the
socket with one send per raw OS/keyboard auto-repeat signal.

Usage:
    poetry run python examples/navigation_playground.py --host 127.0.0.1 --port 44406
    poetry run python examples/navigation_playground.py --host pi.local -v   # verbose wire logging
    poetry run python examples/navigation_playground.py --host pi.local --log-file session.log
"""

import argparse
import logging
import os
import select
import sys
import termios
import threading
import time
import tty

from google.protobuf.descriptor import FieldDescriptor

import hudiy_client.generated.Api_pb2 as hudiy_api
from hudiy_client import Client, ClientEventHandler

logger = logging.getLogger("navigation_playground")

# Single-key bindings. Escape-sequence keys (arrows, Shift+Tab) are handled
# separately in read_key()/ESCAPE_SEQUENCE_KEYS.
KEY_BINDINGS = {
    "\r": ("enter", hudiy_api.KeyEvent.KEY_TYPE_ENTER),
    "\n": ("enter", hudiy_api.KeyEvent.KEY_TYPE_ENTER),
    "\t": ("scroll_right", hudiy_api.KeyEvent.KEY_TYPE_SCROLL_RIGHT),
    "b": ("back", hudiy_api.KeyEvent.KEY_TYPE_BACK),
    "h": ("home", hudiy_api.KeyEvent.KEY_TYPE_HOME),
    # Hudiy has no dedicated skip/revert keys; these map onto the media
    # previous/next-track keys, which double as menu skip/revert controls.
    "n": ("skip", hudiy_api.KeyEvent.KEY_TYPE_NEXT_TRACK),
    "p": ("revert", hudiy_api.KeyEvent.KEY_TYPE_PREVIOUS_TRACK),
    # Moves key-input focus between the projected app (Android Auto/CarPlay)
    # and Hudiy's own overlay -- useful when D-pad navigation seems "stuck".
    "f": ("focus", hudiy_api.KeyEvent.KEY_TYPE_TOGGLE_INPUT_FOCUS),
    # Direct shortcuts to the apps CarPlay/Android Auto's dock usually holds --
    # useful if the dock itself isn't reachable via D-pad/scroll navigation.
    "m": ("media_menu", hudiy_api.KeyEvent.KEY_TYPE_MEDIA_MENU),
    "g": ("nav_menu", hudiy_api.KeyEvent.KEY_TYPE_NAVIGATION_MENU),
    "c": ("phone_menu", hudiy_api.KeyEvent.KEY_TYPE_PHONE_MENU),
}
# ESC [ <char> sequences: arrow keys, plus Shift+Tab (ESC [ Z).
# "C"/"D" (right/left arrow) are filled in by main() based on --left-right-mode.
ESCAPE_SEQUENCE_KEYS = {
    "A": ("up", hudiy_api.KeyEvent.KEY_TYPE_UP),
    "B": ("down", hudiy_api.KeyEvent.KEY_TYPE_DOWN),
    "Z": ("scroll_left", hudiy_api.KeyEvent.KEY_TYPE_SCROLL_LEFT),
}
# Plain KEY_TYPE_LEFT/RIGHT didn't seem to do anything in CarPlay, so the
# right/left arrow keys default to sending scroll events instead -- pass
# --left-right-mode key to revert to the literal directional keys.
LEFT_RIGHT_KEY_TYPES = {
    "scroll": (hudiy_api.KeyEvent.KEY_TYPE_SCROLL_LEFT, hudiy_api.KeyEvent.KEY_TYPE_SCROLL_RIGHT),
    "key": (hudiy_api.KeyEvent.KEY_TYPE_LEFT, hudiy_api.KeyEvent.KEY_TYPE_RIGHT),
}
QUIT_KEYS = {"q", "\x03"}  # 'q' or Ctrl+C

# How often (seconds) a held key is allowed to re-send while repeating.
# Overridable via --repeat-interval-ms; caps the outgoing KeyEvent rate so a
# held key (keyboard auto-repeat, or a polled GPIO button) can't flood Hudiy.
REPEAT_INTERVAL_S = 0.15
# How long (seconds) of silence on a key before we consider it released.
HOLD_RELEASE_TIMEOUT_S = 0.18
# Poll granularity for the main loop -- short enough to detect the release
# timeout and repeat-interval boundaries promptly.
POLL_INTERVAL_S = 0.05

hello_received = threading.Event()
connection_lost = threading.Event()
shutting_down = threading.Event()

media_lock = threading.Lock()
# Assume no media source until Hudiy says otherwise.
media_state = {"source": hudiy_api.MediaSource.MEDIA_SOURCE_NONE, "is_playing": False}


def update_media_state(message):
    """Updates the cached media state; returns True if it actually changed."""
    with media_lock:
        changed = message.source != media_state["source"] or message.is_playing != media_state["is_playing"]
        media_state["source"] = message.source
        media_state["is_playing"] = message.is_playing
    return changed


def has_media_source():
    with media_lock:
        return media_state["source"] != hudiy_api.MediaSource.MEDIA_SOURCE_NONE


def send_key_event(client, key_type, event_type):
    key_event = hudiy_api.KeyEvent()
    key_event.key_type = key_type
    key_event.event_type = event_type
    client.send(hudiy_api.MESSAGE_KEY_EVENT, 0, key_event.SerializeToString())


def send_tap(client, key_type):
    send_key_event(client, key_type, hudiy_api.KeyEvent.EVENT_TYPE_PRESS)
    send_key_event(client, key_type, hudiy_api.KeyEvent.EVENT_TYPE_RELEASE)


def format_message(message):
    """One-line dump of a protobuf message for logging, with bytes fields
    (e.g. cover art PNGs) shown as a length instead of dumped as escaped
    text -- a raw text_format dump of a cover art image can be hundreds of
    KB on a single line."""
    parts = []
    for field, value in message.ListFields():
        if field.type == FieldDescriptor.TYPE_BYTES:
            parts.append(f"{field.name}=<{len(value)} bytes>")
        elif field.type == FieldDescriptor.TYPE_MESSAGE:
            parts.append(f"{field.name}={{{format_message(value)}}}")
        elif field.type == FieldDescriptor.TYPE_ENUM:
            parts.append(f"{field.name}={field.enum_type.values_by_number[value].name}")
        else:
            parts.append(f"{field.name}={value!r}")
    return " ".join(parts)


def end_hold(hold, now):
    duration = now - hold["started_at"]
    print(
        f"[KEY] {hold['name']} released -- sent {hold['sent_count']}x, "
        f"saw {hold['raw_count']} raw signal(s) over {duration:.2f}s"
    )


class EventHandler(ClientEventHandler):
    def on_message(self, client, name, message):
        if name == "HelloResponse":
            hello_received.set()
            if message.result == hudiy_api.HelloResponse.HELLO_RESPONSE_RESULT_OK:
                print(
                    f"[OK] handshake succeeded -- Hudiy app {message.app_version.major}.{message.app_version.minor}, "
                    f"api {message.api_version.major}.{message.api_version.minor}"
                )
            else:
                result_name = hudiy_api.HelloResponse.HelloResponseResult.Name(message.result)
                print(f"[FAIL] handshake rejected: {result_name}")
            return

        if name == "MediaStatus":
            if update_media_state(message):
                source_name = hudiy_api.MediaSource.Name(message.source)
                state = "playing" if message.is_playing else "paused/stopped"
                print(f"[MEDIA] source={source_name} state={state}")
            return

        text = format_message(message)
        print(f"[RECV] {name}" + (f": {text}" if text else ""))


def receive_loop(client):
    """Runs in the background so pings, the hello response, etc. get processed
    while the main thread is blocked waiting for interactive input."""
    try:
        while client.wait_for_message():
            pass
        print("[INFO] server closed the connection")
    except (ConnectionError, OSError) as e:
        if not shutting_down.is_set():
            print(f"[FAIL] connection lost: {e}")
    finally:
        connection_lost.set()


def read_key(fd):
    """Blocks until a key is available and returns it as a single character,
    or one of the ESCAPE_SEQUENCE_KEYS letters for an arrow/Shift+Tab key."""
    ch = os.read(fd, 1).decode(errors="ignore")

    if ch == "\x1b":
        # Could be a lone Escape, or the start of an escape sequence
        # (ESC [ A/B/C/D/Z). Peek briefly for the rest of the sequence.
        if select.select([fd], [], [], 0.05)[0]:
            rest = os.read(fd, 2).decode(errors="ignore")
            if len(rest) == 2 and rest[0] == "[":
                return rest[1]

    return ch


def setup_logging(verbose, log_file):
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        print(f"[INFO] logging full debug transcript to {log_file}")


def main():
    global REPEAT_INTERVAL_S

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="Hudiy host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=44406, help="Hudiy WebSocket port (default: 44406)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every raw message sent/received")
    parser.add_argument("--log-file", metavar="PATH", help="also write a full debug-level log to this file")
    parser.add_argument(
        "--repeat-interval-ms",
        type=int,
        default=int(REPEAT_INTERVAL_S * 1000),
        help="max rate, in ms, to resend a held key (default: 150)",
    )
    parser.add_argument(
        "--left-right-mode",
        choices=sorted(LEFT_RIGHT_KEY_TYPES),
        default="scroll",
        help="what the left/right arrow keys send: 'scroll' (KEY_TYPE_SCROLL_LEFT/RIGHT, "
        "default) or 'key' (the literal KEY_TYPE_LEFT/RIGHT)",
    )
    args = parser.parse_args()

    setup_logging(args.verbose, args.log_file)
    REPEAT_INTERVAL_S = args.repeat_interval_ms / 1000.0

    left_key_type, right_key_type = LEFT_RIGHT_KEY_TYPES[args.left_right_mode]
    ESCAPE_SEQUENCE_KEYS["D"] = ("left", left_key_type)
    ESCAPE_SEQUENCE_KEYS["C"] = ("right", right_key_type)

    client = Client("navigation playground")
    client.set_event_handler(EventHandler())

    url = f"ws://{args.host}:{args.port}/"
    print(f"[INFO] connecting to {url} ...")
    try:
        client.connect(args.host, args.port)
    except Exception as e:
        print(f"[FAIL] could not connect to {url}: {e}")
        sys.exit(1)
    print("[OK] WebSocket connection established, sent HelloRequest")

    threading.Thread(target=receive_loop, args=(client,), daemon=True).start()

    if not hello_received.wait(timeout=5):
        print(
            "[WARN] no HelloResponse after 5s -- is this really a Hudiy WebSocket endpoint, "
            "and is the API enabled in main_configuration.json?"
        )

    client.subscribe([hudiy_api.SetStatusSubscriptions.MEDIA])

    print(
        "Controls: up/down | left/right (mode: "
        f"{args.left_right_mode}) | Tab/Shift+Tab=scroll right/left | Enter | b=back | "
        "h=home | n=skip | p=revert | f=toggle input focus | m=media menu | g=nav menu | "
        "c=phone menu | q=quit (Ctrl+C also works)"
    )
    print("Note: skip/revert are ignored while Hudiy reports no active media source.")

    stdin_fd = sys.stdin.fileno()
    if not os.isatty(stdin_fd):
        print("[FAIL] this playground needs an interactive terminal (stdin is not a tty)")
        shutting_down.set()
        client.disconnect()
        sys.exit(1)

    old_settings = termios.tcgetattr(stdin_fd)
    active_hold = None
    try:
        tty.setcbreak(stdin_fd)

        while not connection_lost.is_set():
            ready = select.select([stdin_fd], [], [], POLL_INTERVAL_S)[0]
            now = time.monotonic()

            if not ready:
                if active_hold is not None and now - active_hold["last_seen_at"] >= HOLD_RELEASE_TIMEOUT_S:
                    end_hold(active_hold, now)
                    active_hold = None
                continue

            key = read_key(stdin_fd)

            if key in QUIT_KEYS:
                break
            elif key in ESCAPE_SEQUENCE_KEYS:
                name, key_type = ESCAPE_SEQUENCE_KEYS[key]
            elif key in KEY_BINDINGS:
                name, key_type = KEY_BINDINGS[key]
            else:
                continue  # ignore unmapped keys, don't disturb an active hold

            if name in ("skip", "revert") and not has_media_source():
                if active_hold is not None:
                    end_hold(active_hold, now)
                    active_hold = None
                print(f"[SKIP] {name} ignored -- no media source active")
                continue

            if active_hold is not None and active_hold["name"] == name:
                # Continuing the same hold -- throttle how often we re-send.
                active_hold["raw_count"] += 1
                active_hold["last_seen_at"] = now
                if now - active_hold["last_sent_at"] >= REPEAT_INTERVAL_S:
                    try:
                        send_tap(client, key_type)
                    except Exception as e:
                        print(f"[FAIL] {name} repeat -- {e}")
                    else:
                        active_hold["sent_count"] += 1
                        active_hold["last_sent_at"] = now
                        print(f"[KEY] {name} sent (#{active_hold['sent_count']})")
                continue

            # A different key (or no hold yet) -- end any previous hold, start this one.
            if active_hold is not None:
                end_hold(active_hold, now)
                active_hold = None

            try:
                send_tap(client, key_type)
            except Exception as e:
                print(f"[FAIL] {name} -- {e}")
                continue

            active_hold = {
                "name": name,
                "sent_count": 1,
                "raw_count": 1,
                "last_sent_at": now,
                "last_seen_at": now,
                "started_at": now,
            }
            print(f"[KEY] {name} sent (#1)")

        if active_hold is not None:
            end_hold(active_hold, time.monotonic())

        if connection_lost.is_set():
            print("[INFO] connection is closed, exiting")
    except KeyboardInterrupt:
        print()
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
        shutting_down.set()
        print("[INFO] disconnecting...")
        client.disconnect()
        print("[OK] disconnected, bye")


if __name__ == "__main__":
    main()
