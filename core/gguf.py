"""Minimal GGUF header reader: pulls metadata key/values, never touches tensor data."""
from __future__ import annotations

import struct
from pathlib import Path

GGUF_MAGIC = b"GGUF"

UINT8, INT8, UINT16, INT16, UINT32, INT32, FLOAT32, BOOL, STRING, ARRAY, UINT64, INT64, FLOAT64 = range(13)

_FIXED_SIZE = {UINT8: 1, INT8: 1, UINT16: 2, INT16: 2, UINT32: 4, INT32: 4,
               FLOAT32: 4, BOOL: 1, UINT64: 8, INT64: 8, FLOAT64: 8}
_FIXED_FMT = {UINT8: "<B", INT8: "<b", UINT16: "<H", INT16: "<h", UINT32: "<I", INT32: "<i",
              FLOAT32: "<f", BOOL: "<?", UINT64: "<Q", INT64: "<q", FLOAT64: "<d"}

_EXACT_KEYS = {
    "general.architecture", "general.name", "general.basename",
    "general.size_label", "general.file_type", "general.quantization_version",
}
_SUFFIX_KEYS = (
    ".context_length", ".block_count", ".embedding_length",
    ".expert_count", ".attention.head_count", ".vocab_size",
)
# Once these are known there is nothing else worth waiting for, so parsing stops.
_ENOUGH = ("general.architecture", "context_length", "block_count")

# File-type enum -> human label. Only the values that actually show up on disk.
FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M",
    16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S",
    22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M",
    28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16", 36: "TQ1_0", 37: "TQ2_0",
}


class _Reader:
    def __init__(self, fh):
        self.fh = fh

    def raw(self, n: int) -> bytes:
        b = self.fh.read(n)
        if len(b) != n:
            raise EOFError("truncated GGUF header")
        return b

    def scalar(self, vtype: int):
        return struct.unpack(_FIXED_FMT[vtype], self.raw(_FIXED_SIZE[vtype]))[0]

    def string(self) -> str:
        (length,) = struct.unpack("<Q", self.raw(8))
        if length > 64 * 1024 * 1024:
            raise ValueError("implausible GGUF string length")
        return self.raw(length).decode("utf-8", errors="replace")

    def skip_value(self, vtype: int) -> None:
        if vtype in _FIXED_SIZE:
            self.fh.seek(_FIXED_SIZE[vtype], 1)
        elif vtype == STRING:
            (length,) = struct.unpack("<Q", self.raw(8))
            self.fh.seek(length, 1)
        elif vtype == ARRAY:
            (elem_type,) = struct.unpack("<I", self.raw(4))
            (count,) = struct.unpack("<Q", self.raw(8))
            if elem_type in _FIXED_SIZE:
                self.fh.seek(_FIXED_SIZE[elem_type] * count, 1)
            elif elem_type == STRING:
                # Token vocabularies land here; seek past each entry rather than materialising it.
                for _ in range(count):
                    (length,) = struct.unpack("<Q", self.raw(8))
                    self.fh.seek(length, 1)
            else:
                raise ValueError(f"unsupported GGUF array element type {elem_type}")
        else:
            raise ValueError(f"unsupported GGUF value type {vtype}")

    def read_value(self, vtype: int):
        if vtype in _FIXED_SIZE:
            return self.scalar(vtype)
        if vtype == STRING:
            return self.string()
        self.skip_value(vtype)
        return None


def _is_wanted(key: str) -> bool:
    return key in _EXACT_KEYS or key.endswith(_SUFFIX_KEYS)


CAPABILITY_KEYS = {"tokenizer.chat_template", "clip.has_vision_encoder", "clip.has_audio_encoder"}
REASONING_MARKERS = ("<think>", "enable_thinking", "reasoning_content", "reasoning_effort")


def read_capabilities(path: str | Path) -> dict:
    """Chat template and projector encoder flags.

    Needs a full header walk because the template sits after the vocab arrays;
    ~100-200 ms on a large model, so callers should cache it.
    """
    out: dict = {}
    try:
        with open(path, "rb", buffering=1 << 20) as fh:
            r = _Reader(fh)
            if r.raw(4) != GGUF_MAGIC:
                return {}
            r.raw(4)
            r.raw(8)
            kv_count, = struct.unpack("<Q", r.raw(8))
            for _ in range(kv_count):
                key = r.string()
                vtype, = struct.unpack("<I", r.raw(4))
                if key in CAPABILITY_KEYS:
                    out[key] = r.read_value(vtype)
                else:
                    r.skip_value(vtype)
    except (OSError, EOFError, ValueError, struct.error):
        pass
    template = out.get("tokenizer.chat_template") or ""
    return {
        "reasoning": any(marker in template for marker in REASONING_MARKERS),
        "tools": "tool" in template,
        "vision": bool(out.get("clip.has_vision_encoder")),
        "audio": bool(out.get("clip.has_audio_encoder")),
    }


def read_metadata(path: str | Path, max_kv: int = 4000) -> dict:
    """Return GGUF header metadata as a plain dict. Returns {} for anything unreadable."""
    out: dict = {}
    try:
        with open(path, "rb") as fh:
            r = _Reader(fh)
            if r.raw(4) != GGUF_MAGIC:
                return {}
            version, = struct.unpack("<I", r.raw(4))
            if version not in (2, 3):
                return {}
            r.raw(8)  # tensor count
            kv_count, = struct.unpack("<Q", r.raw(8))

            for _ in range(min(kv_count, max_kv)):
                key = r.string()
                vtype, = struct.unpack("<I", r.raw(4))
                if _is_wanted(key):
                    value = r.read_value(vtype)
                    if value is not None:
                        out[key] = value
                else:
                    r.skip_value(vtype)
                if sum(1 for probe in _ENOUGH if any(k.endswith(probe) or k == probe for k in out)) == len(_ENOUGH):
                    break
    except (OSError, EOFError, ValueError, struct.error):
        return out
    return out


def summarize(meta: dict) -> dict:
    """Condense raw GGUF metadata into the handful of fields the UI shows."""
    if not meta:
        return {}
    arch = meta.get("general.architecture", "")
    picked = {
        "arch": arch,
        "name": meta.get("general.name", ""),
        "size_label": meta.get("general.size_label", ""),
    }
    for key, value in meta.items():
        if key.endswith(".context_length"):
            picked["n_ctx_train"] = int(value)
        elif key.endswith(".block_count"):
            picked["n_layer"] = int(value)
        elif key.endswith(".expert_count") and value:
            picked["n_expert"] = int(value)
    ftype = meta.get("general.file_type")
    if isinstance(ftype, int):
        picked["file_type"] = FILE_TYPES.get(ftype, f"ftype{ftype}")
    return {k: v for k, v in picked.items() if v not in ("", None)}
