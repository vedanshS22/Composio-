import pytest
from pydantic import ValidationError
from pipeline.schema import AppFinding, Evidence

BASE = dict(app_id=1, category="Test", one_liner="A test app.", auth_methods=["api_key"], self_serve_status="self_serve_free", api_surface_type="rest", api_breadth_notes="A few documented endpoints.", buildability_verdict="yes", evidence=[Evidence(field="auth_methods", url="https://example.com/docs")], confidence=.8)

def test_valid_finding(): assert AppFinding(**BASE).auth_methods == ["api_key"]
def test_unknown_is_partial_not_a_product_no():
    finding = AppFinding(**{**BASE, "self_serve_status": "unknown", "buildability_verdict": None})
    assert finding.research_status == "partial"
def test_rejects_bad_auth():
    with pytest.raises(ValidationError): AppFinding(**{**BASE, "auth_methods": ["password"]})
