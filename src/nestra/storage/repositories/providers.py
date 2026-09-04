"""Runtime loading for encrypted providers created in the web UI."""

from __future__ import annotations

import json

from ...core.config import ProviderConfig
from ...core.crypto import Crypto
from ..db import Database


def web_providers(db: Database, crypto: Crypto) -> list[ProviderConfig]:
    providers = []
    for row in db.query(
        "SELECT name,type,base_url,models_json,max_input_chars,api_key_enc "
        "FROM llm_providers ORDER BY id"
    ):
        providers.append(
            ProviderConfig(
                name=row["name"],
                type=row["type"],
                base_url=row["base_url"],
                models=json.loads(row["models_json"]),
                max_input_chars=row["max_input_chars"],
                api_key_value=crypto.decrypt(bytes(row["api_key_enc"])),
            )
        )
    return providers
