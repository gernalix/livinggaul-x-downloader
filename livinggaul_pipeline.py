"""Oracle catalog, durable Telegram delivery, and verified Fedora archive pull.

Mutable data lives outside Git. Catalog publication and archive ACK share a short lock; notifications use their own lock.
An archive ACK is a durable assertion from the hash-verifying Fedora puller.
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path('/home/livinggaul')
DB = ROOT / '.local/state/livinggaul-x-downloader/download_history.sqlite'
ARCHIVE_ROOT = Path('/mnt/Seagate6TB/LivingGaul_X')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def migrate(con):
    columns = {r[1] for r in con.execute('PRAGMA table_info(downloads)')}
    for name, ddl in {
        'storage_state': "TEXT CHECK(storage_state IN ('staged','archived','failed'))",
        'archive_path': 'TEXT', 'archive_ack_at': 'INTEGER',
    }.items():
        if name not in columns:
            con.execute(f'ALTER TABLE downloads ADD COLUMN {name} {ddl}')
    con.execute('''CREATE TABLE IF NOT EXISTS notification_outbox (
        event_id TEXT PRIMARY KEY, download_id INTEGER REFERENCES downloads(id),
        text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','sending','delivered','ambiguous','rejected')),
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0,
        message_id INTEGER, error TEXT, created_at INTEGER NOT NULL,
        delivered_at INTEGER)''')
    con.execute('''CREATE VIEW IF NOT EXISTS storage_catalog AS
        SELECT id,status,storage_state,sha256,size_bytes,file_path,archive_path,
        archive_ack_at FROM downloads''')


def archived_valid(row):
    if 'storage_state' not in row.keys() or row['storage_state'] != 'archived':
        return False
    path = row['archive_path']
    return bool(path and Path(path).is_absolute()
                and Path(path).parent == ARCHIVE_ROOT / 'videos'
                and row['archive_ack_at'] and row['sha256'] and row['size_bytes'])


def safe_url(value, host=None):
    if not isinstance(value, str) or len(value) > 1800:
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        return None
    if host and parsed.hostname != host:
        return None
    if not host and parsed.hostname not in ('x.com', 'twitter.com'):
        return None
    return html.escape(value, quote=True)


def video_message(row, metadata=None, prefix=''):
    post = safe_url(f"https://x.com/LivingGaul/status/{row['tweet_id']}")
    text = f'{prefix}LivingGaul: <a href="{post}">post {html.escape(str(row["tweet_id"]))}</a>'
    quote = safe_url((metadata or {}).get('quoted_source_url'))
    if quote:
        text += f'\n<a href="{quote}">Post sorgente quoted</a>'
    direct = safe_url(row['direct_video_url'], 'video.twimg.com')
    if direct:
        text += f'\n<a href="{direct}">MP4 diretto</a> (URL CDN può scadere)'
    return text


def enqueue(con, event_id, text, download_id=None):
    # One event per video, avoiding batch splitting and Telegram's 4096-char cap.
    if len(text.encode('utf-16-le')) // 2 > 4000:
        raise ValueError('notification exceeds conservative Telegram limit')
    con.execute('''INSERT OR IGNORE INTO notification_outbox
        (event_id,download_id,text,created_at) VALUES(?,?,?,?)''',
        (event_id, download_id, text, int(time.time())))


def record_staged(con, row, metadata):
    con.execute("UPDATE downloads SET storage_state='staged' WHERE id=?", (row['id'],))
    enqueue(con, f"download:{row['id']}:{row['sha256']}", video_message(row, metadata, os.environ.get('LIVINGGAUL_NOTIFY_PREFIX','')), row['id'])


def credentials():
    directory = Path(os.environ['CREDENTIALS_DIRECTORY'])
    return (directory.joinpath('token').read_text().strip(),
            directory.joinpath('chat').read_text().strip())


def telegram_request(token, payload, method='sendMessage'):
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.loads(response.read(65536))
    except urllib.error.HTTPError as exc:
        # Only a Telegram JSON refusal proves the send did not succeed.
        try:
            result = json.loads(exc.read(65536))
        except (ValueError, OSError):
            raise RuntimeError('ambiguous HTTP delivery') from None
        if result.get('ok') is False:
            return result
        raise RuntimeError('ambiguous HTTP delivery') from None
    except Exception:
        # Never expose credential URLs in exception strings.
        raise RuntimeError('ambiguous network delivery') from None


def drain(con, sender=telegram_request, creds=None):
    token, chat = creds or credentials()
    # A process died after claiming: no blind resend of a possibly delivered event.
    con.execute("UPDATE notification_outbox SET state='ambiguous',error='restart during send' WHERE state='sending'")
    con.commit()
    events = con.execute("SELECT * FROM notification_outbox WHERE state='pending' AND next_attempt<=? ORDER BY created_at LIMIT 20", (int(time.time()),)).fetchall()
    for event in events:
        con.execute("UPDATE notification_outbox SET state='sending',attempts=attempts+1 WHERE event_id=?", (event['event_id'],)); con.commit()
        try:
            result = sender(token, {'chat_id':chat,'text':event['text'],'parse_mode':'HTML',
                                    'link_preview_options':{'is_disabled':True}})
        except Exception:
            con.execute("UPDATE notification_outbox SET state='ambiguous',error='delivery requires receipt reconciliation' WHERE event_id=?", (event['event_id'],))
        else:
            message_id = (result.get('result') or {}).get('message_id')
            if result.get('ok') is True and isinstance(message_id, int):
                con.execute("UPDATE notification_outbox SET state='delivered',message_id=?,delivered_at=?,error=NULL WHERE event_id=?", (message_id,int(time.time()),event['event_id']))
            elif result.get('ok') is False:
                code = result.get('error_code', 0)
                retry = code == 429 or code >= 500
                attempts = event['attempts'] + 1
                wait = max(int((result.get('parameters') or {}).get('retry_after',0)), min(3600, 30 * 2**min(attempts,7)))
                con.execute('UPDATE notification_outbox SET state=?,next_attempt=?,error=? WHERE event_id=?', ('pending' if retry and attempts < 8 else 'rejected', int(time.time())+wait,f'Telegram refusal {code}',event['event_id']))
            else:
                con.execute("UPDATE notification_outbox SET state='ambiguous',error='missing receipt' WHERE event_id=?", (event['event_id'],))
        con.commit()


def reconcile(con, event_id, message_id):
    event = con.execute("SELECT * FROM notification_outbox WHERE event_id=? AND state='ambiguous'", (event_id,)).fetchone()
    if not event:
        raise ValueError('event is not ambiguous')
    token, chat = credentials()
    # Operator supplies the observed message ID. Editing the same message validates
    # receipt without creating another message or another getUpdates consumer.
    result = telegram_request(token, {'chat_id':chat,'message_id':message_id,'text':event['text'], 'parse_mode':'HTML'}, 'editMessageText')
    valid = result.get('ok') or (result.get('error_code') == 400 and 'message is not modified' in result.get('description',''))
    if not valid:
        raise ValueError('receipt not verified')
    con.execute("UPDATE notification_outbox SET state='delivered',message_id=?,delivered_at=?,error=NULL WHERE event_id=?", (message_id,int(time.time()),event_id));con.commit()


def resource_check(path, min_free=4*1024**3, min_mem=100*1024**2):
    if Path(path) == ROOT/'staging' and sum(p.stat().st_size for p in Path(path).rglob('*') if p.is_file()) > 6*1024**3:
        raise RuntimeError('resource guard: staging exceeds 6 GiB')
    if shutil.disk_usage(path).free < min_free:
        raise RuntimeError('resource guard: low disk space')
    mem = dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
    if int(mem['MemAvailable'].strip().split()[0])*1024 < min_mem:
        raise RuntimeError('resource guard: low available RAM')


def snapshot(con, destination):
    from catalog_access import file_lock
    with file_lock(Path(destination).with_suffix('.lock')):
        return _snapshot(con, destination)


def _snapshot(con, destination):
    destination = Path(destination)
    temporary = destination.with_suffix('.partial')
    temporary.unlink(missing_ok=True)
    copy = sqlite3.connect(temporary)
    con.backup(copy)
    assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not copy.execute('PRAGMA foreign_key_check').fetchall()
    copy.close();temporary.chmod(0o600)
    with temporary.open('rb') as stream: os.fsync(stream.fileno())
    os.replace(temporary,destination); fsync_dir(destination.parent)


def fsync_dir(path):
    fd = os.open(path,os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def queue(con):
    return [dict(r) for r in con.execute("SELECT id,tweet_id,file_path,sha256,size_bytes,final_filename FROM downloads WHERE status='downloaded' AND storage_state='staged' ORDER BY id")]


def ack(con, download_id, sha256, size, archive_path):
    row = con.execute('SELECT * FROM downloads WHERE id=?',(download_id,)).fetchone()
    path = Path(archive_path)
    if not row or row['status'] != 'downloaded' or row['sha256'] != sha256 or row['size_bytes'] != size:
        raise ValueError('ACK identity/hash/size mismatch')
    if path.parent != ARCHIVE_ROOT/'videos' or path.name != f'{row["id"]}-{sha256}.mp4':
        raise ValueError('ACK outside canonical archive')
    if row['storage_state'] == 'archived':
        if row['archive_path'] != archive_path: raise ValueError('ACK location changed')
    elif row['storage_state'] == 'staged':
        con.execute("UPDATE downloads SET storage_state='archived',archive_path=?,archive_ack_at=? WHERE id=?",(archive_path,int(time.time()),download_id));con.commit()
    else: raise ValueError('ACK requires staged video')
    # Persist ACK before deleting; re-running after a crash safely finishes deletion.
    staged = Path(row['file_path'])
    if staged.parent != ROOT/'staging': raise ValueError('refuse cleanup outside staging')
    if staged.exists():
        if digest(staged) != sha256: raise ValueError('staging changed after ACK')
        staged.unlink();fsync_dir(staged.parent)


def cleanup_acked(con):
    for row in con.execute("SELECT * FROM downloads WHERE storage_state='archived'").fetchall():
        ack(con,row['id'],row['sha256'],row['size_bytes'],row['archive_path'])


def require_mount(mount):
    mounted = subprocess.check_output(['findmnt','-rn','-T',str(mount),'-o','TARGET'],text=True).strip()
    if mounted != str(mount) or not os.path.ismount(mount):
        raise RuntimeError('Seagate is not mounted at the required mountpoint')
    resource_check(mount,min_free=1024**3,min_mem=0)


def transfer_verified(source, final, expected_hash, expected_size, copier):
    final = Path(final);partial=final.with_suffix('.partial')
    if final.exists():
        if final.stat().st_size != expected_size or digest(final) != expected_hash:
            raise ValueError('existing archive file mismatches; preserve it')
        return
    copier(source,partial)
    if partial.stat().st_size != expected_size or digest(partial) != expected_hash:
        raise ValueError('archive transfer verification failed')
    partial.chmod(0o600)
    with partial.open('rb') as stream: os.fsync(stream.fileno())
    os.replace(partial,final);fsync_dir(final.parent)


def remote(host, action, payload=None):
    cmd=['ssh','-F',str(Path.home()/'.ssh/config'),'-o','BatchMode=yes','-o','ConnectTimeout=15',host,'sudo','-n','-u','livinggaul','/srv/livinggaul/venv/bin/python','/srv/livinggaul/repo/livinggaul_pipeline.py',action]
    result=subprocess.run(cmd,input=json.dumps(payload or {}),text=True,capture_output=True,timeout=180,check=True)
    return json.loads(result.stdout or '{}')


def pull(host, mount):
    require_mount(mount)
    for directory in (ARCHIVE_ROOT/'videos',ARCHIVE_ROOT/'recovery'):
        directory.mkdir(parents=True,exist_ok=True,mode=0o700);directory.chmod(0o700)
    for row in remote(host,'queue'):
        require_mount(mount)
        if Path(row['file_path']).parent != ROOT/'staging' or not row['sha256'] or not row['size_bytes']:
            raise ValueError('invalid remote staging manifest')
        final=ARCHIVE_ROOT/'videos'/f'{row["id"]}-{row["sha256"]}.mp4'
        def copier(source,target):
            subprocess.run(['rsync','--partial','--append-verify','--protect-args','--rsync-path=sudo -n -u livinggaul rsync','-e','ssh -F ' + shlex.quote(str(Path.home()/'.ssh/config')) + ' -o BatchMode=yes -o ConnectTimeout=15',f'{host}:{source}',str(target)],check=True,timeout=1800)
        transfer_verified(row['file_path'],final,row['sha256'],row['size_bytes'],copier)
        require_mount(mount)
        remote(host,'ack',{'id':row['id'],'sha256':row['sha256'],'size':row['size_bytes'],'path':str(final)})
    remote(host,'snapshot')
    recovery=ARCHIVE_ROOT/'recovery'
    subprocess.run(['rsync','--protect-args','--rsync-path=sudo -n -u livinggaul rsync','-e','ssh -F ' + shlex.quote(str(Path.home()/'.ssh/config')) + ' -o BatchMode=yes',f'{host}:{ROOT}/export/catalog.sqlite',str(recovery/'catalog.partial')],check=True,timeout=180)
    verify=sqlite3.connect(recovery/'catalog.partial');verify.row_factory=sqlite3.Row
    assert verify.execute('pragma integrity_check').fetchone()[0]=='ok'
    assert not verify.execute('pragma foreign_key_check').fetchall()
    manifest=[]
    for row in verify.execute("SELECT * FROM downloads WHERE status='downloaded' AND storage_state='archived'"):
        if not archived_valid(row) or digest(row['archive_path']) != row['sha256']:
            raise ValueError('recovery references invalid archive file')
        manifest.append({'id':row['id'],'path':row['archive_path'],'sha256':row['sha256'],'bytes':row['size_bytes']})
    verify.close();os.replace(recovery/'catalog.partial',recovery/'catalog.sqlite')
    (recovery/'manifest.partial').write_text(json.dumps(manifest,indent=2));(recovery/'manifest.partial').chmod(0o600)
    os.replace(recovery/'manifest.partial',recovery/'manifest.json');fsync_dir(recovery)
    subprocess.run(['rsync','--protect-args','--rsync-path=sudo -n -u livinggaul rsync','-e','ssh -F ' + shlex.quote(str(Path.home()/'.ssh/config')) + ' -o BatchMode=yes',f'{host}:{ROOT}/.local/state/livinggaul-x-downloader/yt-dlp-download-archive.txt',str(recovery/'yt-dlp-download-archive.txt')],check=True,timeout=180)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['queue','ack','snapshot','notify','test-notify','reconcile','pull'])
    parser.add_argument('--host',default='oracle-vm');parser.add_argument('--mount',type=Path,default=Path('/mnt/Seagate6TB'));parser.add_argument('--event');parser.add_argument('--message-id',type=int)
    args=parser.parse_args();os.umask(0o077)
    if args.action=='pull':
        lock=Path.home()/'.local/state/livinggaul-x-downloader/pull.lock'
        with lock.open('a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB);pull(args.host,args.mount)
        return
    coordination = 'notification.lock' if args.action in ('notify','test-notify','reconcile') else 'runtime.lock'
    with (ROOT/coordination).open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        con=sqlite3.connect(DB,timeout=60);con.row_factory=sqlite3.Row;con.execute('pragma foreign_keys=on');migrate(con);con.commit()
        if args.action=='queue': cleanup_acked(con);print(json.dumps(queue(con)))
        elif args.action=='ack':
            p=json.load(__import__('sys').stdin);ack(con,p['id'],p['sha256'],p['size'],p['path']);print('{"ack":true}')
        elif args.action=='snapshot':
            export=ROOT/'export';export.mkdir(exist_ok=True,mode=0o700);snapshot(con,export/'catalog.sqlite');print('{"snapshot":true}')
        elif args.action=='reconcile':reconcile(con,args.event,args.message_id)
        else:
            if args.action=='test-notify':
                row=con.execute("SELECT * FROM downloads WHERE status='downloaded' ORDER BY id LIMIT 1").fetchone()
                meta=con.execute("SELECT metadata_json FROM metadata_snapshots WHERE download_id=?",(row['id'],)).fetchone()
                enqueue(con,'P4:test',video_message(row,json.loads(meta[0]) if meta else {},'[TEST] P4 — '),row['id']);con.commit()
            if args.action == 'notify':
                try: resource_check(ROOT/'staging')
                except RuntimeError as exc:
                    enqueue(con,'resource:' + time.strftime('%Y%m%d%H',time.gmtime()),'LivingGaul sospeso: ' + str(exc));con.commit()
                for unit in ('livinggaul-download.service','livinggaul-source.service'):
                    state=subprocess.run(['systemctl','is-failed',unit],text=True,capture_output=True)
                    if state.stdout.strip() == 'failed':
                        enqueue(con,'failure:' + unit + ':' + time.strftime('%Y%m%d%H',time.gmtime()),'LivingGaul: errore servizio ' + unit);con.commit()
            drain(con)
            export=ROOT/'export';export.mkdir(exist_ok=True,mode=0o700)
            snapshot(con,export/'catalog.sqlite')
            print(json.dumps([dict(r) for r in con.execute('SELECT event_id,state,message_id,error FROM notification_outbox')]))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        # Safe errors only, never raw network exception/credential URL.
        print(f'pipeline failed: {type(exc).__name__}',file=__import__('sys').stderr);raise SystemExit(1)
