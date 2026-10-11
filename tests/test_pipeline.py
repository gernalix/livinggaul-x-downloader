import json
import os
from pathlib import Path
import runpy
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import livinggaul_pipeline as p

M=runpy.run_path(str(Path(__file__).resolve().parents[1]/'livinggaul-x-downloader'))

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.con=M['connect_db'](self.root/'catalog.sqlite')
    def tearDown(self):
        self.con.close();self.tmp.cleanup()
    def video(self):
        source=self.root/'video.mp4';source.write_bytes(b'video')
        candidate=M['Candidate']('1234567890123456789','https://x.com/LivingGaul/status/1234567890123456789')
        M['upsert_status'](self.con,candidate,'downloaded',file_path=str(source),sha256=p.digest(source),size_bytes=5,media_key='asset:1',content_fingerprint='dhash16-v1|'+'|'.join(['0'*64+':80:20']*5),duration_ms=1000,direct_video_url='https://video.twimg.com/a.mp4?foo=1&bar=2')
        return candidate,self.con.execute('select * from downloads').fetchone()
    def test_idempotent_migration_and_restore(self):
        self.video();p.migrate(self.con);p.migrate(self.con);self.con.commit()
        p.snapshot(self.con,self.root/'copy.sqlite');copy=sqlite3.connect(self.root/'copy.sqlite')
        self.assertEqual(copy.execute('select count(*) from downloads').fetchone()[0],1)
        self.assertEqual(copy.execute('pragma integrity_check').fetchone()[0],'ok');copy.close()
    def test_archived_dedup_all_identifiers_without_local_file(self):
        candidate,row=self.video();Path(row['file_path']).unlink()
        self.con.execute("UPDATE downloads SET storage_state='archived',archive_path=?,archive_ack_at=1",(str(p.ARCHIVE_ROOT/'videos'/f"{row['id']}-{row['sha256']}.mp4"),));self.con.commit()
        self.assertIsNotNone(M['already_downloaded'](self.con,candidate))
        self.assertIsNotNone(M['downloaded_by_media_key'](self.con,'asset:1'))
        self.assertIsNotNone(M['downloaded_by_sha256'](self.con,row['sha256']))
        self.assertIsNotNone(M['downloaded_by_fingerprint'](self.con,row['content_fingerprint'],1000))
        self.assertEqual(M['reindex_content'](self.con,None),(1,0,0,0))
        self.con.execute('update downloads set archive_path=NULL');self.assertIsNone(M['already_downloaded'](self.con,candidate))
    def test_transaction_staging_event_rolls_back_together(self):
        _,row=self.video();p.record_staged(self.con,row,{})
        self.con.rollback()
        self.assertIsNone(self.con.execute('select storage_state from downloads').fetchone()[0])
        self.assertEqual(self.con.execute('select count(*) from notification_outbox').fetchone()[0],0)
    def test_no_historical_event_and_unique_event(self):
        _,row=self.video();self.assertEqual(self.con.execute('select count(*) from notification_outbox').fetchone()[0],0)
        p.record_staged(self.con,row,{});p.record_staged(self.con,row,{});self.con.commit()
        self.assertEqual(self.con.execute('select count(*) from notification_outbox').fetchone()[0],1)
    def test_receipt_restart_no_duplicate_send(self):
        _,row=self.video();p.record_staged(self.con,row,{});self.con.commit();calls=[]
        def send(*a):calls.append(a);return {'ok':True,'result':{'message_id':42}}
        p.drain(self.con,send,('fake','fake'));p.drain(self.con,send,('fake','fake'))
        self.assertEqual(len(calls),1);self.assertEqual(self.con.execute('select message_id from notification_outbox').fetchone()[0],42)
    def test_ambiguous_send_never_retried(self):
        p.enqueue(self.con,'e','text');self.con.commit();calls=[]
        def send(*a):calls.append(a);raise TimeoutError()
        p.drain(self.con,send,('fake','fake'));p.drain(self.con,send,('fake','fake'))
        self.assertEqual(len(calls),1);self.assertEqual(self.con.execute('select state from notification_outbox').fetchone()[0],'ambiguous')
    def test_crash_claim_becomes_ambiguous(self):
        p.enqueue(self.con,'e','text');self.con.execute("update notification_outbox set state='sending'");self.con.commit()
        p.drain(self.con,lambda *a:self.fail('must not send'),('fake','fake'))
        self.assertEqual(self.con.execute('select state from notification_outbox').fetchone()[0],'ambiguous')
    def test_certain_rate_limit_bounded_retry(self):
        p.enqueue(self.con,'e','text');self.con.commit()
        p.drain(self.con,lambda *a:{'ok':False,'error_code':429,'parameters':{'retry_after':123}},('fake','fake'))
        row=self.con.execute('select * from notification_outbox').fetchone()
        self.assertEqual(row['state'],'pending');self.assertGreater(row['next_attempt'],row['created_at']+120)
    def test_links_quote_and_html_escape(self):
        _,row=self.video();text=p.video_message(row,{'quoted_source_url':'https://x.com/example/status/1'})
        self.assertIn('https://x.com/LivingGaul/status/',text);self.assertIn('MP4 diretto',text);self.assertIn('&amp;',text);self.assertIn('Post sorgente quoted',text)
        self.assertNotIn('evil.test',p.video_message(row,{'quoted_source_url':'https://evil.test/'}))
    def test_partial_transfer_failure_preserves_source_no_final(self):
        source=self.root/'source';source.write_bytes(b'12345');final=self.root/'final.mp4'
        def fail(src,target):target.write_bytes(b'123');raise OSError('network')
        with self.assertRaises(OSError):p.transfer_verified(source,final,p.digest(source),5,fail)
        self.assertTrue(source.exists());self.assertFalse(final.exists())
        p.transfer_verified(source,final,p.digest(source),5,lambda a,b:shutil.copyfile(a,b))
        p.transfer_verified(source,final,p.digest(source),5,lambda a,b:self.fail('duplicate transfer'))
        self.assertEqual(final.read_bytes(),b'12345')
    def test_bad_hash_never_renames(self):
        source=self.root/'source';source.write_bytes(b'12345');final=self.root/'final.mp4'
        with self.assertRaises(ValueError):p.transfer_verified(source,final,'0'*64,5,lambda a,b:shutil.copyfile(a,b))
        self.assertFalse(final.exists());self.assertTrue(source.exists())
    def test_ack_commits_before_delete_and_restart_finishes(self):
        _,row=self.video();stage=self.root/'staging';stage.mkdir();target=stage/'video.mp4';shutil.move(row['file_path'],target)
        self.con.execute("update downloads set storage_state='staged',file_path=?",(str(target),));self.con.commit()
        archive=str(p.ARCHIVE_ROOT/'videos'/f"{row['id']}-{row['sha256']}.mp4")
        with patch.object(p,'ROOT',self.root),patch.object(Path,'unlink',side_effect=OSError('crash')):
            with self.assertRaises(OSError):p.ack(self.con,row['id'],row['sha256'],5,archive)
        self.assertEqual(self.con.execute('select storage_state from downloads').fetchone()[0],'archived');self.assertTrue(target.exists())
        with patch.object(p,'ROOT',self.root):p.ack(self.con,row['id'],row['sha256'],5,archive)
        self.assertFalse(target.exists())
        with self.assertRaises(ValueError):p.ack(self.con,row['id'],'wrong',5,archive)
    def test_missing_mount_and_low_space_fail_closed(self):
        with patch('subprocess.check_output',return_value='/\n'):
            with self.assertRaises(RuntimeError):p.require_mount(Path('/mnt/Seagate6TB'))
        with patch('shutil.disk_usage',return_value=shutil._ntuple_diskusage(100,99,1)):
            with self.assertRaises(RuntimeError):p.resource_check(self.root,min_mem=0)

if __name__=='__main__':unittest.main()
