"""Engine construction: URL normalisation and the missing-driver message.

The message matters more than it looks. SQLAlchemy imports the DBAPI while
building the engine, before any socket is opened, so a missing driver arrives as
a bare ModuleNotFoundError several frames deep — printed directly beneath the
connection string. That reads as "my database URL is wrong", and the natural next
move is to go and re-copy the URL, which cannot help.
"""

from __future__ import annotations

import builtins
import sys

import pytest

from desk.store.engine import build_engine, normalise_url


class TestNormaliseUrl:
    def test_provider_postgres_scheme_is_upgraded_to_psycopg3(self) -> None:
        assert normalise_url("postgres://u:p@h/db").startswith("postgresql+psycopg://")

    def test_postgresql_scheme_is_pinned_to_psycopg3(self) -> None:
        """Left bare, SQLAlchemy 2.0 would reach for psycopg2, which is not a
        dependency of this project."""
        assert normalise_url("postgresql://u:p@h/db").startswith("postgresql+psycopg://")

    def test_query_string_survives(self) -> None:
        out = normalise_url("postgresql://u:p@h/db?sslmode=require&channel_binding=require")
        assert out.endswith("?sslmode=require&channel_binding=require")

    def test_sqlite_is_untouched(self) -> None:
        assert normalise_url("sqlite:///x.db") == "sqlite:///x.db"

    def test_an_explicit_driver_is_not_rewritten_twice(self) -> None:
        url = "postgresql+psycopg://u:p@h/db"
        assert normalise_url(url) == url


class TestMissingUrl:
    def test_empty_url_refuses_rather_than_falling_back_to_a_local_file(self) -> None:
        with pytest.raises(ValueError, match="no silent local-file fallback"):
            build_engine("")


class TestMissingDriver:
    @pytest.fixture
    def no_psycopg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = builtins.__import__

        def fake(name: str, *args: object, **kwargs: object) -> object:
            if name == "psycopg":
                raise ModuleNotFoundError("No module named 'psycopg'", name="psycopg")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        for module in [k for k in sys.modules if k.startswith("psycopg")]:
            monkeypatch.delitem(sys.modules, module)
        monkeypatch.setattr(builtins, "__import__", fake)

    def test_the_message_names_the_dependency_and_the_fix(self, no_psycopg: None) -> None:
        with pytest.raises(ModuleNotFoundError) as caught:
            build_engine("postgresql://u:p@host/db?sslmode=require")
        message = str(caught.value)
        assert "driver is not installed" in message
        assert "[postgres]" in message

    def test_it_says_the_url_is_not_at_fault(self, no_psycopg: None) -> None:
        """The whole point: stop the reader debugging their connection string."""
        with pytest.raises(ModuleNotFoundError, match="connection string is not the problem"):
            build_engine("postgresql://u:p@host/db")

    def test_a_non_postgres_url_propagates_the_original_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the PostgreSQL case is reworded.

        Rewriting every ModuleNotFoundError into advice about the postgres extra
        would send someone chasing the wrong dependency.
        """
        import desk.store.engine as engine_module

        def boom(*args: object, **kwargs: object) -> object:
            raise ModuleNotFoundError("No module named 'something_else'", name="something_else")

        monkeypatch.setattr(engine_module, "create_engine", boom)
        with pytest.raises(ModuleNotFoundError) as caught:
            build_engine("sqlite:///x.db")
        assert "[postgres]" not in str(caught.value)
        assert "something_else" in str(caught.value)
