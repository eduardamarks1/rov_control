"""Carregamento e validação da configuração das demonstrações."""
import json
from pathlib import Path

DEFAULT_CONFIG = {
    "relays": [
        {"role": "primary", "host": "127.0.0.1", "port": 5000},
        {"role": "backup", "host": "127.0.0.1", "port": 5001},
    ],
    "rovs": [{"id": "rov1", "video": True}],
    "pilots": [{"id": "pilotoA", "target": "rov1"}],
    "network": {"loss": 0.0},
}


def load_config(path=None):
    if path is None:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    data = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    validate_config(data)
    return data


def validate_config(data):
    relays = data.get("relays", [])
    rovs = data.get("rovs", [])
    pilots = data.get("pilots", [])
    if len(relays) < 2:
        raise ValueError("A demonstração requer ao menos dois relays.")
    ports = [int(r["port"]) for r in relays]
    if len(ports) != len(set(ports)):
        raise ValueError("As portas dos relays devem ser distintas.")
    rov_ids = [r["id"] for r in rovs]
    if len(rov_ids) != len(set(rov_ids)):
        raise ValueError("IDs de ROV devem ser únicos.")
    unknown = [p["target"] for p in pilots if p["target"] not in rov_ids]
    if unknown:
        raise ValueError(f"Pilotos apontam para ROVs inexistentes: {unknown}")
    loss = float(data.get("network", {}).get("loss", 0.0))
    if not 0 <= loss <= 1:
        raise ValueError("network.loss deve ficar entre 0 e 1.")


def relay_addresses(config):
    return [(r.get("host", "127.0.0.1"), int(r["port"])) for r in config["relays"]]
