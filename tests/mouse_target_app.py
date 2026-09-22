# ==============================================================================
# mouse_target_app.py - Deterministic click target for tests/test_mouse.py.
# A tkinter window with labeled buttons that visibly react when clicked, so
# the point tool's until-proof has real, OCR-readable consequences.
# ==============================================================================
import tkinter as tk


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MouseTarget - Automation Test Window")
        self.geometry("560x420+200+150")
        self.configure(bg="#f4f4f4")

        top = tk.Frame(self, bg="#f4f4f4")
        top.pack(pady=10)
        tk.Label(top, text="TOP ROW", bg="#f4f4f4", fg="#333",
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=12)
        b1 = tk.Button(top, text="Press TOP", font=("Segoe UI", 11),
                       command=lambda: self.react("top was clicked"))
        b1.pack(side="left", padx=6)

        row2 = tk.Frame(self, bg="#f4f4f4")
        row2.pack(pady=6)
        tk.Label(row2, text="SECOND ROW", bg="#f4f4f4", fg="#333",
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=8)
        b2 = tk.Button(row2, text="Press BOTTOM", font=("Segoe UI", 11),
                       command=lambda: self.react("bottom was clicked"))
        b2.pack(side="left", padx=6)

        self.click_btn = tk.Button(self, text="Click Me", font=("Segoe UI", 12, "bold"),
                                   bg="#3b82f6", fg="white", width=18,
                                   command=lambda: self.react("RESULT PANEL opened"))
        self.click_btn.pack(pady=14)

        tk.Button(self, text="Second Button", font=("Segoe UI", 11),
                  command=lambda: self.react("second clicked")).pack(pady=6)

        self.result = tk.Label(self, text="(idle - no result yet)", bg="#f4f4f4",
                               fg="#111", font=("Segoe UI", 13, "bold"), wraplength=500)
        self.result.pack(pady=22)

    def react(self, text: str) -> None:
        self.result.configure(text=text)


if __name__ == "__main__":
    App().mainloop()
