from __future__ import annotations

import pytest

from orchestrator.router import classify

# (title, description, expected task_type, expected capabilities (subset), forbidden capabilities, complexity, risk)
CASES = [
    ("generate a logo image for the app", "", "visual_asset", {"image_generation"}, set(), None, None),
    ("analyze this screenshot", "", "visual_asset", {"screenshot_analysis"}, {"image_generation", "image_understanding"}, None, None),
    ("build the settings page UI", "", "frontend", {"ui_implementation", "coding"}, {"image_generation"}, None, None),
    ("animate the sprite in PixiJS", "", "coding", {"animation", "coding"}, {"image_generation", "pixel_art"}, None, None),
    ("create pixel art for the office characters", "", "visual_asset", {"pixel_art"}, {"animation"}, None, None),
    ("edit the photo: remove the background", "", "visual_asset", {"image_editing"}, {"image_generation"}, None, None),
    ("describe the diagram image", "", "visual_asset", {"image_understanding"}, {"screenshot_analysis"}, None, None),
    ("generate a promo video", "", "visual_asset", {"video_generation"}, {"image_generation"}, None, None),
    ("create a wireframe and mockup for onboarding", "", "visual_asset", {"visual_design"}, set(), None, None),
    ("check the page in the browser with playwright", "", "browser_check", {"browser_inspection"}, set(), None, None),
    ("fix a typo in the readme", "", "documentation", {"documentation"}, set(), "low", "low"),
    ("write unit tests for the scheduler", "", "testing", {"coding"}, set(), "high", "low"),
    ("review the authentication code", "", "review", {"review"}, set(), None, "high"),
    ("research options for a job queue", "", "research", {"research"}, set(), None, None),
    ("plan the milestones for the v2 release", "", "planning", {"planning"}, set(), None, None),
    ("design the system architecture for plugins", "", "architecture", {"planning"}, set(), "high", None),
    ("implement the repository layer", "", "coding", {"coding"}, set(), "medium", "low"),
    ("refactor the db module across the codebase", "", "coding", {"coding"}, set(), "high", None),
    ("deploy to production and drop table users", "", "coding", {"coding"}, set(), None, "critical"),
    ("rotate the credentials and update secrets", "", "coding", {"coding"}, set(), None, "critical"),
    ("add password hashing", "", "coding", {"coding"}, set(), None, "high"),
    ("add a retry helper", "", "coding", {"coding"}, {"ui_implementation"}, "medium", "low"),
    ("implement the dashboard page component in React", "", "frontend", {"ui_implementation"}, set(), None, None),
    ("tweak tailwind styles on the sidebar", "", "frontend", {"ui_implementation"}, set(), None, None),
    ("document the HTTP API", "", "documentation", {"documentation"}, set(), None, None),
    ("rename a variable", "tiny cleanup", "coding", {"coding"}, set(), "low", "low"),
    ("analyze the screenshot of the login page", "", "frontend", {"screenshot_analysis"}, {"image_generation"}, None, None),
    ("generate an illustration for the landing hero", "", "visual_asset", {"image_generation"}, {"screenshot_analysis"}, None, None),
    ("make the modal animate in with a spring", "", "frontend", {"animation", "ui_implementation"}, {"image_generation"}, None, None),
    ("something vague", "", "coding", {"coding"}, set(), "medium", "low"),
]


@pytest.mark.parametrize("title,desc,ttype,caps,forbidden,complexity,risk", CASES, ids=[c[0][:40] for c in CASES])
def test_classification_table(title, desc, ttype, caps, forbidden, complexity, risk):
    c = classify(title, desc)
    assert c.task_type == ttype, c.signals
    assert caps <= set(c.required_capabilities), c.signals
    assert not (forbidden & set(c.required_capabilities)), c.signals
    if complexity:
        assert c.complexity == complexity, c.signals
    if risk:
        assert c.risk == risk, c.signals
    assert c.signals, "classification must be explainable"


def test_case_count():
    assert len(CASES) >= 25


def test_explicit_task_type_and_role_win():
    c = classify("generate a logo image", task_type="coding", role="security")
    assert c.task_type == "coding" and c.role == "security"
    assert "task_type:coding explicit" in c.signals and "role:security explicit" in c.signals
    assert "image_generation" in c.required_capabilities  # capability detection still reports the text


def test_explicit_visual_asset_defaults_to_image_generation():
    c = classify("need some art", task_type="visual_asset")
    assert c.required_capabilities == ["image_generation"]


def test_hints_override():
    c = classify("add a retry helper", hints={"complexity": "critical", "risk": "high", "required_capabilities": ["research"]})
    assert (c.complexity, c.risk) == ("critical", "high")
    assert "research" in c.required_capabilities


def test_invalid_hint_rejected():
    with pytest.raises(ValueError):
        classify("x", hints={"complexity": "huge"})


def test_long_description_bumps_complexity():
    c = classify("add a retry helper", "details " * 300)
    assert c.complexity == "high"


def test_deterministic():
    a = classify("animate the sprite in PixiJS", "smooth walk cycle")
    b = classify("animate the sprite in PixiJS", "smooth walk cycle")
    assert a == b


@pytest.mark.parametrize("title", ["E2E claude1: write answer file", "e2e smoke for the API", "Add E2E coverage"])
def test_e2e_alone_is_not_browser_inspection(title):
    c = classify(title, "Create a file named answer.txt. Do not run any commands.")
    assert "browser_inspection" not in c.required_capabilities
    assert c.task_type != "browser_check"


def test_browser_signals_still_detected_and_marked_inferred():
    c = classify("Check the rendered page in the browser", "")
    assert "browser_inspection" in c.required_capabilities
    assert "browser_inspection" in c.inferred_capabilities


def test_hint_capability_is_explicit_not_inferred():
    c = classify("write docs", "", hints={"required_capabilities": ["browser_inspection"]})
    assert c.explicit_capabilities == ["browser_inspection"]
    assert "browser_inspection" not in c.inferred_capabilities
