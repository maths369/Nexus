from nexus.api.app import app


def test_app_imports_cleanly():
    assert app.title == 'Nexus / 星策'
