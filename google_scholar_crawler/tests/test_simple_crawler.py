"""Offline regression tests; all Scholar responses come from local fixtures."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


CRAWLER_PATH = Path(__file__).resolve().parents[1] / "simple_crawler.py"
SPEC = importlib.util.spec_from_file_location("simple_crawler", CRAWLER_PATH)
crawler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(crawler)
AUTHOR_ID = "6aaQRzoAAAAJ"
PUBLICATION_ID = AUTHOR_ID + ":u-x6o8ySG0sC"


def valid_data(citations=41):
    return {
        "name": "Zhikai Wu",
        "scholar_id": AUTHOR_ID,
        "citedby": citations,
        "hindex": 3 if citations else 0,
        "i10index": 2 if citations else 0,
        "citedby5y": citations,
        "hindex5y": 3 if citations else 0,
        "i10index5y": 2 if citations else 0,
        "updated": "2025-01-02T03:04:05Z",
        "publications": {
            PUBLICATION_ID: {
                "author_pub_id": PUBLICATION_ID,
                "bib": {"title": "A real publication title"},
                "num_citations": citations,
            }
        },
    }


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="scholar-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {"GOOGLE_SCHOLAR_ID": AUTHOR_ID})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def call_main(self, arguments):
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return crawler.main(arguments)

    def write_cache(self, data=None, raw=None):
        path = self.directory / "cache.json"
        path.write_bytes(raw if raw is not None else crawler.json_bytes(data or valid_data()))
        return path

    def test_valid_zero_counts_are_not_placeholders(self):
        data = valid_data(0)
        self.assertIs(crawler.validate_data(data, AUTHOR_ID), data)
        data["publications"] = {}
        self.assertIs(crawler.validate_data(data, AUTHOR_ID), data)

    def test_invalid_and_placeholder_records_are_rejected(self):
        examples = [None, [], {}, {**valid_data(), "name": "Test Author"}]
        examples += [
            {**valid_data(0), "message": "Google Scholar data will be populated once publications are indexed"},
            {**valid_data(), "citedby": True},
            {**valid_data(), "hindex": -1},
            {**valid_data(), "i10index5y": "0"},
            {**valid_data(), "updated": "not a date"},
            {**valid_data(), "publications": []},
            {**valid_data(), "scholar_id": "someone_else"},
            {**valid_data(), "status": "unavailable"},
        ]
        bad_publication = valid_data()
        bad_publication["publications"][PUBLICATION_ID]["author_pub_id"] = "pub_0"
        examples.append(bad_publication)
        for example in examples:
            with self.subTest(record=example):
                with self.assertRaises(ValueError):
                    crawler.validate_data(example, AUTHOR_ID)

    def test_failed_fetch_copies_cache_bytes_without_changing_timestamp(self):
        data = valid_data()
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        cache = self.write_cache(raw=raw)
        output = self.directory / "output"
        with mock.patch.object(crawler, "fetch_with_deadline", side_effect=crawler.FetchError("HTTP 429")):
            code = self.call_main(["--cache-file", str(cache), "--output-dir", str(output)])
        self.assertEqual(code, 0)
        self.assertEqual((output / "gs_data.json").read_bytes(), raw)
        self.assertEqual(json.loads((output / "gs_data.json").read_bytes())["updated"], data["updated"])
        badge = json.loads((output / "gs_data_shieldsio.json").read_bytes())
        self.assertEqual(badge["message"], "41")
        self.assertIn("::warning::", self.stderr.getvalue())

    def test_unchanged_cache_does_not_replace_existing_data_file(self):
        data = valid_data()
        output = self.directory / "output"
        raw = crawler.json_bytes(data)
        crawler.write_results(data, raw, output)
        before = (output / "gs_data.json").stat().st_mtime_ns
        with mock.patch.object(crawler.os, "replace", wraps=os.replace) as replace:
            crawler.write_results(data, raw, output)
        self.assertFalse(replace.called)
        self.assertEqual(before, (output / "gs_data.json").stat().st_mtime_ns)

    def test_unusable_cache_leaves_all_existing_outputs_untouched(self):
        output = self.directory / "output"
        output.mkdir()
        (output / "gs_data.json").write_bytes(b"existing data")
        (output / "gs_data_shieldsio.json").write_bytes(b"existing badge")
        for raw in (b"", b"{invalid json", crawler.json_bytes({**valid_data(), "name": "Test Author"})):
            cache = self.write_cache(raw=raw)
            with self.subTest(cache=raw), mock.patch.object(crawler, "fetch_with_deadline", side_effect=crawler.FetchError("blocked")):
                code = self.call_main(["--cache-file", str(cache), "--output-dir", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual((output / "gs_data.json").read_bytes(), b"existing data")
            self.assertEqual((output / "gs_data_shieldsio.json").read_bytes(), b"existing badge")
            self.assertIn("no valid cache", self.stderr.getvalue())

    def test_missing_cache_does_not_create_synthetic_results(self):
        output = self.directory / "output"
        with mock.patch.object(crawler, "fetch_with_deadline", side_effect=crawler.FetchError("blocked")):
            code = self.call_main(["--cache-file", str(self.directory / "absent.json"), "--output-dir", str(output)])
        self.assertEqual(code, 0)
        self.assertFalse(output.exists())

    def test_missing_id_is_a_configuration_failure_without_fetching(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(crawler, "fetch_with_deadline") as fetch:
            code = self.call_main([])
        self.assertEqual(code, 1)
        fetch.assert_not_called()
        self.assertIn("GOOGLE_SCHOLAR_ID", self.stderr.getvalue())

    def test_dependency_failure_is_not_reported_as_a_successful_refresh(self):
        cache = self.write_cache()
        output = self.directory / "output"
        with mock.patch.object(crawler, "fetch_with_deadline", side_effect=crawler.ConfigurationError("missing scholarly")):
            code = self.call_main(["--cache-file", str(cache), "--output-dir", str(output)])
        self.assertEqual(code, 1)
        self.assertFalse(output.exists())
        self.assertIn("::error::missing scholarly", self.stderr.getvalue())

    def test_validate_only_needs_no_author_id_and_never_fetches(self):
        cache = self.write_cache()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(crawler, "fetch_with_deadline") as fetch:
            self.assertEqual(self.call_main(["--validate-only", str(cache)]), 0)
            cache.write_bytes(b"")
            self.assertEqual(self.call_main(["--validate-only", str(cache)]), 2)
        fetch.assert_not_called()

    def test_failed_atomic_replace_preserves_previous_file_and_cleans_temporary(self):
        target = self.directory / "data.json"
        target.write_bytes(b"previous")
        with mock.patch.object(crawler.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                crawler.atomic_write(target, b"new complete data")
        self.assertEqual(target.read_bytes(), b"previous")
        self.assertEqual(list(self.directory.iterdir()), [target])

    def fake_scholar_cli(self, mode="success", citations=41):
        """Copy the executable and inject a local scholarly module for real IPC."""
        executable_dir = self.directory / "copied-crawler"
        executable_dir.mkdir()
        executable = executable_dir / "simple_crawler.py"
        shutil.copyfile(CRAWLER_PATH, executable)
        fake_packages = self.directory / "fake-packages"
        fake_packages.mkdir()
        author = deepcopy(valid_data(citations))
        author.pop("updated")
        author["publications"] = list(author["publications"].values())
        fake_module = '''
import json
import os
import subprocess
import sys
import time

if os.environ.get("SCHOLAR_TEST_MODE") == "import-error":
    raise ImportError("fixture dependency failure")

class FixtureScholarly:
    def set_timeout(self, timeout):
        assert timeout == 10
        self.timeout = timeout
    def set_retries(self, retries):
        assert retries == 1
        self.retries = retries
    def search_author_id(self, author_id):
        assert author_id == "6aaQRzoAAAAJ"
        assert self.timeout == 10 and self.retries == 1
        print("fixture diagnostic: must not contaminate JSON")
        if os.environ.get("SCHOLAR_TEST_MODE") == "timeout":
            # Descendant keeps inherited pipes open unless its whole group dies.
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            time.sleep(30)
        return json.loads(FIXTURE_JSON)
    def fill(self, author, sections):
        assert sections == ["basics", "indices", "counts", "publications"]
        return author

scholarly = FixtureScholarly()
'''
        (fake_packages / "scholarly.py").write_text("FIXTURE_JSON = " + repr(json.dumps(author)) + "\n" + fake_module)
        environment = os.environ.copy()
        environment.update({"PYTHONPATH": str(fake_packages), "SCHOLAR_TEST_MODE": mode})
        unrelated_cwd = self.directory / "unrelated-cwd"
        unrelated_cwd.mkdir()
        return executable, environment, unrelated_cwd

    def test_subprocess_success_preserves_publication_schema_from_unrelated_cwd(self):
        executable, environment, cwd = self.fake_scholar_cli()
        result = subprocess.run([sys.executable, str(executable)], env=environment, cwd=cwd, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = executable.parent / "results"
        data = json.loads((output / "gs_data.json").read_bytes())
        self.assertEqual(data["citedby"], 41)
        self.assertEqual(data["hindex"], 3)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["scholar_id"], AUTHOR_ID)
        self.assertEqual(data["publications"][PUBLICATION_ID]["author_pub_id"], PUBLICATION_ID)
        self.assertNotEqual(data["updated"], valid_data()["updated"])
        self.assertTrue(data["updated"].endswith("Z"))
        self.assertFalse((cwd / "results").exists())
        self.assertEqual(crawler.validate_data(data, AUTHOR_ID), data)

    def test_subprocess_fresh_zero_counts_are_saved(self):
        executable, environment, cwd = self.fake_scholar_cli(citations=0)
        result = subprocess.run([sys.executable, str(executable)], env=environment, cwd=cwd, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((executable.parent / "results/gs_data.json").read_bytes())
        self.assertEqual(data["citedby"], 0)
        self.assertEqual(data["status"], "ok")

    def test_real_worker_deadline_kills_descendants_and_retains_cache(self):
        executable, environment, cwd = self.fake_scholar_cli(mode="timeout")
        cache = self.write_cache()
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(executable), "--cache-file", str(cache), "--timeout", "0.4"],
            env=environment, cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deadline", result.stderr)
        self.assertEqual((executable.parent / "results/gs_data.json").read_bytes(), cache.read_bytes())

    def test_real_worker_import_failure_has_nonzero_exit(self):
        executable, environment, cwd = self.fake_scholar_cli(mode="import-error")
        result = subprocess.run([sys.executable, str(executable)], env=environment, cwd=cwd, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("fixture dependency failure", result.stderr)
        self.assertFalse((executable.parent / "results").exists())


if __name__ == "__main__":
    unittest.main()
