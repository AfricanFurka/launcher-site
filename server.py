import os
import socket
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8000
os.chdir(BASE_DIR)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ip = local_ip()
    print("=" * 56, flush=True)
    print(" Сайт лаунчера запущен!", flush=True)
    print(f" Этот ПК:      http://localhost:{PORT}", flush=True)
    print(f" По сети:      http://{ip}:{PORT}", flush=True)
    print(" Другие устройства: открыть ссылку выше в браузере", flush=True)
    print(" Остановка: Ctrl+C", flush=True)
    print("=" * 56, flush=True)
    threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        server.server_close()


if __name__ == "__main__":
    main()
