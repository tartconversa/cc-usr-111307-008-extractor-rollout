import json
import threading
import unittest
from urllib.request import urlopen
from src import source

class SupplierSourceTests(unittest.TestCase):
    def setUp(self): self.source = source.Source()
    def route(self, path, headers=None): return self.source.route('GET', path, headers or {}, b'')
    def test_protocol(self):
        page = self.route('/drift')[2].decode()
        self.assertLess(page.index('推荐机构'), page.index('<main'))
        self.assertEqual(source.DATA['gold']['drift']['provider'], '甲供方')
        self.assertEqual(source.DATA['release']['max_gold_conflicts'], 0)
    def test_http_state_endpoint(self):
        with source.make_server() as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                with urlopen('http://127.0.0.1:%d/__state' % server.server_port, timeout=5) as response:
                    self.assertEqual(json.load(response)['generation'], 1)
            finally:
                server.shutdown(); thread.join()

if __name__ == '__main__': unittest.main()
