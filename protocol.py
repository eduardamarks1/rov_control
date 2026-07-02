"""
protocol.py
-----------
Define o "idioma" que Piloto, Relay e ROV usam para se falar, MAIS o esquema
de autenticação do piloto.

As mensagens continuam sendo dicionários (JSON) com um campo "type". O que
mudou em relação à versão anterior é COMO elas viajam: agora vão pela camada
quiclite (UDP), escolhendo o canal certo para cada uma:

  CANAL CONFIÁVEL (não pode perder, tem que chegar em ordem):
    register, registered, auth_challenge, auth_response, auth_ok, auth_fail,
    control_granted, control_denied, command, release_control, rov_offline,
    error, replicate

  CANAL NÃO-CONFIÁVEL (periódico, "último vence", pode perder):
    heartbeat, telemetry, relay_heartbeat, relay_ping


AUTENTICAÇÃO DO PILOTO (desafio-resposta com HMAC):
-----------------------------------------------------
Não queremos mandar a senha em texto puro pela rede. Então usamos o esquema
clássico de desafio-resposta:

  1. Piloto -> Relay:  register (role=pilot, id=pilotoA)
  2. Relay -> Piloto:  auth_challenge (nonce aleatório de uso único)
  3. Piloto -> Relay:  auth_response (HMAC-SHA256(senha_do_piloto, nonce))
  4. Relay verifica recalculando o mesmo HMAC com a senha que ele conhece:
       - bate  -> auth_ok (+ token de sessão) e segue para conceder controle
       - não   -> auth_fail

Vantagens (temas de segurança em Sistemas Distribuídos):
  * A senha NUNCA trafega pela rede -- só o HMAC dela com o nonce.
  * O nonce é de uso único e aleatório, então capturar um auth_response antigo
    e reenviá-lo (ataque de REPLAY) não funciona: o próximo desafio terá outro
    nonce.
  * Depois do login, o relay emite um TOKEN de sessão, identificando a sessão
    autenticada.
"""

import hashlib
import hmac
import secrets

# ---------------------------------------------------------------------------
# "Banco de credenciais" conhecido pelos relays. Em um sistema real isso
# estaria em um servidor de identidade / banco de dados protegido; aqui é um
# dicionário fixo só para a demonstração. Cada piloto tem uma senha.
# (Os dois relays carregam a MESMA tabela, por isso o backup também consegue
# autenticar os pilotos quando assume no lugar do primário.)
# ---------------------------------------------------------------------------
PILOT_CREDENTIALS = {
    "pilotoA": "mergulho2026",
    "pilotoB": "trocaraki",
}


def gen_nonce() -> str:
    """Gera um desafio aleatório de uso único (32 hex = 128 bits)."""
    return secrets.token_hex(16)


def compute_response(password: str, nonce: str) -> str:
    """
    Resposta do piloto ao desafio: HMAC-SHA256 usando a senha como chave e o
    nonce como mensagem. Determinístico para (senha, nonce) -> o relay
    consegue recalcular e comparar.
    """
    return hmac.new(password.encode("utf-8"),
                    nonce.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_response(pilot_id: str, nonce: str, response: str) -> bool:
    """
    O relay verifica a resposta: procura a senha do piloto, recalcula o HMAC
    esperado e compara em tempo constante (hmac.compare_digest evita ataques
    de temporização).
    """
    password = PILOT_CREDENTIALS.get(pilot_id)
    if password is None:
        return False
    expected = compute_response(password, nonce)
    return hmac.compare_digest(expected, response)


def new_session_token() -> str:
    """Token opaco entregue ao piloto após autenticar com sucesso."""
    return secrets.token_hex(16)
