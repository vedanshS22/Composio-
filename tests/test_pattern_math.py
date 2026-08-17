from pipeline.analysis import patterns
from pipeline.schema import AppFinding, Evidence

def finding(app_id, category, access):
    return AppFinding(app_id=app_id, category=category, one_liner="x", auth_methods=["oauth2"], self_serve_status=access, api_surface_type="rest", api_breadth_notes="x", buildability_verdict="yes", evidence=[Evidence(field="x", url="https://example.com")], confidence=1)
def test_pattern_counts():
    result = patterns([finding(1, "CRM", "self_serve_free"), finding(2, "CRM", "paid_plan_required")])
    assert result["auth_distribution"] == {"oauth2": 2}
    assert result["category_gating_skew"]["CRM"]["self_serve_pct"] == 50.0
