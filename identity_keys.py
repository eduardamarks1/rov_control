"""Provisionamento e assinaturas RSA-PSS das identidades da demonstração."""

import base64
import os
import threading
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


KEY_DIR = Path(os.getenv("ROV_IDENTITY_KEY_DIR", Path(__file__).with_name("identity_keys")))
AUTHORIZED_IDENTITIES = {
    "pilot": {"pilotoA", "pilotoB"},
    "rov": {"rov1", "rov2", "rov3"},
    "relay": {"primary", "backup"},
}
_provision_lock = threading.Lock()


def _paths(role, identity):
    prefix = f"{role}-{identity}"
    return KEY_DIR / f"{prefix}-private.pem", KEY_DIR / f"{prefix}-public.pem"


def provision_demo_identities():
    """Cria os pares RSA da demo sem corrida entre processos."""
    with _provision_lock:
        KEY_DIR.mkdir(parents=True, exist_ok=True)
        for role, identities in AUTHORIZED_IDENTITIES.items():
            for identity in identities:
                private_path, public_path = _paths(role, identity)
                if not private_path.exists():
                    private_key = rsa.generate_private_key(
                        public_exponent=65537, key_size=2048
                    )
                    private_pem = private_key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                    try:
                        with private_path.open("xb") as key_file:
                            key_file.write(private_pem)
                    except FileExistsError:
                        pass

                private_key = serialization.load_pem_private_key(
                    private_path.read_bytes(), password=None
                )
                public_pem = private_key.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                if not public_path.exists():
                    try:
                        with public_path.open("xb") as key_file:
                            key_file.write(public_pem)
                    except FileExistsError:
                        pass
                if public_path.read_bytes() != public_pem:
                    raise RuntimeError(f"par RSA inconsistente para {role}:{identity}")

def load_private_key(role, identity, private_key=None):
    """Carrega a chave local padrão ou aceita uma chave injetada para testes."""
    if private_key is not None and not isinstance(private_key, (str, os.PathLike)):
        return private_key
    provision_demo_identities()
    path = Path(private_key) if private_key else _paths(role, identity)[0]
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def load_public_key(role, identity):
    if identity not in AUTHORIZED_IDENTITIES.get(role, set()):
        return None
    provision_demo_identities()
    return serialization.load_pem_public_key(_paths(role, identity)[1].read_bytes())


def generate_untrusted_private_key():
    """Gera identidade não cadastrada para cenários negativos de teste."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def sign_transcript(private_key, handshake_transcript):
    signature = private_key.sign(
        handshake_transcript,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def verify_transcript(role, identity, handshake_transcript, encoded_signature):
    public_key = load_public_key(role, identity)
    if public_key is None:
        return False
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
        public_key.verify(
            signature,
            handshake_transcript,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return True
    except (InvalidSignature, TypeError, ValueError):
        return False
