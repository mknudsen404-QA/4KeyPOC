import time

from switchboard.key_injector import FakeKeyInjector, RepeatingKeyInjector


def make_injector(**overrides):
    posted: list[tuple[int, int, bool]] = []
    kwargs = dict(initial_delay=0.0, repeat_interval=0.01, max_hold=1.0, log=lambda msg: None)
    kwargs.update(overrides)
    injector = RepeatingKeyInjector(lambda pid, keycode, down: posted.append((pid, keycode, down)), **kwargs)
    return injector, posted


def test_hold_posts_down_immediately():
    injector, posted = make_injector()
    injector.hold(1234, 49)
    assert posted == [(1234, 49, True)]
    injector.release(1234, 49)


def test_hold_repeats_while_held_then_release_posts_up():
    injector, posted = make_injector()
    injector.hold(1234, 49)
    time.sleep(0.1)  # ~10 repeat intervals
    injector.release(1234, 49)

    downs = [p for p in posted if p[2] is True]
    assert len(downs) >= 3  # repeated, not a single tap
    assert posted[-1] == (1234, 49, False)


def test_release_without_hold_is_noop():
    injector, posted = make_injector()
    injector.release(1234, 49)
    assert posted == []


def test_second_hold_before_release_is_noop():
    injector, posted = make_injector()
    injector.hold(1234, 49)
    injector.hold(1234, 49)  # already holding; must not restart or double-post
    assert posted == [(1234, 49, True)]
    injector.release(1234, 49)


def test_watchdog_auto_releases_after_max_hold():
    logged = []
    injector, posted = make_injector(max_hold=0.05, log=logged.append)
    injector.hold(1234, 49)
    time.sleep(0.2)
    assert posted[-1] == (1234, 49, False)
    assert any("watchdog" in m for m in logged)


def test_fake_key_injector_records_calls():
    injector = FakeKeyInjector()
    injector.hold(1234, 49)
    injector.release(1234, 49)
    assert injector.held == [(1234, 49)]
    assert injector.released == [(1234, 49)]
