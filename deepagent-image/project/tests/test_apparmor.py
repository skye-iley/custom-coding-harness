"""Narrow-AppArmor-profile invariants (M4 slice J, invariant 38).

The LSM twin of `test_seccomp.py`. Slice J buys the jail's ability to *start* on
an AppArmor host by relaxing one rule in Docker's `docker-default` profile, which
is only a good trade while the relaxation stays that narrow. A regression that
pasted a `mount,` catch-all to get unblocked -- `apparmor=unconfined` wearing a
costume -- has to fail here rather than sail through because the jail now works.

Host-runnable, stdlib only, no network and no AppArmor kernel: the sync path's
fetch is not exercised, only the pure transforms, the pinned upstream fixture,
and the committed artifacts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _bootstrap import _load

apparmor = _load("apparmor")

FIXTURE = Path(__file__).parent / "fixtures" / "apparmor_template_v28.0.1.go"


def _upstream_template() -> str:
    return apparmor.extract_template(FIXTURE.read_text(encoding="utf-8"))


def _rendered() -> str:
    return apparmor.render_template(_upstream_template())


# --- extraction --------------------------------------------------------------


def test_extract_finds_the_base_template():
    """Upstream ships a Go template, not a finished profile -- pin the extraction.

    If moby restructures template.go this must raise, not return a plausible
    fragment: a half-extracted profile would render something nobody reviewed.
    """
    template = _upstream_template()

    assert apparmor.DENY_MOUNT_RULE in template
    assert "{{.Name}}" in template


def test_extract_rejects_a_file_without_the_template():
    with pytest.raises(apparmor.TemplateError):
        apparmor.extract_template("package apparmor\n// nothing here\n")


def test_extract_rejects_a_template_missing_the_deny_mount_rule():
    """Our whole diff targets that line; if upstream drops it, stop and re-read."""
    source = FIXTURE.read_text(encoding="utf-8").replace(apparmor.DENY_MOUNT_RULE, "mount,")

    with pytest.raises(apparmor.TemplateError):
        apparmor.extract_template(source)


# --- rendering ---------------------------------------------------------------


def test_render_substitutes_the_pinned_parameters():
    rendered = _rendered()

    assert f"profile {apparmor.PROFILE_NAME} " in rendered
    assert apparmor.RENDER_IMPORTS[0] in rendered
    assert apparmor.RENDER_INNER_IMPORTS[0] in rendered
    # No directive survives rendering.
    assert "{{" not in rendered and "}}" not in rendered


def test_render_expands_the_signal_peer_to_our_own_profile_name():
    """`signal (send,receive) peer={{.Name}}` must name this profile, not upstream's."""
    rendered = _rendered()

    assert f"signal (send,receive) peer={apparmor.PROFILE_NAME}," in rendered
    assert f"ptrace (trace,read,tracedby,readby) peer={apparmor.PROFILE_NAME}," in rendered


def test_render_raises_on_an_unknown_directive():
    """A renderer that ignores what it does not understand is the worst outcome.

    It would emit a profile that looks plausible and enforces something other
    than what upstream wrote, with no error anywhere.
    """
    template = _upstream_template().replace("{{.Name}}", "{{.Frobnicate}}", 1)

    with pytest.raises(apparmor.TemplateError):
        apparmor.render_template(template)


def test_render_raises_when_the_imports_range_disappears():
    """Otherwise the pinned includes silently stop being emitted."""
    template = "profile {{.Name}} flags=(attach_disconnected) {\n  file,\n}\n"

    with pytest.raises(apparmor.TemplateError):
        apparmor.render_template(template)


# --- the relaxation ----------------------------------------------------------


def test_relax_mount_replaces_exactly_the_deny_line():
    rendered = _rendered()

    relaxed = apparmor.relax_mount(rendered)

    assert apparmor.DENY_MOUNT_RULE not in relaxed
    for rule in apparmor.RELAXED_MOUNT_RULES:
        assert f"  {rule}" in relaxed


def test_relax_mount_leaves_every_other_line_byte_identical():
    """The diff property: one line becomes seven (+ a comment), nothing else moves."""
    rendered = _rendered()
    relaxed = apparmor.relax_mount(rendered)

    before = [ln for ln in rendered.splitlines() if ln.strip() != apparmor.DENY_MOUNT_RULE]
    added = {f"  {rule}" for rule in apparmor.RELAXED_MOUNT_RULES}
    after = [
        ln
        for ln in relaxed.splitlines()
        if ln not in added and not ln.strip().startswith("# holder M4 slice J")
        and not ln.strip().startswith("# the fs jail")
        and not ln.strip().startswith("# construction and asserted")
    ]

    assert after == before


def test_relax_mount_refuses_when_the_deny_line_is_absent_or_doubled():
    """Both mean upstream's shape moved; a blind edit would be unreviewed."""
    with pytest.raises(apparmor.TemplateError):
        apparmor.relax_mount("profile x {\n  file,\n}\n")

    doubled = _rendered().replace(
        f"  {apparmor.DENY_MOUNT_RULE}",
        f"  {apparmor.DENY_MOUNT_RULE}\n  {apparmor.DENY_MOUNT_RULE}",
    )
    with pytest.raises(apparmor.TemplateError):
        apparmor.relax_mount(doubled)


# --- verification ------------------------------------------------------------


def _good_profile() -> str:
    body = apparmor.relax_mount(_rendered())
    return apparmor._with_header(body, "test")


def test_a_freshly_generated_profile_verifies():
    assert apparmor.verify_profile(_good_profile()) == []


def test_stock_docker_default_is_rejected():
    """Forgetting to relax at all is a jail that cannot start -- catch it here."""
    stock = apparmor._with_header(_rendered(), "test")

    problems = apparmor.verify_profile(stock)

    assert any(apparmor.DENY_MOUNT_RULE in p for p in problems)


def test_bare_mount_catch_all_is_rejected():
    """The tempting shortcut when a live host denies something. It is unconfined."""
    text = _good_profile().replace("  mount fstype=tmpfs,", "  mount,")
    text = apparmor._with_header(apparmor.split_header(text)[1], "test")

    problems = apparmor.verify_profile(text)

    assert any("catch-all" in p for p in problems)


def test_widened_mount_rule_set_is_rejected():
    body = apparmor.split_header(_good_profile())[1].replace(
        "  pivot_root,", "  pivot_root,\n  mount fstype=cgroup,"
    )
    text = apparmor._with_header(body, "test")

    problems = apparmor.verify_profile(text)

    assert any("drifted" in p and "cgroup" in p for p in problems)


def test_missing_mount_rule_is_rejected():
    """Dropping one breaks the jail; catch it here rather than at bwrap exec."""
    body = apparmor.split_header(_good_profile())[1].replace("  mount fstype=proc -> /proc/,\n", "")
    text = apparmor._with_header(body, "test")

    problems = apparmor.verify_profile(text)

    assert any("drifted" in p and "proc" in p for p in problems)


def test_removing_an_upstream_deny_rule_is_rejected():
    """The relaxation must narrow the mount rule only, not shed other protections."""
    body = apparmor.split_header(_good_profile())[1].replace(
        "  deny @{PROC}/sysrq-trigger rwklx,\n", ""
    )
    text = apparmor._with_header(body, "test")

    problems = apparmor.verify_profile(text)

    assert any("sysrq-trigger" in p for p in problems)


def test_renamed_profile_is_rejected():
    """`--security-opt apparmor=<name>` selects by name: a rename is unloadable."""
    body = apparmor.split_header(_good_profile())[1].replace(
        f"profile {apparmor.PROFILE_NAME} ", "profile something-else "
    )
    text = apparmor._with_header(body, "test")

    problems = apparmor.verify_profile(text)

    assert any("not named" in p for p in problems)


def test_hand_edit_breaks_the_recorded_hash():
    """The artifact is generated; 'do not hand-edit' is enforced, not requested."""
    text = _good_profile().replace("  network,", "  network,\n  # sneaked in")

    problems = apparmor.verify_profile(text)

    assert any("hash" in p for p in problems)


def test_baseline_diff_property_catches_a_change_outside_the_mount_rule():
    """The strongest offline check: profile == relax_mount(vendored upstream)."""
    baseline = apparmor._with_header(_rendered(), "baseline")
    body = apparmor.split_header(_good_profile())[1].replace("  network,", "  network,\n  dbus,")
    tampered = apparmor._with_header(body, "test")

    problems = apparmor.verify_profile(tampered, baseline)

    assert any("relax_mount" in p for p in problems)


def test_verify_baseline_rejects_a_baseline_that_is_not_docker_default():
    problems = apparmor.verify_baseline("profile x {\n  file,\n}\n")

    assert any(apparmor.DENY_MOUNT_RULE in p for p in problems)


# --- the committed artifacts -------------------------------------------------


def test_committed_profile_is_narrow():
    """The artifact actually shipped is docker-default plus exactly our mount diff.

    This is the CI regression guard (`apparmor-sync --check`) as a unit test: it
    is what fails if someone regenerates from a different source, hand-edits the
    profile wider, or swaps in a permissive rule to unblock a denial.
    """
    profile_path = apparmor.profile_path()
    baseline_path = apparmor.baseline_path()
    assert profile_path.exists(), f"vendored profile missing: {profile_path}"
    assert baseline_path.exists(), f"vendored baseline missing: {baseline_path}"

    profile = apparmor.load_text(profile_path)
    baseline = apparmor.load_text(baseline_path)

    assert apparmor.verify_profile(profile, baseline) == []
    assert apparmor.verify_baseline(baseline) == []


def test_committed_baseline_is_reproducible_from_the_pinned_template():
    """The vendored upstream render is what the pinned tag actually says.

    Without this, the diff property (profile == relax_mount(baseline)) could be
    satisfied by a baseline someone quietly weakened to match a widened profile.
    """
    committed = apparmor.split_header(apparmor.load_text(apparmor.baseline_path()))[1]

    assert committed.strip() == _rendered().strip()


def test_committed_profile_keeps_every_critical_deny():
    body = apparmor.split_header(apparmor.load_text(apparmor.profile_path()))[1]

    for rule in apparmor.CRITICAL_DENY_RULES:
        assert rule in body
