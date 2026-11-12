import pytest

from byoc_sync.config import SiteConfig
from byoc_sync.models import ByocRequest
from byoc_sync.naming import Names


@pytest.fixture
def site():
    return SiteConfig.load("eu")


def request_for(name="alice01", **kw):
    values = dict(
        name=name, password="MyPulS4rP4ssw0rd", max_cores=8, max_mem=31, min_gpus=0, max_gpus=0, nfs_path=None
    )
    values.update(kw)
    return ByocRequest(**values)


@pytest.fixture
def alice(site):
    req = request_for()
    return req, Names.for_(req.name, site.naming), "alice@example.org"
