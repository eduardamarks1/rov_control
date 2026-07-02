"""
run_demo.py
-----------
Sobe TODOS os hosts de uma vez, cada um em sua própria janela, posicionadas
nos quatro cantos da tela -- ideal para apresentar o trabalho em um PC só, sem
precisar abrir vários terminais nem digitar IPs.

  ┌────────────────────┬────────────────────┐
  │ RELAY primário (tl)│ RELAY backup  (tr) │
  ├────────────────────┼────────────────────┤
  │ ROV rov1      (bl) │ PILOTO pilotoA (br)│
  └────────────────────┴────────────────────┘

Uso:
    python run_demo.py                 # 2 relays + 1 ROV + 1 piloto
    python run_demo.py --two-pilots    # adiciona um 2º piloto (concorrência)
    python run_demo.py --loss 0.2      # 20% de perda de pacotes nos relays
                                       # (mostra a retransmissão do canal
                                       #  confiável funcionando ao vivo)

Como demonstrar o FAILOVER:
    Depois que tudo estiver rodando, FECHE a janela do RELAY primário
    (ou mate o processo). Em poucos segundos o backup vira ATIVO e o ROV e o
    piloto migram sozinhos para ele -- a telemetria volta a fluir.

Para encerrar tudo: volte a este terminal e tecle Enter (ou Ctrl+C).
"""

import argparse
import subprocess
import sys
import time

PRIMARY_PORT = 5000
BACKUP_PORT = 5001
RELAYS = f"127.0.0.1:{PRIMARY_PORT},127.0.0.1:{BACKUP_PORT}"


def spawn(args):
    """Abre um novo processo Python rodando um dos hosts."""
    return subprocess.Popen([sys.executable] + args)


def main():
    ap = argparse.ArgumentParser(description="Sobe a demonstração completa")
    ap.add_argument("--loss", type=float, default=0.0,
                    help="fração de perda de pacotes nos relays (0..1)")
    ap.add_argument("--two-pilots", action="store_true",
                    help="também abre um segundo piloto (demonstra concorrência)")
    args = ap.parse_args()

    loss = ["--loss", str(args.loss)] if args.loss else []
    procs = []

    print("Subindo RELAY primário (canto superior esquerdo)…")
    procs.append(spawn(["relay_server.py", "--role", "primary",
                        "--port", str(PRIMARY_PORT),
                        "--peer", f"127.0.0.1:{BACKUP_PORT}",
                        "--corner", "tl"] + loss))
    time.sleep(0.6)

    print("Subindo RELAY backup (canto superior direito)…")
    procs.append(spawn(["relay_server.py", "--role", "backup",
                        "--port", str(BACKUP_PORT),
                        "--peer", f"127.0.0.1:{PRIMARY_PORT}",
                        "--corner", "tr"] + loss))
    time.sleep(0.6)

    print("Subindo ROV rov1 (canto inferior esquerdo)…")
    procs.append(spawn(["rov_simulator.py", "--id", "rov1",
                        "--relays", RELAYS, "--corner", "bl"]))
    time.sleep(0.6)

    print("Subindo PILOTO pilotoA (canto inferior direito)…")
    procs.append(spawn(["pilot_client.py", "--id", "pilotoA",
                        "--target", "rov1", "--relays", RELAYS, "--corner", "br"]))

    if args.two_pilots:
        time.sleep(0.6)
        print("Subindo PILOTO pilotoB (centro) — vai disputar o rov1…")
        procs.append(spawn(["pilot_client.py", "--id", "pilotoB",
                            "--target", "rov1", "--relays", RELAYS, "--corner", "c"]))

    print("\nTudo no ar! Dica de demonstração:")
    print("  • Feche a janela do RELAY primário para ver o FAILOVER.")
    print("  • Use os botões do piloto (Frente/Ré/Parar) e veja a telemetria.")
    print("\nTecle Enter (ou Ctrl+C) aqui para encerrar tudo.")
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass
        print("Encerrado.")


if __name__ == "__main__":
    main()
