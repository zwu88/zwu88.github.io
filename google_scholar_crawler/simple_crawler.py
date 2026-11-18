#!/usr/bin/env python3
"""Refresh Scholar statistics within a deadline, retaining verified cached data."""

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile


SCRIPT_DIR = Path(__file__).resolve().parent
METRICS = ("citedby", "hindex", "i10index")
RECENT_METRICS = ("citedby5y", "hindex5y", "i10index5y")
PLACEHOLDER_MESSAGE = "data will be populated once publications are indexed"


class FetchError(Exception):
    """Scholar was unavailable or returned incomplete data."""


class ConfigurationError(Exception):
    """A local setting or installed dependency needs attention."""


def validate_data(data, scholar_id=None):
    """Reject incomplete/placeholder records while accepting real zero counts."""
    if not isinstance(data, dict):
        raise ValueError("Scholar data must be a JSON object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or name.strip().lower() == "test author":
        raise ValueError("Scholar data has no real author name")
    message = data.get("message", "")
    if isinstance(message, str) and PLACEHOLDER_MESSAGE in message.lower():
        raise ValueError("Scholar data contains the legacy placeholder")
    for key in METRICS:
        if type(data.get(key)) is not int or data[key] < 0:
            raise ValueError(f"Scholar data needs a nonnegative integer {key}")
    for key in RECENT_METRICS:
        if key in data and (type(data[key]) is not int or data[key] < 0):
            raise ValueError(f"Scholar data has an invalid {key}")
    updated = data.get("updated")
    if not isinstance(updated, str) or not updated.strip():
        raise ValueError("Scholar data has no last-success timestamp")
    try:
        datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Scholar data has an invalid last-success timestamp") from error
    if "status" in data and data["status"] not in ("ok", "cached"):
        raise ValueError("Scholar data is marked unavailable or invalid")
    if "scholar_id" in data:
        if not isinstance(data["scholar_id"], str) or not data["scholar_id"]:
            raise ValueError("Scholar data has an invalid author ID")
        if scholar_id and data["scholar_id"] != scholar_id:
            raise ValueError("Cached data belongs to a different Scholar author")
    publications = data.get("publications")
    if not isinstance(publications, dict):
        raise ValueError("Scholar publications must be an object keyed by author_pub_id")
    for publication_id, publication in publications.items():
        if not isinstance(publication, dict) or publication.get("author_pub_id") != publication_id:
            raise ValueError("Scholar publication IDs do not match their keys")
        if not isinstance(publication_id, str) or ":" not in publication_id:
            raise ValueError("Scholar publication has no genuine author_pub_id")
        if scholar_id and not publication_id.startswith(scholar_id + ":"):
            raise ValueError("Publication belongs to a different Scholar author")
        bib = publication.get("bib")
        if not isinstance(bib, dict) or not isinstance(bib.get("title"), str) or not bib["title"].strip():
            raise ValueError("Scholar publication has no title")
        if type(publication.get("num_citations")) is not int or publication["num_citations"] < 0:
            raise ValueError("Scholar publication has an invalid citation count")
    return data


def load_cache(path, scholar_id=None):
    """Return both validated data and its unchanged serialized representation."""
    raw = Path(path).read_bytes()
    data = validate_data(json.loads(raw), scholar_id)
    return data, raw


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def atomic_write(path, contents):
    """Replace a complete file; leave identical existing files untouched."""
    path = Path(path)
    if path.is_file() and path.read_bytes() == contents:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary_path = Path(output.name)
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_results(data, raw, output_dir):
    badge = {"schemaVersion": 1, "label": "citations", "message": str(data["citedby"])}
    atomic_write(Path(output_dir) / "gs_data_shieldsio.json", json_bytes(badge))
    atomic_write(Path(output_dir) / "gs_data.json", raw)


def fetch_author(scholar_id):
    """Run only in the worker: avoid automatic proxy discovery and detail crawls."""
    from scholarly import scholarly

    scholarly.set_timeout(10)
    scholarly.set_retries(1)
    author = scholarly.search_author_id(scholar_id)
    filled = scholarly.fill(author, sections=["basics", "indices", "counts", "publications"])
    if filled is not None:
        author = filled
    if not isinstance(author, dict):
        raise ValueError("Scholar returned no author object")
    author = dict(author)
    publications = author.get("publications")
    if not isinstance(publications, list):
        raise ValueError("Scholar returned no publication list")
    by_id = {}
    for publication in publications:
        if not isinstance(publication, dict) or not publication.get("author_pub_id"):
            raise ValueError("Scholar returned a publication without its author_pub_id")
        publication_id = publication["author_pub_id"]
        if publication_id in by_id:
            raise ValueError("Scholar returned duplicate publication IDs")
        by_id[publication_id] = publication
    author["publications"] = by_id
    if author.get("scholar_id", scholar_id) != scholar_id:
        raise ValueError("Scholar returned a different author")
    author["scholar_id"] = scholar_id
    author["updated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    author["status"] = "ok"
    validate_data(author, scholar_id)
    return author


def worker_main(scholar_id):
    try:
        # Keep stdout a JSON-only channel, even if a dependency prints diagnostics.
        with redirect_stdout(sys.stderr):
            data = fetch_author(scholar_id)
        sys.stdout.buffer.write(json_bytes(data))
        return 0
    except ImportError as error:
        print(f"Crawler dependency could not be imported: {error}", file=sys.stderr)
        return 3
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


def fetch_with_deadline(scholar_id, timeout):
    """Enforce a wall-clock deadline beyond scholarly's request/retry settings."""
    command = [sys.executable, "-u", str(SCRIPT_DIR / "simple_crawler.py"), "--fetch-worker", scholar_id]
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.communicate()
            raise FetchError(f"refresh exceeded its {timeout:g}-second deadline") from error
    details = stderr.decode("utf-8", errors="replace").strip()[-1500:]
    if process.returncode == 3:
        raise ConfigurationError(details or "crawler dependency is unavailable")
    if process.returncode:
        raise FetchError(details or f"crawler worker exited with code {process.returncode}")
    try:
        return validate_data(json.loads(stdout), scholar_id), stdout
    except (ValueError, UnicodeError) as error:
        raise FetchError(f"invalid Scholar response: {error}") from error


def annotation(level, message):
    escaped = str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{escaped}", file=sys.stderr)


def positive_timeout(value):
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a positive number of seconds") from error
    if not 0 < timeout < float("inf"):
        raise argparse.ArgumentTypeError("timeout must be a positive finite number of seconds")
    return timeout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-file", type=Path, default=SCRIPT_DIR / "results" / "gs_data.json")
    parser.add_argument("--output-dir", type=Path, default=SCRIPT_DIR / "results")
    parser.add_argument("--timeout", type=positive_timeout, default=90.0, help="total fetch deadline in seconds (default: 90)")
    parser.add_argument("--validate-only", type=Path, metavar="JSON_FILE", help="validate a cache without fetching (exit 0 valid, 2 invalid)")
    parser.add_argument("--fetch-worker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.fetch_worker:
        return worker_main(args.fetch_worker)
    if args.validate_only:
        try:
            load_cache(args.validate_only, os.environ.get("GOOGLE_SCHOLAR_ID") or None)
        except (OSError, ValueError, UnicodeError) as error:
            print(f"Invalid Scholar cache: {error}", file=sys.stderr)
            return 2
        print(f"Valid Scholar cache: {args.validate_only}")
        return 0
    scholar_id = os.environ.get("GOOGLE_SCHOLAR_ID", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", scholar_id):
        annotation("error", "GOOGLE_SCHOLAR_ID must contain a valid Google Scholar author ID")
        return 1
    try:
        try:
            data, raw = fetch_with_deadline(scholar_id, args.timeout)
        except FetchError as error:
            try:
                data, raw = load_cache(args.cache_file, scholar_id)
            except (OSError, ValueError, UnicodeError) as cache_error:
                annotation("warning", f"Google Scholar refresh failed ({error}); no valid cache ({cache_error}). Existing output is unchanged.")
                return 0
            annotation("warning", f"Google Scholar refresh failed ({error}); retaining data last fetched at {data['updated']}.")
        write_results(data, raw, args.output_dir)
    except (ConfigurationError, OSError, ValueError) as error:
        annotation("error", error)
        return 1
    print(f"Scholar statistics: {data['citedby']} citations, h-index {data['hindex']}; last success {data['updated']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
