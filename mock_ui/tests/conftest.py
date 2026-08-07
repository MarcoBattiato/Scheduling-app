"""Keep the tests away from the real saved session.

`app.py` holds `SNAPSHOT` as a module global pointing at the session file next
to the package, and the tests exercise the real `app` object — so without this,
any test that resets, saves, or merely mutates would write over, or delete,
whatever the person running the suite had built up in the browser.

That is not hypothetical: it happened. `/api/reset` deletes the snapshot, which
is right for the application and disastrous for a test suite pointed at the
same path.
"""
import pytest


@pytest.fixture(autouse=True, scope="session")
def isolate_the_session_file(tmp_path_factory):
    from mock_ui import app

    original = app.SNAPSHOT
    app.SNAPSHOT = tmp_path_factory.mktemp("mock_ui") / "session.json"
    yield
    app.SNAPSHOT = original
