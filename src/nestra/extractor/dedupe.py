"""稳定的 URL 哈希与正文 simhash。"""

from __future__ import annotations

import hashlib
import re


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _tokens(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", normalized)
    # 单字对中文内容过于宽泛；相邻三元组保留语序，短文本仍可工作。
    return words if len(words) < 3 else ["\0".join(words[i : i + 3]) for i in range(len(words) - 2)]


def simhash(text: str) -> str:
    weights = [0] * 64
    for token in _tokens(text):
        value = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    fingerprint = sum(1 << bit for bit, weight in enumerate(weights) if weight > 0)
    return f"{fingerprint:016x}"


def hamming_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()
