import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class FakeDownloader:
    transfers = 0
    fail = False

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def extract_info(self, url, download=False):
        assert download is False
        return {"id": url.split('?')[0].rsplit('/', 1)[-1], "extractor_key": "Example",
                "title": "Sample", "uploader": "Creator", "ext": "mp4"}

    def prepare_filename(self, info):
        return self.opts['outtmpl'].replace('%(ext)s', 'mp4')

    def process_ie_result(self, info, download=True):
        if self.fail:
            raise RuntimeError('Test transfer failure')
        type(self).transfers += 1
        Path(self.prepare_filename(info)).write_bytes(info['id'].replace('alias', 'one').encode())
        return info


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(app, 'HISTORY_DB', self.root / 'history.sqlite3'),
                        patch.object(app, 'CURRENT_OUTPUT_DIR', self.root),
                        patch.object(app.yt_dlp, 'YoutubeDL', FakeDownloader)]
        for p in self.patches:
            p.start()
        app.JOBS.clear()
        FakeDownloader.transfers = 0
        FakeDownloader.fail = False

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def prepare(self, job_id, niche='Du lịch', url='https://example.com/one'):
        name, key = app.normalize_niche(niche)
        app.JOBS[job_id] = dict(status='queued', percent=0, url=url, niche=name,
                               niche_key=key, output_dir=str(app.niche_directory(self.root, name, key)))
        return url

    def run_job(self, job_id, niche='Du lịch', url='https://example.com/one'):
        app.run_download(job_id, self.prepare(job_id, niche, url))
        return app.JOBS[job_id]['status']

    def test_history_persists_and_niche_is_case_insensitive(self):
        self.assertEqual(self.run_job('a'), 'done')
        app.JOBS.clear()
        self.assertEqual(self.run_job('b', ' DU LỊCH ', 'https://example.com/one?tracking=2'), 'duplicate')
        self.assertEqual(FakeDownloader.transfers, 1)

    def test_other_niche_is_blocked_globally(self):
        self.assertEqual(self.run_job('a'), 'done')
        self.assertEqual(self.run_job('b', 'Động vật'), 'duplicate')
        self.assertEqual(app.JOBS['b']['duplicate_niche'], 'Du lịch')
        self.assertEqual(app.JOBS['a']['filepath'], app.JOBS['b']['filepath'])
        self.assertEqual(FakeDownloader.transfers, 1)

    def test_chung_does_not_check_against_other_niches(self):
        self.assertEqual(self.run_job('a', niche='Chung'), 'done')
        self.assertEqual(self.run_job('b', niche='Du lịch'), 'done')
        self.assertEqual(FakeDownloader.transfers, 2)
        self.assertNotEqual(app.JOBS['a']['filepath'], app.JOBS['b']['filepath'])

        self.assertEqual(self.run_job('c', niche='Động vật'), 'duplicate')
        self.assertEqual(app.JOBS['c']['duplicate_niche'], 'Du lịch')
        self.assertEqual(self.run_job('d', niche='Chung'), 'duplicate')
        self.assertEqual(app.JOBS['d']['duplicate_niche'], 'Chung')
        self.assertEqual(FakeDownloader.transfers, 2)

    def test_named_niche_does_not_check_against_chung_by_digest(self):
        self.assertEqual(self.run_job('a', niche='Chung', url='https://example.com/one'), 'done')
        self.assertEqual(self.run_job('b', niche='Du lịch', url='https://example.com/alias'), 'done')
        self.assertEqual(len(list(self.root.rglob('*.mp4'))), 2)

    def test_grouped_text_parser_supports_markdown_links(self):
        entries = app.parse_download_text(
            '1. Đeo bám\\\n'
            '   [https://example.com/one](https://example.com/one)\\\n'
            '   https://example.com/two\\\n'
            '2. Âu yếm\\\n'
            '   [https://example.com/three](https://example.com/three)'
        )
        self.assertEqual(entries, [
            {'url': 'https://example.com/one', 'group': '1. Đeo bám'},
            {'url': 'https://example.com/two', 'group': '1. Đeo bám'},
            {'url': 'https://example.com/three', 'group': '2. Âu yếm'},
        ])

    def test_grouped_api_creates_subfolders_and_plain_list_stays_normal(self):
        client = app.app.test_client()
        grouped = '1. Đeo bám\nhttps://example.com/one\n2. Âu yếm\nhttps://example.com/two'
        with patch.object(app.threading, 'Thread'):
            response = client.post('/api/download', json={
                'text': grouped, 'niche': 'Chung', 'prevent_duplicates': True,
            })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual([job['group'] for job in response.json['jobs']], ['1. Đeo bám', '2. Âu yếm'])
        output_dirs = [Path(app.JOBS[job['job_id']]['output_dir']) for job in response.json['jobs']]
        self.assertEqual([path.name for path in output_dirs], ['1. Đeo bám', '2. Âu yếm'])
        self.assertEqual(output_dirs[0].parent, self.root)
        self.assertTrue(all(path.is_dir() for path in output_dirs))

        with patch.object(app.threading, 'Thread'):
            plain = client.post('/api/download', json={
                'text': 'https://example.com/three', 'prevent_duplicates': False,
            })
        plain_job = plain.json['jobs'][0]
        self.assertIsNone(plain_job['group'])
        self.assertEqual(Path(app.JOBS[plain_job['job_id']]['output_dir']), self.root)

    def test_retry_keeps_group_subfolder(self):
        client = app.app.test_client()
        with patch.object(app.threading, 'Thread'):
            response = client.post('/api/download', json={
                'text': '1. Đeo bám\nhttps://example.com/one',
                'niche': 'Chung',
            })
            old_id = response.json['job_ids'][0]
            app.JOBS[old_id]['status'] = 'error'
            retried = client.post('/api/retry', json={'job_ids': [old_id]})
        new_id = retried.json['jobs'][0]['job_id']
        self.assertEqual(app.JOBS[new_id]['group'], '1. Đeo bám')
        self.assertEqual(app.JOBS[new_id]['output_dir'], app.JOBS[old_id]['output_dir'])
        self.assertEqual(app.JOBS[new_id]['source_url'], app.JOBS[old_id]['url'])
        self.assertEqual(app.JOBS[new_id]['source_order'], app.JOBS[old_id]['source_order'])

    def test_export_links_groups_and_naturally_sorts_folders(self):
        jobs = [
            ('a', '10. Leo trèo', 'https://example.com/ten', 3),
            ('b', '2. Âu yếm', 'https://example.com/two', 2),
            ('c', '1. Hài hước', 'https://example.com/one', 1),
            ('d', '1. Hài hước', 'https://example.com/three', 4),
        ]
        for job_id, group, url, source_order in jobs:
            self.prepare(job_id, niche='Du lịch', url=url)
            app.JOBS[job_id].update(
                group=group, source_url=url, submitted_at=100,
                source_order=source_order,
                output_dir=str(self.root / group),
            )
            app.run_download(job_id, url)

        response = app.app.test_client().get('/api/export_links?niche=Du%20lịch')
        self.assertEqual(response.status_code, 200, response.json)
        content = response.data.decode('utf-8-sig')
        self.assertLess(content.index('1. Hài hước'), content.index('2. Âu yếm'))
        self.assertLess(content.index('2. Âu yếm'), content.index('10. Leo trèo'))
        self.assertLess(content.index('https://example.com/one'), content.index('https://example.com/three'))
        self.assertIn('attachment;', response.headers['Content-Disposition'])

    def test_export_excludes_legacy_rows_without_source_url(self):
        with app.history_connection() as conn:
            conn.execute("""INSERT INTO downloads
                (niche_key,niche,video_key,title,filepath,digest)
                VALUES (?,?,?,?,?,?)""",
                ('du lịch', 'Du lịch', 'example:legacy', 'Legacy', 'legacy.mp4', 'abc'))
        response = app.app.test_client().get('/api/export_links?niche=Du%20lịch')
        self.assertEqual(response.status_code, 404)
        self.assertIn('bản cũ', response.json['error'])

    def test_group_folder_name_cannot_escape_parent(self):
        _, folder = app.normalize_group_folder('../CON')
        result = app.grouped_output_directory(self.root, '../CON')
        self.assertEqual(result.parent, self.root)
        self.assertNotIn('..', folder)

    def test_concurrent_same_video(self):
        threads = [threading.Thread(target=app.run_download, args=(str(i), self.prepare(str(i)))) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive())
        self.assertEqual(FakeDownloader.transfers, 1)
        self.assertEqual(sorted(j['status'] for j in app.JOBS.values()), ['done', 'duplicate', 'duplicate'])

    def test_equal_files_different_ids(self):
        self.run_job('a')
        self.assertEqual(self.run_job('b', niche='Động vật', url='https://example.com/alias'), 'duplicate')
        self.assertEqual(app.JOBS['b']['duplicate_niche'], 'Du lịch')
        self.assertEqual(len(list(self.root.rglob('*.mp4'))), 1)

    def test_distinct_videos_same_creator(self):
        self.run_job('a')
        self.assertEqual(self.run_job('b', url='https://example.com/two'), 'done')
        self.assertEqual(len(list(self.root.rglob('*.mp4'))), 2)

    def test_failure_does_not_record_success_and_retry_keeps_niche(self):
        FakeDownloader.fail = True
        self.assertEqual(self.run_job('a'), 'error')
        self.assertIsNone(app.find_download('example:one'))
        client = app.app.test_client()
        with patch.object(app.threading, 'Thread'):
            response = client.post('/api/retry', json={'job_ids': ['a']})
        new_id = response.json['jobs'][0]['job_id']
        self.assertEqual(app.JOBS[new_id]['output_dir'], app.JOBS['a']['output_dir'])
        self.assertEqual(app.JOBS[new_id]['niche'], 'Du lịch')
        FakeDownloader.fail = False
        app.run_download(new_id, app.JOBS[new_id]['url'])
        self.assertEqual(app.JOBS[new_id]['status'], 'done')

    def test_api_validation_snapshot_and_counts(self):
        client = app.app.test_client()
        self.assertEqual(client.post('/api/download', json={'urls': ['x'], 'niche': ''}).status_code, 400)
        with patch.object(app.threading, 'Thread'):
            response = client.post('/api/download', json={'urls': ['https://example.com/one'], 'niche': 'Du lịch'})
        job_id = response.json['job_ids'][0]
        self.assertTrue(app.JOBS[job_id]['prevent_duplicates'])
        app.CURRENT_OUTPUT_DIR = self.root / 'changed'
        app.run_download(job_id, app.JOBS[job_id]['url'])
        self.assertNotIn('changed', app.JOBS[job_id]['filepath'])
        self.assertEqual(client.get('/api/niches').json['niches'][0]['count'], 1)
        self.assertEqual(client.get('/').status_code, 200)

    def test_disabled_allows_same_id_and_same_content(self):
        self.run_job('a')
        for job_id, url in [('b', 'https://example.com/one'), ('c', 'https://example.com/alias')]:
            self.prepare(job_id, url=url)
            app.JOBS[job_id]['prevent_duplicates'] = False
            app.run_download(job_id, url)
            self.assertEqual(app.JOBS[job_id]['status'], 'done')
        self.assertEqual(len(list(self.root.rglob('*.mp4'))), 3)
        self.assertEqual(self.run_job('d'), 'duplicate')

    def test_disabled_download_does_not_touch_history(self):
        url = self.prepare('a')
        app.JOBS['a']['prevent_duplicates'] = False
        with patch.object(app, 'history_connection', side_effect=AssertionError('History must not be accessed')):
            app.run_download('a', url)
        self.assertEqual(app.JOBS['a']['status'], 'done')
        self.assertFalse(app.HISTORY_DB.exists())
        self.assertEqual(self.run_job('b'), 'done')

    def test_api_toggle_validation_and_retry(self):
        client = app.app.test_client()
        self.assertEqual(client.post('/api/download', json={'urls': ['x'], 'prevent_duplicates': 'false'}).status_code, 400)
        with patch.object(app.threading, 'Thread'):
            response = client.post('/api/download', json={'urls': ['https://example.com/one'], 'prevent_duplicates': False, 'niche': ''})
            job_id = response.json['job_ids'][0]
            self.assertFalse(app.JOBS[job_id]['prevent_duplicates'])
            self.assertIsNone(app.JOBS[job_id]['niche'])
            self.assertIsNone(app.JOBS[job_id]['niche_key'])
            self.assertEqual(app.JOBS[job_id]['output_dir'], str(self.root))
            app.JOBS[job_id]['status'] = 'error'
            response = client.post('/api/retry', json={'job_ids': [job_id]})
        new_id = response.json['jobs'][0]['job_id']
        self.assertFalse(app.JOBS[new_id]['prevent_duplicates'])
        with patch.object(app, 'history_connection', side_effect=AssertionError('History must not be accessed')):
            app.run_download(new_id, app.JOBS[new_id]['url'])
        self.assertEqual(Path(app.JOBS[new_id]['filepath']).parent, self.root)
        self.assertFalse(app.HISTORY_DB.exists())

    def test_create_niche_persists_before_download_and_rejects_duplicates(self):
        client = app.app.test_client()
        self.assertEqual(client.post('/api/niches', json={'name': '  Du lịch  '}).status_code, 201)
        self.assertEqual(client.get('/api/niches').json['niches'],
                         [{'name': 'Du lịch', 'niche_key': 'du lịch', 'count': 0}])
        self.assertEqual(client.post('/api/niches', json={'name': 'DU LỊCH'}).status_code, 409)
        self.assertEqual(client.post('/api/niches', json={'name': '   '}).status_code, 400)
        self.run_job('a')
        self.assertEqual(client.get('/api/niches').json['niches'][0]['count'], 1)

    def test_authentication_never_copies_live_chrome_cookie_database(self):
        social_urls = [
            'https://www.instagram.com/reel/example',
            'https://www.facebook.com/reel/example',
            'https://www.tiktok.com/@user/video/1',
            'https://www.douyin.com/video/1',
        ]
        with patch.object(app, 'cookie_file_for', return_value=None):
            for url in social_urls:
                self.assertEqual(app.authentication_attempts_for(url), [{}])
        with patch.object(app, 'cookie_file_for', return_value='cookies/exported.txt'):
            for url in social_urls:
                attempts = app.authentication_attempts_for(url)
                expected = ([{'cookiefile': 'cookies/exported.txt'}, {}]
                            if 'instagram.com' in url
                            else [{}, {'cookiefile': 'cookies/exported.txt'}])
                self.assertEqual(attempts, expected)
                self.assertTrue(all('cookiesfrombrowser' not in attempt for attempt in attempts))

    def test_instagram_rate_limit_errors_are_explained(self):
        message = app.clean_error_message(
            'ERROR: [Instagram] abc: Failed to parse JSON '
            '(caused by JSONDecodeError("Expecting value"))'
        )
        self.assertIn('giới hạn truy cập', message)
        self.assertIn('cookies/instagram.txt', message)

    def test_selected_niche_and_output_folder_persist(self):
        saved_folder = self.root / 'thu-muc-da-chon'
        saved_folder.mkdir()
        app.set_setting('output_dir', str(saved_folder))
        app.CURRENT_OUTPUT_DIR = self.root / 'tam-thoi'
        app.restore_saved_output_dir()
        self.assertEqual(app.CURRENT_OUTPUT_DIR, saved_folder)

        client = app.app.test_client()
        response = client.post('/api/settings', json={'selected_niche': 'Du lịch'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/api/settings').json['selected_niche'], 'Du lịch')
        self.assertEqual(client.get('/api/niches').json['selected_niche'], 'Du lịch')

    def test_delete_niche_removes_folder_files_and_history(self):
        self.assertEqual(self.run_job('a', niche='Du lịch'), 'done')
        downloaded = Path(app.JOBS['a']['filepath'])
        niche_folder = downloaded.parent
        self.assertTrue(downloaded.exists())

        client = app.app.test_client()
        client.post('/api/settings', json={'selected_niche': 'Du lịch'})
        response = client.delete('/api/niches', json={'name': 'Du lịch'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertGreaterEqual(response.json['removed_files'], 1)
        self.assertFalse(niche_folder.exists())
        self.assertIsNone(app.find_download('example:one'))
        self.assertEqual(client.get('/api/settings').json['selected_niche'], 'Chung')

    def test_delete_niche_removes_nested_group_folder_from_old_root(self):
        url = self.prepare('a', niche='Du lịch')
        niche_root = Path(app.JOBS['a']['output_dir'])
        app.JOBS['a']['output_dir'] = str(niche_root / 'Đeo bám')
        app.JOBS['a']['group'] = 'Đeo bám'
        app.run_download('a', url)
        self.assertTrue(Path(app.JOBS['a']['filepath']).exists())
        app.CURRENT_OUTPUT_DIR = self.root / 'new-root'

        response = app.app.test_client().delete('/api/niches', json={'name': 'Du lịch'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(niche_root.exists())

    def test_delete_niche_removes_only_its_files_from_shared_group_folder(self):
        group_dir = self.root / 'Đeo bám'
        url = self.prepare('a', niche='Du lịch')
        app.JOBS['a']['output_dir'] = str(group_dir)
        app.JOBS['a']['group'] = 'Đeo bám'
        app.run_download('a', url)
        downloaded = Path(app.JOBS['a']['filepath'])
        unrelated = group_dir / 'giu-lai.mp4'
        unrelated.write_bytes(b'keep')

        response = app.app.test_client().delete('/api/niches', json={'name': 'Du lịch'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(downloaded.exists())
        self.assertTrue(unrelated.exists())

    def test_cannot_delete_default_or_active_niche(self):
        client = app.app.test_client()
        self.assertEqual(client.delete('/api/niches', json={'name': 'Chung'}).status_code, 400)
        client.post('/api/niches', json={'name': 'Động vật'})
        self.prepare('active', niche='Động vật')
        app.JOBS['active']['status'] = 'downloading'
        response = client.delete('/api/niches', json={'name': 'Động vật'})
        self.assertEqual(response.status_code, 409)


if __name__ == '__main__':
    unittest.main()
