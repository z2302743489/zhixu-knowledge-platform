import os
import tempfile
from pathlib import Path

_test_dir = Path(tempfile.mkdtemp(prefix="zhixu-tests-"))
os.environ["ZHIXU_DATA_DIR"] = str(_test_dir)
os.environ["ZHIXU_DATABASE_URL"] = "sqlite:///" + str(_test_dir / "test.db")
os.environ["ZHIXU_TESTING"] = "true"
os.environ["ZHIXU_TASK_MODE"] = "eager"
os.environ["ZHIXU_SECRET_KEY"] = ""
os.environ["ZHIXU_ALLOWED_PRIVATE_HOSTS"] = "127.0.0.1,localhost"

import pytest
from fastapi.testclient import TestClient
from app.db import Base, SessionLocal, engine


@pytest.fixture(autouse=True)
def database():
    from app import models
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as value:
        yield value
