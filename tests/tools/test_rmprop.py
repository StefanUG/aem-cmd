# coding: utf-8
from io import StringIO

from mock import patch
from httmock import urlmatch, HTTMock
from nose.tools import eq_

from acmd import tool_repo, Server

def _parse(body):
    lines = body.split('&')
    ret = dict()
    for line in lines:
        parts = line.split('%40')
        ret[parts[0]] = parts[1]
    return ret

@urlmatch(netloc='localhost:4502', method='POST')
def service_mock(url, request):

    expected = {
        'prop1': 'Delete=',
        'prop0': 'Delete='
    }

    eq_(expected, _parse(request.body))
    return ""


@patch('sys.stdout', new_callable=StringIO)
@patch('sys.stderr', new_callable=StringIO)
def test_rmprop(stderr, stdout):
    with HTTMock(service_mock):
        tool = tool_repo.get_tool('rmprop')
        server = Server('localhost')
        status = tool.execute(server, ['rmprop', 'prop0,prop1', '/content/path/node'])
        eq_(0, status)
        eq_('/content/path/node\n', stdout.getvalue())


@patch('sys.stdout', new_callable=StringIO)
@patch('sys.stderr', new_callable=StringIO)
@patch('sys.stdin', new=StringIO("/path0\n/path1\n"))
def test_rmprop_stdin(stderr, stdout):
    with HTTMock(service_mock):
        tool = tool_repo.get_tool('rmprop')
        server = Server('localhost')
        status = tool.execute(server, ['rmprop', 'prop0,prop1'])
        eq_(0, status)
        eq_('/path0\n/path1\n', stdout.getvalue())

