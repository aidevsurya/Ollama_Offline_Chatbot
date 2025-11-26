"""
Chatbot V2 — Clean, fixed, and ready

Features:
- Local Ollama client (default http://localhost:11434)
- Vision: embeds images as base64 in "images" (for vision-capable models)
- Enter => send, Shift+Enter => newline (SPACE won't add newline)
- Paperclip icon for image attach, inline preview (Pillow optional)
- Per-model in-memory history during the program run
- Streaming assistant responses update only the last assistant block
"""

import os
import json
import base64
import hashlib
import threading
import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

# Optional Pillow for inline image previews
try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


# -------------------- Utilities --------------------
def ensure_str(x):
    if isinstance(x, (bytes, bytearray)):
        return x.decode("utf-8", errors="replace")
    return str(x)


def extract_text_from_obj(obj):
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return None

    for k in ("response", "text", "generated_text", "output_text", "content"):
        v = obj.get(k)
        if isinstance(v, str):
            return v

    msg = obj.get("message") or obj.get("msg")
    if isinstance(msg, dict):
        for k in ("content", "text"):
            if k in msg and isinstance(msg[k], str):
                return msg[k]
        if "content" in msg and isinstance(msg["content"], list):
            parts = [p.get("text") or p.get("content") for p in msg["content"] if isinstance(p, dict)]
            parts = [p for p in parts if isinstance(p, str)]
            if parts:
                return "".join(parts)

    delta = obj.get("delta")
    if isinstance(delta, dict):
        if "content" in delta and isinstance(delta["content"], str):
            return delta["content"]
        if "text" in delta and isinstance(delta["text"], str):
            return delta["text"]

    choices = obj.get("choices")
    if isinstance(choices, list):
        texts = []
        for ch in choices:
            if isinstance(ch, dict):
                if "text" in ch and isinstance(ch["text"], str):
                    texts.append(ch["text"])
                    continue
                cmsg = ch.get("message")
                if isinstance(cmsg, dict) and "content" in cmsg and isinstance(cmsg["content"], str):
                    texts.append(cmsg["content"])
                    continue
                cdelta = ch.get("delta")
                if isinstance(cdelta, dict) and "content" in cdelta and isinstance(cdelta["content"], str):
                    texts.append(cdelta["content"])
                    continue
        if texts:
            return "".join(texts)

    return None


# -------------------- Ollama client --------------------
class OllamaClient:
    def __init__(self, base_url=BASE_URL, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def list_models(self):
        url = f"{self.base_url}/api/tags"
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "tags" in data:
            return data["tags"]
        if isinstance(data, list):
            return data
        return data

    def upload_blob(self, file_path):
        with open(file_path, "rb") as f:
            data = f.read()
        sha = hashlib.sha256(data).hexdigest()
        digest = f"sha256:{sha}"
        url = f"{self.base_url}/api/blobs/{digest}"
        headers = {"Content-Type": "application/octet-stream"}
        r = self.session.put(url, data=data, headers=headers, timeout=self.timeout)
        if r.status_code not in (200, 201):
            r = self.session.post(url, data=data, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return digest

    def chat_stream(self, model, messages, params=None):
        url = f"{self.base_url}/api/chat"
        payload = {"model": model, "messages": messages}
        if params:
            payload.update(params)
        with self.session.post(url, json=payload, stream=True, timeout=self.timeout) as r:
            r.raise_for_status()
            for raw in r.iter_lines(decode_unicode=False):
                if not raw:
                    continue
                line = ensure_str(raw)
                try:
                    obj = json.loads(line)
                    extracted = extract_text_from_obj(obj)
                    if extracted is not None:
                        yield ensure_str(extracted)
                    else:
                        yield line
                except json.JSONDecodeError:
                    yield line


# -------------------- ExpandingEntry --------------------
class ExpandingEntry(tk.Text):
    def __init__(self, master, send_callback, min_lines=1, max_lines=6, **kwargs):
        kwargs.setdefault("wrap", "none")
        super().__init__(master, height=min_lines, **kwargs)
        self.send_callback = send_callback
        self.min_lines = min_lines
        self.max_lines = max_lines

        try: self.unbind("<Return>")
        except: pass
        try: self.unbind("<KP_Enter>")
        except: pass

        self.bind("<KeyPress-Return>", self._on_return)
        self.bind("<KeyRelease>", self._on_key_release)
        self.bind("<<Paste>>", lambda e: self.after(1, self._on_key_release))

        self.config(borderwidth=0, relief="flat", undo=True)

    def _on_return(self, event):
        try:
            shift_held = (event.state & 0x0001) != 0
        except:
            shift_held = False

        if shift_held:
            self.insert("insert", "\n")
            self._on_key_release()
            return "break"

        content = self.get("1.0", "end-1c")
        if content.strip():
            self.send_callback()
        return "break"

    def _on_key_release(self, event=None):
        content = self.get("1.0", "end-1c")
        lines = content.count("\n") + 1 if content.strip() else 1
        new_h = max(self.min_lines, min(self.max_lines, lines))
        if int(self.cget("height")) != new_h:
            self.configure(height=new_h)

    def get_text(self):
        return self.get("1.0", "end-1c").strip()

    def clear(self):
        self.delete("1.0", "end")
        self.configure(height=self.min_lines)


# -------------------- Chat App --------------------
class ChatApp(tk.Tk):

    CLEAR_TOKEN = "§"
    START_TOKEN = "▶"
    STOP_TOKEN = "■"

    def __init__(self, client: OllamaClient):
        super().__init__()
        self.client = client
        self.title("Chatbot V2 — Ollama Local")
        self.geometry("1100x720")
        self.minsize(900, 600)

        self.current_model = None
        self.attached_image_path = None
        self.image_refs = []
        self.dark = True
        self.last_assistant_index = None
        self.model_running = True
        self.histories = {}

        self._setup_styles()
        self.create_widgets()
        self.refresh_models()

    def _setup_styles(self):
        self.style = ttk.Style(self)
        self.dark_colors = {
            "bg": "#0f1115",
            "panel": "#111319",
            "fg": "#e6eef6",
            "muted": "#9aa4b2",
            "accent": "#6ccfff",
            "input_bg": "#0b1220",
        }
        self.light_colors = {
            "bg": "#f4f7fb",
            "panel": "#ffffff",
            "fg": "#111827",
            "muted": "#6b7280",
            "accent": "#0ea5e9",
            "input_bg": "#ffffff",
        }
        self._apply_theme()

    def _apply_theme(self):
        c = self.dark_colors if self.dark else self.light_colors
        self.configure(bg=c["bg"])
        self.style.configure("Card.TFrame", background=c["panel"])
        self.style.configure("Header.TLabel", background=c["panel"],
                             foreground=c["fg"], font=("Segoe UI", 11, "bold"))

    def create_widgets(self):

        c = self.dark_colors if self.dark else self.light_colors

        # LEFT SIDEBAR
        left = ttk.Frame(self, style="Card.TFrame", width=260)
        left.grid(row=0, column=0, sticky="nsw", padx=(12, 8), pady=12)
        left.grid_propagate(False)

        header = ttk.Label(left, text="Models", style="Header.TLabel")
        header.pack(anchor="w", pady=(8, 6), padx=12)

        self.model_listbox = tk.Listbox(left, height=30, bd=0,
                                        highlightthickness=0)
        self.model_listbox.pack(fill="both", expand=True,
                                padx=12, pady=(0, 8))
        self.model_listbox.bind("<<ListboxSelect>>", self.on_model_select)

        ttk.Button(left, text="Refresh",
                   command=self.refresh_models).pack(
                       pady=(0, 8), padx=12, anchor="w"
                   )

        # RIGHT MAIN AREA
        right = ttk.Frame(self, style="Card.TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 12), pady=12)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        # TOP BAR
        topbar = ttk.Frame(right, style="Card.TFrame")
        topbar.grid(row=0, column=0, sticky="ew", padx=12, pady=8)
        topbar.columnconfigure(0, weight=1)

        self.model_label = ttk.Label(topbar, text="Model: (none)",
                                     style="Header.TLabel")
        self.model_label.grid(row=0, column=0, sticky="w")

        # >>> SPECIAL BUTTONS ON TOP BAR <<<
        top_controls = ttk.Frame(topbar, style="Card.TFrame")
        top_controls.grid(row=0, column=1, sticky="e", padx=6)

        def _send_special(token):
            try:
                self.input_box.delete("1.0", "end")
                self.input_box.insert("1.0", token)
                self.on_send()
            except Exception:
                pass

        btn_clear = ttk.Button(
            top_controls, text=self.CLEAR_TOKEN, width=3,
            command=lambda: _send_special(self.CLEAR_TOKEN)
        )
        btn_clear.pack(side="left", padx=4)

        btn_start = ttk.Button(
            top_controls, text=self.START_TOKEN, width=3,
            command=lambda: _send_special(self.START_TOKEN)
        )
        btn_start.pack(side="left", padx=4)

        btn_stop = ttk.Button(
            top_controls, text=self.STOP_TOKEN, width=3,
            command=lambda: _send_special(self.STOP_TOKEN)
        )
        btn_stop.pack(side="left", padx=4)
        # <<< END SPECIAL BUTTONS >>>

        # CHAT HISTORY BOX
        self.history = ScrolledText(right, state="disabled", wrap="word",
                                    bd=0, relief="flat")
        self.history.grid(row=1, column=0, sticky="nsew",
                          padx=12, pady=(0, 8))
        self.history.tag_configure("user", foreground=c["accent"],
                                   font=("Segoe UI", 10, "bold"))
        self.history.tag_configure("assistant", foreground=c["fg"],
                                   font=("Segoe UI", 10))

        # INPUT AREA
        input_container = ttk.Frame(right, style="Card.TFrame")
        input_container.grid(row=2, column=0, sticky="ew",
                             padx=12, pady=(6, 12))
        input_container.columnconfigure(0, weight=1)

        self.input_box = ExpandingEntry(
            input_container,
            send_callback=self.on_send,
            min_lines=1, max_lines=6,
            font=("Segoe UI", 10),
            padx=6, pady=6
        )
        self.input_box.grid(row=0, column=0, sticky="ew",
                            padx=(6, 6), pady=6)

        btns = ttk.Frame(input_container, style="Card.TFrame")
        btns.grid(row=0, column=1, sticky="e", padx=(6, 0))

        attach_label = tk.Label(btns, text="📎", cursor="hand2",
                                font=("Segoe UI Emoji", 14))
        attach_label.pack(side="left", padx=6)
        attach_label.bind("<Button-1>", lambda e: self.attach_image())

        def _on_hover_in(e):
            try: attach_label.config(font=("Segoe UI Emoji", 15))
            except: pass

        def _on_hover_out(e):
            try: attach_label.config(font=("Segoe UI Emoji", 14))
            except: pass

        attach_label.bind("<Enter>", _on_hover_in)
        attach_label.bind("<Leave>", _on_hover_out)

        send_btn = ttk.Button(btns, text="Send", command=self.on_send)
        send_btn.pack(side="left", padx=4)

        self._apply_theme_to_widgets()

    def _apply_theme_to_widgets(self):
        c = self.dark_colors if self.dark else self.light_colors
        self.model_listbox.configure(
            background=c["panel"], foreground=c["fg"],
            selectbackground=c["accent"], selectforeground=c["bg"]
        )
        self.history.configure(
            background=c["panel"], foreground=c["fg"],
            insertbackground=c["fg"]
        )
        self.input_box.configure(
            background=c["input_bg"], foreground=c["fg"],
            insertbackground=c["fg"]
        )

    # -------------------- History helpers --------------------
    def _insert_user_text(self, text):
        txt = ensure_str(text)
        self.history.configure(state="normal")
        existing = self.history.get("1.0", "end-1c")
        sep = "\n\n" if existing.strip() else ""
        self.history.insert("end", sep + "User: " + txt + "\n", "user")
        self.history.see("end")
        self.history.configure(state="disabled")

    def _insert_assistant_text(self, text):
        txt = ensure_str(text)
        self.history.configure(state="normal")
        existing = self.history.get("1.0", "end-1c")
        sep = "\n\n" if existing.strip() else ""
        start_index = self.history.index("end-1c")
        self.history.insert("end", sep + "Assistant: " + txt + "\n",
                            "assistant")
        self.last_assistant_index = start_index
        self.history.see("end")
        self.history.configure(state="disabled")

    def append_user(self, text, record=True):
        txt = ensure_str(text)
        if self.current_model and record:
            self.histories.setdefault(self.current_model,
                                      []).append({"role": "user",
                                                  "text": txt})
        self._insert_user_text(txt)

    def append_assistant(self, text, placeholder=False, record=True):
        txt = ensure_str(text)
        if self.current_model and (record and not placeholder):
            self.histories.setdefault(self.current_model,
                                      []).append({"role": "assistant",
                                                  "text": txt})
        if placeholder:
            self.history.configure(state="normal")
            existing = self.history.get("1.0", "end-1c")
            sep = "\n\n" if existing.strip() else ""
            start_index = self.history.index("end-1c")
            self.history.insert("end",
                                sep + "Assistant: " + txt + "\n",
                                "assistant")
            self.last_assistant_index = start_index
            self.history.see("end")
            self.history.configure(state="disabled")
        else:
            self._insert_assistant_text(txt)

    def append_image(self, pil_image, caption=None, record=True):
        if not PIL_AVAILABLE:
            return
        max_w = 420
        w, h = pil_image.size
        if w > max_w:
            ratio = max_w / w
            pil_image = pil_image.resize(
                (int(w * ratio), int(h * ratio)),
                Image.LANCZOS
            )
        photo = ImageTk.PhotoImage(pil_image)
        self.image_refs.append(photo)
        self.history.configure(state="normal")
        existing = self.history.get("1.0", "end-1c")
        sep = "\n\n" if existing.strip() else ""
        self.history.insert("end", sep)
        self.history.image_create("end", image=photo)
        if caption:
            self.history.insert("end", "\n" + caption + "\n")
        else:
            self.history.insert("end", "\n")
        if self.current_model and record and \
                getattr(self, "attached_image_path", None):
            self.histories.setdefault(self.current_model,
                                      []).append(
                {"role": "image",
                 "path": self.attached_image_path,
                 "caption": caption}
            )
        self.history.see("end")
        self.history.configure(state="disabled")

    # -------------------- Model / image actions --------------------
    def refresh_models(self):
        def worker():
            try:
                models = self.client.list_models()
            except Exception as exc:
                self.after(
                    0,
                    lambda exc=exc: messagebox.showerror(
                        "Error", "Failed to list models:\n" + str(exc)
                    )
                )
                return

            model_names = []
            if isinstance(models, dict):
                for v in models.values():
                    if isinstance(v, list):
                        model_names = [
                            m if isinstance(m, str)
                            else (m.get("name")
                                  if isinstance(m, dict)
                                  else str(m))
                            for m in v
                        ]
                        break
            elif isinstance(models, list):
                model_names = [
                    m if isinstance(m, str)
                    else (m.get("name")
                          if isinstance(m, dict)
                          else str(m))
                    for m in models
                ]
            else:
                model_names = [str(models)]

            def update_list():
                self.model_listbox.delete(0, "end")
                for m in model_names:
                    self.model_listbox.insert("end", ensure_str(m))

            self.after(0, update_list)

        threading.Thread(target=worker, daemon=True).start()

    def on_model_select(self, _event=None):
        sel = self.model_listbox.curselection()
        if not sel:
            return
        model = self.model_listbox.get(sel[0])
        self.current_model = model
        self.model_label.config(text=f"Model: {model}")

        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        self.history.configure(state="disabled")

        entries = self.histories.get(model, [])
        for entry in entries:
            role = entry.get("role")
            if role == "user":
                self._insert_user_text(entry.get("text", ""))
            elif role == "assistant":
                self._insert_assistant_text(entry.get("text", ""))
            elif role == "image":
                path = entry.get("path")
                caption = entry.get("caption")
                if PIL_AVAILABLE and path:
                    try:
                        pil_img = Image.open(path).convert("RGBA")
                        self.append_image(pil_img, caption=caption,
                                          record=False)
                    except:
                        self._insert_user_text(caption or "Image")

        self.last_assistant_index = None

    def attach_image(self):
        path = filedialog.askopenfilename(
            title="Select image",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.gif"),
                ("All files", "*.*")
            ]
        )
        if not path:
            return
        self.attached_image_path = path
        if PIL_AVAILABLE:
            try:
                pil_img = Image.open(path).convert("RGBA")
                self.append_image(pil_img, caption="Image attached",
                                  record=True)
            except Exception as exc:
                messagebox.showerror("Image Error", str(exc))
        else:
            if self.current_model:
                self.histories.setdefault(self.current_model,
                                          []).append(
                    {"role": "image",
                     "path": path,
                     "caption": "Image attached"}
                )
            messagebox.showinfo("Attached",
                                "Image selected: " + path)

    # -------------------- Special token handler --------------------
    def handle_special_token(self, token: str):
        if token == self.CLEAR_TOKEN:
            if not self.current_model:
                messagebox.showinfo(
                    "Clear",
                    "No model selected to clear history for."
                )
                return
            self.history.configure(state="normal")
            self.history.delete("1.0", "end")
            self.history.configure(state="disabled")

            if self.current_model in self.histories:
                self.histories[self.current_model] = []
            messagebox.showinfo(
                "Clear",
                f"Cleared history for model: {self.current_model}"
            )
            return

        if token == self.START_TOKEN:
            if self.model_running:
                messagebox.showinfo("Model", "Model already running.")
                return
            self.model_running = True
            messagebox.showinfo("Model", "Model started.")
            return

        if token == self.STOP_TOKEN:
            if not self.model_running:
                messagebox.showinfo("Model", "Model already stopped.")
                return
            self.model_running = False
            messagebox.showinfo(
                "Model",
                "Model stopped. Sending messages disabled until start."
            )
            return

    # -------------------- Sending --------------------
    def on_send(self):
        if not self.current_model:
            messagebox.showwarning(
                "No model",
                "Select a model from the left before sending."
            )
            return

        user_text = self.input_box.get_text()

        if not user_text and not getattr(self, "attached_image_path", None):
            messagebox.showwarning("Empty",
                                   "Write a message or attach an image.")
            return

        # Handle special token
        if user_text in (
                self.CLEAR_TOKEN,
                self.START_TOKEN,
                self.STOP_TOKEN
        ) and not getattr(self, "attached_image_path", None):
            try:
                self.handle_special_token(user_text)
            finally:
                self.input_box.clear()
            return

        # Model stopped → block sending
        if not self.model_running:
            messagebox.showwarning(
                "Model stopped",
                "Model is stopped. Press ▶ to start it."
            )
            return

        system_prompt = {
            "role": "system",
            "content": (
                "You are an assistant that describes images.\n"
                "When a user message contains an attached image, "
                "treat that image as the primary content.\n"
                "Do not mention upload mechanisms.\n"
                "Only describe visible content."
            ),
        }

        user_msg = {"role": "user", "content": user_text}

        if getattr(self, "attached_image_path", None):
            try:
                with open(self.attached_image_path, "rb") as f:
                    img_bytes = f.read()
                encoded = base64.b64encode(img_bytes).decode("utf-8")
                user_msg["images"] = [encoded]
            except Exception as exc:
                messagebox.showerror("Image Error", str(exc))
                return

        messages = [system_prompt, user_msg]

        if user_text:
            self.append_user(user_text, record=True)
        elif getattr(self, "attached_image_path", None):
            self.append_user("Sent an image", record=True)

        self.input_box.clear()
        self.attached_image_path = None

        threading.Thread(
            target=self._stream_response,
            args=(self.current_model, messages),
            daemon=True
        ).start()

    # -------------------- Streaming --------------------
    def _stream_response(self, model, messages):
        try:
            self.after(
                0,
                lambda: self.append_assistant("...", placeholder=True,
                                              record=False)
            )
            acc = ""
            for chunk in self.client.chat_stream(model, messages):
                chunk = ensure_str(chunk)
                acc += chunk

                def update_block(acc_text=acc):
                    try:
                        self.history.configure(state="normal")
                        if self.last_assistant_index:
                            try:
                                self.history.delete(
                                    self.last_assistant_index, "end"
                                )
                            except:
                                all_text = self.history.get(
                                    "1.0", "end-1c"
                                )
                                idx = all_text.rfind("\n\nAssistant:")
                                if idx == -1:
                                    idx = all_text.rfind("Assistant:")
                                if idx != -1:
                                    pre = all_text[:idx]
                                    self.history.delete("1.0", "end")
                                    self.history.insert("end", pre)
                        else:
                            all_text = self.history.get(
                                "1.0", "end-1c"
                            )
                            idx = all_text.rfind("\n\nAssistant:")
                            if idx == -1:
                                idx = all_text.rfind("Assistant:")
                            if idx != -1:
                                pre = all_text[:idx]
                                self.history.delete("1.0", "end")
                                self.history.insert("end", pre)

                        existing = self.history.get("1.0", "end-1c")
                        sep = "\n\n" if existing.strip() else ""
                        start_index = self.history.index("end-1c")

                        self.history.insert(
                            "end",
                            sep + "Assistant: " + acc_text + "\n",
                            "assistant"
                        )
                        self.last_assistant_index = start_index
                        self.history.see("end")
                        self.history.configure(state="disabled")

                    except:
                        try:
                            self.history.configure(state="disabled")
                        except:
                            pass

                self.after(0, update_block)

            final_text = acc.strip()
            if final_text and self.current_model:
                self.histories.setdefault(
                    self.current_model, []
                ).append({"role": "assistant", "text": final_text})

        except Exception as exc:
            self.after(
                0,
                lambda exc=exc: messagebox.showerror(
                    "Error", "Chat failed:\n" + str(exc)
                )
            )


# -------------------- Run --------------------
if __name__ == "__main__":
    client = OllamaClient()
    app = ChatApp(client)
    app.mainloop()
