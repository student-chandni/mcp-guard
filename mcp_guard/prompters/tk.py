import queue
import threading
from dataclasses import dataclass, field

from mcp_guard.prompters.base import (
    ALLOW, ALLOW_ALWAYS, BLOCK, CANCELLED, TIMEOUT, Prompter, format_details,
)


@dataclass
class _Ticket:
    req: object
    timeout: float
    cancelled: threading.Event
    done: threading.Event = field(default_factory=threading.Event)
    answer: str = BLOCK


class TkPrompter(Prompter):
    """Popup for Windows/Linux. Tk isn't thread safe, so worker threads push
    tickets onto a queue and the main thread turns each one into a window."""

    name = "tk"

    def __init__(self):
        import tkinter  # noqa: F401 - fail now if Tk is missing

        self._inbox = queue.Queue()

    def ask(self, req, timeout, cancelled):
        ticket = _Ticket(req, timeout, cancelled)
        self._inbox.put(ticket)
        ticket.done.wait()
        return ticket.answer

    def run_mainloop(self, stop):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        windows = []

        def finish(ticket, win, answer):
            if not ticket.done.is_set():
                ticket.answer = answer
                ticket.done.set()
            if win in windows:
                windows.remove(win)
            win.destroy()

        def show(ticket):
            win = tk.Toplevel(root)
            win.title(f"MCP Guard: approve {ticket.req.tool}?")
            win.attributes("-topmost", True)
            win.resizable(False, False)

            body = tk.Text(win, width=72, height=18, wrap="word", font=("TkFixedFont", 11))
            body.insert("1.0", format_details(ticket.req))
            body.configure(state="disabled")
            body.pack(padx=12, pady=(12, 6))
            countdown = tk.Label(win)
            countdown.pack()

            buttons = tk.Frame(win)
            buttons.pack(pady=(4, 12))
            for text, answer in (("Block", BLOCK), ("Always Allow", ALLOW_ALWAYS), ("Allow", ALLOW)):
                tk.Button(buttons, text=text, width=12,
                          command=lambda a=answer: finish(ticket, win, a)).pack(side="left", padx=6)
            win.protocol("WM_DELETE_WINDOW", lambda: finish(ticket, win, BLOCK))
            win.bind("<Escape>", lambda _e: finish(ticket, win, BLOCK))

            # cascade so concurrent popups don't sit exactly on top of each other
            offset = 30 * (len(windows) % 8)
            win.geometry(f"+{200 + offset}+{150 + offset}")
            win.lift()
            win.focus_force()
            windows.append(win)
            remaining = [int(ticket.timeout)]

            def tick():
                if ticket.done.is_set():
                    return
                if ticket.cancelled.is_set():
                    return finish(ticket, win, CANCELLED)
                if remaining[0] <= 0:
                    return finish(ticket, win, TIMEOUT)
                countdown.configure(text=f"Blocks automatically in {remaining[0]}s")
                remaining[0] -= 1
                win.after(1000, tick)

            tick()

        def poll():
            if stop.is_set():
                for w in list(windows):
                    w.destroy()
                root.quit()
                return
            while True:
                try:
                    show(self._inbox.get_nowait())
                except queue.Empty:
                    break
            root.after(100, poll)

        poll()
        root.mainloop()

        while not self._inbox.empty():
            t = self._inbox.get_nowait()
            t.answer = CANCELLED
            t.done.set()
