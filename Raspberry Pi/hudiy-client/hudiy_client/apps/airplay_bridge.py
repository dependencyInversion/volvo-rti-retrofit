"""AirPlay bridge: integrates uxplay (AirPlay mirroring) into Hudiy.

Registers an action (default "airplay_toggle") that a Hudiy shortcut or menu item
can be bound to. Pressing it while idle makes sure uxplay is running and shows a
toast explaining what to do on the phone; pressing it while mirroring stops the
session.

While the phone is mirroring (an established TCP connection on uxplay's
mirroring data port), a status icon is shown and Hudiy's audio focus is taken so
its own media pauses. When mirroring stops, audio focus is released again.

The phone has to be on a network uxplay is reachable from (e.g. Hudiy's hotspot).

Usage:
    airplay-bridge                       # as deployed on the Pi (systemd user unit)
    airplay-bridge --host pi.local -v    # from a dev machine, verbose
"""

import argparse
import logging
import signal
import subprocess
import sys
import threading
import time

import hudiy_client.generated.Api_pb2 as hudiy_api
from hudiy_client import Client, ClientEventHandler

logger = logging.getLogger("airplay_bridge")

ICON_FONT_FAMILY = "Material Symbols Rounded"
ICON_NAME = "airplay"

IDLE = "idle"
MIRRORING = "mirroring"

# How often the mirroring port is checked, and how many consecutive empty
# checks count as "mirroring stopped" -- a short gap (e.g. phone rotating,
# Wi-Fi hiccup) shouldn't bounce CarPlay back in.
MIRROR_POLL_INTERVAL_S = 1.0
MIRROR_STOP_POLLS = 3

RECONNECT_MIN_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 15.0


class Bridge(ClientEventHandler):
    def __init__(self, client, action):
        self._client = client
        self._action = action

        self._lock = threading.RLock()
        self._state = IDLE

        # Ids handed out by Hudiy; reset on every (re)connect.
        self._connected = False
        self._toast_channel_id = None
        self._status_icon_id = None
        self._audio_focus_id = None
        self._audio_focus_held = False

    # --- Hudiy connection ---------------------------------------------------

    def on_connection_lost(self):
        with self._lock:
            self._connected = False
            self._toast_channel_id = None
            self._status_icon_id = None
            self._audio_focus_id = None
            self._audio_focus_held = False

    def on_message(self, client, name, message):
        if name == "HelloResponse":
            if message.result != hudiy_api.HelloResponse.HELLO_RESPONSE_RESULT_OK:
                result_name = hudiy_api.HelloResponse.HelloResponseResult.Name(message.result)
                logger.error("handshake rejected: %s", result_name)
                return
            logger.info(
                "handshake ok - Hudiy %d.%d, api %d.%d",
                message.app_version.major,
                message.app_version.minor,
                message.api_version.major,
                message.api_version.minor,
            )
            with self._lock:
                self._connected = True
            self._register()
        elif name == "RegisterActionResponse":
            if message.result:
                logger.info("action '%s' registered", message.action)
            else:
                logger.error("failed to register action '%s' (name already taken?)", message.action)
        elif name == "RegisterToastChannelResponse":
            if message.result == hudiy_api.RegisterToastChannelResponse.REGISTER_TOAST_CHANNEL_RESULT_OK:
                with self._lock:
                    self._toast_channel_id = message.id
            else:
                logger.error("failed to register toast channel")
        elif name == "RegisterStatusIconResponse":
            if message.result == hudiy_api.RegisterStatusIconResponse.REGISTER_STATUS_ICON_RESULT_OK:
                with self._lock:
                    self._status_icon_id = message.id
                    self._update_status_icon()
            else:
                logger.error("failed to register status icon")
        elif name == "RegisterAudioFocusReceiverResponse":
            ok = hudiy_api.RegisterAudioFocusReceiverResponse.REGISTER_AUDIO_FOCUS_RECEIVER_RESULT_OK
            if message.result == ok:
                with self._lock:
                    self._audio_focus_id = message.id
                    # Reconnected mid-session -- take the focus again.
                    if self._state == MIRRORING:
                        self._request_audio_focus(True)
            else:
                logger.error("failed to register audio focus receiver")
        elif name == "AudioFocusChangeResponse":
            logger.info("audio focus change %s", "granted" if message.result else "rejected")
        elif name == "DispatchAction":
            if message.action == self._action:
                self.on_button()

    def _register(self):
        self._client.register_action(self._action)
        self._client.register_toast_channel("AirPlay", "AirPlay mirroring status")
        self._client.register_status_icon("AirPlay mirroring active", ICON_FONT_FAMILY, ICON_NAME)
        self._client.register_audio_focus_receiver(
            "AirPlay",
            hudiy_api.RegisterAudioFocusReceiverRequest.AUDIO_STREAM_CATEGORY_ENTERTAINMENT,
            50,  # below navigation prompts (100)
        )

    def _send(self, what, fn, *args):
        """Sends are best effort: Hudiy may be restarting, and the state machine
        must keep working (e.g. release audio focus) regardless."""
        if not self._connected:
            logger.debug("not connected to Hudiy, skipping %s", what)
            return
        try:
            fn(*args)
        except Exception as e:
            logger.warning("failed to send %s: %s", what, e)

    def _toast(self, text):
        logger.info("toast: %s", text)
        if self._toast_channel_id is not None:
            self._send("toast", self._client.show_toast, self._toast_channel_id, text, ICON_FONT_FAMILY, ICON_NAME)

    def _update_status_icon(self):
        if self._status_icon_id is not None:
            visible = self._state != IDLE
            self._send("status icon", self._client.set_status_icon_visible, self._status_icon_id, visible)

    def _request_audio_focus(self, gain):
        if self._audio_focus_id is None:
            self._audio_focus_held = False
            return
        focus_type = hudiy_api.AudioFocusChangeRequest
        self._send(
            "audio focus",
            self._client.request_audio_focus,
            self._audio_focus_id,
            focus_type.AUDIO_FOCUS_TYPE_GAIN if gain else focus_type.AUDIO_FOCUS_TYPE_RELEASE,
        )
        self._audio_focus_held = gain

    # --- State machine --------------------------------------------------------

    def on_button(self):
        with self._lock:
            logger.info("button pressed in state %s", self._state)
            if self._state == MIRRORING:
                # Kick the phone -- restarting uxplay drops the session.
                subprocess.run(["systemctl", "--user", "restart", "uxplay"], check=False)
                self._finish("AirPlay stopped")
            else:
                # It may have been switched off from the desktop shortcut.
                subprocess.run(["systemctl", "--user", "start", "uxplay"], check=False)
                self._toast("AirPlay ready - First join Hudiy's wifi then on the iPhone: Control Center > Screen Mirroring")

    def on_mirroring(self, active):
        with self._lock:
            if active and self._state != MIRRORING:
                logger.info("mirroring started")
                self._state = MIRRORING
                self._update_status_icon()
                self._request_audio_focus(True)
            elif not active and self._state == MIRRORING:
                logger.info("mirroring stopped")
                self._finish("AirPlay ended")

    def _finish(self, reason):
        if self._audio_focus_held:
            self._request_audio_focus(False)
        self._state = IDLE
        self._update_status_icon()
        self._toast(reason)

    def shutdown(self):
        with self._lock:
            if self._state != IDLE:
                self._finish("AirPlay stopped")


def mirroring_active(port):
    """True while a client holds an established TCP connection on uxplay's
    mirroring data port - that connection only exists while the screen is
    actually being mirrored, unlike the RTSP one, which iOS also opens when
    just browsing the Screen Mirroring list."""
    try:
        result = subprocess.run(
            ["ss", "-Htn", "state", "established", f"( sport = :{port} )"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("ss failed: %s", e)
        return False
    return bool(result.stdout.strip())


def watch_mirroring(bridge, port, stop_event):
    active = False
    empty_polls = 0
    while not stop_event.is_set():
        if mirroring_active(port):
            empty_polls = 0
            if not active:
                active = True
                bridge.on_mirroring(True)
        elif active:
            empty_polls += 1
            if empty_polls >= MIRROR_STOP_POLLS:
                active = False
                bridge.on_mirroring(False)
        stop_event.wait(MIRROR_POLL_INTERVAL_S)


def run_hudiy_connection(client, bridge, host, port, stop_event):
    """Keeps a connection to Hudiy up: Hudiy is started from labwc's autostart
    and may be quit and restarted at any time, and our registrations die with it."""
    delay = RECONNECT_MIN_DELAY_S
    while not stop_event.is_set():
        try:
            client.connect(host, port)
        except Exception:
            stop_event.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY_S)
            continue

        delay = RECONNECT_MIN_DELAY_S
        try:
            while client.wait_for_message():
                pass
            logger.info("Hudiy closed the connection")
        except Exception as e:
            if not stop_event.is_set():
                logger.warning("connection to Hudiy lost: %s", e)
        finally:
            bridge.on_connection_lost()
            client.close()
        stop_event.wait(delay)


def setup_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="Hudiy host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=44406, help="Hudiy WebSocket port (default: 44406)")
    parser.add_argument("--action", default="airplay_toggle", help="Hudiy action name to register")
    parser.add_argument(
        "--mirror-port",
        type=int,
        default=7100,
        help="uxplay's TCP mirroring data port (7100 with uxplay's -p flag; default: 7100)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every raw message sent/received")
    args = parser.parse_args()

    setup_logging(args.verbose)

    stop_event = threading.Event()
    client = Client("airplay bridge")
    bridge = Bridge(client, args.action)
    client.set_event_handler(bridge)

    def handle_signal(signum, _frame):
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()
        client.disconnect()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    threading.Thread(
        target=watch_mirroring, args=(bridge, args.mirror_port, stop_event), daemon=True
    ).start()
    threading.Thread(
        target=run_hudiy_connection, args=(client, bridge, args.host, args.port, stop_event), daemon=True
    ).start()

    while not stop_event.is_set():
        time.sleep(0.5)

    bridge.shutdown()


if __name__ == "__main__":
    main()
