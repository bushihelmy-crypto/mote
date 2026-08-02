from pathlib import Path

from mote.product.toolsets.builtin.web_browser import WebBrowser


def test_profile_write_is_explicit_permission_visible_action() -> None:
    tool = WebBrowser()
    tool.get_browser_profile = lambda: "account"
    tool.get_browser_profile_target = lambda name: f"/profiles/{name}.profile"
    assert tool.mutates_filesystem_for({"action": "save_profile"}) is True
    assert tool.permission_targets({"action": "save_profile"}) == ["/profiles/account.profile"]
    assert tool.mutates_filesystem_for({"action": "navigate"}) is False


def test_successful_browser_action_does_not_implicitly_persist_profile() -> None:
    root = Path(__file__).parents[2]
    source = (root / "product/toolsets/builtin/web_browser.py").read_text(encoding="utf-8")
    call_body = source[source.index("    async def call(") : source.index("    def check_permissions")]
    assert "await self._persist_profile(driver.session)" not in call_body
    assert 'if action == "save_profile"' in call_body
