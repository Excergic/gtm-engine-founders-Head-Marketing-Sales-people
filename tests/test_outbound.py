from gtm.icp import email_matches_company
from gtm.outbound import offer_for, style_problems


def test_email_domain_must_look_like_the_company():
    assert email_matches_company("ad@blue-pill.ai", "BluePill")
    assert email_matches_company("thomas@dcubed-space.com", "Dcubed")
    assert email_matches_company("jasmine@quilr.ai", "WhiteMatter (formerly QuilrAI)")
    assert email_matches_company("burkay@fal.ai", "fal")
    assert email_matches_company("vasek@e2b.dev", "E2B")
    assert email_matches_company("patrick@7ai.com", "7AI")
    # previous employers returned by enrichment
    assert not email_matches_company("angelica.tigan@blackrock.com", "FinBursa")
    assert not email_matches_company("mmidkiff@cylinderhealth.com", "Vida Health")
    assert not email_matches_company(None, "Acme")


def test_offer_follows_the_signal():
    assert offer_for("Posted on X: hiring a GTM Engineer in SF") == "role"
    assert offer_for("Raised $7M seed to grow sales") == "project"


def draft(**kw):
    body = (
        "Hi Ana,\n\nCongrats on the round at Acme. "
        + "word " * 40
        + "\n\nIf this isn't relevant, reply \"no\" and I won't email again."
    )
    return {"subject": "Acme after the round", "body": body, "company": "Acme"} | kw


def test_house_style():
    assert style_problems(draft()) == []
    assert "em/en dash" in style_problems(draft(subject="Acme — hi"))
    assert any("banned" in p for p in style_problems(draft(subject="Unlock Acme")))
    assert "no opt-out line" in style_problems(draft(body="Hi, Acme " + "word " * 50))
    assert "unfilled placeholder" in style_problems(draft(subject="Acme [CONFIRM]"))
