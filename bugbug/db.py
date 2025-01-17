# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import gzip
import io
import logging
import os
import pickle
from contextlib import contextmanager
from urllib.parse import urljoin

import orjson
import requests
import zstandard

from bugbug import utils
from bugbug.utils import extract_tar_zst, zstd_decompress

DATABASES = {}

logger = logging.getLogger(__name__)


def register(path, url, version, support_files=[]):
    DATABASES[path] = {"url": url, "version": version, "support_files": support_files}

    # Create DB parent directory.
    os.makedirs(os.path.abspath(os.path.dirname(path)), exist_ok=True)

    if not os.path.exists(f"{path}.version"):
        with open(f"{path}.version", "w") as f:
            f.write(str(version))


def exists(path):
    return os.path.exists(path)


def is_old_schema(path):
    url = urljoin(DATABASES[path]["url"], f"{os.path.basename(path)}.version")
    r = requests.get(url)

    if not r.ok:
        logger.info(f"Version file is not yet available to download for {path}")
        return True

    prev_version = int(r.text)

    return DATABASES[path]["version"] > prev_version


def extract_file(path):
    inner_path, _ = os.path.splitext(path)

    if str(path).endswith(".tar.zst"):
        extract_tar_zst(inner_path)
    elif str(path).endswith(".zst"):
        zstd_decompress(inner_path)
    else:
        assert False, f"Unexpected compression type for {path}"


def download_support_file(path, file_name):
    # If a DB with the current schema is not available yet, we can't download.
    if is_old_schema(path):
        return False

    try:
        url = urljoin(DATABASES[path]["url"], file_name)
        path = os.path.join(os.path.dirname(path), file_name)

        logger.info(f"Downloading {url} to {path}")
        updated = utils.download_check_etag(url, path)

        if updated and path.endswith(".zst"):
            extract_file(path)

        return True
    except requests.exceptions.HTTPError:
        logger.info(
            f"{file_name} is not yet available to download for {path}", exc_info=True
        )
        return False


# Download and extract databases.
def download(path, support_files_too=False):
    if is_older_version(path):
        return False

    zst_path = f"{path}.zst"

    url = DATABASES[path]["url"]
    try:
        logger.info(f"Downloading {url} to {zst_path}")
        updated = utils.download_check_etag(url, zst_path)

        if updated:
            extract_file(zst_path)

        successful = True
        if support_files_too:
            for support_file in DATABASES[path]["support_files"]:
                successful |= download_support_file(path, support_file)

        return successful
    except requests.exceptions.HTTPError:
        logger.info(f"{url} is not yet available to download", exc_info=True)
        return False


# Remove DBs when the schema of the downloaded DB is older than the current schema
# If the db at the new path is an older version
def is_older_version(path):
    url = urljoin(DATABASES[path]["url"], f"{os.path.basename(path)}.version")
    r = requests.get(url)
    new_version = int(r.text)

    for curr_entry in DATABASES:
        if new_version < curr_entry:
            return False
    return True


def last_modified(path):
    url = DATABASES[path]["url"]
    last_modified = utils.get_last_modified(url)

    if last_modified is None:
        raise Exception("Last-Modified is not available")

    return last_modified


class Store:
    def __init__(self, fh):
        self.fh = fh


class JSONStore(Store):
    def write(self, elems):
        for elem in elems:
            self.fh.write(orjson.dumps(elem) + b"\n")

    def read(self):
        for line in io.TextIOWrapper(self.fh, encoding="utf-8"):
            yield orjson.loads(line)


class PickleStore(Store):
    def write(self, elems):
        for elem in elems:
            self.fh.write(pickle.dumps(elem))

    def read(self):
        try:
            while True:
                yield pickle.load(self.fh)
        except EOFError:
            pass


COMPRESSION_FORMATS = ["gz", "zstd"]
SERIALIZATION_FORMATS = {"json": JSONStore, "pickle": PickleStore}


@contextmanager
def _db_open(path, mode):
    parts = str(path).split(".")
    assert len(parts) > 1, "Extension needed to figure out serialization format"
    if len(parts) == 2:
        db_format = parts[-1]
        compression = None
    else:
        db_format = parts[-2]
        compression = parts[-1]

    assert compression is None or compression in COMPRESSION_FORMATS
    assert db_format in SERIALIZATION_FORMATS

    store_constructor = SERIALIZATION_FORMATS[db_format]

    if compression == "gz":
        with gzip.GzipFile(path, mode) as f:
            yield store_constructor(f)
    elif compression == "zstd":
        if "w" in mode or "a" in mode:
            cctx = zstandard.ZstdCompressor()
            with open(path, mode) as f:
                with cctx.stream_writer(f) as writer:
                    yield store_constructor(writer)
        else:
            dctx = zstandard.ZstdDecompressor()
            with open(path, mode) as f:
                with dctx.stream_reader(f) as reader:
                    yield store_constructor(reader)
    else:
        with open(path, mode) as f:
            yield store_constructor(f)


def read(path):
    assert path in DATABASES

    if not os.path.exists(path):
        return ()

    with _db_open(path, "rb") as store:
        for elem in store.read():
            yield elem


def write(path, elems):
    assert path in DATABASES

    with _db_open(path, "wb") as store:
        store.write(elems)


def append(path, elems):
    assert path in DATABASES

    with _db_open(path, "ab") as store:
        store.write(elems)


def delete(path, match):
    assert path in DATABASES

    dirname, basename = os.path.split(path)
    new_path = os.path.join(dirname, f"new_{basename}")

    def matching_elems(store):
        for elem in store.read():
            if not match(elem):
                yield elem

    try:
        with _db_open(path, "rb") as rstore:
            with _db_open(new_path, "wb") as wstore:
                wstore.write(matching_elems(rstore))
    except FileNotFoundError:
        return

    os.unlink(path)
    os.rename(new_path, path)
