"""
demo_dashboard.py
-----------------
PAINEL DE DEMONSTRAÇÃO (o "showpiece" da apresentação).

Em UMA janela só, roda TODOS os nós do sistema (2 relays, o ROV e os pilotos)
no mesmo processo, mas ainda se comunicando por **UDP de verdade** (loopback) —
ou seja, é distribuído de fato, só que co-localizado para caber em um PC.

Dois painéis lado a lado:

  ┌── TOPOLOGIA DA REDE ──────────┬── ROV NA ÁGUA ────────────┐
  │ nós + arestas + PACOTES reais │ cena submarina 2D que      │
  │ voando (azul=confiável,       │ reage à telemetria: o ROV  │
  │ cinza=telemetria, verde=ACK,  │ desce/sobe, solta bolhas   │
  │ vermelho=perdido). O failover │ com o thruster e apaga a   │
  │ acontece VISUALMENTE.         │ luz quando a bateria cai.  │
  └───────────────────────────────┴────────────────────────────┘

Controles ao vivo: conectar o piloto, comandar o ROV, **derrubar o relay
primário** (e ver a topologia se curar), **ligar perda de pacotes** (e ver a
retransmissão) e **adicionar um 2º piloto** (concorrência).

Rodar:   python demo_dashboard.py
Teste:   python demo_dashboard.py --selftest   (roteiriza a demo e verifica)
"""

import argparse
import math
import queue
import tkinter as tk

import quiclite as q
from quiclite import PKT_DATA_REL, PKT_DATA_UNREL, PKT_ACK
from relay_server import RelayNode
from rov_simulator import RovNode
from pilot_client import PilotNode
from protocol import PILOT_CREDENTIALS
import gui_common as g

PRIMARY = ("127.0.0.1", 5000)
BACKUP = ("127.0.0.1", 5001)

# Cores dos pacotes por tipo (o "idioma visual" da rede).
PKT_COLOR = {PKT_DATA_REL: "#42a5f5", PKT_DATA_UNREL: "#78909c", PKT_ACK: "#66bb6a"}
DROP_COLOR = "#ef5350"

TOPO_W, TOPO_H = 520, 500
WATER_W, WATER_H = 460, 500
FPS_MS = 33  # ~30 quadros por segundo


def lerp(a, b, t):
    return a + (b - a) * t


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(c1, c2, t):
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    return f"#{int(lerp(r1, r2, t)):02x}{int(lerp(g1, g2, t)):02x}{int(lerp(b1, b2, t)):02x}"


class Dashboard:
    def __init__(self, root):
        self.root = root
        self.frame = 0
        self.tap_q = queue.Queue()
        self.evt_q = queue.Queue()
        self.packets = []   # pacotes voando na topologia
        self.bubbles = []   # bolhas na água
        self.logs = []
        self.primary_alive = True

        # --- posições dos nós na topologia ---
        self.pos = {
            "relay-primary": (150, 120),
            "relay-backup": (370, 120),
            "rov-rov1": (150, 370),
            "pilot-pilotoA": (370, 370),
            "pilot-pilotoB": (260, 455),
        }
        self.port_name = {PRIMARY[1]: "relay-primary", BACKUP[1]: "relay-backup"}
        self.has_pilotB = False

        # --- cria e liga os nós ---
        self.primary = RelayNode("primary", PRIMARY, BACKUP)
        self.backup = RelayNode("backup", BACKUP, PRIMARY)
        self.rov = RovNode("rov1", [PRIMARY, BACKUP])
        self.pilotA = PilotNode("pilotoA", PILOT_CREDENTIALS["pilotoA"], "rov1", [PRIMARY, BACKUP])

        self._start_node(self.primary)
        self._start_node(self.backup)
        self._start_node(self.rov)
        self._start_node(self.pilotA, autoconnect=False)  # piloto conecta no botão

        self.port_name[self.rov.endpoint.local_port] = "rov-rov1"
        self.port_name[self.pilotA.endpoint.local_port] = "pilot-pilotoA"

        self._build_ui()
        self._tick()
        g.start_pump(root, self.evt_q, self._on_evt)

    # -- ciclo de vida dos nós ---------------------------------------------
    def _start_node(self, node, autoconnect=True):
        node.on_event = self.evt_q.put
        if isinstance(node, PilotNode):
            node.start(autoconnect=autoconnect)
        else:
            node.start()
        node.endpoint.on_tap = self.tap_q.put

    def _on_evt(self, ev):
        if ev.get("kind") == "log":
            self.logs.append(ev["text"])
            self.logs = self.logs[-200:]
            self.log_box.configure(state="normal")
            self.log_box.insert("end", ev["text"] + "\n")
            if int(self.log_box.index("end-1c").split(".")[0]) > 200:
                self.log_box.delete("1.0", "50.0")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

    # ======================================================================
    # INTERFACE
    # ======================================================================
    def _build_ui(self):
        top = tk.Frame(self.root, bg=g.BG)
        top.pack(fill="x", padx=8, pady=6)

        self.topo = tk.Canvas(top, width=TOPO_W, height=TOPO_H, bg="#0c0f16",
                              highlightthickness=0)
        self.topo.pack(side="left")
        self.water = tk.Canvas(top, width=WATER_W, height=WATER_H, bg="#021018",
                               highlightthickness=0)
        self.water.pack(side="left", padx=(8, 0))
        self._draw_water_bg()

        # --- barra de controles ---
        ctl = tk.Frame(self.root, bg=g.BG)
        ctl.pack(fill="x", padx=8)

        self.btn_connect = self._btn(ctl, "🔌 Conectar Piloto A", g.ACCENT, self.connect_pilotA)
        self.btn_connect.pack(side="left", padx=2)

        pf = tk.Frame(ctl, bg=g.BG); pf.pack(side="left", padx=6)
        tk.Label(pf, text="Potência", bg=g.BG, fg=g.FG, font=("Segoe UI", 8)).pack()
        self.power = tk.Scale(pf, from_=0, to=100, orient="horizontal", length=110,
                              bg=g.BG, fg=g.FG, troughcolor="#11151c", highlightthickness=0)
        self.power.set(60); self.power.pack()

        self._btn(ctl, "▲ Frente", g.OKC, lambda: self.cmd("thruster_frente")).pack(side="left", padx=2)
        self._btn(ctl, "▼ Ré", g.ACCENT, lambda: self.cmd("thruster_re")).pack(side="left", padx=2)
        self._btn(ctl, "■ Parar", g.WARN, lambda: self.cmd("parar")).pack(side="left", padx=2)
        self._btn(ctl, "＋ Piloto B", "#7e57c2", self.add_pilotB).pack(side="left", padx=2)

        self.btn_kill = self._btn(ctl, "☠ Derrubar primário", g.BAD, self.toggle_primary)
        self.btn_kill.pack(side="left", padx=2)

        lf = tk.Frame(ctl, bg=g.BG); lf.pack(side="left", padx=6)
        tk.Label(lf, text="Perda de pacotes %", bg=g.BG, fg=g.FG, font=("Segoe UI", 8)).pack()
        self.loss = tk.Scale(lf, from_=0, to=40, orient="horizontal", length=120,
                             bg=g.BG, fg=g.FG, troughcolor="#11151c", highlightthickness=0,
                             command=self._set_loss)
        self.loss.pack()

        # --- legenda + log ---
        bottom = tk.Frame(self.root, bg=g.BG)
        bottom.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        legend = tk.Frame(bottom, bg=g.BG); legend.pack(anchor="w")
        for txt, col in [("● confiável (comando)", "#42a5f5"),
                         ("● telemetria", "#78909c"),
                         ("● ACK", "#66bb6a"),
                         ("✕ perdido", DROP_COLOR)]:
            tk.Label(legend, text=txt, bg=g.BG, fg=col, font=("Segoe UI", 9, "bold")).pack(side="left", padx=8)
        self.log_box = g.make_log(bottom, height=6)
        self.log_box.pack(fill="both", expand=True, pady=(4, 0))

    def _btn(self, parent, text, color, fn):
        return tk.Button(parent, text=text, command=fn, bg=color, fg="#0f1117",
                         activebackground=color, relief="flat",
                         font=("Segoe UI", 9, "bold"), padx=6, pady=6)

    # ======================================================================
    # AÇÕES (botões)
    # ======================================================================
    def connect_pilotA(self):
        self.pilotA.connect()
        self.btn_connect.config(state="disabled", text="Piloto A conectando…")

    def cmd(self, action):
        val = 0 if action == "parar" else int(self.power.get())
        self.pilotA.send_command(action, val)

    def add_pilotB(self):
        if self.has_pilotB:
            return
        self.has_pilotB = True
        self.pilotB = PilotNode("pilotoB", PILOT_CREDENTIALS["pilotoB"], "rov1", [PRIMARY, BACKUP])
        self._start_node(self.pilotB, autoconnect=False)
        self.port_name[self.pilotB.endpoint.local_port] = "pilot-pilotoB"
        self.pilotB.connect()

    def toggle_primary(self):
        if self.primary_alive:
            self.primary.stop()
            self.primary_alive = False
            self.btn_kill.config(text="♻ Reviver primário", bg=g.OKC)
        else:
            self.primary = RelayNode("primary", PRIMARY, BACKUP)
            self._start_node(self.primary)
            self.primary_alive = True
            self.btn_kill.config(text="☠ Derrubar primário", bg=g.BAD)

    def _set_loss(self, _=None):
        frac = self.loss.get() / 100.0
        for relay in (self.primary, self.backup):
            if relay and relay.endpoint:
                relay.endpoint.loss = frac

    # ======================================================================
    # ANIMAÇÃO
    # ======================================================================
    def _tick(self):
        self.frame += 1
        self._drain_taps()
        self._advance()
        self._draw_topology()
        self._draw_water()
        self.root.after(FPS_MS, self._tick)

    def _drain_taps(self):
        try:
            while True:
                t = self.tap_q.get_nowait()
                if len(self.packets) > 90:
                    continue  # evita acúmulo em rajadas
                src = t["src"]
                dst = self.port_name.get(t["dst_port"])
                if src not in self.pos or dst not in self.pos:
                    continue
                sx, sy = self.pos[src]
                dx, dy = self.pos[dst]
                self.packets.append({"sx": sx, "sy": sy, "dx": dx, "dy": dy,
                                     "prog": 0.0, "dropped": t["dropped"],
                                     "color": DROP_COLOR if t["dropped"] else PKT_COLOR.get(t["ptype"], "#fff")})
        except queue.Empty:
            pass

    def _advance(self):
        alive = []
        for p in self.packets:
            p["prog"] += 0.045
            # pacote perdido "morre" no meio do caminho
            if p["dropped"] and p["prog"] >= 0.5:
                continue
            if p["prog"] < 1.0:
                alive.append(p)
        self.packets = alive

        # bolhas sobem
        for b in self.bubbles:
            b["y"] -= b["vy"]
            b["x"] += math.sin((self.frame + b["ph"]) * 0.2) * 0.6
        self.bubbles = [b for b in self.bubbles if b["y"] > 50]

    # -- topologia ----------------------------------------------------------
    def _draw_topology(self):
        c = self.topo
        c.delete("dyn")
        c.create_text(TOPO_W // 2, 16, text="TOPOLOGIA DA REDE", fill=g.ACCENT,
                      font=("Segoe UI", 11, "bold"), tags="dyn")

        # arestas candidatas (faint)
        edges = [("relay-primary", "relay-backup"),
                 ("relay-primary", "rov-rov1"), ("relay-primary", "pilot-pilotoA"),
                 ("relay-backup", "rov-rov1"), ("relay-backup", "pilot-pilotoA")]
        if self.has_pilotB:
            edges += [("relay-primary", "pilot-pilotoB"), ("relay-backup", "pilot-pilotoB")]
        for a, b in edges:
            dash = (4, 3) if {"relay-primary", "relay-backup"} == {a, b} else None
            x1, y1 = self.pos[a]; x2, y2 = self.pos[b]
            c.create_line(x1, y1, x2, y2, fill="#1e2836", width=2,
                          dash=dash, tags="dyn")

        # arestas ATIVAS (cliente -> relay atual) destacadas
        for node, name in ((self.rov, "rov-rov1"), (self.pilotA, "pilot-pilotoA")):
            self._active_edge(node, name)
        if self.has_pilotB:
            self._active_edge(self.pilotB, "pilot-pilotoB")

        # pacotes voando
        for p in self.packets:
            t = p["prog"]
            x = lerp(p["sx"], p["dx"], t); y = lerp(p["sy"], p["dy"], t)
            if p["dropped"] and t >= 0.42:
                c.create_text(x, y, text="✕", fill=DROP_COLOR,
                              font=("Segoe UI", 13, "bold"), tags="dyn")
            else:
                c.create_oval(x - 4, y - 4, x + 4, y + 4, fill=p["color"],
                              outline="", tags="dyn")

        # nós por cima
        self._node_box("relay-primary", "RELAY P",
                       ("ATIVO", g.OKC) if self.primary_alive else ("CAIU ☠", g.BAD))
        self._node_box("relay-backup", "RELAY B",
                       ("ATIVO (assumiu)", g.WARN) if self.backup.active else ("em espera", g.MUTE))
        self._node_box("rov-rov1", "ROV rov1",
                       ("online", g.OKC) if self.rov.registered else ("reconectando…", g.WARN))
        self._node_box("pilot-pilotoA", "PILOTO A", self._pilot_status(self.pilotA))
        if self.has_pilotB:
            self._node_box("pilot-pilotoB", "PILOTO B", self._pilot_status(self.pilotB))

    def _active_edge(self, node, name):
        if not getattr(node, "registered", False) and not getattr(node, "authed", False):
            return
        relay = "relay-primary" if node.current == PRIMARY else "relay-backup"
        x1, y1 = self.pos[name]; x2, y2 = self.pos[relay]
        self.topo.create_line(x1, y1, x2, y2, fill="#37556b", width=4, tags="dyn")

    def _pilot_status(self, p):
        if getattr(p, "controlling", None):
            return ("controla ✓", g.OKC)
        if getattr(p, "authed", False):
            return ("autenticado", g.ACCENT)
        if getattr(p, "_connect_started", False):
            return ("conectando…", g.WARN)
        return ("desconectado", g.MUTE)

    def _node_box(self, name, title, status):
        if name not in self.pos:
            return
        x, y = self.pos[name]
        label, color = status
        w, h = 104, 52
        self.topo.create_rectangle(x - w // 2, y - h // 2, x + w // 2, y + h // 2,
                                   fill="#161d29", outline=color, width=2, tags="dyn")
        self.topo.create_text(x, y - 9, text=title, fill=g.FG,
                              font=("Segoe UI", 10, "bold"), tags="dyn")
        self.topo.create_text(x, y + 11, text=label, fill=color,
                              font=("Segoe UI", 8, "bold"), tags="dyn")

    # -- água ---------------------------------------------------------------
    def _draw_water_bg(self):
        c = self.water
        bands = 26
        for i in range(bands):
            y0 = 40 + i * (WATER_H - 40) / bands
            y1 = 40 + (i + 1) * (WATER_H - 40) / bands
            col = mix("#0a4a6b", "#02141f", i / bands)
            c.create_rectangle(0, y0, WATER_W, y1, fill=col, outline="")
        c.create_rectangle(0, WATER_H - 16, WATER_W, WATER_H, fill="#2e2318", outline="")  # fundo

    def _draw_water(self):
        c = self.water
        c.delete("dyn")
        snap = self.rov.state.snapshot()
        depth, battery = snap["depth"], snap["battery"]
        thr = snap["thruster_power"]

        # superfície ondulada
        pts = []
        for x in range(0, WATER_W + 1, 20):
            pts += [x, 40 + 4 * math.sin(x * 0.05 + self.frame * 0.08)]
        c.create_line(*pts, fill="#7fd7ff", width=2, smooth=True, tags="dyn")
        c.create_text(WATER_W // 2, 18, text="ROV NA ÁGUA", fill="#7fd7ff",
                      font=("Segoe UI", 11, "bold"), tags="dyn")

        # posição do ROV a partir da profundidade
        cx = WATER_W // 2 + 12 * math.sin(self.frame * 0.05)
        cy = 60 + min(max(depth, 0), 28) / 28 * (WATER_H - 130)

        # bolhas do propulsor (quando há empuxo)
        if abs(thr) > 0 and self.frame % 3 == 0:
            for _ in range(1 + min(int(abs(thr) / 35), 3)):
                self.bubbles.append({"x": cx - 46, "y": cy + 4, "r": 2 + abs(thr) / 60.0,
                                     "vy": 1.6 + abs(thr) / 80.0, "ph": self.frame % 10})
        for b in self.bubbles:
            c.create_oval(b["x"] - b["r"], b["y"] - b["r"], b["x"] + b["r"], b["y"] + b["r"],
                          outline="#bfeaff", tags="dyn")

        # corpo do ROV
        c.create_rectangle(cx - 40, cy - 16, cx + 40, cy + 16, fill="#ffb300",
                           outline="#5b3d00", width=2, tags="dyn")
        c.create_rectangle(cx - 24, cy - 26, cx + 20, cy - 16, fill="#ff8f00",
                           outline="#5b3d00", width=2, tags="dyn")  # torre/hull
        # propulsor (esquerda) girando
        spin = 6 if (self.frame // 2) % 2 == 0 else -6
        c.create_line(cx - 40, cy - spin, cx - 40, cy + spin, fill="#263238", width=3, tags="dyn")
        c.create_oval(cx - 46, cy - 8, cx - 34, cy + 8, outline="#263238", width=2, tags="dyn")
        # farol frontal (brilho pela bateria)
        bright = max(0.15, battery / 100.0)
        light = mix("#3a3a00", "#fff59d", bright)
        c.create_oval(cx + 30, cy - 8, cx + 48, cy + 8, fill=light, outline="#5b3d00", tags="dyn")
        if battery < 25 and (self.frame // 10) % 2 == 0:
            c.create_text(cx, cy - 38, text="⚠ BATERIA BAIXA", fill=g.BAD,
                          font=("Segoe UI", 9, "bold"), tags="dyn")

        # instrumentos
        self._gauge(10, 54, "Bateria", f"{battery:.0f}%", battery / 100.0,
                    g.OKC if battery > 25 else g.BAD)
        c.create_text(WATER_W - 12, 54, anchor="e", fill=g.FG, tags="dyn",
                      font=("Consolas", 10, "bold"),
                      text=f"prof {depth:5.2f} m\ntemp {snap['temperature']:.1f}°C\nthr  {thr:+d}")

    def _gauge(self, x, y, label, val, frac, color):
        c = self.water
        c.create_text(x, y - 12, anchor="w", text=label, fill=g.FG,
                      font=("Segoe UI", 9), tags="dyn")
        c.create_rectangle(x, y, x + 130, y + 12, outline="#37474f", tags="dyn")
        c.create_rectangle(x, y, x + 130 * max(0, min(frac, 1)), y + 12, fill=color,
                           outline="", tags="dyn")
        c.create_text(x + 65, y + 6, text=val, fill="#0f1117",
                      font=("Segoe UI", 8, "bold"), tags="dyn")

    def stop_all(self):
        for n in [self.primary, self.backup, self.rov, self.pilotA]:
            try:
                n.stop()
            except Exception:
                pass
        if self.has_pilotB:
            try:
                self.pilotB.stop()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description="Painel de demonstração do sistema ROV")
    ap.add_argument("--selftest", action="store_true",
                    help="roteiriza a demo e verifica o failover automaticamente")
    args = ap.parse_args()

    root = g.make_root("ROV distribuído — Painel de Demonstração", "c", 1000, 730)
    dash = Dashboard(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (dash.stop_all(), root.destroy()))

    if args.selftest:
        result = {}

        def check_and_quit():
            result["backup_active"] = dash.backup.active
            result["pilotA_controla"] = dash.pilotA.controlling == "rov1"
            result["rov_no_backup"] = dash.rov.current == BACKUP
            ok = all(result.values())
            print("SELFTEST:", result)
            print("SELFTEST_OK" if ok else "SELFTEST_FALHOU")
            dash.stop_all(); root.destroy()

        root.after(800, dash.connect_pilotA)
        root.after(2500, lambda: dash.cmd("thruster_frente"))
        root.after(4000, dash.add_pilotB)
        root.after(6500, dash.toggle_primary)   # derruba o primário
        root.after(15000, check_and_quit)

    root.mainloop()


if __name__ == "__main__":
    main()
