import logging
import struct
import threading

import websocket

import hudiy_client.generated.Api_pb2 as hudiy_api
from hudiy_client.message import Message

logger = logging.getLogger(__name__)

# Wire header: little-endian (payload_size, message_id, flags), all uint32.
_HEADER_FORMAT = "<III"
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)  # 12 bytes, derived so it can't drift from the format above


def _message_name(message_id):
    try:
        return hudiy_api.MessageType.Name(message_id)
    except ValueError:
        return f"UNKNOWN({message_id})"

# Messages the client receives from Hudiy, keyed by MessageType id.
_RESPONSE_TYPES = {
    hudiy_api.MESSAGE_HELLO_RESPONSE: hudiy_api.HelloResponse,
    hudiy_api.MESSAGE_PROJECTION_STATUS: hudiy_api.ProjectionStatus,
    hudiy_api.MESSAGE_MEDIA_STATUS: hudiy_api.MediaStatus,
    hudiy_api.MESSAGE_MEDIA_METADATA: hudiy_api.MediaMetadata,
    hudiy_api.MESSAGE_NAVIGATION_STATUS: hudiy_api.NavigationStatus,
    hudiy_api.MESSAGE_NAVIGATION_MANEUVER_DETAILS: hudiy_api.NavigationManeuverDetails,
    hudiy_api.MESSAGE_NAVIGATION_MANEUVER_DISTANCE: hudiy_api.NavigationManeuverDistance,
    hudiy_api.MESSAGE_REGISTER_STATUS_ICON_RESPONSE: hudiy_api.RegisterStatusIconResponse,
    hudiy_api.MESSAGE_REGISTER_NOTIFICATION_CHANNEL_RESPONSE: hudiy_api.RegisterNotificationChannelResponse,
    hudiy_api.MESSAGE_REGISTER_TOAST_CHANNEL_RESPONSE: hudiy_api.RegisterToastChannelResponse,
    hudiy_api.MESSAGE_OBD_CONNECTION_STATUS: hudiy_api.ObdConnectionStatus,
    hudiy_api.MESSAGE_QUERY_OBD_DEVICE_RESPONSE: hudiy_api.QueryObdDeviceResponse,
    hudiy_api.MESSAGE_REGISTER_AUDIO_FOCUS_RECEIVER_RESPONSE: hudiy_api.RegisterAudioFocusReceiverResponse,
    hudiy_api.MESSAGE_AUDIO_FOCUS_CHANGE_RESPONSE: hudiy_api.AudioFocusChangeResponse,
    hudiy_api.MESSAGE_AUDIO_FOCUS_ACTION: hudiy_api.AudioFocusAction,
    hudiy_api.MESSAGE_AUDIO_FOCUS_MEDIA_KEY: hudiy_api.AudioFocusMediaKey,
    hudiy_api.MESSAGE_PHONE_CONNECTION_STATUS: hudiy_api.PhoneConnectionStatus,
    hudiy_api.MESSAGE_PHONE_VOICE_CALL_STATUS: hudiy_api.PhoneVoiceCallStatus,
    hudiy_api.MESSAGE_PHONE_LEVELS_STATUS: hudiy_api.PhoneLevelsStatus,
    hudiy_api.MESSAGE_REGISTER_ACTION_RESPONSE: hudiy_api.RegisterActionResponse,
    hudiy_api.MESSAGE_DISPATCH_ACTION: hudiy_api.DispatchAction,
    hudiy_api.MESSAGE_COVERART_REQUEST: hudiy_api.CoverartRequest,
    hudiy_api.MESSAGE_CURRENT_MENU_ACTION: hudiy_api.CurrentMenuAction,
}


class ClientEventHandler:
    """Override on_message to react to messages pushed by Hudiy."""

    def on_message(self, client, name, message):
        pass


class Client:
    """WebSocket client for the Hudiy Api.proto protocol."""

    def __init__(self, name):
        self._name = name
        self._websocket = None
        self._connected = False
        self._closing = False
        self._event_handler = None
        self._send_lock = threading.Lock()
        self._receive_lock = threading.Lock()
        self._receive_buffer = b""

    def set_event_handler(self, event_handler):
        self._event_handler = event_handler

    def connect(self, host, port=44406):
        if self._connected:
            self.disconnect()

        url = f"ws://{host}:{port}/"
        logger.info("connecting to %s", url)
        try:
            self._websocket = websocket.create_connection(url, timeout=10)
        except Exception as e:
            logger.error("failed to connect to %s: %s", url, e)
            raise

        self._connected = True
        self._closing = False
        logger.info("connection established, sending HelloRequest")
        self._send_hello()

    def close(self):
        if not self._connected:
            return

        logger.info("closing connection")
        self._websocket.close()
        self._connected = False

    def disconnect(self):
        if not self._connected:
            return

        self._closing = True
        try:
            self.send(hudiy_api.MESSAGE_BYEBYE, 0, bytes())
        except Exception as e:
            logger.warning("failed to send BYEBYE: %s", e)
        finally:
            self.close()

    def send(self, message_id, flags, payload):
        name = _message_name(message_id)
        with self._send_lock:
            header = struct.pack(_HEADER_FORMAT, len(payload), message_id, flags)
            try:
                self._websocket.send_binary(header + payload)
            except Exception as e:
                logger.error("-> %s failed: %s", name, e)
                raise
        logger.debug("-> %s (id=%s, flags=%s, %d bytes)", name, message_id, flags, len(payload))

    def receive(self):
        with self._receive_lock:
            header_data = self._receive_exact(_HEADER_SIZE)
            payload_size, message_id, flags = struct.unpack(_HEADER_FORMAT, header_data)
            payload = self._receive_exact(payload_size)
            return Message(message_id, flags, payload)

    def _receive_exact(self, size):
        while len(self._receive_buffer) < size:
            try:
                chunk = self._websocket.recv()
            except websocket.WebSocketTimeoutException:
                # No data arrived within the socket timeout -- that's not the
                # same as the connection being gone (e.g. Hudiy can be briefly
                # slow under load), so just keep waiting instead of failing.
                logger.debug("recv timed out, no data yet -- still waiting")
                continue
            except Exception as e:
                if self._closing:
                    logger.debug("connection closed locally: %s", e)
                else:
                    logger.error("connection lost while receiving: %s", e)
                raise

            if not chunk:
                if self._closing:
                    logger.debug("connection closed locally")
                else:
                    logger.error("connection closed by remote host")
                raise ConnectionError("connection closed")

            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            self._receive_buffer += chunk

        data, self._receive_buffer = self._receive_buffer[:size], self._receive_buffer[size:]
        return data

    def subscribe(self, subscriptions):
        """Subscribe to status updates (e.g. SetStatusSubscriptions.MEDIA). Replaces
        any previous subscription set -- pass the full list you want active."""
        message = hudiy_api.SetStatusSubscriptions()
        message.subscriptions.extend(subscriptions)
        self.send(hudiy_api.MESSAGE_SET_STATUS_SUBSCRIPTIONS, 0, message.SerializeToString())

    def _send_hello(self):
        hello_request = hudiy_api.HelloRequest()
        hello_request.name = self._name
        hello_request.api_version.major = hudiy_api.API_MAJOR_VERSION
        hello_request.api_version.minor = hudiy_api.API_MINOR_VERSION

        self.send(hudiy_api.MESSAGE_HELLO_REQUEST, 0, hello_request.SerializeToString())

    def wait_for_message(self):
        """Receive and dispatch one message. Returns False once the server says goodbye."""
        can_continue = True
        message = self.receive()
        name = _message_name(message.id)
        logger.debug("<- %s (id=%s, flags=%s, %d bytes)", name, message.id, message.flags, len(message.payload))

        if message.id == hudiy_api.MESSAGE_PING:
            logger.debug("received ping, replying with pong")
            self.send(hudiy_api.MESSAGE_PONG, 0, bytes())
        elif message.id == hudiy_api.MESSAGE_BYEBYE:
            logger.info("server closed the session (BYEBYE)")
            can_continue = False

        proto_cls = _RESPONSE_TYPES.get(message.id)
        if proto_cls is not None:
            parsed = proto_cls()
            parsed.ParseFromString(message.payload)
            if self._event_handler is not None:
                self._event_handler.on_message(self, proto_cls.__name__, parsed)
        elif message.id not in (hudiy_api.MESSAGE_PING, hudiy_api.MESSAGE_PONG, hudiy_api.MESSAGE_BYEBYE):
            logger.debug("no parser registered for %s", name)

        return can_continue
