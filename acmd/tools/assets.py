# coding: utf-8
import datetime
import hashlib
import mimetypes
import optparse
import os
import re
import sys
import time

import requests

from acmd import OK, USER_ERROR, SERVER_ERROR
from acmd import tool, error, log
from acmd.tools.tool_utils import get_argument, get_command

ROOT_IMPORT_DIR = "/tmp/acmd_assets_ingest"

parser = optparse.OptionParser("acmd assets <import|touch> [options] <file>")
parser.add_option("-r", "--raw",
                  action="store_const", const=True, dest="raw",
                  help="output raw response data")
parser.add_option("-D", "--dry-run",
                  action="store_const", const=True, dest="dry_run",
                  help="Do not change repository")
parser.add_option("-d", "--destination", dest="destination_root",
                  help="The root directory to import to")
parser.add_option("-l", "--lock-dir", dest="lock_dir",
                  help="Directory to store information on uploaded files")


class AssetException(Exception):
    pass


@tool('assets')
class AssetsTool(object):
    """ Manage AEM DAM assets """

    def __init__(self):
        self.created_paths = set([])
        # TODO, separate per server
        self.lock_dir = ROOT_IMPORT_DIR
        self.total_files = 1
        self.current_file = 1

    def execute(self, server, argv):
        options, args = parser.parse_args(argv)
        log("Cache dir is {}".format(self.lock_dir))

        action = get_command(args)
        actionarg = get_argument(args)

        if action == 'import':
            return self.import_path(server, options, actionarg)
        else:
            error("Unknown action {}".format(action))
            return USER_ERROR

    def import_path(self, server, options, path):
        """ Import generic file system path, could be file or dir """
        if options.lock_dir is not None:
            self.lock_dir = options.lock_dir
        else:
            self.lock_dir = ROOT_IMPORT_DIR + "/" + hash_job(server, path)
        if os.path.isdir(path):
            return self.import_directory(server, options, path)
        else:
            import_root = os.path.dirname(path)
            if options.destination_root is not None:
                import_root = options.destination_root
            return self.import_file(server, options, import_root, path)

    def import_directory(self, server, options, rootdir):
        """ Import directory recursively """
        assert os.path.isdir(rootdir)

        self.total_files = _count_files(rootdir)
        log("Importing {n} files in {path}".format(n=self.total_files, path=rootdir))

        status = OK
        for subdir, dirs, files in os.walk(rootdir):
            # _create_dir(server, subdir)
            for filename in files:
                try:
                    filepath = os.path.join(subdir, filename)
                    if _filter(filename):
                        log("Skipping {path}".format(path=filepath))
                        continue
                    self.import_file(server, options, rootdir, filepath)
                    self.current_file += 1
                except AssetException as e:
                    error("Failed to import {}: {}".format(filepath, e.message))
                    status = SERVER_ERROR
        return status

    def _lock_file(self, filepath):
        """ Return the filepath to the lock file for a given file """
        if filepath.startswith('/'):
            filepath = filepath[1:]
        return os.path.join(self.lock_dir, filepath)

    def import_file(self, server, options, local_import_root, filepath):
        """ Import single file """
        assert os.path.isfile(filepath)
        t0 = time.time()
        lock_file = self._lock_file(filepath)
        if os.path.exists(lock_file):
            msg = "{ts}\t{i}/{n}\tSkipping {local}\n".format(ts=format_timestamp(time.time()),
                                                             i=self.current_file,
                                                             n=self.total_files,
                                                             local=filepath)
            sys.stdout.write(msg)
            return OK

        dam_path = get_dam_path(filepath, local_import_root, options.destination_root)

        log("Uplading {} to {}".format(filepath, dam_path))

        if dam_path not in self.created_paths:
            _create_dir(server, dam_path, options.dry_run)
            self.created_paths.add(dam_path)
        else:
            log("Skipping creating dam path {}".format(dam_path))

        _post_file(server, filepath, dam_path, options.dry_run)
        t1 = time.time()
        benchmark = '{0:.3g}'.format(t1 - t0)
        sys.stdout.write("{ts}\t{i}/{n}\t{local} -> {dam}\t{benchmark}\n".format(ts=format_timestamp(t1),
                                                                                 i=self.current_file,
                                                                                 n=self.total_files,
                                                                                 local=filepath, dam=dam_path,
                                                                                 benchmark=benchmark))
        _touch(lock_file)
        return OK


def get_dam_path(filepath, local_import_root, dam_import_root):
    local_dir = os.path.dirname(filepath)
    if dam_import_root is None:
        dam_import_root = os.path.join('/content/dam', os.path.basename(local_import_root))
    dam_path = create_dam_path(local_dir, local_import_root, dam_import_root)
    return dam_path


def create_dam_path(local_path, local_import_root, dam_import_root):
    """ Returns <ok>, <path> """
    return local_path.replace(local_import_root, dam_import_root)


def clean_path(path):
    """ Replace spaces in target path """
    ret = path.replace(' ', '_')
    pattern = re.compile("[a-zA-Z0-9_/-]+")
    if pattern.match(ret) is None:
        raise AssetException("File path {} contains unallowed characters".format(path))
    return ret


def format_timestamp(t):
    return datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')


def hash_job(server, path):
    """ Produce unique folder for upload based on path and server """
    return hashlib.sha1('{}:{}'.format(server.name, path)).hexdigest()[:8]


def _create_dir(server, path, dry_run):
    """ Create file in the DAM
        e.g. curl -s -u admin:admin -X POST -F "jcr:primaryType=sling:OrderedFolder" $HOST$dampath > /dev/null
    """
    if dry_run:
        log("SKipping creating folder, dry run")
        return

    form_data = {'jcr:primaryType': 'sling:OrderedFolder'}
    url = server.url(path)
    log("POSTing to {}".format(url))
    resp = requests.post(url, auth=server.auth, data=form_data)
    if not _ok(resp.status_code):
        raise AssetException("Failed to create directory {}\n{}".format(url, resp.content))


def _post_file(server, filepath, dst_path, dry_run):
    """ POST single file to DAM
        curl -v -u admin:admin -X POST -i -F "file=@\"$FILENAME\"" $HOST$dampath.createasset.html &> $tempfile
    """
    assert os.path.isfile(filepath)

    if dry_run:
        return OK

    filename = os.path.basename(filepath)
    f = open(filepath, 'rb')
    mime, enc = mimetypes.guess_type(filepath)
    log("Uploading {} as {}, {}".format(f, mime, enc))
    form_data = dict(
        file=(filename, f, mime, dict()),
        fileName=filename
    )

    url = server.url("{path}.createasset.html".format(path=dst_path, filename=os.path.basename(filepath)))
    log("POSTing to {}".format(url))
    resp = requests.post(url, auth=server.auth, files=form_data)
    if not _ok(resp.status_code):
        raise AssetException("Failed to upload file {}\n{}".format(filepath, resp.content))
    return OK


def _filter(filename):
    """ Returns true for hidden or unwanted files """
    return filename.startswith(".")


def _ok(status_code):
    """ Returns true if http status code is considered success """
    return status_code == 200 or status_code == 201


def _touch(filename):
    """ Create empty file """
    par_dir = os.path.dirname(filename)
    if not os.path.exists(par_dir):
        log("Creating directory {}".format(par_dir))
        os.makedirs(par_dir, mode=0755)
    log("Creating lock file {}".format(filename))
    open(filename, 'a').close()


def _count_files(dirpath):
    """ Return the number of files in directory """
    i = 0
    for subdir, dirs, files in os.walk(dirpath):
        for filename in files:
            if not _filter(filename):
                i += 1
    return i
