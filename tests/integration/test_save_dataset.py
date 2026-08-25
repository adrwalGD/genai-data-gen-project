"""F5.4 — services.save_dataset persists to the registry and loads into the real PostgreSQL."""

from pathlib import Path

import pytest

from genai_data_gen_project.config import Settings
from genai_data_gen_project.generation import engine
from genai_data_gen_project.storage import datasets, postgres
from genai_data_gen_project.ui import services

pytestmark = pytest.mark.integration


def test_save_dataset_loads_into_postgres(
    sample_ddl: dict[str, str], settings: Settings, tmp_path: Path
) -> None:
    cfg = Settings(_env_file=None, database_url=settings.database_url, data_dir=tmp_path)
    request = engine.GenerationRequest(ddl=sample_ddl["restaurants"], rows_per_table=20, seed=4)
    dataset = engine.generate(request, None, cfg)
    try:
        outcome = services.save_dataset(dataset, name="save test", cfg=cfg)
        assert outcome.loaded and outcome.warning is None
        assert outcome.row_counts == dict.fromkeys(dataset.schema.table_names, 20)
        assert outcome.path == tmp_path / "datasets" / dataset.id
        assert postgres.is_loaded(dataset.id, cfg)
        assert [i.id for i in datasets.list_datasets(cfg.datasets_dir)] == [dataset.id]
        assert datasets.load(dataset.id, cfg.datasets_dir).name == "save test"
    finally:
        postgres.drop_dataset(dataset.id, cfg)
