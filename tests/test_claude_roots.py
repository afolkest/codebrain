import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codebrain import claude_roots, collect, ingest
from tests._helpers import memory_db, write_jsonl


class MultipleClaudeHomes(unittest.TestCase):
    def test_live_refresh_archive_and_remote_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            work, personal, pool = [base / name for name in ('work', 'personal', 'pool')]
            config = base / 'roots.json'
            config.write_text(json.dumps([str(personal), str(personal), str(work)]))
            for root, sid in ((work, 'work-session'), (personal, 'personal-session')):
                project = root / 'projects' / 'same-project'
                project.mkdir(parents=True)
                write_jsonl(project, sid + '.jsonl', [{
                    'type': 'user', 'uuid': sid + '-message', 'parentUuid': None,
                    'sessionId': sid, 'cwd': '/work',
                    'timestamp': '2026-10-03T00:00:00Z',
                    'message': {'role': 'user', 'content': sid},
                }])
                (root / 'history.jsonl').write_text(sid)
                (root / '.credentials.json').write_text('DO NOT COPY')
            with patch.object(claude_roots, 'CONFIG_PATH', config), \
                 patch.object(ingest, 'DEFAULT_CLAUDE_ROOT', work), \
                 patch.dict(collect.DEFAULT_ROOTS, claude=work):
                conn = memory_db()
                self.addCleanup(conn.close)
                self.assertEqual(ingest.refresh(conn, sources=('claude',))['sessions'], 2)
                self.assertEqual(ingest.refresh(conn, sources=('claude',))['files'], 0)
                self.assertEqual(collect.collect_source('claude', pool_root=pool, machine='remote')['new'], 4)
                self.assertEqual(collect.collect_source('claude', pool_root=pool, machine='remote')['unchanged'], 4)
                archived = pool / 'raw' / 'remote' / 'claude'
                self.assertEqual((archived / 'history.jsonl').read_text(), 'work-session')
                self.assertEqual((archived / 'profiles' / claude_roots.archive_key(personal) / 'history.jsonl').read_text(), 'personal-session')
                self.assertEqual(list(pool.rglob('.credentials.json')), [])
                remote = memory_db()
                self.addCleanup(remote.close)
                stats = ingest.refresh_pool(remote, pool_root=pool, sources=('claude',))
                self.assertEqual(stats['sessions'], 2)
                self.assertEqual({r['machine'] for r in remote.execute('SELECT machine FROM sessions')}, {'remote'})
                explicit = memory_db()
                self.addCleanup(explicit.close)
                self.assertEqual(ingest.refresh(explicit, sources=('claude',), roots={'claude': work})['sessions'], 1)

    def test_invalid_config_does_not_silently_drop_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'roots.json'
            with patch.object(claude_roots, 'CONFIG_PATH', config):
                self.assertEqual(claude_roots.local_roots(Path('/work')), [Path('/work')])
                for value in ({}, ['relative/path'], [42]):
                    config.write_text(json.dumps(value))
                    with self.assertRaises(ValueError):
                        claude_roots.local_roots(Path('/work'))
