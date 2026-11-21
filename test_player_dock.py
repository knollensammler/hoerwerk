"""X11 protocol check using a synthetic window under Xvfb."""

import os
from pathlib import Path
import unittest
from unittest import mock

from player_dock import PlayerDock


@unittest.skipUnless(os.environ.get("DISPLAY"), "Keine X11-Testanzeige")
class PlayerDockTests(unittest.TestCase):
    def test_embeds_and_restores_only_the_matching_new_window(self):
        import tkinter as tk
        from Xlib import X, Xatom, display

        root = tk.Tk()
        self.addCleanup(root.destroy)
        host = tk.Frame(root, width=400, height=500)
        host.pack()
        root.update()
        connection = display.Display()
        self.addCleanup(connection.close)
        screen = connection.screen()
        parent = screen.root
        status = mock.Mock()
        dock = PlayerDock(root, host, status)
        self.addCleanup(lambda: dock.close() if dock.attached else None)
        first = parent.create_window(0, 0, 100, 60, 0, screen.root_depth)
        target = parent.create_window(0, 0, 100, 60, 0, screen.root_depth)
        first.change_property(dock.pid_atom, Xatom.CARDINAL, 32, [100])
        target.change_property(dock.pid_atom, Xatom.CARDINAL, 32, [101])
        for window in (first, target):
            window.map()
        parent.change_property(dock.clients_atom, Xatom.WINDOW, 32, [first.id, target.id])
        connection.sync()

        dock.profile_dir = Path("/fixture/profile")
        with mock.patch("player_dock.uses_player_profile", side_effect=lambda pid, _: pid == 101):
            dock.poll()
        self.assertTrue(dock.attached)
        self.assertEqual(target.query_tree().parent.id, host.winfo_id())
        self.assertEqual(first.query_tree().parent.id, parent.id)
        self.assertEqual(target.get_attributes().map_state, X.IsViewable)

        dock.close()
        dock.attached = False
        self.assertEqual(target.query_tree().parent.id, parent.id)
        self.assertEqual(target.get_attributes().map_state, X.IsViewable)
        status.assert_called_once()
