"""Telegram link discovery and dashboard for the existing Oracle video catalog."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import selectors
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(os.environ.get('LIVINGGAUL_ENGINE_ROOT', str(Path(__file__).resolve().parent)))
sys.path.insert(0, str(ROOT))
STATE = Path.home() / '.local/state/video_downloader'
CONFIG = Path.home() / '.config/video_downloader/telegram.json'
SESSION = STATE / 'telegram'
REQUEST_TIMEOUT = 60


def chat_id(value):
    if not re.fullmatch(r'-?[1-9][0-9]{0,15}', str(value)):
        raise argparse.ArgumentTypeError('Inserisci un chat_id numerico diverso da zero.')
    return int(value)


def message_links(message):
    text = getattr(message, 'message', None) or ''
    links = re.findall(r'https?://[^\s<>"\x00-\x1f]+', text)
    for entity in getattr(message, 'entities', None) or []:
        url = getattr(entity, 'url', None)
        if url:
            links.append(url)
    for row in getattr(getattr(message, 'reply_markup', None), 'rows', []) or []:
        links.extend(button.url for button in row.buttons if getattr(button, 'url', None))
    return [url.rstrip('.,;!') for url in links if urlsplit(url).scheme in ('http', 'https')]


def clean(value):
    return re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value))[:180]


class Dashboard:
    def __init__(self, channel, stream=None):
        self.stream = stream or sys.stdout
        self.channel = channel
        self.phase = 'Connessione Telegram'
        self.messages = 0
        self.links = 0
        self.total = 0
        self.processed = 0
        self.found = 0
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.current = '—'
        self.percent = None
        self.bytes = 0
        self.last = 0
        self.started = time.monotonic()
        self.ansi = self.stream.isatty() or os.environ.get('VIDEO_DOWNLOADER_DASH') == '1'
        self._draw_lock = threading.RLock()

    @contextlib.contextmanager
    def live(self, interval=1):
        """Use one terminal screen; refresh even while synchronous work waits."""
        stopped = threading.Event()
        def refresh():
            while not stopped.wait(interval):
                self.draw()
        worker = threading.Thread(target=refresh, name='dashboard-refresh', daemon=True)
        if self.ansi:
            self.stream.write('\033[?1049h\033[?25l\033[2J\033[H')
            self.stream.flush()
        worker.start()
        try:
            yield worker
        finally:
            stopped.set()
            worker.join(timeout=2)
            with self._draw_lock:
                if self.ansi:
                    self.stream.write('\033[?25h\033[?1049l')
                    self.stream.flush()
                # Keep the final counters visible after leaving the alternate screen.
                ansi, self.ansi = self.ansi, False
                try:
                    self.draw(True)
                finally:
                    self.ansi = ansi

    def progress(self, value):
        self.bytes = value.get('downloaded_bytes') or 0
        total = value.get('total_bytes') or value.get('total_bytes_estimate')
        self.percent = min(100, self.bytes * 100 / total) if total else None
        self.draw()

    def draw(self, force=False):
        with self._draw_lock:
            self._draw(force)

    def _draw(self, force=False):
        now = time.monotonic()
        if not force and now - self.last < 0.2:
            return
        self.last = now
        global_progress = f'{self.processed * 100 / self.total:.1f}%' if self.total else 'in calcolo'
        current_progress = f'{self.percent:.1f}%' if self.percent is not None else 'in attesa / dimensione sconosciuta'
        lines = [f'VIDEO DOWNLOADER — Telegram {self.channel}',
                 f'Fase: {self.phase} · trascorsi {int(now - self.started)} s',
                 f'Messaggi letti: {self.messages} · link unici: {self.links}',
                 f'Video confermati finora: {self.found} · scaricati su Oracle: {self.downloaded}',
                 f'Già presenti/duplicati: {self.skipped} · errori/non video: {self.failed}',
                 f'Progresso globale sui link: {self.processed}/{self.total or "?"} ({global_progress})',
                 f'In elaborazione: {clean(self.current)}',
                 f'Download corrente: {current_progress} · {self.bytes / 1048576:.1f} MiB',
                 'Archivio Fedora: trasferimento automatico del servizio esistente.',
                 'Ctrl+C interrompe; rilancia lo stesso chat_id per riprendere.']
        prefix = '\033[H' if self.ansi else '\n'
        suffix = '\033[J' if self.ansi else ''
        self.stream.write(prefix + '\n'.join(lines) + '\n' + suffix)
        self.stream.flush()


def client(receive_updates=False):
    from telethon import TelegramClient
    cfg = json.loads(CONFIG.read_text())
    return TelegramClient(str(SESSION), int(cfg['api_id']), cfg['api_hash'], receive_updates=receive_updates,
                          timeout=10, connection_retries=2, request_retries=2,
                          retry_delay=1, flood_sleep_threshold=0)


async def telegram_step(awaitable, dash, phase, timeout=REQUEST_TIMEOUT):
    """Keep the terminal alive and bound each network wait, without logging peers."""
    changed = dash.phase != phase
    dash.phase = phase
    dash.draw(changed)
    task = asyncio.ensure_future(awaitable)
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f'Telegram: tempo limite durante «{phase}». '
                                   'Rilancia il comando; non occorre comunicare il chat_id.')
            done, _ = await asyncio.wait({task}, timeout=min(1, remaining))
            dash.draw()
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def collect_links(channel, dash):
    from telethon.tl.types import PeerChannel
    from telethon.utils import get_peer_id
    telegram = client()
    try:
        await telegram_step(telegram.connect(), dash, 'Connessione Telegram')
        if not await telegram_step(telegram.is_user_authorized(), dash, 'Verifica della sessione Telegram'):
            raise RuntimeError('Telegram non autorizzato. Esegui video_downloader --login e scansiona il QR.')
        peer = PeerChannel(channel) if channel > 0 else channel
        try:
            entity = await telegram_step(telegram.get_input_entity(peer), dash, 'Ricerca del canale')
        except ValueError:
            # A fresh QR session has no cached access hashes. Stop as soon as
            # the requested dialog is found instead of loading every dialog.
            dialogs = telegram.iter_dialogs(limit=None).__aiter__()
            target = get_peer_id(peer)
            count = 0
            while True:
                try:
                    dialog = await telegram_step(anext(dialogs), dash,
                                                 f'Ricerca nei dialoghi Telegram · esaminati {count}')
                except StopAsyncIteration:
                    raise RuntimeError('Canale non trovato tra i dialoghi accessibili. '
                                       'Controlla localmente il chat_id e l’account collegato.') from None
                count += 1
                if dialog.id == target:
                    entity = dialog.input_entity
                    break
        links = {}
        messages = telegram.iter_messages(entity, limit=None).__aiter__()
        while True:
            try:
                message = await telegram_step(anext(messages), dash, 'Lettura della cronologia accessibile')
            except StopAsyncIteration:
                break
            dash.messages += 1
            for url in message_links(message):
                links.setdefault(url, None)
            dash.links = len(links)
            dash.draw()
        return list(links)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(telegram.disconnect(), timeout=10)


async def login():
    import qrcode
    from telethon.errors import SessionPasswordNeededError
    from getpass import getpass
    telegram = client(receive_updates=True)
    await telegram.connect()
    try:
        if await telegram.is_user_authorized():
            print('Sessione Oracle già autorizzata.')
            return 0
        print('Telegram sul telefono → Impostazioni → Dispositivi → Collega dispositivo desktop.')
        for attempt in range(5):
            qr = await telegram.qr_login()
            image = qrcode.QRCode(border=2)
            image.add_data(qr.url)
            image.print_ascii(invert=True)
            print('Scansiona questo QR dal tuo Telegram (scade entro circa un minuto).', flush=True)
            try:
                await qr.wait(timeout=60)
            except asyncio.TimeoutError:
                print('QR scaduto, ne preparo uno nuovo.')
                continue
            except SessionPasswordNeededError:
                password = getpass('Password della verifica in due passaggi Telegram: ')
                try:
                    await telegram.sign_in(password=password)
                finally:
                    password = None
            print('Autorizzazione Oracle completata. Ora puoi usare il pulsante della mini-app.')
            return 0
        print('Autorizzazione non completata. Rilancia video_downloader --login.')
        return 1
    finally:
        await telegram.disconnect()


def progress_run(original, engine_globals, dash):
    def run(cmd, timeout=180, *, text=True):
        if '--output' not in cmd or '--format' not in cmd:
            return original(cmd, timeout=timeout, text=text)
        command = [*cmd[:1], '--newline', '--progress', '--progress-template',
                   'download:VD_PROGRESS %(progress)j', *cmd[1:]]
        started = time.monotonic()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, bufsize=0)
        tail = b''
        pending = b''
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    for key, _ in selector.select(timeout=0.2):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        tail = (tail + chunk)[-4000:]
                        pending += chunk
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            if line.startswith(b'VD_PROGRESS '):
                                with contextlib.suppress(ValueError, TypeError):
                                    dash.progress(json.loads(line[len(b'VD_PROGRESS '):]))
                        pending = pending[-65536:]
                    engine_globals['pipeline'].resource_check(engine_globals['DEFAULT_OUTPUT_DIR'])
                    if time.monotonic() - started > timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
            code = process.wait(timeout=5)
            return subprocess.CompletedProcess(command, code, '', tail.decode(errors='replace'))
        except BaseException:
            import signal
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        finally:
            process.stdout.close()
    return run


def candidates(engine, links):
    seen = set()
    result = []
    for url in links:
        match = re.fullmatch(r'https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)(?:[/?#].*)?', url)
        post = match[1] if match else None
        key = post or url
        if key not in seen:
            seen.add(key)
            result.append(engine['canonical_candidate'](post, 'telegram') if post else
                          engine['Candidate'](None, url, source='telegram'))
    return result


@contextlib.contextmanager
def overrides(target, **values):
    is_dict = isinstance(target, dict)
    old = {key: target[key] if is_dict else getattr(target, key) for key in values}
    try:
        for key, value in values.items():
            if is_dict:
                target[key] = value
            else:
                setattr(target, key, value)
        yield
    finally:
        for key, value in old.items():
            if is_dict:
                target[key] = value
            else:
                setattr(target, key, value)


@contextlib.contextmanager
def catalog_lock(path, dash):
    if path is None:
        yield
        return
    dash.phase = 'Attesa del catalogo Oracle / trasferimento Fedora'
    dash.draw(True)
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def download(links, channel, dash, engine=None, runtime_lock=None):
    engine = engine or runpy.run_path(str(ROOT / 'livinggaul-x-downloader'))
    globals_ = engine['download_one'].__globals__
    original_extract = globals_['extract_metadata']
    original_title = globals_['stable_title']
    original_fallback = globals_['extract_fxtwitter_metadata']
    items = candidates(engine, links)
    dash.total = len(items)

    def extract(options, candidate, logfile):
        dash.phase = 'Ricerca metadati video'
        dash.draw(True)
        meta = original_extract(options, candidate, logfile)
        meta['telegram_chat_id'] = channel
        dash.found += 1
        dash.current = meta.get('title') or candidate.url
        dash.phase = 'Download / verifica del file'
        dash.draw(True)
        return meta

    def fallback(candidate, logfile):
        if candidate.tweet_id is None:
            raise RuntimeError('Link senza video supportato da yt-dlp')
        return original_fallback(candidate, logfile)

    def title(meta, candidate):
        value = original_title(meta, candidate)
        return value if candidate.tweet_id else value + '-' + hashlib.sha256(candidate.url.encode()).hexdigest()[:16]

    # Mantiene lo staging/ACK esistente; questa dashboard non invia notifiche.
    pipeline = globals_['pipeline']
    def staged(con, row, metadata):
        con.execute("UPDATE downloads SET storage_state='staged' WHERE id=?", (row['id'],))

    def log(message, logfile=None):
        # Il log rimane privato su Oracle; non mescola righe alla dashboard.
        if logfile:
            with logfile.open('a', encoding='utf-8') as stream:
                stream.write(f"{engine['utc_now']()} {message}\n")

    replacements = dict(extract_metadata=extract, extract_fxtwitter_metadata=fallback, stable_title=title,
                        run=progress_run(globals_['run'], globals_, dash), log=log)
    output, db = globals_['DEFAULT_OUTPUT_DIR'], globals_['DEFAULT_DB_PATH']
    engine['mkdirs'](output, db)
    options = argparse.Namespace(output_dir=output, cookies_mode='none', firefox_profile=None,
                                 dry_run=False, retry_failed=True)
    summary = engine['Summary']()
    # Patch solo nel processo worker, mai il servizio o il sorgente condiviso.
    with overrides(globals_, **replacements), overrides(pipeline, record_staged=staged,
            drain=lambda *a, **k: None, enqueue=lambda *a, **k: None):
        with catalog_lock(runtime_lock, dash):
            con = engine['connect_db'](db)
        try:
            for candidate in items:
                with catalog_lock(runtime_lock, dash), con:
                    dash.current = candidate.url
                    dash.phase = 'Verifica catalogo / metadati'
                    dash.percent, dash.bytes = None, 0
                    dash.draw(True)
                    before = summary.skipped_duplicate
                    duplicate = con.execute(
                        "SELECT canonical.* FROM downloads AS link JOIN downloads AS canonical "
                        "ON canonical.id=link.duplicate_of_id WHERE link.original_url=? "
                        "AND link.status='skipped_duplicate' AND canonical.status='downloaded'",
                        (candidate.url,),
                    ).fetchone()
                    if duplicate and engine['row_has_existing_file'](duplicate):
                        summary.posts_analyzed += 1
                        summary.skipped_duplicate += 1
                        dash.found += 1
                    else:
                        engine['download_one'](options, con, candidate, engine['log_path'](), summary)
                    if summary.skipped_duplicate > before:
                        row = con.execute("SELECT status FROM downloads WHERE original_url=?", (candidate.url,)).fetchone()
                        if row and row['status'] == 'downloaded':
                            dash.found += 1
                dash.processed += 1
                dash.downloaded, dash.skipped, dash.failed = summary.downloaded, summary.skipped_duplicate, summary.failed
                dash.draw(True)
        finally:
            con.close()
    dash.phase = 'Completato' if not summary.failed else 'Terminato con errori / link non video'
    dash.draw(True)
    return int(bool(summary.failed))


def fixture_demo():
    dash = Dashboard('DEMO — nessuna rete o video reale')
    dash.messages, dash.links, dash.total = 250, 10, 10
    dash.phase, dash.current = 'Dimostrazione locale della dashboard', 'Video di esempio'
    dash.found, dash.downloaded, dash.skipped, dash.failed, dash.processed = 7, 4, 2, 1, 7
    with dash.live():
        dash.progress({'downloaded_bytes': 40 * 1048576, 'total_bytes': 100 * 1048576})
        time.sleep(3)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('chat_id', type=chat_id, nargs='?')
    parser.add_argument('--login', action='store_true')
    parser.add_argument('--check', action='store_true', help='controlla configurazione senza rete')
    parser.add_argument('--demo', action='store_true', help='dashboard con dati fittizi, senza rete')
    args = parser.parse_args(argv)
    if args.demo:
        return fixture_demo()
    if not (args.login or args.check) and args.chat_id is None:
        parser.error('Inserisci chat_id oppure --login')
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / 'session.lock').open('a') as session_lock:
        try:
            fcntl.flock(session_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Un comando video_downloader è già attivo su Oracle.')
            return 1
        if args.check:
            cfg = json.loads(CONFIG.read_text())
            if not cfg.get('api_id') or not cfg.get('api_hash'):
                raise RuntimeError('Configurazione API incompleta')
            path = Path(str(SESSION) + '.session')
            authorized = False
            if path.exists():
                with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as con:
                    authorized = bool(con.execute('SELECT auth_key FROM sessions').fetchone()[0])
            print('Configurazione API: pronta. Sessione Oracle: ' + ('presente' if authorized else 'da autorizzare con --login'))
            return 0 if authorized else 2
        if args.login:
            return asyncio.run(login())
        dash = Dashboard(args.chat_id)
        dash.phase = 'Caricamento del client Telegram su Oracle'
        with dash.live():
            dash.draw(True)
            links = asyncio.run(collect_links(args.chat_id, dash))
            return download(links, args.chat_id, dash, runtime_lock=Path.home() / 'runtime.lock')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nInterrotto. Puoi rilanciare lo stesso chat_id per riprendere.')
        raise SystemExit(130)
    except FileNotFoundError:
        print('Configurazione Telegram mancante su Oracle. Completa la configurazione iniziale.', file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            print(clean(exc), file=sys.stderr)
        else:
            print(f'Operazione non completata ({type(exc).__name__}). Verifica accesso al canale e rete.', file=sys.stderr)
        raise SystemExit(1)
