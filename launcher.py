import ctypes
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import queue
import uuid as uuidlib
import hashlib
import zipfile
import platform
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import requests

LAUNCHER_NAME = "FurkaLauncher"
LAUNCHER_VERSION = "1.0"
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    DATA_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    DATA_DIR = BASE_DIR
CONFIG_FILE = BASE_DIR / "config.json"
DB_FILE = BASE_DIR / "accounts.db"

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
LIBRARY_FALLBACK_URL = "https://libraries.minecraft.net/"
ASSETS_FALLBACK_URL = "https://resources.download.minecraft.net/"
DEFAULT_NEWS_URL = "https://launchercontent.mojang.com/v2/javaPatchNotes.json"

BG = "#141417"
PANEL = "#18181C"
SIDEBAR = "#151517"
SEL = "#26262B"
BORDER = "#1C1C20"
ACCENT = "#8B5CF6"
ACCENT_HOVER = "#7C3AED"
TEXT = "#FFFFFF"
TEXT2 = "#D8D8D8"
MUTED = "#656571"
GREEN = "#14CF7E"
ASSETS_DIR = DATA_DIR / "assets"
FONT_DIR = ASSETS_DIR / "fonts"
ICON_DIR = ASSETS_DIR / "icons"


def load_fonts():
    if os.name == "nt":
        for f in FONT_DIR.glob("*.ttf"):
            try:
                ctypes.windll.gdi32.AddFontResourceExW(str(f.resolve()), 0x10, 0)
            except Exception:
                pass


def hash_password(password, salt=None):
    if salt is None:
        salt = uuidlib.uuid4().hex
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return digest, salt


class AccountDB:
    def __init__(self):
        self.conn = sqlite3.connect(str(DB_FILE))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "username TEXT PRIMARY KEY, "
            "password_hash TEXT NOT NULL, "
            "salt TEXT NOT NULL, "
            "uuid TEXT NOT NULL)"
        )
        self.conn.commit()

    def register(self, username, password):
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", username):
            raise ValueError("Имя: 3-16 символов, только латиница, цифры и _")
        if not password:
            raise ValueError("Пароль не может быть пустым")
        if self.conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError("Такой пользователь уже существует")
        digest, salt = hash_password(password)
        self.conn.execute(
            "INSERT INTO users (username, password_hash, salt, uuid) VALUES (?,?,?,?)",
            (username, digest, salt, str(uuidlib.uuid4())),
        )
        self.conn.commit()
        return True

    def login(self, username, password):
        row = self.conn.execute(
            "SELECT password_hash, salt, uuid FROM users WHERE username=?", (username.strip(),)
        ).fetchone()
        if not row:
            raise ValueError("Пользователь не найден")
        digest, salt, user_uuid = row
        if hash_password(password, salt)[0] != digest:
            raise ValueError("Неверный пароль")
        return {"username": username.strip(), "uuid": user_uuid}


class Config:
    def __init__(self):
        self.data = {
            "java_path": "",
            "ram": 2048,
            "game_dir": str(BASE_DIR / "minecraft"),
            "news_url": DEFAULT_NEWS_URL,
            "version": "Phobia",
            "last_user": "",
        }
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                self.data.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        CONFIG_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


class MojangAPI:
    @staticmethod
    def session():
        return requests.Session()

    @staticmethod
    def get_json(url, session=None):
        s = session or requests
        resp = s.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def download(url, dest: Path, session=None):
        s = session or requests
        resp = s.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
        return dest


class Minecraft:
    def __init__(self, config: Config):
        self.config = config
        self.base_dir = Path(config.data["game_dir"])
        self.libraries_dir = self.base_dir / "libraries"
        self.assets_dir = self.base_dir / "assets"
        self.natives_dir = self.base_dir / "natives"
        self.versions_dir = self.base_dir / "versions"

    def resolve_version(self, session) -> dict:
        manifest = MojangAPI.get_json(MANIFEST_URL, session)
        if self.config.data["version"] == "latest_release":
            version_id = manifest["latest"]["release"]
        elif self.config.data["version"] == "latest_snapshot":
            version_id = manifest["latest"]["snapshot"]
        else:
            version_id = self.config.data["version"]
        entry = next((v for v in manifest["versions"] if v["id"] == version_id), None)
        if not entry:
            raise RuntimeError(f"Версия {version_id} не найдена в манифесте Mojang")
        return entry

    @staticmethod
    def _rule_allows(rules, os_name) -> bool:
        if not rules:
            return True
        result = False
        for rule in rules:
            ok = True
            if "os" in rule:
                r_os = rule["os"]
                if "name" in r_os and r_os["name"] != os_name:
                    ok = False
            if ok:
                result = rule.get("action") == "allow"
        return result

    def prepare(self, version_entry: dict, session, report) -> dict:
        """Скачивает всё необходимое, возвращает параметры запуска."""
        version_dir = self.versions_dir / version_entry["id"]
        jar = version_dir / f"{version_entry['id']}.jar"
        json_path = version_dir / f"{version_entry['id']}.json"

        version_json = MojangAPI.get_json(version_entry["url"], session)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(version_json, indent=2), encoding="utf-8")

        os_name = "windows" if platform.system() == "Windows" else ("linux" if platform.system() == "Linux" else "osx")
        classpath = []

        libraries = version_json.get("libraries", [])
        for i, lib in enumerate(libraries):
            report(f"Библиотека {i + 1}/{len(libraries)}: {lib.get('name', '')}")
            if not self._rule_allows(lib.get("rules"), os_name):
                continue
            artifact = lib.get("downloads", {}).get("artifact", {})
            if not artifact:
                continue
            dest = self.libraries_dir / artifact["path"]
            if not dest.exists() or (dest.stat().st_size != artifact.get("size", dest.stat().st_size)):
                url = artifact.get("url")
                if not url:
                    url = LIBRARY_FALLBACK_URL + artifact["path"]
                MojangAPI.download(url, dest, session)
            classpath.append(str(dest))

            if lib.get("natives"):
                natives_entry = lib.get("downloads", {}).get("classifiers", {})
                classifier_key = f"natives-{os_name}"
                if classifier_key not in natives_entry:
                    continue
                n = natives_entry[classifier_key]
                jar_path = self.libraries_dir / n["path"]
                if not jar_path.exists():
                    MojangAPI.download(n.get("url") or LIBRARY_FALLBACK_URL + n["path"], jar_path, session)
                self.natives_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(jar_path) as zf:
                    for member in zf.namelist():
                        if member.endswith(".dll") or member.endswith(".so") or member.endswith(".dylib"):
                            zf.extract(member, self.natives_dir)

        if not jar.exists():
            report("Скачивание клиента игры...")
            client = version_json.get("downloads", {}).get("client", {})
            if not client or not client.get("url"):
                raise RuntimeError("В манифесте версии нет ссылки на клиент")
            MojangAPI.download(client["url"], jar, session)
        classpath.append(str(jar))

        report("Проверка ассетов...")
        asset_index = version_json.get("assetIndex", {})
        index_path = self.assets_dir / "indexes" / f"{asset_index.get('id', 'legacy')}.json"
        if not index_path.exists():
            MojangAPI.download(asset_index["url"], index_path, session)
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        objects = index_data.get("objects", {})
        for i, (name, obj) in enumerate(objects.items()):
            if i % 100 == 0:
                report(f"Ассеты {i}/{len(objects)}")
            h = obj["hash"]
            dest = self.assets_dir / "objects" / h[:2] / h
            if not dest.exists():
                MojangAPI.download(
                    ASSETS_FALLBACK_URL + h[:2] + "/" + h, dest, session
                )
            virtual = self.assets_dir / "virtual" / asset_index["id"] / name
            if asset_index.get("virtual") and not virtual.exists():
                virtual.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, virtual)

        report("Подготовка команд запуска...")
        return {
            "version_json": version_json,
            "classpath": classpath,
            "assets_index": asset_index.get("id", "legacy"),
            "jar": jar,
        }

    def build_command(self, prepared: dict, account: dict) -> list:
        vj = prepared["version_json"]
        java_path = self.config.data["java_path"].strip() or "java"
        args = ["-Xmx" + str(self.config.data["ram"]) + "M"]

        if "arguments" in vj:
            jvm = vj["arguments"].get("jvm", [])
            game = vj["arguments"].get("game", [])
        else:
            jvm = []
            game = [a for a in vj.get("minecraftArguments", "").split(" ") if a]

        def expand(a):
            return (
                a.replace("${auth_player_name}", account["username"])
                .replace("${version_name}", vj["id"])
                .replace("${game_directory}", str(self.base_dir).replace("\\", "/"))
                .replace("${assets_root}", str(self.assets_dir).replace("\\", "/"))
                .replace("${assets_index_name}", prepared["assets_index"])
                .replace("${auth_uuid}", account.get("uuid", uuidlib.uuid4().hex))
                .replace("${auth_access_token}", "0")
                .replace("${clientid}", LAUNCHER_NAME)
                .replace("${auth_xuid}", "0")
                .replace("${user_type}", "legacy")
                .replace("${version_type}", vj.get("type", "release"))
                .replace("${resolution_width}", "854")
                .replace("${resolution_height}", "480")
                .replace("${launcher_name}", LAUNCHER_NAME)
                .replace("${launcher_version}", LAUNCHER_VERSION)
            )

        jvm_args = []
        for item in jvm:
            if isinstance(item, str):
                jvm_args.append(expand(item))
            elif isinstance(item, dict):
                if self._rule_allows(item.get("rules"), platform.system().lower()):
                    for a in item["value"] if isinstance(item["value"], list) else [item["value"]]:
                        jvm_args.append(expand(a))

        game_args = []
        for item in game:
            if isinstance(item, str):
                game_args.append(expand(item))
            elif isinstance(item, dict):
                if self._rule_allows(item.get("rules"), platform.system().lower()):
                    for a in item["value"] if isinstance(item["value"], list) else [item["value"]]:
                        game_args.append(expand(a))

        if not jvm_args:
            jvm_args = ["-Djava.library.path=" + str(self.natives_dir).replace("\\", "/"),
                        "-cp", os.pathsep.join([str(p) for p in prepared["classpath"]])]
        else:
            jvm_args = [a.replace("${classpath}", os.pathsep.join([str(p) for p in prepared["classpath"]])) for a in jvm_args]
            jvm_args = [a.replace("${natives_directory}", str(self.natives_dir).replace("\\", "/")) for a in jvm_args]

        cmd = [java_path] + jvm_args + [vj["mainClass"]] + game_args
        return cmd


class NewsFeed:
    def __init__(self, url):
        self.url = url

    def fetch(self):
        try:
            data = requests.get(self.url, timeout=20).json()
        except Exception:
            return "Новости недоступны (проверьте соединение или URL в настройках)."
        lines = []
        entries = data.get("entries", []) if isinstance(data, dict) else data
        for e in entries[:15]:
            date = e.get("date", "")[:10]
            title = e.get("title", "Без названия")
            body = (e.get("body", "") or "").strip()
            text = f"[{date}] {title}"
            if body:
                text += "\n" + body[:300] + ("..." if len(body) > 300 else "")
            lines.append(text + "\n" + "-" * 40)
        return "\n".join(lines) if lines else "Новостей нет."


class LauncherApp:
    NAV = [("play", "Игра"), ("news", "Новости"), ("account", "Аккаунт"), ("settings", "Настройки")]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = Config()
        self.db = AccountDB()
        self.account = None
        self.game_process = None
        self.msg_queue = queue.Queue()
        self.nav_buttons = {}
        self.pages = {}

        root.title(LAUNCHER_NAME)
        root.geometry("1120x650")
        root.minsize(960, 560)
        root.configure(bg=BG)
        try:
            root.iconbitmap(str(ICON_DIR / "app.ico"))
        except tk.TclError:
            pass

        self._build_chrome()
        self._build_play_page()
        self._build_news_page()
        self._build_account_page()
        self._build_settings_page()
        self.show_page("play")

        if self.config.data["last_user"]:
            self.user_var.set(self.config.data["last_user"])

        root.after(100, self._poll_queue)
        root.after(300, self._load_news)

    def _btn(self, parent, text, command, kind="ghost", font=("Onest", 11), padx=8, pady=6):
        bg = ACCENT if kind == "primary" else SEL
        hover = ACCENT_HOVER if kind == "primary" else "#303037"
        b = tk.Button(parent, text=text, command=command,
                      bg=bg, fg=TEXT, activebackground=hover, activeforeground=TEXT,
                      relief="flat", bd=0, highlightthickness=0,
                      font=font, padx=padx, pady=pady, cursor="hand2")
        b.bind("<Enter>", lambda e: b.config(bg=hover))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    def _entry(self, parent, variable, show=None, width=30):
        e = tk.Entry(parent, textvariable=variable, show=show, width=width,
                     bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                     highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=ACCENT, font=("Onest", 11))
        e.bind("<FocusIn>", lambda ev: e.config(highlightbackground=ACCENT))
        e.bind("<FocusOut>", lambda ev: e.config(highlightbackground=BORDER))
        return e

    def _card(self, parent):
        return tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)

    def _build_chrome(self):
        bar = tk.Frame(self.root, bg=BG, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        logo = tk.Frame(bar, bg=BG)
        logo.pack(side="left", padx=24)
        tk.Label(logo, text=LAUNCHER_NAME, font=("Onest", 13, "bold"), bg=BG, fg=TEXT).pack(side="left")
        tk.Label(logo, text="v" + LAUNCHER_VERSION, font=("Onest", 9), bg=BG, fg=MUTED).pack(side="left", padx=(8, 0))

        win = tk.Frame(bar, bg=BG)
        win.pack(side="right", padx=16)
        self._btn(win, "—", self.root.iconify, padx=10).pack(side="left", padx=3)
        self._btn(win, "✕", self.root.destroy, padx=10).pack(side="left")

        self.account_pill = tk.Label(bar, text="Не авторизован", font=("Onest", 10), bg=PANEL, fg=MUTED,
                                     padx=12, pady=6)
        self.account_pill.pack(side="right", padx=16)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        sidebar = tk.Frame(body, bg=SIDEBAR, width=210)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        for key, label in self.NAV:
            b = tk.Button(sidebar, text=label, command=lambda k=key: self.show_page(k),
                          bg=SIDEBAR, fg=MUTED, activebackground=SEL, activeforeground=TEXT,
                          relief="flat", bd=0, anchor="w", padx=24, pady=12,
                          font=("Onest", 12), cursor="hand2")
            b.pack(fill="x", pady=1)
            self.nav_buttons[key] = b

        self.content = tk.Frame(body, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

    def show_page(self, key):
        for k, b in self.nav_buttons.items():
            active = k == key
            b.config(bg=SEL if active else SIDEBAR, fg=TEXT if active else MUTED)
        self.pages[key].tkraise()

    def _build_play_page(self):
        page = tk.Frame(self.content, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages["play"] = page

        row = tk.Frame(page, bg=BG)
        row.pack(fill="both", expand=True, padx=24, pady=20)

        card = self._card(row)
        card.pack(side="left", fill="y", padx=(0, 16))
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(padx=28, pady=24)

        tk.Label(inner, text="Версия", font=("Onest", 10), bg=PANEL, fg=MUTED).pack(anchor="w")
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("RV.TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT,
                        arrowcolor=ACCENT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        selectbackground=PANEL, selectforeground=TEXT, font=("Onest", 11))
        self.version_var = tk.StringVar(value=self.config.data["version"])
        versions = ["Phobia", "latest_release", "latest_snapshot"]
        if self.config.data["version"] not in versions:
            versions.append(self.config.data["version"])
        self.version_combo = ttk.Combobox(inner, textvariable=self.version_var, values=versions,
                                          state="readonly", style="RV.TCombobox", width=34)
        self.version_combo.pack(fill="x", pady=(4, 24))

        self.play_btn = self._btn(inner, "Играть", self.on_play, kind="primary",
                                  font=("Onest", 18, "bold"), padx=70, pady=16)
        self.play_btn.pack(pady=(0, 20))

        self.status_var = tk.StringVar(value="Готов к запуску")
        tk.Label(inner, textvariable=self.status_var, font=("Onest", 10), bg=PANEL, fg=MUTED).pack(anchor="w")

        right = self._card(row)
        right.pack(side="left", fill="both", expand=True)
        head = tk.Frame(right, bg=PANEL)
        head.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(head, text="Журнал", font=("Onest", 11, "bold"), bg=PANEL, fg=TEXT).pack(side="left")
        self.log_text = tk.Text(right, wrap="word", bg=PANEL, fg=TEXT2, insertbackground=TEXT,
                                relief="flat", bd=0, padx=14, pady=8, font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _build_news_page(self):
        page = tk.Frame(self.content, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages["news"] = page

        card = self._card(page)
        card.pack(fill="both", expand=True, padx=24, pady=20)
        tk.Label(card, text="Новости", font=("Onest", 13, "bold"), bg=PANEL, fg=TEXT).pack(
            anchor="w", padx=18, pady=(14, 4))
        self.news_text = tk.Text(card, wrap="word", bg=PANEL, fg=TEXT2, insertbackground=TEXT,
                                 relief="flat", bd=0, padx=18, pady=8, font=("Onest", 11))
        self.news_text.pack(fill="both", expand=True, padx=8, pady=(0, 10))

    def _build_account_page(self):
        page = tk.Frame(self.content, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages["account"] = page

        card = self._card(page)
        card.pack(side="left", fill="y", padx=24, pady=20)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(padx=28, pady=24)

        tk.Label(inner, text="Аккаунт", font=("Onest", 13, "bold"), bg=PANEL, fg=TEXT).pack(anchor="w", pady=(0, 16))

        self.user_var = tk.StringVar()
        self.pass_var = tk.StringVar()

        tk.Label(inner, text="Имя пользователя", font=("Onest", 10), bg=PANEL, fg=MUTED).pack(anchor="w")
        self._entry(inner, self.user_var, width=34).pack(fill="x", pady=(4, 12))
        tk.Label(inner, text="Пароль", font=("Onest", 10), bg=PANEL, fg=MUTED).pack(anchor="w")
        self._entry(inner, self.pass_var, show="*", width=34).pack(fill="x", pady=(4, 16))

        btns = tk.Frame(inner, bg=PANEL)
        btns.pack(fill="x")
        self._btn(btns, "Войти", self.on_login, kind="primary", padx=18).pack(side="left", padx=(0, 8))
        self._btn(btns, "Регистрация", self.on_register, padx=14).pack(side="left", padx=4)
        self._btn(btns, "Выйти", self.on_logout, padx=18).pack(side="left", padx=4)

        self.account_status = tk.StringVar(value="Не авторизован")
        tk.Label(inner, textvariable=self.account_status, font=("Onest", 10), bg=PANEL, fg=GREEN).pack(
            anchor="w", pady=(16, 0))

    def _build_settings_page(self):
        page = tk.Frame(self.content, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages["settings"] = page

        card = self._card(page)
        card.pack(side="left", fill="both", expand=True, padx=24, pady=20)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=28, pady=24)

        tk.Label(inner, text="Настройки", font=("Onest", 13, "bold"), bg=PANEL, fg=TEXT).pack(anchor="w", pady=(0, 16))

        self.java_var = tk.StringVar(value=self.config.data["java_path"])
        self.ram_var = tk.StringVar(value=str(self.config.data["ram"]))
        self.game_dir_var = tk.StringVar(value=self.config.data["game_dir"])
        self.news_var = tk.StringVar(value=self.config.data["news_url"])
        self.custom_version_var = tk.StringVar()

        rows = [
            ("Путь к Java (пусто = авто)", self.java_var, "Обзор", self.on_browse_java),
            ("Память (МБ)", self.ram_var, None, None),
            ("Папка игры", self.game_dir_var, "Обзор", self.on_browse_dir),
            ("URL новостей", self.news_var, None, None),
            ("Своя версия (id, напр. 1.8.9)", self.custom_version_var, None, None),
        ]
        for label, var, btext, bcmd in rows:
            tk.Label(inner, text=label, font=("Onest", 10), bg=PANEL, fg=MUTED).pack(anchor="w", pady=(4, 4))
            box = tk.Frame(inner, bg=PANEL)
            box.pack(fill="x", pady=(0, 12))
            self._entry(box, var, width=40).pack(side="left", fill="x", expand=True)
            if btext:
                self._btn(box, btext, bcmd, padx=12).pack(side="left", padx=(8, 0))

        self._btn(inner, "Сохранить", self.on_save_settings, kind="primary", padx=24).pack(anchor="w")

        tk.Label(inner,
                 text="Для игры в одиночном режиме вход не обязателен.\n"
                      "Авторизация на серверах с онлайн-режимом требует аккаунт Microsoft.",
                 font=("Onest", 9), bg=PANEL, fg=MUTED, justify="left").pack(anchor="w", pady=(16, 0))

    def _refresh_account_pill(self):
        if self.account:
            self.account_pill.config(text=f"● {self.account['username']}", fg=GREEN)
        else:
            self.account_pill.config(text="Не авторизован", fg=MUTED)

    def _load_news(self):
        url = self.config.data["news_url"]
        if url:
            def work():
                try:
                    text = NewsFeed(url).fetch()
                except Exception as e:
                    text = f"Ошибка загрузки новостей: {e}"
                self.msg_queue.put(("news", text))
            threading.Thread(target=work, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "news":
                    self.news_text.delete("1.0", tk.END)
                    self.news_text.insert("1.0", payload)
                elif kind == "done":
                    self.play_btn.config(state="normal", text="Играть")
                    self._log(payload)
                elif kind == "error":
                    self.play_btn.config(state="normal", text="Играть")
                    self._log(payload)
                    messagebox.showerror("Ошибка", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _log(self, text):
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)

    def on_browse_java(self):
        path = filedialog.askopenfilename(title="Выберите java.exe")
        if path:
            self.java_var.set(path)

    def on_browse_dir(self):
        path = filedialog.askdirectory(title="Выберите папку игры")
        if path:
            self.game_dir_var.set(path)

    def on_save_settings(self):
        try:
            self.config.data["java_path"] = self.java_var.get().strip()
            self.config.data["ram"] = int(self.ram_var.get().strip() or 2048)
            self.config.data["game_dir"] = self.game_dir_var.get().strip()
            self.config.data["news_url"] = self.news_var.get().strip()
            if self.custom_version_var.get().strip():
                self.config.data["version"] = self.custom_version_var.get().strip()
            self.config.save()
            messagebox.showinfo("Настройки", "Сохранено")
        except ValueError:
            messagebox.showerror("Ошибка", "Память должна быть числом")

    def on_login(self):
        try:
            self.account = self.db.login(self.user_var.get(), self.pass_var.get())
            self.config.data["last_user"] = self.account["username"]
            self.config.save()
            self.account_status.set(f"Авторизован: {self.account['username']}")
            self._refresh_account_pill()
            self._log(f"Вход выполнен: {self.account['username']}")
        except ValueError as e:
            messagebox.showerror("Вход", str(e))

    def on_register(self):
        try:
            self.db.register(self.user_var.get(), self.pass_var.get())
            messagebox.showinfo("Регистрация", "Аккаунт создан, теперь войдите")
        except ValueError as e:
            messagebox.showerror("Регистрация", str(e))

    def on_logout(self):
        self.account = None
        self.account_status.set("Не авторизован")
        self._refresh_account_pill()
        self.pass_var.set("")

    def on_play(self):
        if self.game_process and self.game_process.poll() is None:
            messagebox.showwarning("Внимание", "Игра уже запущена")
            return
        self.play_btn.config(state="disabled", text="Запуск...")
        self.config.data["version"] = self.version_var.get()
        self.config.save()
        threading.Thread(target=self._play_worker, daemon=True).start()

    def _tail_process(self):
        try:
            if self.game_process and self.game_process.stdout:
                for line in self.game_process.stdout:
                    self.msg_queue.put(("log", line.rstrip()))
        except Exception:
            pass
        self.msg_queue.put(("done", "Игра завершена"))

    def _play_worker(self):
        try:
            def report(msg):
                self.msg_queue.put(("log", msg))
                self.msg_queue.put(("status", msg[:60]))

            if self.config.data["version"] == "Phobia":
                phobia_dir = Path(r"C:\phobiadlc_fun")
                jar = phobia_dir / "client" / "client.jar"
                if not jar.exists():
                    raise RuntimeError(f"Клиент не найден: {jar}")
                java = None
                for j in phobia_dir.rglob("java.exe"):
                    java = str(j)
                    break
                if not java:
                    java = self.config.data["java_path"].strip() or "java"
                ram = self.config.data["ram"]
                cfg = phobia_dir / "loader.cfg"
                if cfg.exists():
                    try:
                        ram = int(cfg.read_text(encoding="utf-8", errors="ignore").strip() or ram)
                    except ValueError:
                        pass
                report("Запуск Phobia...")
                cmd = [java, "-Xverify:none", f"-Xmx{ram}M", "-jar", str(jar)]
                try:
                    self.game_process = subprocess.Popen(
                        cmd, cwd=str(phobia_dir),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    )
                except OSError:
                    self.game_process = subprocess.Popen(
                        f'"{java}" -Xverify:none -Xmx{ram}M -jar "{jar}"', cwd=str(phobia_dir), shell=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    )
                threading.Thread(target=self._tail_process, daemon=True).start()
                return

            mc = Minecraft(self.config)
            session = requests.Session()

            def report(msg):
                self.msg_queue.put(("log", msg))
                self.msg_queue.put(("status", msg[:60]))

            report("Получение манифеста версий...")
            version_entry = mc.resolve_version(session)
            report(f"Версия: {version_entry['id']}")

            prepared = mc.prepare(version_entry, session, report)

            account = self.account or {"username": "Player" + uuidlib.uuid4().hex[:6],
                                       "uuid": str(uuidlib.uuid4())}
            cmd = mc.build_command(prepared, account)
            report("Запуск Minecraft...")
            report(" ".join(cmd[:8]) + " ...")

            self.game_process = subprocess.Popen(
                cmd,
                cwd=str(mc.base_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            threading.Thread(target=self._tail_process, daemon=True).start()
        except Exception as e:
            self.msg_queue.put(("error", str(e)))


def main():
    load_fonts()
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
