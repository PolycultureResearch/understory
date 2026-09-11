from understory.tenant import expand_env, tenants_dir
from understory.types import MetricSpec, TimeSpec


def test_expand_env(monkeypatch):
    monkeypatch.setenv("X_ONE", "one")
    assert expand_env("${X_ONE}/${X_TWO:-two}") == "one/two"


def test_load_all_tenants(tenants):
    assert set(tenants) == {"alpenglow", "white_cube", "meridian", "bristlecone"}
    for cfg in tenants.values():
        assert cfg.manifest_path.exists(), cfg.name
        assert cfg.log.events_prefix.endswith(f"{cfg.name}/events")


def test_spec_hash_ignores_question():
    a = MetricSpec(metrics=["net_revenue"], time=TimeSpec(grain="month"), question="x")
    b = MetricSpec(metrics=["net_revenue"], time=TimeSpec(grain="month"), question="y")
    assert a.hash() == b.hash()
    c = MetricSpec(metrics=["gross_revenue"], time=TimeSpec(grain="month"))
    assert a.hash() != c.hash()


def test_tenants_dir_points_at_repo():
    assert (tenants_dir() / "alpenglow" / "tenant.yml").exists()
