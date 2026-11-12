import pytest

from .harness import fake_psycopg2


@pytest.fixture(autouse=True)
def _no_tiaas_database(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake_psycopg2())
