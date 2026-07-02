"""
gui_common.py
-------------
Pequenos utilitários de interface gráfica (Tkinter) compartilhados pelos três
hosts (relay, ROV, piloto). Nada de Sistemas Distribuídos aqui -- é só a
"casca visual".

Dois problemas práticos que este módulo resolve:

  1. POSICIONAR cada janela em um canto diferente da tela, para que os três
     (ou mais) processos apareçam lado a lado na hora da apresentação -- sem
     nenhum navegador nem "localhost:8000", cada host é uma JANELA nativa.

  2. ATUALIZAR a interface a partir das threads de rede com segurança. Tkinter
     não é thread-safe: só a thread principal pode mexer nos widgets. Então as
     threads de rede apenas EMPURRAM eventos para uma fila (queue.Queue), e a
     própria thread da interface DRENA essa fila periodicamente (via .after()).
"""

import queue
import tkinter as tk
from tkinter import scrolledtext

# Paleta simples para deixar as janelas visualmente distintas.
BG = "#0f1117"
FG = "#e6e6e6"
ACCENT = "#4fc3f7"
OKC = "#66bb6a"
WARN = "#ffa726"
BAD = "#ef5350"
MUTE = "#78909c"


def place_window(root, corner, width, height):
    """
    Posiciona a janela em um dos quatro cantos da tela.
    corner: 'tl' (sup-esq), 'tr' (sup-dir), 'bl' (inf-esq), 'br' (inf-dir),
            'c' (centro).
    """
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    margin = 12
    taskbar = 70  # folga para a barra de tarefas na parte de baixo
    positions = {
        "tl": (margin, margin),
        "tr": (sw - width - margin, margin),
        "bl": (margin, sh - height - taskbar),
        "br": (sw - width - margin, sh - height - taskbar),
        "c": ((sw - width) // 2, (sh - height) // 2),
    }
    x, y = positions.get(corner, positions["c"])
    root.geometry(f"{width}x{height}+{x}+{y}")


def start_pump(root, q, handler, interval=80):
    """
    Faz a interface drenar a fila 'q' a cada 'interval' ms, chamando
    handler(item) para cada evento. É a ponte segura entre threads de rede
    e a thread da interface.
    """
    def _pump():
        try:
            while True:
                item = q.get_nowait()
                handler(item)
        except queue.Empty:
            pass
        root.after(interval, _pump)
    root.after(interval, _pump)


def make_log(parent, height=12):
    """Cria um painel de log rolável, já com visual escuro."""
    box = scrolledtext.ScrolledText(
        parent, height=height, bg="#11151c", fg=FG, insertbackground=FG,
        font=("Consolas", 9), relief="flat", wrap="word", state="disabled",
    )
    return box


def log_append(box, text, max_lines=400):
    """Adiciona uma linha ao painel de log e rola para o fim."""
    box.configure(state="normal")
    box.insert("end", text + "\n")
    # limita o histórico para não crescer sem fim
    line_count = int(box.index("end-1c").split(".")[0])
    if line_count > max_lines:
        box.delete("1.0", f"{line_count - max_lines}.0")
    box.see("end")
    box.configure(state="disabled")


def make_root(title, corner, width, height):
    """Cria a janela principal já posicionada e estilizada."""
    root = tk.Tk()
    root.title(title)
    root.configure(bg=BG)
    place_window(root, corner, width, height)
    return root
