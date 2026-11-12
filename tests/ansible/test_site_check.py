"""A bundled site name is checked against the sites shipped in the cloned byoc-sync."""

from __future__ import annotations

from .conftest import good_vars


def _source(tmp_path):
    sites = tmp_path / "src" / "src" / "byoc_sync" / "sites"
    sites.mkdir(parents=True)
    (sites / "eu.yml").write_text("section: byoc_pulsar\n")
    (sites / "other.yml").write_text("section: byoc_pulsar\n")
    return str(tmp_path / "src")


def test_known_bundled_site_passes(run_role, tmp_path):
    proc = run_role("site_check", good_vars(tmp_path, byoc_sync_src_dir=_source(tmp_path), byoc_sync_site_config="eu"))
    assert proc.returncode == 0, proc.stdout


def test_unknown_bundled_site_fails_listing_the_known_ones(run_role, tmp_path):
    values = good_vars(tmp_path, byoc_sync_src_dir=_source(tmp_path), byoc_sync_site_config="usegalaxy")
    proc = run_role("site_check", values)
    assert proc.returncode != 0
    assert "byoc_sync_site_config: no bundled site 'usegalaxy' in byoc-sync (bundled: eu, other)" in proc.stdout


def test_site_file_is_not_looked_up_among_bundled_sites(run_role, tmp_path):
    site = tmp_path / "mysite.yml"
    site.write_text("section: byoc_pulsar\n")
    values = good_vars(tmp_path, byoc_sync_src_dir=_source(tmp_path), byoc_sync_site_config=str(site))
    assert run_role("site_check", values).returncode == 0
