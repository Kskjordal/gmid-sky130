#!/usr/bin/env python3
"""
run_gide.py -- launch GIDE with a fix for the customtkinter mouse-wheel crash.

Symptom, on every scroll anywhere in the GIDE window:

    Exception in Tkinter callback
      ...customtkinter/windows/widgets/ctk_scrollable_frame.py, in
      _check_if_valid_scroll
        elif widget.master is not None:
    AttributeError: 'str' object has no attribute 'master'

Cause: CTkScrollableFrame binds the mouse wheel with `bind_all`, so it sees
wheel events from every widget in the application -- including ones Tk hands
back as a pathname string rather than a Python widget object (embedded
matplotlib canvases are the usual source, and GIDE's Plotter is full of them).
`_check_if_valid_scroll` then walks `.master` on a `str` and blows up.

It is non-fatal -- Tk prints the traceback and carries on -- but it spams the
console and scrolling misbehaves. The bug is in customtkinter, not GIDE, and
it is present in both 5.2.2 (as `check_if_master_is_canvas`) and 6.0.0 (as
`_check_if_valid_scroll`), so downgrading does not reliably help.

This launcher patches the method at runtime to resolve a pathname string back
to its widget via `nametowidget`, and to give up quietly if that fails. It
touches nothing on disk, so a `pip install --upgrade customtkinter` will not
undo it.

Usage -- from inside the GIDE checkout:

    python run_gide.py

Or point it at one:

    python run_gide.py --gide-dir /path/to/GIDE-Universal-Design-Studio
"""

from __future__ import annotations

import argparse
import os
import sys


def patch_customtkinter() -> str:
    """Make CTkScrollableFrame's wheel handler tolerate pathname strings."""
    from customtkinter.windows.widgets.ctk_scrollable_frame import (
        CTkScrollableFrame,
    )

    # 6.x calls it _check_if_valid_scroll; 5.x calls it
    # check_if_master_is_canvas. Both walk .master and both can be handed a str.
    for name in ("_check_if_valid_scroll", "check_if_master_is_canvas"):
        original = getattr(CTkScrollableFrame, name, None)
        if original is None:
            continue

        def make_safe(orig):
            def safe(self, widget):
                if isinstance(widget, str):
                    try:
                        widget = self.nametowidget(widget)
                    except Exception:
                        return False
                try:
                    return orig(self, widget)
                except AttributeError:
                    return False
            return safe

        setattr(CTkScrollableFrame, name, make_safe(original))
        return name

    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gide-dir", default=".",
                    help="the GIDE-Universal-Design-Studio checkout "
                         "(default: current directory)")
    args = ap.parse_args()

    gide = os.path.abspath(os.path.expanduser(args.gide_dir))
    if not os.path.isfile(os.path.join(gide, "main.py")):
        sys.exit(f"No main.py in {gide}\n"
                 f"Run this from inside the GIDE checkout, or pass --gide-dir.")

    sys.path.insert(0, gide)
    os.chdir(gide)

    patched = patch_customtkinter()
    if patched:
        print(f"Patched CTkScrollableFrame.{patched} "
              f"(mouse-wheel AttributeError workaround)")
    else:
        print("Note: no scrollable-frame scroll check found to patch -- "
              "your customtkinter may already be fixed.")

    from gui.app import App
    App().mainloop()


if __name__ == "__main__":
    main()
