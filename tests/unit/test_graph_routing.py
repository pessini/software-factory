from software_factory.graph import after_review, after_tests


def test_test_routing_is_bounded() -> None:
    assert after_tests({"test_passed": True}) == "review"
    assert (
        after_tests({"test_passed": False, "test_repair_count": 1, "max_test_repairs": 2})
        == "repair"
    )
    assert (
        after_tests({"test_passed": False, "test_repair_count": 2, "max_test_repairs": 2})
        == "failed"
    )


def test_review_routing_is_bounded() -> None:
    assert after_review({"review_approved": True}) == "finalize"
    assert (
        after_review(
            {"review_approved": False, "review_revision_count": 0, "max_review_revisions": 1}
        )
        == "revise"
    )
    assert (
        after_review(
            {"review_approved": False, "review_revision_count": 1, "max_review_revisions": 1}
        )
        == "failed"
    )
