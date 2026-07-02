"""
relay_server.py
----------------
O RELAY é o "ponto de encontro" com endereço conhecido: pilotos e ROVs sempre
SAEM de suas redes para se conectar nele (resolvendo NAT/firewall), e ele
roteia mensagens entre eles.

CONCEITOS DE SISTEMAS DISTRIBUÍDOS NESTE ARQUIVO:

1) NAT/firewall traversal: cliente e ROV iniciam a conexão em direção ao relay.

2) Registro distribuído de sessões: os dicionários `rovs` e `pilots`.

3) Controle de concorrência / exclusão mútua: `_try_grant_control` garante que
   apenas UM piloto controla cada ROV por vez.

4) Detecção de falhas: heartbeats + `_liveness_monitor` derrubam clientes que
   ficaram mudos.

5) Autenticação: pilotos passam por desafio-resposta HMAC antes de controlar.

6) REPLICAÇÃO e TOLERÂNCIA A FALHAS DO PRÓPRIO RELAY (a novidade principal):
   rodamos DOIS relays em esquema PRIMÁRIO-BACKUP.
     * O primário REPLICA cada mudança de estado para o backup.
     * Os dois trocam "relay_ping" para se vigiarem.
     * Se o primário cai, o backup detecta o silêncio e se declara ATIVO; os
       clientes (que monitoram o relay) fazem FAILOVER e se re-registram no
       backup, restaurando o serviço. Assim o relay deixa de ser um ponto
       único de falha.

Este arquivo separa a LÓGICA (classe RelayNode, testável sem tela) da
INTERFACE (classe RelayGUI). Isso permite testar o sistema de forma headless.
"""

import argparse
import threading
import time

import quiclite as q
from protocol import gen_nonce, verify_response, new_session_token

# --- Parâmetros de tempo (segundos) ---------------------------------------
# Importante: PRIMARY_TIMEOUT (backup assume) é bem MENOR que o FAILOVER_TIMEOUT
# dos clientes (em rov_simulator/pilot_client). Assim o backup vira ATIVO e
# reserva os controles ANTES de os clientes migrarem, evitando corridas.
CLIENT_TIMEOUT = 6.0      # cliente mudo por mais que isso => considerado offline
RELAY_HB_INTERVAL = 1.0   # de quanto em quanto o relay pinga clientes e o par
PRIMARY_TIMEOUT = 2.5     # backup: primário mudo por mais que isso => assumir
RESERVE_WINDOW = 12.0     # por quanto tempo o backup reserva o controle ao dono


class RelayNode:
    """Toda a lógica do relay. Sem nenhuma dependência de interface gráfica."""

    def __init__(self, role, bind_addr, peer_addr=None, loss=0.0, on_event=None):
        self.role = role                 # 'primary' ou 'backup'
        self.bind_addr = bind_addr       # (ip, porta) onde este relay escuta
        self.peer_addr = peer_addr       # (ip, porta) do OUTRO relay
        self.loss = loss
        self.on_event = on_event

        self.lock = threading.Lock()
        # Estado "vivo" (sessões realmente conectadas a ESTE relay):
        self.rovs = {}      # id -> {"addr","controlled_by","last_seen"}
        self.pilots = {}    # id -> {"addr","controlling","last_seen","authed","nonce","token","target"}
        self.by_addr = {}   # addr -> ("rov"|"pilot", id)

        # Estado REPLICADO recebido do primário (só o backup usa, p/ exibir):
        self.mirror_rovs = set()
        self.mirror_pilots = set()
        self.mirror_control = {}   # rov_id -> pilot_id

        # Papel ativo: o primário já nasce ativo; o backup só ao assumir.
        self.active = (role == "primary")
        self.last_peer_relay = time.time()  # última vez que ouvimos o outro relay
        self.peer_down_logged = False

        # Reserva de controle no failover: ao assumir, o backup usa o estado
        # replicado para RESERVAR cada ROV ao seu dono anterior por uma janela
        # de tempo, para que a troca de relay não roube o controle de quem já
        # tinha. É o que torna a replicação realmente útil (não só decorativa).
        self.reserved = {}          # rov_id -> pilot_id (dono anterior)
        self.reserved_until = 0.0   # instante em que a reserva expira

        self.endpoint = None
        self.running = False

    # -- infraestrutura -----------------------------------------------------
    def start(self):
        sock = q.make_udp_socket(self.bind_addr)
        self.endpoint = q.Endpoint(sock, self._on_message, loss=self.loss,
                                   name=f"relay-{self.role}")
        self.running = True
        threading.Thread(target=self._liveness_monitor, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        self._log(f"Relay {self.role.upper()} escutando em "
                  f"{self.bind_addr[0]}:{self.bind_addr[1]}"
                  + (f" | par: {self.peer_addr[0]}:{self.peer_addr[1]}" if self.peer_addr else ""))
        self._push_state()

    def stop(self):
        self.running = False
        if self.endpoint:
            self.endpoint.close()

    def _emit(self, event):
        if self.on_event:
            self.on_event(event)

    def _log(self, text):
        print(f"[relay-{self.role}] {text}")
        self._emit({"kind": "log", "text": text})

    def _push_state(self):
        """Emite um retrato do estado atual para a interface desenhar."""
        with self.lock:
            snap = {
                "kind": "state",
                "role": self.role,
                "active": self.active,
                "rovs": [
                    {"id": rid, "controlled_by": r["controlled_by"]}
                    for rid, r in sorted(self.rovs.items())
                ],
                "pilots": [
                    {"id": pid, "authed": p["authed"], "controlling": p["controlling"]}
                    for pid, p in sorted(self.pilots.items())
                ],
                "mirror_rovs": sorted(self.mirror_rovs),
                "mirror_pilots": sorted(self.mirror_pilots),
                "mirror_control": dict(self.mirror_control),
            }
        self._emit(snap)

    # -- replicação primário -> backup -------------------------------------
    def _replicate(self, event, **fields):
        """O primário envia cada mudança de estado ao backup (canal confiável)."""
        if self.role == "primary" and self.peer_addr:
            self.endpoint.send_reliable(self.peer_addr,
                                        {"type": "replicate", "event": event, **fields})

    # -- roteamento de mensagens -------------------------------------------
    def _on_message(self, addr, msg, reliable):
        # Mensagens vindas do OUTRO relay (replicação / ping) chegam do peer_addr.
        if self.peer_addr and addr == self.peer_addr:
            self._on_relay_message(msg)
            return
        self._on_client_message(addr, msg)

    def _on_relay_message(self, msg):
        self.last_peer_relay = time.time()
        self.peer_down_logged = False
        mtype = msg.get("type")
        if mtype == "relay_ping":
            return
        if mtype == "replicate":
            self._apply_replication(msg)

    def _apply_replication(self, msg):
        event = msg.get("event")
        with self.lock:
            if event == "rov_up":
                self.mirror_rovs.add(msg["id"])
            elif event == "rov_down":
                self.mirror_rovs.discard(msg["id"])
                self.mirror_control.pop(msg["id"], None)
            elif event == "pilot_up":
                self.mirror_pilots.add(msg["id"])
            elif event == "pilot_down":
                self.mirror_pilots.discard(msg["id"])
                self.mirror_control = {r: p for r, p in self.mirror_control.items()
                                       if p != msg["id"]}
            elif event == "control":
                rid, pid = msg.get("rov"), msg.get("pilot")
                if pid:
                    self.mirror_control[rid] = pid
                else:
                    self.mirror_control.pop(rid, None)
        self._log(f"[replicação] recebido do primário: {event} {msg.get('id') or msg.get('rov') or ''}")
        self._push_state()

    def _on_client_message(self, addr, msg):
        mtype = msg.get("type")

        # Qualquer mensagem serve de "sinal de vida" do cliente.
        with self.lock:
            ident = self.by_addr.get(addr)
            if ident:
                role, cid = ident
                table = self.rovs if role == "rov" else self.pilots
                if cid in table:
                    table[cid]["last_seen"] = time.time()

        if mtype == "register":
            self._handle_register(addr, msg)
        elif mtype == "auth_response":
            self._handle_auth(addr, msg)
        elif mtype == "command":
            self._handle_command(addr, msg)
        elif mtype == "telemetry":
            self._handle_telemetry(addr, msg)
        elif mtype == "release_control":
            self._handle_release(addr)
        elif mtype == "heartbeat":
            pass  # já atualizou last_seen acima
        else:
            self._log(f"mensagem desconhecida de {addr}: {msg}")

    # -- registro -----------------------------------------------------------
    def _handle_register(self, addr, msg):
        role = msg.get("role")
        cid = msg.get("id")
        if not role or not cid:
            self.endpoint.send_reliable(addr, {"type": "error", "message": "faltam role/id"})
            return

        if role == "rov":
            with self.lock:
                self.rovs[cid] = {"addr": addr, "controlled_by": None, "last_seen": time.time()}
                self.by_addr[addr] = ("rov", cid)
            self.endpoint.send_reliable(addr, {"type": "registered", "ok": True, "role": "rov"})
            self._log(f"ROV '{cid}' registrado {addr}")
            self._replicate("rov_up", id=cid)
            # Um piloto pode já estar esperando para controlar este ROV.
            self._try_pair_rov(cid)

        elif role == "pilot":
            nonce = gen_nonce()
            with self.lock:
                self.pilots[cid] = {"addr": addr, "controlling": None,
                                    "last_seen": time.time(), "authed": False,
                                    "nonce": nonce, "token": None,
                                    "target": msg.get("target")}
                self.by_addr[addr] = ("pilot", cid)
            self.endpoint.send_reliable(addr, {"type": "registered", "ok": True,
                                               "role": "pilot", "need_auth": True})
            self.endpoint.send_reliable(addr, {"type": "auth_challenge", "nonce": nonce})
            self._log(f"Piloto '{cid}' conectou {addr}; desafio de autenticação enviado")
        else:
            self.endpoint.send_reliable(addr, {"type": "error", "message": "role inválido"})
            return
        self._push_state()

    # -- autenticação -------------------------------------------------------
    def _handle_auth(self, addr, msg):
        with self.lock:
            ident = self.by_addr.get(addr)
        if not ident or ident[0] != "pilot":
            return
        pid = ident[1]
        with self.lock:
            p = self.pilots.get(pid)
            nonce = p["nonce"] if p else None
        if p is None:
            return

        if verify_response(pid, nonce, msg.get("response", "")):
            token = new_session_token()
            with self.lock:
                p["authed"] = True
                p["token"] = token
            self.endpoint.send_reliable(addr, {"type": "auth_ok", "token": token})
            self._log(f"Piloto '{pid}' AUTENTICADO (token {token[:8]}…)")
            self._replicate("pilot_up", id=pid)
            if p.get("target"):
                self._try_grant_control(pid)
        else:
            self.endpoint.send_reliable(addr, {"type": "auth_fail",
                                               "reason": "credenciais inválidas"})
            self._log(f"Piloto '{pid}' FALHOU na autenticação — sessão recusada")
            with self.lock:
                self.pilots.pop(pid, None)
                self.by_addr.pop(addr, None)
        self._push_state()

    # -- controle (exclusão mútua) -----------------------------------------
    def _reservation_blocks(self, rid, pid):
        """
        Diz se o ROV 'rid' está RESERVADO a OUTRO piloto (o dono anterior ao
        failover) durante a janela de failover. Bloqueia durante toda a janela
        (mesmo que o dono ainda não tenha reconectado), dando a ele tempo de
        migrar; quando a janela expira, qualquer piloto pode assumir. Deve ser
        chamado com self.lock já adquirido.
        """
        if time.time() > self.reserved_until:
            return False
        owner = self.reserved.get(rid)
        return bool(owner and owner != pid)

    def _try_grant_control(self, pid):
        """Concede o controle do ROV alvo ao piloto, se ninguém mais controla."""
        with self.lock:
            p = self.pilots.get(pid)
            if not p or not p["authed"]:
                return
            rid = p.get("target")
            rov = self.rovs.get(rid)
            if rov is None:
                addr = p["addr"]
                deny = ("ROV não está online", rid)
                rov_ok = False
            elif rov["controlled_by"] not in (None, pid):
                addr = p["addr"]
                deny = (f"já controlado por '{rov['controlled_by']}'", rid)
                rov_ok = False
            elif self._reservation_blocks(rid, pid):
                addr = p["addr"]
                deny = (f"reservado ao dono anterior '{self.reserved.get(rid)}' (failover)", rid)
                rov_ok = False
            else:
                rov["controlled_by"] = pid
                p["controlling"] = rid
                addr = p["addr"]
                rov_ok = True
                self.reserved.pop(rid, None)  # dono reassumiu / concessão normal

        if rov_ok:
            self.endpoint.send_reliable(addr, {"type": "control_granted", "rov": rid})
            self._log(f"Controle de '{rid}' concedido a '{pid}'")
            self._replicate("control", rov=rid, pilot=pid)
        else:
            self.endpoint.send_reliable(addr, {"type": "control_denied",
                                               "rov": deny[1], "reason": deny[0]})
            self._log(f"Controle de '{deny[1]}' NEGADO a '{pid}': {deny[0]}")
        self._push_state()

    def _try_pair_rov(self, rid):
        """Quando um ROV entra, procura um piloto autenticado esperando por ele."""
        with self.lock:
            free = self.rovs.get(rid, {}).get("controlled_by") is None
            waiting = [pid for pid, p in self.pilots.items()
                       if p["authed"] and p.get("target") == rid and p["controlling"] is None]
            # Se há uma reserva de failover para este ROV e o dono está entre os
            # que esperam, ele tem prioridade; senão, o primeiro não-bloqueado.
            owner = self.reserved.get(rid) if time.time() <= self.reserved_until else None
            if owner and owner in waiting:
                chosen = owner
            else:
                chosen = next((pid for pid in waiting if not self._reservation_blocks(rid, pid)), None)
        if free and chosen:
            self._try_grant_control(chosen)

    def _handle_command(self, addr, msg):
        with self.lock:
            ident = self.by_addr.get(addr)
            pid = ident[1] if ident and ident[0] == "pilot" else None
            p = self.pilots.get(pid) if pid else None
            if p is None:
                target_addr = None
            elif not p["authed"]:
                target_addr = "unauth"
            else:
                rid = p["controlling"]
                rov = self.rovs.get(rid) if rid else None
                target_addr = rov["addr"] if rov else None

        if p is None:
            return
        if target_addr == "unauth":
            self.endpoint.send_reliable(addr, {"type": "error", "message": "não autenticado"})
            return
        if target_addr is None:
            self.endpoint.send_reliable(addr, {"type": "error",
                                               "message": "você não controla nenhum ROV"})
            return
        # Encaminha o comando pelo canal CONFIÁVEL (não pode se perder).
        self.endpoint.send_reliable(target_addr, {"type": "command", "from": pid,
                                                  "action": msg.get("action"),
                                                  "value": msg.get("value")})

    def _handle_telemetry(self, addr, msg):
        with self.lock:
            ident = self.by_addr.get(addr)
            rid = ident[1] if ident and ident[0] == "rov" else None
            controller = self.rovs.get(rid, {}).get("controlled_by") if rid else None
            dest = self.pilots.get(controller, {}).get("addr") if controller else None
        if dest:
            fwd = dict(msg)
            fwd["rov"] = rid
            # Telemetria segue pelo canal NÃO-CONFIÁVEL até o piloto.
            self.endpoint.send_unreliable(dest, fwd)

    def _handle_release(self, addr):
        with self.lock:
            ident = self.by_addr.get(addr)
            pid = ident[1] if ident and ident[0] == "pilot" else None
            rid = self.pilots.get(pid, {}).get("controlling") if pid else None
            if rid and rid in self.rovs:
                self.rovs[rid]["controlled_by"] = None
            if pid and pid in self.pilots:
                self.pilots[pid]["controlling"] = None
        if pid:
            self._log(f"Piloto '{pid}' liberou o controle de '{rid}'")
            self._replicate("control", rov=rid, pilot=None)
            self._push_state()

    # -- detecção de falhas de clientes ------------------------------------
    def _liveness_monitor(self):
        while self.running:
            time.sleep(2.0)
            now = time.time()
            with self.lock:
                dead_rovs = [rid for rid, r in self.rovs.items()
                             if now - r["last_seen"] > CLIENT_TIMEOUT]
                dead_pilots = [pid for pid, p in self.pilots.items()
                               if now - p["last_seen"] > CLIENT_TIMEOUT]

            for rid in dead_rovs:
                self._drop_rov(rid, reason="sem heartbeat")
            for pid in dead_pilots:
                self._drop_pilot(pid, reason="sem heartbeat")

    def _drop_rov(self, rid, reason):
        with self.lock:
            info = self.rovs.pop(rid, None)
            if info:
                self.by_addr.pop(info["addr"], None)
            controller = info["controlled_by"] if info else None
            controller_addr = self.pilots.get(controller, {}).get("addr") if controller else None
            if controller and controller in self.pilots:
                self.pilots[controller]["controlling"] = None
        if info:
            self.endpoint.remove_peer(info["addr"])
            self._log(f"ROV '{rid}' OFFLINE ({reason})")
            if controller_addr:
                self.endpoint.send_reliable(controller_addr, {"type": "rov_offline", "rov": rid})
            self._replicate("rov_down", id=rid)
            self._push_state()

    def _drop_pilot(self, pid, reason):
        with self.lock:
            info = self.pilots.pop(pid, None)
            if info:
                self.by_addr.pop(info["addr"], None)
            rid = info["controlling"] if info else None
            if rid and rid in self.rovs:
                self.rovs[rid]["controlled_by"] = None
        if info:
            self.endpoint.remove_peer(info["addr"])
            self._log(f"Piloto '{pid}' OFFLINE ({reason})")
            self._replicate("pilot_down", id=pid)
            self._push_state()

    # -- heartbeats do relay e vigilância do par ---------------------------
    def _heartbeat_loop(self):
        while self.running:
            time.sleep(RELAY_HB_INTERVAL)
            now = time.time()

            # Pinga o outro relay (para ele saber que estamos vivos).
            if self.peer_addr:
                self.endpoint.send_unreliable(self.peer_addr,
                                              {"type": "relay_ping", "role": self.role})

            # Anuncia-se aos clientes conectados: é isso que permite ao cliente
            # detectar a QUEDA do relay (se estes pings pararem, ele faz failover).
            with self.lock:
                addrs = [r["addr"] for r in self.rovs.values()] + \
                        [p["addr"] for p in self.pilots.values()]
                active = self.active
            hb = {"type": "relay_heartbeat", "role": self.role, "active": active}
            for a in addrs:
                self.endpoint.send_unreliable(a, hb)

            # Backup vigiando o primário: silêncio longo => assumir o serviço.
            if self.peer_addr:
                silent = now - self.last_peer_relay
                if self.role == "backup" and not self.active and silent > PRIMARY_TIMEOUT:
                    with self.lock:
                        self.active = True
                        # Usa o estado replicado para reservar os controles aos
                        # donos anteriores durante a janela de failover.
                        self.reserved = dict(self.mirror_control)
                        self.reserved_until = time.time() + RESERVE_WINDOW
                    self._log("PRIMÁRIO sem resposta — BACKUP assumindo como ATIVO. "
                              f"Reservando controles (replicados): {self.reserved or '—'}. "
                              "Aguardando clientes fazerem failover…")
                    self._push_state()
                elif self.role == "primary" and silent > PRIMARY_TIMEOUT and not self.peer_down_logged:
                    self.peer_down_logged = True
                    self._log("Backup sem resposta (par possivelmente offline).")


# ===========================================================================
# INTERFACE GRÁFICA (Tkinter) — camada fina por cima do RelayNode.
# ===========================================================================
def run_gui(node, corner):
    import queue
    import tkinter as tk
    import gui_common as g

    ui_q = queue.Queue()
    node.on_event = ui_q.put  # thread de rede só empurra eventos para a fila

    title = f"RELAY — {'PRIMÁRIO' if node.role == 'primary' else 'BACKUP'}  ({node.bind_addr[0]}:{node.bind_addr[1]})"
    root = g.make_root(title, corner, 460, 560)

    status = tk.Label(root, text="", font=("Segoe UI", 12, "bold"),
                      bg=g.BG, fg=g.FG, pady=6)
    status.pack(fill="x")

    state_box = tk.Text(root, height=14, bg="#11151c", fg=g.FG, relief="flat",
                        font=("Consolas", 9), wrap="word", state="disabled")
    state_box.pack(fill="x", padx=8)

    tk.Label(root, text="Eventos", bg=g.BG, fg=g.ACCENT,
             font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(6, 0))
    log = g.make_log(root, height=14)
    log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def render_state(s):
        role = "PRIMÁRIO" if s["role"] == "primary" else "BACKUP"
        if s["role"] == "primary":
            status.config(text=f"● {role} — ATIVO", fg=g.OKC)
        elif s["active"]:
            status.config(text=f"● {role} — ATIVO (assumiu o primário)", fg=g.WARN)
        else:
            status.config(text=f"○ {role} — em espera (espelhando primário)", fg=g.MUTE)

        lines = ["ROVs conectados:"]
        if s["rovs"]:
            for r in s["rovs"]:
                who = r["controlled_by"] or "—"
                lines.append(f"   • {r['id']:8}  controlado por: {who}")
        else:
            lines.append("   (nenhum)")
        lines.append("")
        lines.append("Pilotos conectados:")
        if s["pilots"]:
            for p in s["pilots"]:
                auth = "auth✓" if p["authed"] else "auth…"
                ctl = p["controlling"] or "—"
                lines.append(f"   • {p['id']:8}  {auth}  controla: {ctl}")
        else:
            lines.append("   (nenhum)")
        if s["role"] == "backup":
            lines.append("")
            lines.append("Estado replicado do primário:")
            if s["mirror_rovs"] or s["mirror_pilots"]:
                lines.append(f"   ROVs: {', '.join(s['mirror_rovs']) or '—'}")
                lines.append(f"   Pilotos: {', '.join(s['mirror_pilots']) or '—'}")
                for rid, pid in s["mirror_control"].items():
                    lines.append(f"   {rid} ← {pid}")
            else:
                lines.append("   (nada replicado ainda)")

        state_box.configure(state="normal")
        state_box.delete("1.0", "end")
        state_box.insert("1.0", "\n".join(lines))
        state_box.configure(state="disabled")

    def handle(item):
        if item["kind"] == "log":
            g.log_append(log, item["text"])
        elif item["kind"] == "state":
            render_state(item)

    g.start_pump(root, ui_q, handle)
    node.start()
    root.protocol("WM_DELETE_WINDOW", lambda: (node.stop(), root.destroy()))
    root.mainloop()


def parse_addr(s):
    host, port = s.split(":")
    return (host, int(port))


def main():
    ap = argparse.ArgumentParser(description="Relay (primário ou backup) do sistema ROV")
    ap.add_argument("--role", choices=["primary", "backup"], default="primary")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--peer", default=None, help="endereço do outro relay, ex: 127.0.0.1:5001")
    ap.add_argument("--loss", type=float, default=0.0, help="fração de pacotes descartados (0..1)")
    ap.add_argument("--corner", default="tl", help="canto da janela: tl/tr/bl/br/c")
    ap.add_argument("--no-gui", action="store_true")
    args = ap.parse_args()

    peer = parse_addr(args.peer) if args.peer else None
    node = RelayNode(args.role, (args.host, args.port), peer, loss=args.loss)

    if args.no_gui:
        node.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            node.stop()
    else:
        run_gui(node, args.corner)


if __name__ == "__main__":
    main()
