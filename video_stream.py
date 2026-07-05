"""Vídeo didático: frames PPM fragmentados em datagramas pequenos."""
import base64
import math
import time

CHUNK_SIZE = 850
FRAME_TTL = 1.0


def generate_ppm(frame_id, width=96, height=72):
    pixels = bytearray()
    rov_x = int((math.sin(frame_id * 0.12) * 0.35 + 0.5) * width)
    for y in range(height):
        for x in range(width):
            depth = y / max(1, height - 1)
            r, g, b = int(3 + 8 * depth), int(42 - 25 * depth), int(68 - 35 * depth)
            if abs(x - rov_x) < 13 and abs(y - height // 2) < 7:
                r, g, b = 245, 169, 20
            elif ((x + frame_id) % 31 == 0) and y < height // 2:
                r, g, b = 150, 220, 235
            pixels.extend((r, g, b))
    return f"P6\n{width} {height}\n255\n".encode("ascii") + bytes(pixels)


def fragment_frame(rov_id, frame_id, ppm, sent_at=None):
    encoded = base64.b64encode(ppm).decode("ascii")
    chunks = [encoded[i:i + CHUNK_SIZE] for i in range(0, len(encoded), CHUNK_SIZE)]
    timestamp = sent_at if sent_at is not None else time.time()
    for index, payload in enumerate(chunks):
        yield {"type": "video_chunk", "rov": rov_id, "frame_id": frame_id,
               "chunk_id": index, "chunk_count": len(chunks), "sent_at": timestamp,
               "payload": payload}


class FrameAssembler:
    def __init__(self):
        self.frames = {}
        self.dropped = 0

    def add(self, msg):
        now = time.time()
        self._expire(now)
        key = (str(msg.get("rov")), int(msg.get("frame_id", -1)))
        total, index = int(msg.get("chunk_count", 0)), int(msg.get("chunk_id", -1))
        if total <= 0 or total > 256 or not 0 <= index < total:
            return None
        entry = self.frames.setdefault(
            key, {"chunks": {}, "total": total, "created": now,
                  "sent_at": float(msg.get("sent_at", now))})
        entry["chunks"][index] = str(msg.get("payload", ""))
        if len(entry["chunks"]) != entry["total"]:
            return None
        encoded = "".join(entry["chunks"][i] for i in range(entry["total"]))
        del self.frames[key]
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            self.dropped += 1
            return None
        return {"rov": key[0], "frame_id": key[1], "ppm": data,
                "latency_ms": round((now - entry["sent_at"]) * 1000, 1),
                "dropped": self.dropped}

    def _expire(self, now):
        expired = [key for key, value in self.frames.items()
                   if now - value["created"] > FRAME_TTL]
        for key in expired:
            del self.frames[key]
            self.dropped += 1
