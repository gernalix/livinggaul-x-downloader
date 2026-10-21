"""Process-level acceptance on temporary data only; never contacts a source site."""
import argparse
import hashlib
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import video_engine as engine
from catalog_access import file_lock, lock_path


def worker(db, output, source, barrier, events, payload=b'same fixture video'):
    con = engine.connect_db(Path(db))
    candidate = engine.Candidate('1234567890123456789' if source == 'x' else None,
        'https://x.com/fixture/status/1234567890123456789' if source == 'x' else 'https://example.org/video',
        source=source, source_context='987' if source == 'telegram' else '')
    options = argparse.Namespace(output_dir=Path(output), dry_run=False, retry_failed=True,
                                 cookies_mode='none', firefox_profile=None)
    def transfer(command, **kwargs):
        events.put(('transferring', source))
        barrier.wait(15)
        target = Path(command[command.index('--output') + 1].replace('%(ext)s', 'mp4'))
        target.write_bytes(payload)
        from subprocess import CompletedProcess
        return CompletedProcess(command, 0, '', '')
    hooks = {'extract_metadata': lambda *a: {'title': source, 'ext': 'mp4', 'url': 'https://example.org/file.mp4'},
             'run': transfer, 'log': lambda *a: None}
    summary = engine.Summary()
    with patch.object(engine, 'tool_path', return_value='fixture-tool'), \
            patch.object(engine, 'ensure_max_480', side_effect=lambda path, log: (path, '480p')), \
            patch.object(engine, 'ffprobe_file_metadata', return_value={'duration_ms': 1000, 'width': 640, 'height': 480}), \
            patch.object(engine, 'video_content_fingerprint', return_value=None):
        engine.download_one(options, con, candidate, None, summary, hooks=hooks)
    events.put(('done', source, summary.downloaded, summary.skipped_duplicate, summary.failed))
    con.close()


class SharedArchiveTests(unittest.TestCase):
    def test_parallel_inputs_single_canonical_file_and_both_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); db = root / 'catalog.sqlite'; output = root / 'staging'; output.mkdir()
            engine.connect_db(db).close()
            ctx = multiprocessing.get_context('spawn')
            barrier = ctx.Barrier(3); events = ctx.Queue()
            workers = [ctx.Process(target=worker, args=(str(db), str(output), source, barrier, events))
                       for source in ('x', 'telegram')]
            for process in workers: process.start()
            try:
                started = [events.get(timeout=15), events.get(timeout=15)]
                self.assertEqual({item[:2] for item in started}, {('transferring','x'),('transferring','telegram')})
                # Both transfers are live while another writer commits and readers see it.
                with file_lock(lock_path(db)):
                    con = sqlite3.connect(db, timeout=1)
                    con.execute("INSERT INTO notification_outbox(event_id,text,created_at) VALUES('fixture','fixture',0)")
                    con.commit(); con.close()
                barrier.wait(15)
                finished = [events.get(timeout=15), events.get(timeout=15)]
                self.assertTrue(all(row[0] == 'done' and row[-1] == 0 for row in finished), finished)
                self.assertEqual(sum(row[2] for row in finished), 1)
                self.assertEqual(sum(row[3] for row in finished), 1)
            finally:
                for process in workers:
                    process.join(timeout=15)
                    if process.is_alive(): process.terminate(); process.join()
                events.close()
            self.assertTrue(all(p.exitcode == 0 for p in workers))
            con = engine.connect_db(db)
            self.assertEqual(con.execute("SELECT count(*) FROM downloads WHERE status='downloaded'").fetchone()[0], 1)
            duplicate = con.execute("SELECT duplicate_of_id FROM downloads WHERE status='skipped_duplicate'").fetchone()
            canonical = con.execute("SELECT * FROM downloads WHERE status='downloaded'").fetchone()
            self.assertEqual(duplicate[0], canonical['id'])
            self.assertEqual(Path(canonical['file_path']).read_bytes(), b'same fixture video')
            self.assertEqual(len(list(output.glob('*.mp4'))), 1)
            self.assertEqual(con.execute('SELECT count(*) FROM metadata_snapshots').fetchone()[0], 2)
            self.assertEqual({r[0] for r in con.execute('SELECT input FROM video_origins')}, {'x','telegram'})
            self.assertEqual({r[0] for r in con.execute('SELECT video_id FROM video_origins')}, {canonical['id']})
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])
            con.close()

    def test_migration_keeps_historical_rows_ids_snapshots_and_archive_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'catalog.sqlite'; con = engine.connect_db(db)
            c = engine.Candidate('1234567890123456789','https://x.com/fixture/status/1234567890123456789')
            engine.upsert_status(con,c,'downloaded',file_path='/original/staging/file.mp4',sha256='a'*64,
                size_bytes=123,source_metadata={'title':'Historical','telegram_chat_id':987,'extra':{'keep':[1,2]}})
            con.execute("UPDATE downloads SET storage_state='archived',archive_path='/original/archive/file.mp4',archive_ack_at=123")
            con.execute('DROP VIEW video_origins');con.execute('DROP TABLE acquisition_sources');con.execute('PRAGMA user_version=5');con.commit()
            before = {table:[tuple(row) for row in con.execute('SELECT * FROM '+table)]
                      for table in ('downloads','metadata_snapshots','notification_outbox')};con.close()
            for _ in range(2):
                con = engine.connect_db(db)
                for table, rows in before.items():
                    self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM '+table)], rows)
                self.assertEqual(tuple(con.execute('SELECT input,context FROM acquisition_sources').fetchone()),('telegram','987'))
                self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(),[])
                con.close()

    def test_same_url_can_have_x_and_telegram_provenance_without_redownload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); output=root/'staging';output.mkdir(); media=output/'historical.mp4';media.write_bytes(b'old')
            con=engine.connect_db(root/'catalog.sqlite')
            x=engine.Candidate('1234567890123456789','https://x.com/fixture/status/1234567890123456789',source='x')
            engine.upsert_status(con,x,'downloaded',file_path=str(media),sha256=hashlib.sha256(b'old').hexdigest(),size_bytes=3)
            tg=engine.Candidate(x.tweet_id,x.url,source='telegram',source_context='987')
            options=argparse.Namespace(output_dir=output,dry_run=False,retry_failed=True)
            summary=engine.Summary()
            engine.download_one(options,con,tg,None,summary,hooks={'extract_metadata':lambda *a:self.fail('must not download again'),'log':lambda *a:None})
            self.assertEqual(summary.skipped_duplicate,1)
            self.assertEqual({r[0] for r in con.execute('SELECT input FROM acquisition_sources')},{'x','telegram'})
            self.assertEqual(media.read_bytes(),b'old');con.close()

    def test_interrupted_transfer_is_preserved_and_reused(self):
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); output=root/'staging';output.mkdir();con=engine.connect_db(root/'catalog.sqlite')
            candidate=engine.Candidate(None,'https://example.org/fixture',source='telegram')
            options=argparse.Namespace(output_dir=output,dry_run=False,retry_failed=True,cookies_mode='none',firefox_profile=None)
            metadata=lambda *a:{'title':'Fixture','ext':'mp4','url':'https://example.org/file.mp4'}
            paths=[]
            def failed(command, **kwargs):
                target=Path(command[command.index('--output')+1].replace('%(ext)s','mp4'))
                paths.append(target); target.with_suffix('.part').write_bytes(b'partial')
                return CompletedProcess(command,1,'','fixture interruption')
            def resumed(command, **kwargs):
                target=Path(command[command.index('--output')+1].replace('%(ext)s','mp4'))
                self.assertEqual(target,paths[0]);self.assertEqual(target.with_suffix('.part').read_bytes(),b'partial')
                target.write_bytes(b'complete');target.with_suffix('.part').unlink()
                return CompletedProcess(command,0,'','')
            with patch.object(engine,'tool_path',return_value='fixture'), \
                    patch.object(engine,'ensure_max_480',side_effect=lambda path,log:(path,'480p')), \
                    patch.object(engine,'ffprobe_file_metadata',return_value={}), \
                    patch.object(engine,'video_content_fingerprint',return_value=None):
                summary=engine.Summary()
                engine.download_one(options,con,candidate,None,summary,hooks={'extract_metadata':metadata,'run':failed,'log':lambda *a:None})
                self.assertEqual(summary.failed,1)
                self.assertTrue(paths[0].with_suffix('.part').exists())
                summary=engine.Summary()
                engine.download_one(options,con,candidate,None,summary,hooks={'extract_metadata':metadata,'run':resumed,'log':lambda *a:None})
                self.assertEqual(summary.downloaded,1)
            self.assertEqual(con.execute('SELECT count(*) FROM downloads').fetchone()[0],1);con.close()

    def test_non_x_metadata_failure_does_not_call_x_fallback(self):
        from subprocess import CompletedProcess
        candidate=engine.Candidate(None,'https://example.org/fixture',source='telegram')
        options=argparse.Namespace(cookies_mode='none',firefox_profile=None)
        with patch.object(engine,'tool_path',return_value='fixture'), \
                patch.object(engine,'run',return_value=CompletedProcess([],1,'','fixture refusal')), \
                patch.object(engine,'extract_fxtwitter_metadata') as fallback:
            with self.assertRaisesRegex(RuntimeError,'fixture refusal'):
                engine.extract_metadata(options,candidate,None)
            fallback.assert_not_called()

    def test_telegram_x_link_keeps_its_url_channel_and_message(self):
        import telegram_video_downloader as telegram
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); output=root/'staging';output.mkdir();media=output/'historical.mp4';media.write_bytes(b'old')
            con=engine.connect_db(root/'catalog.sqlite')
            original=engine.Candidate('1234567890123456789','https://x.com/LivingGaul/status/1234567890123456789',source='x')
            engine.upsert_status(con,original,'downloaded',file_path=str(media),sha256=hashlib.sha256(b'old').hexdigest(),size_bytes=3)
            url='https://x.com/another_author/status/1234567890123456789'
            candidate=telegram.candidates(vars(engine),[url])[0]
            self.assertEqual(candidate.url,url)
            candidate=replace(candidate,source_context='987',source_references=('42','43'))
            summary=engine.Summary();options=argparse.Namespace(output_dir=output,dry_run=False,retry_failed=True)
            engine.download_one(options,con,candidate,None,summary,hooks={'extract_metadata':lambda *a:self.fail('must not redownload'),'log':lambda *a:None})
            self.assertEqual(summary.skipped_duplicate,1)
            refs=[tuple(r) for r in con.execute("SELECT original_url,context,reference FROM video_origins WHERE input='telegram' ORDER BY reference")]
            self.assertEqual(refs,[(url,'987','42'),(url,'987','43')])
            self.assertEqual(con.execute('SELECT count(*) FROM video_library').fetchone()[0],1)
            self.assertEqual(media.read_bytes(),b'old');con.close()


if __name__ == '__main__': unittest.main()
