"""Regression: a command after the first 50 inbox comments must be read."""
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from bridge_commands import scan

class PaginationTests(unittest.TestCase):
    def run_scan(self, request, **kw):
        return scan(request, 'test-token', 'owner/repo', 49,
                    executor=None, ledger=None, processed={}, **kw)

    def test_command_on_second_page_is_not_lost(self):
        calls=[]
        def request(method, path, token):
            q=parse_qs(urlsplit(path).query)
            page=int(q.get('page',['1'])[0]); calls.append(page)
            return ([{'id':n} for n in range(50)] if page==1 else
                    [{'id':50,'body':'new command'}]), {}
        with patch('bridge_commands.process_comment', side_effect=lambda *a,**k: a[3]['id']) as process:
            result=self.run_scan(request)
        self.assertEqual(calls,[1,2]); self.assertEqual(result,list(range(51)))
        self.assertEqual(process.call_count,51)

    def test_exact_full_page_requires_empty_page_confirmation(self):
        calls=[]
        def request(method,path,token):
            calls.append(path)
            return ([{'id':1}] if len(calls)==1 else []), {}
        with patch('bridge_commands.process_comment',return_value={}) as process:
            self.run_scan(request,limit=1)
        self.assertEqual(len(calls),2); self.assertEqual(process.call_count,1)

    def test_later_page_failure_does_not_execute_partial_inbox(self):
        def request(method,path,token):
            if 'page=2' in path: raise OSError('temporary outage')
            return [{'id':1}], {}
        with patch('bridge_commands.process_comment') as process:
            with self.assertRaises(OSError): self.run_scan(request,limit=1)
            process.assert_not_called()

    def test_nonlist_page_is_rejected(self):
        with patch('bridge_commands.process_comment') as process:
            with self.assertRaises(ValueError): self.run_scan(lambda *a: ({'error':'bad'},{}))
            process.assert_not_called()

    def test_exhausted_page_cap_does_not_execute_partial_inbox(self):
        with patch('bridge_commands.process_comment') as process:
            with self.assertRaises(RuntimeError): self.run_scan(lambda *a: ([{'id':1}],{}),limit=1)
            process.assert_not_called()

    def test_invalid_page_sizes_fail_before_network(self):
        for limit in (0,-1,101,True,'50'):
            with self.subTest(limit=limit), patch('bridge_commands.process_comment') as process:
                with self.assertRaises(ValueError): self.run_scan(lambda *a: self.fail('network call'),limit=limit)
                process.assert_not_called()

    def test_empty_inbox_is_safe(self):
        with patch('bridge_commands.process_comment') as process:
            self.assertEqual(self.run_scan(lambda *a: ([],{})),[])
            process.assert_not_called()
