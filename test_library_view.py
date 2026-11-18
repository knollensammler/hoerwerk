import tempfile
import threading
from pathlib import Path
import unittest
from unittest import mock

from batch import download_selected
from core import BackendFailed, Core, PortablePaths
from gui import App, local_audio_files, _valid_voucher
from jobs import JobContext
from library_view import download_status, font_scale, ordered_items


class LibraryViewTests(unittest.TestCase):
    def test_sorting_filter_and_missing_data(self):
        a = {"asin": "B012345678", "title": "Äther", "purchase_date": "2023-05-01", "runtime_length_min": 95}
        b = {"asin": "B012345679", "title": "Ziel", "authors": "Müller", "date_added": "2025-01-01", "runtime_length_min": 3}
        c = {"asin": "B012345680", "title": "Nicht datiert"}
        self.assertEqual(ordered_items([a, b, c], "Titel Z–A"), [b, c, a])
        self.assertEqual(ordered_items([a, b, c], "Neu hinzugefügt"), [b, a, c])
        self.assertEqual(ordered_items([a, b, c], "Längste Laufzeit"), [a, b, c])
        self.assertEqual(ordered_items([a, b, c], "Kleinste lokale Datei", sizes={a["asin"]: 10, b["asin"]: 2}), [b, a, c])
        self.assertEqual(ordered_items([a, b, c], "Titel A–Z", "muller"), [b])
        self.assertGreater(font_scale(1001, 680), font_scale(1000, 680))

    def test_sort_by_download_permission_keeps_unchecked_and_local_last(self):
        allowed = {"asin": "B000000001", "title": "Ziel", "download_allowed": True}
        blocked = {"asin": "B000000002", "title": "Buch", "download_allowed": False}
        other_blocked = {"asin": "B000000005", "title": "Abend", "download_allowed": False}
        unknown = {"asin": "B000000003", "title": "Altbestand"}
        local = {"asin": "local-123", "title": "Import", "kind": "local", "download_allowed": True}
        items = [allowed, blocked, other_blocked, unknown, local]
        self.assertEqual(ordered_items(items, "Gesperrt zuerst"),
                         [other_blocked, blocked, allowed, unknown, local])
        self.assertEqual(ordered_items(items, "Freigegeben zuerst"),
                         [allowed, other_blocked, blocked, unknown, local])

    def test_play_opens_webplayer_without_a_playable_local_file(self):
        app = App.__new__(App)
        app.paths, app.profile, app.status = mock.Mock(), mock.Mock(), mock.Mock()
        app.open_player = mock.Mock()
        app.selected_many = lambda: [
            {"asin": "B012345678", "title": "Audible", "download_allowed": False}
        ]
        with mock.patch("gui.open_standard_browser") as browser, mock.patch("gui.local_audio_files") as files:
            for found in ([], [Path("encrypted.aaxc")]):
                files.return_value = found
                app.play_selection()
            expected = "https://www.audible.de/webplayer?asin=B012345678&isSample=false"
            self.assertEqual(browser.call_args_list, [mock.call(expected), mock.call(expected)])
            app.open_player.assert_not_called()

            files.return_value = [Path("example.mp3")]
            app.play_selection()
            app.open_player.assert_called_once_with(Path("example.mp3"), "title:B012345678", "Audible")
            self.assertEqual(browser.call_count, 2)

    def test_download_sends_only_explicitly_enabled_audible_titles(self):
        items = [
            {"asin": "B000000001", "title": "Gesperrt", "download_allowed": False},
            {"asin": "B000000002", "title": "Altbestand"},
            {"asin": "B000000003", "title": "Fehlformat", "download_allowed": 1},
            {"asin": "local-123", "title": "Import", "kind": "local", "download_allowed": True},
            {"asin": "B000000004", "title": "Freigegeben", "download_allowed": True},
        ]
        app = App.__new__(App)
        app.selected_many = lambda: items[:-1]
        app.start = mock.Mock()
        app.download()
        app.start.assert_not_called()

        app.selected_many = lambda: items
        app.download()
        self.assertEqual(app.start.call_args.args[:2], ("download", "B000000004"))
        self.assertEqual([download_status(item) for item in items],
                         ["Gesperrt", "Ungeprüft", "Ungeprüft", "Lokal", "Freigegeben"])

    def test_batch_preserves_success_when_a_title_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = PortablePaths(Path(temporary))
            good, failed = "B012345678", "B012345679"
            core = Core(paths, audible="fixture")
            commands = []

            def run(_program, args, _env, **kwargs):
                commands.extend(args)
                folder = Path(args[args.index("--output-dir") + 1])
                (folder / f"{good}-test.mp3").write_bytes(b"audio")
                raise BackendFailed("Backend meldet einen Fehler.")

            core._run = run
            context = JobContext(paths, "standard", None, threading.Event(), None)
            context.core = lambda: core
            result = download_selected(context, [(good, "Fertig"), (failed, "Fehlt")],
                                       local_audio_files, _valid_voucher)
            self.assertEqual(commands[commands.index("--jobs") + 1], "2")
            self.assertEqual(commands.count("--asin"), 2)
            self.assertEqual(result.files[0].read_bytes(), b"audio")
            self.assertEqual(result.failures, (("Fehlt", "Backend meldet einen Fehler."),))
            self.assertEqual(list(paths.downloads("standard").iterdir()), [paths.downloads("standard") / good])
