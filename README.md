# gbp-integration

Google Business Profile (GBP) integration — internal project. This repository
holds the data foundation: SQLite schema, a minimal repository layer, and tests.

This is an OFFLINE / INTERNAL project. It makes no network calls and does not
connect to Google. Tests use only the Python standard library.

## Layout

    README.md
    pyproject.toml
    sql/schema.sql                 SQLite schema
    src/gbp/__init__.py
    src/gbp/models.py              dataclasses (Account, Location, Review, BrandProfile, ReplyDraft)
    src/gbp/fixtures.py            fixture loader helpers
    src/gbp/repo/__init__.py
    src/gbp/repo/sqlite_repo.py    Repository layer
    src/gbp/client/__init__.py
    src/gbp/client/mock_client.py  offline MockGBPClient (pagination, retry, typed errors)
    fixtures/*.json                mock accounts, locations, review pages, error payloads
    tests/test_schema_and_repo.py  unittest suite
    tests/test_mock_client.py      unittest suite (pagination, dedup, errors, backoff)

## Running tests

From the project root:

    cd /opt/data/gbp-integration
    PYTHONPATH=src python3 -m unittest discover -s tests -v

Tests use an in-memory SQLite database (`:memory:`); no external deps required.