"""F6.1 — run_readonly executes guarded SQL inside ds_<id> read-only with a timeout, never writes."""

from collections.abc import Iterator
from decimal import Decimal

import pytest

from genai_data_gen_project.config import Settings
from genai_data_gen_project.generation import engine
from genai_data_gen_project.storage import postgres
from genai_data_gen_project.storage.dataset import Dataset
from genai_data_gen_project.storage.sql_guard import SqlRejected

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def loaded(settings: Settings) -> Iterator[Dataset]:
    from tests.conftest import SAMPLE_DDLS

    ddl = SAMPLE_DDLS["restaurants"].read_text(encoding="utf-8")
    dataset = engine.generate(engine.GenerationRequest(ddl=ddl, rows_per_table=60, seed=6), None, settings)
    postgres.load_dataset(dataset, settings)
    try:
        yield dataset
    finally:
        postgres.drop_dataset(dataset.id, settings)


def test_select_count_and_join(loaded: Dataset, settings: Settings) -> None:
    result = postgres.run_readonly(loaded.id, "SELECT count(*) AS n FROM Restaurants", settings)
    assert (
        result.columns == ["n"]
        and result.rows == [[60]]
        and not result.truncated
        and result.elapsed_ms < 5000
    )
    assert result.sql.endswith("LIMIT 501") and result.tables == ["restaurants"]
    joined = postgres.run_readonly(
        loaded.id,
        'SELECT r.name, sum(o.total_amount) AS revenue FROM "Orders" o '
        "JOIN restaurants r USING (restaurant_id) GROUP BY r.name ORDER BY revenue DESC",
        settings,
        limit=5,
    )
    assert joined.columns == ["name", "revenue"] and len(joined.rows) == 5 and joined.truncated
    assert isinstance(joined.rows[0][1], Decimal)


def test_truncation_flag_and_limit_cap(loaded: Dataset, settings: Settings) -> None:
    result = postgres.run_readonly(loaded.id, "SELECT menu_id FROM menu ORDER BY menu_id", settings, limit=10)
    assert result.row_count == 10 and result.truncated and [r[0] for r in result.rows] == list(range(1, 11))
    exact = postgres.run_readonly(
        loaded.id, "SELECT menu_id FROM menu WHERE menu_id <= 10", settings, limit=10
    )
    assert exact.row_count == 10 and not exact.truncated


def test_guard_blocks_writes_before_the_database(loaded: Dataset, settings: Settings) -> None:
    with pytest.raises(SqlRejected, match="got DELETE"):
        postgres.run_readonly(loaded.id, "DELETE FROM menu", settings)
    assert postgres.run_readonly(loaded.id, "SELECT count(*) FROM menu", settings).rows == [[60]]


def test_search_path_isolates_the_dataset_and_errors_are_hinted(loaded: Dataset, settings: Settings) -> None:
    with pytest.raises(postgres.SqlError) as exc_info:
        postgres.run_readonly(loaded.id, "SELECT nope FROM menu", settings)
    assert 'column "nope" does not exist' in str(exc_info.value) and exc_info.value.sqlstate == "42703"
    with pytest.raises(postgres.SqlError, match="does not exist"):
        postgres.run_readonly(loaded.id, "SELECT * FROM information_schema_tables_nope", settings)
    # tables from other schemas are not reachable without qualification
    with pytest.raises(postgres.SqlError, match='relation "pg_namespace_copy" does not exist'):
        postgres.run_readonly(loaded.id, "SELECT * FROM pg_namespace_copy", settings)


def test_statement_timeout(loaded: Dataset, settings: Settings) -> None:
    with pytest.raises(postgres.SqlError, match="exceeded 50 ms"):
        postgres.run_readonly(
            loaded.id, "SELECT count(*) FROM generate_series(1, 200000000) g", settings, timeout_ms=50
        )
