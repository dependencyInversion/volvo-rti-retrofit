from dataclasses import dataclass


@dataclass
class Message:
    id: int
    flags: int
    payload: bytes
