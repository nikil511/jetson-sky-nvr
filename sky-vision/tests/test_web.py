import datetime as dt
import json
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from src.web import start_server


def test_dashboard_api_history_and_stream(tmp_path):
    now = dt.datetime.now(dt.timezone.utc)
    day = tmp_path / 'observations' / now.date().isoformat()
    day.mkdir(parents=True)
    row = dict(timestamp=now.isoformat(), cloud_pct=42.0, oktas=3)
    (day / 'obs_current.json').write_text(json.dumps(row))
    (day / 'obs_old.json').write_text(json.dumps({**row, 'timestamp': (now-dt.timedelta(hours=25)).isoformat()}))
    (day / 'obs_partial.json').write_text('{')
    server = start_server(tmp_path, 0)
    root = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(root) as r:
            assert b'Athens Sky Lab' in r.read()
        with pytest.raises(HTTPError) as exc:
            urlopen(root + '/api/latest')
        assert exc.value.code == 503
        (tmp_path / 'latest_observation.json').write_text(json.dumps(row))
        with urlopen(root + '/api/latest') as r:
            assert json.load(r) == row
        with urlopen(root + '/api/history') as r:
            assert json.load(r) == [row]
        with pytest.raises(HTTPError) as exc:
            urlopen(root + '/../../etc/passwd')
        assert exc.value.code == 404
        (tmp_path / 'latest_sky_debug.jpg').write_bytes(b'jpeg-test')
        with urlopen(root + '/stream.mjpeg', timeout=3) as r:
            assert 'multipart/x-mixed-replace' in r.headers['Content-Type']
            assert r.read(7) == b'--frame'
    finally:
        server.shutdown()
        server.server_close()
