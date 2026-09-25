from app.ask.prompts import build_system_prompt


def test_prompt_mentions_project_role_and_tools():
    p = build_system_prompt(project_name="Acme", connected=["Analytics", "Advertising"], role="viewer")
    assert "Acme" in p
    assert "viewer" in p
    assert "Analytics, Advertising" in p


def test_prompt_is_read_only_and_points_to_mcp_clients():
    p = build_system_prompt(project_name="Acme", connected=[], role="member")
    lower = p.lower()
    assert "read-only" in lower
    assert "mcp client" in lower
    assert "no platform tools are available" in lower


def test_prompt_has_no_removed_features_or_branding():
    p = build_system_prompt(project_name="Acme", connected=["Analytics"], role="owner").lower()
    for word in (
        "flux ",
        "ask fluxito",
        "tracking plan",
        "dashboard",
        "draft",
        "propose_card",
        "ask_choices",
    ):
        assert word not in p, word


def test_prompt_without_tools():
    p = build_system_prompt(project_name="Acme", connected=["Analytics"], role="owner", tools_enabled=False)
    assert "not available with the current model" in p
