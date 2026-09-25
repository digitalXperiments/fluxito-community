from pathlib import Path

from jinja2 import Environment

PROFILE_TEMPLATE = Path("app/templates/profile.html")


def test_profile_template_syntax_valid():
    source = PROFILE_TEMPLATE.read_text()
    Environment().parse(source)


def test_profile_tabs_structure_and_order():
    source = PROFILE_TEMPLATE.read_text()
    assert 'id="pfTabs"' in source
    assert 'data-tab="general"' in source
    assert 'data-tab="preferences"' in source
    assert 'data-tab="tokens"' in source
    assert 'data-tab="activity"' in source
    assert 'data-tab="ai"' not in source

    # Tab contents carry matching data-pf-tab markers
    assert 'data-pf-tab="general"' in source
    assert 'data-pf-tab="preferences"' in source
    assert 'data-pf-tab="tokens"' in source
    assert 'data-pf-tab="activity"' in source
    assert 'data-pf-tab="ai"' not in source


def test_profile_uses_redesign_panels_and_rows():
    source = PROFILE_TEMPLATE.read_text()
    # Prototype layout: ui-panel cards with label/control ui-rowf rows.
    assert 'class="ui-panel"' in source
    assert 'class="ui-rowf"' in source
    assert "ui-pill-opt" in source
    assert 'id="profileAvatar"' in source
    assert 'id="profileDisplayName"' in source
    # The old MCP Tokens tab points users to AI & MCP.
    assert 'href="/ai#tokens"' in source
    # Rail highlight follows the active tab.
    assert "settings:tab" in source


def test_profile_preserves_required_hooks_and_forms():
    source = PROFILE_TEMPLATE.read_text()
    for hook in (
        "profileForm",
        "displayName",
        "emailField",
        "sendResetBtn",
        "saveProfileBtn",
        "danger",
        "accountActions",
        "pfClientGrid",
        "pfMcpUrl",
        "pfCopyMcpUrl",
        "mcpTokensCard",
        "mcpTokenForm",
        "mcpTokenName",
        "mcpTokenExpiry",
        "mcpCreateBtn",
        "mcpPatReveal",
        "mcpPatPlain",
        "mcpPatSnippet",
        "mcpPatList",
        "usageModal",
        "usageModalBody",
        "pfToolCalls",
        "savePrefsBtn",
    ):
        assert f'id="{hook}"' in source, f"Missing required hook: {hook}"


def test_profile_js_functions_defined():
    source = PROFILE_TEMPLATE.read_text()
    for fn in (
        "Fluxito._saveProfile",
        "Fluxito._sendPasswordReset",
        "Fluxito._openUsageModal",
        "Fluxito._deactivateAccount",
        "Fluxito._reactivateAccount",
        "Fluxito._deleteAccount",
        "Fluxito._savePrefs",
        "Fluxito._savePrefsForm",
        "Fluxito._createMcpPat",
        "Fluxito._addPatToList",
        "Fluxito._revokeMcpPat",
    ):
        assert fn in source, f"Missing JS function: {fn}"
