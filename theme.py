"""Hörwerks Farben, Typografie und Zustände für Tk."""
from tkinter import font, ttk

from library_view import font_scale

BACKGROUND = "#101e24"
PANEL = "#1a3037"
FIELD = "#142831"
TEXT = "#eff6ff"
MUTED = "#a9bacd"
BORDER = "#36585d"
ACCENT = "#78decb"
INK = "#09261f"


def apply(root):
    root.configure(background=BACKGROUND)
    family = font.nametofont("TkDefaultFont").actual("family")
    baseline = root.winfo_fpixels("1p")
    sizes = {"regular": 11, "title": 29, "section": 15, "small": 10}
    fonts = {name: font.Font(root=root, family=family, size=-round(size * baseline),
                             weight="normal" if name == "regular" else "bold")
             for name, size in sizes.items()}
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BACKGROUND, foreground=TEXT, font=fonts["regular"],
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.configure("TFrame", background=BACKGROUND)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("TLabel", background=BACKGROUND, foreground=TEXT)
    style.configure("Panel.TLabel", background=PANEL)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("PanelMuted.TLabel", foreground=MUTED, background=PANEL)
    style.configure("Title.TLabel", font=fonts["title"])
    style.configure("Section.TLabel", font=fonts["section"])
    style.configure("TEntry", padding=8, fieldbackground=FIELD, insertcolor=TEXT, foreground=TEXT)
    style.map("TEntry", fieldbackground=[("disabled", PANEL), ("focus", FIELD)],
              bordercolor=[("focus", ACCENT)])
    style.configure("TButton", padding=(14, 9), background=PANEL, foreground=TEXT, relief="flat")
    style.map("TButton", background=[("active", "#243950"), ("pressed", "#304963")],
              foreground=[("disabled", "#8496aa")], bordercolor=[("focus", ACCENT)])
    style.configure("Accent.TButton", background=ACCENT, foreground=INK, bordercolor=ACCENT)
    style.map("Accent.TButton", background=[("disabled", PANEL), ("pressed", "#44bda3"), ("active", "#8aefda")],
              foreground=[("disabled", MUTED), ("!disabled", INK)])
    style.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                    rowheight=35, borderwidth=0)
    style.configure("Treeview.Heading", background=PANEL, foreground=MUTED,
                    font=fonts["small"], padding=(10, 9), relief="flat")
    style.map("Treeview", background=[("selected", "#254b65")], foreground=[("selected", TEXT)])
    style.map("Treeview.Heading", background=[("active", PANEL)])
    style.configure("Vertical.TScrollbar", background=PANEL, troughcolor=FIELD, arrowsize=12)
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL,
                    borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT, thickness=4)

    style.configure("Brand.TLabel", font=fonts["small"], foreground=ACCENT)
    style.configure("TCombobox", padding=6, fieldbackground=FIELD, background=PANEL,
                    foreground=TEXT, arrowcolor=TEXT)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
              foreground=[("readonly", TEXT)], selectbackground=[("readonly", FIELD)],
              selectforeground=[("readonly", TEXT)])
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", "#254d58")
    root.option_add("*TCombobox*Listbox.font", str(fonts["regular"]))
    root._hoerwerk_fonts = fonts
    applied = {}
    pending = None

    def resize():
        nonlocal pending
        pending = None
        scale = font_scale(root.winfo_width(), root.winfo_height())
        for name, size in sizes.items():
            pixels = -max(1, round(size * baseline * scale))
            if applied.get(name) != pixels:
                fonts[name].configure(size=pixels)
                applied[name] = pixels
        height = max(25, round(35 * scale))
        if applied.get("row") != height:
            style.configure("Treeview", rowheight=height)
            applied["row"] = height

    def configure(event):
        nonlocal pending
        if event.widget is root and pending is None:
            pending = root.after_idle(resize)

    root.bind("<Configure>", configure, add="+")
