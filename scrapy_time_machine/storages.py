import dbm
import gzip
import logging
from os.path import basename, dirname, exists, join
from tempfile import NamedTemporaryFile
from time import time
from urllib import parse

import boto3
from scrapy.exceptions import CloseSpider
from scrapy.http import Headers
from scrapy.responsetypes import responsetypes
from scrapy.utils.project import data_path
from scrapy.utils.request import request_fingerprint
from six.moves import cPickle as pickle
from w3lib.url import file_uri_to_path

logger = logging.getLogger(__name__)


class DbmTimeMachineStorage:
    time_machine_dir = "timemachine"

    def __init__(self, settings):
        self.db = None
        self.snapshot_uri = None

    def set_uri(self, uri, uri_params, retrieve=False):
        self.snapshot_uri = file_uri_to_path(uri % uri_params)
        path = dirname(self.snapshot_uri)
        path = data_path(path, createdir=True)
        db_name = basename(self.snapshot_uri)
        self.snapshot_uri = join(path, db_name)

    def is_uri_valid(self):
        return exists(self.snapshot_uri)

    def open_spider(self, spider, settings):
        self._spider = spider

        self._prepare_time_machine(settings)

        if not self.snapshot_uri:
            raise CloseSpider("Snapshot uri not configured.")

        self.db = dbm.open(self.snapshot_uri, "c")

        logger.debug(
            "Using DBM time machine storage in %(dbpath)s"
            % {"dbpath": self.snapshot_uri},
            extra={"spider": spider},
        )

    def _prepare_time_machine(self, settings):
        pass

    def close_spider(self, spider, settings):
        if self.db:
            self.db.close()

        self._finish_time_machine(settings)

    def _finish_time_machine(self, settings):
        pass

    def retrieve_response(self, spider, request):
        data = self._read_data(spider, request)
        if data is None:
            return  # not stored
        url = data["url"]
        status = data["status"]
        headers = Headers(data["headers"])
        body = gzip.decompress(data["body"])
        respcls = responsetypes.from_args(headers=headers, url=url)
        response = respcls(url=url, headers=headers, status=status, body=body)
        return response

    def store_response(self, spider, request, response):
        key = self._request_key(request)
        data = {
            "status": response.status,
            "url": response.url,
            "headers": dict(response.headers),
            "body": gzip.compress(response.body),
        }
        data = pickle.dumps(data, protocol=2)
        self.db["%s_data" % key] = data
        self.db["%s_time" % key] = str(time())

    def _read_data(self, spider, request):
        key = self._request_key(request)
        db = self.db
        tkey = f"{key}_time"
        if tkey not in db:
            return  # not found

        return pickle.loads(db[f"{key}_data"])

    def _request_key(self, request):
        return request_fingerprint(request)


class S3TimeMachineStorage(DbmTimeMachineStorage):
    def __init__(self, settings):
        self.db = None
        self.snapshot_uri = None
        self.s3_uri = settings.get("TIME_MACHINE_URI")
        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=settings.get("AWS_SECRET_ACCESS_KEY"),
        )
        self.retrieve_mode = settings.get("TIME_MACHINE_RETRIEVE")
        self.snapshot_mode = settings.get("TIME_MACHINE_SNAPSHOT")

    def get_netloc_and_path(self, s3_uri):
        scheme, netloc, path, _, _, _ = parse.urlparse(s3_uri)
        if not scheme == "s3":
            raise ValueError(f"Provided uri scheme is not s3: {scheme}")

        if not netloc or not path:
            raise ValueError("bucket and path must not be empty")

        return netloc, path

    def is_uri_valid(self):
        return bool(self.get_netloc_and_path(self.snapshot_uri))

    def set_uri(self, uri, uri_params, retrieve=False):
        self.snapshot_uri = self.s3_uri % uri_params
        return self.snapshot_uri

    def open_spider(self, spider, **kwargs):
        self._prepare_time_machine(spider.settings)
        logger.debug(
            "Using S3 time machine storage in %(s3_uri)s" % {"s3_uri": self.s3_uri},
            extra={"spider": spider},
        )

    def _prepare_time_machine(self, settings):
        # Create a local file to host the db data
        tempfile = NamedTemporaryFile(mode="wb", suffix=".db", delete=False)
        if self.retrieve_mode:
            # download db file from s3 snapshot_uri
            s3_bucket, s3_path = self.get_netloc_and_path(self.snapshot_uri)
            self.s3_client.download_fileobj(s3_bucket, s3_path.lstrip('/'), tempfile)
            self.db = dbm.open(tempfile.name, "c")
        else:
            self.db = dbm.open(tempfile.name, "n")

        # Save refence to DB underlaying file
        self.path_to_local_file = tempfile

    def _finish_time_machine(self, settings):
        if self.snapshot_mode:
            # Flush local file content
            self.path_to_local_file.flush()
            # Upload file to s3
            s3_bucket, s3_path = self.get_netloc_and_path(self.snapshot_uri)
            self.s3_client.upload_file(
                self.path_to_local_file.name, s3_bucket, s3_path.lstrip('/')
            )
            logger.info(f"Uploaded Time Machine file to {self.snapshot_uri}")
            # Close and remove local db file
            self.path_to_local_file.close()
