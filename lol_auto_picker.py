#!/usr/bin/env python3
"""
lol_auto_picker.py
Mini app rétro (Tk classique) avec deux onglets :
  - POOL   : grille d'icônes de champions, un onglet par rôle, clic pour
             ajouter/retirer un champion de la pool de ce rôle. Sauvegarde
             automatique dans pools.json.
  - PICKER : détecte ton tour de pick en champion select, lock automatiquement
             un champion aléatoire dans la pool du rôle détecté.

Dépendances : pip install requests psutil Pillow
"""

import json
import random
import threading
import time
import tkinter as tk
from pathlib import Path

import psutil
import requests
import urllib3
from PIL import Image, ImageTk

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROLES = ["top", "jungle", "middle", "bottom", "utility"]
ROLE_LABELS = {"top": "TOP", "jungle": "JUNGLE", "middle": "MID", "bottom": "BOT", "utility": "SUPPORT"}

POOLS_FILE = Path(__file__).parent / "pools.json"
ICON_DIR = Path(__file__).parent / "champion_icons"
ICON_SIZE = 44

DEFAULT_POOLS = {
    "top": ["Garen", "Darius", "Malphite"],
    "jungle": ["Warwick", "Amumu"],
    "middle": ["Annie", "Lux"],
    "bottom": ["Ashe", "Miss Fortune"],
    "utility": ["Soraka", "Leona"],
}

FALLBACK_ROLE = "middle"
POLL_INTERVAL = 1.0

RETRO_BG = "#c0c0c0"
RETRO_FONT = ("Courier New", 9, "bold")
RETRO_FONT_BIG = ("Courier New", 12, "bold")


# ----------------------------------------------------------------------
# Persistence des pools
# ----------------------------------------------------------------------
def load_pools():
    if POOLS_FILE.exists():
        with open(POOLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for role in ROLES:
            data.setdefault(role, [])
        return data
    return {role: list(champs) for role, champs in DEFAULT_POOLS.items()}


def save_pools(pools):
    with open(POOLS_FILE, "w", encoding="utf-8") as f:
        json.dump(pools, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# LCU (League Client Update API)
# ----------------------------------------------------------------------
def find_lcu_credentials():
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.info["name"] == "LeagueClientUx.exe":
                cmdline = proc.info["cmdline"] or []
                port = token = None
                for arg in cmdline:
                    if arg.startswith("--app-port="):
                        port = arg.split("=", 1)[1]
                    elif arg.startswith("--remoting-auth-token="):
                        token = arg.split("=", 1)[1]
                if port and token:
                    return port, token
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None, None


class LCUClient:
    def __init__(self, port, token):
        self.base_url = f"https://127.0.0.1:{port}"
        self.auth = ("riot", token)

    def get(self, path):
        r = requests.get(self.base_url + path, auth=self.auth, verify=False, timeout=3)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def patch(self, path, payload):
        r = requests.patch(
            self.base_url + path, auth=self.auth, verify=False, json=payload, timeout=3
        )
        r.raise_for_status()
        return r


# ----------------------------------------------------------------------
# Data Dragon
# ----------------------------------------------------------------------
def load_champion_data():
    """Retourne (version, { nom_affiche: {"id": id_image, "key": id_int} })"""
    versions = requests.get(
        "https://ddragon.leagueoflegends.com/api/versions.json", timeout=5
    ).json()
    latest = versions[0]
    data = requests.get(
        f"https://ddragon.leagueoflegends.com/cdn/{latest}/data/fr_FR/champion.json", timeout=5
    ).json()["data"]
    result = {}
    for v in data.values():
        result[v["name"]] = {"id": v["id"], "key": int(v["key"])}
    return latest, result


def get_icon(version, champ_id):
    ICON_DIR.mkdir(exist_ok=True)
    path = ICON_DIR / f"{champ_id}.png"
    if not path.exists():
        url = f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{champ_id}.png"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        path.write_bytes(resp.content)
    img = Image.open(path).resize((ICON_SIZE, ICON_SIZE))
    return ImageTk.PhotoImage(img)


# ----------------------------------------------------------------------
# Logique de pick
# ----------------------------------------------------------------------
def get_role(session, cell_id):
    for player in session.get("myTeam", []):
        if player.get("cellId") == cell_id:
            return player.get("assignedPosition") or None
    return None


def get_excluded_ids(session):
    excluded = set()
    bans = session.get("bans", {})
    excluded.update(bans.get("myTeamBans", []))
    excluded.update(bans.get("theirTeamBans", []))
    for action_row in session.get("actions", []):
        for action in action_row:
            if action.get("type") == "pick" and action.get("completed"):
                cid = action.get("championId")
                if cid:
                    excluded.add(cid)
    return excluded


def find_my_pending_pick(session, cell_id):
    for action_row in session.get("actions", []):
        for action in action_row:
            if (
                action.get("actorCellId") == cell_id
                and action.get("type") == "pick"
                and action.get("isInProgress")
                and not action.get("completed")
            ):
                return action
    return None


# ----------------------------------------------------------------------
# App
# ----------------------------------------------------------------------
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("LoL Auto Picker")
        self.root.configure(bg=RETRO_BG)
        self.root.geometry("560x480")
        self.root.attributes("-topmost", True)

        print("Chargement des données champions (Data Dragon)...")
        self.version, self.champ_data = load_champion_data()
        self.pools = load_pools()
        self.current_role = ROLES[0]
        self.icon_cache = {}
        self.champ_buttons = {}

        self.picking = False
        self.picker_status = tk.StringVar(value="Arrêté.")

        self._build_ui()

    # ---- Construction UI ----
    def _build_ui(self):
        title = tk.Label(
            self.root, text="=== LoL AUTO PICKER ===", bg=RETRO_BG, font=RETRO_FONT_BIG
        )
        title.pack(pady=4)

        tabs_frame = tk.Frame(self.root, bg=RETRO_BG)
        tabs_frame.pack(fill="x")
        self.tab_pool_btn = tk.Button(
            tabs_frame, text="POOL", font=RETRO_FONT, bd=3, bg=RETRO_BG,
            command=lambda: self.show_tab("pool")
        )
        self.tab_pool_btn.pack(side="left", padx=2, pady=2)
        self.tab_picker_btn = tk.Button(
            tabs_frame, text="PICKER", font=RETRO_FONT, bd=3, bg=RETRO_BG,
            command=lambda: self.show_tab("picker")
        )
        self.tab_picker_btn.pack(side="left", padx=2, pady=2)

        self.content_frame = tk.Frame(self.root, bg=RETRO_BG, bd=3, relief="ridge")
        self.content_frame.pack(fill="both", expand=True, padx=6, pady=6)

        self._build_pool_tab()
        self._build_picker_tab()
        self.show_tab("pool")

    def _build_pool_tab(self):
        self.pool_tab = tk.Frame(self.content_frame, bg=RETRO_BG)

        role_bar = tk.Frame(self.pool_tab, bg=RETRO_BG)
        role_bar.pack(fill="x", pady=4)
        self.role_buttons = {}
        for role in ROLES:
            btn = tk.Button(
                role_bar, text=ROLE_LABELS[role], font=RETRO_FONT, width=8, bd=3,
                bg=RETRO_BG, command=lambda r=role: self.select_role(r)
            )
            btn.pack(side="left", padx=2)
            self.role_buttons[role] = btn

        canvas_frame = tk.Frame(self.pool_tab, bg=RETRO_BG)
        canvas_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            canvas_frame, bg="white", highlightthickness=2, highlightbackground="black"
        )
        scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.grid_frame = tk.Frame(self.canvas, bg="white")
        self.grid_frame.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._populate_grid()
        self._refresh_role_buttons()

    def _populate_grid(self):
        names = sorted(self.champ_data.keys())
        cols = 8
        for i, name in enumerate(names):
            champ_id = self.champ_data[name]["id"]
            if champ_id not in self.icon_cache:
                try:
                    self.icon_cache[champ_id] = get_icon(self.version, champ_id)
                except Exception:
                    continue
            icon = self.icon_cache[champ_id]
            btn = tk.Button(
                self.grid_frame, image=icon, bd=3, relief="raised",
                command=lambda n=name: self.toggle_champ(n)
            )
            btn.image = icon
            btn.grid(row=i // cols, column=i % cols, padx=2, pady=2)
            self.champ_buttons[name] = btn
        self._refresh_grid_selection()

    def _build_picker_tab(self):
        self.picker_tab = tk.Frame(self.content_frame, bg=RETRO_BG)

        status_label = tk.Label(
            self.picker_tab, textvariable=self.picker_status, bg="black", fg="#00ff00",
            font=("Courier New", 10, "bold"), width=50, height=8, anchor="nw", justify="left"
        )
        status_label.pack(padx=8, pady=8, fill="both", expand=True)

        self.start_btn = tk.Button(
            self.picker_tab, text="DEMARRER", font=RETRO_FONT, bd=3, bg=RETRO_BG,
            command=self.toggle_picker
        )
        self.start_btn.pack(pady=6)

    # ---- Bascule d'onglet ----
    def show_tab(self, tab):
        self.pool_tab.pack_forget()
        self.picker_tab.pack_forget()
        if tab == "pool":
            self.tab_pool_btn.config(relief="sunken")
            self.tab_picker_btn.config(relief="raised")
            self.pool_tab.pack(fill="both", expand=True)
        else:
            self.tab_pool_btn.config(relief="raised")
            self.tab_picker_btn.config(relief="sunken")
            self.picker_tab.pack(fill="both", expand=True)

    # ---- Edition des pools ----
    def select_role(self, role):
        self.current_role = role
        self._refresh_role_buttons()
        self._refresh_grid_selection()

    def _refresh_role_buttons(self):
        for role, btn in self.role_buttons.items():
            btn.config(relief="sunken" if role == self.current_role else "raised")

    def toggle_champ(self, name):
        pool = self.pools.setdefault(self.current_role, [])
        if name in pool:
            pool.remove(name)
        else:
            pool.append(name)
        save_pools(self.pools)
        self._refresh_grid_selection()

    def _refresh_grid_selection(self):
        pool = self.pools.get(self.current_role, [])
        for name, btn in self.champ_buttons.items():
            if name in pool:
                btn.config(relief="sunken", bg="#7fff7f")
            else:
                btn.config(relief="raised", bg=RETRO_BG)

    # ---- Picker ----
    def toggle_picker(self):
        if self.picking:
            self.picking = False
            self.start_btn.config(text="DEMARRER")
            self.picker_status.set("Arrêté.")
        else:
            self.picking = True
            self.start_btn.config(text="ARRETER")
            threading.Thread(target=self._picker_loop, daemon=True).start()

    def _picker_loop(self):
        port, token = None, None
        while self.picking and port is None:
            port, token = find_lcu_credentials()
            if port is None:
                self.picker_status.set("League Client non détecté, en attente...")
                time.sleep(2)
        if not self.picking:
            return

        client = LCUClient(port, token)

        while self.picking:
            session = client.get("/lol-champ-select/v1/session")
            if session is None:
                self.picker_status.set("Pas en champion select, en attente...")
                time.sleep(POLL_INTERVAL)
                continue

            cell_id = session.get("localPlayerCellId")
            action = find_my_pending_pick(session, cell_id)

            if action is None:
                self.picker_status.set("En champion select, pas encore ton tour...")
                time.sleep(POLL_INTERVAL)
                continue

            role = get_role(session, cell_id)
            if not role or role not in self.pools:
                self.picker_status.set(f"Rôle non détecté ({role}), fallback '{FALLBACK_ROLE}'")
                role = FALLBACK_ROLE

            excluded = get_excluded_ids(session)
            pool_names = self.pools.get(role, [])
            pool_ids = [self.champ_data[n]["key"] for n in pool_names if n in self.champ_data]
            candidates = [cid for cid in pool_ids if cid not in excluded]

            if not candidates:
                self.picker_status.set(f"Pool '{role}' épuisé, fallback aléatoire global")
                candidates = [d["key"] for d in self.champ_data.values() if d["key"] not in excluded]

            champ_key = random.choice(candidates)
            champ_name = next(n for n, d in self.champ_data.items() if d["key"] == champ_key)

            client.patch(
                f"/lol-champ-select/v1/session/actions/{action['id']}",
                {"championId": champ_key, "completed": True},
            )

            self.picker_status.set(f"Locké : {champ_name} ({role})\nEn attente de la prochaine partie...")
            time.sleep(5)  # laisse la partie démarrer avant de repoll une session

    def run(self):
        self.root.mainloop()


def main():
    app = App()
    app.run()


if __name__ == "__main__":
    main()
