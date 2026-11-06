"""Real Redis AOF durability with persistent SQLite and no network listener."""

import hashlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from marina import create_app
from tests.test_identity import app, signed


class PrivateRedis:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.socket = self.directory / "redis.sock"
        self.process = None
        self.url = "unix://" + str(self.socket)
        self.client = redis.Redis.from_url(
            self.url,
            decode_responses=True,
            socket_connect_timeout=0.1,
            socket_timeout=0.1,
            retry=Retry(NoBackoff(), 0),
        )

    def start(self):
        assert self.process is None
        self.process = subprocess.Popen(
            [
                shutil.which("redis-server"),
                "--port",
                "0",
                "--unixsocket",
                str(self.socket),
                "--unixsocketperm",
                "700",
                "--dir",
                str(self.directory),
                "--save",
                "",
                "--appendonly",
                "yes",
                "--appendfsync",
                "always",
                "--daemonize",
                "no",
                "--logfile",
                str(self.directory / "redis.log"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert self.process.poll() is None, (
                "Private Redis exited: " + (self.directory / "redis.log").read_text()
            )
            try:
                if self.client.ping():
                    return
            except redis.RedisError:
                time.sleep(0.02)
        raise AssertionError("Private Redis did not become ready")

    def stop(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        finally:
            self.process = None
            self.client.connection_pool.disconnect()


@pytest.fixture
def private_redis():
    if not shutil.which("redis-server"):
        pytest.skip("redis-server is needed for the real Redis durability test")
    # Keep the path short enough for a Unix-domain socket on every CI runner.
    with tempfile.TemporaryDirectory(prefix="mr-", dir="/tmp") as directory:
        server = PrivateRedis(directory)
        try:
            server.start()
            yield server
        finally:
            server.stop()
            server.client.close()


def test_session_survives_real_redis_restart_and_complete_outage(app, private_redis):
    config = {**app.config, "REDIS_URL": private_redis.url}
    before = create_app(config)
    client = before.test_client()
    response = client.post("/api/auth/telegram", json={"init_data": signed(2, app.clock[0])})
    assert response.status_code == 200, response.json
    token = response.json["token"]
    owner = response.json["account"]["id"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    key = "marina:session:" + digest
    cached = private_redis.client.get(key)
    assert cached is not None
    assert private_redis.client.ttl(key) > 0
    assert private_redis.client.config_get("port")["port"] == "0"
    assert private_redis.client.config_get("appendonly")["appendonly"] == "yes"

    private_redis.stop()
    private_redis.start()
    # Read before any application request can repopulate the mirror from SQLite.
    assert private_redis.client.get(key) == cached

    restarted = create_app(config)
    restored = restarted.test_client()
    restored.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + token
    response = restored.get("/api/me")
    assert response.status_code == 200, response.json
    assert response.json["account"]["id"] == owner

    private_redis.stop()
    response = restored.get("/api/me")
    assert response.status_code == 200, response.json
    assert response.json["account"]["id"] == owner
