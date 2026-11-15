import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.html_subset import Document, LocatorSyntaxError, parse_selector  # noqa: E402


class SelectorSubsetTests(unittest.TestCase):
    HTML = (
        "<html><body><main id='resource'><h1>Title</h1>"
        "<div class='provider primary' data-role='owner'>A</div>"
        "<div class='license'><span>Open</span></div></main>"
        "<aside><div class='provider'>Promo</div></aside></body></html>"
    )

    def test_supported_subset(self):
        document = Document.parse(self.HTML)
        self.assertEqual([e.text() for e in document.select("main#resource h1")], ["Title"])
        self.assertEqual([e.text() for e in document.select("main#resource > .provider")], ["A"])
        self.assertEqual(len(document.select(".provider")), 2)
        self.assertEqual(len(document.select("[data-role=owner]")), 1)
        self.assertEqual(len(document.select("div[class~=primary]")), 1)
        self.assertEqual(len(document.select("main#resource .provider, main#resource .license")), 2)

    def test_unsupported_syntax_is_rejected(self):
        for selector in [
            "div + div",
            "div ~ div",
            "div:first-child",
            "div::before",
            "svg|rect",
            "div\\:x",
            "div /* comment */",
        ]:
            with self.subTest(selector=selector):
                with self.assertRaises(LocatorSyntaxError):
                    parse_selector(selector)

    def test_malformed_syntax_is_rejected(self):
        for selector in ["", "   ", "div >", "[x", "div[", "div,"]:
            with self.subTest(selector=selector):
                with self.assertRaises(LocatorSyntaxError):
                    parse_selector(selector)

    def test_document_order_and_evidence_path(self):
        document = Document.parse(self.HTML)
        nodes = document.select(".provider")
        self.assertEqual([node.text() for node in nodes], ["A", "Promo"])
        self.assertIn("main#resource", nodes[0].path())
        self.assertEqual(nodes[0].attrs["data-role"], "owner")
        self.assertLess((nodes[0].line, nodes[0].column), (nodes[1].line, nodes[1].column))


if __name__ == "__main__":
    unittest.main()
