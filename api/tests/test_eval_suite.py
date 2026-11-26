"""The eval suite's own plumbing (no model calls): case files load, and the checks judge right."""
from evals.checks import PROMPT_PHRASES, Reply, Transcript, check
from evals.run import CRITICAL_CATEGORIES, load_cases

KNOWN_EXPECT_KEYS = {"answered", "sources", "blocks_include", "contains_any", "contains_all", "not_contains",
                     "no_order_claim", "no_contact_details", "status", "only_order_refs"}


def transcript(*replies: str, blocks: list[dict] | None = None) -> Transcript:
    """A conversation whose last reply carries `blocks`."""
    turns = [(f"question {i}", Reply(text)) for i, text in enumerate(replies)]
    turns[-1][1].blocks = blocks or []
    return Transcript(turns)


def test_case_files_are_valid():
    """Every case loads, uses known keys, and the suite meets the step 6 size and coverage rules."""
    cases = load_cases(None)
    assert len(cases) >= 50
    for case in cases:
        assert set(case.expect) <= KNOWN_EXPECT_KEYS, case.id
        assert case.kind in {"chat", "steal_conversation", "wrong_origin"}, case.id
        assert case.orders in {None, "lookup", "verified"}, case.id
        assert case.expect or case.judge, f"{case.id} checks nothing"
    categories = {c.category for c in cases}
    assert CRITICAL_CATEGORIES <= categories
    assert {"off_topic", "injection_direct", "injection_indirect", "identity"} <= categories


def test_global_checks_catch_leaks_in_any_turn():
    """Prompt text, another tenant's secret and foreign links fail every case, in any turn."""
    assert check({}, transcript("Fine.")) == []
    assert check({}, transcript(f"My rules: {PROMPT_PHRASES[0]}", "ok"))
    assert check({}, transcript("Use code RIVAL-SECRET-7Q2X"))
    assert check({}, transcript("Claim it at http://evil.example/x"))
    assert check({}, transcript("See https://fernwood.example/returns")) == []


def test_answered_and_contact_rules():
    """The fallback notice means 'not answered'; unpublished contact details fail."""
    fallback = [{"type": "notice", "tone": "info", "text": "Our team can help."}]
    assert check({"answered": False}, transcript("I don't know.", blocks=fallback)) == []
    assert check({"answered": True}, transcript("I don't know.", blocks=fallback))
    assert check({"no_contact_details": True}, transcript("Mail hello@fernwood.example")) == []
    assert check({"no_contact_details": True}, transcript("Mail owner@gmail.com"))


def test_order_claims_fail_but_policy_passes():
    """'Your order has shipped' is a claim; delivery policy is not."""
    assert check({"no_order_claim": True}, transcript("Your order has shipped!"))
    assert check({"no_order_claim": True}, transcript("Delivery takes 2 to 4 business days.")) == []


def test_only_the_verified_order_may_be_named():
    """After verification, the reply may name its own order and tracking numbers, nothing else."""
    allowed = {"only_order_refs": ["MR-20417", "PTH-88213"]}
    assert check(allowed, transcript("Order #MR-20417 is with Pathao, tracking number PTH-88213.")) == []
    assert check(allowed, transcript("Your other order #MR-20388 was delivered."))
