"""MCP 网页工具的下载与解压资源上限。"""
import gzip
import io
import unittest
from unittest.mock import patch

from mcp_server import sys_tools_server as server


class HttpLimitTests(unittest.TestCase):
    def test_large_http_body_is_rejected_before_full_read(self):
        class Response(io.BytesIO):
            headers = {}
            status = 200

            def geturl(self):
                return "https://example.test/"

        with patch.object(server, "_HTTP_MAX_TRANSFER_BYTES", 64):
            with patch.object(server, "urlopen", return_value=Response(b"x" * 65)):
                with self.assertRaisesRegex(ValueError, "下载上限"):
                    server._http_request("https://example.test/")

    def test_compressed_body_expansion_is_bounded(self):
        payload = gzip.compress(b"x" * 65)
        with patch.object(server, "_HTTP_MAX_DECOMPRESSED_BYTES", 64):
            with self.assertRaisesRegex(ValueError, "解压后"):
                server._decompress_http_body(payload, 16 + server.zlib.MAX_WBITS)


if __name__ == "__main__":
    unittest.main()
