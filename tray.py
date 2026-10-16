"""
ChatGPT2API (Web Edition) - System Tray Companion & Server Manager
- Icon khay he thong (Taskbar System Tray)
- Mo Web Dashboard voi 1 click
- Hien thi va sao chep thong tin ket noi (Base URL, Auth Key, Port)
- Tu dong giam sat va khoi chay Uvicorn backend
"""

import os
import sys
import time
import json
import socket
import threading
import subprocess
import webbrowser
from pathlib import Path

# Tkinter for Info GUI
import tkinter as tk
from tkinter import ttk, messagebox

# Pystray and PIL for System Tray
import pystray
from pystray import MenuItem as item, Menu
from PIL import Image, ImageDraw

APP_DIR = Path(__file__).resolve().parent
PYTHON_EXE = r"D:\chatgpt2api\backend\python\python.exe"
PYTHONW_EXE = r"D:\chatgpt2api\backend\python\pythonw.exe"
if not os.path.exists(PYTHON_EXE):
    PYTHON_EXE = sys.executable
if not os.path.exists(PYTHONW_EXE):
    PYTHONW_EXE = sys.executable

DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = APP_DIR / ".env"
CONFIG_FILE = APP_DIR / "config.json"
FAVICON_FILE = APP_DIR / "web_dist" / "favicon.ico"
LOG_FILE = APP_DIR / "service.log"
LOCK_FILE = DATA_DIR / "tray.lock"

# Global state
server_process = None
server_running = False
tray_icon = None
should_exit = False


def load_config():
    auth_key = "sk-yeZIi4tv0OUtk3ef0ygLZL6PWDSCXdO5"
    port = 13000
    host = "127.0.0.1"

    if ENV_FILE.exists():
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k == "CHATGPT2API_AUTH_KEY" and v:
                            auth_key = v
                        elif k == "CHATGPT2API_PORT" and v:
                            try:
                                port = int(v)
                            except ValueError:
                                pass
                        elif k == "CHATGPT2API_HOST" and v:
                            host = v
        except Exception:
            pass
    elif CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "auth-key" in data:
                    auth_key = data["auth-key"]
                if "port" in data:
                    port = int(data["port"])
        except Exception:
            pass
    return host, port, auth_key


def is_port_listening(port, host="127.0.0.1"):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.6)
            s.connect((host, port))
            return True
    except Exception:
        return False


def copy_to_clipboard(text: str, notify_title="ChatGPT2API"):
    try:
        r = tk.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
    except Exception:
        try:
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
            p.communicate(text.encode("utf-8"))
        except Exception:
            pass

    if tray_icon:
        try:
            tray_icon.notify(f"Đã sao chép:\n{text}", notify_title)
        except Exception:
            pass


def get_icon_image():
    if FAVICON_FILE.exists():
        try:
            return Image.open(str(FAVICON_FILE))
        except Exception:
            pass
    # Fallback to green AI circle badge
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill="#10b981", outline="#059669", width=2)
    draw.ellipse((20, 20, 44, 44), fill="#ffffff")
    return img


def start_backend():
    global server_process, server_running
    host, port, auth_key = load_config()

    if is_port_listening(port, host):
        server_running = True
        return

    cmd = [
        PYTHON_EXE,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]

    env = os.environ.copy()
    env["CHATGPT2API_AUTH_KEY"] = auth_key
    env["CHATGPT2API_PORT"] = str(port)
    env["CHATGPT2API_HOST"] = host
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    creation_flags = 0x08000000  # CREATE_NO_WINDOW
    log_fp = open(LOG_FILE, "a", encoding="utf-8")

    server_process = subprocess.Popen(
        cmd,
        cwd=str(APP_DIR),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        creationflags=creation_flags,
    )

    # Wait up to 5s for port to listen
    for _ in range(25):
        if is_port_listening(port, host):
            server_running = True
            break
        time.sleep(0.2)


def stop_backend():
    global server_process, server_running
    if server_process and server_process.poll() is None:
        try:
            server_process.terminate()
            server_process.wait(timeout=3)
        except Exception:
            try:
                server_process.kill()
            except Exception:
                pass
    server_process = None
    server_running = False


def restart_backend():
    stop_backend()
    time.sleep(1)
    start_backend()
    if tray_icon:
        host, port, _ = load_config()
        tray_icon.notify(f"Đã khởi động lại server trên cổng {port}", "ChatGPT2API")


def show_info_window():
    # Run info window in separate process to prevent any UI blocking
    subprocess.Popen([PYTHONW_EXE, str(APP_DIR / "tray.py"), "--info"], cwd=str(APP_DIR))


def render_info_gui():
    host, port, auth_key = load_config()
    base_url = f"http://{host}:{port}/v1"
    dashboard_url = f"http://{host}:{port}/"
    model_id = "gpt-6-astra-wm"

    root = tk.Tk()
    root.title("ChatGPT2API (Web Edition) - Thông Tin Kết Nối")
    root.geometry("540x440")
    root.resizable(False, False)

    # Center on screen
    root.update_idletasks()
    x = (root.winfo_screenwidth() - 540) // 2
    y = (root.winfo_screenheight() - 440) // 2
    root.geometry(f"+{x}+{y}")

    if FAVICON_FILE.exists():
        try:
            root.iconbitmap(str(FAVICON_FILE))
        except Exception:
            pass

    bg_color = "#f8fafc"
    card_bg = "#ffffff"
    border_color = "#e2e8f0"
    primary_color = "#2563eb"
    text_dark = "#0f172a"
    text_muted = "#64748b"

    root.configure(bg=bg_color)

    header_frame = tk.Frame(root, bg=bg_color, pady=10)
    header_frame.pack(fill="x", padx=20)

    title_lbl = tk.Label(header_frame, text="ChatGPT2API (Web Edition)", font=("Segoe UI", 16, "bold"), fg=text_dark, bg=bg_color)
    title_lbl.pack(anchor="w")
    sub_lbl = tk.Label(header_frame, text=f"Cổng cấp API OpenAI ChatGPT Plus | Port: {port}", font=("Segoe UI", 9), fg=text_muted, bg=bg_color)
    sub_lbl.pack(anchor="w")

    card = tk.Frame(root, bg=card_bg, highlightbackground=border_color, highlightthickness=1, padx=16, pady=14)
    card.pack(fill="both", expand=True, padx=20, pady=5)

    def on_copy(val, btn):
        root.clipboard_clear()
        root.clipboard_append(val)
        root.update()
        old_txt = btn.cget("text")
        btn.config(text="Đã copy!")
        root.after(1500, lambda: btn.config(text=old_txt))

    rows = [
        ("Base URL:", base_url, True),
        ("Mã kết nối (Auth Key):", auth_key, True),
        ("Model khuyên dùng:", model_id, True),
        ("Web Dashboard:", dashboard_url, False),
    ]

    for label_text, val_text, has_copy in rows:
        row_frame = tk.Frame(card, bg=card_bg, pady=6)
        row_frame.pack(fill="x")

        lbl = tk.Label(row_frame, text=label_text, font=("Segoe UI", 9, "bold"), fg=text_dark, bg=card_bg, width=22, anchor="w")
        lbl.pack(side="left")

        entry = tk.Entry(row_frame, font=("Consolas", 9), fg="#1e293b", bg="#f1f5f9", relief="flat", highlightbackground="#cbd5e1", highlightthickness=1)
        entry.insert(0, val_text)
        entry.config(state="readonly")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=3)

        if has_copy:
            btn = tk.Button(row_frame, text="Copy", font=("Segoe UI", 8, "bold"), bg="#e2e8f0", fg="#334155", relief="flat", padx=10, cursor="hand2")
            btn.config(command=lambda v=val_text, b=btn: on_copy(v, b))
            btn.pack(side="right")
        else:
            btn = tk.Button(row_frame, text="Mở Web", font=("Segoe UI", 8, "bold"), bg=primary_color, fg="white", relief="flat", padx=8, cursor="hand2", command=lambda u=val_text: webbrowser.open(u))
            btn.pack(side="right")

    test_frame = tk.Frame(card, bg="#f8fafc", highlightbackground="#e2e8f0", highlightthickness=1, padx=10, pady=8)
    test_frame.pack(fill="x", pady=(12, 0))

    status_lbl = tk.Label(test_frame, text="Trạng thái: Đang kiểm tra...", font=("Segoe UI", 9), fg="#475569", bg="#f8fafc")
    status_lbl.pack(side="left")

    def ping_test():
        status_lbl.config(text="Đang kết nối...", fg="#475569")
        root.update()
        try:
            import urllib.request
            req = urllib.request.Request(f"http://{host}:{port}/v1/models", headers={"Authorization": f"Bearer {auth_key}"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    status_lbl.config(text=f"🟢 Dịch vụ hoạt động tốt trên cổng {port}!", fg="#15803d")
                    return
        except Exception as e:
            status_lbl.config(text=f"🔴 Chưa kết nối được: {e}", fg="#b91c1c")

    ping_btn = tk.Button(test_frame, text="Kiểm tra kết nối", font=("Segoe UI", 8, "bold"), bg="#3b82f6", fg="white", relief="flat", padx=10, cursor="hand2", command=ping_test)
    ping_btn.pack(side="right")

    btn_frame = tk.Frame(root, bg=bg_color, pady=12)
    btn_frame.pack(fill="x", padx=20)

    open_dash_btn = tk.Button(btn_frame, text="🌐 Mở Web Dashboard", font=("Segoe UI", 10, "bold"), bg=primary_color, fg="white", relief="flat", padx=16, pady=6, cursor="hand2", command=lambda: webbrowser.open(dashboard_url))
    open_dash_btn.pack(side="left")

    close_btn = tk.Button(btn_frame, text="Đóng", font=("Segoe UI", 10), bg="#cbd5e1", fg="#334155", relief="flat", padx=16, pady=6, cursor="hand2", command=root.destroy)
    close_btn.pack(side="right")

    root.after(200, ping_test)
    root.mainloop()


def open_dashboard():
    host, port, _ = load_config()
    webbrowser.open(f"http://{host}:{port}/")


def copy_base_url():
    host, port, _ = load_config()
    copy_to_clipboard(f"http://{host}:{port}/v1", "Base URL")


def copy_auth_key():
    _, _, auth_key = load_config()
    copy_to_clipboard(auth_key, "Auth Key")


def open_folder():
    try:
        os.startfile(str(APP_DIR))
    except Exception:
        subprocess.Popen(["explorer", str(APP_DIR)])


def exit_app(icon):
    global should_exit
    should_exit = True
    stop_backend()
    if icon:
        icon.stop()


def monitor_loop():
    global server_running
    host, port, _ = load_config()
    while not should_exit:
        alive = is_port_listening(port, host)
        server_running = alive
        if not alive and not should_exit:
            start_backend()
        if tray_icon:
            st = "🟢 Đang chạy" if server_running else "🔴 Đã dừng"
            tray_icon.title = f"ChatGPT2API (Web Edition) - {st} (Port {port})"
        time.sleep(4)


def build_menu():
    host, port, _ = load_config()
    return Menu(
        item(f"ChatGPT2API - Port {port}", show_info_window, default=True),
        item("🌐 Mở Web Dashboard", open_dashboard),
        item("🔑 Xem mã kết nối & Port...", show_info_window),
        Menu.SEPARATOR,
        item(f"📋 Copy Base URL (Port {port})", copy_base_url),
        item("📋 Copy Auth Key", copy_auth_key),
        Menu.SEPARATOR,
        item("🔄 Khởi động lại Server", restart_backend),
        item("📁 Mở thư mục ứng dụng", open_folder),
        Menu.SEPARATOR,
        item("❌ Thoát", exit_app),
    )


def main():
    global tray_icon

    if "--info" in sys.argv:
        render_info_gui()
        return

    # Single instance lock
    try:
        lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except OSError:
        # Already running! Open info GUI instead
        render_info_gui()
        return

    # Start backend server
    start_backend()

    # Start background monitor thread
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    # Start Tray Icon
    host, port, _ = load_config()
    tray_icon = pystray.Icon(
        name="ChatGPT2API",
        icon=get_icon_image(),
        title=f"ChatGPT2API (Web Edition) - Đang chạy (Port {port})",
        menu=build_menu(),
    )

    try:
        tray_icon.run()
    finally:
        try:
            os.close(lock_fd)
            if LOCK_FILE.exists():
                LOCK_FILE.unlink()
        except Exception:
            pass
        stop_backend()


if __name__ == "__main__":
    main()
