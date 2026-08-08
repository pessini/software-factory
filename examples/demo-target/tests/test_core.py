from demo_target import greeting


def test_greeting() -> None:
    assert greeting("Factory") == "Hello, Factory!"
