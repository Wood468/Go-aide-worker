"""No-network tests of direct Gemini API probe; never use live credentials."""
import importlib.util
import json
from pathlib import Path
from io import BytesIO
import unittest
import urllib.error
from unittest.mock import patch

spec=importlib.util.spec_from_file_location("gemini_probe",Path(__file__).resolve().parents[1]/"addon/gemini_probe.py")
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)

class FakeResponse:
    status=200
    def __init__(self, payload):
        self.data=BytesIO(json.dumps(payload).encode())
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def read(self,limit): return self.data.read(limit)

class GeminiProbeTests(unittest.TestCase):
    def test_success(self):
        def open_request(request,timeout):
            self.assertEqual(request.get_header("X-goog-api-key"),"dummy-key")
            self.assertNotIn("dummy-key",request.full_url)
            self.assertEqual(timeout,25)
            return FakeResponse({"candidates":[{"content":{"parts":[{"text":"READY"}]}}]})
        self.assertEqual(probe.probe("dummy-key",open_request),0)

    def test_rejects_missing_key(self):
        self.assertEqual(probe.probe(""),11)

    def test_auth_failure(self):
        def fail(*args,**kwargs):
            raise urllib.error.HTTPError("https://redacted.invalid",403,"forbidden",{},None)
        self.assertEqual(probe.probe("dummy",fail),11)

    def test_quota_failure(self):
        def fail(*args,**kwargs):
            raise urllib.error.HTTPError("https://redacted.invalid",429,"quota",{},None)
        self.assertEqual(probe.probe("dummy",fail),12)

    def test_unexpected_content(self):
        self.assertEqual(probe.probe("dummy",lambda *a,**k:FakeResponse({"candidates":[]})),15)

if __name__=="__main__": unittest.main()
