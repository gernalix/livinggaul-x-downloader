import argparse
import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import telegram_video_downloader as video
LAUNCHER = runpy.run_path(str(ROOT / 'video_downloader'))


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.window_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.window_tmp.cleanup)
        self.window_patch = patch.dict(LAUNCHER['main'].__globals__, WINDOW_STATE=Path(self.window_tmp.name))
        self.window_patch.start()
        self.addCleanup(self.window_patch.stop)

    def test_parallel_window_requests_reserve_only_one_terminal(self):
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            tokens = list(pool.map(lambda _: LAUNCHER['reserve_window'](), range(8)))
        token = next(t for t in tokens if t)
        self.assertEqual(sum(t is not None for t in tokens), 1)
        self.assertTrue(LAUNCHER['claim_window'](token))
        self.assertIsNone(LAUNCHER['reserve_window']())
        LAUNCHER['release_window'](token)
        self.assertIsNotNone(LAUNCHER['reserve_window']())

    def test_stale_terminal_pid_does_not_block_a_new_window(self):
        token = LAUNCHER['reserve_window']()
        self.assertTrue(LAUNCHER['claim_window'](token))
        with LAUNCHER['window_state']() as (state, path):
            state['start'] = 'wrong-process-generation'
            LAUNCHER['save_window'](path, state)
        self.assertIsNotNone(LAUNCHER['reserve_window']())

    def test_archive_counts_read_only_aggregates_without_video_files(self):
        engine = runpy.run_path(str(ROOT / 'livinggaul-x-downloader'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'catalog.sqlite'
            con = engine['connect_db'](path)
            for index, state in enumerate(('archived', 'archived', 'staged'), 1):
                con.execute("INSERT INTO downloads (id,original_url,status,storage_state,archive_ack_at,created_at,updated_at) VALUES (?,?,'downloaded',?,?,?,?)",
                            (index, 'fixture:' + str(index), state, 1 if state == 'archived' else None, '', ''))
            con.commit(); con.close()
            with patch.object(Path, 'open', side_effect=AssertionError('Video files must not be opened')):
                counts = video.archive_counts(path, {1, 3})
            self.assertEqual((counts['global_confirmed'], counts['global_waiting']), (2, 1))
            self.assertEqual((counts['run_completed'], counts['run_confirmed'], counts['run_waiting']), (2, 1, 1))

    def test_monitor_status_excludes_contents_and_current_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            dash = video.Dashboard('PRIVATE-CHANNEL', io.StringIO())
            dash.current = 'PRIVATE-TITLE https://private.invalid/item'
            dash.telemetry_path = state / 'progress.json'
            dash.draw(True)
            raw = dash.telemetry_path.read_text()
            self.assertNotIn('PRIVATE-', raw)
            self.assertNotIn('private.invalid', raw)
            self.assertEqual(dash.telemetry_path.stat().st_mode & 0o777, 0o600)
            with patch.object(video, 'STATE', state), patch.object(video, 'archive_counts', return_value={}), \
                    patch.object(video, 'client', side_effect=AssertionError('No Telegram access')), \
                    contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(video.main(['--status']), 0)
            data = json.loads(stream.getvalue())
            self.assertNotIn('phase', data['progress'])
            self.assertTrue(all(v is None or isinstance(v, (int, float, bool)) for v in data['progress'].values()))

    def test_redraw_erases_each_line_and_prevents_wrapping(self):
        dash = video.Dashboard('fixture', io.StringIO())
        dash.ansi = True
        dash.current = 'long title ' * 40
        with patch.object(video.shutil, 'get_terminal_size', return_value=os.terminal_size((70, 24))):
            dash.draw(True)
        lines = dash.stream.getvalue().removeprefix('\033[H').removesuffix('\033[J').splitlines()
        self.assertTrue(all(line.endswith('\033[K') for line in lines))
        self.assertTrue(all(len(line.removesuffix('\033[K')) <= 69 for line in lines))

    def test_chat_id_is_argument_not_fixed_channel(self):
        for value in ('7', '987654321', '-100987654321'):
            self.assertEqual(video.chat_id(value), int(value))
            self.assertEqual(LAUNCHER['valid_chat_id'](value), value)
        for value in ('0', 'test', '1;uname', '--login', ' 7'):
            with self.assertRaises(argparse.ArgumentTypeError):
                video.chat_id(value)

    def test_caption_hidden_links_and_buttons(self):
        message = NS(message='🎬 https://example.org/video.',
                     entities=[NS(url='https://x.com/user/status/1234567890123456789')],
                     reply_markup=NS(rows=[NS(buttons=[NS(url='https://example.org/hidden')])]))
        self.assertEqual(video.message_links(message), [
            'https://example.org/video', 'https://x.com/user/status/1234567890123456789',
            'https://example.org/hidden'])

    def test_dashboard_separates_link_percentage_from_current_bytes(self):
        stream = io.StringIO()
        dash = video.Dashboard(987, stream)
        dash.total, dash.processed = 10, 3
        dash.progress({'downloaded_bytes': 20, 'total_bytes': 100})
        self.assertIn('3/10 (30.0%)', stream.getvalue())
        self.assertIn('20.0%', stream.getvalue())
        dash.progress({'downloaded_bytes': 30})
        self.assertIsNone(dash.percent)

    def test_scan_complete_history_and_disconnect(self):
        telegram = NS(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                      get_input_entity=AsyncMock(return_value='entity'))
        async def messages(*args, **kwargs):
            self.assertIsNone(kwargs['limit'])
            for text in ('https://example.org/video', 'https://example.org/video', 'https://example.org/other'):
                yield NS(message=text)
        telegram.iter_messages = messages
        dash = video.Dashboard(987, io.StringIO())
        with patch.object(video, 'client', return_value=telegram):
            links = asyncio.run(video.collect_links(987, dash))
        self.assertEqual(len(links), 2)
        self.assertEqual(dash.messages, 3)
        telegram.disconnect.assert_awaited_once()

    def test_dashboard_uses_one_screen_and_restores_terminal_on_error(self):
        stream = io.StringIO()
        dash = video.Dashboard('fixture', stream)
        dash.ansi = True
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with dash.live() as worker:
                dash.draw(True)
                dash.phase = 'Fixture successiva'
                dash.draw(True)
                raise RuntimeError('fixture')
        output = stream.getvalue()
        self.assertEqual(output.count('\033[?1049h'), 1)
        self.assertEqual(output.count('\033[?1049l'), 1)
        self.assertEqual(output.count('\033[2J'), 1)
        self.assertIn('\033[?25h', output)
        self.assertIn('Fixture successiva', output.split('\033[?1049l')[1])
        self.assertFalse(worker.is_alive())

    def test_dashboard_refreshes_during_synchronous_work(self):
        dash = video.Dashboard('fixture', io.StringIO())
        refreshed = threading.Event()
        with patch.object(dash, 'draw', side_effect=lambda *a: refreshed.set()):
            with dash.live(interval=0.01) as worker:
                # No asyncio loop or download progress callback is running.
                self.assertTrue(refreshed.wait(timeout=1))
        self.assertFalse(worker.is_alive())

    def test_connection_timeout_cancels_and_disconnects(self):
        cancelled = []
        async def blocked():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        telegram = NS(connect=blocked, disconnect=AsyncMock())
        dash = video.Dashboard(987, io.StringIO())
        step = video.telegram_step
        async def short_step(awaitable, dash, phase):
            return await step(awaitable, dash, phase, timeout=0.02)
        with patch.object(video, 'client', return_value=telegram), \
                patch.object(video, 'telegram_step', side_effect=short_step):
            with self.assertRaisesRegex(RuntimeError, 'tempo limite.*Connessione Telegram') as error:
                asyncio.run(video.collect_links(987, dash))
        self.assertNotIn('987', str(error.exception))
        self.assertEqual(cancelled, [True])
        telegram.disconnect.assert_awaited_once()

    def test_fresh_session_stops_dialog_search_at_requested_channel(self):
        telegram = NS(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                      get_input_entity=AsyncMock(side_effect=ValueError('not cached')))
        async def dialogs(**kwargs):
            yield NS(id=-1000000000001, input_entity='other')
            yield NS(id=-1000000000987, input_entity='target')
            self.fail('Must not read unrelated dialogs after finding target')
        async def messages(entity, **kwargs):
            self.assertEqual(entity, 'target')
            yield NS(message='https://example.org/video')
        telegram.iter_dialogs, telegram.iter_messages = dialogs, messages
        dash = video.Dashboard(987, io.StringIO())
        with patch.object(video, 'client', return_value=telegram):
            self.assertEqual(asyncio.run(video.collect_links(987, dash)), ['https://example.org/video'])
        self.assertIn('Verifica della sessione Telegram', dash.stream.getvalue())
        self.assertIn('Ricerca nei dialoghi Telegram', dash.stream.getvalue())
        telegram.disconnect.assert_awaited_once()

    def test_network_wait_keeps_dashboard_refreshing(self):
        dash = Mock(phase='old')
        async def request():
            await asyncio.sleep(0.02)
            return 'ready'
        self.assertEqual(asyncio.run(video.telegram_step(request(), dash, 'Connessione', timeout=1)), 'ready')
        self.assertGreaterEqual(dash.draw.call_count, 2)

    def test_real_catalog_metadata_dedup_and_no_notifications(self):
        import video_engine
        engine = vars(video_engine)
        globals_ = engine['download_one'].__globals__
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'existing.sqlite'
            lock_file = root / 'runtime.lock'
            def fake_run(command, **kwargs):
                # Catalog publication must remain available during transfer.
                with lock_file.open('a') as probe:
                    video.fcntl.flock(probe, video.fcntl.LOCK_EX | video.fcntl.LOCK_NB)
                target = Path(command[command.index('--output') + 1].replace('%(ext)s', 'mp4'))
                target.write_bytes(b'fixture content')
                return subprocess.CompletedProcess(command, 0, '', '')
            metadata = {'title': 'Fixture video', 'uploader': 'Fixture', 'url': 'https://video.twimg.com/v.mp4',
                        'formats': [{'vcodec': 'h264', 'url': 'https://video.twimg.com/v.mp4'}]}
            dash = video.Dashboard(987, io.StringIO())
            with patch.dict(os.environ, LIVINGGAUL_MANAGED='1'), patch.dict(globals_,
                    DEFAULT_OUTPUT_DIR=root / 'output', DEFAULT_DB_PATH=db,
                    DEFAULT_STATE_DIR=root, DEFAULT_LOG_DIR=root / 'logs', DEFAULT_ARCHIVE=root / 'archive.txt',
                    extract_metadata=Mock(side_effect=lambda *a: dict(metadata)),
                    run=fake_run, tool_path=lambda name: name, ensure_max_480=lambda path, log: (path, '480p'),
                    ffprobe_file_metadata=lambda path: {'width': 640, 'height': 480, 'duration_ms': 1000},
                    video_content_fingerprint=lambda *a: None), patch.object(video, 'progress_run', side_effect=lambda original, *a: original), \
                    patch.object(subprocess, 'Popen', side_effect=AssertionError('Network/process forbidden in catalog test')), \
                    patch.object(globals_['pipeline'], 'drain') as notify:
                result = video.download(['https://x.com/a/status/1234567890123456789',
                                                 'https://example.org/video'], 987, dash, engine, runtime_lock=lock_file)
                self.assertEqual(result, 0, '\n'.join(p.read_text() for p in (root / 'logs').glob('*')))
                con = engine['connect_db'](db)
                rows = con.execute('SELECT * FROM video_catalog ORDER BY id').fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]['tweet_id'], '1234567890123456789')
                self.assertIsNone(rows[1]['tweet_id'])
                self.assertEqual(rows[1]['original_url'], 'https://example.org/video')
                self.assertEqual(rows[1]['title'], 'Fixture video')
                self.assertEqual(rows[1]['status'], 'skipped_duplicate')
                self.assertEqual(rows[0]['width'], 640)
                self.assertEqual(con.execute('SELECT count(*) FROM notification_outbox').fetchone()[0], 0)
                snapshots = con.execute('SELECT metadata_json FROM metadata_snapshots').fetchall()
                self.assertEqual(len(snapshots), 2)
                self.assertTrue(all(json.loads(row[0])['telegram_chat_id'] == 987 for row in snapshots))
                con.close()
                notify.assert_not_called()
                second = video.Dashboard(987, io.StringIO())
                self.assertEqual(video.download(['https://x.com/a/status/1234567890123456789',
                                                 'https://example.org/video'], 987, second, engine), 0)
                self.assertEqual((second.processed, second.downloaded, second.skipped), (2, 0, 2))
                self.assertEqual(second.found, 2)
                self.assertEqual(globals_['extract_metadata'].call_count, 2)

            self.assertEqual((dash.processed, dash.downloaded, dash.skipped), (2, 1, 1))
            self.assertEqual(dash.found, 2)

    def test_progress_stream_from_process_without_network(self):
        dash = video.Dashboard(987, io.StringIO())
        class LocalProcess:
            def __new__(cls, command, **kwargs):
                return subprocess.Popen([sys.executable, '-c', 'print(\'VD_PROGRESS {"downloaded_bytes": 25, "total_bytes": 100}\')'], **kwargs)
        globals_ = {'pipeline': NS(resource_check=Mock()), 'DEFAULT_OUTPUT_DIR': Path('/unused')}
        original = subprocess.Popen
        with patch.object(video.subprocess, 'Popen', side_effect=lambda command, **kwargs: original(
                [sys.executable, '-c', 'print(\'VD_PROGRESS {"downloaded_bytes": 25, "total_bytes": 100}\')'], **kwargs)):
            result = video.progress_run(None, globals_, dash)(['fake-ytdlp', '--output', 'unused', '--format', 'unused'])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(dash.percent, 25)

    def test_ssh_uses_existing_resource_limits_and_no_uploaded_session(self):
        command = LAUNCHER['remote_command'](['--', '-100987'])
        self.assertIn('Environment=HOME=/home/livinggaul', command)
        self.assertIn('MemoryMax=220M', command)
        self.assertEqual(command[-2:], ['--', '-100987'])
        self.assertFalse(any('LoadCredential=' in token for token in command))

    def test_interrupt_and_window_hangup_stop_only_the_owned_remote_unit(self):
        for action in ('ctrl_c', 'window_close', 'normal_exit'):
            with self.subTest(action=action):
                previous = signal.getsignal(signal.SIGHUP)
                def interactive(command):
                    if action == 'ctrl_c':
                        raise KeyboardInterrupt
                    if action == 'window_close':
                        signal.raise_signal(signal.SIGHUP)
                    return 0
                with patch.object(subprocess, 'call', side_effect=interactive) as call, \
                        patch.object(subprocess, 'run', return_value=NS(returncode=0)) as control:
                    code = LAUNCHER['connect'](['--demo'])
                self.assertEqual(code, 0 if action == 'normal_exit' else 130)
                self.assertEqual(signal.getsignal(signal.SIGHUP), previous)
                script = call.call_args.args[0][-1]
                import re
                unit = re.search(r'--unit=(livinggaul-cli-[0-9a-f]{12})', script)[1]
                stop_script = control.call_args.args[0][-1]
                self.assertIn('systemctl stop ' + unit, stop_script)
                self.assertNotIn('--demo', stop_script)
                self.assertEqual(control.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_failed_cleanup_does_not_claim_remote_stop(self):
        with patch.object(subprocess, 'call', side_effect=KeyboardInterrupt), \
                patch.object(subprocess, 'run', return_value=NS(returncode=255)), \
                contextlib.redirect_stderr(io.StringIO()) as stream:
            self.assertEqual(LAUNCHER['connect'](['--demo']), 130)
        self.assertIn('Chiusura remota non confermata', stream.getvalue())

    def test_default_launch_opens_terminal_with_chat_id_as_one_argument(self):
        fake = Mock(wait=Mock(return_value=0))
        globals_ = LAUNCHER['main'].__globals__
        with patch.dict(globals_, shutil=NS(which=lambda name: '/usr/bin/ptyxis')), \
                patch.dict(os.environ, DISPLAY='', WAYLAND_DISPLAY=''), \
                patch.object(subprocess, 'run', return_value=NS(stdout='WAYLAND_DISPLAY=wayland-fixture\n')) as manager, \
                patch.object(subprocess, 'Popen', return_value=fake) as spawn:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(LAUNCHER['main'](['--', '-100987']), 0)
        command = spawn.call_args.args[0]
        self.assertIn('--terminal-session', command)
        self.assertIn('--terminal-token', command)
        self.assertEqual(command[-2:], ['--', '-100987'])
        self.assertIn('--new-window', command)
        self.assertEqual(spawn.call_args.kwargs['env']['WAYLAND_DISPLAY'], 'wayland-fixture')
        manager.assert_called_once()
        with contextlib.redirect_stdout(io.StringIO()) as stream, patch.object(subprocess, 'Popen') as again:
            self.assertEqual(LAUNCHER['main'](['--', '-100987']), 0)
            again.assert_not_called()
        self.assertEqual(json.loads(stream.getvalue())['status'], 'already_open')
